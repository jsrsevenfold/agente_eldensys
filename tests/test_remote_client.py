"""Pareamento e transporte do canal remoto — o contrato com o EldenSys.

Sem rede: o `opener` do urllib e trocado por um dublê que guarda a
requisicao montada e devolve a resposta combinada.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import __version__  # noqa: E402
from agent.remote_client import (  # noqa: E402
    CODE_SEM_BIOMETRIA,
    ModuloInativo,
    PairingCodeError,
    PairingRejected,
    RateLimited,
    RemoteClient,
    ServidorRecusou,
    StoreBlocked,
    TransportError,
    parse_pairing_code,
)

TOKEN = "esa_" + "a1B2c3D4" * 5 + "xyz"


def codigo(u="https://api.eldensys.com.br/api", t="loja_centro", k=TOKEN, **extra) -> str:
    dados = {"u": u, "t": t, "k": k, **extra}
    corpo = base64.urlsafe_b64encode(json.dumps(dados).encode()).decode().rstrip("=")
    return "ELD1-" + corpo


# ── código de pareamento ─────────────────────────────────────────────────
def test_codigo_valido_e_decodificado():
    p = parse_pairing_code(codigo())
    assert (p.url, p.tenant, p.token) == ("https://api.eldensys.com.br/api", "loja_centro", TOKEN)


def test_codigo_colado_com_espacos_quebras_e_barra_final():
    c = codigo(u="https://api.eldensys.com.br/api/")
    sujo = "  " + c[:20] + "\n" + c[20:40] + " \r\n" + c[40:] + "\n"
    assert parse_pairing_code(sujo).url == "https://api.eldensys.com.br/api"


def test_http_so_e_aceito_em_localhost():
    assert parse_pairing_code(codigo(u="http://localhost:8000/api")).url.startswith("http://")
    assert parse_pairing_code(codigo(u="http://127.0.0.1:8000/api")).tenant == "loja_centro"
    # O token viaja em todo poll: http numa rede de loja seria entrega-lo.
    with pytest.raises(PairingCodeError, match="https"):
        parse_pairing_code(codigo(u="http://192.168.0.10:8000/api"))
    with pytest.raises(PairingCodeError, match="https"):
        parse_pairing_code(codigo(u="http://api.eldensys.com.br/api"))


@pytest.mark.parametrize(
    "ruim",
    [
        "",
        "qualquer coisa",
        "ELD1-",
        "ELD1-@@@nao-e-base64@@@",
        "ELD1-" + base64.urlsafe_b64encode(b"[1,2,3]").decode(),
        "ELD2-" + codigo()[5:],
        None,
        codigo(u=""),
        codigo(u="ftp://api.eldensys.com.br/api"),
        codigo(u="https://usuario:senha@api.eldensys.com.br/api"),
        codigo(t=""),
        codigo(t="loja centro"),
        codigo(t="loja\r\nX-Injetado: 1"),
        codigo(k="curto"),
        codigo(k=TOKEN + "\r\nX-Injetado: 1"),
        codigo(k=None),
    ],
)
def test_codigos_invalidos_sao_recusados(ruim):
    with pytest.raises(PairingCodeError):
        parse_pairing_code(ruim)


# ── transporte ───────────────────────────────────────────────────────────
class _Resposta:
    def __init__(self, corpo: bytes):
        self._corpo = corpo

    def read(self, n=-1):
        return self._corpo if n < 0 else self._corpo[:n]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Opener:
    """Dublê do OpenerDirector: guarda as requisicoes, devolve o combinado."""

    def __init__(self, resposta=None, erro=None):
        self.resposta = resposta
        self.erro = erro
        self.reqs: list = []
        self.timeouts: list = []

    def open(self, req, timeout=None):
        self.reqs.append(req)
        self.timeouts.append(timeout)
        if self.erro:
            raise self.erro
        corpo = self.resposta if isinstance(self.resposta, bytes) else json.dumps(self.resposta).encode()
        return _Resposta(corpo)


def cliente(opener) -> RemoteClient:
    return RemoteClient(
        "https://api.eldensys.com.br/api/", "loja_centro", TOKEN, "inst-123", opener=opener
    )


def http_error(status: int, corpo: dict | None = None, headers: dict | None = None):
    return urllib.error.HTTPError(
        "https://x", status, "erro", headers or {}, io.BytesIO(json.dumps(corpo or {}).encode())
    )


def test_poll_monta_url_headers_e_corpo():
    op = _Opener({"jobs": [], "poll_interval": 10})
    caps = {"scale": {"configured": True, "ok": True, "folder": "C:\\Toledo"}}
    resp = cliente(op).poll("0.5.0", "PC-ACOUGUE", caps)

    assert resp == {"jobs": [], "poll_interval": 10}
    req = op.reqs[0]
    # Barra final obrigatoria: o Django nao redireciona POST sem ela.
    assert req.full_url == "https://api.eldensys.com.br/api/agent/poll/"
    assert req.get_method() == "POST"
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"
    assert req.get_header("X-tenant") == "loja_centro"
    assert req.get_header("X-agent-instance") == "inst-123"
    assert req.get_header("User-agent") == f"EldenSysAgent/{__version__}"
    assert req.get_header("Content-type") == "application/json"
    assert json.loads(req.data) == {
        "agent_version": "0.5.0",
        "hostname": "PC-ACOUGUE",
        "capabilities": caps,
    }
    assert op.timeouts[0] and op.timeouts[0] > 0


def test_ack_escapa_o_id_e_manda_o_resultado():
    op = _Opener({})
    cliente(op).ack("ab/../cd", "done", "", {"paths": ["C:\\Toledo\\txitens.txt"]})
    req = op.reqs[0]
    assert req.full_url == "https://api.eldensys.com.br/api/agent/jobs/ab%2F..%2Fcd/ack/"
    assert json.loads(req.data) == {
        "status": "done",
        "detail": "",
        "result": {"paths": ["C:\\Toledo\\txitens.txt"]},
    }


def test_resposta_vazia_vale_como_objeto_vazio():
    assert cliente(_Opener(b"")).ack("1", "done") == {}


@pytest.mark.parametrize("status", [401, 404])
def test_401_e_404_pedem_novo_pareamento(status):
    op = _Opener(erro=http_error(status, {"detail": "Token revogado.", "code": "agent_token_invalid"}))
    with pytest.raises(PairingRejected) as exc:
        cliente(op).poll("0.5.0", "PC", {})
    assert str(exc.value) == "Token revogado."
    assert exc.value.code == "agent_token_invalid"


def test_403_e_loja_bloqueada():
    op = _Opener(erro=http_error(403, {"detail": "Loja bloqueada.", "code": "tenant_blocked"}))
    with pytest.raises(StoreBlocked, match="bloqueada"):
        cliente(op).poll("0.5.0", "PC", {})


def test_429_carrega_o_retry_after():
    op = _Opener(erro=http_error(429, headers={"Retry-After": "42"}))
    with pytest.raises(RateLimited) as exc:
        cliente(op).poll("0.5.0", "PC", {})
    assert exc.value.retry_after == 42.0


@pytest.mark.parametrize(
    "erro",
    [
        http_error(500, {"detail": "boom"}),
        http_error(502),
        http_error(302),  # redirect nao e seguido: vira erro de transporte
        urllib.error.URLError("getaddrinfo failed"),
        TimeoutError("timed out"),
        ConnectionResetError("reset"),
    ],
)
def test_falhas_temporarias_sao_transporterror(erro):
    with pytest.raises(TransportError):
        cliente(_Opener(erro=erro)).poll("0.5.0", "PC", {})


@pytest.mark.parametrize("corpo", [b"<html>502 Bad Gateway</html>", b"[1, 2]", b"\xff\xfe"])
def test_resposta_que_nao_e_objeto_json_e_transporterror(corpo):
    with pytest.raises(TransportError):
        cliente(_Opener(corpo)).poll("0.5.0", "PC", {})


def test_cliente_recusa_url_http_fora_do_localhost():
    with pytest.raises(PairingCodeError):
        RemoteClient("http://192.168.0.10/api", "loja", TOKEN, "inst")


def test_opener_padrao_nao_segue_redirect():
    """O urllib reenviaria o Authorization ao destino do redirect."""
    from agent.remote_client import _NoRedirect

    assert _NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://outro") is None


# ── _request: GET, prazo por chamada e erros de negócio (v0.6) ───────────
def test_request_get_com_query_sem_corpo_e_prazo_proprio():
    op = _Opener({"versao": "3:2026", "igual": True})
    resp = cliente(op)._request(
        "GET",
        "/agent/biometria/digitais/",
        query={"versao": "3:2026-11-24T10:00:00+00:00", "nada": None},
        timeout=7.5,
        negocio=True,
    )
    assert resp == {"versao": "3:2026", "igual": True}
    req = op.reqs[0]
    assert req.get_method() == "GET"
    assert req.full_url == (
        "https://api.eldensys.com.br/api/agent/biometria/digitais/"
        "?versao=3%3A2026-11-24T10%3A00%3A00%2B00%3A00"
    )
    assert req.data is None
    assert req.get_header("Content-type") is None
    assert req.get_header("Authorization") == f"Bearer {TOKEN}"
    assert req.get_header("X-tenant") == "loja_centro"
    assert req.get_header("X-agent-instance") == "inst-123"
    assert op.timeouts == [7.5]


def test_post_continua_usando_o_prazo_da_instancia():
    op = _Opener({})
    c = RemoteClient(
        "https://api.eldensys.com.br/api", "loja_centro", TOKEN, "inst-123", opener=op, timeout=3
    )
    c._post("/agent/poll/", {"a": 1})
    c._request("POST", "/agent/biometria/login/", {"b": 2}, negocio=True)
    assert op.timeouts == [3, 3]
    assert [r.get_method() for r in op.reqs] == ["POST", "POST"]
    assert json.loads(op.reqs[1].data) == {"b": 2}


def _negocio(erro):
    cliente(_Opener(erro=erro))._request("GET", "/agent/biometria/desafios/x/", negocio=True)


def test_negocio_401_continua_pareamento_recusado():
    with pytest.raises(PairingRejected) as exc:
        _negocio(http_error(401, {"detail": "Token revogado.", "code": "agent_token_invalid"}))
    assert exc.value.code == "agent_token_invalid"


def test_negocio_403_com_module_e_modulo_inativo():
    corpo = {"detail": "Módulo não contratado para este estabelecimento.", "module": "biometria"}
    with pytest.raises(ModuloInativo) as exc:
        _negocio(http_error(403, corpo))
    assert exc.value.module == "biometria"


@pytest.mark.parametrize("code", ["tenant_blocked", "tenant_inactive"])
def test_negocio_403_tenant_e_loja_bloqueada(code):
    with pytest.raises(StoreBlocked):
        _negocio(http_error(403, {"detail": "Esta empresa está desativada.", "code": code}))


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, "biometria_agente_nao_liberado"),
        (403, "biometria_sem_vinculo"),
        (404, "not_found"),
        (409, "biometria_desafio_usado"),
        (410, "biometria_desafio_expirado"),
        (422, "biometria_template_invalido"),
        (400, "biometria_dedo_invalido"),
    ],
)
def test_negocio_recusa_com_code_vira_servidor_recusou(status, code):
    with pytest.raises(ServidorRecusou) as exc:
        _negocio(http_error(status, {"detail": "Explicação em português.", "code": code}))
    assert (exc.value.status, exc.value.code, exc.value.detail) == (
        status,
        code,
        "Explicação em português.",
    )
    assert str(exc.value) == "Explicação em português."


def test_negocio_404_sem_code_e_servidor_sem_biometria():
    """404 de rota inexistente (EldenSys anterior à biometria) não é pareamento recusado."""
    erro = urllib.error.HTTPError(
        "https://x", 404, "Not Found", {}, io.BytesIO(b"<h1>Not Found</h1>")
    )
    with pytest.raises(ServidorRecusou) as exc:
        _negocio(erro)
    assert exc.value.code == CODE_SEM_BIOMETRIA
    assert "ainda não tem a biometria" in exc.value.detail


def test_negocio_429_e_5xx_seguem_como_antes():
    with pytest.raises(RateLimited) as exc:
        _negocio(http_error(429, headers={"Retry-After": "12"}))
    assert exc.value.retry_after == 12.0
    with pytest.raises(TransportError):
        _negocio(http_error(503, {"detail": "manutenção"}))
    with pytest.raises(TransportError):
        _negocio(urllib.error.URLError("getaddrinfo failed"))


@pytest.mark.parametrize("status", [404, 409, 410, 422])
def test_poll_e_ack_nao_mudaram_com_as_rotas_de_negocio(status):
    """Sem `negocio`, 404 segue pedindo novo pareamento e o resto é transporte."""
    erro = http_error(status, {"detail": "x", "code": "biometria_algo"})
    esperado = PairingRejected if status == 404 else TransportError
    with pytest.raises(esperado):
        cliente(_Opener(erro=erro)).poll("0.6.0", "PC", {})
    erro = http_error(403, {"detail": "x", "module": "biometria"})
    with pytest.raises(StoreBlocked):
        cliente(_Opener(erro=erro)).ack("1", "done")
