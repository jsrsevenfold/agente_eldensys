"""Sondagem do leitor pelo poll: o status não pode ficar "sem leitor" para sempre.

Fase 0 (24/11/2026, leitor real): o ``Init`` devolve -2 tanto com o cabo solto
quanto com o leitor aberto por OUTRO programa (o PDV esperando o dedo no mesmo
PC), e o cabo religado só volta a abrir ~18 s depois. O status e o heartbeat
leem fatos; quem torna a olhar o leitor é o poll — só enquanto o último fato
for "sem leitor" e no máximo a cada ``RESONDAR_SEM_LEITOR_S``.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.biometria.servico import RESONDAR_SEM_LEITOR_S, ServicoBiometria  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from fake_idbio import SdkFalso  # noqa: E402

#: Poll com o módulo ligado e este PC ainda não liberado: só a sondagem toca
#: no leitor (o espelho fica parado).
POLL = {
    "agent": {"id": "0b8f8d7e-1d2c-4b5a-9e8f-7a6b5c4d3e2f"},
    "store_name": "Loja Centro",
    "biometria": {"habilitado": False, "versao": "v1"},
}


def esperar(cond, timeout=3.0):
    fim = time.monotonic() + timeout
    while time.monotonic() < fim:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


class Relogio:
    def __init__(self) -> None:
        self.agora = 1000.0

    def __call__(self) -> float:
        return self.agora


@pytest.fixture
def fake():
    return SdkFalso()


@pytest.fixture
def relogio():
    return Relogio()


@pytest.fixture
def servico(tmp_path, fake, relogio):
    cfg = AgentConfig(
        server_url="https://api.eldensys.com.br/api",
        server_tenant="loja_centro",
        server_token="esa_" + "a" * 40,
        instance_id="instancia-teste",
        biometria_ocioso_s=0.05,
    )
    s = ServicoBiometria(
        load_config_fn=lambda: cfg,
        carregar_sdk_fn=lambda: fake,
        dll_presente_fn=lambda: True,
        cliente_fn=lambda c: None,
        status_remoto_fn=lambda: None,
        caminho_estado=tmp_path / "biometria.json",
        relogio_fn=relogio,
    )
    yield s
    s.parar()


def tentativas_de_abrir(fake) -> int:
    return fake.chamadas.count("Init")


def quieto(servico) -> None:
    """Espera a fila do leitor esvaziar e o leitor fechar (a sondagem é tarefa
    de fundo: o leitor é devolvido logo depois)."""
    time.sleep(0.05)
    esperar(lambda: servico.leitor.estado()["operacao"] is None and not servico.leitor.aberto)
    time.sleep(0.05)


def test_sem_leitor_o_poll_torna_a_olhar_e_o_status_volta_sozinho(servico, fake, relogio):
    fake.desconectar()
    servico.ao_poll(POLL)
    assert esperar(lambda: servico.leitor.fatos().conectado is False)
    assert "outro programa" in servico.status()["leitor"]["erro"]
    antes = tentativas_de_abrir(fake)

    # Cabo religado (ou o PDV soltou o leitor). Poll antes do intervalo: não olha.
    fake.conectado = True
    relogio.agora += RESONDAR_SEM_LEITOR_S - 1
    servico.ao_poll(POLL)
    quieto(servico)
    assert tentativas_de_abrir(fake) == antes
    assert servico.status()["leitor"]["conectado"] is False

    # Passado o intervalo, o poll olha de novo e o status mostra o leitor.
    relogio.agora += 2
    servico.ao_poll(POLL)
    assert esperar(lambda: servico.leitor.fatos().conectado is True)
    leitor = servico.status()["leitor"]
    assert leitor["conectado"] is True
    assert leitor["modelo"] == "iDBio Pro"


def test_leitor_conectado_nao_e_sondado_de_novo(servico, fake, relogio):
    servico.ao_poll(POLL)
    assert esperar(lambda: servico.leitor.fatos().conectado is True)
    quieto(servico)
    n = len(fake.chamadas)
    for _ in range(3):
        relogio.agora += RESONDAR_SEM_LEITOR_S * 4
        servico.ao_poll(POLL)
    quieto(servico)
    assert len(fake.chamadas) == n


def test_status_nao_toca_no_leitor_mesmo_sem_leitor(servico, fake, relogio):
    fake.desconectar()
    servico.ao_poll(POLL)
    assert esperar(lambda: servico.leitor.fatos().conectado is False)
    quieto(servico)
    n = len(fake.chamadas)
    relogio.agora += RESONDAR_SEM_LEITOR_S * 4
    for _ in range(3):
        servico.status()
    quieto(servico)
    assert len(fake.chamadas) == n


def test_modulo_desligado_nao_sonda(servico, fake, relogio):
    sem_modulo = {k: v for k, v in POLL.items() if k != "biometria"}
    servico.ao_poll(sem_modulo)
    relogio.agora += RESONDAR_SEM_LEITOR_S * 4
    servico.ao_poll(sem_modulo)
    quieto(servico)
    assert fake.chamadas == []
