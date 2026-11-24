"""Tabela de códigos do iDBio (contrato, seção 0) e o formato de erro das rotas."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.biometria import erros  # noqa: E402
from agent.biometria.erros import ErroBiometria, ErroLeitor, erro_remoto, traduzir  # noqa: E402
from agent.remote_client import (  # noqa: E402
    ModuloInativo,
    PairingRejected,
    RateLimited,
    ServidorRecusou,
    StoreBlocked,
    TransportError,
)

#: Copiado do contrato (seção 0), palavra por palavra.
CONTRATO = {
    3: ("sobrescrito", "Digital substituída na memória do leitor"),
    2: ("memoria_vazia", "Leitor sem digitais gravadas"),
    1: ("ja_iniciado", "Leitor já estava aberto"),
    -1: ("desconhecido", "Erro desconhecido no leitor"),
    -2: ("sem_leitor", "Leitor biométrico não encontrado. Confira o cabo USB."),
    -3: ("argumento", "Erro interno ao chamar o leitor"),
    -4: ("argumento", "Erro interno ao chamar o leitor"),
    -5: ("falha_captura", "Não foi possível ler o dedo. Tente de novo."),
    -6: ("tempo_esgotado", "Nenhum dedo colocado a tempo"),
    -7: ("usb", "Falha de comunicação USB com o leitor"),
    -8: ("io_host", "Falha de comunicação com o leitor neste computador"),
    -9: ("ja_cadastrada", "Esta digital já está gravada no leitor"),
    -10: ("merge", "As três leituras não parecem do mesmo dedo. Recomece."),
    -11: ("match", "Falha ao comparar digitais"),
    -13: ("memoria_cheia", "Memória do leitor cheia"),
    -14: ("id_inexistente", "Digital não encontrada no leitor"),
    -16: ("nao_pro", "Este leitor não é o iDBio Pro (sem identificação)"),
    -18: ("nao_identificado", "Digital não reconhecida"),
    -19: ("ocupado", "Leitor ocupado"),
    -20: ("cancelado", "Leitura cancelada"),
    -21: ("sem_dedo", "Nenhum dedo no leitor"),
    -22: ("template_invalido", "Digital em formato inválido"),
}


@pytest.mark.parametrize("codigo", sorted(CONTRATO))
def test_tabela_igual_ao_contrato(codigo):
    assert traduzir(codigo) == CONTRATO[codigo]


@pytest.mark.parametrize("codigo", [-12, -15, -17, -99, 42])
def test_codigo_fora_do_contrato_e_desconhecido(codigo):
    assert traduzir(codigo)[0] == "desconhecido"
    e = ErroLeitor(codigo, "Op")
    assert f"código {codigo}" in e.detail


@pytest.mark.parametrize(
    ("codigo", "status", "erro"),
    [
        (-2, 503, "leitor_desconectado"),
        (-7, 503, "leitor_desconectado"),
        (-19, 503, "leitor_em_uso"),
        (-16, 422, "leitor_nao_pro"),
        (-13, 409, "memoria_cheia"),
        (-10, 422, "merge"),
        (-18, 422, "nao_identificado"),
        (-6, 422, "tempo_esgotado"),
        (-22, 422, "template_invalido"),
        (-1, 500, "interno"),
        (-4, 500, "interno"),
    ],
)
def test_codigo_vira_status_http_e_erro_da_rota(codigo, status, erro):
    e = ErroLeitor(codigo, "Op")
    assert (e.status, e.erro) == (status, erro)
    corpo = e.corpo()
    assert set(corpo) >= {"erro", "mensagem", "detail"}
    assert corpo["mensagem"] == CONTRATO[codigo][1]


def test_corpo_leva_campos_extras():
    e = erros.LeitorOcupado(pode_cancelar=True, operacao="identificar")
    assert e.status == 409
    assert e.corpo()["erro"] == "ocupado"
    assert e.corpo()["pode_cancelar"] is True


@pytest.mark.parametrize(
    ("exc", "status", "erro"),
    [
        (PairingRejected("x"), 409, "pareamento_recusado"),
        (ModuloInativo("x", "biometria"), 403, "modulo_inativo"),
        (StoreBlocked("Loja bloqueada."), 409, "servidor_recusou"),
        (ServidorRecusou(409, "biometria_desafio_usado", "Já usado."), 409, "servidor_recusou"),
        (TransportError("caiu"), 503, "sem_conexao"),
        (RateLimited("calma", 3), 503, "sem_conexao"),
    ],
)
def test_erro_remoto(exc, status, erro):
    e = erro_remoto(exc)
    assert isinstance(e, ErroBiometria)
    assert (e.status, e.erro) == (status, erro)


def test_servidor_recusou_leva_o_code_e_a_frase_do_servidor():
    e = erro_remoto(ServidorRecusou(403, "biometria_sem_vinculo", "Entre com a senha antes."))
    assert e.corpo()["codigo"] == "biometria_sem_vinculo"
    assert e.mensagem == "Entre com a senha antes."
    assert erro_remoto(TransportError("x"), pode_reenviar=True).corpo()["pode_reenviar"] is True
