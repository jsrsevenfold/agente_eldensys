"""Thread dona da porta da balança: estratégias, fila, ociosidade e quedas.

Nada de COM de verdade: a balança é o ``TransporteFalso`` (roteiro de
respostas) e a abertura é o ``AbridorFalso``. Os tempos curtos do Dart
(250 ms do Urano, 700 ms do DETECTAR) são encurtados por ``monkeypatch``
quando o teste não depende deles. No fim, um teste de fumaça do ``porta.py``
com o ``loop://`` do próprio pyserial e a abertura de uma COM que não existe.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

import pytest
import serial

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.balanca import erros  # noqa: E402
from agent.balanca import leitor as leitor_mod  # noqa: E402
from agent.balanca import porta as porta_mod  # noqa: E402
from agent.balanca.erros import ErroBalanca  # noqa: E402
from agent.balanca.leitor import LeitorBalanca  # noqa: E402
from agent.balanca.protocolos import EstadoFrame, PedidoUrano  # noqa: E402
from fake_balanca import (  # noqa: E402
    ENQ,
    EOT,
    ERRO_DISPOSITIVO,
    ERRO_EM_USO,
    ERRO_EM_USO_EN,
    ERRO_INEXISTENTE,
    ERRO_INEXISTENTE_EN,
    AbridorFalso,
    TransporteFalso,
    balanca_elgin,
    balanca_lixo,
    balanca_muda,
    balanca_toledo,
    balanca_urano_enq,
    balanca_urano_eot,
    stx_etx,
)


@pytest.fixture
def criar():
    """Fábrica de leitores; todos param no fim do teste (soltam a thread)."""
    feitos: list[LeitorBalanca] = []

    def _criar(abridor, ocioso=5.0):
        lt = LeitorBalanca(abridor, ocioso_s=ocioso)
        feitos.append(lt)
        return lt

    yield _criar
    for lt in feitos:
        lt.parar()


@pytest.fixture
def rapido(monkeypatch):
    """Encurta os prazos do Dart onde o teste não mede esses tempos."""
    monkeypatch.setattr(leitor_mod, "CURTO_URANO_S", 0.05)
    monkeypatch.setattr(leitor_mod, "PRAZO_DETECTAR_S", 0.15)


def esperar(cond, timeout=3.0):
    fim = time.monotonic() + timeout
    while time.monotonic() < fim:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def ler(lt, protocolo="toledo", porta="COM3", baud=9600, timeout_s=0.5):
    return lt.ler(porta=porta, baud=baud, protocolo=protocolo, timeout_s=timeout_s)


# ── estratégias ──────────────────────────────────────────────────────────
def test_toledo_pede_com_enq_e_termina_no_fim_do_frame(criar):
    ab = balanca_toledo()
    lt = criar(ab)
    inicio = time.monotonic()
    r = ler(lt, timeout_s=1.0)
    gasto = time.monotonic() - inicio
    assert r.frame.estado == EstadoFrame.peso and r.frame.kg == 1.234
    assert (r.protocolo, r.lido_como) == ("toledo", "toledo")
    assert r.dados == stx_etx("01234")
    assert ab.ultimo.escritos == [ENQ]
    # ETX + 60 ms de silêncio encerram a leitura muito antes do prazo.
    assert gasto < 0.6
    assert lt.aberta and lt.estado_porta() == {"aberta": True, "erro": None}


def test_resposta_em_pedacos_e_lida_inteira(criar):
    ab = AbridorFalso(
        lambda p, b: TransporteFalso(
            p, b, respostas={ENQ: [(0.01, b"\x02012"), (0.04, b"34\x03")]}
        )
    )
    r = ler(criar(ab))
    assert r.frame.kg == 1.234


def test_urano_explicito_tenta_eot_e_lembra_o_byte(criar, monkeypatch):
    monkeypatch.setattr(leitor_mod, "CURTO_URANO_S", 0.05)
    ab = balanca_urano_eot()
    lt = criar(ab)
    r = ler(lt, "urano")
    assert r.frame.kg == 0.475 and r.lido_como == "urano"
    # ENQ primeiro; sem resposta Urano no prazo curto, o EOT de reserva.
    assert ab.ultimo.escritos == [ENQ, EOT]
    assert lt._pedido_urano.lembrado == PedidoUrano.EOT
    ab.ultimo.escritos.clear()
    assert ler(lt, "urano").frame.kg == 0.475
    assert ab.ultimo.escritos == [EOT]  # lembrado: pede direto com ele


def test_urano_que_responde_ao_enq_nao_recebe_eot(criar):
    ab = balanca_urano_enq()
    lt = criar(ab)
    r = ler(lt, "urano")
    assert r.frame.kg == 0.475
    assert ab.ultimo.escritos == [ENQ]
    assert lt._pedido_urano.lembrado == PedidoUrano.ENQ


def test_fecha_por_ociosidade_guardando_o_byte_do_urano_e_a_ordem(criar, monkeypatch):
    """Divergência deliberada do Dart: fechar parado não esquece nada."""
    monkeypatch.setattr(leitor_mod, "CURTO_URANO_S", 0.05)
    ab = balanca_urano_eot()
    lt = criar(ab, ocioso=0.1)
    assert ler(lt, "urano").frame is not None
    assert esperar(lambda: not lt.aberta)
    assert ab.transportes[0].fechado
    assert lt.ultima_leitura() is not None  # o status continua mostrando
    assert ler(lt, "urano").frame.kg == 0.475
    assert len(ab.transportes) == 2  # reabriu
    assert ab.transportes[1].escritos == [EOT]  # sem pagar o ENQ de novo


def test_auto_vai_do_urano_ao_toledo_e_gruda_no_que_reconheceu(criar):
    ab = balanca_toledo()
    lt = criar(ab, ocioso=0.1)
    r = ler(lt, "auto")
    assert (r.protocolo, r.lido_como) == ("toledo", "toledo")
    assert r.frame.kg == 1.234
    # Urano pediu com ENQ (sem EOT no auto), recebeu Toledo, não reconheceu.
    assert ab.ultimo.escritos == [ENQ, ENQ]
    assert lt._pedido_urano.lembrado is None
    ab.ultimo.escritos.clear()
    assert ler(lt, "auto").frame.kg == 1.234
    assert ab.ultimo.escritos == [ENQ]  # Toledo na frente
    # A ordem sobrevive à porta fechada por ociosidade.
    assert esperar(lambda: not lt.aberta)
    assert ler(lt, "auto").frame.kg == 1.234
    assert ab.ultimo.escritos == [ENQ]


def test_auto_nao_manda_eot(criar, monkeypatch):
    monkeypatch.setattr(leitor_mod, "CURTO_URANO_S", 0.05)
    ab = balanca_urano_eot()
    lt = criar(ab)
    r = ler(lt, "auto", timeout_s=0.1)
    assert r.frame is None
    assert EOT not in ab.ultimo.escritos


def test_toledo_salvo_com_balanca_urano_le_pela_compatibilidade(criar, caplog):
    ab = balanca_urano_enq()
    lt = criar(ab)
    with caplog.at_level(logging.INFO, logger="eldensys.agent.balanca.leitor"):
        r = ler(lt, "toledo")
        ler(lt, "toledo")
    assert (r.protocolo, r.lido_como) == ("toledo", "urano")
    assert r.frame.estado == EstadoFrame.peso and r.frame.kg == 0.475
    avisos = [m for m in caplog.messages if "protocolo salvo 'toledo'" in m]
    assert len(avisos) == 1  # uma vez só


def test_sem_resposta_e_nao_reconhecido(criar):
    muda = criar(balanca_muda())
    r = ler(muda, timeout_s=0.1)
    assert (r.frame, r.dados) == (None, b"")
    lixo = criar(balanca_lixo())
    r = ler(lixo, timeout_s=0.3)
    assert r.frame is None and r.dados == b"\xff\xfe#@! \r\n"
    assert r.protocolo == "toledo" and r.lido_como is None


def test_log_da_balanca_muda_sai_uma_vez_por_minuto(criar, caplog):
    lt = criar(balanca_muda())
    with caplog.at_level(logging.INFO, logger="eldensys.agent.balanca.leitor"):
        for _ in range(3):
            ler(lt, timeout_s=0.05)
    assert len([m for m in caplog.messages if "sem resposta" in m]) == 1


def test_passivo_parado_comeca_com_purga(criar, monkeypatch):
    """Divergência deliberada: peso velho parado no driver não sai como atual."""
    monkeypatch.setattr(leitor_mod, "PURGA_PASSIVA_APOS_S", 0.1)
    ab = balanca_elgin(b"P001.000\r", intervalo=0.03)
    lt = criar(ab)
    assert ler(lt, "elgin", timeout_s=0.3).frame.kg == 1.0
    t = ab.ultimo
    # O produto saiu do prato, mas o agente ficou sem ler: 1 kg velho no
    # driver e a balança agora mandando zero.
    time.sleep(0.15)
    t.chegar(b"P001.000\rP001.000\r")
    t.mudar_fluxo(0.03, b"P000.000\r")
    purgas = t.purgas
    r = ler(lt, "elgin", timeout_s=0.3)
    assert t.purgas == purgas + 1
    assert r.frame.estado == EstadoFrame.zero
    # Leituras seguidas (a web pesando) não purgam: o buffer está fresco.
    ler(lt, "elgin", timeout_s=0.3)
    assert t.purgas == purgas + 1


def test_auto_purga_antes_de_cada_passivo(criar):
    """No auto, passivo depois de um pedido não herda a resposta atrasada dele
    (pedaço de frame Urano sem ESC leria "193" como peso): Urano, Toledo e
    Filizola purgam antes do pedido; Elgin e genérico, antes de escutar."""
    ab = balanca_muda()
    lt = criar(ab)
    r = ler(lt, "auto", timeout_s=0.03)
    assert r.frame is None and r.protocolo == "urano"
    assert ab.ultimo.purgas == 5
    assert ab.ultimo.escritos == [ENQ, ENQ, ENQ]


# ── porta: troca, queda, erros ───────────────────────────────────────────
def test_trocar_porta_ou_velocidade_fecha_e_esquece(criar, monkeypatch):
    monkeypatch.setattr(leitor_mod, "CURTO_URANO_S", 0.05)
    ab = balanca_urano_eot()
    lt = criar(ab)
    ler(lt, "urano", porta="COM3")
    assert lt._pedido_urano.lembrado == PedidoUrano.EOT
    ler(lt, "urano", porta="COM4")
    assert ab.transportes[0].fechado
    assert ab.aberturas == [("COM3", 9600), ("COM4", 9600)]
    assert ab.transportes[1].escritos == [ENQ, EOT]  # outra balança: do zero
    ler(lt, "urano", porta="COM4", baud=4800)
    assert ab.transportes[1].fechado
    assert ab.aberturas[-1] == ("COM4", 4800)


def test_trocar_protocolo_zera_a_ordem_do_auto(criar):
    ab = balanca_toledo()
    lt = criar(ab)
    ler(lt, "auto")
    assert lt._ordem_auto.preferida == "toledo"
    ler(lt, "toledo")
    ler(lt, "auto")
    # Voltou ao auto: começou pelo Urano de novo (ENQ, ENQ).
    assert ab.ultimo.escritos[-2:] == [ENQ, ENQ]
    assert len(ab.transportes) == 1  # mesma porta: não reabriu


def test_porta_que_cai_na_leitura_fecha_e_a_proxima_reabre(criar):
    ab = balanca_toledo()
    lt = criar(ab)
    assert ler(lt).frame.kg == 1.234
    ab.ultimo.puxar_cabo()
    with pytest.raises(ErroBalanca) as e:
        ler(lt)
    assert (e.value.erro, e.value.status) == (erros.PORTA_FALHOU, 503)
    assert "caiu" in e.value.mensagem and "COM3" in e.value.mensagem
    assert "ClearCommError" in e.value.detail or "WriteFile" in e.value.detail
    assert ab.transportes[0].fechado
    assert lt.estado_porta() == {"aberta": False, "erro": e.value.mensagem}
    assert lt.ultima_leitura() is None
    # Cabo de volta: a próxima leitura abre de novo e o erro some.
    assert ler(lt).frame.kg == 1.234
    assert len(ab.transportes) == 2
    assert lt.estado_porta() == {"aberta": True, "erro": None}


@pytest.mark.parametrize(
    ("texto", "codigo"),
    [
        (ERRO_EM_USO, erros.PORTA_EM_USO),
        (ERRO_EM_USO_EN, erros.PORTA_EM_USO),
        (ERRO_INEXISTENTE, erros.PORTA_INEXISTENTE),
        (ERRO_INEXISTENTE_EN, erros.PORTA_INEXISTENTE),
        (ERRO_DISPOSITIVO, erros.PORTA_FALHOU),
    ],
)
def test_erros_de_abertura_do_windows_sao_traduzidos(criar, texto, codigo):
    ab = balanca_toledo()
    ab.erros.append(serial.SerialException(texto))
    lt = criar(ab)
    with pytest.raises(ErroBalanca) as e:
        ler(lt)
    assert (e.value.erro, e.value.status) == (codigo, 503)
    assert "COM3" in e.value.mensagem
    assert e.value.detail == texto
    assert lt.estado_porta() == {"aberta": False, "erro": e.value.mensagem}
    # Liberou a porta no outro programa: a próxima abre.
    assert ler(lt).frame.kg == 1.234
    assert lt.estado_porta()["erro"] is None


def test_porta_em_uso_cita_o_pdv_offline():
    e = porta_mod.traduzir_erro_abertura("COM5", serial.SerialException(ERRO_EM_USO))
    assert e.mensagem == (
        "A porta COM5 está em uso por outro programa (ex.: o PDV offline EldenSys, "
        "Acesso Toledo). Feche o outro programa ou desligue a balança nele."
    )
    assert porta_mod.traduzir_erro_abertura("COM5", PermissionError(13, "x")).erro == "porta_em_uso"
    assert (
        porta_mod.traduzir_erro_abertura("COM5", FileNotFoundError(2, "x")).erro
        == "porta_inexistente"
    )
    assert porta_mod.traduzir_erro_abertura("COM5", ValueError("baud")).erro == "porta_falhou"


# ── DETECTAR ─────────────────────────────────────────────────────────────
def test_detectar_toledo_com_dois_reconhecimentos(criar, rapido):
    ab = balanca_toledo()
    lt = criar(ab)
    d = lt.detectar(porta="COM3", baud=9600)
    assert d.protocolo == "toledo"
    assert d.frame.kg == 1.234
    assert d.dados == stx_etx("01234")
    # Urano: ENQ, sem resposta Urano no prazo curto → EOT; depois 2 ENQ.
    assert ab.ultimo.escritos == [ENQ, EOT, ENQ, ENQ]


def test_detectar_urano_que_so_responde_eot_e_a_leitura_herda_o_byte(criar, rapido):
    ab = balanca_urano_eot()
    lt = criar(ab)
    d = lt.detectar(porta="COM3", baud=9600)
    assert d.protocolo == "urano" and d.frame.kg == 0.475
    ab.ultimo.escritos.clear()
    assert ler(lt, "urano").frame.kg == 0.475
    assert ab.ultimo.escritos == [EOT]


def test_detectar_zera_o_byte_lembrado_de_outra_balanca(criar, rapido):
    ab = balanca_toledo()
    lt = criar(ab)
    lt._pedido_urano.registrar(respondeu_com=EOT, silencio=False)
    lt._ordem_auto.registrar("elgin")
    lt.detectar(porta="COM3", baud=9600)
    assert ab.ultimo.escritos[0] == ENQ
    assert lt._ordem_auto.preferida is None


def test_detectar_balanca_muda_e_lixo(criar, rapido):
    d = criar(balanca_muda()).detectar(porta="COM3", baud=9600)
    assert (d.protocolo, d.frame, d.dados) == (None, None, b"")
    d = criar(balanca_lixo()).detectar(porta="COM3", baud=9600)
    assert d.protocolo is None and d.dados == b"\xff\xfe#@! \r\n"


def test_detectar_elgin_pelo_stream(criar, rapido):
    d = criar(balanca_elgin()).detectar(porta="COM3", baud=9600)
    assert d.protocolo == "elgin" and d.frame.kg == 0.5


def test_detectar_cancelado_para_entre_as_tentativas(criar, rapido):
    ab = balanca_muda()
    lt = criar(ab)
    tok = threading.Event()
    tok.set()
    with pytest.raises(ErroBalanca) as e:
        lt.detectar(porta="COM3", baud=9600, cancelamento=tok)
    assert e.value.erro == erros.CANCELADO
    tok2 = threading.Event()
    threading.Timer(0.1, tok2.set).start()
    inicio = time.monotonic()
    with pytest.raises(ErroBalanca):
        lt.detectar(porta="COM3", baud=9600, cancelamento=tok2)
    # Sem o cancelamento seriam 5 protocolos × 0,15 s; parou na tentativa seguinte.
    assert time.monotonic() - inicio < 0.5


def test_detectar_com_porta_que_cai(criar, rapido):
    ab = balanca_toledo()
    lt = criar(ab)
    ler(lt)
    ab.ultimo.puxar_cabo()
    with pytest.raises(ErroBalanca) as e:
        lt.detectar(porta="COM3", baud=9600)
    assert e.value.erro == erros.PORTA_FALHOU
    assert not lt.aberta


# ── fila, vigia, liberar, parar ──────────────────────────────────────────
def test_fila_serializa_pedidos_simultaneos(criar):
    ab = AbridorFalso(
        lambda p, b: TransporteFalso(p, b, respostas={ENQ: stx_etx("01234")}, atraso=0.02)
    )
    lt = criar(ab)
    resultados: list = []

    def pesar():
        resultados.append(ler(lt).frame.kg)

    threads = [threading.Thread(target=pesar) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert resultados == [1.234] * 5
    assert ab.ultimo.escritos == [ENQ] * 5
    assert len(ab.transportes) == 1
    # Só a thread do leitor tocou na porta.
    assert len(ab.ultimo.threads) == 1


def test_fila_cheia_responde_ocupada(criar):
    lt = criar(balanca_toledo())
    solta = threading.Event()
    tarefas = [lt._enviar(lambda: solta.wait(5), "presa", 10.0)]
    for _ in range(leitor_mod.FILA_MAX - 1):
        tarefas.append(lt._enviar(lambda: None, "na fila", 10.0))
    with pytest.raises(ErroBalanca) as e:
        ler(lt)
    assert (e.value.erro, e.value.status) == (erros.OCUPADA, 409)
    solta.set()
    for t in tarefas:
        t.future.result(5)
    assert ler(lt).frame.kg == 1.234


def test_vigia_responde_sem_resposta_quando_a_thread_trava(criar, monkeypatch):
    monkeypatch.setattr(leitor_mod, "FOLGA_VIGIA_S", 0.1)
    lt = criar(balanca_toledo())
    solta = threading.Event()
    with pytest.raises(ErroBalanca) as e:
        lt._executar(lambda: solta.wait(5), "presa", 0.05)
    assert (e.value.erro, e.value.status) == (erros.BALANCA_SEM_RESPOSTA, 503)
    solta.set()
    assert esperar(lambda: lt._atual is None)


def test_liberar_fecha_na_hora_e_guarda_a_memoria(criar, monkeypatch):
    monkeypatch.setattr(leitor_mod, "CURTO_URANO_S", 0.05)
    ab = balanca_urano_eot()
    lt = criar(ab)
    ler(lt, "urano")
    lt.liberar()
    assert not lt.aberta and ab.ultimo.fechado
    assert lt._pedido_urano.lembrado == PedidoUrano.EOT
    lt.liberar()  # sem porta aberta: nada a fazer, sem erro


def test_liberar_sem_uso_nao_cria_thread(criar):
    lt = criar(balanca_toledo())
    lt.liberar()
    assert lt._thread is None


def test_construir_nao_abre_nem_cria_thread():
    ab = balanca_toledo()
    lt = LeitorBalanca(ab)
    assert ab.aberturas == [] and lt._thread is None
    assert lt.estado_porta() == {"aberta": False, "erro": None}
    lt.parar()


def test_parar_fecha_a_porta_e_recusa_o_resto(criar):
    ab = balanca_toledo()
    lt = criar(ab)
    ler(lt)
    lt.parar()
    assert ab.ultimo.fechado
    assert not lt._thread.is_alive()
    with pytest.raises(ErroBalanca) as e:
        ler(lt)
    assert (e.value.erro, e.value.status) == (erros.AGENTE_ENCERRANDO, 503)


def test_parar_com_pedidos_na_fila_responde_encerrando(criar):
    lt = criar(balanca_toledo())
    solta = threading.Event()
    presa = lt._enviar(lambda: solta.wait(5), "presa", 10.0)
    fila = [lt._enviar(lambda: "nunca", "na fila", 10.0) for _ in range(3)]
    threading.Timer(0.05, solta.set).start()
    lt.parar()
    presa.future.result(5)
    for t in fila:
        with pytest.raises(ErroBalanca) as e:
            t.future.result(5)
        assert e.value.erro == erros.AGENTE_ENCERRANDO


# ── porta.py de verdade (pyserial) ───────────────────────────────────────
def test_fumaca_porta_serial_com_loop():
    p = porta_mod.abrir_porta("loop://", 9600)
    try:
        p.escrever(b"\x05")
        assert esperar(lambda: p._s.in_waiting > 0, 1.0)
        assert p.ler_disponivel() == b"\x05"
        assert p.ler_disponivel() == b""
        p.escrever(b"lixo")
        p.purgar()
        assert p.ler_disponivel() == b""
    finally:
        p.fechar()
    p.fechar()  # idempotente


def test_fumaca_leitor_com_loop_le_o_proprio_eco(criar):
    """loop:// devolve o ENQ: o Toledo não reconhece (é lixo de 1 byte)."""
    lt = criar(porta_mod.abrir_porta)
    r = lt.ler(porta="loop://", baud=9600, protocolo="toledo", timeout_s=0.15)
    assert r.frame is None and r.dados == b"\x05"


@pytest.mark.skipif(sys.platform != "win32", reason="mensagem do Windows")
def test_com_inexistente_de_verdade_vira_porta_inexistente(criar):
    lt = criar(porta_mod.abrir_porta)
    with pytest.raises(ErroBalanca) as e:
        lt.ler(porta="COM250", baud=9600, protocolo="toledo", timeout_s=0.1)
    assert e.value.erro == erros.PORTA_INEXISTENTE
    assert "FileNotFoundError" in e.value.detail


def test_listar_portas_pelo_numero(monkeypatch):
    class P:
        def __init__(self, device, description):
            self.device, self.description = device, description

    monkeypatch.setattr(
        porta_mod.serial.tools.list_ports,
        "comports",
        lambda: [P("COM10", "USB-SERIAL CH340 (COM10)"), P("COM2", None), P("COM3", "Prolific")],
    )
    assert porta_mod.listar_portas() == [
        {"porta": "COM2", "descricao": ""},
        {"porta": "COM3", "descricao": "Prolific"},
        {"porta": "COM10", "descricao": "USB-SERIAL CH340 (COM10)"},
    ]
