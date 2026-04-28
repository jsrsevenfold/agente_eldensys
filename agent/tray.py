"""System tray application — pystray icon + uvicorn server in background thread."""

from __future__ import annotations

import logging
import os
import sys
import threading
import webbrowser
from pathlib import Path

import pystray
import uvicorn
from PIL import Image, ImageDraw

from .config import APP_DIR, CONFIG_PATH, LOG_DIR, load_config
from .server import create_app

log = logging.getLogger("eldensys.agent.tray")


def _icon_image(color: str = "#1f7a3a") -> Image.Image:
    """Load packaged icon, falling back to a generated one."""
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "assets" / "icon.png")
        candidates.append(Path(sys.executable).parent / "assets" / "icon.png")
    candidates.append(Path(__file__).resolve().parent.parent / "assets" / "icon.png")

    for c in candidates:
        if c.exists():
            try:
                return Image.open(c)
            except Exception:
                pass

    # Fallback: generate a 64x64 colored circle with a "P"
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=color)
    d.text((22, 18), "P", fill="white")
    return img


class AgentTray:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.app = create_app(self.cfg)
        self.server: uvicorn.Server | None = None
        self.server_thread: threading.Thread | None = None
        self.icon: pystray.Icon | None = None

    # ── server lifecycle ───────────────────────────────
    def _serve(self) -> None:
        config = uvicorn.Config(
            app=self.app,
            host=self.cfg.host,
            port=self.cfg.port,
            log_level=self.cfg.log_level.lower(),
            access_log=False,
        )
        self.server = uvicorn.Server(config)
        try:
            self.server.run()
        except Exception:
            log.exception("Servidor uvicorn caiu")

    def start_server(self) -> None:
        self.server_thread = threading.Thread(target=self._serve, daemon=True, name="agent-http")
        self.server_thread.start()

    def stop_server(self) -> None:
        if self.server:
            self.server.should_exit = True

    # ── menu actions ───────────────────────────────────
    def _open_url(self, _icon=None, _item=None) -> None:
        webbrowser.open(f"http://{self.cfg.host}:{self.cfg.port}/health")

    def _open_logs(self, _icon=None, _item=None) -> None:
        os.startfile(str(LOG_DIR))  # type: ignore[attr-defined]

    def _open_config(self, _icon=None, _item=None) -> None:
        os.startfile(str(CONFIG_PATH))  # type: ignore[attr-defined]

    def _open_appdir(self, _icon=None, _item=None) -> None:
        os.startfile(str(APP_DIR))  # type: ignore[attr-defined]

    def _quit(self, _icon=None, _item=None) -> None:
        log.info("Encerrando agente via tray")
        self.stop_server()
        if self.icon:
            self.icon.stop()

    # ── run ────────────────────────────────────────────
    def run(self) -> None:
        self.start_server()

        menu = pystray.Menu(
            pystray.MenuItem("Abrir pasta do agente", self._open_appdir),
            pystray.MenuItem("Sair", self._quit),
        )

        self.icon = pystray.Icon(
            "EldenSysAgent",
            _icon_image(),
            "EldenSys Print Agent",
            menu,
        )
        self.icon.run()
