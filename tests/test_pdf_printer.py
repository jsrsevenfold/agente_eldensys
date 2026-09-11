"""Testes do caminho de impressao de PDF — o coracao do agente.

Nao imprimem nada: o subprocess do SumatraPDF e trocado por um espiao, entao
o que se verifica e a LINHA DE COMANDO montada. E ali que moram as decisoes
que quebram loja (noscale, disable-auto-rotation, -appdata).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import pdf_printer  # noqa: E402
from agent.config import AgentConfig  # noqa: E402


class _Result:
    returncode = 0
    stdout = ""
    stderr = ""


@pytest.fixture
def espiao(monkeypatch):
    """Captura o argv passado ao subprocess, sem executar nada."""
    capturado: dict = {}

    def fake_run(args, **kwargs):
        capturado["args"] = list(args)
        return _Result()

    monkeypatch.setattr(pdf_printer.subprocess, "run", fake_run)
    return capturado


@pytest.fixture
def sumatra_bundled(monkeypatch, tmp_path):
    """Finge que o Sumatra empacotado existe em <tmp>/vendor/SumatraPDF.exe."""
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    exe = vendor / "SumatraPDF.exe"
    exe.write_bytes(b"MZ")
    monkeypatch.setattr(pdf_printer, "_resource_dir", lambda: tmp_path)
    monkeypatch.setattr(pdf_printer, "find_sumatra", lambda: str(exe))
    return exe


@pytest.fixture
def appdata_tmp(monkeypatch, tmp_path):
    d = tmp_path / "sumatra-label"
    monkeypatch.setattr(pdf_printer, "LABEL_APPDATA_DIR", d)
    return d


def _cfg(monkeypatch, **kw):
    cfg = AgentConfig(**kw)
    monkeypatch.setattr(pdf_printer, "load_config", lambda: cfg)
    return cfg


# ── -print-settings ────────────────────────────────────────────────
def test_print_settings_sempre_tem_noscale_e_sem_rotacao():
    s = pdf_printer._build_print_settings(1, None, False)
    assert s == "noscale,disable-auto-rotation"


def test_print_settings_copias_papel_duplex():
    s = pdf_printer._build_print_settings(3, "A4", True)
    assert s == "noscale,disable-auto-rotation,3x,paper=A4,duplexlong"


def test_print_settings_uma_copia_nao_vira_token():
    # "1x" e ruido e ja confundiu log de suporte no passado.
    assert "1x" not in pdf_printer._build_print_settings(1, None, False)


# ── perfil -appdata do modo etiqueta ───────────────────────────────
def test_ensure_label_appdata_escreve_disable_antialias(appdata_tmp):
    d = pdf_printer.ensure_label_appdata()
    assert d == appdata_tmp
    txt = (appdata_tmp / pdf_printer.LABEL_SETTINGS_NAME).read_text(encoding="utf-8")
    assert "DisableAntiAlias = true" in txt
    # O perfil e isolado: nao pode herdar sessao nem reusar instancia, senao
    # um Sumatra ja aberto pelo usuario atenderia o job com OUTRAS settings.
    assert "ReuseInstance = false" in txt
    assert "PrintScale = noscale" in txt


def test_ensure_label_appdata_e_idempotente(appdata_tmp):
    assert pdf_printer.ensure_label_appdata() is not None
    assert pdf_printer.ensure_label_appdata() is not None


def test_ensure_label_appdata_nunca_levanta(monkeypatch, tmp_path):
    alvo = tmp_path / "nao-da"
    monkeypatch.setattr(pdf_printer, "LABEL_APPDATA_DIR", alvo)

    def boom(*a, **k):
        raise OSError("disco cheio")

    monkeypatch.setattr(Path, "mkdir", boom)
    assert pdf_printer.ensure_label_appdata() is None


def test_is_bundled_sumatra(sumatra_bundled, tmp_path):
    assert pdf_printer.is_bundled_sumatra(str(sumatra_bundled)) is True
    # Mesmo nome de arquivo, outra pasta: e o Sumatra de alguem, nao o nosso.
    intruso = tmp_path / "ProgramFiles" / "SumatraPDF.exe"
    intruso.parent.mkdir()
    intruso.write_bytes(b"MZ")
    assert pdf_printer.is_bundled_sumatra(str(intruso)) is False
    assert pdf_printer.is_bundled_sumatra("") is False


# ── print_pdf: quando a flag entra e quando NAO entra ──────────────
def test_label_passa_appdata(monkeypatch, espiao, sumatra_bundled, appdata_tmp):
    _cfg(monkeypatch)
    pdf_printer.print_pdf("Elgin L42", b"%PDF-1.4", render_mode="label")
    args = espiao["args"]
    i = args.index("-appdata")
    assert args[i + 1] == str(appdata_tmp)
    # -appdata tem que vir ANTES do resto: o Sumatra le preferencias cedo.
    assert i < args.index("-print-to")
    assert "noscale,disable-auto-rotation" in args


def test_default_nao_passa_appdata(monkeypatch, espiao, sumatra_bundled, appdata_tmp):
    """NFC-e, DANFE e comanda passam por aqui. Nada pode mudar para eles."""
    _cfg(monkeypatch)
    pdf_printer.print_pdf("Laser", b"%PDF-1.4")
    assert "-appdata" not in espiao["args"]


def test_render_mode_desconhecido_degrada_para_default(
    monkeypatch, espiao, sumatra_bundled, appdata_tmp
):
    """EldenSys mais novo que o agente nao pode quebrar impressao."""
    _cfg(monkeypatch)
    pdf_printer.print_pdf("Elgin L42", b"%PDF-1.4", render_mode="futuro")
    assert "-appdata" not in espiao["args"]


def test_config_desligada_nao_passa_appdata(
    monkeypatch, espiao, sumatra_bundled, appdata_tmp
):
    _cfg(monkeypatch, sumatra_label_disable_antialias=False)
    pdf_printer.print_pdf("Elgin L42", b"%PDF-1.4", render_mode="label")
    assert "-appdata" not in espiao["args"]


def test_sumatra_de_sistema_nunca_recebe_appdata(
    monkeypatch, espiao, appdata_tmp, tmp_path
):
    """O portao que impede a loja de parar de imprimir TUDO.

    Sumatra antigo trata flag desconhecida como arquivo a abrir: returncode
    != 0 -> RuntimeError -> nenhum documento sai, nem cupom.
    """
    _cfg(monkeypatch)
    outro = tmp_path / "sistema" / "SumatraPDF.exe"
    outro.parent.mkdir()
    outro.write_bytes(b"MZ")
    monkeypatch.setattr(pdf_printer, "_resource_dir", lambda: tmp_path / "vazio")
    monkeypatch.setattr(pdf_printer, "find_sumatra", lambda: str(outro))
    pdf_printer.print_pdf("Elgin L42", b"%PDF-1.4", render_mode="label")
    assert "-appdata" not in espiao["args"]


def test_sem_sumatra_levanta(monkeypatch):
    monkeypatch.setattr(pdf_printer, "find_sumatra", lambda: None)
    with pytest.raises(RuntimeError, match="SumatraPDF"):
        pdf_printer.print_pdf("X", b"%PDF-1.4")


def test_returncode_nao_zero_levanta(monkeypatch, sumatra_bundled):
    _cfg(monkeypatch)

    class Falha:
        returncode = 2
        stdout = ""
        stderr = "erro"

    monkeypatch.setattr(pdf_printer.subprocess, "run", lambda *a, **k: Falha())
    with pytest.raises(RuntimeError, match="code=2"):
        pdf_printer.print_pdf("X", b"%PDF-1.4")
