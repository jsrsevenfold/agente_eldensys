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

from .balanca.servico import ServicoBalanca
from .balanca.servico import set_servico as set_servico_balanca
from .biometria.erros import ErroBiometria
from .biometria.servico import ServicoBiometria, set_servico
from .config import APP_DIR, CONFIG_PATH, LOG_DIR, load_config
from .config_window import describe_teste_leitor, open_config_window_async
from .remote_jobs import RemoteJobsWorker, set_worker
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
        # Leitor biométrico (v0.6). Construir não abre o leitor nem carrega a
        # DLL: tudo começa no primeiro uso (rota, menu ou poll da loja).
        self.bio = ServicoBiometria()
        set_servico(self.bio)
        # Balança de checkout (v0.7). Também preguiçosa: a COM só abre na
        # primeira leitura e fecha sozinha depois de alguns segundos parada.
        self.balanca = ServicoBalanca()
        set_servico_balanca(self.balanca)
        self.app = create_app(self.cfg, biometria=self.bio, balanca=self.balanca)
        self.server: uvicorn.Server | None = None
        self.server_thread: threading.Thread | None = None
        self.icon: pystray.Icon | None = None
        # Canal remoto (carga de balança / etiquetas vindas de outro aparelho).
        # Fica parado, sem rede, enquanto o agente não for pareado com a loja.
        # O heartbeat leva o estado do leitor e o poll traz a versão das
        # digitais da loja (espelho da memória do leitor).
        self.remote = RemoteJobsWorker(
            capacidades_biometria_fn=self.bio.capacidades,
            ao_poll_fn=self.bio.ao_poll,
            capacidades_balanca_fn=self.balanca.capacidades,
        )
        set_worker(self.remote)

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

    def _open_config_window(self, _icon=None, _item=None) -> None:
        open_config_window_async()

    def _open_appdir(self, _icon=None, _item=None) -> None:
        os.startfile(str(APP_DIR))  # type: ignore[attr-defined]

    def _testar_leitor(self, _icon=None, _item=None) -> None:
        # A leitura pode levar alguns segundos: fora da thread do menu.
        threading.Thread(
            target=self._testar_leitor_bg, daemon=True, name="biometria-testar"
        ).start()

    def _testar_leitor_bg(self) -> None:
        try:
            texto = describe_teste_leitor(self.bio.testar())
        except ErroBiometria as e:
            texto = e.mensagem
        except Exception:
            log.exception("Falha ao testar o leitor biométrico")
            texto = "Falha ao testar o leitor biométrico. Veja o log do agente."
        self._notificar("Leitor biométrico", texto)

    def _notificar(self, titulo: str, texto: str) -> None:
        if not self.icon:
            return
        try:
            self.icon.notify(texto, titulo)
        except Exception:
            log.warning("Não foi possível mostrar a notificação: %s", texto)

    def _quit(self, _icon=None, _item=None) -> None:
        log.info("Encerrando agente via tray")
        self.remote.stop()
        # Solta o leitor: o PDV offline neste computador pode precisar dele.
        try:
            self.bio.parar()
        except Exception:
            log.exception("Falha ao encerrar a biometria")
        # Solta a COM da balança pelo mesmo motivo.
        try:
            self.balanca.parar()
        except Exception:
            log.exception("Falha ao encerrar a balança de checkout")
        self.stop_server()
        if self.icon:
            self.icon.stop()

    # ── run ────────────────────────────────────────────
    def run(self) -> None:
        self.start_server()
        self.remote.start()

        menu = pystray.Menu(
            pystray.MenuItem(
                "Configurações...",
                self._open_config_window,
                default=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Abrir pasta do agente", self._open_appdir),
            pystray.MenuItem("Abrir pasta de logs", self._open_logs),
            pystray.MenuItem("Verificar agente (/health)", self._open_url),
            pystray.MenuItem("Testar leitor biométrico", self._testar_leitor),
            pystray.MenuItem("Editar config.json (avançado)", self._open_config),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Sair", self._quit),
        )

        self.icon = pystray.Icon(
            "EldenSysAgent",
            _icon_image(),
            "EldenSys Print Agent",
            menu,
        )
        self.icon.run()
