"""FastAPI server exposing the print agent on localhost."""

from __future__ import annotations

import base64
import logging
from io import BytesIO
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from . import __version__
from .config import AgentConfig, load_config, save_config
from .escpos_renderer import EscposPayload, render
from .pdf_printer import print_pdf
from .printers import get_default_printer, list_printers, write_raw

log = logging.getLogger("eldensys.agent.server")


# ── Pydantic body models (module-level for proper FastAPI body detection) ──
class ConfigUpdate(BaseModel):
    allowed_origins: list[str] | None = None
    log_level: str | None = None
    sumatra_path: str | None = None
    # ESC/POS defaults
    escpos_default_width: int | None = Field(default=None, ge=1, le=8)
    escpos_default_height: int | None = Field(default=None, ge=1, le=8)
    escpos_default_font: Literal["a", "b"] | None = None
    escpos_size_multiplier: float | None = Field(default=None, ge=0.5, le=4.0)
    escpos_default_bold: bool | None = None
    escpos_left_margin_mm: float | None = Field(default=None, ge=0, le=30)
    escpos_right_margin_mm: float | None = Field(default=None, ge=0, le=30)
    escpos_top_margin_mm: float | None = Field(default=None, ge=0, le=30)
    escpos_bottom_margin_mm: float | None = Field(default=None, ge=0, le=30)
    # PDF defaults
    pdf_fit_mode: Literal["fit", "noscale", "shrink"] | None = None
    pdf_scale: float | None = Field(default=None, ge=0.5, le=3.0)
    pdf_margin_top_mm: float | None = Field(default=None, ge=0, le=50)
    pdf_margin_right_mm: float | None = Field(default=None, ge=0, le=50)
    pdf_margin_bottom_mm: float | None = Field(default=None, ge=0, le=50)
    pdf_margin_left_mm: float | None = Field(default=None, ge=0, le=50)


class RawPayload(BaseModel):
    printer: str
    data_base64: str
    doc_name: str = "EldenSys Raw"


class PdfPayload(BaseModel):
    printer: str
    pdf_base64: str
    copies: int = Field(default=1, ge=1, le=99)
    paper: Literal["A4", "A5", "Letter", "Legal"] | None = None
    duplex: bool = False


class TestPayload(BaseModel):
    printer: str
    kind: Literal["escpos", "pdf"] = "escpos"
    profile: Literal["58mm", "80mm"] = "80mm"


# Minimal hand-crafted single-page PDF used as fallback test.
_MINIMAL_PDF = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Count 1/Kids[3 0 R]>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 595 842]"
    b"/Resources<<>>/Contents 4 0 R>>endobj\n"
    b"4 0 obj<</Length 44>>stream\n"
    b"BT /F1 24 Tf 100 700 Td (EldenSys Test) Tj ET\n"
    b"endstream endobj\n"
    b"xref\n0 5\n0000000000 65535 f \n"
    b"0000000010 00000 n \n0000000053 00000 n \n"
    b"0000000100 00000 n \n0000000178 00000 n \n"
    b"trailer<</Size 5/Root 1 0 R>>\nstartxref\n260\n%%EOF\n"
)


def _build_test_escpos(printer: str, profile: Literal["58mm", "80mm"]) -> EscposPayload:
    return EscposPayload.model_validate(
        {
            "printer": printer,
            "profile": profile,
            "commands": [
                {"type": "text", "text": "EldenSys", "align": "center",
                 "bold": True, "width": 2, "height": 2},
                {"type": "text", "text": "Agente de Impressão", "align": "center"},
                {"type": "line"},
                {"type": "text", "text": "Página de teste"},
                {"type": "text", "text": f"Impressora: {printer}"},
                {"type": "text", "text": f"Perfil: {profile}"},
                {"type": "line"},
                {"type": "qrcode", "text": "https://eldensys.com.br", "size": 6},
                {"type": "newline", "count": 3},
                {"type": "cut"},
            ],
        }
    )


def create_app(cfg: AgentConfig | None = None) -> FastAPI:
    cfg = cfg or load_config()
    app = FastAPI(title="EldenSys Print Agent", version=__version__)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.allowed_origins,
        allow_origin_regex=cfg.allowed_origin_regex or None,
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # ── Health ─────────────────────────────────────────
    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "service": "eldensys-agent",
            "version": __version__,
        }

    # ── Printers ───────────────────────────────────────
    @app.get("/printers")
    def printers() -> dict:
        try:
            items = list_printers()
            return {"default": get_default_printer(), "printers": items}
        except Exception as e:
            log.exception("Falha ao listar impressoras")
            raise HTTPException(status_code=500, detail=str(e))

    # ── Config (read/write) ────────────────────────────
    @app.get("/config")
    def config_get() -> dict:
        c = load_config()
        return {
            "host": c.host,
            "port": c.port,
            "allowed_origins": c.allowed_origins,
            "log_level": c.log_level,
            "sumatra_path": c.sumatra_path,
            "escpos_default_width": c.escpos_default_width,
            "escpos_default_height": c.escpos_default_height,
            "escpos_default_font": c.escpos_default_font,
            "escpos_size_multiplier": c.escpos_size_multiplier,
            "escpos_default_bold": c.escpos_default_bold,
            "escpos_left_margin_mm": c.escpos_left_margin_mm,
            "escpos_right_margin_mm": c.escpos_right_margin_mm,
            "escpos_top_margin_mm": c.escpos_top_margin_mm,
            "escpos_bottom_margin_mm": c.escpos_bottom_margin_mm,
            "pdf_fit_mode": c.pdf_fit_mode,
            "pdf_scale": c.pdf_scale,
            "pdf_margin_top_mm": c.pdf_margin_top_mm,
            "pdf_margin_right_mm": c.pdf_margin_right_mm,
            "pdf_margin_bottom_mm": c.pdf_margin_bottom_mm,
            "pdf_margin_left_mm": c.pdf_margin_left_mm,
        }

    @app.post("/config")
    def config_set(payload: ConfigUpdate) -> dict:
        c = load_config()
        # Campos que exigem restart (CORS, log_level, sumatra_path)
        restart = False
        if payload.allowed_origins is not None:
            c.allowed_origins = payload.allowed_origins
            restart = True
        if payload.log_level is not None:
            c.log_level = payload.log_level
            restart = True
        if payload.sumatra_path is not None:
            c.sumatra_path = payload.sumatra_path
        # Campos de impressão — aplicados em tempo real (load_config por request)
        for fld in (
            "escpos_default_width",
            "escpos_default_height",
            "escpos_default_font",
            "escpos_size_multiplier",
            "escpos_default_bold",
            "escpos_left_margin_mm",
            "escpos_right_margin_mm",
            "escpos_top_margin_mm",
            "escpos_bottom_margin_mm",
            "pdf_fit_mode",
            "pdf_scale",
            "pdf_margin_top_mm",
            "pdf_margin_right_mm",
            "pdf_margin_bottom_mm",
            "pdf_margin_left_mm",
        ):
            val = getattr(payload, fld, None)
            if val is not None:
                setattr(c, fld, val)
        save_config(c)
        return {"status": "ok", "restart_required": restart}

    # ── Print: ESC/POS ─────────────────────────────────
    @app.post("/print/escpos")
    def print_escpos(payload: EscposPayload) -> dict:
        try:
            render(payload)
            return {"status": "ok"}
        except Exception as e:
            log.exception("ESC/POS print failed")
            raise HTTPException(status_code=500, detail=str(e))

    # ── Print: RAW ─────────────────────────────────────
    @app.post("/print/raw")
    def print_raw(payload: RawPayload) -> dict:
        try:
            data = base64.b64decode(payload.data_base64)
            job = write_raw(payload.printer, data, payload.doc_name)
            return {"status": "ok", "job_id": job}
        except Exception as e:
            log.exception("RAW print failed")
            raise HTTPException(status_code=500, detail=str(e))

    # ── Print: PDF ─────────────────────────────────────
    @app.post("/print/pdf")
    def print_pdf_route(payload: PdfPayload) -> dict:
        try:
            data = base64.b64decode(payload.pdf_base64)
            print_pdf(
                printer_name=payload.printer,
                pdf_bytes=data,
                copies=payload.copies,
                paper=payload.paper,
                duplex=payload.duplex,
            )
            return {"status": "ok"}
        except Exception as e:
            log.exception("PDF print failed")
            raise HTTPException(status_code=500, detail=str(e))

    # ── Print: Test ────────────────────────────────────
    @app.post("/print/test")
    def print_test(payload: TestPayload) -> dict:
        try:
            if payload.kind == "escpos":
                render(_build_test_escpos(payload.printer, payload.profile))
            else:
                try:
                    from reportlab.pdfgen import canvas  # type: ignore

                    buf = BytesIO()
                    c = canvas.Canvas(buf)
                    c.drawString(100, 750, "EldenSys - Página de teste")
                    c.showPage()
                    c.save()
                    pdf_bytes = buf.getvalue()
                except ImportError:
                    pdf_bytes = _MINIMAL_PDF
                print_pdf(payload.printer, pdf_bytes)
            return {"status": "ok"}
        except Exception as e:
            log.exception("Test print failed")
            raise HTTPException(status_code=500, detail=str(e))

    return app
