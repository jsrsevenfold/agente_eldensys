"""Rotas locais ``/balanca`` (contrato do lote de 24/11/2026, seção 3).

O app de teste liga a biometria (que instala o middleware de origem e o
tratador de corpo inválido), as rotas da balança com um ``ServicoBalanca``
de verdade sobre a balança falsa, e uma rota de impressão de mentira para
provar que o CORS ``*`` dela não mudou.
"""

from __future__ import annotations

import asyncio
import sys
import time
from copy import deepcopy
from pathlib import Path

import pytest
import serial
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import __version__  # noqa: E402
from agent.balanca import leitor as leitor_mod  # noqa: E402
from agent.balanca import rotas  # noqa: E402
from agent.balanca.servico import ServicoBalanca  # noqa: E402
from agent.biometria import rotas as rotas_biometria  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from fake_balanca import (  # noqa: E402
    ENQ,
    ERRO_EM_USO,
    ERRO_INEXISTENTE,
    AbridorFalso,
    TransporteFalso,
    balanca_lixo,
    balanca_muda,
    balanca_toledo,
    stx_etx,
)

PROD = "https://eldensys.hubdesktop.com.br"
SUB = "https://loja1.eldensys.hubdesktop.com.br"

ROTAS = [
    ("GET", "/balanca/status", None),
    ("GET", "/balanca/portas", None),
    ("POST", "/balanca/config", {"habilitada": True}),
    ("POST", "/balanca/leitura", {}),
    ("POST", "/balanca/detectar", {}),
    ("POST", "/balanca/liberar", {}),
]
POSTS = [r for r in ROTAS if r[0] == "POST"]


class CorpoPdf(BaseModel):
    printer: str


class Mundo:
    def __init__(self) -> None:
        self.cfg = AgentConfig(
            balanca_habilitada=True,
            balanca_porta="COM3",
            balanca_protocolo="toledo",
            balanca_timeout_ms=500,
        )
        self.salvos: list[AgentConfig] = []
        self.abridor = balanca_toledo()
        self.portas = [{"porta": "COM3", "descricao": "USB-SERIAL CH340 (COM3)"}]
        self.servico = ServicoBalanca(
            load_config_fn=lambda: self.cfg,
            save_config_fn=lambda cfg: self.salvos.append(deepcopy(cfg)),
            abrir_porta_fn=lambda porta, baud: self.abridor(porta, baud),
            listar_portas_fn=lambda: list(self.portas),
        )
        app = FastAPI()

        @app.post("/print/pdf")
        def print_pdf(corpo: CorpoPdf) -> dict:
            return {"status": "ok"}

        rotas_biometria.instalar(
            app,
            object(),
            load_config_fn=lambda: self.cfg,
            cors={
                "allow_origins": ["*"],
                "allow_credentials": False,
                "allow_methods": ["GET", "POST", "OPTIONS"],
                "allow_headers": ["*"],
            },
        )
        rotas.instalar(app, self.servico)
        self.app = app
        self.client = TestClient(app, base_url="http://localhost:17777")

    def chamar(self, metodo, caminho, corpo=None, *, origem=PROD, headers=None):
        h = {} if origem is None else {"Origin": origem}
        h.update(headers or {})
        return self.client.request(metodo, caminho, json=corpo, headers=h)

    def post(self, caminho, corpo=None, **kw):
        return self.chamar("POST", caminho, corpo, **kw)

    def nada_tocado(self) -> bool:
        return self.abridor.aberturas == [] and self.salvos == []


@pytest.fixture
def m():
    mundo = Mundo()
    yield mundo
    mundo.servico.parar()


@pytest.fixture
def rapido(monkeypatch):
    monkeypatch.setattr(leitor_mod, "CURTO_URANO_S", 0.05)
    monkeypatch.setattr(leitor_mod, "PRAZO_DETECTAR_S", 0.15)


# ── inventário ───────────────────────────────────────────────────────────
def test_todas_as_rotas_da_balanca_estao_cobertas(m):
    no_app = {
        (sorted(r.methods)[0], r.path)
        for r in m.app.routes
        if getattr(r, "path", "").startswith("/balanca")
    }
    assert no_app == {(metodo, caminho) for metodo, caminho, _ in ROTAS}


# ── origem / Host / CORS (mesma política da biometria) ───────────────────
@pytest.mark.parametrize(("metodo", "caminho", "corpo"), ROTAS)
@pytest.mark.parametrize(
    "origem",
    [
        "https://site-malicioso.example",
        "null",
        "https://eldensys.hubdesktop.com.br.evil.com",
        "http://eldensys.hubdesktop.com.br",
        "https://evil-eldensys.hubdesktop.com.br",
        "http://localhost:5173",
    ],
)
def test_origem_fora_da_politica_e_403_sem_tocar_em_nada(m, metodo, caminho, corpo, origem):
    r = m.chamar(metodo, caminho, corpo, origem=origem)
    assert r.status_code == 403
    assert r.json() == {
        "erro": "origem_recusada",
        "mensagem": "Este site não pode usar a balança deste computador.",
        "detail": "Este site não pode usar a balança deste computador.",
    }
    assert "access-control-allow-origin" not in r.headers
    assert m.nada_tocado()


@pytest.mark.parametrize(("metodo", "caminho", "corpo"), ROTAS)
@pytest.mark.parametrize("host", ["evil.example:17777", "eldensys.hubdesktop.com.br", "192.168.0.10:17777"])
def test_host_fora_do_loopback_e_403(m, metodo, caminho, corpo, host):
    """DNS rebinding: o site aponta o próprio domínio para 127.0.0.1."""
    r = m.chamar(metodo, caminho, corpo, headers={"Host": host})
    assert r.status_code == 403
    assert m.nada_tocado()


@pytest.mark.parametrize(("metodo", "caminho", "corpo"), POSTS)
def test_post_sem_origin_e_403(m, metodo, caminho, corpo):
    r = m.chamar(metodo, caminho, corpo, origem=None)
    assert r.status_code == 403
    assert m.nada_tocado()


@pytest.mark.parametrize("caminho", ["/balanca/status", "/balanca/portas"])
def test_get_sem_origin_e_ferramenta_local(m, caminho):
    r = m.chamar("GET", caminho, origem=None)
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


@pytest.mark.parametrize(("metodo", "caminho", "corpo"), ROTAS)
@pytest.mark.parametrize("origem", [PROD, SUB])
def test_producao_e_subdominio_passam_e_recebem_a_propria_origem(
    m, rapido, metodo, caminho, corpo, origem
):
    r = m.chamar(metodo, caminho, corpo, origem=origem)
    assert r.status_code != 403
    assert r.headers["access-control-allow-origin"] == origem
    assert "Origin" in r.headers.get("vary", "")


@pytest.mark.parametrize(("metodo", "caminho", "corpo"), POSTS)
def test_preflight_ecoa_a_origem_e_libera_rede_privada(m, metodo, caminho, corpo):
    r = m.chamar(
        "OPTIONS",
        caminho,
        headers={
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
            "Access-Control-Request-Private-Network": "true",
        },
    )
    assert r.status_code == 204
    assert r.headers["access-control-allow-origin"] == PROD
    assert r.headers["access-control-allow-private-network"] == "true"
    assert "POST" in r.headers["access-control-allow-methods"]
    assert m.nada_tocado()


def test_preflight_de_origem_estranha_e_recusado(m):
    r = m.chamar(
        "OPTIONS",
        "/balanca/leitura",
        origem="https://site-malicioso.example",
        headers={"Access-Control-Request-Method": "POST"},
    )
    assert r.status_code == 403
    assert "access-control-allow-private-network" not in r.headers


def test_impressao_continua_com_cors_estrela(m):
    r = m.post("/print/pdf", {"printer": "X"}, origem="https://qualquer.example")
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
    r = m.post("/print/pdf", {"sem": "printer"})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


def test_dev_e_extras_valem_como_na_biometria(m):
    assert m.chamar("GET", "/balanca/status", origem="http://localhost:5173").status_code == 403
    m.cfg.biometria_permitir_dev = True
    assert m.chamar("GET", "/balanca/status", origem="http://localhost:5173").status_code == 200


def test_corpo_invalido_vira_requisicao_invalida_da_balanca(m):
    r = m.client.post(
        "/balanca/leitura",
        content=b"{ nao e json",
        headers={"Origin": PROD, "Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json() == {
        "erro": "requisicao_invalida",
        "mensagem": "Pedido inválido para a balança.",
        "detail": "Pedido inválido para a balança.",
    }
    r = m.post("/balanca/config", {"baud": "9600"})  # texto onde vai número
    assert (r.status_code, r.json()["erro"]) == (422, "requisicao_invalida")
    assert m.nada_tocado()


# ── status / portas ──────────────────────────────────────────────────────
def test_status_nao_abre_a_porta(m):
    r = m.chamar("GET", "/balanca/status")
    assert r.status_code == 200
    assert r.json() == {
        "versao_agente": __version__,
        "habilitada": True,
        "config": {
            "habilitada": True,
            "porta": "COM3",
            "baud": 9600,
            "protocolo": "toledo",
            "timeout_ms": 500,
        },
        "porta": {"aberta": False, "erro": None},
        "ultima_leitura": None,
        "protocolos": [
            {"id": "auto", "rotulo": "Auto-detectar"},
            {"id": "urano", "rotulo": "Urano UDC / POP (Urano 12)"},
            {"id": "toledo", "rotulo": "Toledo Prix"},
            {"id": "filizola", "rotulo": "Filizola"},
            {"id": "elgin", "rotulo": "Elgin DP"},
            {"id": "generico", "rotulo": "Genérico ASCII"},
        ],
        "bauds": [2400, 4800, 9600, 19200, 38400],
    }
    assert m.nada_tocado()
    assert m.servico.leitor._thread is None


def test_status_desligada_nao_e_erro(m):
    m.cfg.balanca_habilitada = False
    corpo = m.chamar("GET", "/balanca/status").json()
    assert corpo["habilitada"] is False and corpo["config"]["habilitada"] is False


def test_status_com_agente_fora_do_loopback_nao_esta_habilitada(m):
    m.cfg.host = "0.0.0.0"
    corpo = m.chamar("GET", "/balanca/status").json()
    assert corpo["habilitada"] is False and corpo["config"]["habilitada"] is True


def test_status_mostra_a_ultima_leitura_e_a_porta(m):
    m.post("/balanca/leitura", {})
    corpo = m.chamar("GET", "/balanca/status").json()
    assert corpo["porta"] == {"aberta": True, "erro": None}
    assert corpo["ultima_leitura"] == {
        "estado": "peso",
        "kg": 1.234,
        "protocolo": "toledo",
        "lido_como": "toledo",
    }


def test_status_saneia_o_config_editado_a_mao(m):
    m.cfg.balanca_habilitada = "true"  # texto não liga
    m.cfg.balanca_porta = "com7"
    m.cfg.balanca_baud = 1234
    m.cfg.balanca_protocolo = "Toledo"
    m.cfg.balanca_timeout_ms = 50
    corpo = m.chamar("GET", "/balanca/status").json()
    assert corpo["config"] == {
        "habilitada": False,
        "porta": "COM7",
        "baud": 9600,
        "protocolo": "toledo",
        "timeout_ms": 1500,
    }


def test_portas(m):
    r = m.chamar("GET", "/balanca/portas")
    assert r.json() == {"portas": [{"porta": "COM3", "descricao": "USB-SERIAL CH340 (COM3)"}]}

    def quebrada():
        raise OSError("SetupAPI")

    m.servico._listar_portas = quebrada
    assert m.chamar("GET", "/balanca/portas").json() == {"portas": []}


# ── config ───────────────────────────────────────────────────────────────
def test_config_grava_so_o_que_veio(m):
    r = m.post("/balanca/config", {"porta": "COM12", "baud": 4800, "protocolo": "urano"})
    assert r.status_code == 200
    assert r.json() == {
        "config": {
            "habilitada": True,
            "porta": "COM12",
            "baud": 4800,
            "protocolo": "urano",
            "timeout_ms": 500,
        }
    }
    assert len(m.salvos) == 1
    salvo = m.salvos[0]
    assert (salvo.balanca_porta, salvo.balanca_baud, salvo.balanca_protocolo) == (
        "COM12",
        4800,
        "urano",
    )
    r = m.post("/balanca/config", {"habilitada": False, "porta": "", "timeout_ms": 10000})
    assert r.json()["config"]["habilitada"] is False
    assert (m.salvos[-1].balanca_porta, m.salvos[-1].balanca_timeout_ms) == ("", 10000)


def test_config_nao_mexe_no_pareamento(m):
    m.cfg.server_token = "esa_segredo"
    m.post("/balanca/config", {"protocolo": "elgin"})
    assert m.salvos[-1].server_token == "esa_segredo"


@pytest.mark.parametrize(
    "corpo",
    [
        {"porta": "COM"},
        {"porta": "COM1234"},
        {"porta": "/dev/ttyS0"},
        {"porta": "loop://"},
        {"porta": "com3"},
        {"baud": 1200},
        {"baud": 115200},
        {"protocolo": "prix"},
        {"protocolo": "AUTO"},
        {"timeout_ms": 199},
        {"timeout_ms": 10001},
        {"habilitada": "sim"},
        {"baud": 9600.0},
        {"baud": True},
    ],
)
def test_config_invalido_e_422_sem_gravar(m, corpo):
    r = m.post("/balanca/config", corpo)
    assert r.status_code == 422
    assert r.json()["erro"] == "requisicao_invalida"
    assert m.salvos == []


def test_config_que_troca_a_porta_solta_a_antiga(m):
    m.post("/balanca/leitura", {})
    assert m.servico.leitor.aberta
    m.post("/balanca/config", {"porta": "COM4"})
    assert not m.servico.leitor.aberta
    assert m.abridor.ultimo.fechado


def test_config_com_agente_fora_do_loopback_e_recusada(m):
    m.cfg.host = "0.0.0.0"
    r = m.post("/balanca/config", {"porta": "COM4"})
    assert (r.status_code, r.json()["erro"]) == (409, "balanca_desligada")
    assert "127.0.0.1" in r.json()["mensagem"]
    assert m.salvos == []


# ── leitura ──────────────────────────────────────────────────────────────
def test_leitura_de_peso(m):
    r = m.post("/balanca/leitura", {"timeout_ms": 500})
    assert r.status_code == 200
    assert r.json() == {
        "estado": "peso",
        "kg": 1.234,
        "protocolo": "toledo",
        "lido_como": "toledo",
        "bytes": 7,
    }


def test_leitura_sem_corpo_usa_o_config(m):
    r = m.client.post("/balanca/leitura", headers={"Origin": PROD})
    assert (r.status_code, r.json()["estado"]) == (200, "peso")


def test_leitura_com_diagnostico_traz_o_frame(m):
    r = m.post("/balanca/leitura", {"diagnostico": True})
    assert r.json()["frame"] == {"texto": "<STX>01234<ETX>", "hex": "02 30 31 32 33 34 03"}


@pytest.mark.parametrize(
    ("corpo", "estado", "kg"),
    [
        ("00000", "zero", 0.0),
        ("-00.100", "negativo", -0.1),
        ("NNNNN", "negativo", None),
        ("IIIII", "instavel", None),
        ("SSSSS", "sobrecarga", None),
    ],
)
def test_leitura_devolve_o_frame_cru(m, corpo, estado, kg):
    m.abridor = balanca_toledo(frame=stx_etx(corpo))
    r = m.post("/balanca/leitura", {})
    assert (r.json()["estado"], r.json()["kg"]) == (estado, kg)


def test_leitura_sem_resposta_e_nao_reconhecido(m):
    m.abridor = balanca_muda()
    r = m.post("/balanca/leitura", {"timeout_ms": 100})
    assert r.status_code == 200
    assert r.json() == {
        "estado": "sem_resposta",
        "kg": None,
        "protocolo": "toledo",
        "lido_como": None,
        "bytes": 0,
    }
    m.post("/balanca/liberar", {})
    m.abridor = balanca_lixo()
    r = m.post("/balanca/leitura", {"timeout_ms": 300, "diagnostico": True})
    assert r.json() == {
        "estado": "nao_reconhecido",
        "kg": None,
        "protocolo": "toledo",
        "lido_como": None,
        "bytes": 8,
        "frame": {"texto": "..#@! <CR><LF>", "hex": "FF FE 23 40 21 20 0D 0A"},
    }


def test_leitura_desligada_e_409(m):
    m.cfg.balanca_habilitada = False
    r = m.post("/balanca/leitura", {})
    assert (r.status_code, r.json()["erro"]) == (409, "balanca_desligada")
    assert m.nada_tocado()


def test_leitura_sem_porta_e_409(m):
    m.cfg.balanca_porta = ""
    r = m.post("/balanca/leitura", {})
    assert (r.status_code, r.json()["erro"]) == (409, "sem_porta")
    assert m.nada_tocado()


def test_leitura_com_agente_fora_do_loopback_e_409(m):
    m.cfg.host = "0.0.0.0"
    r = m.post("/balanca/leitura", {"teste": {"porta": "COM3", "baud": 9600, "protocolo": "toledo"}})
    assert (r.status_code, r.json()["erro"]) == (409, "balanca_desligada")
    assert m.nada_tocado()


@pytest.mark.parametrize(
    ("texto", "erro"), [(ERRO_EM_USO, "porta_em_uso"), (ERRO_INEXISTENTE, "porta_inexistente")]
)
def test_leitura_com_porta_ocupada_ou_inexistente_e_503(m, texto, erro):
    m.abridor.erros.append(serial.SerialException(texto))
    r = m.post("/balanca/leitura", {})
    assert r.status_code == 503
    corpo = r.json()
    assert corpo["erro"] == erro and "COM3" in corpo["mensagem"]
    assert m.chamar("GET", "/balanca/status").json()["porta"]["erro"] == corpo["mensagem"]


@pytest.mark.parametrize("timeout_ms", [99, 2001, 0, -5])
def test_leitura_timeout_fora_da_faixa_e_422(m, timeout_ms):
    r = m.post("/balanca/leitura", {"timeout_ms": timeout_ms})
    assert (r.status_code, r.json()["erro"]) == (422, "requisicao_invalida")
    assert m.nada_tocado()


def test_teste_usa_os_valores_sem_salvar_e_dispensa_a_balanca_ligada(m):
    m.cfg.balanca_habilitada = False
    m.cfg.balanca_porta = ""
    r = m.post(
        "/balanca/leitura",
        {"timeout_ms": 500, "teste": {"porta": "COM5", "baud": 19200, "protocolo": "toledo"}},
    )
    assert (r.status_code, r.json()["kg"]) == (200, 1.234)
    assert m.abridor.aberturas == [("COM5", 19200)]
    assert m.salvos == []


@pytest.mark.parametrize(
    "teste",
    [
        {"porta": "COM5", "baud": 19200},
        {"porta": "COM5", "baud": 1, "protocolo": "toledo"},
        {"porta": "X", "baud": 9600, "protocolo": "toledo"},
        {"porta": "COM5", "baud": 9600, "protocolo": "nada"},
    ],
)
def test_teste_invalido_e_422(m, teste):
    r = m.post("/balanca/leitura", {"teste": teste})
    assert (r.status_code, r.json()["erro"]) == (422, "requisicao_invalida")
    assert m.nada_tocado()


def test_teste_sem_porta_e_409(m):
    r = m.post("/balanca/leitura", {"teste": {"porta": "", "baud": 9600, "protocolo": "toledo"}})
    assert (r.status_code, r.json()["erro"]) == (409, "sem_porta")


def test_erro_inesperado_e_500_sem_o_texto_da_excecao(m):
    def explode(*a, **kw):
        raise RuntimeError("segredo interno")

    m.servico.ler = explode
    r = m.post("/balanca/leitura", {})
    assert r.status_code == 500
    assert r.json()["erro"] == "interno"
    assert "segredo" not in r.text


# ── detectar / liberar ───────────────────────────────────────────────────
def test_detectar_devolve_o_protocolo_e_o_frame(m, rapido):
    r = m.post("/balanca/detectar", {})
    assert r.status_code == 200
    assert r.json() == {
        "protocolo": "toledo",
        "estado": "peso",
        "kg": 1.234,
        "bytes": 7,
        "frame": {"texto": "<STX>01234<ETX>", "hex": "02 30 31 32 33 34 03"},
    }
    assert m.salvos == []  # quem salva é a tela (POST /balanca/config)


def test_detectar_em_outra_porta_dispensa_a_balanca_ligada(m, rapido):
    m.cfg.balanca_habilitada = False
    r = m.post("/balanca/detectar", {"porta": "COM8", "baud": 4800})
    assert r.json()["protocolo"] == "toledo"
    assert m.abridor.aberturas == [("COM8", 4800)]


def test_detectar_sem_nada(m, rapido):
    m.abridor = balanca_muda()
    r = m.post("/balanca/detectar", {})
    assert r.json() == {
        "protocolo": None,
        "estado": "sem_resposta",
        "kg": None,
        "bytes": 0,
        "frame": {"texto": "", "hex": ""},
    }
    m.cfg.balanca_porta = ""
    r = m.post("/balanca/detectar", {})
    assert (r.status_code, r.json()["erro"]) == (409, "sem_porta")
    r = m.post("/balanca/detectar", {"porta": "COMX"})
    assert (r.status_code, r.json()["erro"]) == (422, "requisicao_invalida")


def test_navegador_que_sai_cancela_o_detectar(m, monkeypatch):
    monkeypatch.setattr(leitor_mod, "CURTO_URANO_S", 0.05)
    monkeypatch.setattr(leitor_mod, "PRAZO_DETECTAR_S", 0.3)
    m.abridor = balanca_muda()

    class RequestQueSai:
        async def is_disconnected(self):
            return True

    inicio = time.monotonic()
    resposta = asyncio.run(
        rotas.com_cancelamento(RequestQueSai(), lambda tok: m.servico.detectar(cancelamento=tok))
    )
    gasto = time.monotonic() - inicio
    assert resposta.status_code == 409  # cancelado; ninguém mais ouve
    # Inteira seriam 5 protocolos × 0,3 s; parou na tentativa seguinte.
    assert gasto < 1.2
    assert len(m.abridor.ultimo.escritos) < 3


def test_navegador_que_fica_recebe_o_resultado(m, rapido):
    class RequestQueFica:
        async def is_disconnected(self):
            return False

    corpo = asyncio.run(
        rotas.com_cancelamento(RequestQueFica(), lambda tok: m.servico.detectar(cancelamento=tok))
    )
    assert corpo["protocolo"] == "toledo"


def test_liberar_fecha_a_porta(m):
    m.post("/balanca/leitura", {})
    assert m.servico.leitor.aberta
    r = m.post("/balanca/liberar", {})
    assert (r.status_code, r.json()) == (200, {"liberada": True})
    assert not m.servico.leitor.aberta
    assert m.post("/balanca/liberar", {}).json() == {"liberada": True}


def test_fila_cheia_e_409_ocupada(m):
    leitor = m.servico.leitor
    import threading

    solta = threading.Event()
    tarefas = [leitor._enviar(lambda: solta.wait(5), "presa", 10.0)]
    tarefas += [leitor._enviar(lambda: None, "fila", 10.0) for _ in range(leitor_mod.FILA_MAX - 1)]
    r = m.post("/balanca/leitura", {})
    assert (r.status_code, r.json()["erro"]) == (409, "ocupada")
    solta.set()
    for t in tarefas:
        t.future.result(5)


def test_agente_encerrando_e_503(m):
    m.servico.parar()
    r = m.post("/balanca/leitura", {})
    assert (r.status_code, r.json()["erro"]) == (503, "agente_encerrando")


# ── heartbeat ────────────────────────────────────────────────────────────
def test_capacidades_do_heartbeat(m):
    assert m.servico.capacidades() == {
        "configured": True,
        "ok": True,
        "porta": "COM3",
        "protocolo": "toledo",
    }
    m.abridor = AbridorFalso(lambda p, b: TransporteFalso(p, b, respostas={ENQ: b""}))
    m.post("/balanca/leitura", {"timeout_ms": 100})  # muda: não reconheceu
    assert m.servico.capacidades()["ok"] is False
    m.cfg.balanca_habilitada = False
    assert m.servico.capacidades() == {
        "configured": False,
        "ok": False,
        "porta": "COM3",
        "protocolo": "toledo",
    }
    assert m.abridor.aberturas  # só a leitura acima abriu a porta
