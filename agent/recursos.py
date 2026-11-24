"""Onde ficam os arquivos que vão junto com o agente (SumatraPDF, DLL do leitor).

Saiu de ``pdf_printer._resource_dir`` na v0.6 porque a biometria também
precisa achar a ``libcidbio.dll``. No PyInstaller 6 (onedir) é a pasta
``_internal`` ao lado do .exe (``sys._MEIPASS``); em desenvolvimento, a raiz
do repositório.
"""

from __future__ import annotations

import sys
from pathlib import Path


def resource_dir() -> Path:
    """Pasta dos recursos empacotados (compatível com o PyInstaller)."""
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent
