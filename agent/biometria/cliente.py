"""Chamadas do agente às rotas de biometria do EldenSys (contrato, seção 2.2).

Reaproveita o ``RemoteClient`` do canal remoto (token ``esa_…``, ``X-Tenant``,
``X-Agent-Instance``, sem seguir redirect) com ``negocio=True``: uma recusa da
regra ("desafio expirado", "sem vínculo") vira ``ServidorRecusou`` em vez de
"pareamento recusado". Só agente LIBERADO pelo administrador passa (o
servidor responde 403 ``biometria_agente_nao_liberado`` aos outros).

O template só existe aqui em trânsito: vem do servidor para o leitor
(``buscar``/``desafio``) e do leitor para o servidor (``enviar_digital``).
Nunca vai para log, disco ou navegador.
"""

from __future__ import annotations

import uuid
from typing import Any

from ..config import AgentConfig
from ..remote_client import RemoteClient, TransportError
from .sdk import id_da_loja_valido, template_valido

BASE = "/agent/biometria"
#: Contrato: no máximo 200 ids por ``buscar``.
LOTE_BUSCA = 200

PRAZO_LISTA_S = 30.0
PRAZO_DESAFIO_S = 15.0


def uuid_canonico(valor: object) -> str:
    """UUID em texto canônico (minúsculo, com hífens). ``ValueError`` se não for."""
    if not isinstance(valor, str):
        raise ValueError("id não é texto")
    return str(uuid.UUID(valor.strip()))


def digitais_validas(valor: object) -> list[tuple[int, str]]:
    """``[{"device_id", "template"}]`` do servidor → pares conferidos.

    Item malformado é descartado em silêncio (sem log do conteúdo: pode ser
    um template).
    """
    if not isinstance(valor, list):
        return []
    pares: list[tuple[int, str]] = []
    for item in valor:
        if not isinstance(item, dict):
            continue
        device_id, template = item.get("device_id"), item.get("template")
        if id_da_loja_valido(device_id) and template_valido(template):
            pares.append((device_id, template))
    return pares


class ClienteBiometria:
    def __init__(self, remote: Any) -> None:
        self._rc = remote

    @classmethod
    def da_config(cls, cfg: AgentConfig) -> ClienteBiometria:
        return cls(RemoteClient(cfg.server_url, cfg.server_tenant, cfg.server_token, cfg.instance_id))

    def _chamar(self, method: str, path: str, body: dict | None = None, **kw: Any) -> dict:
        return self._rc._request(method, BASE + path, body, negocio=True, **kw)

    # ── espelho ──
    def digitais(self, versao: str | None) -> dict:
        """``{"versao","igual":true}`` ou ``{"versao","limite","usuarios","clientes","removidos"}``."""
        return self._chamar(
            "GET", "/digitais/", query={"versao": versao or None}, timeout=PRAZO_LISTA_S
        )

    def buscar(self, ids: list[int]) -> list[tuple[int, str]]:
        """Templates de até 200 ``device_id`` → ``[(device_id, template)]``."""
        if len(ids) > LOTE_BUSCA:
            raise ValueError(f"no máximo {LOTE_BUSCA} ids por busca")
        resp = self._chamar(
            "POST", "/digitais/buscar/", {"ids": [int(i) for i in ids]}, timeout=PRAZO_LISTA_S
        )
        if not isinstance(resp.get("digitais"), list):
            raise TransportError("Resposta inesperada do EldenSys ao buscar digitais.")
        return digitais_validas(resp["digitais"])

    # ── desafios ──
    def desafio(self, desafio_id: str) -> dict:
        did = uuid_canonico(desafio_id)
        return self._chamar("GET", f"/desafios/{did}/", timeout=PRAZO_DESAFIO_S)

    def resultado(
        self,
        desafio_id: str,
        *,
        device_id: int | None,
        erro: str | None,
        score: int,
        qualidade: int,
        leitor: dict,
    ) -> dict:
        did = uuid_canonico(desafio_id)
        corpo = {
            "device_id": device_id,
            "erro": erro,
            "score": int(score),
            "qualidade": int(qualidade),
            "leitor": leitor,
        }
        return self._chamar("POST", f"/desafios/{did}/resultado/", corpo, timeout=PRAZO_DESAFIO_S)

    def enviar_digital(self, desafio_id: str, *, template: str, qualidade: int, leitor: dict) -> dict:
        did = uuid_canonico(desafio_id)
        corpo = {"template": template, "qualidade": int(qualidade), "leitor": leitor}
        return self._chamar("POST", f"/desafios/{did}/digital/", corpo, timeout=PRAZO_DESAFIO_S)

    # ── login ──
    def login(
        self, *, device_id: int, score: int, qualidade: int, challenge: str, leitor: dict
    ) -> dict:
        corpo = {
            "device_id": int(device_id),
            "score": int(score),
            "qualidade": int(qualidade),
            "challenge": challenge,
            "leitor": leitor,
        }
        return self._chamar("POST", "/login/", corpo, timeout=PRAZO_DESAFIO_S)
