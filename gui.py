"""
Modern GUI for Twitch Drops Miner.

The interface itself is HTML/CSS/JS (see the ``web`` folder), rendered inside a native
desktop window by pywebview (Edge WebView2 on Windows) - no external browser involved.

This module keeps the exact same public API the rest of the miner used with the old
Tkinter GUI (``GUIManager`` and its ``status``, ``login``, ``progress``, ``channels``,
``inv``, ``settings``, ``help``, ``tray`` and ``websockets`` members), so the mining
logic didn't have to change. Every component here just keeps its state and pushes
updates to the web page, which renders them.

Threading model:
• The main thread runs the pywebview GUI loop (required by Windows).
• The asyncio event loop (the whole miner) runs in a worker thread.
• Python -> JS: updates are queued and flushed in small batches via ``window.run_js``.
• JS -> Python: ``JsApi`` methods hop back onto the asyncio loop, never blocking.
"""
from __future__ import annotations

import os
import re
import sys
import json
import shlex
import ctypes
import asyncio
import logging
import plistlib
import threading
from pathlib import Path
from collections import abc, deque
from textwrap import dedent
from datetime import datetime, timezone
from typing import Any, NoReturn, TYPE_CHECKING

import pystray
import webview
from yarl import URL
from PIL import Image as Image_module

if sys.platform == "win32":
    import winsound
    from registry import RegistryKey, ValueType, ValueNotFound

from translate import _
from remote import MobileServer, qr_svg
from exceptions import MinerException, ExitRequest
from utils import resource_path, webopen, task_wrapper, Game, _T
from constants import (
    SELF_PATH,
    IS_PACKAGED,
    SCRIPTS_PATH,
    WINDOW_TITLE,
    LOGGING_LEVELS,
    MAX_WEBSOCKETS,
    WS_TOPICS_LIMIT,
    OUTPUT_FORMATTER,
    State,
    PriorityMode,
)

if TYPE_CHECKING:
    from twitch import Twitch
    from channel import Channel
    from settings import Settings
    from inventory import DropsCampaign, TimedDrop


logger = logging.getLogger("TwitchDrops")

# Shared between the main (GUI) thread and the asyncio thread.
_shared: dict[str, Any] = {
    "manager": None,  # GUIManager, once created
    "loop": None,  # asyncio loop the manager lives in
    "js_ready": False,  # set when the page called api.ready() before the manager existed
}


def web_path(*parts: str) -> str:
    return str(resource_path(os.path.join("web", *parts)))


_SYMBOLS_RE = re.compile(r"\s*[✔✓❌✖✗✅⏳]️?\s*")


def clean(text: str) -> str:
    """Strip the check/cross emoji the old Tk GUI baked into translations."""
    return _SYMBOLS_RE.sub(" ", text).strip()


def _fmt_time(dt: datetime) -> str:
    return dt.astimezone().strftime("%d/%m %H:%M")


def _fmt_remaining(minutes: int, seconds: int) -> str:
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}"


######################
# JS -> PYTHON BRIDGE #
######################


class JsApi:
    """
    Exposed to the page as ``window.pywebview.api``.
    Called from pywebview's threads - every call is forwarded onto the asyncio loop
    and returns immediately, so the UI thread never waits on the miner.
    """

    def _run(self, name: str, *args: Any) -> None:
        manager: GUIManager | None = _shared["manager"]
        loop: asyncio.AbstractEventLoop | None = _shared["loop"]
        if manager is None or loop is None:
            if name == "on_ready":
                _shared["js_ready"] = True
            return
        loop.call_soon_threadsafe(manager.handle_js, name, args)

    # lifecycle
    def ready(self) -> None:
        self._run("on_ready")

    # login
    def login_press(self) -> None:
        self._run("login_press")

    def login_open_window(self) -> None:
        self._run("login_open_window")

    # channels
    def select_channel(self, iid: str | None) -> None:
        self._run("select_channel", iid)

    def switch_channel(self) -> None:
        self._run("switch_channel")

    # inventory
    def inv_refresh(self) -> None:
        self._run("inv_refresh")

    # settings
    def set_setting(self, name: str, value: Any) -> None:
        self._run("set_setting", name, value)

    def set_proxy(self, value: str) -> None:
        self._run("set_proxy", value)

    def priority_add(self, name: str) -> None:
        self._run("priority_add", name)

    def priority_move(self, index: int, amount: int) -> None:
        self._run("priority_move", index, amount)

    def priority_delete(self, index: int) -> None:
        self._run("priority_delete", index)

    def exclude_add(self, name: str) -> None:
        self._run("exclude_add", name)

    def exclude_delete(self, name: str) -> None:
        self._run("exclude_delete", name)

    def reload(self) -> None:
        self._run("reload")

    # misc
    def invalidate(self) -> None:
        self._run("invalidate")

    def open_url(self, url: str) -> None:
        if isinstance(url, str) and url.startswith("https://"):
            webopen(url)

    def minimize_to_tray(self) -> None:
        self._run("minimize_to_tray")

    def mobile_new_token(self) -> None:
        self._run("mobile_new_token")

    def test_notification(self) -> None:
        self._run("test_notification")


##################
# GUI COMPONENTS #
##################


class _OutputHandler(logging.Handler):
    def __init__(self, manager: GUIManager):
        super().__init__()
        self._manager = manager

    def emit(self, record):
        self._manager.print(self.format(record))


class StatusBar:
    def __init__(self, manager: GUIManager):
        self._manager = manager
        self._text: str = ''

    def update(self, text: str):
        self._text = text
        self._manager.emit("status", text, key="status")

    def clear(self):
        self.update('')

    def snapshot(self) -> str:
        return self._text


class WebsocketStatus:
    def __init__(self, manager: GUIManager):
        self._manager = manager
        self._items: dict[int, dict[str, Any] | None] = {i: None for i in range(MAX_WEBSOCKETS)}

    def update(self, idx: int, status: str | None = None, topics: int | None = None):
        if status is None and topics is None:
            raise TypeError("You need to provide at least one of: status, topics")
        entry = self._items.get(idx)
        if entry is None:
            entry = self._items[idx] = {
                "status": _("gui", "websocket", "disconnected"), "topics": 0
            }
        if status is not None:
            entry["status"] = status
        if topics is not None:
            entry["topics"] = topics
        self._push()

    def remove(self, idx: int):
        if idx in self._items:
            del self._items[idx]
            self._push()

    def snapshot(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for idx in range(MAX_WEBSOCKETS):
            item = self._items.get(idx)
            if item is not None:
                result.append({
                    "id": idx + 1,
                    "status": item["status"],
                    "topics": item["topics"],
                    "limit": WS_TOPICS_LIMIT,
                })
        return result

    def _push(self):
        self._manager.emit("ws", self.snapshot(), key="ws")


class LoginData:
    def __init__(self, username: str, password: str, token: str):
        self.username = username
        self.password = password
        self.token = token


class LoginForm:
    """
    Login is done inside the app: a separate native window shows Twitch's login page,
    and the token is picked up from the redirect URL once the user logs in.
    """
    TOKEN_URL_PREFIX = "https://www.twitch.tv/#access_token="

    def __init__(self, manager: GUIManager):
        self._manager = manager
        self._confirm = asyncio.Event()
        self._status: str = _("gui", "login", "logged_out")
        self._user_id: int | None = None
        self._awaiting_press: bool = False
        self._mode: str | None = None  # None | "browser" | "code"
        self._code: str = ''
        self._code_url: str = ''
        self._login_window: webview.Window | None = None

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self._status,
            "user_id": self._user_id,
            "logged_in": self._user_id is not None,
            "awaiting_press": self._awaiting_press,
            "mode": self._mode,
            "code": self._code,
            "code_url": self._code_url,
        }

    def _push(self):
        self._manager.emit("login", self.snapshot(), key="login")

    def clear(self, login: bool = False, password: bool = False, token: bool = False):
        # no text entries to clear in this GUI
        pass

    async def wait_for_login_press(self) -> None:
        self._confirm.clear()
        self._awaiting_press = True
        self._push()
        try:
            await self._manager.coro_unless_closed(self._confirm.wait())
        finally:
            self._awaiting_press = False
            self._push()

    def press(self) -> None:
        self._confirm.set()

    async def ask_login(self) -> LoginData:
        # Username/password login isn't used by the miner anymore - kept for API compatibility.
        raise MinerException("Username/password login is not supported")

    async def ask_enter_code(self, page_url: URL, user_code: str) -> None:
        self.update(_("gui", "login", "required"), None)
        self._mode = "code"
        self._code = user_code
        self._code_url = str(page_url)
        self._push()
        self._manager.grab_attention(sound=False)
        self._manager.tray.notify(
            f"Código: {user_code}", "Login necessário"
        )

    async def browser_login(self, url: str) -> str | None:
        """
        Opens Twitch's login page in a native window owned by this app.
        Returns the access token once the user logs in, or None if they closed the window.
        """
        loop = asyncio.get_running_loop()
        self.update(_("gui", "login", "required"), None)
        self._mode = "browser"
        self._push()
        self._manager.grab_attention(sound=False)
        closed = threading.Event()
        window = webview.create_window(
            "Entrar na Twitch",
            url,
            width=500,
            height=780,
            min_size=(400, 600),
            background_color="#0e0e10",
        )
        window.events.closed += closed.set
        self._login_window = window
        try:
            while not closed.is_set():
                await asyncio.sleep(0.5)
                try:
                    current: str | None = await loop.run_in_executor(
                        None, window.get_current_url
                    )
                except Exception:
                    continue
                if current and current.startswith(self.TOKEN_URL_PREFIX):
                    match = re.search(r'#access_token=(\w+)', current, re.I)
                    if match is not None:
                        return match.group(1)
            return None
        finally:
            self._login_window = None
            if not closed.is_set():
                await loop.run_in_executor(None, window.destroy)
            self._mode = None
            self._push()

    def focus_login_window(self) -> None:
        if self._login_window is not None:
            try:
                self._login_window.show()
                self._login_window.restore()
            except Exception:
                pass
        elif self._mode == "code" and self._code_url:
            # device code flow - open the activation page in an app window
            webview.create_window(
                "Ativar dispositivo", self._code_url, width=500, height=780,
                background_color="#0e0e10",
            )

    def update(self, status: str, user_id: int | None):
        self._status = status
        self._user_id = user_id
        if user_id is not None:
            self._mode = None
            self._code = ''
            self._code_url = ''
        self._push()


class CampaignProgress:
    ALMOST_DONE_SECONDS = 10

    def __init__(self, manager: GUIManager):
        self._manager = manager
        self._drop: TimedDrop | None = None
        self._seconds: int = 0
        self._timer_task: asyncio.Task[None] | None = None

    def snapshot(self) -> dict[str, Any] | None:
        drop = self._drop
        if drop is None:
            return None
        campaign = drop.campaign
        seconds = self._seconds % 60

        def remaining(minutes: int) -> str:
            if self._seconds < 60 and minutes > 0:
                minutes -= 1
            return _fmt_remaining(max(minutes, 0), seconds)

        return {
            "game": campaign.game.name,
            "campaign": campaign.name,
            "campaign_image": str(campaign.image_url),
            "campaign_progress": campaign.progress,
            "campaign_remaining": remaining(campaign.remaining_minutes),
            "claimed": campaign.claimed_drops,
            "total": campaign.total_drops,
            "ends": _fmt_time(campaign.ends_at),
            "rewards": drop.rewards_text(),
            "benefits": [
                {"name": b.name, "image": str(b.image_url)} for b in drop.benefits
            ],
            "drop_progress": drop.progress,
            "drop_remaining": remaining(drop.remaining_minutes),
            "drop_minutes": drop.current_minutes,
            "drop_required": drop.required_minutes,
            "segments": [
                {
                    "name": d.rewards_text(),
                    "state": (
                        "claimed" if d.is_claimed
                        else "current" if d.id == drop.id
                        else "ready" if d.can_claim
                        else "queued"
                    ),
                    "progress": d.progress,
                }
                for d in campaign.drops
                if d.required_minutes > 0
            ],
        }

    def _push(self):
        self._manager.emit("progress", self.snapshot(), key="progress")

    def _update_time(self, seconds: int | None = None):
        if seconds is not None:
            self._seconds = seconds
        self._push()

    async def _timer_loop(self):
        self._update_time(60)
        while self._seconds > 0:
            await asyncio.sleep(1)
            self._seconds -= 1
            self._update_time()
        self._timer_task = None

    def start_timer(self):
        if self._timer_task is None:
            if self._drop is None or self._drop.remaining_minutes <= 0:
                self._update_time(60)
            else:
                self._timer_task = asyncio.create_task(self._timer_loop())

    def stop_timer(self):
        if self._timer_task is not None:
            self._timer_task.cancel()
            self._timer_task = None

    def minute_almost_done(self) -> bool:
        return self._timer_task is None or self._seconds <= self.ALMOST_DONE_SECONDS

    def display(self, drop: TimedDrop | None, *, countdown: bool = True, subone: bool = False):
        self._drop = drop
        self.stop_timer()
        if drop is None:
            self._update_time(0)
            return
        if countdown:
            self.start_timer()
        elif subone:
            self._update_time(0)
        else:
            self._update_time(60)


class ConsoleOutput:
    MAX_LINES = 500

    def __init__(self, manager: GUIManager):
        self._manager = manager
        self._lines: deque[str] = deque(maxlen=self.MAX_LINES)

    def print(self, message: str):
        stamp = datetime.now().strftime("%H:%M:%S")
        for line in message.split('\n'):
            entry = f"{stamp}  {line}"
            self._lines.append(entry)
            self._manager.emit("log", entry)

    def snapshot(self) -> list[str]:
        return list(self._lines)


class ChannelList:
    def __init__(self, manager: GUIManager):
        self._manager = manager
        self._channel_map: dict[str, Channel] = {}
        self._order: list[str] = []
        self._watching: str | None = None
        self._selected: str | None = None

    def _row(self, channel: Channel) -> dict[str, Any]:
        if channel.online:
            status, status_key = clean(_("gui", "channels", "online")), "online"
        elif channel.pending_online:
            status, status_key = clean(_("gui", "channels", "pending")), "pending"
        else:
            status, status_key = clean(_("gui", "channels", "offline")), "offline"
        return {
            "iid": str(channel.iid),
            "name": channel.name,
            "status": status,
            "status_key": status_key,
            "game": str(channel.game or ''),
            "drops": bool(channel.drops_enabled),
            "viewers": channel.viewers,
            "acl": bool(channel.acl_based),
        }

    def snapshot(self) -> dict[str, Any]:
        return {
            "rows": [self._row(self._channel_map[iid]) for iid in self._order],
            "watching": self._watching,
            "selected": self._selected,
        }

    def _push(self):
        self._manager.emit("channels", self.snapshot(), key="channels")

    def select(self, iid: str | None) -> None:
        self._selected = iid if iid in self._channel_map else None
        self._push()

    def clear_watching(self):
        self._watching = None
        self._push()

    def set_watching(self, channel: Channel):
        self._watching = str(channel.iid)
        self._push()

    def get_selection(self) -> Channel | None:
        if self._selected is None:
            return None
        return self._channel_map.get(self._selected)

    def clear_selection(self):
        self._selected = None
        self._push()

    def clear(self):
        self._channel_map.clear()
        self._order.clear()
        self._selected = None
        self._push()

    def display(self, channel: Channel, *, add: bool = False):
        iid = str(channel.iid)
        if iid not in self._channel_map:
            if not add:
                return
            self._channel_map[iid] = channel
            self._order.append(iid)
        self._push()

    def remove(self, channel: Channel):
        iid = str(channel.iid)
        if iid in self._channel_map:
            del self._channel_map[iid]
            self._order.remove(iid)
            if self._selected == iid:
                self._selected = None
            if self._watching == iid:
                self._watching = None
            self._push()


class TrayIcon:
    """
    System tray icon. On Windows it stays in the tray the whole time, so that
    drop claim alerts can always be shown as native Windows notifications.
    """
    TITLE = "Twitch Drops Miner"

    def __init__(self, manager: GUIManager):
        self._manager = manager
        self.icon: pystray.Icon | None = None  # type: ignore[unused-ignore]
        self._icon_images: dict[str, Image_module.Image] = {
            "pickaxe": Image_module.open(resource_path("icons/pickaxe.ico")),
            "active": Image_module.open(resource_path("icons/active.ico")),
            "idle": Image_module.open(resource_path("icons/idle.ico")),
            "error": Image_module.open(resource_path("icons/error.ico")),
            "maint": Image_module.open(resource_path("icons/maint.ico")),
        }
        self._icon_state: str = "pickaxe"
        self._drop: TimedDrop | None = None

    def _shorten(self, text: str, by_len: int, min_len: int) -> str:
        if (text_len := len(text)) <= min_len + 3 or by_len <= 0:
            return text
        return text[:-min(by_len + 3, text_len - min_len)] + "..."

    def get_title(self, drop: TimedDrop | None) -> str:
        if drop is None:
            return self.TITLE
        campaign = drop.campaign
        title_parts: list[str] = [
            f"{self.TITLE}\n",
            f"{campaign.game.name}\n",
            drop.rewards_text(),
            f" {drop.progress:.1%} ({campaign.claimed_drops}/{campaign.total_drops})"
        ]
        min_len: int = 30
        max_len: int = 127
        missing_len = len(''.join(title_parts)) - max_len
        if missing_len > 0:
            title_parts[2] = self._shorten(title_parts[2], missing_len, min_len)
            missing_len = len(''.join(title_parts)) - max_len
        if missing_len > 0:
            title_parts[1] = self._shorten(title_parts[1], missing_len, min_len)
            missing_len = len(''.join(title_parts)) - max_len
        if missing_len > 0:
            return self.TITLE
        return ''.join(title_parts)

    def start(self):
        if sys.platform == "darwin" or self.icon is not None:
            return
        loop = asyncio.get_running_loop()

        def bridge(func):
            return lambda: loop.call_soon_threadsafe(func)

        menu = pystray.Menu(
            pystray.MenuItem("Abrir", bridge(self.restore), default=True),
            pystray.MenuItem("Minimizar para a bandeja", bridge(self.minimize)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Sair", bridge(self.quit)),
        )
        self.icon = pystray.Icon(
            "twitch_miner",
            self._icon_images[self._icon_state],
            self.get_title(self._drop),
            menu,
        )
        loop.run_in_executor(None, self.icon.run)

    def stop(self):
        if self.icon is not None:
            self.icon.stop()
            self.icon = None

    def quit(self):
        self._manager.close()

    def minimize(self):
        if sys.platform == "darwin":
            return
        self.start()
        self._manager.window_call("hide")

    def restore(self):
        self._manager.window_call("show")
        self._manager.window_call("restore")

    def notify(
        self, message: str, title: str | None = None, duration: float = 10
    ) -> asyncio.Task[None] | None:
        if not self._manager._twitch.settings.tray_notifications:
            return None
        if self.icon is None:
            return None
        icon = self.icon

        async def notifier():
            try:
                icon.notify(message, title)
                await asyncio.sleep(duration)
                icon.remove_notification()
            except Exception:
                logger.exception("Notification failed")

        return asyncio.create_task(notifier())

    def update_title(self, drop: TimedDrop | None):
        self._drop = drop
        if self.icon is not None:
            self.icon.title = self.get_title(drop)

    def change_icon(self, state: str):
        if state not in self._icon_images:
            raise ValueError("Invalid icon state")
        self._icon_state = state
        if self.icon is not None:
            self.icon.icon = self._icon_images[state]
        self._manager.emit("state", state, key="state")


class InventoryOverview:
    def __init__(self, manager: GUIManager):
        self._manager = manager
        self._campaigns: dict[str, DropsCampaign] = {}
        self._drop_campaign: dict[str, str] = {}
        self._claimed_ids: set[str] = set()
        self.session_claimed: int = 0
        # campaigns we already warned about (not linked) - one notification per campaign
        self._link_notified: set[str] = set()

    @staticmethod
    def _status(campaign: DropsCampaign) -> tuple[str, str]:
        if campaign.active:
            return clean(_("gui", "inventory", "status", "active")), "active"
        elif campaign.upcoming:
            return clean(_("gui", "inventory", "status", "upcoming")), "upcoming"
        return clean(_("gui", "inventory", "status", "expired")), "expired"

    @staticmethod
    def _drop_state(drop: TimedDrop) -> dict[str, Any]:
        state: str
        text: str
        extra: str = ''
        if drop.is_claimed:
            state, text = "claimed", clean(_("gui", "inventory", "status", "claimed"))
        elif drop.can_claim:
            state, text = "ready", clean(_("gui", "inventory", "status", "ready_to_claim"))
        elif drop.current_minutes or drop.can_earn():
            state = "progress"
            text = f"{drop.current_minutes}/{drop.required_minutes} min"
            if drop.ends_at < drop.campaign.ends_at:
                extra = _("gui", "inventory", "ends").format(time=_fmt_time(drop.ends_at))
        else:
            state = "locked"
            text = f"{drop.required_minutes} min" if drop.required_minutes > 0 else "Sub"
            if datetime.now(timezone.utc) < drop.starts_at > drop.campaign.starts_at:
                extra = _("gui", "inventory", "starts").format(time=_fmt_time(drop.starts_at))
            elif drop.ends_at < drop.campaign.ends_at:
                extra = _("gui", "inventory", "ends").format(time=_fmt_time(drop.ends_at))
        return {
            "id": drop.id,
            "state": state,
            "text": text,
            "extra": extra,
            "progress": drop.progress,
            "benefits": [
                {"name": b.name, "image": str(b.image_url)} for b in drop.benefits
            ],
        }

    def _campaign_dict(self, campaign: DropsCampaign) -> dict[str, Any]:
        status_text, status_key = self._status(campaign)
        acl = campaign.allowed_channels
        return {
            "id": campaign.id,
            "name": campaign.name,
            "game": campaign.game.name,
            "image": str(campaign.image_url),
            "status": status_text,
            "status_key": status_key,
            "starts": _fmt_time(campaign.starts_at),
            "ends": _fmt_time(campaign.ends_at),
            "eligible": campaign.eligible,
            "link_url": campaign.link_url,
            "channels": [ch.name for ch in acl],
            "finished": campaign.finished,
            "sub_only": campaign.required_minutes <= 0,
            "claimed": campaign.claimed_drops,
            "total": campaign.total_drops,
            "progress": campaign.progress,
            "drops": [self._drop_state(drop) for drop in campaign.drops],
        }

    def snapshot(self) -> list[dict[str, Any]]:
        return [self._campaign_dict(c) for c in self._campaigns.values()]

    def refresh(self):
        self._manager.emit("inventory", self.snapshot(), key="inventory")
        self.push_alerts()

    async def add_campaign(self, campaign: DropsCampaign) -> None:
        self._campaigns[campaign.id] = campaign
        for drop in campaign.drops:
            self._drop_campaign[drop.id] = campaign.id
            if drop.is_claimed:
                self._claimed_ids.add(drop.id)
        self._manager.emit(
            "campaign", self._campaign_dict(campaign), key=f"campaign:{campaign.id}"
        )
        if self._needs_link(campaign):
            self.push_alerts()
            if campaign.id not in self._link_notified:
                self._link_notified.add(campaign.id)
                self._manager.print(
                    f"A campanha \"{campaign.name}\" de {campaign.game.name} está ativa, "
                    "mas sua conta do jogo não está vinculada à Twitch - ela não será minerada."
                )
                self._manager.tray.notify(
                    f"{campaign.game.name}: vincule sua conta para minerar \"{campaign.name}\".",
                    "Campanha não vinculada",
                )

    def _needs_link(self, campaign: DropsCampaign) -> bool:
        """
        True for an active campaign we'd want to mine, that can't be mined
        only because the game account isn't linked to Twitch.
        """
        if campaign.linked or campaign.has_badge_or_emote:
            return False
        if not campaign.active or campaign.finished or campaign.required_minutes <= 0:
            return False
        settings = self._manager._twitch.settings
        game = campaign.game.name
        if settings.priority_mode is PriorityMode.PRIORITY_ONLY:
            return game in settings.priority
        return game not in settings.exclude

    def link_alerts(self) -> list[dict[str, Any]]:
        priority = list(self._manager._twitch.settings.priority)
        alerts = [
            {
                "id": c.id,
                "game": c.game.name,
                "name": c.name,
                "image": str(c.image_url),
                "link_url": c.link_url,
                "ends": _fmt_time(c.ends_at),
                "priority": priority.index(c.game.name) if c.game.name in priority else 999,
            }
            for c in self._campaigns.values()
            if self._needs_link(c)
        ]
        alerts.sort(key=lambda a: (a["priority"], a["game"]))
        return alerts

    def push_alerts(self) -> None:
        self._manager.emit("alerts", self.link_alerts(), key="alerts")

    def clear(self) -> None:
        self._campaigns.clear()
        self._drop_campaign.clear()
        self._manager.emit("inventory", [], key="inventory")
        self.push_alerts()

    def update_drop(self, drop: TimedDrop) -> None:
        campaign_id = self._drop_campaign.get(drop.id)
        if campaign_id is None:
            return
        campaign = self._campaigns[campaign_id]
        if drop.is_claimed and drop.id not in self._claimed_ids:
            self._claimed_ids.add(drop.id)
            self.session_claimed += 1
        self._manager.emit(
            "campaign", self._campaign_dict(campaign), key=f"campaign:{campaign.id}"
        )


class SettingsPanel:
    AUTOSTART_NAME: str = "TwitchDropsMiner"
    AUTOSTART_KEY: str = "HKCU/Software/Microsoft/Windows/CurrentVersion/Run"
    BOOL_SETTINGS = (
        "dark_mode", "tray_notifications", "enable_badges_emotes", "available_drops_check",
    )

    def __init__(self, manager: GUIManager):
        self._manager = manager
        self._settings: Settings = manager._twitch.settings
        if self._settings.priority_mode not in self.priority_modes():
            self._settings.priority_mode = PriorityMode.PRIORITY_ONLY
        self._game_names: set[str] = set()
        try:
            self._autostart: bool = self._query_autostart()
        except Exception:
            self._autostart = False

    @staticmethod
    def priority_modes() -> dict[PriorityMode, str]:
        return {
            PriorityMode.PRIORITY_ONLY: _("gui", "settings", "priority_modes", "priority_only"),
            PriorityMode.ENDING_SOONEST: _("gui", "settings", "priority_modes", "ending_soonest"),
            PriorityMode.LOW_AVBL_FIRST: _(
                "gui", "settings", "priority_modes", "low_availability"
            ),
        }

    def snapshot(self) -> dict[str, Any]:
        s = self._settings
        return {
            "language": _.current,
            "languages": list(_.languages),
            "autostart": self._autostart,
            "autostart_tray": bool(s.autostart_tray),
            "tray_notifications": bool(s.tray_notifications),
            "dark_mode": bool(s.dark_mode),
            "enable_badges_emotes": bool(s.enable_badges_emotes),
            "available_drops_check": bool(s.available_drops_check),
            "priority_mode": s.priority_mode.name,
            "priority_modes": [
                {"value": mode.name, "label": label}
                for mode, label in self.priority_modes().items()
            ],
            "proxy": str(s.proxy),
            "priority": list(s.priority),
            "exclude": sorted(s.exclude),
            "games": sorted(self._game_names),
            "mobile": self._manager.mobile_info(),
        }

    def _push(self):
        self._manager.emit("settings", self.snapshot(), key="settings")

    def set_setting(self, name: str, value: Any) -> None:
        if name in self.BOOL_SETTINGS:
            setattr(self._settings, name, bool(value))
        elif name == "language" and value in _.languages:
            self._settings.language = value
        elif name == "priority_mode":
            for mode in self.priority_modes():
                if mode.name == value:
                    self._settings.priority_mode = mode
                    break
        elif name == "autostart":
            self._autostart = bool(value)
            self.update_autostart()
        elif name == "mobile_access":
            self._settings.mobile_access = bool(value)
            self._manager.mobile_apply()
        elif name == "autostart_tray":
            self._settings.autostart_tray = bool(value)
            self.update_autostart()
        else:
            return
        self._push()

    def set_proxy(self, raw_url: str) -> None:
        raw_url = (raw_url or '').strip()
        url = URL(raw_url) if raw_url else URL()
        if raw_url and (url.host is None or url.port is None):
            self._manager.print("Proxy inválido - use o formato http://usuario:senha@host:porta")
            url = URL()
        self._settings.proxy = url
        self._push()

    # autostart
    def _get_self_path(self) -> str:
        return f'"{SELF_PATH.resolve()!s}"'

    def _get_autostart_path(self) -> str:
        flags: list[str] = []
        for lvl_idx, lvl_value in LOGGING_LEVELS.items():
            if lvl_value == self._settings.logging_level:
                if lvl_idx > 0:
                    flags.append(f"-{'v' * lvl_idx}")
                break
        if self._settings.autostart_tray:
            flags.append("--tray")
        if not IS_PACKAGED:
            return f"\"{SCRIPTS_PATH / 'pythonw'!s}\" {self._get_self_path()} {' '.join(flags)}"
        return f"{self._get_self_path()} {' '.join(flags)}"

    def _get_linux_autostart_filepath(self) -> Path:
        autostart_folder: Path = Path("~/.config/autostart").expanduser()
        if (config_home := os.environ.get("XDG_CONFIG_HOME")) is not None:
            config_autostart: Path = Path(config_home, "autostart").expanduser()
            if config_autostart.exists():
                autostart_folder = config_autostart
        return autostart_folder / f"{self.AUTOSTART_NAME}.desktop"

    def _get_mac_autostart_filepath(self) -> Path:
        return Path(
            Path.home(), f"Library/LaunchAgents/com.devilxd.{self.AUTOSTART_NAME.lower()}.plist"
        )

    def _query_autostart(self) -> bool:
        if sys.platform == "win32":
            with RegistryKey(self.AUTOSTART_KEY, read_only=True) as key:
                try:
                    value_type, value = key.get(self.AUTOSTART_NAME)
                except ValueNotFound:
                    return False
                return value_type is ValueType.REG_SZ and self._get_self_path() in value
        elif sys.platform == "linux":
            autostart_file: Path = self._get_linux_autostart_filepath()
            if not autostart_file.exists():
                return False
            with autostart_file.open('r', encoding="utf8") as file:
                return self._get_self_path() in file.read()
        elif sys.platform == "darwin":
            plist_file = self._get_mac_autostart_filepath()
            if not plist_file.exists():
                return False
            with plist_file.open('r', encoding="utf8") as file:
                return str(SELF_PATH.resolve()) in file.read()
        return False

    def update_autostart(self) -> None:
        enabled = self._autostart
        if sys.platform == "win32":
            with RegistryKey(self.AUTOSTART_KEY) as key:
                if enabled:
                    key.set(self.AUTOSTART_NAME, ValueType.REG_SZ, self._get_autostart_path())
                else:
                    key.delete(self.AUTOSTART_NAME, silent=True)
        elif sys.platform == "linux":
            autostart_file: Path = self._get_linux_autostart_filepath()
            if enabled:
                file_contents: str = dedent(
                    f"""
                    [Desktop Entry]
                    Type=Application
                    Name=Twitch Drops Miner
                    Description=Mine timed drops on Twitch
                    Exec=sh -c '{self._get_autostart_path()}'
                    """
                )
                with autostart_file.open('w', encoding="utf8") as file:
                    file.write(file_contents)
            else:
                autostart_file.unlink(missing_ok=True)
        elif sys.platform == "darwin":
            plist_file = self._get_mac_autostart_filepath()
            if enabled:
                plist_data = {
                    "Label": f"com.devilxd.{self.AUTOSTART_NAME.lower()}",
                    "ProgramArguments": shlex.split(self._get_autostart_path()),
                    "RunAtLoad": True,
                }
                plist_file.parent.mkdir(parents=True, exist_ok=True)
                with plist_file.open("wb") as file:
                    plistlib.dump(plist_data, file)
            else:
                plist_file.unlink(missing_ok=True)

    # games / lists
    def set_games(self, games: set[Game]) -> None:
        self._game_names.update(game.name for game in games)
        self._push()

    def clear_selection(self) -> None:
        pass

    def priority_add(self, game_name: str) -> None:
        game_name = (game_name or '').strip()
        if game_name and game_name not in self._settings.priority:
            self._settings.priority.append(game_name)
            self._settings.alter()
        self._push()

    def priority_move(self, idx: int, amount: int) -> None:
        # amount > 0 = up, amount < 0 = down
        priority = self._settings.priority
        if not 0 <= idx < len(priority) or amount == 0:
            return
        new_idx = min(max(idx - amount, 0), len(priority) - 1)
        if new_idx == idx:
            return
        item = priority.pop(idx)
        priority.insert(new_idx, item)
        self._settings.alter()
        self._push()

    def priority_delete(self, idx: int) -> None:
        if 0 <= idx < len(self._settings.priority):
            del self._settings.priority[idx]
            self._settings.alter()
        self._push()

    def exclude_add(self, game_name: str) -> None:
        game_name = (game_name or '').strip()
        if game_name and game_name not in self._settings.exclude:
            self._settings.exclude.add(game_name)
            self._settings.alter()
        self._push()

    def exclude_delete(self, game_name: str) -> None:
        if game_name in self._settings.exclude:
            self._settings.exclude.discard(game_name)
            self._settings.alter()
        self._push()


class _InvalidateButton:
    """Shim for the old Tk button - twitch.py toggles its state on login/logout."""

    def __init__(self, help_tab: HelpTab):
        self._help = help_tab

    def config(self, *, state: str = "normal", **kwargs: Any) -> None:
        self._help.set_invalidate_enabled(state == "normal")

    configure = config


class HelpTab:
    def __init__(self, manager: GUIManager):
        self._manager = manager
        self._twitch = manager._twitch
        self._enabled: bool = False
        self._invalidate_button = _InvalidateButton(self)

    def set_invalidate_enabled(self, enabled: bool) -> None:
        self._enabled = enabled
        self._manager.emit("help", self.snapshot(), key="help")

    def snapshot(self) -> dict[str, Any]:
        return {"invalidate_enabled": self._enabled}

    def invalidate_token(self) -> None:
        asyncio.create_task(task_wrapper(self._invalidate_token)())

    async def _invalidate_token(self) -> None:
        auth_state = await self._twitch.get_auth()
        async with self._twitch.request(
            "POST",
            "https://id.twitch.tv/oauth2/revoke",
            data={
                "client_id": self._twitch._client_type.CLIENT_ID,
                "token": auth_state.access_token,
            }
        ) as response:
            if response.status == 200:
                auth_state.invalidate(delete_cookies=True)
            else:
                logger.error(f"Failed to invalidate the auth token: {response.status}")
        self._twitch.change_state(State.RESTART)


###############
# GUI MANAGER #
###############


class GUIManager:
    FLUSH_INTERVAL = 0.1

    def __init__(self, twitch: Twitch):
        self._twitch: Twitch = twitch
        self._loop = asyncio.get_running_loop()
        self._close_requested = asyncio.Event()
        self._flush_task: asyncio.Task[NoReturn] | None = None
        self._js_ready: bool = False
        self._started_at: float = datetime.now(timezone.utc).timestamp()
        self._pending: dict[str, tuple[str, Any]] = {}
        self._pending_seq: int = 0
        windows = webview.windows
        self._window: webview.Window | None = windows[0] if windows else None
        # components
        self.status = StatusBar(self)
        self.websockets = WebsocketStatus(self)
        self.login = LoginForm(self)
        self.progress = CampaignProgress(self)
        self.output = ConsoleOutput(self)
        self.channels = ChannelList(self)
        self.tray = TrayIcon(self)
        self.inv = InventoryOverview(self)
        self.settings = SettingsPanel(self)
        self.help = HelpTab(self)
        self.mobile = MobileServer(twitch.settings, self.mobile_state, self.mobile_action)
        self._mobile_qr: tuple[str, str] | None = None
        # logging
        self._handler = _OutputHandler(self)
        self._handler.setFormatter(OUTPUT_FORMATTER)
        logger_main = logging.getLogger("TwitchDrops")
        logger_main.addHandler(self._handler)
        if (logging_level := logger_main.getEffectiveLevel()) < logging.ERROR:
            self.print(f"Logging level: {logging.getLevelName(logging_level)}")
        # register with the JS bridge
        _shared["loop"] = self._loop
        _shared["manager"] = self
        if _shared["js_ready"]:
            self.handle_js("on_ready", ())
        # the tray icon stays on all the time on Windows (needed for notifications)
        if sys.platform == "win32":
            self.tray.start()
        self.apply_theme(self._twitch.settings.dark_mode)

    # Python -> JS
    def emit(self, kind: str, payload: Any = None, *, key: str | None = None) -> None:
        if key is None:
            self._pending_seq += 1
            key = f"_{self._pending_seq}"
        else:
            # keep ordering stable: a replaced keyed event moves to the end
            self._pending.pop(key, None)
        self._pending[key] = (kind, payload)

    def full_state(self) -> dict[str, Any]:
        return {
            "title": WINDOW_TITLE,
            "started_at": self._started_at,
            "status": self.status.snapshot(),
            "state": self.tray._icon_state,
            "ws": self.websockets.snapshot(),
            "login": self.login.snapshot(),
            "progress": self.progress.snapshot(),
            "log": self.output.snapshot(),
            "channels": self.channels.snapshot(),
            "inventory": self.inv.snapshot(),
            "alerts": self.inv.link_alerts(),
            "settings": self.settings.snapshot(),
            "help": self.help.snapshot(),
            "i18n": _._translation.get("gui", {}),
        }

    async def _flush_loop(self) -> NoReturn:
        while True:
            await asyncio.sleep(self.FLUSH_INTERVAL)
            await self._flush()

    async def _flush(self) -> None:
        if not self._js_ready or not self._pending or self._window is None:
            return
        events = [{"type": kind, "data": data} for kind, data in self._pending.values()]
        self._pending.clear()
        script = f"window.TDM && window.TDM.batch({json.dumps(events, default=str)})"
        try:
            await self._loop.run_in_executor(None, self._window.run_js, script)
        except Exception:
            logger.debug("Failed to push GUI update", exc_info=True)

    def window_call(self, method: str, *args: Any) -> None:
        if self._window is None:
            return
        func = getattr(self._window, method)
        self._loop.run_in_executor(None, lambda: func(*args))

    # JS -> Python
    def handle_js(self, name: str, args: tuple[Any, ...]) -> None:
        try:
            self._handle_js(name, args)
        except Exception:
            logger.exception(f"GUI action failed: {name}")

    def _handle_js(self, name: str, args: tuple[Any, ...]) -> None:
        if name == "on_ready":
            self._js_ready = True
            self._pending.clear()
            self.emit("full", self.full_state())
        elif name == "login_press":
            self.login.press()
        elif name == "login_open_window":
            self.login.focus_login_window()
        elif name == "select_channel":
            self.channels.select(args[0])
        elif name == "switch_channel":
            self._twitch.change_state(State.CHANNEL_SWITCH)
        elif name == "inv_refresh":
            self.inv.refresh()
        elif name == "set_setting":
            self.settings.set_setting(*args)
            if args[0] == "dark_mode":
                self.apply_theme(bool(args[1]))
            if args[0] in ("priority_mode",):
                self.inv.refresh()
        elif name == "set_proxy":
            self.settings.set_proxy(args[0])
        elif name == "priority_add":
            self.settings.priority_add(args[0])
            self.inv.push_alerts()
        elif name == "priority_move":
            self.settings.priority_move(int(args[0]), int(args[1]))
        elif name == "priority_delete":
            self.settings.priority_delete(int(args[0]))
            self.inv.push_alerts()
        elif name == "exclude_add":
            self.settings.exclude_add(args[0])
            self.inv.push_alerts()
        elif name == "exclude_delete":
            self.settings.exclude_delete(args[0])
            self.inv.push_alerts()
        elif name == "reload":
            self._twitch.change_state(State.INVENTORY_FETCH)
        elif name == "invalidate":
            self.help.invalidate_token()
        elif name == "minimize_to_tray":
            self.tray.minimize()
        elif name == "mobile_new_token":
            self.mobile.new_token()
            self.settings._push()
        elif name == "test_notification":
            if self.tray.notify(
                "As notificações estão funcionando!", "Twitch Drops Miner"
            ) is None:
                self.print("Notificações estão desativadas nas configurações.")

    # phone access
    def mobile_apply(self) -> None:
        async def apply():
            if self._twitch.settings.mobile_access:
                if await self.mobile.start():
                    self.print(f"Acesso pelo celular ativo: {self.mobile.url}")
            else:
                await self.mobile.stop()
            self.settings._push()
        asyncio.ensure_future(apply())

    def mobile_info(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "enabled": bool(self._twitch.settings.mobile_access),
            "running": self.mobile.running,
            "error": self.mobile.error,
            "url": "",
            "qr": "",
        }
        if self.mobile.running:
            url = self.mobile.url
            if self._mobile_qr is None or self._mobile_qr[0] != url:
                try:
                    self._mobile_qr = (url, qr_svg(url))
                except Exception:
                    logger.exception("QR code generation failed")
                    self._mobile_qr = (url, "")
            info["url"] = url
            info["qr"] = self._mobile_qr[1]
        return info

    def mobile_action(self, name: str, args: list[Any]) -> None:
        if name == "switch_to":
            self.channels.select(str(args[0]) if args else None)
            if self.channels.get_selection() is not None:
                self._twitch.change_state(State.CHANNEL_SWITCH)
            return
        if name == "priority_mode":
            self.handle_js("set_setting", ("priority_mode", args[0] if args else ''))
            return
        self.handle_js(name, tuple(args))

    def mobile_state(self) -> dict[str, Any]:
        channels = self.channels.snapshot()
        watching = next(
            (r for r in channels["rows"] if r["iid"] == channels["watching"]), None
        )
        active = [
            c for c in self.inv.snapshot()
            if c["status_key"] == "active" and c["eligible"] and not c["sub_only"]
        ]
        priority = list(self._twitch.settings.priority)
        if self._twitch.settings.priority_mode is PriorityMode.PRIORITY_ONLY:
            active = [c for c in active if c["game"] in priority]
        return {
            "status": self.status.snapshot(),
            "state": self.tray._icon_state,
            "logged_in": self.login.snapshot()["logged_in"],
            "progress": self.progress.snapshot(),
            "watching": watching,
            "online": sum(1 for r in channels["rows"] if r["status_key"] == "online"),
            "ws_ok": any(
                "connect" in str(w["status"]).lower() and "dis" not in str(w["status"]).lower()
                for w in self.websockets.snapshot()
            ),
            "campaigns": [
                {k: c[k] for k in (
                    "id", "name", "game", "image", "ends", "claimed", "total", "progress",
                    "finished",
                )}
                for c in active
            ],
            "log": self.output.snapshot()[-40:],
            "channels": channels["rows"],
            "watching_iid": channels["watching"],
            "settings": {
                k: v for k, v in self.settings.snapshot().items()
                if k in ("priority", "exclude", "priority_mode", "priority_modes", "games")
            },
            "started_at": self._started_at,
            "session_claimed": self.inv.session_claimed,
            "alerts": self.inv.link_alerts(),
        }

    # API used by the miner (same as the old Tk GUI)
    @property
    def running(self) -> bool:
        return self._flush_task is not None

    @property
    def close_requested(self) -> bool:
        return self._close_requested.is_set()

    async def wait_until_closed(self):
        await self._close_requested.wait()

    async def coro_unless_closed(self, coro: abc.Awaitable[_T]) -> _T:
        tasks = [asyncio.ensure_future(coro), asyncio.ensure_future(self._close_requested.wait())]
        done: set[asyncio.Task[Any]]
        pending: set[asyncio.Task[Any]]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        if self._close_requested.is_set():
            raise ExitRequest()
        return await next(iter(done))

    def prevent_close(self):
        self._close_requested.clear()

    def start(self):
        if self._flush_task is None:
            self._flush_task = asyncio.create_task(self._flush_loop())
            if self._twitch.settings.mobile_access:
                self.mobile_apply()

    def stop(self):
        if self.mobile.running:
            asyncio.ensure_future(self.mobile.stop())
        self.progress.stop_timer()
        if self._flush_task is not None:
            self._flush_task.cancel()
            self._flush_task = None

    def close(self, *args) -> int:
        """
        Requests the application to close.
        The window itself will be closed in the closing sequence later.
        """
        self._close_requested.set()
        self._twitch.close()
        return 0

    def close_window(self):
        self.tray.stop()
        logging.getLogger("TwitchDrops").removeHandler(self._handler)
        _shared["manager"] = None
        if self._window is not None:
            try:
                self._window.destroy()
            except Exception:
                pass

    def unfocus(self, event=None):
        self.channels.clear_selection()

    def save(self, *, force: bool = False) -> None:
        # images are cached by the WebView itself - nothing to save here
        pass

    def grab_attention(self, *, sound: bool = True):
        self.tray.restore()
        if sound and sys.platform == "win32":
            try:
                winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
            except Exception:
                pass

    def set_games(self, games: set[Game]) -> None:
        self.settings.set_games(games)

    def display_drop(
        self, drop: TimedDrop, *, countdown: bool = True, subone: bool = False
    ) -> None:
        self.progress.display(drop, countdown=countdown, subone=subone)
        self.tray.update_title(drop)

    def clear_drop(self):
        self.progress.display(None)
        self.tray.update_title(None)

    def print(self, message: str):
        self.output.print(message)

    def apply_theme(self, dark: bool) -> None:
        self.emit("theme", "dark" if dark else "light", key="theme")
        set_title_bar_dark(dark)


def set_title_bar_dark(dark: bool) -> None:
    """Match the native Windows title bar to the app theme (Windows 10 20H1+ / 11)."""
    if sys.platform != "win32":
        return
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            return
        value = ctypes.c_int(1 if dark else 0)
        # DWMWA_USE_IMMERSIVE_DARK_MODE = 20
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 20, ctypes.byref(value), ctypes.sizeof(value)
        )
    except Exception:
        pass
