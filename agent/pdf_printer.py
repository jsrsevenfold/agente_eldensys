"""PDF printing via embedded SumatraPDF."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from io import BytesIO
from pathlib import Path

from .config import AgentConfig, load_config

log = logging.getLogger("eldensys.agent.pdf")

MM_TO_PT = 72.0 / 25.4


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


def _needs_transform(cfg: AgentConfig) -> bool:
    return (
        abs(cfg.pdf_scale - 1.0) > 1e-6
        or cfg.pdf_margin_top_mm > 0
        or cfg.pdf_margin_right_mm > 0
        or cfg.pdf_margin_bottom_mm > 0
        or cfg.pdf_margin_left_mm > 0
    )


def _transform_pdf(pdf_bytes: bytes, cfg: AgentConfig) -> bytes:
    """Apply pdf_scale + pdf_margin_* to each page, returning a new PDF.

    Estratégia:
      - Calcula nova mediabox = (original × escala) + margens (esq+dir, top+bot).
      - Aplica Transformation.scale(s,s).translate(margin_left, margin_bottom)
        no conteúdo da página — o conteúdo cresce e fica deslocado pelas margens.
      - Conteúdo original que ficaria fora da nova mediabox é cortado pelo viewer.
    """
    try:
        from pypdf import PdfReader, PdfWriter, Transformation
        from pypdf.generic import RectangleObject
    except ImportError:
        log.warning("pypdf não instalado; ignorando pdf_scale/margins")
        return pdf_bytes

    scale = max(0.1, float(cfg.pdf_scale or 1.0))
    m_top = cfg.pdf_margin_top_mm * MM_TO_PT
    m_right = cfg.pdf_margin_right_mm * MM_TO_PT
    m_bottom = cfg.pdf_margin_bottom_mm * MM_TO_PT
    m_left = cfg.pdf_margin_left_mm * MM_TO_PT

    try:
        reader = PdfReader(BytesIO(pdf_bytes))
        writer = PdfWriter()
        for page in reader.pages:
            orig_w = float(page.mediabox.width)
            orig_h = float(page.mediabox.height)
            new_w = orig_w * scale + m_left + m_right
            new_h = orig_h * scale + m_top + m_bottom

            page.add_transformation(
                Transformation().scale(scale, scale).translate(m_left, m_bottom)
            )
            rect = RectangleObject((0, 0, new_w, new_h))
            page.mediabox = rect
            page.cropbox = rect
            writer.add_page(page)

        buf = BytesIO()
        writer.write(buf)
        return buf.getvalue()
    except Exception:
        log.exception("Falha ao transformar PDF; enviando original")
        return pdf_bytes


def _build_print_settings(
    copies: int,
    paper: str | None,
    duplex: bool,
    cfg: AgentConfig,
) -> str:
    parts: list[str] = []
    if copies and copies > 1:
        parts.append(f"{copies}x")
    if paper:
        parts.append(f"paper={paper}")
    if duplex:
        parts.append("duplexlong")
    mode = (cfg.pdf_fit_mode or "fit").lower()
    if mode in {"fit", "shrink", "noscale"}:
        parts.append(mode)
    return ",".join(parts)


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

    cfg = load_config()
    if _needs_transform(cfg):
        pdf_bytes = _transform_pdf(pdf_bytes, cfg)

    settings = _build_print_settings(copies, paper, duplex, cfg)

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
