"""Política de origem: as funções puras por trás do ``MiddlewareOrigem``."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.biometria.origem import (  # noqa: E402
    agente_em_loopback,
    eh_rota_balanca,
    eh_rota_biometria,
    eh_rota_protegida,
    host_loopback,
    origem_permitida,
    origem_permitida_cfg,
    origens_extras,
)
from agent.config import AgentConfig  # noqa: E402


@pytest.mark.parametrize(
    "origem",
    [
        "https://eldensys.hubdesktop.com.br",
        "https://loja1.eldensys.hubdesktop.com.br",
        "https://a.b-c.eldensys.hubdesktop.com.br",
    ],
)
def test_producao_e_subdominios(origem):
    assert origem_permitida(origem)


@pytest.mark.parametrize(
    "origem",
    [
        None,
        "",
        "null",
        "http://eldensys.hubdesktop.com.br",
        "https://eldensys.hubdesktop.com.br.evil.com",
        "https://evil-eldensys.hubdesktop.com.br",
        "https://eldensys.hubdesktop.com.br:8443",
        "https://eldensys.hubdesktop.com.br/",
        "https://eldensys.hubdesktop.com.br\n",
        "https://hubdesktop.com.br",
        "https://outro.hubdesktop.com.br",
        "https://eldensys.com.br",
        "http://localhost:5173",
    ],
)
def test_fora_da_politica(origem):
    assert not origem_permitida(origem)


def test_dev_so_com_a_chave():
    assert not origem_permitida("http://localhost:5173")
    assert origem_permitida("http://localhost:5173", permitir_dev=True)
    assert origem_permitida("http://127.0.0.1:5173", permitir_dev=True)
    assert not origem_permitida("http://localhost:3000", permitir_dev=True)


def test_extras_saneados():
    assert origens_extras("https://x.example") == frozenset()
    assert origens_extras(None) == frozenset()
    assert origens_extras(["https://ERP.Loja.com/", "*", "null", "ftp://x", 3, "https://*.x"]) == {
        "https://erp.loja.com"
    }
    cfg = AgentConfig(biometria_origens_extras=["https://erp.loja.com"])
    assert origem_permitida_cfg("https://erp.loja.com", cfg)
    assert not origem_permitida_cfg("https://erp.loja.com.evil", cfg)


@pytest.mark.parametrize(
    ("host", "ok"),
    [
        ("localhost:17777", True),
        ("127.0.0.1:17777", True),
        ("127.0.0.1", True),
        ("[::1]:17777", True),
        ("LocalHost:17777", True),
        ("evil.example:17777", False),
        ("localhost.evil.example", False),
        ("127.0.0.1.nip.io:17777", False),
        ("192.168.0.10:17777", False),
        ("::1", False),
        ("[::1]x", False),
        ("localhost:abc", False),
        ("", False),
        (None, False),
    ],
)
def test_host_loopback(host, ok):
    assert host_loopback(host) is ok


@pytest.mark.parametrize(
    ("host", "ok"), [("127.0.0.1", True), ("localhost", True), ("0.0.0.0", False), ("192.168.0.5", False)]
)
def test_agente_em_loopback(host, ok):
    assert agente_em_loopback(AgentConfig(host=host)) is ok


def test_rota_biometria():
    assert eh_rota_biometria("/biometria")
    assert eh_rota_biometria("/biometria/status")
    assert not eh_rota_biometria("/biometriax")
    assert not eh_rota_biometria("/print/pdf")


def test_rota_balanca_tambem_e_protegida():
    assert eh_rota_balanca("/balanca")
    assert eh_rota_balanca("/balanca/leitura")
    assert not eh_rota_balanca("/balancas")
    assert not eh_rota_balanca("/print/pdf")
    for path in ("/biometria/status", "/balanca/status", "/balanca"):
        assert eh_rota_protegida(path)
    for path in ("/print/pdf", "/health", "/config", "/balancax", "/scale"):
        assert not eh_rota_protegida(path)
