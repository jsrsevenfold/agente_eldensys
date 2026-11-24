"""Política de origem das rotas locais sensíveis do agente.

O agente escuta em ``http://127.0.0.1:17777`` e as rotas de impressão aceitam
qualquer site (CORS ``*``): qualquer página aberta no navegador da loja
alcança o loopback. Para imprimir isso é aceitável; para gravar configuração
ou mexer no leitor biométrico, não. Estas rotas só atendem:

- ``https://eldensys.hubdesktop.com.br`` e os subdomínios dele (produção);
- ``http://localhost:5173`` e ``http://127.0.0.1:5173`` (Vite), só com
  ``biometria_permitir_dev = true`` no config.json;
- as origens extras de ``biometria_origens_extras`` — só pelo config.json,
  nunca por rota HTTP (senão um site se autorizaria sozinho).

O regex de ``config.DEFAULT_ALLOWED_ORIGIN_REGEX`` NÃO serve aqui: ele não
cobre o domínio de produção e só existe para o CORS das rotas de impressão.

Nas rotas ``/biometria*`` o ``MiddlewareOrigem`` também:

- confere o ``Host`` (só ``localhost``/``127.0.0.1``/``[::1]``): sem isso um
  site com DNS apontando para 127.0.0.1 (DNS rebinding) falaria com o agente
  como se fosse a mesma origem;
- recusa ``Origin: null`` e qualquer POST sem ``Origin`` (o navegador sempre
  manda ``Origin`` num POST; sem ele, não é a retaguarda);
- responde o próprio CORS: ecoa a origem aceita (nunca ``*``) e atende o
  preflight com ``Access-Control-Allow-Private-Network: true`` (o Chrome pede
  isso para um site público falar com o loopback).

O resto das rotas segue com o CORS ``*`` de sempre (impressão).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, Response

log = logging.getLogger("eldensys.agent.biometria.origem")

DOMINIO_PRODUCAO = "eldensys.hubdesktop.com.br"

#: Origem serializada pelo navegador: esquema + host em minúsculas, sem porta
#: padrão e sem barra final. Âncoras dos dois lados: ``…com.br.evil.com`` e
#: ``evil-eldensys.hubdesktop.com.br`` ficam de fora.
_PRODUCAO_RE = re.compile(r"^https://(?:[a-z0-9-]+\.)*eldensys\.hubdesktop\.com\.br$")

ORIGENS_DEV = frozenset({"http://localhost:5173", "http://127.0.0.1:5173"})


def _normalizar(origem: str) -> str:
    return origem.strip().rstrip("/").lower()


def origens_extras(valor: object) -> frozenset[str]:
    """``biometria_origens_extras`` saneada (o config.json é editado à mão).

    Curinga e ``null`` são descartados: uma linha errada no arquivo não pode
    virar "qualquer site".
    """
    if not isinstance(valor, (list, tuple)):
        return frozenset()
    saida: set[str] = set()
    for item in valor:
        if not isinstance(item, str):
            continue
        origem = _normalizar(item)
        if "*" in origem or not origem.startswith(("https://", "http://")):
            continue
        saida.add(origem)
    return frozenset(saida)


def origem_permitida(
    origem: str | None, *, permitir_dev: bool = False, extras: Iterable[str] = ()
) -> bool:
    """True se ``origem`` (cabeçalho ``Origin`` cru) pode usar as rotas sensíveis.

    ``Origin: null`` (iframe com sandbox, arquivo local, redirecionamento
    entre origens) é sempre recusada.
    """
    if not origem or origem == "null":
        return False
    if _PRODUCAO_RE.fullmatch(origem):
        return True
    if permitir_dev and origem in ORIGENS_DEV:
        return True
    return origem in frozenset(extras)


def origem_permitida_cfg(origem: str | None, cfg: object) -> bool:
    """``origem_permitida`` com as chaves do config.json.

    ``is True`` de propósito: ``"biometria_permitir_dev": "false"`` (texto)
    escrito à mão não pode ligar as origens de desenvolvimento.
    """
    return origem_permitida(
        origem,
        permitir_dev=getattr(cfg, "biometria_permitir_dev", False) is True,
        extras=origens_extras(getattr(cfg, "biometria_origens_extras", None)),
    )


# ── Host e interface de escuta ───────────────────────────────────────────
NOMES_LOOPBACK = frozenset({"localhost", "127.0.0.1", "::1"})
PREFIXO_BIOMETRIA = "/biometria"
_CABECALHOS_SEGUROS_RE = re.compile(r"^[A-Za-z0-9-]+(\s*,\s*[A-Za-z0-9-]+)*$")


def host_loopback(host: str | None) -> bool:
    """``Host`` de um pedido feito ao loopback (porta qualquer)."""
    if not host:
        return False
    h = host.strip().lower()
    if h.startswith("["):
        fim = h.find("]")
        if fim < 0 or (h[fim + 1 :] and not re.fullmatch(r":\d{1,5}", h[fim + 1 :])):
            return False
        return h[1:fim] in NOMES_LOOPBACK
    if h.count(":") > 1:
        return False  # IPv6 sem colchetes não é um Host válido
    nome, _, porta = h.partition(":")
    if porta and not porta.isdigit():
        return False
    return nome in NOMES_LOOPBACK


def agente_em_loopback(cfg: object) -> bool:
    """O agente só atende este computador? (``host`` do config.json)

    Com ``0.0.0.0`` (ou o IP da rede) qualquer máquina da loja falaria com o
    agente forjando ``Host``/``Origin``: a biometria fica desligada.
    """
    return str(getattr(cfg, "host", "") or "").strip().lower() in NOMES_LOOPBACK


def eh_rota_biometria(path: str) -> bool:
    return path == PREFIXO_BIOMETRIA or path.startswith(PREFIXO_BIOMETRIA + "/")


def _recusa(scope: dict, headers: Headers, cfg: object) -> str | None:
    """Motivo da recusa (para o log) ou None."""
    if not host_loopback(headers.get("host")):
        return "Host fora do loopback"
    origem = headers.get("origin")
    if origem is None:
        if scope.get("method") in ("GET", "HEAD"):
            return None  # ferramenta local (curl); o navegador sempre manda Origin
        return "pedido sem Origin"
    if not origem_permitida_cfg(origem, cfg):
        return "origem fora da política"
    return None


def _cabecalhos_preflight(origem: str, headers: Headers) -> dict[str, str]:
    pedidos = (headers.get("access-control-request-headers") or "").strip()
    return {
        "Access-Control-Allow-Origin": origem,
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": (
            pedidos if pedidos and _CABECALHOS_SEGUROS_RE.fullmatch(pedidos) else "Content-Type"
        ),
        "Access-Control-Allow-Private-Network": "true",
        "Access-Control-Max-Age": "600",
        "Vary": "Origin, Access-Control-Request-Headers, Access-Control-Request-Private-Network",
    }


class MiddlewareOrigem:
    """ASGI: CORS ``*`` nas rotas de impressão, política própria em ``/biometria``.

    Envolve o ``CORSMiddleware`` em vez de ficar por fora dele: assim o ``*``
    nunca é acrescentado a uma resposta da biometria.
    """

    def __init__(
        self, app: Any, *, load_config_fn: Callable[[], object], cors: dict[str, Any]
    ) -> None:
        self.app = app
        self.cors = CORSMiddleware(app, **cors)
        self._load_config = load_config_fn

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http" or not eh_rota_biometria(scope.get("path", "")):
            await self.cors(scope, receive, send)
            return
        headers = Headers(scope=scope)
        motivo = _recusa(scope, headers, self._load_config())
        if motivo is not None:
            log.warning(
                "Biometria: pedido recusado (%s): %s %s Origin=%r Host=%r",
                motivo,
                scope.get("method"),
                scope.get("path"),
                (headers.get("origin") or "")[:200],
                (headers.get("host") or "")[:200],
            )
            resposta = JSONResponse(
                status_code=403,
                content={
                    "erro": "origem_recusada",
                    "mensagem": "Este site não pode usar o leitor biométrico deste computador.",
                    "detail": "Este site não pode usar o leitor biométrico deste computador.",
                },
            )
            await resposta(scope, receive, send)
            return
        origem = headers.get("origin")
        if (
            scope.get("method") == "OPTIONS"
            and origem
            and "access-control-request-method" in headers
        ):
            await Response(status_code=204, headers=_cabecalhos_preflight(origem, headers))(
                scope, receive, send
            )
            return

        async def send_com_cors(message: dict) -> None:
            if message["type"] == "http.response.start" and origem:
                h = MutableHeaders(scope=message)
                h["Access-Control-Allow-Origin"] = origem
                h.add_vary_header("Origin")
            await send(message)

        await self.app(scope, receive, send_com_cors)
