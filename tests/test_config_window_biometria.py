"""Textos da aba "Leitor biométrico" e do "Testar leitor" (funções puras).

Não cria janela: só importa o módulo (tkinter vem com o Python do Windows).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.config_window import describe_bio_status, describe_teste_leitor  # noqa: E402


def status(**partes):
    base = {
        "sdk": {"presente": True, "carregado": True, "erro": None},
        "leitor": {
            "conectado": True,
            "pro": True,
            "modelo": "iDBio Pro",
            "serie": "SN-9",
            "firmware": "1.2",
            "ocupado": False,
            "operacao": None,
            "digitais": 120,
            "erro": None,
            "travado": False,
        },
        "pareamento": {"pareado": True},
        "espelho": {
            "versao": "v",
            "completo": True,
            "faltando": 0,
            "sincronizado_em": "2026-11-24T10:00:00+00:00",
            "em_andamento": False,
            "progresso": None,
            "erro": None,
        },
    }
    for chave, valor in partes.items():
        base[chave] = {**base[chave], **valor}
    return base


def test_teste_do_leitor_pro_e_comum():
    texto = describe_teste_leitor(
        {"ok": True, "modelo": "iDBio Pro", "serie": "SN-9", "firmware": "1.2", "pro": True, "digitais": 7}
    )
    assert "iDBio Pro respondeu (série SN-9, firmware 1.2)." in texto
    assert "7 digitais" in texto
    comum = describe_teste_leitor({"ok": True, "modelo": "iDBio", "pro": False, "digitais": None})
    assert "não é o iDBio Pro" in comum


def test_estado_com_tudo_em_dia():
    texto = describe_bio_status(status(), poll={"modulo": True, "liberado": True})
    assert "iDBio Pro conectado (série SN-9) — 120 digitais na memória." in texto
    assert "em dia" in texto


def test_estado_sem_dll_travado_e_ocupado():
    assert "reinstale" in describe_bio_status(status(sdk={"presente": False}))
    assert "parou de responder" in describe_bio_status(status(leitor={"travado": True}))
    texto = describe_bio_status(status(leitor={"ocupado": True, "operacao": "identificar"}))
    assert "Em uso agora (identificar)" in texto


def test_estado_sem_uso_desconectado_e_erro():
    nunca = status(leitor={"conectado": False, "erro": None})
    assert "Testar leitor" in describe_bio_status(nunca)
    sem = status(leitor={"conectado": False, "erro": "Leitor biométrico não encontrado. Confira o cabo USB."})
    assert "cabo USB" in describe_bio_status(sem)


def test_estado_da_loja():
    assert "não pareado" in describe_bio_status(status(pareamento={"pareado": False}))
    assert "módulo" in describe_bio_status(status(), poll={"modulo": False})
    assert "liberado" in describe_bio_status(status(), poll={"modulo": True, "liberado": False})
    andando = status(espelho={"em_andamento": True, "progresso": 40})
    assert "40%" in describe_bio_status(andando, poll={"modulo": True, "liberado": True})
    faltou = status(espelho={"completo": False, "faltando": 3, "erro": "Memória do leitor cheia"})
    texto = describe_bio_status(faltou, poll={"modulo": True, "liberado": True})
    assert "3 ficaram de fora" in texto and "Memória do leitor cheia" in texto


def test_estado_com_erro_ou_sem_servico():
    assert describe_bio_status(None, erro="A biometria está desligada.") == "A biometria está desligada."
    assert describe_bio_status(None) == "Biometria indisponível neste agente."
