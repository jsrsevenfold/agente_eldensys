"""FastAPI server exposing the print agent on localhost.

Rotas de impressão com CORS ``*`` (qualquer página da loja imprime) e, a
partir da v0.6, as rotas ``/biometria`` com política de origem própria
(``agent/biometria/origem.py``): só a retaguarda do EldenSys, só pelo
loopback.
"""

from __future__ import annotations

import base64
import logging
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .biometria import rotas as rotas_biometria
from .biometria.origem import origem_permitida_cfg
from .config import AgentConfig, load_config, save_config
from .escpos_renderer import EscposPayload, render
from .pdf_printer import ensure_label_appdata, print_pdf
from .printers import get_default_printer, list_printers, write_raw

if TYPE_CHECKING:
    from .biometria.servico import ServicoBiometria

log = logging.getLogger("eldensys.agent.server")

#: O que esta versão do agente sabe fazer (a web decide o que oferecer).
RECURSOS = ["impressao", "canal_remoto", "biometria"]


# ── Pydantic body models (module-level for proper FastAPI body detection) ──
class ConfigUpdate(BaseModel):
    """Campos editáveis via API. O agente não tem mais opções de formatação
    de impressão — tudo isso é controlado pelo EldenSys que gera os PDFs."""

    allowed_origins: list[str] | None = None
    log_level: str | None = None
    sumatra_path: str | None = None
    sumatra_label_disable_antialias: bool | None = None


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
    #: "label" pede rasterizacao sem anti-aliasing (termica 1-bit).
    #: Agentes <= 0.3.0 ignoram o campo (Pydantic descarta extras), entao
    #: o EldenSys pode mandar sempre, sem gate de versao.
    render_mode: Literal["default", "label"] = "default"


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


def sumatra_path_aceito(caminho: str) -> bool:
    """O ``sumatra_path`` vira o programa executado a cada PDF impresso.

    Só aceita um ``SumatraPDF*.exe`` que exista NESTE computador (caminho
    absoluto, fora de compartilhamento de rede). Vazio volta à detecção
    automática (é o que "Restaurar padrões" grava).
    """
    caminho = caminho.strip()
    if not caminho:
        return True
    if caminho.startswith(("\\\\", "//")):
        return False
    p = Path(caminho)
    nome = p.name.lower()
    if not p.is_absolute() or not (nome.startswith("sumatrapdf") and nome.endswith(".exe")):
        return False
    try:
        return p.is_file()
    except OSError:
        return False


def _erro(status: int, erro: str, mensagem: str) -> JSONResponse:
    """Erro no formato das rotas novas: ``{erro, mensagem, detail}``.

    ``detail`` repete a mensagem para quem só lê ``detail`` (printAgent.ts).
    """
    return JSONResponse(
        status_code=status, content={"erro": erro, "mensagem": mensagem, "detail": mensagem}
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


def create_app(
    cfg: AgentConfig | None = None, biometria: ServicoBiometria | None = None
) -> FastAPI:
    cfg = cfg or load_config()
    app = FastAPI(title="EldenSys Print Agent", version=__version__)

    # Perfil -appdata do modo etiqueta: UMA escrita, aqui no startup.
    # Reescrever por job disputaria com o proprio Sumatra, que regrava o
    # arquivo ao sair. Nunca levanta.
    ensure_label_appdata()

    if biometria is None:
        # Construir não abre o leitor nem carrega a DLL: tudo é preguiçoso.
        from .biometria.servico import ServicoBiometria

        biometria = ServicoBiometria()
    # Rotas /biometria + o middleware de origem, que envolve o CORS `*` das
    # rotas de impressão (o `*` nunca chega a uma resposta da biometria).
    # `load_config` resolvido a cada pedido: o config.json pode mudar sem
    # reiniciar (origens extras, biometria ligada/desligada).
    rotas_biometria.instalar(
        app,
        biometria,
        load_config_fn=lambda: load_config(),
        cors={
            "allow_origins": cfg.allowed_origins,
            "allow_origin_regex": cfg.allowed_origin_regex or None,
            "allow_credentials": False,
            "allow_methods": ["GET", "POST", "OPTIONS"],
            "allow_headers": ["*"],
        },
    )

    # ── Health ─────────────────────────────────────────
    @app.get("/health")
    def health() -> dict:
        return {
            "status": "ok",
            "service": "eldensys-agent",
            "version": __version__,
            "recursos": list(RECURSOS),
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
            "sumatra_label_disable_antialias": c.sumatra_label_disable_antialias,
        }

    @app.post("/config")
    def config_set(payload: ConfigUpdate, request: Request):
        c = load_config()
        # O CORS `*` deixa qualquer site aberto no navegador da loja chegar
        # aqui, e `sumatra_path` é o programa que o agente executa no próximo
        # PDF. O navegador SEMPRE manda Origin num POST de outra origem (até
        # num ataque de DNS rebinding); sem Origin = ferramenta local (curl,
        # testes), que já roda nesta máquina.
        origem = request.headers.get("origin")
        if origem is not None and not origem_permitida_cfg(origem, c):
            log.warning("POST /config recusado: origem %r", origem[:200])
            return _erro(
                403,
                "origem_recusada",
                "Este site não pode alterar a configuração do EldenSys Agent.",
            )
        if payload.sumatra_path is not None and not sumatra_path_aceito(payload.sumatra_path):
            return _erro(
                422,
                "sumatra_invalido",
                "O caminho do SumatraPDF precisa apontar para um arquivo "
                "SumatraPDF*.exe existente neste computador.",
            )
        # Todas as alterações de infra (CORS, log, sumatra) exigem restart.
        restart = False
        if payload.allowed_origins is not None:
            c.allowed_origins = payload.allowed_origins
            restart = True
        if payload.log_level is not None:
            c.log_level = payload.log_level
            restart = True
        if payload.sumatra_path is not None:
            c.sumatra_path = payload.sumatra_path.strip()
        if payload.sumatra_label_disable_antialias is not None:
            # Vale a partir do proximo job: e lido em print_pdf.
            c.sumatra_label_disable_antialias = payload.sumatra_label_disable_antialias
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
                render_mode=payload.render_mode,
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
