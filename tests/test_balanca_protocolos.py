"""Tradução de ``eldensys_mobile/test/core/services/balanca_protocolos_test.dart``.

Os 70 casos do Dart, nos mesmos grupos (uma classe por ``group``), com os
mesmos auxiliares (``_urano12``, ``_stx_etx``, ``_txt``). No fim, os casos das
armadilhas do porte Dart → Python (arredondamento, regex, ``trim``, latin-1),
que o teste do Dart não precisa ter.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.balanca import protocolos as p  # noqa: E402
from agent.balanca.protocolos import (  # noqa: E402
    EstadoFrame,
    FrameBalanca,
    OrdemAuto,
    PedidoUrano,
    arredondar_gramas,
    descrever_bytes,
    descrever_frame,
    interpretar_compatibilidade,
    interpretar_elgin,
    interpretar_filizola,
    interpretar_generico,
    interpretar_toledo,
    interpretar_urano,
    protocolo_do_frame,
    resultado_do_frame,
    rotulo_protocolo,
    urano_completo,
)

_STX = 0x02
_ETX = 0x03
_ESC = 0x1B
_CAN = 0x18


def _cu(s: str) -> list[int]:
    """``codeUnits`` do Dart (só ASCII/latin-1 nestes testes)."""
    return list(s.encode("latin-1"))


def _urano12(
    peso: str = "   475",
    unidade: str = " g",
    estab: str = "*",
    sinal: str = " ",
    marca: str = "PESO:",
    trailer: int = _ESC,
) -> list[int]:
    """Frame Urano 12 de 46 bytes, no layout do manual da UDC CO / CO-E:
    ``ESC T1 ESC A13 ESC N0 ESC S2 ESC D4 ESC Q193 ESC B`` + `` `` + estab +
    `` `` + marca + sinal + peso (6) + unidade (``kg``|`` g``) + ``ESC E ESC
    P01``. ``trailer`` é o controle antes de ``E``/``P`` (o PDV do cliente
    mostrou ``18``)."""
    return [
        _ESC, *_cu("T1"),
        _ESC, *_cu("A13"),
        _ESC, *_cu("N0"),
        _ESC, *_cu("S2"),
        _ESC, *_cu("D4"),
        _ESC, *_cu("Q193"),
        _ESC, *_cu("B"),
        *_cu(f" {estab} {marca}{sinal}{peso}{unidade}"),
        trailer, *_cu("E"),
        trailer, *_cu("P01"),
    ]  # fmt: skip


def _stx_etx(corpo: str) -> list[int]:
    """``STX corpo ETX`` (Toledo Prix, Urano Std01–Std05, Filizola)."""
    return [_STX, *_cu(corpo), _ETX]


def _txt(s: str) -> list[int]:
    return _cu(s)


def _kg(f):
    return None if f is None else f.kg


def _estado(f):
    return None if f is None else f.estado


# ── Urano 12 (UDC CO / CO-E / POP) ───────────────────────────────────────
class TestUrano12:
    def test_o_frame_de_teste_tem_o_layout_do_manual_46_bytes_sem_stx_etx_cr(self):
        # O mesmo frame em hex que a UDC-CO 30/5E do cliente manda com 475 g
        # no prato (bytes 25 = '*', 32 = sinal, 33-38 = peso, 39-40 = ' g').
        hexa = (
            "1B 54 31 1B 41 31 33 1B 4E 30 1B 53 32 1B 44 34 1B 51 31 39 33 1B "
            "42 20 2A 20 50 45 53 4F 3A 20 20 20 20 34 37 35 20 67 1B 45 1B 50 "
            "30 31"
        )
        esperado = [int(h, 16) for h in hexa.split(" ")]
        assert _urano12() == esperado
        assert len(_urano12()) == 46

    def test_causa_raiz_colar_todos_os_digitos_da_o_11302419347_501_do_print(self):
        # O que o parser Toledo antigo fazia com este frame: apagar tudo que
        # não é dígito e dividir por 1000.
        import re

        colado = re.sub(r"[^0-9.,]", "", bytes(_urano12()).decode("latin-1"))
        assert colado == "11302419347501"
        assert int(colado) / 1000 == 11302419347.501

    def test_frame_do_cliente_475_g_vira_0_475_kg_estavel(self):
        f = interpretar_urano(_urano12())
        assert _estado(f) == EstadoFrame.peso
        assert _kg(f) == 0.475
        assert f.byte_estabilidade == 0x2A

    def test_trailer_com_can_no_lugar_de_esc_nao_muda_nada(self):
        f = interpretar_urano(_urano12(trailer=_CAN))
        assert _estado(f) == EstadoFrame.peso
        assert _kg(f) == 0.475

    def test_visor_em_kg_com_virgula_vira_kg(self):
        f = interpretar_urano(_urano12(peso=" 1,234", unidade="kg"))
        assert _estado(f) == EstadoFrame.peso
        assert _kg(f) == 1.234

    def test_a_unidade_manda_nao_o_separador_150kg_sao_150_kg(self):
        f = interpretar_urano(_urano12(peso="   150", unidade="kg"))
        assert _kg(f) == 150.0

    def test_exemplo_do_comentario_do_acbr_campo_de_4_caracteres_da_5_10_kg(self):
        b = [
            _ESC, *_cu("T1"), _ESC, *_cu("A13"),
            _ESC, *_cu("N0"), _ESC, *_cu("S2"),
            _ESC, *_cu("D4"), _ESC, *_cu("Q193"),
            _ESC, *_cu("B * PESO: 5,10kg"),
            _ESC, *_cu("E"), _ESC, *_cu("P01"),
        ]  # fmt: skip
        f = interpretar_urano(b)
        assert _estado(f) == EstadoFrame.peso
        assert _kg(f) == 5.10

    def test_prato_vazio_vira_zero_retorno_a_zero_da_refeicao_por_quilo(self):
        assert _estado(interpretar_urano(_urano12(peso="     0"))) == EstadoFrame.zero

    def test_espaco_no_byte_25_e_instavel_reconhecido_nao_none(self):
        f = interpretar_urano(_urano12(estab=" "))
        assert f is not None
        assert f.estado == EstadoFrame.instavel

    def test_byte_de_estabilidade_estranho_conta_como_estavel_regra_do_acbr(self):
        f = interpretar_urano(_urano12(estab="#"))
        assert _estado(f) == EstadoFrame.peso
        assert f.byte_estabilidade == 0x23

    def test_sinal_menos_vira_negativo(self):
        f = interpretar_urano(_urano12(sinal="-", peso="   100"))
        assert _estado(f) == EstadoFrame.negativo

    def test_frame_cortado_logo_depois_de_peso_e_instavel_reconhecido(self):
        f = interpretar_urano(_urano12()[:32])
        assert _estado(f) == EstadoFrame.instavel

    def test_visor_com_mensagem_hhhhhh_sobrecarga_llllll_abaixo_do_minimo(self):
        assert _estado(interpretar_urano(_urano12(peso="HHHHHH"))) == EstadoFrame.sobrecarga
        assert _estado(interpretar_urano(_urano12(peso="LLLLLL"))) == EstadoFrame.negativo

    def test_dois_frames_no_buffer_vale_o_ultimo(self):
        f = interpretar_urano([*_urano12(), *_urano12(peso="   480")])
        assert _kg(f) == 0.480

    def test_buffer_comecando_no_meio_de_um_frame_mais_frame_inteiro_da_0_475(self):
        f = interpretar_urano([*_urano12(peso="   900")[26:], *_urano12()])
        assert _estado(f) == EstadoFrame.peso
        assert _kg(f) == 0.475

    def test_frame_completo_seguido_de_um_cortado_vale_o_completo(self):
        f = interpretar_urano([*_urano12(), *_urano12(peso="   900")[:32]])
        assert _kg(f) == 0.475

    def test_urano_c_peso_l_e_tara_vira_peso_liquido_tara_ignorada(self):
        b = [
            _ESC, *_cu("T2"), _ESC, *_cu("A13"),
            _ESC, *_cu("N0"), _ESC, *_cu("S2"),
            _ESC, *_cu("D4"), _ESC, *_cu("Q193"),
            _ESC, *_cu("B * PESO(L):    600 g      TARA:    300 g"),
            _ESC, *_cu("E"), _ESC, *_cu("P01"),
        ]  # fmt: skip
        f = interpretar_urano(b)
        assert _estado(f) == EstadoFrame.peso
        assert _kg(f) == 0.600

    def test_peso_l_do_pop_s_use_cb2_nao_e_urano_12(self):
        assert (
            interpretar_urano(_txt("TARA:   0.000kg       PESO L:  1.542kg      R$/kg:  0.00"))
            is None
        )

    def test_balanca_generica_imprimindo_peso_1_234_kg_nao_vira_urano(self):
        assert interpretar_urano(_txt("PESO:  1,234 kg\r\n")) is None
        assert interpretar_urano(_txt("   PESO: 1,234 kg\r\n")) is None

    def test_toledo_nao_e_urano(self):
        assert interpretar_urano(_stx_etx("01234")) is None
        assert interpretar_urano([]) is None

    def test_unidade_em_maiusculas_kg_tambem_vale(self):
        f = interpretar_urano(_urano12(peso=" 1,234", unidade="KG"))
        assert _estado(f) == EstadoFrame.peso
        assert _kg(f) == 1.234

    def test_urano_completo_fim_de_frame_sem_terminador(self):
        assert urano_completo(_urano12()) is True
        assert urano_completo(_urano12()[:32]) is False
        assert urano_completo(_urano12(peso="HHHHHH")) is True
        assert urano_completo(_stx_etx("01234")) is False


# ── Toledo estrito (Toledo Prix e Urano Std01–Std05) ─────────────────────
class TestToledoEstrito:
    def test_stx_01234_etx_da_1_234_kg(self):
        f = interpretar_toledo(_stx_etx("01234"))
        assert _estado(f) == EstadoFrame.peso
        assert _kg(f) == 1.234

    def test_eco_do_enq_antes_do_frame(self):
        assert _kg(interpretar_toledo([0x05, *_stx_etx("01234")])) == 1.234

    def test_sem_stx_etx_terminado_em_cr_7_digitos_da_1_234_kg(self):
        assert _kg(interpretar_toledo(_txt("0001234\r"))) == 1.234

    def test_stx_cr_com_kg_protocolo_c_do_acbr(self):
        assert _kg(interpretar_toledo([_STX, *_txt("  1.234kg"), 0x0D])) == 1.234

    def test_dois_frames_vale_o_ultimo(self):
        assert _kg(interpretar_toledo([*_stx_etx("01234"), *_stx_etx("01240")])) == 1.240

    def test_cauda_atrasada_mais_frame_novo_vale_o_frame_novo(self):
        assert _kg(interpretar_toledo([*_txt("34"), _ETX, *_stx_etx("01234")])) == 1.234

    def test_frame_completo_seguido_de_um_cortado_vale_o_completo(self):
        assert _kg(interpretar_toledo([*_stx_etx("01234"), _STX, *_txt("012")])) == 1.234

    def test_frame_cortado_sozinho_e_none_o_antigo_lia_0_012_kg(self):
        assert interpretar_toledo([_STX, *_txt("012")]) is None

    def test_etx_solto_depois_do_frame_nao_estraga_a_leitura(self):
        assert _kg(interpretar_toledo([*_stx_etx("01234"), *_txt("A"), _ETX])) == 1.234

    def test_stx_lf_tambem_e_frame(self):
        assert _kg(interpretar_toledo([_STX, *_txt("01234"), 0x0A])) == 1.234

    def test_variante_sem_stx_com_eco_do_enq_nul_ou_linha_de_ruido(self):
        assert _kg(interpretar_toledo([0x05, *_txt("0001234"), 0x0D])) == 1.234
        assert _kg(interpretar_toledo([0x00, *_txt("0001234"), 0x0D])) == 1.234
        assert _kg(interpretar_toledo([*_txt("0001234"), 0x0D, 0x0A, 0x00, 0x0D, 0x0A])) == 1.234

    def test_std01_real_7_caracteres_espacos_a_esquerda(self):
        assert _kg(interpretar_toledo(_stx_etx("  0.475"))) == 0.475
        assert _estado(interpretar_toledo(_stx_etx("- 0.100"))) == EstadoFrame.negativo

    def test_std03_zeros_a_esquerda(self):
        assert _kg(interpretar_toledo(_stx_etx(" 00.475"))) == 0.475
        assert _estado(interpretar_toledo(_stx_etx("-00.100"))) == EstadoFrame.negativo

    def test_std02_zero_espacos_vira_zero(self):
        assert _estado(interpretar_toledo(_stx_etx("  0.000"))) == EstadoFrame.zero
        assert _estado(interpretar_toledo(_stx_etx("   .000"))) == EstadoFrame.zero

    def test_std04_gramas_com_zeros_a_esquerda_da_0_475_kg(self):
        assert _kg(interpretar_toledo(_stx_etx("00475"))) == 0.475

    def test_status_de_5_e_de_7_letras(self):
        assert _estado(interpretar_toledo(_stx_etx("IIIII"))) == EstadoFrame.instavel
        assert _estado(interpretar_toledo(_stx_etx("IIIIIII"))) == EstadoFrame.instavel
        assert _estado(interpretar_toledo(_stx_etx("NNNNN"))) == EstadoFrame.negativo
        assert _estado(interpretar_toledo(_stx_etx("SSSSSSS"))) == EstadoFrame.sobrecarga

    def test_status_do_self_checkout_toledo(self):
        assert _estado(interpretar_toledo(_stx_etx("+III,III"))) == EstadoFrame.instavel
        assert _estado(interpretar_toledo(_stx_etx("-III,III"))) == EstadoFrame.negativo
        assert _kg(interpretar_toledo(_stx_etx("+000,475"))) == 0.475

    def test_campos_colados_15_digitos_status_word_e_none_nao_peso(self):
        assert interpretar_toledo(_stx_etx("100001234000000")) is None
        # P03 contínuo: STX SWA SWB SWC PPPPPP TTTTTT CR CS.
        assert (
            interpretar_toledo([_STX, 0x31, 0x30, 0x30, *_txt("001234000000"), 0x0D, 0x35])
            is None
        )

    def test_acima_do_teto_de_um_item_99_999_nao_e_peso_de_balanca(self):
        assert interpretar_toledo(_stx_etx("99999.9")) is None

    def test_regressao_o_frame_urano_12_nao_e_toledo_nunca_11302419347_501(self):
        assert interpretar_toledo(_urano12()) is None
        assert interpretar_toledo(_urano12(peso=" 1,234", unidade="kg")) is None


# ── Filizola ─────────────────────────────────────────────────────────────
class TestFilizola:
    def test_stx_01234_etx_da_1_234_kg_hoje_dava_1234_kg(self):
        assert _kg(interpretar_filizola(_stx_etx("01234"))) == 1.234

    def test_cabecalho_ps_ii_tolerancia_do_formato_antigo(self):
        assert _kg(interpretar_filizola(_txt("PS01.234\r\n"))) == 1.234
        assert _estado(interpretar_filizola(_txt("II01.234\r\n"))) == EstadoFrame.instavel

    def test_cauda_atrasada_mais_frame_novo_vale_o_frame_novo(self):
        assert _kg(interpretar_filizola([*_txt("34"), _ETX, *_stx_etx("01234")])) == 1.234

    def test_o_frame_urano_12_nao_e_filizola_hoje_dava_1_000_kg(self):
        assert interpretar_filizola(_urano12()) is None


# ── Passivos (Elgin, genérico) ───────────────────────────────────────────
class TestPassivos:
    def test_formatos_da_elgin_continuam_lidos(self):
        assert _kg(interpretar_elgin(_txt("P000.500\r"))) == 0.5
        assert _kg(interpretar_elgin(_txt("ST,GS,+00.500kg\r\n"))) == 0.5
        assert _estado(interpretar_elgin(_txt("P000.000\r"))) == EstadoFrame.zero

    def test_generico_continua_lendo_numero_decimal_e_inteiro_em_gramas(self):
        assert _kg(interpretar_generico(_txt("  1.234kg\r\n"))) == 1.234
        assert _kg(interpretar_generico(_txt("PESO: 1,234 kg\r\n"))) == 1.234
        assert _kg(interpretar_generico(_txt("001234\r\n"))) == 1.234

    def test_475_g_sao_0_475_kg_nunca_475_kg_inteiro_sem_unidade_e_gramas(self):
        assert _kg(interpretar_elgin(_txt("  475 g\r\n"))) == 0.475
        assert _kg(interpretar_elgin(_txt("PESO: 1234 g\r\n"))) == 1.234
        assert _kg(interpretar_elgin(_txt("00001234\r"))) == 1.234
        assert _kg(interpretar_generico(_txt("  475 g\r\n"))) == 0.475

    def test_elgin_ignora_o_frame_cortado_depois_do_ultimo_cr(self):
        # "P01" no fim leria 1 kg; "P012" leria 12 kg.
        assert _kg(interpretar_elgin(_txt("P012.500\rP012.500\rP01"))) == 12.5

    def test_o_frame_urano_12_nao_e_elgin_nem_generico_a_elgin_lia_1_000_kg(self):
        for b in (
            _urano12(),
            _urano12(peso=" 1,234", unidade="kg"),
            # Frame pela metade, sem o PESO ainda: o cabeçalho já denuncia.
            _urano12()[:20],
        ):
            assert interpretar_elgin(b) is None
            assert interpretar_generico(b) is None


# ── protocolo_do_frame (dica do diagnóstico) ─────────────────────────────
class TestProtocoloDoFrame:
    def test_classifica_pelo_formato_do_frame(self):
        assert protocolo_do_frame(_urano12()) == "urano"
        assert protocolo_do_frame(_urano12(estab=" ")) == "urano"
        assert protocolo_do_frame(_stx_etx("01234")) == "toledo"
        assert protocolo_do_frame(_txt("PS01.234\r\n")) == "filizola"

    def test_elgin_so_pelo_formato_dela_generico_e_lixo_nunca(self):
        assert protocolo_do_frame(_txt("P000.500\r")) == "elgin"
        assert protocolo_do_frame(_txt("ST,GS,+00.500kg\r\n")) == "elgin"
        assert protocolo_do_frame(_txt("PESO: 1,234 kg\r\n")) is None
        assert protocolo_do_frame(_txt("abc 12,5 xyz")) is None
        assert protocolo_do_frame([]) is None


# ── interpretar_compatibilidade (salvo toledo/filizola) ──────────────────
class TestInterpretarCompatibilidade:
    def test_a_urano_udc_do_cliente_com_toledo_salvo_vira_urano_12(self):
        c = interpretar_compatibilidade(_urano12())
        assert c.protocolo == "urano"
        assert c.frame.kg == 0.475

    def test_elgin_que_o_detectar_antigo_salvou_como_toledo_continua_lendo(self):
        a = interpretar_compatibilidade(_txt("P000.500\r"))
        assert a.protocolo == "elgin"
        assert a.frame.kg == 0.5
        b = interpretar_compatibilidade(_txt("ST,GS,+00.500kg\r\n"))
        assert b.protocolo == "elgin"
        assert b.frame.kg == 0.5
        c = interpretar_compatibilidade(_txt("US,GS,+00.500kg\r\n"))
        assert c.frame.estado == EstadoFrame.instavel

    def test_ps_com_toledo_salvo_vira_filizola(self):
        c = interpretar_compatibilidade(_txt("PS01.234\r\n"))
        assert c.protocolo == "filizola"
        assert c.frame.kg == 1.234

    def test_balanca_de_texto_com_um_numero_so_vira_generico(self):
        a = interpretar_compatibilidade(_txt("PESO: 1,234 kg\r\n"))
        assert a.protocolo == "generico"
        assert a.frame.kg == 1.234
        assert interpretar_compatibilidade(_txt("  475 g\r\n")).frame.kg == 0.475
        # Um só com unidade decide ("N0" da Urano POP-Z antiga).
        assert interpretar_compatibilidade(_txt("N0   0,555kg\r\n")).frame.kg == 0.555

    def test_dois_numeros_sem_unidade_ou_frame_binario_e_none_nada_de_colar(self):
        assert interpretar_compatibilidade(_txt("T 12 P 475\r\n")) is None
        # Toledo protocolo A: STX S1 PPPPPP S2 TTTTTT UUUUUU CR CS.
        assert (
            interpretar_compatibilidade(
                [_STX, 0x31, *_txt("000475"), 0x30, *_txt("001234002600"), 0x0D, 0x35]
            )
            is None
        )
        assert interpretar_compatibilidade([]) is None


# ── resultado_do_frame (contrato do lerPeso) ─────────────────────────────
class TestResultadoDoFrame:
    def test_peso_vazia_e_o_resto(self):
        assert resultado_do_frame(FrameBalanca(EstadoFrame.peso, kg=0.475)) == (0.475, False)
        assert resultado_do_frame(FrameBalanca(EstadoFrame.zero)) == (None, True)
        assert resultado_do_frame(FrameBalanca(EstadoFrame.instavel)) == (None, False)
        assert resultado_do_frame(None) == (None, False)

    def test_negativo_conta_como_vazia_retorno_a_zero_com_tara(self):
        assert resultado_do_frame(FrameBalanca(EstadoFrame.negativo, kg=0.35)) == (None, True)


# ── PedidoUrano (byte de pedido 05h/04h) ─────────────────────────────────
class TestPedidoUrano:
    def test_comeca_em_enq_e_so_tenta_eot_onde_for_permitido(self):
        ped = PedidoUrano()
        assert ped.primeiro == PedidoUrano.ENQ
        assert ped.deve_tentar_eot(permitido=True) is True
        assert ped.deve_tentar_eot(permitido=False) is False

    def test_lembra_o_byte_que_trouxe_frame_e_para_de_alternar(self):
        ped = PedidoUrano()
        ped.registrar(respondeu_com=0x04, silencio=False)
        assert ped.primeiro == PedidoUrano.EOT
        assert ped.deve_tentar_eot(permitido=True) is False

    def test_3_silencios_seguidos_esquecem_o_byte(self):
        ped = PedidoUrano()
        ped.registrar(respondeu_com=0x04, silencio=False)
        ped.registrar(silencio=True)
        ped.registrar(silencio=True)
        # Chegou algo (não reconhecido): a balança ouviu, zera a contagem.
        ped.registrar(silencio=False)
        ped.registrar(silencio=True)
        ped.registrar(silencio=True)
        assert ped.lembrado == PedidoUrano.EOT
        ped.registrar(silencio=True)
        assert ped.lembrado is None
        assert ped.primeiro == PedidoUrano.ENQ


# ── OrdemAuto ────────────────────────────────────────────────────────────
class TestOrdemAuto:
    def test_do_mais_especifico_ao_mais_generico(self):
        assert OrdemAuto().ordem == ["urano", "toledo", "filizola", "elgin", "generico"]

    def test_quem_reconheceu_vai_na_frente_sem_repetir(self):
        o = OrdemAuto()
        o.registrar("toledo")
        assert o.ordem == ["toledo", "urano", "filizola", "elgin", "generico"]

    def test_3_ticks_sem_reconhecer_soltam_a_preferida(self):
        o = OrdemAuto()
        o.registrar("toledo")
        o.registrar(None)
        o.registrar(None)
        assert o.preferida == "toledo"
        o.registrar(None)
        assert o.preferida is None


# ── diagnóstico ──────────────────────────────────────────────────────────
class TestDiagnostico:
    def test_controles_por_nome_esc_e_can_nao_viram_o_mesmo_ponto(self):
        d = descrever_bytes(_urano12(trailer=_CAN))
        assert d.texto.startswith("<ESC>T1<ESC>A13")
        assert "<ESC>B * PESO:    475 g<CAN>E<CAN>P01" in d.texto
        assert descrever_bytes(_stx_etx("01")).hex == "02 30 31 03"

    def test_mostra_so_os_ultimos_bytes(self):
        d = descrever_bytes([0x41] * 200, max=96)
        assert len(d.texto) == 96

    def test_rotulos_filizola_nao_promete_mais_urano(self):
        assert rotulo_protocolo("filizola") == "Filizola"
        assert "Urano" in rotulo_protocolo("urano")
        assert rotulo_protocolo("xyz") == "xyz"

    def test_descrever_frame(self):
        assert descrever_frame(FrameBalanca(EstadoFrame.peso, kg=0.475)) == "0,475 kg"
        assert descrever_frame(FrameBalanca(EstadoFrame.zero)) == "prato vazio"


# ── armadilhas do porte Dart → Python (fora do teste do Dart) ────────────
class TestArmadilhasDoPorte:
    @pytest.mark.parametrize(
        ("v", "esperado"),
        [
            # A metade afasta do zero como o roundToDouble do Dart; o round()
            # do Python daria 0,000 / 0,002 / −0,000 (metade para o par).
            (0.0005, 0.001),
            (0.0025, 0.003),
            (-0.0005, -0.001),
            (-0.0025, -0.003),
            (0.0015, 0.002),
            (1.2344, 1.234),
            (0.475, 0.475),
            (0.0, 0.0),
        ],
    )
    def test_arredondar_gramas_afasta_a_metade_do_zero(self, v, esperado):
        assert arredondar_gramas(v) == esperado

    def test_arredondamento_nao_cai_no_floor_mais_meio(self):
        # floor(0,49999999999999994 + 0,5) daria 1: a soma vira 1,0 no double.
        quase_meio = 0.49999999999999994
        assert quase_meio + 0.5 == 1.0
        assert p._arredondar_dart(quase_meio) == 0.0
        assert p._arredondar_dart(2.5) == 3.0
        assert p._arredondar_dart(-2.5) == -3.0
        assert p._arredondar_dart(3.5) == 4.0

    def test_to_fixed_3_empata_longe_do_zero_como_o_dart(self):
        # 0,0625 é exato em binário: format() do Python dá "0.062".
        assert f"{0.0625:.3f}" == "0.062"
        assert descrever_frame(FrameBalanca(EstadoFrame.peso, kg=0.0625)) == "0,063 kg"

    def test_digitos_e_fronteiras_so_ascii(self):
        # "٣" (dígito árabe) não está no latin-1, mas um \d sem re.ASCII o
        # aceitaria se o texto viesse de outra fonte: o padrão é ASCII.
        assert p._RE_DECIMAL.flags & p.re.ASCII
        assert p._RE_TOKEN_PESO.search("٣") is None
        # \b do JavaScript: "²" (0xB2) não é caractere de palavra.
        assert _kg(interpretar_generico(_txt("x\xb21234\xb2\r\n"))) == 1.234

    def test_ponto_do_dart_nao_casa_cr_nem_lf(self):
        # Byte de estabilidade = CR: no Dart o "." não casa, sem frame
        # completo; fica só o cabeçalho? Também não: a marca usa o mesmo ".".
        b = _urano12(estab="\r")
        assert interpretar_urano(b) is None
        assert urano_completo(b) is False
        assert interpretar_urano(_urano12(estab="\n")) is None
        # Qualquer outro byte (até 0x85) é aceito, como no Dart.
        assert _estado(interpretar_urano(_urano12(estab="\x85"))) == EstadoFrame.peso

    def test_fim_de_texto_e_so_o_fim_nao_antes_do_lf_final(self):
        # Corpo "1234\n": o $ do Python casaria antes do \n; o do Dart não.
        assert p._RE_CORPO_NUMERICO.search("1234\n") is None
        assert p._RE_CORPO_STATUS.search("IIIII\n") is None
        assert p._RE_KG_FINAL.search("1.234kg\n") is None
        assert p._RE_CORPO_NUMERICO.search("1234") is not None

    def test_s_do_javascript_inclui_nbsp_e_nao_inclui_separadores(self):
        # "2.5<NBSP>g": o \s do Dart casa o NBSP, a unidade vale → 0,0025 kg,
        # arredondado longe do zero = 0,003 (o \s do Python com re.ASCII não
        # casaria o NBSP: sem unidade, decimal = 2,5 kg).
        assert _kg(interpretar_elgin(_txt("2.5\xa0g\r\n"))) == 0.003
        # 0x1C não é espaço para o Dart: sem unidade, decimal = kg (o \s do
        # Python sem re.ASCII casaria e daria 0,003).
        assert _kg(interpretar_elgin(_txt("2.5\x1cg\r\n"))) == 2.5

    def test_trim_do_dart_nao_e_o_strip_do_python(self):
        assert p._trim_dart("\x85 1 \xa0") == "1"
        assert p._trim_dart("\x1c1\x1f") == "\x1c1\x1f"
        assert "\x1c1\x1f".strip() == "1"
        # Linha sem terminador com 0x1C na frente: no Dart não sai no trim(),
        # então a linha não começa com "US" e é lida (o strip() a pularia).
        assert _kg(interpretar_elgin(_txt("\x1cUS 0.500"))) == 0.5

    def test_bytes_viram_texto_em_latin_1(self):
        # 0xB5 (µ) e 0xE9 (é) não são UTF-8 válido sozinhos: nada de exceção.
        assert interpretar_generico(bytes([0xB5, 0xE9, *_txt(" 1.234kg\r\n")])) is not None
        assert descrever_bytes(bytes([0xE9])).texto == "."

    def test_double_try_parse_como_o_dart(self):
        assert p._double_try_parse("1.5") == 1.5
        assert p._double_try_parse(".000") == 0.0
        assert p._double_try_parse(" 2 ") == 2.0
        for ruim in ("1_000", "inf", "nan", "", "1,5", "abc"):
            assert p._double_try_parse(ruim) is None

    def test_ultimo_do_all_matches(self):
        # Três frames: vale o último completo (finditer, não search).
        f = interpretar_urano([*_urano12(peso="   100"), *_urano12(peso="   200"), *_urano12()])
        assert _kg(f) == 0.475

    def test_aceita_bytes_bytearray_e_lista(self):
        for b in (bytes(_stx_etx("01234")), bytearray(_stx_etx("01234")), _stx_etx("01234")):
            assert _kg(interpretar_toledo(b)) == 1.234
