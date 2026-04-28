"""Entry point: `python -m agent`."""

from __future__ import annotations

import ctypes
import sys

from .config import load_config
from .logging_setup import setup_logging
from .single_instance import SingleInstance
from .tray import AgentTray


def main() -> int:
    cfg = load_config()
    log = setup_logging(cfg.log_level)

    lock = SingleInstance()
    if lock.already_running:
        log.warning("EldenSys Agent já está em execução (mutex). Encerrando.")
        if sys.platform == "win32":
            ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
                None,
                (
                    "O EldenSys Agent já está em execução.\n\n"
                    "Verifique o ícone na bandeja do sistema (próximo ao relógio)."
                ),
                "EldenSys Agent",
                0x40 | 0x10000,  # MB_ICONINFORMATION | MB_SETFOREGROUND
            )
        return 0

    log.info("Iniciando EldenSys Agent em %s:%s", cfg.host, cfg.port)
    try:
        AgentTray().run()
    finally:
        lock.release()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
