"""
Phone access: a small read-only web server on the local network.

When enabled, the miner serves a mobile-friendly page (web/mobile.html) and a JSON
state endpoint. Every request needs the secret key that's embedded in the URL/QR code
shown in the desktop app, so other devices on the network can't just browse to it.
The phone can also control the miner (switch channel, edit the priority/exclude lists,
change the priority mode, reload) - those go through the same handlers the desktop UI uses.
The page can be "installed" on the phone's home screen (PWA) to look and feel like an app.
"""
from __future__ import annotations

import socket
import secrets
import logging
from typing import Any, Callable, TYPE_CHECKING

from aiohttp import web

from utils import resource_path

if TYPE_CHECKING:
    from settings import Settings


logger = logging.getLogger("TwitchDrops")


def local_ip() -> str:
    """Best guess of this PC's address on the local network."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # no packets are actually sent - this just picks the outgoing interface
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


class MobileServer:
    # actions the phone is allowed to trigger
    ALLOWED_ACTIONS = {
        "switch_to", "reload", "priority_add", "priority_move", "priority_delete",
        "exclude_add", "exclude_delete", "priority_mode",
    }

    def __init__(
        self,
        settings: Settings,
        get_state: Callable[[], dict[str, Any]],
        on_action: Callable[[str, list[Any]], None],
    ):
        self._settings = settings
        self._get_state = get_state
        self._on_action = on_action
        self._icon_png: bytes | None = None
        self._runner: web.AppRunner | None = None
        self.error: str | None = None
        if not settings.mobile_token:
            settings.mobile_token = secrets.token_urlsafe(12)
            settings.alter()

    @property
    def running(self) -> bool:
        return self._runner is not None

    @property
    def port(self) -> int:
        return int(self._settings.mobile_port or 8642)

    @property
    def url(self) -> str:
        return f"http://{local_ip()}:{self.port}/?k={self._settings.mobile_token}"

    def _authorized(self, request: web.Request) -> bool:
        key = request.query.get("k", "")
        return bool(key) and secrets.compare_digest(key, self._settings.mobile_token)

    async def _index(self, request: web.Request) -> web.StreamResponse:
        if not self._authorized(request):
            return web.Response(status=403, text="Acesso negado - use o QR code do programa.")
        return web.FileResponse(
            resource_path("web/mobile.html"),
            headers={"Cache-Control": "no-store"},
        )

    async def _state(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "forbidden"}, status=403)
        return web.json_response(
            self._get_state(),
            dumps=_dumps,
            headers={"Cache-Control": "no-store"},
        )

    async def _action(self, request: web.Request) -> web.Response:
        if not self._authorized(request):
            return web.json_response({"error": "forbidden"}, status=403)
        try:
            body = await request.json()
            name = str(body.get("name", ""))
            args = body.get("args") or []
            if not isinstance(args, list):
                raise ValueError
        except Exception:
            return web.json_response({"error": "bad request"}, status=400)
        if name not in self.ALLOWED_ACTIONS:
            return web.json_response({"error": "unknown action"}, status=400)
        self._on_action(name, args)
        return web.json_response({"ok": True})

    async def _manifest(self, request: web.Request) -> web.Response:
        key = request.query.get("k", "")
        return web.json_response({
            "name": "Twitch Drops Miner",
            "short_name": "Drops Miner",
            "start_url": f"/?k={key}",
            "display": "standalone",
            "background_color": "#0e0e10",
            "theme_color": "#0e0e10",
            "icons": [{"src": "/icon.png", "sizes": "256x256", "type": "image/png"}],
        })

    async def _icon(self, request: web.Request) -> web.Response:
        if self._icon_png is None:
            import io
            from PIL import Image
            with Image.open(resource_path("icons/pickaxe.ico")) as img:
                img = img.convert("RGBA").resize((256, 256))
                buf = io.BytesIO()
                img.save(buf, "PNG")
                self._icon_png = buf.getvalue()
        return web.Response(body=self._icon_png, content_type="image/png")

    async def start(self) -> bool:
        if self._runner is not None:
            return True
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/state", self._state)
        app.router.add_post("/action", self._action)
        app.router.add_get("/manifest.json", self._manifest)
        app.router.add_get("/icon.png", self._icon)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=self.port)
        try:
            await site.start()
        except OSError as exc:
            await runner.cleanup()
            self.error = f"Não foi possível abrir a porta {self.port}: {exc.strerror or exc}"
            logger.error(self.error)
            return False
        self._runner = runner
        self.error = None
        return True

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def new_token(self) -> None:
        self._settings.mobile_token = secrets.token_urlsafe(12)
        self._settings.alter()


def _dumps(obj: Any) -> str:
    import json
    return json.dumps(obj, default=str)


def qr_svg(data: str) -> str:
    """QR code as an inline SVG string (dark modules, transparent background)."""
    import segno
    qr = segno.make(data, error="m")
    return qr.svg_inline(scale=5, border=2, dark="#0e0e10", light="#ffffff")
