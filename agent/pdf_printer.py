"""PDF printing via embedded SumatraPDF."""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import load_config


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


def print_pdf(
    printer_name: str,
    pdf_bytes: bytes,
    copies: int = 1,
    paper: str | None = None,
    duplex: bool = False,
) -> None:
    """Print a PDF document using SumatraPDF in silent mode."""
    sumatra = find_sumatra()
    if not sumatra:
        raise RuntimeError(
            "SumatraPDF.exe não encontrado. Instale-o ou coloque em vendor/SumatraPDF.exe."
        )

    settings_parts: list[str] = []
    if copies and copies > 1:
        settings_parts.append(f"{copies}x")
    if paper:
        settings_parts.append(f"paper={paper}")
    if duplex:
        settings_parts.append("duplexlong")
    settings = ",".join(settings_parts)

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
        ]
        if settings:
            args.extend(["-print-settings", settings])
        args.append(tmp.name)

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
