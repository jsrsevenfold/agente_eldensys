"""Entry point para o executável PyInstaller.

`agent/__main__.py` usa imports relativos (`from .config import ...`),
que só funcionam ao rodar como módulo (`python -m agent`).
Este script importa o pacote por nome, garantindo que os imports
relativos resolvam corretamente quando o PyInstaller executar o EXE.
"""

from __future__ import annotations

import os
import sys


def _ensure_std_streams() -> None:
    """No modo --noconsole, sys.stdout/stderr/stdin são None.
    Uvicorn/logging quebram ao tentar usar esses streams.
    Redireciona para os.devnull para mantê-los acessíveis."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")  # noqa: SIM115


_ensure_std_streams()

from agent.__main__ import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())

