"""config.json: migracao entre versoes e o segredo do pareamento.

A partir da v0.5 o arquivo guarda o pareamento com a loja. Perder ou
corromper o arquivo numa atualizacao obrigaria a loja a parear de novo.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import config  # noqa: E402
from agent.config import AgentConfig, is_paired, load_config, save_config  # noqa: E402

CAMPOS_REMOTOS = {
    "scale_dir",
    "remote_label_printer",
    "server_url",
    "server_tenant",
    "server_token",
    "instance_id",
}


@pytest.fixture
def cfg_path(monkeypatch, tmp_path):
    """Aponta o config.json para uma pasta temporaria (nunca o %APPDATA% real)."""
    path = tmp_path / "config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    return path


def test_primeira_execucao_cria_config_com_identidade_e_sem_pareamento(cfg_path):
    cfg = load_config()
    assert cfg_path.exists()
    assert len(cfg.instance_id) == 32
    assert not is_paired(cfg)
    assert (cfg.scale_dir, cfg.remote_label_printer) == ("", "")
    # A identidade e estavel: carregar de novo nao gera outra.
    assert load_config().instance_id == cfg.instance_id


def test_config_da_v04_ganha_os_campos_novos_sem_perder_os_antigos(cfg_path):
    cfg_path.write_text(
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": 18888,
                "allowed_origins": ["*"],
                "allowed_origin_regex": "",
                "log_level": "DEBUG",
                "sumatra_path": "C:\\Sumatra\\SumatraPDF.exe",
                "sumatra_label_disable_antialias": False,
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config()
    assert (cfg.port, cfg.log_level) == (18888, "DEBUG")
    assert cfg.sumatra_path == "C:\\Sumatra\\SumatraPDF.exe"
    assert cfg.sumatra_label_disable_antialias is False

    gravado = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert CAMPOS_REMOTOS <= gravado.keys()
    assert gravado["instance_id"] == cfg.instance_id != ""


CAMPOS_BIOMETRIA = {
    "biometria_habilitada": True,
    "biometria_permitir_dev": False,
    "biometria_previa": True,
    "biometria_origens_extras": [],
    "biometria_ocioso_s": 20.0,
}


def test_config_da_v05_ganha_os_campos_da_biometria_sem_perder_o_pareamento(cfg_path):
    v05 = {
        "host": "127.0.0.1",
        "port": 17777,
        "allowed_origins": ["*"],
        "allowed_origin_regex": "",
        "log_level": "INFO",
        "sumatra_path": "",
        "sumatra_label_disable_antialias": True,
        "scale_dir": "C:\\Toledo\\Carga",
        "remote_label_printer": "Argox OS-214",
        "server_url": "https://api.eldensys.com.br/api",
        "server_tenant": "loja_centro",
        "server_token": "esa_segredo",
        "instance_id": "a" * 32,
    }
    cfg_path.write_text(json.dumps(v05), encoding="utf-8")
    cfg = load_config()
    assert is_paired(cfg)
    assert (cfg.server_token, cfg.instance_id, cfg.scale_dir) == (
        "esa_segredo",
        "a" * 32,
        "C:\\Toledo\\Carga",
    )
    for campo, padrao in CAMPOS_BIOMETRIA.items():
        assert getattr(cfg, campo) == padrao
    gravado = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert {k: gravado[k] for k in CAMPOS_BIOMETRIA} == CAMPOS_BIOMETRIA
    assert {k: gravado[k] for k in v05} == v05


CAMPOS_BALANCA = {
    "balanca_habilitada": False,
    "balanca_porta": "",
    "balanca_baud": 9600,
    "balanca_protocolo": "auto",
    "balanca_timeout_ms": 1500,
    "balanca_ocioso_s": 5.0,
}


def test_padroes_da_balanca_de_checkout():
    """Nasce desligada: PC sem balança não abre COM nenhuma."""
    cfg = AgentConfig()
    for campo, padrao in CAMPOS_BALANCA.items():
        assert getattr(cfg, campo) == padrao
    # Não confundir com a carga da balança etiquetadora.
    assert cfg.scale_dir == ""


def test_config_da_v06_ganha_os_campos_da_balanca_sem_perder_o_pareamento(cfg_path):
    v06 = {
        "host": "127.0.0.1",
        "port": 17777,
        "allowed_origins": ["*"],
        "allowed_origin_regex": "",
        "log_level": "INFO",
        "sumatra_path": "",
        "sumatra_label_disable_antialias": True,
        "scale_dir": "C:\\Toledo\\Carga",
        "remote_label_printer": "Argox OS-214",
        "server_url": "https://api.eldensys.com.br/api",
        "server_tenant": "loja_centro",
        "server_token": "esa_segredo",
        "instance_id": "b" * 32,
        "biometria_permitir_dev": False,
        "biometria_origens_extras": ["https://erp.minhaloja.com.br"],
        "biometria_habilitada": False,
        "biometria_previa": False,
        "biometria_ocioso_s": 12.0,
    }
    cfg_path.write_text(json.dumps(v06), encoding="utf-8")
    cfg = load_config()
    assert is_paired(cfg)
    assert (cfg.server_token, cfg.instance_id, cfg.scale_dir) == (
        "esa_segredo",
        "b" * 32,
        "C:\\Toledo\\Carga",
    )
    assert (cfg.biometria_habilitada, cfg.biometria_ocioso_s) == (False, 12.0)
    for campo, padrao in CAMPOS_BALANCA.items():
        assert getattr(cfg, campo) == padrao
    gravado = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert {k: gravado[k] for k in CAMPOS_BALANCA} == CAMPOS_BALANCA
    assert {k: gravado[k] for k in v06} == v06


def test_campos_da_balanca_sobrevivem_a_salvar_e_carregar(cfg_path):
    cfg = load_config()
    cfg.balanca_habilitada = True
    cfg.balanca_porta = "COM4"
    cfg.balanca_baud = 4800
    cfg.balanca_protocolo = "urano"
    cfg.balanca_timeout_ms = 800
    cfg.balanca_ocioso_s = 10.0
    save_config(cfg)
    assert load_config() == cfg


def test_campos_da_biometria_sobrevivem_a_salvar_e_carregar(cfg_path):
    cfg = load_config()
    cfg.biometria_habilitada = False
    cfg.biometria_previa = False
    cfg.biometria_origens_extras = ["https://erp.minhaloja.com.br"]
    cfg.biometria_ocioso_s = 5.0
    save_config(cfg)
    assert load_config() == cfg


def test_pareamento_sobrevive_a_salvar_e_carregar(cfg_path):
    cfg = load_config()
    cfg.server_url = "https://api.eldensys.com.br/api"
    cfg.server_tenant = "loja_centro"
    cfg.server_token = "esa_segredo"
    cfg.scale_dir = "C:\\Toledo\\Carga"
    cfg.remote_label_printer = "Argox OS-214"
    save_config(cfg)

    lido = load_config()
    assert is_paired(lido)
    assert lido == cfg
    # Gravacao atomica nao deixa o temporario para tras.
    assert [p.name for p in cfg_path.parent.iterdir()] == ["config.json"]


@pytest.mark.parametrize(
    "faltando", ["server_url", "server_tenant", "server_token"]
)
def test_pareamento_incompleto_nao_conta(faltando):
    dados = {"server_url": "https://x/api", "server_tenant": "loja", "server_token": "k"}
    dados[faltando] = ""
    assert not is_paired(AgentConfig(**dados))


@pytest.mark.parametrize("conteudo", ["{ json quebrado", "[1, 2, 3]", ""])
def test_arquivo_corrompido_cai_no_padrao_sem_sobrescrever(cfg_path, conteudo):
    """Nao regrava por cima: o usuario ainda pode consertar o arquivo a mao."""
    cfg_path.write_text(conteudo, encoding="utf-8")
    cfg = load_config()
    assert not is_paired(cfg)
    assert cfg.port == 17777
    assert cfg_path.read_text(encoding="utf-8") == conteudo


def test_salvar_com_destino_travado_cai_na_gravacao_direta(cfg_path, monkeypatch):
    """config.json aberto no Bloco de Notas: os.replace falha no Windows."""
    load_config()

    def replace_travado(src, dst):
        raise PermissionError("em uso")

    monkeypatch.setattr(config.os, "replace", replace_travado)
    cfg = load_config()
    cfg.scale_dir = "C:\\Balanca"
    save_config(cfg)
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["scale_dir"] == "C:\\Balanca"
    assert [p.name for p in cfg_path.parent.iterdir()] == ["config.json"]
