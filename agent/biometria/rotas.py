"""Rotas locais da biometria (contrato, seção 3) em ``http://localhost:17777``.

Quem chama é a retaguarda do EldenSys aberta NESTE computador. Origem, Host
e CORS são conferidos antes, no ``origem.MiddlewareOrigem``.

- As rotas que esperam o dedo são ``async``: a leitura roda numa thread e a
  rota olha a conexão; se o navegador fechar a aba ou abortar o ``fetch``, a
  captura é cancelada (``CancelCapture``) em vez de prender o leitor por 30 s.
- ``GET /biometria/status`` não tem efeito colateral: não abre o leitor nem
  chama o servidor.
- Erro sai como ``{"erro", "mensagem", "detail"}``; o 500 nunca carrega o
  texto da exceção (vai só para o log).
- Nenhuma resposta carrega template: o que vem do servidor passa por um
  filtro, e o que vem do leitor fica no agente.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .erros import ErroBiometria
from .leitor import Cancelamento
from .origem import MiddlewareOrigem, eh_rota_balanca, eh_rota_biometria

log = logging.getLogger("eldensys.agent.biometria.rotas")

#: De quanto em quanto tempo a rota confere se o navegador ainda está lá.
INTERVALO_DESCONEXAO_S = 0.25
MSG_INTERNO = "Erro interno no EldenSys Agent. Veja o log do agente."
_SESSAO_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


class CorpoDesafio(BaseModel):
    desafio_id: str = ""
    tenant: str | None = None


class CorpoLogin(BaseModel):
    challenge: str = ""
    tenant: str | None = None


def _erro(e: ErroBiometria) -> JSONResponse:
    return JSONResponse(status_code=e.status, content=e.corpo())


def _interno() -> JSONResponse:
    return JSONResponse(
        status_code=500, content={"erro": "interno", "mensagem": MSG_INTERNO, "detail": MSG_INTERNO}
    )


def _responder(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except ErroBiometria as e:
        return _erro(e)
    except Exception:
        log.exception("Falha inesperada numa rota da biometria")
        return _interno()


def _sessao(sessao_id: str) -> str:
    if not _SESSAO_RE.fullmatch(sessao_id or ""):
        raise ErroBiometria(
            "sessao_inexistente",
            "Este cadastro expirou ou foi descartado. Comece de novo.",
            status=404,
        )
    return sessao_id


async def com_captura(request: Any, fn: Callable[[Cancelamento], Any]) -> Any:
    """Roda ``fn`` numa thread; navegador desconectado = cancela a leitura."""
    tok = Cancelamento()
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(None, fn, tok)
    while True:
        feitos, _ = await asyncio.wait({fut}, timeout=INTERVALO_DESCONEXAO_S)
        if feitos:
            break
        if await request.is_disconnected():
            log.info("Navegador saiu durante a leitura: cancelando a captura")
            tok.cancelar()
            await asyncio.wait({fut})
            break
    try:
        return fut.result()
    except ErroBiometria as e:
        return _erro(e)
    except Exception:
        log.exception("Falha inesperada numa leitura da biometria")
        return _interno()


def criar_router(servico: Any) -> APIRouter:
    r = APIRouter(prefix="/biometria")

    @r.get("/status")
    def status() -> Any:
        return _responder(servico.status)

    @r.post("/testar")
    def testar() -> Any:
        return _responder(servico.testar)

    @r.post("/identificar")
    async def identificar(corpo: CorpoDesafio, request: Request) -> Any:
        return await com_captura(
            request, lambda tok: servico.identificar(corpo.desafio_id, corpo.tenant, tok)
        )

    @r.post("/login")
    async def login(corpo: CorpoLogin, request: Request) -> Any:
        return await com_captura(
            request, lambda tok: servico.login(corpo.challenge, corpo.tenant, tok)
        )

    @r.post("/cadastro/iniciar")
    def cadastro_iniciar(corpo: CorpoDesafio) -> Any:
        return _responder(lambda: servico.cadastro_iniciar(corpo.desafio_id, corpo.tenant))

    @r.post("/cadastro/{sessao_id}/verificar")
    async def cadastro_verificar(sessao_id: str, request: Request) -> Any:
        return await com_captura(
            request, lambda tok: servico.cadastro_verificar(_sessao(sessao_id), tok)
        )

    @r.post("/cadastro/{sessao_id}/captura")
    async def cadastro_captura(sessao_id: str, request: Request) -> Any:
        return await com_captura(
            request, lambda tok: servico.cadastro_capturar(_sessao(sessao_id), tok)
        )

    @r.post("/cadastro/{sessao_id}/concluir")
    def cadastro_concluir(sessao_id: str) -> Any:
        return _responder(lambda: servico.cadastro_concluir(_sessao(sessao_id)))

    @r.post("/cadastro/{sessao_id}/descartar")
    def cadastro_descartar(sessao_id: str) -> Any:
        return _responder(lambda: servico.cadastro_descartar(sessao_id))

    @r.post("/cancelar")
    def cancelar() -> Any:
        return _responder(servico.cancelar)

    @r.post("/sincronizar")
    def sincronizar() -> Any:
        resposta = _responder(servico.sincronizar)
        if isinstance(resposta, dict):
            return JSONResponse(status_code=202, content=resposta)
        return resposta

    return r


def instalar(
    app: FastAPI, servico: Any, *, load_config_fn: Callable[[], object], cors: dict[str, Any]
) -> None:
    """Liga a biometria no app: rotas, middleware de origem e erro de validação.

    ``cors`` são os argumentos do CORS ``*`` das demais rotas (impressão): o
    middleware de origem envolve esse CORS e só o aplica fora das rotas
    protegidas (/biometria e, desde a v0.7, /balanca — o middleware e este
    tratador valem para as duas; ``balanca.rotas.instalar`` só põe as rotas).
    """
    app.include_router(criar_router(servico))
    app.add_middleware(MiddlewareOrigem, load_config_fn=load_config_fn, cors=cors)

    async def validacao(request: Request, exc: RequestValidationError) -> Any:
        path = request.url.path
        if eh_rota_biometria(path) or eh_rota_balanca(path):
            msg = (
                "Pedido inválido para a balança."
                if eh_rota_balanca(path)
                else "Pedido inválido para o leitor biométrico."
            )
            return JSONResponse(
                status_code=422,
                content={"erro": "requisicao_invalida", "mensagem": msg, "detail": msg},
            )
        return await request_validation_exception_handler(request, exc)

    app.add_exception_handler(RequestValidationError, validacao)
