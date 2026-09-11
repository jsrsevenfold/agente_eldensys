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
