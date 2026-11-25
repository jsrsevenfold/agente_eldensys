"""Aba "Balança de checkout": textos (funções puras) e a montagem da aba.

O teste da montagem cria a janela de verdade, mas escondida (``withdraw``
logo no ``Tk()``), com o serviço da balança trocado por um dublê: confere
que o protocolo vai como rótulo na tela e volta como id no config, que
"Restaurar padrões" volta a "Auto-detectar" e que o resultado do Detectar
chega pela fila.
"""

from __future__ import annotations

import sys
import tkinter as tk
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import config_window  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.config_window import (  # noqa: E402
    describe_balanca_status,
    describe_deteccao_balanca,
    describe_leitura_balanca,
)


def status(**partes):
    base = {
        "versao_agente": "0.7.0",
        "habilitada": True,
        "config": {
            "habilitada": True,
            "porta": "COM3",
            "baud": 9600,
            "protocolo": "toledo",
            "timeout_ms": 1500,
        },
        "porta": {"aberta": False, "erro": None},
        "ultima_leitura": None,
    }
    for chave, valor in partes.items():
        atual = base[chave]
        base[chave] = {**atual, **valor} if isinstance(atual, dict) else valor
    return base


# ── textos ───────────────────────────────────────────────────────────────
def test_leitura_de_peso_e_estados():
    assert describe_leitura_balanca({"estado": "peso", "kg": 0.475, "lido_como": "toledo"}) == (
        "0,475 kg — Toledo Prix"
    )
    assert describe_leitura_balanca({"estado": "zero", "kg": 0.0}) == "prato vazio"
    assert "instável" in describe_leitura_balanca({"estado": "instavel"})
    assert "negativo" in describe_leitura_balanca({"estado": "negativo", "kg": -0.1})
    assert "sobrecarga" in describe_leitura_balanca({"estado": "sobrecarga"})
    assert "não respondeu" in describe_leitura_balanca({"estado": "sem_resposta", "bytes": 0})


def test_leitura_nao_reconhecida_mostra_o_hex():
    texto = describe_leitura_balanca(
        {"estado": "nao_reconhecido", "frame": {"texto": "..", "hex": "FF FE"}}
    )
    assert "formato desconhecido" in texto and "Recebido: FF FE" in texto


def test_leitura_pela_compatibilidade_pede_para_detectar():
    texto = describe_leitura_balanca(
        {"estado": "peso", "kg": 0.475, "protocolo": "toledo", "lido_como": "urano"}
    )
    assert texto.startswith("0,475 kg — Urano UDC / POP (Urano 12)")
    assert "Detectar" in texto
    # No auto, quem leu é a vez do auto: nada de aviso.
    assert "\n" not in describe_leitura_balanca(
        {"estado": "peso", "kg": 1.0, "protocolo": "auto", "lido_como": "elgin"}
    )


def test_deteccao():
    achou = describe_deteccao_balanca(
        {"protocolo": "urano", "estado": "peso", "kg": 0.475, "bytes": 46}
    )
    assert achou.startswith("Encontrada: Urano UDC / POP (Urano 12) (lendo 0,475 kg).")
    assert "Salvar" in achou
    lixo = describe_deteccao_balanca(
        {"protocolo": None, "estado": "nao_reconhecido", "bytes": 3, "frame": {"hex": "FF 00 01"}}
    )
    assert "nenhum protocolo conhecido" in lixo and "FF 00 01" in lixo
    assert "não respondeu" in describe_deteccao_balanca({"protocolo": None, "bytes": 0})


def test_estado_ligada_desligada_e_loopback():
    assert describe_balanca_status(status()) == "Ligada na COM3 (Toledo Prix, 9600 bps)."
    assert "Desligada" in describe_balanca_status(status(config={"habilitada": False}))
    fora = status(habilitada=False)
    assert "127.0.0.1" in describe_balanca_status(fora)
    assert "sem porta" in describe_balanca_status(status(config={"porta": ""}))


def test_estado_da_porta_e_ultima_leitura():
    aberta = describe_balanca_status(
        status(
            porta={"aberta": True},
            ultima_leitura={"estado": "peso", "kg": 1.234, "protocolo": "toledo", "lido_como": "toledo"},
        )
    )
    assert "Porta aberta agora" in aberta
    assert "Última leitura: 1,234 kg — Toledo Prix" in aberta
    em_uso = describe_balanca_status(status(porta={"erro": "A porta COM3 está em uso por outro programa"}))
    assert "em uso por outro programa" in em_uso
    assert "Porta aberta" not in em_uso


def test_estado_com_erro_ou_sem_servico():
    assert describe_balanca_status(None, erro="falhou") == "falhou"
    assert describe_balanca_status(None) == "Balança de checkout indisponível neste agente."


# ── a aba montada (janela escondida) ─────────────────────────────────────
class ServicoFalso:
    def __init__(self):
        self.chamadas: list[str] = []

    def portas(self):
        self.chamadas.append("portas")
        return {"portas": [{"porta": "COM3", "descricao": "USB-SERIAL CH340 (COM3)"}]}

    def status(self):
        self.chamadas.append("status")
        return status()


@pytest.fixture(scope="module")
def _janela_do_modulo():
    """UMA janela para o módulo: o Tcl do Windows às vezes não acha o
    ``init.tcl`` ao criar vários interpretadores seguidos no mesmo processo."""

    class TkOculto(tk.Tk):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.withdraw()

    cfg = AgentConfig(
        balanca_habilitada=True, balanca_porta="COM3", balanca_protocolo="urano", balanca_baud=4800
    )
    servico = ServicoFalso()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(config_window.tk, "Tk", TkOculto)
        mp.setattr(config_window, "load_config", lambda: cfg)
        mp.setattr(config_window, "list_printers", lambda: [])
        mp.setattr(config_window, "get_worker", lambda: None)
        mp.setattr(config_window, "get_servico", lambda: None)
        mp.setattr(config_window, "get_servico_balanca", lambda: servico)
        mp.setattr(config_window.messagebox, "askyesno", lambda *a, **kw: True)
        erro: Exception | None = None
        for _ in range(3):
            try:
                w = config_window.ConfigWindow()
                break
            except tk.TclError as e:  # pragma: no cover - Tcl/Tk instável ou ausente
                erro = e
        else:  # pragma: no cover
            pytest.skip(f"Tk indisponível: {erro}")
        yield w, servico
        w._on_close()


@pytest.fixture
def janela(_janela_do_modulo):
    """A janela do módulo com os campos da balança como vieram do config."""
    w, servico = _janela_do_modulo
    w.vars["balanca_habilitada"].set(True)
    w.vars["balanca_baud"].set(4800)
    w.vars["balanca_timeout_ms"].set(1500)
    w.balanca_porta_var.set("COM3")
    w.balanca_protocolo_var.set("Urano UDC / POP (Urano 12)")
    w.balanca_resultado.set("")
    return w, servico


def test_aba_mostra_o_rotulo_e_grava_o_id(janela):
    w, servico = janela
    assert w.balanca_protocolo_var.get() == "Urano UDC / POP (Urano 12)"
    assert w.balanca_porta_var.get() == "COM3"
    assert "COM3 (USB-SERIAL CH340 (COM3))" in w.balanca_portas_info.get()
    assert "Ligada na COM3" in w.balanca_status.get()
    assert {"portas", "status"} <= set(servico.chamadas)
    w.balanca_protocolo_var.set("Filizola")
    c = w._collect()
    assert (c.balanca_protocolo, c.balanca_porta, c.balanca_baud) == ("filizola", "COM3", 4800)
    assert c.balanca_habilitada is True


def test_aba_valida_tempo_e_porta(janela):
    w, _ = janela
    w.vars["balanca_timeout_ms"].set(50)
    with pytest.raises(ValueError, match="200 e 10000"):
        w._collect()
    w.vars["balanca_timeout_ms"].set(1500)
    w.balanca_porta_var.set(config_window.SEM_PORTA_BALANCA)
    with pytest.raises(ValueError, match="porta"):
        w._collect()
    w.vars["balanca_habilitada"].set(False)
    assert w._collect().balanca_porta == ""


def test_restaurar_padroes_volta_ao_auto(janela):
    w, _ = janela
    w._reset_defaults()
    assert w.balanca_protocolo_var.get() == "Auto-detectar"
    assert w.balanca_porta_var.get() == config_window.SEM_PORTA_BALANCA
    c = w._collect()
    assert (c.balanca_habilitada, c.balanca_protocolo, c.balanca_baud, c.balanca_timeout_ms) == (
        False,
        "auto",
        9600,
        1500,
    )


def test_resultado_do_detectar_chega_pela_fila_e_escolhe_o_protocolo(janela):
    w, _ = janela
    w._balanca_ocupada(True)
    w._fila_balanca.put(
        ("deteccao", {"protocolo": "toledo", "estado": "peso", "kg": 1.234, "bytes": 7})
    )
    w._fila_balanca.put(("fim", None))
    w._drenar_balanca()
    assert w.balanca_protocolo_var.get() == "Toledo Prix"
    assert w.balanca_resultado.get().startswith("Encontrada: Toledo Prix")
    assert "disabled" not in w.balanca_btn_testar.state()
    w._fila_balanca.put(("leitura", {"estado": "peso", "kg": 0.5, "lido_como": "toledo"}))
    w._drenar_balanca()
    assert w.balanca_resultado.get() == "Ao vivo: 0,500 kg — Toledo Prix"


def test_testar_sem_porta_pede_a_porta(janela):
    w, _ = janela
    w.balanca_porta_var.set(config_window.SEM_PORTA_BALANCA)
    w._balanca_testar()
    assert w.balanca_resultado.get() == "Escolha a porta da balança primeiro."
