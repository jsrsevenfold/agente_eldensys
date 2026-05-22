"""PDF printing via embedded SumatraPDF.

Política: o agente imprime PDFs **no tamanho original** (`noscale`). Toda
a formatação (largura, margens, escala) é responsabilidade do EldenSys
que gera o PDF. Aqui só relayamos pro Sumatra sem alterar nada.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import load_config

log = logging.getLogger("eldensys.agent.pdf")


def _resource_dir() -> Path:
    """Return the dir where bundled resources live (PyInstaller compatible)."""
    if getattr(sys, "frozen", False):
        # PyInstaller onedir: sys._MEIPASS for onefile, else exe dir
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def find_sumatra() -> str | None:
    cfg = load_config()
    if cfg.sumatra_path and Path(cfg.sumatra_path).exists():
        return cfg.sumatra_path

    candidates = [
        _resource_dir() / "vendor" / "SumatraPDF.exe",
        _resource_dir() / "SumatraPDF.exe",
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "SumatraPDF" / "SumatraPDF.exe",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
        / "SumatraPDF"
        / "SumatraPDF.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)

    found = shutil.which("SumatraPDF")
    return found


def _build_print_settings(
    copies: int,
    paper: str | None,
    duplex: bool,
) -> str:
    """Constrói a string -print-settings do Sumatra.

    Sempre inclui `noscale` — o agente nunca escala o PDF; o EldenSys
    é quem decide o tamanho do papel ao gerar o documento.
    """
    parts: list[str] = ["noscale"]
    if copies and copies > 1:
        parts.append(f"{copies}x")
    if paper:
        parts.append(f"paper={paper}")
    if duplex:
        parts.append("duplexlong")
    return ",".join(parts)


def print_pdf(
    printer_name: str,
    pdf_bytes: bytes,
    copies: int = 1,
    paper: str | None = None,
    duplex: bool = False,
) -> None:
    """Print a PDF document using SumatraPDF in silent mode, sem transformações."""
    sumatra = find_sumatra()
    if not sumatra:
        raise RuntimeError(
            "SumatraPDF.exe não encontrado. Instale-o ou coloque em vendor/SumatraPDF.exe."
        )

    settings = _build_print_settings(copies, paper, duplex)

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp.write(pdf_bytes)
        tmp.flush()
        tmp.close()

        args = [
            sumatra,
            "-print-to",
            printer_name,
            "-silent",
            "-exit-when-done",
            "-print-settings",
            settings,
            tmp.name,
        ]

        # CREATE_NO_WINDOW = 0x08000000 to suppress console flash on frozen builds
        creationflags = 0x08000000 if sys.platform == "win32" else 0
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=creationflags,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"SumatraPDF falhou (code={result.returncode}): {result.stderr or result.stdout}"
            )
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
