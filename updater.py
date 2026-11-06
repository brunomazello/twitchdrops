"""
Self-updater.

Checks the latest GitHub Release of our repository. When a newer version is published,
the packaged .exe downloads it, swaps itself (a running .exe can be renamed on Windows,
just not deleted), starts the new version and closes gracefully so settings get saved.

Publishing an update = pushing a "vX.Y.Z" git tag; the release workflow builds the .exe
and attaches it to the release (see .github/workflows/release.yml).
"""
from __future__ import annotations

import os
import re
import sys
import asyncio
import hashlib
import logging
import subprocess
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

import aiohttp

from constants import IS_PACKAGED
from version import __app_version__

if TYPE_CHECKING:
    from settings import Settings


logger = logging.getLogger("TwitchDrops")

REPOSITORY = "brunomazello/twitchdrops"
LATEST_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPOSITORY}/releases/latest"
CHECK_EVERY = 6 * 3600  # seconds
FIRST_CHECK_DELAY = 45  # let the miner start up first
AFTER_UPDATE_FLAG = "--after-update"


def parse_version(text: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", text or "")
    return tuple(int(n) for n in numbers[:4]) or (0,)


def is_newer(remote: str, local: str = __app_version__) -> bool:
    return parse_version(remote) > parse_version(local)


def cleanup_old_files() -> None:
    """Removes the previous .exe left behind by an update (it can't be deleted while running)."""
    if not IS_PACKAGED:
        return
    exe = Path(sys.executable)
    for leftover in (exe.with_name(exe.name + ".old"), exe.with_name(exe.name + ".new")):
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            pass


class Updater:
    def __init__(
        self,
        settings: Settings,
        on_change: Callable[[], None],
        on_restart: Callable[[], None],
        notify: Callable[[str, str], Any],
        is_hidden: Callable[[], bool],
    ):
        self._settings = settings
        self._on_change = on_change
        self._on_restart = on_restart
        self._notify = notify
        self._is_hidden = is_hidden
        self._task: asyncio.Task[None] | None = None
        self._busy = asyncio.Lock()
        self.supported: bool = IS_PACKAGED and sys.platform == "win32"
        self.current: str = __app_version__
        self.latest: str | None = None
        self.notes: str = ""
        self.asset: dict[str, Any] | None = None
        self.status: str = ""  # human readable
        self.state: str = "idle"  # idle | checking | available | downloading | restarting | error
        self.progress: float = 0.0
        self.notified_version: str | None = None

    # info for the UI
    def snapshot(self) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "current": self.current,
            "latest": self.latest,
            "available": self.available,
            "state": self.state,
            "status": self.status,
            "progress": self.progress,
            "notes": self.notes[:1500],
            "auto": bool(self._settings.auto_update),
            "page": RELEASES_PAGE,
        }

    @property
    def available(self) -> bool:
        return bool(self.latest and is_newer(self.latest) and self.asset)

    def _set(self, state: str, status: str = "", progress: float | None = None) -> None:
        self.state = state
        self.status = status
        if progress is not None:
            self.progress = progress
        self._on_change()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        await asyncio.sleep(FIRST_CHECK_DELAY)
        while True:
            await self.check(auto_apply=True)
            await asyncio.sleep(CHECK_EVERY)

    async def check(self, *, auto_apply: bool = False) -> None:
        if self._busy.locked():
            return
        async with self._busy:
            self._set("checking", "Procurando atualização…")
            try:
                timeout = aiohttp.ClientTimeout(total=20)
                headers = {
                    "Accept": "application/vnd.github+json",
                    "User-Agent": "TwitchDropsMiner-updater",
                }
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                    async with session.get(LATEST_URL) as response:
                        if response.status == 404:
                            self._set("idle", "Nenhuma versão publicada ainda.")
                            return
                        response.raise_for_status()
                        data = await response.json()
            except Exception as exc:
                logger.debug("Update check failed", exc_info=True)
                self._set("error", f"Não foi possível procurar atualização ({type(exc).__name__}).")
                return
            self.latest = str(data.get("tag_name") or "").lstrip("vV") or None
            self.notes = str(data.get("body") or "")
            self.asset = next(
                (
                    a for a in data.get("assets") or []
                    if str(a.get("name", "")).lower().endswith(".exe")
                ),
                None,
            )
            if not self.available:
                self._set("idle", "Você está na versão mais recente.")
                return
            self._set("available", f"Versão {self.latest} disponível.")
            if self.notified_version != self.latest:
                self.notified_version = self.latest
                if not (auto_apply and self._settings.auto_update and self.supported):
                    self._notify(
                        f"A versão {self.latest} está disponível. Abra o programa para atualizar.",
                        "Atualização disponível",
                    )
        if auto_apply and self._settings.auto_update and self.supported:
            await self.apply()

    async def apply(self) -> None:
        if not self.supported:
            self._set("error", "A atualização automática só funciona no .exe.")
            return
        if not self.available or self.asset is None or self._busy.locked():
            return
        async with self._busy:
            asset = self.asset
            exe = Path(sys.executable)
            new = exe.with_name(exe.name + ".new")
            old = exe.with_name(exe.name + ".old")
            self._notify(f"Baixando a versão {self.latest}…", "Atualizando o Drops Miner")
            self._set("downloading", f"Baixando a versão {self.latest}…", 0.0)
            try:
                expected_size = int(asset.get("size") or 0)
                digest = str(asset.get("digest") or "")
                sha = hashlib.sha256()
                written = 0
                timeout = aiohttp.ClientTimeout(total=None, sock_read=60)
                headers = {"User-Agent": "TwitchDropsMiner-updater"}
                async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                    async with session.get(asset["browser_download_url"]) as response:
                        response.raise_for_status()
                        with new.open("wb") as file:
                            async for chunk in response.content.iter_chunked(256 * 1024):
                                file.write(chunk)
                                sha.update(chunk)
                                written += len(chunk)
                                if expected_size:
                                    self.progress = written / expected_size
                                    self._on_change()
                if expected_size and written != expected_size:
                    raise ValueError("download incompleto")
                if digest.startswith("sha256:") and sha.hexdigest() != digest.split(":", 1)[1]:
                    raise ValueError("arquivo corrompido (hash não confere)")
            except Exception as exc:
                logger.error(f"Update download failed: {exc}")
                new.unlink(missing_ok=True)
                self._set("error", f"Falha ao baixar a atualização: {exc}")
                return
            # swap: rename the running exe away, put the new one in its place
            try:
                old.unlink(missing_ok=True)
                exe.rename(old)
                new.rename(exe)
            except OSError as exc:
                logger.error(f"Update swap failed: {exc}")
                if not exe.exists() and old.exists():
                    old.rename(exe)
                self._set("error", f"Não foi possível trocar o arquivo: {exc}")
                return
            self._set("restarting", f"Reiniciando na versão {self.latest}…", 1.0)
            args = [str(exe), AFTER_UPDATE_FLAG]
            if self._is_hidden():
                args.append("--tray")
            flags = 0
            if sys.platform == "win32":
                flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            # start as an independent instance: without this, PyInstaller hands the new
            # process our own unpacked temp folder, which then can't be cleaned up
            env = dict(os.environ, PYINSTALLER_RESET_ENVIRONMENT="1")
            try:
                subprocess.Popen(
                    args, cwd=str(exe.parent), creationflags=flags, close_fds=True, env=env
                )
            except OSError as exc:
                logger.error(f"Couldn't start the updated version: {exc}")
                self._set("error", "Atualizado, mas não foi possível reabrir. Abra o programa de novo.")
                return
            logger.info(f"Updated to {self.latest}, restarting")
            self._on_restart()


def wait_for_lock(try_lock: Callable[[], bool], timeout: float = 60) -> bool:
    """Used right after an update: the previous instance may still be closing."""
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if try_lock():
            return True
        time.sleep(1)
    return False
