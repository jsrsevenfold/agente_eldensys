"""Contrato HTTP do agente — o que o EldenSys depende que nao mude."""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import __version__, server  # noqa: E402
from agent.config import AgentConfig  # noqa: E402


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "ensure_label_appdata", lambda: tmp_path)
    return TestClient(server.create_app(AgentConfig()))


@pytest.fixture
def jobs(monkeypatch):
    """Intercepta print_pdf: guarda os kwargs em vez de imprimir."""
    vistos: list[dict] = []
    monkeypatch.setattr(server, "print_pdf", lambda **kw: vistos.append(kw))
    return vistos


PDF = base64.b64encode(b"%PDF-1.4").decode()


def test_health_reporta_versao(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["version"] == __version__


def test_print_pdf_default_sem_render_mode(client, jobs):
    r = client.post("/print/pdf", json={"printer": "X", "pdf_base64": PDF})
    assert r.status_code == 200
    assert jobs[0]["render_mode"] == "default"


def test_print_pdf_repassa_render_mode_label(client, jobs):
    r = client.post(
        "/print/pdf",
        json={"printer": "Elgin L42", "pdf_base64": PDF, "render_mode": "label"},
    )
    assert r.status_code == 200
    assert jobs[0]["render_mode"] == "label"


def test_campo_desconhecido_e_ignorado(client, jobs):
    """A garantia que dispensa gate de versao no frontend.

    O EldenSys manda campos novos para TODA a base instalada; um agente que
    respondesse 422 faria a loja antiga parar de imprimir no dia do deploy.
    """
    r = client.post(
        "/print/pdf",
        json={"printer": "X", "pdf_base64": PDF, "campo_do_futuro": 42},
    )
    assert r.status_code == 200


def test_render_mode_invalido_e_rejeitado(client, jobs):
    r = client.post(
        "/print/pdf",
        json={"printer": "X", "pdf_base64": PDF, "render_mode": "raster"},
    )
    assert r.status_code == 422
    assert not jobs


def test_config_expoe_e_alterna_o_antialias(client, monkeypatch, tmp_path):
    salvos: list = []
    monkeypatch.setattr(server, "save_config", lambda c: salvos.append(c))
    monkeypatch.setattr(
        server, "load_config", lambda: AgentConfig(sumatra_label_disable_antialias=True)
    )
    assert client.get("/config").json()["sumatra_label_disable_antialias"] is True
    r = client.post("/config", json={"sumatra_label_disable_antialias": False})
    assert r.status_code == 200
    # Nao exige restart: print_pdf le a config a cada job.
    assert r.json()["restart_required"] is False
    assert salvos[-1].sumatra_label_disable_antialias is False


PAREADO = {
    "server_url": "https://api.eldensys.com.br/api",
    "server_tenant": "loja_centro",
    "server_token": "esa_segredo_do_pareamento",
    "instance_id": "inst-123",
    "scale_dir": "C:\\Toledo\\Carga",
    "remote_label_printer": "Argox OS-214",
}


def test_config_nao_vaza_o_pareamento(client, monkeypatch):
    """O loopback aceita qualquer origem (CORS *): o que GET /config devolve,
    qualquer site aberto no navegador da loja consegue ler."""
    monkeypatch.setattr(server, "load_config", lambda: AgentConfig(**PAREADO))
    r = client.get("/config")
    assert r.status_code == 200
    assert not (set(PAREADO) & set(r.json()))
    assert "esa_segredo_do_pareamento" not in r.text
    assert "inst-123" not in r.text


def test_post_config_preserva_o_pareamento_e_nao_o_altera(client, monkeypatch):
    salvos: list = []
    monkeypatch.setattr(server, "save_config", lambda c: salvos.append(c))
    monkeypatch.setattr(server, "load_config", lambda: AgentConfig(**PAREADO))
    r = client.post(
        "/config",
        json={
            "log_level": "DEBUG",
            # Um site malicioso tentando sequestrar o canal ou a pasta:
            "server_url": "https://atacante.example/api",
            "server_token": "trocado",
            "scale_dir": "C:\\Windows\\System32",
            "remote_label_printer": "Outra",
        },
    )
    assert r.status_code == 200
    gravado = salvos[-1]
    assert gravado.log_level == "DEBUG"
    for campo, valor in PAREADO.items():
        assert getattr(gravado, campo) == valor


def test_nao_existe_rota_local_que_grave_arquivo_ou_receba_trabalho(client):
    """O canal remoto e so de SAIDA: nenhuma rota nova no loopback sem senha."""
    rotas = {r.path for r in client.app.routes}
    assert not any(p.startswith(("/scale", "/files", "/agent", "/jobs", "/remote")) for p in rotas)
