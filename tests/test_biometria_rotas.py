"""Rotas locais ``/biometria`` (contrato, seção 3): origem, CORS e fluxos.

O app de teste liga só a biometria (``rotas.instalar``) e uma rota de
impressão de mentira para provar que o CORS ``*`` dela não mudou. Leitor e
servidor são dublês (``SdkFalso`` e ``ServidorFalso``).
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import __version__  # noqa: E402
from agent.biometria import espelho as espelho_mod  # noqa: E402
from agent.biometria import rotas  # noqa: E402
from agent.biometria.sdk import Captura, Identificacao, device_id_de  # noqa: E402
from agent.biometria.servico import ServicoBiometria  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.remote_client import ServidorRecusou, TransportError  # noqa: E402
from agent.remote_jobs import RemoteStatus  # noqa: E402
from fake_idbio import BLOQUEAR, ServidorFalso, SdkFalso, tpl  # noqa: E402

PROD = "https://eldensys.hubdesktop.com.br"
SUB = "https://loja1.eldensys.hubdesktop.com.br"
TOKEN = "esa_" + "s" * 43
INSTANCIA = "instancia-secreta-123"
DESAFIO_SUP = "11111111-1111-4111-8111-111111111111"
DESAFIO_CLI = "22222222-2222-4222-8222-222222222222"
DESAFIO_CAD = "33333333-3333-4333-8333-333333333333"
DIGITAL_ID = "44444444-4444-4444-8444-444444444444"
DEVICE = device_id_de(DIGITAL_ID)
CHALLENGE = "Q" * 43
U1, OUTRA = 1001, 1002
SESSAO_FICTICIA = "abcdefghijklmnop"

ROTAS = [
    ("GET", "/biometria/status", None),
    ("POST", "/biometria/testar", None),
    ("POST", "/biometria/identificar", {"desafio_id": DESAFIO_SUP}),
    ("POST", "/biometria/login", {"challenge": CHALLENGE}),
    ("POST", "/biometria/cadastro/iniciar", {"desafio_id": DESAFIO_CAD}),
    ("POST", f"/biometria/cadastro/{SESSAO_FICTICIA}/verificar", None),
    ("POST", f"/biometria/cadastro/{SESSAO_FICTICIA}/captura", None),
    ("POST", f"/biometria/cadastro/{SESSAO_FICTICIA}/concluir", None),
    ("POST", f"/biometria/cadastro/{SESSAO_FICTICIA}/descartar", None),
    ("POST", "/biometria/cancelar", None),
    ("POST", "/biometria/sincronizar", None),
]
TEMPLATES_DO_TESTE = [tpl(n) for n in (1, 5, 7, 41, 99)]


class CorpoPdf(BaseModel):
    printer: str


class Mundo:
    def __init__(self, tmp_path: Path) -> None:
        self.fake = SdkFalso()
        self.servidor = ServidorFalso()
        self.cfg = AgentConfig(
            server_url="https://api.eldensys.com.br/api",
            server_tenant="loja_centro",
            server_token=TOKEN,
            instance_id=INSTANCIA,
            biometria_ocioso_s=0.05,
        )
        self.remoto = RemoteStatus(state="online", store_name="Loja Centro")
        self.servico = ServicoBiometria(
            load_config_fn=lambda: self.cfg,
            carregar_sdk_fn=lambda: self.fake,
            dll_presente_fn=lambda: True,
            cliente_fn=lambda cfg: self.servidor,
            status_remoto_fn=lambda: self.remoto,
            caminho_estado=tmp_path / "biometria.json",
        )
        app = FastAPI()

        @app.post("/print/pdf")
        def print_pdf(corpo: CorpoPdf) -> dict:
            return {"status": "ok"}

        rotas.instalar(
            app,
            self.servico,
            load_config_fn=lambda: self.cfg,
            cors={
                "allow_origins": ["*"],
                "allow_credentials": False,
                "allow_methods": ["GET", "POST", "OPTIONS"],
                "allow_headers": ["*"],
            },
        )
        self.app = app
        self.client = TestClient(app, base_url="http://localhost:17777")
        self.respostas: list[str] = []

    def chamar(self, metodo, caminho, corpo=None, *, origem=PROD, headers=None):
        h = {} if origem is None else {"Origin": origem}
        h.update(headers or {})
        r = self.client.request(metodo, caminho, json=corpo, headers=h)
        self.respostas.append(r.text)
        return r

    def post(self, caminho, corpo=None, **kw):
        return self.chamar("POST", caminho, corpo, **kw)

    def nada_tocado(self) -> bool:
        return self.fake.chamadas == [] and [
            c for c in self.servidor.chamadas if c[0] != "digitais"
        ] == []


@pytest.fixture
def m(tmp_path, monkeypatch):
    monkeypatch.setattr(espelho_mod, "PAUSA_ENTRE_LOTES_S", 0)
    mundo = Mundo(tmp_path)
    yield mundo
    # Nenhuma resposta, em teste nenhum, carrega um template.
    for texto in mundo.respostas:
        for t in TEMPLATES_DO_TESTE:
            assert t not in texto
    mundo.servico.parar()


def esperar(cond, timeout=3.0):
    fim = time.monotonic() + timeout
    while time.monotonic() < fim:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def resposta_poll(*, habilitado=True, versao="0:2026-11-24T10:00:00+00:00"):
    """Poll com o módulo ligado (a chave ``biometria`` só vem assim)."""
    return {
        "agent": {"id": "ag-1", "name": "Caixa 2"},
        "store_name": "Loja Centro",
        "biometria": {"habilitado": habilitado, "versao": versao},
    }


def desafio(did, finalidade, **extra):
    base = {
        "id": did,
        "finalidade": finalidade,
        "escopo": "",
        "status": "pendente",
        "expira_em": (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat(),
        "digital_id": None,
        "device_id": None,
        "alvo": None,
        "alvo_digitais": [],
    }
    base.update(extra)
    return base


# ── inventário ───────────────────────────────────────────────────────────
def test_todas_as_rotas_da_biometria_estao_cobertas(m):
    no_app = {
        (sorted(r.methods)[0], r.path)
        for r in m.app.routes
        if getattr(r, "path", "").startswith("/biometria")
    }
    esperado = {
        (metodo, caminho.replace(SESSAO_FICTICIA, "{sessao_id}")) for metodo, caminho, _ in ROTAS
    }
    assert no_app == esperado


# ── origem / Host / CORS ─────────────────────────────────────────────────
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
    assert r.json()["erro"] == "origem_recusada"
    assert "access-control-allow-origin" not in r.headers
    assert m.nada_tocado()


@pytest.mark.parametrize(("metodo", "caminho", "corpo"), ROTAS)
@pytest.mark.parametrize("host", ["evil.example:17777", "eldensys.hubdesktop.com.br", "192.168.0.10:17777"])
def test_host_fora_do_loopback_e_403(m, metodo, caminho, corpo, host):
    """DNS rebinding: o site aponta o próprio domínio para 127.0.0.1."""
    r = m.chamar(metodo, caminho, corpo, headers={"Host": host})
    assert r.status_code == 403
    assert m.nada_tocado()


@pytest.mark.parametrize(("metodo", "caminho", "corpo"), [r for r in ROTAS if r[0] == "POST"])
def test_post_sem_origin_e_403(m, metodo, caminho, corpo):
    r = m.chamar(metodo, caminho, corpo, origem=None)
    assert r.status_code == 403
    assert m.nada_tocado()


def test_get_status_sem_origin_e_ferramenta_local(m):
    r = m.chamar("GET", "/biometria/status", origem=None)
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


@pytest.mark.parametrize(("metodo", "caminho", "corpo"), ROTAS)
@pytest.mark.parametrize("origem", [PROD, SUB])
def test_producao_e_subdominio_passam_e_recebem_a_propria_origem(m, metodo, caminho, corpo, origem):
    r = m.chamar(metodo, caminho, corpo, origem=origem)
    assert r.status_code != 403
    assert r.headers["access-control-allow-origin"] == origem
    assert "Origin" in r.headers.get("vary", "")


@pytest.mark.parametrize("host", ["localhost:17777", "127.0.0.1:17777", "[::1]:17777", "LOCALHOST"])
def test_hosts_de_loopback_passam(m, host):
    assert m.chamar("GET", "/biometria/status", headers={"Host": host}).status_code == 200


def test_dev_e_extras_so_pelo_config(m):
    assert m.chamar("GET", "/biometria/status", origem="http://localhost:5173").status_code == 403
    m.cfg.biometria_permitir_dev = True
    m.cfg.biometria_origens_extras = ["https://erp.minhaloja.com.br"]
    for origem in ("http://localhost:5173", "http://127.0.0.1:5173", "https://erp.minhaloja.com.br"):
        assert m.chamar("GET", "/biometria/status", origem=origem).status_code == 200


@pytest.mark.parametrize(("metodo", "caminho", "corpo"), [r for r in ROTAS if r[0] == "POST"])
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
    assert r.headers["access-control-allow-headers"] == "content-type"
    assert m.nada_tocado()


def test_preflight_de_origem_estranha_e_recusado(m):
    r = m.chamar(
        "OPTIONS",
        "/biometria/identificar",
        origem="https://site-malicioso.example",
        headers={"Access-Control-Request-Method": "POST"},
    )
    assert r.status_code == 403
    assert "access-control-allow-origin" not in r.headers
    assert "access-control-allow-private-network" not in r.headers


def test_impressao_continua_com_cors_estrela(m):
    r = m.post("/print/pdf", {"printer": "X"}, origem="https://qualquer.example")
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
    r = m.chamar(
        "OPTIONS",
        "/print/pdf",
        origem="https://qualquer.example",
        headers={"Access-Control-Request-Method": "POST"},
    )
    assert r.status_code == 200
    assert r.headers["access-control-allow-origin"] == "*"
    # Erro de validação fora da biometria segue no formato do FastAPI.
    r = m.post("/print/pdf", {"sem": "printer"})
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], list)


def test_corpo_invalido_na_biometria_vira_requisicao_invalida(m):
    r = m.client.post(
        "/biometria/identificar",
        content=b"{ nao e json",
        headers={"Origin": PROD, "Content-Type": "application/json"},
    )
    assert r.status_code == 422
    assert r.json()["erro"] == "requisicao_invalida"


# ── status ───────────────────────────────────────────────────────────────
def test_status_sem_efeito_colateral_e_sem_segredos(m):
    m.servico.ao_poll({"agent": {"id": "ag-1", "name": "Caixa 2"}, "store_name": "Loja Centro"})
    r = m.chamar("GET", "/biometria/status")
    assert r.status_code == 200
    corpo = r.json()
    assert set(corpo) == {"versao_agente", "sdk", "leitor", "pareamento", "espelho"}
    assert corpo["versao_agente"] == __version__
    assert set(corpo["sdk"]) == {"presente", "carregado", "erro"}
    assert set(corpo["leitor"]) == {
        "conectado",
        "pro",
        "modelo",
        "serie",
        "firmware",
        "ocupado",
        "operacao",
        "digitais",
        "capacidade",
        "erro",
        "travado",
    }
    assert set(corpo["pareamento"]) == {
        "pareado",
        "estado",
        "agent_id",
        "loja",
        "tenant",
        "api",
        "biometria_habilitada",
    }
    assert set(corpo["espelho"]) == {
        "versao",
        "completo",
        "faltando",
        "sincronizado_em",
        "em_andamento",
        "progresso",
        "erro",
    }
    assert corpo["pareamento"]["tenant"] == "loja_centro"
    assert corpo["pareamento"]["agent_id"] == "ag-1"
    # Poll sem o bloco "biometria" (módulo desligado ou servidor antigo):
    # liberação desconhecida.
    assert corpo["pareamento"]["biometria_habilitada"] is None
    assert corpo["sdk"]["carregado"] is False  # nem carregou a DLL
    assert TOKEN not in r.text and INSTANCIA not in r.text
    assert m.fake.chamadas == [] and m.servidor.chamadas == []


def test_status_informa_se_o_pc_foi_liberado(m):
    """A web mostra "PC não liberado" antes de criar o desafio (contrato §7)."""
    base = {"agent": {"id": "ag-1", "name": "Caixa 2"}, "store_name": "Loja Centro"}
    m.servico.ao_poll({**base, "biometria": {"habilitado": True, "versao": "1:1"}})
    assert m.chamar("GET", "/biometria/status").json()["pareamento"]["biometria_habilitada"] is True
    m.servico.ao_poll({**base, "biometria": {"habilitado": False, "versao": None}})
    assert m.chamar("GET", "/biometria/status").json()["pareamento"]["biometria_habilitada"] is False


def test_status_nao_pareado(m):
    m.cfg.server_url = m.cfg.server_tenant = m.cfg.server_token = ""
    corpo = m.chamar("GET", "/biometria/status").json()
    assert corpo["pareamento"]["pareado"] is False
    assert corpo["pareamento"]["tenant"] is None


@pytest.mark.parametrize("liberado", [True, False], ids=["pc_liberado", "pc_nao_liberado"])
def test_primeiro_poll_sonda_o_leitor_uma_vez_e_devolve_na_hora(m, tmp_path, liberado):
    """Agente reiniciado com o espelho em dia (versão do poll = a do disco):
    ninguém mais tocaria no leitor, o status diria ``conectado: false`` e
    ``pro: null`` (a web esconde a biometria) e o heartbeat ``ok: false``."""
    versao = m.servidor.versao
    (tmp_path / "biometria.json").write_text(
        json.dumps(
            {
                "versao": versao,
                "tenant": m.cfg.server_tenant,
                "api": m.cfg.server_url,
                "serie": "SN-0001",
                "completo": True,
                "faltando": 0,
            }
        ),
        encoding="utf-8",
    )
    m.fake.memoria.update({U1: tpl(1), OUTRA: tpl(7)})
    leitor = m.chamar("GET", "/biometria/status").json()["leitor"]
    assert (leitor["conectado"], leitor["pro"]) == (False, None)

    m.servico.ao_poll(resposta_poll(habilitado=liberado, versao=versao))
    assert esperar(lambda: m.servico.capacidades()["ok"] is True)
    leitor = m.chamar("GET", "/biometria/status").json()["leitor"]
    assert (leitor["conectado"], leitor["pro"], leitor["digitais"]) == (True, True, 2)
    assert leitor["serie"] == "SN-0001"
    # Tarefa de fundo: o leitor volta na hora para o PDV do mesmo computador.
    assert esperar(lambda: not m.fake.iniciado)
    # Espelho em dia: nada foi baixado do servidor.
    assert m.servidor.chamadas == []
    # Uma vez por processo: os polls seguintes não abrem o leitor de novo.
    inits = m.fake.inits
    m.servico.ao_poll(resposta_poll(habilitado=liberado, versao=versao))
    time.sleep(0.05)
    assert m.fake.inits == inits


def test_agente_do_ultimo_poll_vale_ate_o_proximo(m, tmp_path):
    """Sem internet ao ligar não há poll: o status ainda informa este agente
    (a web precisa do ``agent_id`` para pedir a digital) — mas só para o
    mesmo pareamento."""
    m.servidor.vivas({U1: tpl(1)})
    m.servico.ao_poll(resposta_poll(versao=m.servidor.versao))
    assert esperar(
        lambda: m.servico.espelho.estado()["versao"] == m.servidor.versao
        and not m.servico.espelho.estado()["em_andamento"]
    )
    texto = (tmp_path / "biometria.json").read_text(encoding="utf-8")
    salvo = json.loads(texto)
    # A carga do espelho (que terminou depois do poll) não apagou o agente.
    assert (salvo["agent_id"], salvo["loja"], salvo["versao"]) == (
        "ag-1",
        "Loja Centro",
        m.servidor.versao,
    )
    assert TOKEN not in texto and INSTANCIA not in texto and tpl(1) not in texto

    depois = Mundo(tmp_path)  # agente reiniciado, ainda sem poll (sem internet)
    depois.remoto = RemoteStatus(state="error", message="Sem conexão com o EldenSys.")
    try:
        pareamento = depois.chamar("GET", "/biometria/status").json()["pareamento"]
        assert (pareamento["agent_id"], pareamento["loja"]) == ("ag-1", "Loja Centro")
        # Colaram outro código de pareamento: o agente salvo é de outro pareamento.
        depois.cfg.server_token = "esa_" + "t" * 43
        pareamento = depois.chamar("GET", "/biometria/status").json()["pareamento"]
        assert (pareamento["agent_id"], pareamento["loja"]) == (None, None)
        # O poll seguinte manda.
        depois.cfg.server_token = TOKEN
        depois.servico.ao_poll({**resposta_poll(), "agent": {"id": "ag-2", "name": "Caixa 2"}})
        pareamento = depois.chamar("GET", "/biometria/status").json()["pareamento"]
        assert pareamento["agent_id"] == "ag-2"
    finally:
        depois.servico.parar()


# ── conferências baratas (sem tocar no SDK) ──────────────────────────────
PEDE_LOJA = [r for r in ROTAS if r[1] not in ("/biometria/status", "/biometria/testar", "/biometria/cancelar")]
PEDE_LOJA = [r for r in PEDE_LOJA if not r[1].endswith("/descartar")]


@pytest.mark.parametrize(("metodo", "caminho", "corpo"), PEDE_LOJA)
def test_nao_pareado_e_409_sem_tocar_no_sdk(m, metodo, caminho, corpo):
    m.cfg.server_token = ""
    r = m.chamar(metodo, caminho, corpo)
    assert r.status_code == 409
    assert r.json()["erro"] == "nao_pareado"
    assert m.nada_tocado()


@pytest.mark.parametrize(
    "caminho", ["/biometria/identificar", "/biometria/login", "/biometria/cadastro/iniciar"]
)
def test_outra_loja_e_409_sem_tocar_no_sdk(m, caminho):
    corpo = {"desafio_id": DESAFIO_SUP, "challenge": CHALLENGE, "tenant": "loja_vizinha"}
    r = m.post(caminho, corpo)
    assert r.status_code == 409
    assert r.json()["erro"] == "outra_loja"
    assert m.nada_tocado()


def test_pareamento_recusado_e_modulo_inativo(m):
    m.remoto = RemoteStatus(state="rejected", message="Token revogado.")
    r = m.post("/biometria/identificar", {"desafio_id": DESAFIO_SUP})
    assert (r.status_code, r.json()["erro"]) == (409, "pareamento_recusado")
    m.remoto = RemoteStatus(state="online")
    m.servico.ao_poll({"store_name": "Loja"})  # sem a chave biometria = módulo desligado
    r = m.post("/biometria/identificar", {"desafio_id": DESAFIO_SUP})
    assert (r.status_code, r.json()["erro"]) == (403, "modulo_inativo")
    assert m.nada_tocado()


@pytest.mark.parametrize(("metodo", "caminho", "corpo"), [r for r in ROTAS if not r[1].endswith(("/descartar", "/cancelar"))])
@pytest.mark.parametrize("ajuste", [{"biometria_habilitada": False}, {"host": "0.0.0.0"}])
def test_biometria_desligada_neste_pc(m, metodo, caminho, corpo, ajuste):
    for k, v in ajuste.items():
        setattr(m.cfg, k, v)
    r = m.chamar(metodo, caminho, corpo)
    # Inclusive o status: a web distingue "desligada" de "leitor desconectado".
    assert (r.status_code, r.json()["erro"]) == (503, "biometria_desligada")
    assert m.fake.chamadas == []


# ── testar / cancelar / sincronizar ──────────────────────────────────────
def test_testar_le_o_leitor(m):
    m.fake.memoria.update({1: tpl(1), 2: tpl(7)})
    r = m.post("/biometria/testar")
    assert r.status_code == 200
    assert r.json() == {
        "ok": True,
        "modelo": "iDBio Pro",
        "serie": "SN-0001",
        "firmware": "1.2.0",
        "pro": True,
        "digitais": 2,
    }
    assert m.chamar("GET", "/biometria/status").json()["leitor"]["serie"] == "SN-0001"


def test_testar_leitor_comum_e_desconectado(m):
    m.fake.pro = False
    assert m.post("/biometria/testar").json()["pro"] is False
    m.fake.desconectar()
    r = m.post("/biometria/testar")
    assert (r.status_code, r.json()["erro"]) == (503, "leitor_desconectado")


def test_sdk_ausente_e_503(m):
    from agent.biometria.erros import SdkIndisponivel

    def sem_dll():
        raise SdkIndisponivel("A biblioteca do leitor não está nesta instalação.")

    m.servico.leitor._carregar_sdk = sem_dll
    r = m.post("/biometria/testar")
    assert (r.status_code, r.json()["erro"]) == (503, "sdk_ausente")


def test_erro_inesperado_e_500_sem_o_texto_da_excecao(m):
    def quebra():
        raise RuntimeError("detalhe interno que não pode vazar")

    m.fake.info = quebra
    r = m.post("/biometria/testar")
    assert r.status_code == 500
    assert r.json()["erro"] == "interno"
    assert "detalhe interno" not in r.text


def test_cancelar_sem_nada_e_sincronizar(m):
    assert m.post("/biometria/cancelar").json() == {"cancelado": False}
    m.servidor.vivas({U1: tpl(1)})
    r = m.post("/biometria/sincronizar")
    assert r.status_code == 202
    assert r.json()["em_andamento"] is True
    assert esperar(lambda: U1 in m.fake.memoria)


# ── identificar ──────────────────────────────────────────────────────────
def test_identificar_supervisor_informa_o_servidor_e_devolve_o_veredito(m):
    m.fake.memoria[U1] = tpl(1)
    m.fake.dedo = tpl(1)
    m.servidor.desafios[DESAFIO_SUP] = desafio(DESAFIO_SUP, "supervisor")
    m.servidor.veredito = {"status": "aprovado", "motivo": "", "nome": "Gerente Ana"}
    r = m.post("/biometria/identificar", {"desafio_id": DESAFIO_SUP, "tenant": "loja_centro"})
    assert r.status_code == 200
    assert r.json() == {"status": "aprovado", "motivo": "", "nome": "Gerente Ana"}
    enviado = m.servidor.resultados[0]
    assert (enviado["device_id"], enviado["erro"]) == (U1, None)
    # Série lida antes da captura (sem dedo): vai para a auditoria do servidor.
    assert enviado["leitor"] == {"modelo": "iDBio Pro", "serie": "SN-0001", "firmware": "1.2.0"}


def test_identificar_dedo_desconhecido_vai_como_nao_identificado(m):
    m.fake.dedo = tpl(99)
    m.servidor.desafios[DESAFIO_SUP] = desafio(DESAFIO_SUP, "supervisor")
    r = m.post("/biometria/identificar", {"desafio_id": DESAFIO_SUP})
    assert r.json()["status"] == "recusado"
    assert m.servidor.resultados[0]["device_id"] is None
    assert m.servidor.resultados[0]["erro"] == "nao_identificado"


@pytest.mark.parametrize(("codigo", "status"), [(-6, "tempo_esgotado"), (-21, "sem_dedo"), (-20, "cancelado")])
def test_identificar_sem_dedo_nao_fala_com_o_servidor(m, codigo, status):
    m.fake.identificacoes.append(codigo)
    m.servidor.desafios[DESAFIO_SUP] = desafio(DESAFIO_SUP, "supervisor")
    r = m.post("/biometria/identificar", {"desafio_id": DESAFIO_SUP})
    assert r.status_code == 200
    assert r.json()["status"] == status
    assert m.servidor.resultados == []


def test_identificar_cliente_garante_as_digitais_dele_antes(m):
    m.fake.dedo = tpl(41)
    alvo = [{"device_id": 41, "template": tpl(41)}, {"device_id": 42, "template": "invalido"}]
    m.servidor.desafios[DESAFIO_CLI] = desafio(DESAFIO_CLI, "cliente", alvo_digitais=alvo)
    r = m.post("/biometria/identificar", {"desafio_id": DESAFIO_CLI})
    assert r.json()["status"] == "aprovado"
    assert m.fake.memoria == {41: tpl(41)}
    assert m.servidor.resultados[0]["device_id"] == 41


@pytest.mark.parametrize("dedo", [tpl(1), tpl(99)], ids=["reconhecido", "desconhecido"])
def test_identificar_anota_leitor_conectado_e_pro_mesmo_sem_sondagem(m, dedo):
    """Sem internet ao ligar não há poll nem sondagem: a primeira
    identificação, achando alguém ou não, já prova o iDBio Pro conectado."""
    m.fake.memoria[U1] = tpl(1)
    m.fake.dedo = dedo
    m.servidor.desafios[DESAFIO_SUP] = desafio(DESAFIO_SUP, "supervisor")
    # Na leitura anterior o cabo saiu (-2): desconectado, com o erro anotado.
    m.fake.identificacoes.append(-2)
    r = m.post("/biometria/identificar", {"desafio_id": DESAFIO_SUP})
    assert (r.status_code, r.json()["erro"]) == (503, "leitor_desconectado")
    leitor = m.chamar("GET", "/biometria/status").json()["leitor"]
    assert leitor["conectado"] is False and "cabo USB" in leitor["erro"]
    # Cabo de volta: a identificação respondeu (achando ou não).
    assert m.post("/biometria/identificar", {"desafio_id": DESAFIO_SUP}).status_code == 200
    leitor = m.chamar("GET", "/biometria/status").json()["leitor"]
    assert (leitor["conectado"], leitor["pro"], leitor["erro"]) == (True, True, None)
    assert m.servico.capacidades()["ok"] is True


@pytest.mark.parametrize("estranho", [0, -5, 2**60, 2**62])
def test_id_de_outro_sistema_vale_como_nao_identificado(m, estranho, caplog):
    """Leitor que veio de outro sistema: o id que ele devolve não é de digital
    da loja (0 < id < 2**60) e o servidor responderia 400 "Id da digital
    inválido". Vale como "não reconhecida" e fica no log."""
    m.servidor.desafios[DESAFIO_SUP] = desafio(DESAFIO_SUP, "supervisor")
    m.fake.identificacoes.append(Identificacao(estranho, 9000, 80))
    with caplog.at_level(logging.WARNING):
        r = m.post("/biometria/identificar", {"desafio_id": DESAFIO_SUP})
    assert r.json()["status"] == "recusado"
    enviado = m.servidor.resultados[0]
    assert (enviado["device_id"], enviado["erro"]) == (None, "nao_identificado")
    assert "outro sistema" in caplog.text
    # Login pela digital: a mesma regra (422 nao_identificado, sem chamar o servidor).
    m.fake.identificacoes.append(Identificacao(estranho, 9000, 80))
    r = m.post("/biometria/login", {"challenge": CHALLENGE})
    assert (r.status_code, r.json()["erro"]) == (422, "nao_identificado")
    assert m.servidor.logins == []


def test_desafio_ja_usado_nao_abre_o_leitor(m):
    m.servidor.desafios[DESAFIO_SUP] = desafio(
        DESAFIO_SUP, "supervisor", status="consumido", motivo="Já usado."
    )
    r = m.post("/biometria/identificar", {"desafio_id": DESAFIO_SUP})
    assert r.json() == {"status": "consumido", "motivo": "Já usado."}
    assert m.fake.chamadas == []


def test_identificar_pedido_de_cadastro_e_passo_invalido(m):
    m.servidor.desafios[DESAFIO_CAD] = desafio(DESAFIO_CAD, "cadastro")
    r = m.post("/biometria/identificar", {"desafio_id": DESAFIO_CAD})
    assert (r.status_code, r.json()["erro"]) == (409, "passo_invalido")


@pytest.mark.parametrize("ruim", ["", "123", "../../x", "11111111-1111-4111-8111-11111111111Z"])
def test_identificar_id_invalido(m, ruim):
    r = m.post("/biometria/identificar", {"desafio_id": ruim})
    assert (r.status_code, r.json()["erro"]) == (422, "requisicao_invalida")
    assert m.nada_tocado()


def test_identificar_recusa_e_falta_de_rede_do_servidor(m):
    m.servidor.falhas["desafio"] = ServidorRecusou(410, "biometria_desafio_expirado", "Expirou.")
    r = m.post("/biometria/identificar", {"desafio_id": DESAFIO_SUP})
    assert r.status_code == 409
    assert r.json()["erro"] == "servidor_recusou"
    assert r.json()["codigo"] == "biometria_desafio_expirado"
    assert r.json()["mensagem"] == "Expirou."
    m.servidor.falhas["desafio"] = TransportError("sem rede")
    r = m.post("/biometria/identificar", {"desafio_id": DESAFIO_SUP})
    assert (r.status_code, r.json()["erro"]) == (503, "sem_conexao")


def test_supervisor_na_primeira_carga_do_espelho_pede_para_aguardar(m):
    m.servidor.desafios[DESAFIO_SUP] = desafio(DESAFIO_SUP, "supervisor")
    m.servico.espelho._em_andamento = True
    m.servico.espelho._progresso = 40
    r = m.post("/biometria/identificar", {"desafio_id": DESAFIO_SUP})
    assert r.status_code == 503
    assert (r.json()["erro"], r.json()["progresso"]) == ("sincronizando", 40)
    m.servico.espelho._em_andamento = False


def test_segunda_leitura_e_ocupado_e_cancelar_solta_a_primeira(m):
    m.fake.identificacoes.append(BLOQUEAR)
    m.servidor.desafios[DESAFIO_SUP] = desafio(DESAFIO_SUP, "supervisor")
    primeira: dict = {}

    def pedir():
        primeira["r"] = m.post("/biometria/identificar", {"desafio_id": DESAFIO_SUP})

    t = threading.Thread(target=pedir)
    t.start()
    assert m.fake.bloqueado.wait(3)
    r = m.post("/biometria/login", {"challenge": CHALLENGE})
    assert r.status_code == 409
    assert r.json()["erro"] == "ocupado"
    assert r.json()["pode_cancelar"] is True
    assert m.post("/biometria/cancelar").json() == {"cancelado": True}
    t.join(5)
    assert primeira["r"].json()["status"] == "cancelado"
    assert m.servidor.resultados == []


def test_navegador_que_sai_cancela_a_captura(m):
    m.fake.identificacoes.append(BLOQUEAR)

    class RequestQueSai:
        async def is_disconnected(self):
            return m.fake.bloqueado.is_set()

    def ler(tok):
        return m.servico.leitor.executar(
            lambda sdk, ctx: ctx.capturar(sdk.identificar),
            usuario=True,
            nome="identificar",
            captura=True,
            cancelamento=tok,
        )

    resposta = asyncio.run(rotas.com_captura(RequestQueSai(), ler))
    assert m.fake.cancelamentos >= 1
    assert resposta.status_code == 422  # leitura cancelada; ninguém mais ouve
    assert esperar(lambda: not m.servico.leitor.ocupado_por_usuario())


# ── login ────────────────────────────────────────────────────────────────
def test_login_pela_digital_devolve_so_o_ticket(m):
    m.fake.memoria[U1] = tpl(1)
    m.fake.dedo = tpl(1)
    m.servidor.resposta_login = {
        "ticket": "tkt",
        "expira_em": "2026-11-24T10:01:00Z",
        "nome": "Ana",
        "email": "ana@loja.com",
        "tenant": "loja_centro",
        "loja": "Loja Centro",
        "template": tpl(1),
    }
    r = m.post("/biometria/login", {"challenge": CHALLENGE, "tenant": "loja_centro"})
    assert r.status_code == 200
    assert set(r.json()) == {"ticket", "expira_em", "nome", "email", "tenant", "loja"}
    assert m.servidor.logins[0]["device_id"] == U1
    assert m.servidor.logins[0]["challenge"] == CHALLENGE


@pytest.mark.parametrize("ruim", ["", "curto", "Q" * 42 + "=", "Q" * 43 + "\n", "Q" * 44])
def test_login_challenge_invalido_nem_abre_o_leitor(m, ruim):
    r = m.post("/biometria/login", {"challenge": ruim})
    assert (r.status_code, r.json()["erro"]) == (422, "requisicao_invalida")
    assert m.fake.chamadas == []


def test_login_dedo_desconhecido_e_sem_vinculo(m):
    m.fake.dedo = tpl(99)
    r = m.post("/biometria/login", {"challenge": CHALLENGE})
    assert (r.status_code, r.json()["erro"]) == (422, "nao_identificado")
    assert m.servidor.logins == []
    m.fake.memoria[U1] = tpl(1)
    m.fake.dedo = tpl(1)
    m.servidor.falhas["login"] = ServidorRecusou(
        403, "biometria_sem_vinculo", "Entre com a senha uma vez neste computador."
    )
    r = m.post("/biometria/login", {"challenge": CHALLENGE})
    assert r.status_code == 409
    assert r.json()["codigo"] == "biometria_sem_vinculo"


# ── cadastro ─────────────────────────────────────────────────────────────
def preparar_cadastro(m, **extra):
    m.servidor.desafios[DESAFIO_CAD] = desafio(
        DESAFIO_CAD,
        "cadastro",
        digital_id=DIGITAL_ID,
        device_id=DEVICE,
        alvo={"tipo": "usuario", "id": "u-1", "nome": "Ana"},
        **extra,
    )
    m.servidor.resposta_digital = {
        "status": "aprovado",
        "digital_id": DIGITAL_ID,
        "device_id": DEVICE,
        "nome": "Ana",
    }
    r = m.post("/biometria/cadastro/iniciar", {"desafio_id": DESAFIO_CAD, "tenant": "loja_centro"})
    assert r.status_code == 200, r.text
    corpo = r.json()
    assert corpo["passos"] == 4
    assert 100 <= corpo["expira_em_s"] <= 120
    return corpo["sessao_id"]


def verificar_livre(m, sessao):
    m.fake.dedo = tpl(5)
    m.servidor.veredito = {"status": "pendente", "motivo": "", "verificado": True}
    r = m.post(f"/biometria/cadastro/{sessao}/verificar")
    assert r.json() == {"status": "livre"}


def tres_capturas(m, sessao):
    for passo in (1, 2, 3):
        r = m.post(f"/biometria/cadastro/{sessao}/captura")
        assert r.json()["aceita"] is True
        assert r.json()["passo"] == passo


def test_cadastro_completo_grava_no_leitor_antes_do_servidor(m):
    sessao = preparar_cadastro(m)
    # Leitura antes de conferir o dedo: fora de ordem.
    r = m.post(f"/biometria/cadastro/{sessao}/captura")
    assert (r.status_code, r.json()["erro"]) == (409, "passo_invalido")
    verificar_livre(m, sessao)
    assert m.servidor.resultados[-1]["device_id"] is None

    # Leitura fraca: não conta, mostra a prévia.
    m.fake.capturas.append(Captura(tpl(5), bytes(range(256)) * 75, 120, 160, 30))
    r = m.post(f"/biometria/cadastro/{sessao}/captura")
    corpo = r.json()
    assert (corpo["aceita"], corpo["passo"], corpo["de"], corpo["qualidade"]) == (False, 0, 3, 30)
    from PIL import Image

    img = Image.open(io.BytesIO(base64.b64decode(corpo["previa_png"])))
    assert img.format == "PNG" and max(img.size) <= 96

    tres_capturas(m, sessao)
    r = m.post(f"/biometria/cadastro/{sessao}/captura")
    assert (r.status_code, r.json()["erro"]) == (409, "passo_invalido")

    no_leitor_ao_enviar = []
    original = m.servidor.enviar_digital

    def enviar(*a, **kw):
        no_leitor_ao_enviar.append(DEVICE in m.fake.memoria)
        return original(*a, **kw)

    m.servidor.enviar_digital = enviar
    r = m.post(f"/biometria/cadastro/{sessao}/concluir")
    assert r.status_code == 200
    assert r.json() == {
        "status": "aprovado",
        "digital_id": DIGITAL_ID,
        "nome": "Ana",
        "gravada_no_leitor": True,
    }
    assert no_leitor_ao_enviar == [True]
    assert m.fake.memoria[DEVICE] == tpl(5)
    assert m.servidor.enviadas[0]["template"] == tpl(5)
    # Repetir o concluir (resposta perdida) não reenvia nada.
    assert m.post(f"/biometria/cadastro/{sessao}/concluir").json()["status"] == "aprovado"
    assert len(m.servidor.enviadas) == 1


def test_cadastro_sem_previa_quando_desligada(m):
    m.cfg.biometria_previa = False
    sessao = preparar_cadastro(m)
    verificar_livre(m, sessao)
    r = m.post(f"/biometria/cadastro/{sessao}/captura")
    assert r.json()["previa_png"] is None


def test_cadastro_dedo_de_outra_pessoa_e_duplicada(m):
    sessao = preparar_cadastro(m)
    m.fake.memoria[OUTRA] = tpl(5)
    m.fake.dedo = tpl(5)
    m.servidor.veredito = {
        "status": "recusado",
        "motivo": "Esta digital já está cadastrada para Bia",
        "nome": "Bia",
    }
    r = m.post(f"/biometria/cadastro/{sessao}/verificar")
    assert r.json()["status"] == "duplicada"
    assert r.json()["nome"] == "Bia"
    assert m.servidor.resultados[-1]["device_id"] == OUTRA
    r = m.post(f"/biometria/cadastro/{sessao}/captura")
    assert r.status_code == 409


def test_cadastro_servidor_recusou_apaga_do_leitor(m):
    sessao = preparar_cadastro(m)
    verificar_livre(m, sessao)
    tres_capturas(m, sessao)
    m.servidor.falhas["digital"] = ServidorRecusou(409, "biometria_limite_dedos", "Limite de dedos.")
    r = m.post(f"/biometria/cadastro/{sessao}/concluir")
    assert r.status_code == 409
    assert r.json()["codigo"] == "biometria_limite_dedos"
    assert esperar(lambda: DEVICE not in m.fake.memoria)
    r = m.post(f"/biometria/cadastro/{sessao}/concluir")
    assert (r.status_code, r.json()["erro"]) == (404, "sessao_inexistente")


def test_cadastro_sem_rede_repete_o_envio_sem_novas_leituras(m):
    sessao = preparar_cadastro(m)
    verificar_livre(m, sessao)
    tres_capturas(m, sessao)
    m.servidor.falhas["digital"] = TransportError("caiu")
    r = m.post(f"/biometria/cadastro/{sessao}/concluir")
    assert r.status_code == 503
    assert (r.json()["erro"], r.json()["pode_reenviar"]) == ("sem_conexao", True)
    assert DEVICE in m.fake.memoria
    m.servidor.falhas.clear()
    capturas_antes = m.fake.chamadas.count("CaptureImageAndTemplate")
    r = m.post(f"/biometria/cadastro/{sessao}/concluir")
    assert r.json()["status"] == "aprovado"
    assert m.fake.chamadas.count("CaptureImageAndTemplate") == capturas_antes
    assert m.fake.chamadas.count("SaveTemplate") == 1
    assert len(m.servidor.enviadas) == 2


def test_descartar_depois_de_gravar_sem_confirmacao_tira_do_leitor(m):
    sessao = preparar_cadastro(m)
    verificar_livre(m, sessao)
    tres_capturas(m, sessao)
    m.servidor.falhas["digital"] = TransportError("caiu")
    m.post(f"/biometria/cadastro/{sessao}/concluir")
    assert DEVICE in m.fake.memoria
    assert m.post(f"/biometria/cadastro/{sessao}/descartar").json() == {"ok": True}
    assert esperar(lambda: DEVICE not in m.fake.memoria)
    r = m.post(f"/biometria/cadastro/{sessao}/captura")
    assert (r.status_code, r.json()["erro"]) == (404, "sessao_inexistente")


def test_leituras_de_dedos_diferentes_recomecam(m):
    sessao = preparar_cadastro(m)
    verificar_livre(m, sessao)
    tres_capturas(m, sessao)
    m.fake.mesclas.append(-10)
    r = m.post(f"/biometria/cadastro/{sessao}/concluir")
    assert (r.status_code, r.json()["erro"]) == (422, "merge")
    assert "Recomece" in r.json()["mensagem"]
    tres_capturas(m, sessao)  # volta ao passo 1, sem conferir o dedo de novo
    assert m.post(f"/biometria/cadastro/{sessao}/concluir").json()["status"] == "aprovado"


def test_recadastro_do_mesmo_dedo_tira_a_digital_antiga(m):
    """-9 no SaveTemplate com a digital antiga da MESMA pessoa (verificada)."""
    sessao = preparar_cadastro(m)
    m.fake.memoria[OUTRA] = tpl(5)
    m.fake.dedo = tpl(5)
    m.fake.rejeitar_duplicado = True
    m.servidor.veredito = {"status": "pendente", "verificado": True}
    assert m.post(f"/biometria/cadastro/{sessao}/verificar").json() == {"status": "livre"}
    tres_capturas(m, sessao)
    assert m.post(f"/biometria/cadastro/{sessao}/concluir").json()["gravada_no_leitor"] is True
    assert OUTRA not in m.fake.memoria and DEVICE in m.fake.memoria


def test_cadastro_com_memoria_cheia_vai_so_para_o_servidor(m):
    sessao = preparar_cadastro(m)
    verificar_livre(m, sessao)
    tres_capturas(m, sessao)
    m.fake.capacidade = 0
    r = m.post(f"/biometria/cadastro/{sessao}/concluir")
    assert r.json()["status"] == "aprovado"
    assert r.json()["gravada_no_leitor"] is False


def test_leitura_que_falhou_pede_outra(m):
    sessao = preparar_cadastro(m)
    verificar_livre(m, sessao)
    m.fake.capturas.append(-6)
    r = m.post(f"/biometria/cadastro/{sessao}/captura")
    assert r.status_code == 200
    assert (r.json()["aceita"], r.json()["passo"]) == (False, 0)
    assert r.json()["mensagem"] == "Nenhum dedo colocado a tempo"


def test_iniciar_recusa_desafio_sem_o_id_certo(m):
    m.servidor.desafios[DESAFIO_CAD] = desafio(
        DESAFIO_CAD, "cadastro", digital_id=DIGITAL_ID, device_id=DEVICE + 1
    )
    r = m.post("/biometria/cadastro/iniciar", {"desafio_id": DESAFIO_CAD})
    assert r.status_code == 409
    assert r.json()["codigo"] == "biometria_desafio_invalido"
    m.servidor.desafios[DESAFIO_CAD] = desafio(DESAFIO_CAD, "supervisor")
    r = m.post("/biometria/cadastro/iniciar", {"desafio_id": DESAFIO_CAD})
    assert (r.status_code, r.json()["erro"]) == (409, "passo_invalido")


@pytest.mark.parametrize("passo", ["verificar", "captura", "concluir"])
def test_sessao_inexistente_e_404(m, passo):
    r = m.post(f"/biometria/cadastro/{SESSAO_FICTICIA}/{passo}")
    assert (r.status_code, r.json()["erro"]) == (404, "sessao_inexistente")
    r = m.post(f"/biometria/cadastro/x/{passo}")
    assert r.status_code == 404


def relogio_do_cadastro(m) -> list[float]:
    """Relógio monotônico de mentira para as sessões de cadastro."""
    agora = [5000.0]
    m.servico.cadastros._relogio = lambda: agora[0]
    return agora


@pytest.mark.parametrize(
    "pc_adiantado",
    [timedelta(minutes=8), timedelta(hours=-1)],
    ids=["relogio_do_pc_adiantado", "relogio_do_pc_atrasado"],
)
def test_prazo_do_cadastro_nao_depende_do_relogio_do_pc(m, pc_adiantado):
    """O ``expira_em`` absoluto do servidor contra o relógio de parede do PC
    dava 30 s (PC adiantado) ou 600 s (atrasado). O prazo local é 120 s pelo
    relógio monotônico; quem recusa de fato é o servidor."""
    agora = relogio_do_cadastro(m)
    expira_no_pc = datetime.now(timezone.utc) + timedelta(seconds=120) - pc_adiantado
    sessao = preparar_cadastro(m, expira_em=expira_no_pc.isoformat())
    agora[0] += 119
    verificar_livre(m, sessao)


def test_sem_atividade_a_sessao_de_cadastro_vence(m):
    agora = relogio_do_cadastro(m)
    sessao = preparar_cadastro(m)
    agora[0] += 121
    r = m.post(f"/biometria/cadastro/{sessao}/verificar")
    assert (r.status_code, r.json()["erro"]) == (404, "sessao_inexistente")
    sessao = preparar_cadastro(m)
    verificar_livre(m, sessao)
    agora[0] += 301
    r = m.post(f"/biometria/cadastro/{sessao}/captura")
    assert (r.status_code, r.json()["erro"]) == (404, "sessao_inexistente")


def test_verificacao_livre_e_leituras_aceitas_renovam_o_prazo(m):
    """O servidor estende o desafio para +5 min quando a verificação dá
    "livre"; o agente renova a sessão para 300 s ali e a cada leitura aceita."""
    agora = relogio_do_cadastro(m)
    sessao = preparar_cadastro(m)
    agora[0] += 110
    verificar_livre(m, sessao)
    for passo in (1, 2, 3):
        agora[0] += 290  # já passou dos 120 s do iniciar; dentro dos 300 s da renovação
        r = m.post(f"/biometria/cadastro/{sessao}/captura")
        assert r.status_code == 200, r.text
        assert (r.json()["aceita"], r.json()["passo"]) == (True, passo)
    agora[0] += 290
    assert m.post(f"/biometria/cadastro/{sessao}/concluir").json()["status"] == "aprovado"


def test_verificacao_do_cadastro_garante_as_digitais_da_pessoa_antes(m):
    """Espelho incompleto (memória cheia): a digital que a Ana já tem não está
    no leitor. Sem gravá-la antes, o mesmo dedo passaria como "livre" e seria
    cadastrado de novo como outro dedo dela."""
    antiga = device_id_de("55555555-5555-4555-8555-555555555555")
    estranha = 1003
    sessao = preparar_cadastro(m, alvo_digitais=[{"device_id": antiga, "template": tpl(5)}])
    m.fake.capacidade = 1
    m.fake.memoria[estranha] = tpl(8)  # id que a loja não conhece ocupando a memória
    m.fake.dedo = tpl(5)
    m.servidor.veredito = lambda device_id: (
        {"status": "recusado", "motivo": "Este dedo já está cadastrado para Ana", "nome": "Ana"}
        if device_id == antiga
        else {"status": "pendente", "motivo": "", "verificado": True}
    )
    r = m.post(f"/biometria/cadastro/{sessao}/verificar")
    assert r.json()["status"] == "duplicada"
    assert m.servidor.resultados[-1]["device_id"] == antiga
    assert m.fake.memoria == {antiga: tpl(5)}


def test_verificacao_do_cadastro_com_id_de_outro_sistema_vale_como_nao_identificado(m):
    sessao = preparar_cadastro(m)
    m.fake.identificacoes.append(Identificacao(2**61, 9000, 80))
    m.servidor.veredito = {"status": "pendente", "motivo": "", "verificado": True}
    assert m.post(f"/biometria/cadastro/{sessao}/verificar").json() == {"status": "livre"}
    enviado = m.servidor.resultados[-1]
    assert (enviado["device_id"], enviado["erro"]) == (None, "nao_identificado")
