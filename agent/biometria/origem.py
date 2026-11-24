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
"""

from __future__ import annotations

import re
from collections.abc import Iterable

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
