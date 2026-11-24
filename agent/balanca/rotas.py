"""Rotas locais da balança de checkout em ``http://127.0.0.1:17777/balanca``.

Quem chama é o PDV online do EldenSys aberto NESTE computador. Origem, Host
e CORS são conferidos antes, no ``biometria.origem.MiddlewareOrigem`` (a
mesma política da biometria: ``/balanca`` é rota protegida), e o corpo
inválido vira ``422 requisicao_invalida`` no tratador instalado pela
biometria.

- ``POST /balanca/detectar`` é ``async``: o DETECTAR leva até ~8 s; se o
  navegador fechar a aba ou abortar o ``fetch``, a detecção para na próxima
  tentativa em vez de segurar a porta.
- As demais são ``def`` síncronas (rodam no pool de threads do FastAPI): a
  leitura é curta e a fila do leitor já serializa a porta.
- Erro sai como ``{"erro", "mensagem", "detail"}``; o 500 nunca carrega o
  texto da exceção (vai só para o log).
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictBool, StrictInt, StrictStr

from .erros import ErroBalanca

log = logging.getLogger("eldensys.agent.balanca.rotas")

#: De quanto em quanto tempo a rota confere se o navegador ainda está lá.
INTERVALO_DESCONEXAO_S = 0.25
MSG_INTERNO = "Erro interno no EldenSys Agent. Veja o log do agente."


class CorpoConfig(BaseModel):
    habilitada: StrictBool | None = None
    porta: StrictStr | None = None
    baud: StrictInt | None = None
    protocolo: StrictStr | None = None
    timeout_ms: StrictInt | None = None


class CorpoTeste(BaseModel):
    porta: StrictStr
    baud: StrictInt
    protocolo: StrictStr


class CorpoLeitura(BaseModel):
    timeout_ms: StrictInt | None = None
    teste: CorpoTeste | None = None
    diagnostico: StrictBool = False


class CorpoDetectar(BaseModel):
    porta: StrictStr | None = None
    baud: StrictInt | None = None


def _erro(e: ErroBalanca) -> JSONResponse:
    return JSONResponse(status_code=e.status, content=e.corpo())


def _interno() -> JSONResponse:
    return JSONResponse(
        status_code=500, content={"erro": "interno", "mensagem": MSG_INTERNO, "detail": MSG_INTERNO}
    )


def _responder(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except ErroBalanca as e:
        return _erro(e)
    except Exception:
        log.exception("Falha inesperada numa rota da balança")
        return _interno()


async def com_cancelamento(request: Any, fn: Callable[[threading.Event], Any]) -> Any:
    """Roda ``fn`` numa thread; navegador desconectado = liga o cancelamento.

    Cópia do ``com_captura`` da biometria, com um ``threading.Event`` no lugar
    da ficha do leitor biométrico.
    """
    cancelamento = threading.Event()
    loop = asyncio.get_running_loop()
    fut = loop.run_in_executor(None, fn, cancelamento)
    while True:
        feitos, _ = await asyncio.wait({fut}, timeout=INTERVALO_DESCONEXAO_S)
        if feitos:
            break
        if await request.is_disconnected():
            log.info("Navegador saiu durante o DETECTAR da balança: cancelando")
            cancelamento.set()
            await asyncio.wait({fut})
            break
    try:
        return fut.result()
    except ErroBalanca as e:
        return _erro(e)
    except Exception:
        log.exception("Falha inesperada no DETECTAR da balança")
        return _interno()


def criar_router(servico: Any) -> APIRouter:
    r = APIRouter(prefix="/balanca")

    @r.get("/status")
    def status() -> Any:
        return _responder(servico.status)

    @r.get("/portas")
    def portas() -> Any:
        return _responder(servico.portas)

    @r.post("/config")
    def config(corpo: CorpoConfig) -> Any:
        return _responder(lambda: servico.salvar_config(corpo.model_dump()))

    @r.post("/leitura")
    def leitura(corpo: CorpoLeitura | None = None) -> Any:
        c = corpo or CorpoLeitura()
        return _responder(
            lambda: servico.ler(
                timeout_ms=c.timeout_ms,
                teste=c.teste.model_dump() if c.teste is not None else None,
                diagnostico=c.diagnostico,
            )
        )

    @r.post("/detectar")
    async def detectar(request: Request, corpo: CorpoDetectar | None = None) -> Any:
        c = corpo or CorpoDetectar()
        return await com_cancelamento(
            request, lambda tok: servico.detectar(porta=c.porta, baud=c.baud, cancelamento=tok)
        )

    @r.post("/liberar")
    def liberar() -> Any:
        return _responder(servico.liberar)

    return r


def instalar(app: FastAPI, servico: Any) -> None:
    """Liga as rotas ``/balanca`` no app.

    O middleware de origem e o tratador de corpo inválido são os da
    biometria (``biometria.rotas.instalar``), que cobrem ``/balanca`` também.
    """
    app.include_router(criar_router(servico))
