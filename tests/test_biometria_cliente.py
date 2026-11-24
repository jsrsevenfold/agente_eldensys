"""Chamadas às rotas ``/api/agent/biometria/*`` (contrato, seção 2.2).

Usa o ``RemoteClient`` de verdade com o ``opener`` trocado: confere método,
URL, corpo e o mapeamento dos status de negócio.
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.biometria.cliente import ClienteBiometria, digitais_validas, uuid_canonico  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.remote_client import (  # noqa: E402
    ModuloInativo,
    PairingRejected,
    RemoteClient,
    ServidorRecusou,
    StoreBlocked,
    TransportError,
)
from fake_idbio import tpl  # noqa: E402

TOKEN = "esa_" + "k" * 43
DESAFIO = "123e4567-e89b-42d3-a456-426614174000"
API = "https://api.eldensys.com.br/api"


class _Resp:
    def __init__(self, corpo: bytes):
        self._c = corpo

    def read(self, n=-1):
        return self._c

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class Opener:
    def __init__(self, *respostas):
        self.respostas = list(respostas)
        self.reqs: list = []
        self.timeouts: list = []

    def open(self, req, timeout=None):
        self.reqs.append(req)
        self.timeouts.append(timeout)
        r = self.respostas.pop(0) if self.respostas else {}
        if isinstance(r, Exception):
            raise r
        return _Resp(json.dumps(r).encode())


def erro(status, corpo=None):
    return urllib.error.HTTPError(API, status, "x", {}, io.BytesIO(json.dumps(corpo or {}).encode()))


def cliente(*respostas):
    op = Opener(*respostas)
    return ClienteBiometria(RemoteClient(API, "loja_centro", TOKEN, "inst-1", opener=op)), op


def test_da_config_usa_o_pareamento():
    c = ClienteBiometria.da_config(
        AgentConfig(server_url=API, server_tenant="loja", server_token=TOKEN, instance_id="i-123456")
    )
    assert (c._rc.url, c._rc.tenant, c._rc.instance_id) == (API, "loja", "i-123456")


def test_digitais_com_e_sem_versao():
    c, op = cliente({"versao": "v", "igual": True}, {"versao": "v2", "usuarios": []})
    assert c.digitais("3:2026-11-24") == {"versao": "v", "igual": True}
    assert c.digitais(None) == {"versao": "v2", "usuarios": []}
    assert op.reqs[0].get_method() == "GET"
    assert op.reqs[0].full_url == f"{API}/agent/biometria/digitais/?versao=3%3A2026-11-24"
    assert op.reqs[1].full_url == f"{API}/agent/biometria/digitais/"
    assert op.reqs[0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert op.timeouts[0] == 30.0


def test_buscar_ate_200_e_filtra_o_que_veio_estranho():
    resposta = {
        "digitais": [
            {"device_id": 5, "template": tpl(5)},
            {"device_id": 6, "template": "curto"},
            {"device_id": "7", "template": tpl(7)},
            {"device_id": 2**60, "template": tpl(8)},
            "lixo",
        ]
    }
    c, op = cliente(resposta)
    assert c.buscar([5, 6, 7]) == [(5, tpl(5))]
    req = op.reqs[0]
    assert (req.get_method(), req.full_url) == ("POST", f"{API}/agent/biometria/digitais/buscar/")
    assert json.loads(req.data) == {"ids": [5, 6, 7]}
    with pytest.raises(ValueError):
        c.buscar(list(range(201)))


def test_buscar_resposta_sem_lista_e_transporte():
    c, _ = cliente({"outra": 1})
    with pytest.raises(TransportError):
        c.buscar([1])


def test_desafio_e_resultado():
    c, op = cliente({"id": DESAFIO, "status": "pendente"}, {"status": "aprovado", "nome": "Ana"})
    assert c.desafio(DESAFIO.upper())["status"] == "pendente"
    assert op.reqs[0].full_url == f"{API}/agent/biometria/desafios/{DESAFIO}/"
    assert op.reqs[0].get_method() == "GET"
    leitor = {"modelo": "iDBio Pro", "serie": "S1", "firmware": "1.2"}
    resp = c.resultado(DESAFIO, device_id=99, erro=None, score=9000, qualidade=80, leitor=leitor)
    assert resp == {"status": "aprovado", "nome": "Ana"}
    assert op.reqs[1].full_url == f"{API}/agent/biometria/desafios/{DESAFIO}/resultado/"
    assert json.loads(op.reqs[1].data) == {
        "device_id": 99,
        "erro": None,
        "score": 9000,
        "qualidade": 80,
        "leitor": leitor,
    }


def test_nao_identificado_vai_com_device_id_nulo():
    c, op = cliente({"status": "recusado"})
    c.resultado(DESAFIO, device_id=None, erro="nao_identificado", score=0, qualidade=0, leitor={})
    assert json.loads(op.reqs[0].data)["device_id"] is None
    assert json.loads(op.reqs[0].data)["erro"] == "nao_identificado"


@pytest.mark.parametrize("ruim", ["../../agent/poll", "123", "", None])
def test_id_do_desafio_e_conferido_antes_da_rede(ruim):
    c, op = cliente()
    with pytest.raises(ValueError):
        c.desafio(ruim)
    assert op.reqs == []
    with pytest.raises(ValueError):
        uuid_canonico(ruim)


def test_enviar_digital_e_login():
    c, op = cliente(
        {"status": "aprovado", "digital_id": "d", "device_id": 1, "nome": "Ana"},
        {"ticket": "t", "expira_em": "x", "nome": "Ana", "email": "a@b", "tenant": "l", "loja": "L"},
    )
    c.enviar_digital(DESAFIO, template=tpl(1), qualidade=77, leitor={"serie": "S1"})
    assert op.reqs[0].full_url == f"{API}/agent/biometria/desafios/{DESAFIO}/digital/"
    assert json.loads(op.reqs[0].data) == {
        "template": tpl(1),
        "qualidade": 77,
        "leitor": {"serie": "S1"},
    }
    c.login(device_id=5, score=1, qualidade=2, challenge="c" * 43, leitor={})
    assert op.reqs[1].full_url == f"{API}/agent/biometria/login/"
    assert json.loads(op.reqs[1].data)["challenge"] == "c" * 43


@pytest.mark.parametrize(
    ("resposta", "esperado"),
    [
        (erro(401, {"detail": "x", "code": "agent_token_invalid"}), PairingRejected),
        (erro(403, {"detail": "x", "module": "biometria"}), ModuloInativo),
        (erro(403, {"detail": "x", "code": "tenant_blocked"}), StoreBlocked),
        (erro(403, {"detail": "PC não liberado.", "code": "biometria_agente_nao_liberado"}), ServidorRecusou),
        (erro(403, {"detail": "Sem vínculo.", "code": "biometria_sem_vinculo"}), ServidorRecusou),
        (erro(404, {"detail": "Não encontrado.", "code": "not_found"}), ServidorRecusou),
        (erro(410, {"detail": "Expirou.", "code": "biometria_desafio_expirado"}), ServidorRecusou),
        (erro(409, {"detail": "Usado.", "code": "biometria_desafio_usado"}), ServidorRecusou),
        (erro(500, {"detail": "boom"}), TransportError),
    ],
)
def test_status_de_negocio(resposta, esperado):
    c, _ = cliente(resposta)
    with pytest.raises(esperado):
        c.desafio(DESAFIO)


def test_404_sem_code_e_servidor_sem_biometria():
    c, _ = cliente(urllib.error.HTTPError(API, 404, "x", {}, io.BytesIO(b"<html>Not Found</html>")))
    with pytest.raises(ServidorRecusou) as exc:
        c.digitais(None)
    assert exc.value.code == "biometria_indisponivel"


def test_digitais_validas_aceita_so_pares_bons():
    assert digitais_validas(None) == []
    assert digitais_validas([{"device_id": True, "template": tpl(1)}]) == []
    assert digitais_validas([{"device_id": 1, "template": tpl(1)}]) == [(1, tpl(1))]
