from __future__ import annotations

# import an additional thing for proper PyInstaller freeze support
from multiprocessing import freeze_support


if __name__ == "__main__":
    freeze_support()
    import io
    import os
    import sys
    import signal
    import asyncio
    import logging
    import argparse
    import warnings
    import traceback
    import tkinter as tk
    from tkinter import messagebox
    from typing import NoReturn, TYPE_CHECKING

    import truststore
    truststore.inject_into_ssl()

    from translate import _
    from twitch import Twitch
    from settings import Settings
    from version import __version__
    from exceptions import CaptchaRequired
    from utils import lock_file, resource_path, set_root_icon
    from constants import LOGGING_LEVELS, SELF_PATH, FILE_FORMATTER, LOG_PATH, LOCK_PATH

    if TYPE_CHECKING:
        from _typeshed import SupportsWrite

    warnings.simplefilter("default", ResourceWarning)

    # import tracemalloc
    # tracemalloc.start(3)

    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 or higher is required")

    # Suppress X11 Input Method registration on Linux to prevent
    # XWayland/Mutter lockups during heavy Tkinter layout updates.
    if sys.platform.startswith("linux") and "XMODIFIERS" not in os.environ:
        os.environ["XMODIFIERS"] = "@im=none"

    class Parser(argparse.ArgumentParser):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._message: io.StringIO = io.StringIO()

        def _print_message(self, message: str, file: SupportsWrite[str] | None = None) -> None:
            self._message.write(message)
            # print(message, file=self._message)

        def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
            try:
                super().exit(status, message)  # sys.exit(2)
            finally:
                messagebox.showerror("Argument Parser Error", self._message.getvalue())

    class ParsedArgs(argparse.Namespace):
        _verbose: int
        _debug_ws: bool
        _debug_gql: bool
        log: bool
        tray: bool
        dump: bool

        # TODO: replace int with union of literal values once typeshed updates
        @property
        def logging_level(self) -> int:
            return LOGGING_LEVELS[min(self._verbose, 4)]

        @property
        def debug_ws(self) -> int:
            """
            If the debug flag is True, return DEBUG.
            If the main logging level is DEBUG, return INFO to avoid seeing raw messages.
            Otherwise, return NOTSET to inherit the global logging level.
            """
            if self._debug_ws:
                return logging.DEBUG
            elif self._verbose >= 4:
                return logging.INFO
            return logging.NOTSET

        @property
        def debug_gql(self) -> int:
            if self._debug_gql:
                return logging.DEBUG
            elif self._verbose >= 4:
                return logging.INFO
            return logging.NOTSET

    # handle input parameters
    # NOTE: parser output is shown via message box
    # we also need a dummy invisible window for the parser
    root = tk.Tk()
    root.overrideredirect(True)
    root.withdraw()
    set_root_icon(root, resource_path("icons/pickaxe.ico"))
    root.update()
    parser = Parser(
        SELF_PATH.name,
        description="A program that allows you to mine timed drops on Twitch.",
    )
    parser.add_argument("--version", action="version", version=f"v{__version__}")
    parser.add_argument("-v", dest="_verbose", action="count", default=0)
    parser.add_argument("--tray", action="store_true")
    parser.add_argument("--log", action="store_true")
    parser.add_argument("--dump", action="store_true")
    # set by the self-updater when it restarts the app on a new version
    parser.add_argument("--after-update", dest="_after_update", action="store_true",
                        help=argparse.SUPPRESS)
    # undocumented debug args
    parser.add_argument(
        "--debug-ws", dest="_debug_ws", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--debug-gql", dest="_debug_gql", action="store_true", help=argparse.SUPPRESS
    )
    args = parser.parse_args(namespace=ParsedArgs())
    # load settings
    try:
        settings = Settings(args)
    except Exception:
        messagebox.showerror(
            "Settings error",
            f"There was an error while loading the settings file:\n\n{traceback.format_exc()}"
        )
        sys.exit(4)
    # dummy window isn't needed anymore
    root.destroy()
    # get rid of unneeded objects
    del root, parser

    # client run
    import threading
    import webview
    from gui import JsApi, web_path, set_title_bar_dark
    from constants import WINDOW_TITLE

    exit_code: list[int] = [0]

    async def main():
        # set language
        try:
            _.set_language(settings.language)
        except ValueError:
            # this language doesn't exist - stick to English
            pass

        # handle logging stuff
        if settings.logging_level > logging.DEBUG:
            # redirect the root logger into a NullHandler, effectively ignoring all logging calls
            # that aren't ours. This always runs, unless the main logging level is DEBUG or lower.
            logging.getLogger().addHandler(logging.NullHandler())
        logger = logging.getLogger("TwitchDrops")
        logger.setLevel(settings.logging_level)
        if settings.log:
            handler = logging.FileHandler(LOG_PATH)
            handler.setFormatter(FILE_FORMATTER)
            logger.addHandler(handler)
        logging.getLogger("TwitchDrops.gql").setLevel(settings.debug_gql)
        logging.getLogger("TwitchDrops.websocket").setLevel(settings.debug_ws)

        exit_status = 0
        client = Twitch(settings)
        _client_ref.append(client)
        try:
            await client.run()
        except CaptchaRequired:
            exit_status = 1
            client.prevent_close()
            client.print(_("error", "captcha"))
        except Exception:
            exit_status = 1
            client.prevent_close()
            client.print("Fatal error encountered:\n")
            client.print(traceback.format_exc())
        finally:
            client.print(_("gui", "status", "exiting"))
            await client.shutdown()
        if not client.gui.close_requested:
            # user didn't request the closure
            client.gui.tray.change_icon("error")
            client.print(_("status", "terminated"))
            client.gui.status.update(_("gui", "status", "terminated"))
            # notify the user about the closure
            client.gui.grab_attention(sound=True)
        await client.gui.wait_until_closed()
        # save the application state
        # NOTE: we have to do it after wait_until_closed,
        # because the user can alter some settings between app termination and closing the window
        client.save(force=True)
        # give the GUI a moment to flush the last updates
        await asyncio.sleep(0.2)
        client.gui.stop()
        client.gui.close_window()
        exit_code[0] = exit_status

    _client_ref: list[Twitch] = []
    _closing_allowed = threading.Event()

    def miner_thread():
        try:
            asyncio.run(main())
        except BaseException:
            traceback.print_exc()
        finally:
            _closing_allowed.set()
            for window in list(webview.windows):
                try:
                    window.destroy()
                except Exception:
                    pass

    def on_closing() -> bool:
        # X button: ask the miner to shut down gracefully instead of killing the window
        if _closing_allowed.is_set() or not _client_ref:
            return True
        client = _client_ref[0]
        loop = client.gui._loop
        loop.call_soon_threadsafe(client.gui.close)
        return False

    def webview2_installed() -> bool:
        """The Edge WebView2 runtime renders the interface (built into Windows 11)."""
        if sys.platform != "win32":
            return True
        import winreg
        client = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        locations = (
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\{client}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\{client}"),
            (winreg.HKEY_CURRENT_USER, rf"Software\{client}"),
        )
        for hive, path in locations:
            try:
                with winreg.OpenKey(hive, path) as key:
                    version, _ = winreg.QueryValueEx(key, "pv")
                    if version and version != "0.0.0.0":
                        return True
            except OSError:
                continue
        return False

    def ask_install_webview2() -> None:
        import ctypes
        import webbrowser
        MB_YESNO, MB_ICONWARNING, IDYES = 0x04, 0x30, 6
        answer = ctypes.windll.user32.MessageBoxW(
            None,
            "Falta um componente gratuito da Microsoft para mostrar a interface do "
            "Twitch Drops Miner (Microsoft Edge WebView2).\n\n"
            "Ele já vem no Windows 11, mas alguns Windows 10 não têm.\n\n"
            "Quer baixar agora? Depois de instalar, abra o programa de novo.",
            "Twitch Drops Miner",
            MB_YESNO | MB_ICONWARNING,
        )
        if answer == IDYES:
            webbrowser.open("https://go.microsoft.com/fwlink/p/?LinkId=2124703")

    def on_shown():
        set_title_bar_dark(settings.dark_mode)

    def on_minimized():
        # minimize button: hide into the system tray instead of the taskbar (if enabled)
        if sys.platform == "darwin" or not _client_ref or not settings.minimize_to_tray:
            return
        client = _client_ref[0]
        client.gui._loop.call_soon_threadsafe(client.gui.tray.minimize)

    try:
        # use lock_file to check if we're not already running
        from updater import cleanup_old_files, wait_for_lock
        success, file = lock_file(LOCK_PATH)
        if not success and getattr(args, "_after_update", False):
            # right after an update, the previous version may still be saving and closing
            file.close()

            def try_lock() -> bool:
                global file
                try:
                    ok, file = lock_file(LOCK_PATH)
                except OSError:
                    return False
                if not ok:
                    file.close()
                return ok

            success = wait_for_lock(try_lock)
        if not success:
            # already running: ask that copy to show its window, then exit
            try:
                from constants import WAKE_PATH
                WAKE_PATH.touch()
                if sys.platform == "win32":
                    import ctypes
                    # we hold the user's click, so let the running copy take the foreground
                    ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
            except Exception:
                pass
            sys.exit(3)
        cleanup_old_files()

        if not webview2_installed():
            ask_install_webview2()
            sys.exit(5)

        start_hidden = bool(settings.tray) and sys.platform != "darwin"
        window = webview.create_window(
            WINDOW_TITLE,
            web_path("index.html"),
            js_api=JsApi(),
            width=1280,
            height=820,
            min_size=(900, 620),
            hidden=start_hidden,
            background_color="#0e0e10" if settings.dark_mode else "#f3f3f6",
            text_select=True,
        )
        window.events.closing += on_closing
        window.events.shown += on_shown
        window.events.minimized += on_minimized
        # persistent WebView2 data folder (a temporary one can get cleaned up mid-run)
        webview_storage = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"),
            "TwitchDropsMiner", "webview",
        )
        os.makedirs(webview_storage, exist_ok=True)
        # the interface is served from the same local address on every run, so WebView2
        # would keep showing cached files from the previous version after an update:
        # drop its HTTP cache whenever the version changes (and always when running from source)
        from version import __app_version__
        import shutil
        marker = os.path.join(webview_storage, "ui-version.txt")
        try:
            with open(marker, encoding="utf8") as f:
                cached_version = f.read().strip()
        except OSError:
            cached_version = ""
        if cached_version != __app_version__ or not getattr(sys, "frozen", False):
            for sub in ("Cache", "Code Cache", "Service Worker"):
                shutil.rmtree(
                    os.path.join(webview_storage, "EBWebView", "Default", sub), ignore_errors=True
                )
            try:
                with open(marker, "w", encoding="utf8") as f:
                    f.write(__app_version__)
            except OSError:
                pass
        webview.start(
            miner_thread,
            gui="edgechromium" if sys.platform == "win32" else None,
            icon=str(resource_path("icons/pickaxe.ico")),
            private_mode=False,
            storage_path=webview_storage,
            debug=bool(os.environ.get("TDM_UI_DEBUG")),
        )
        sys.exit(exit_code[0])
    finally:
        file.close()
