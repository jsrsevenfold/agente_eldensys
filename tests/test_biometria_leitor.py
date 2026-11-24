"""Thread dona do leitor: fila, ocupado, cancelar, ocioso, desconexão, travado.

O SDK é o ``SdkFalso``; o que se confere são as decisões que, erradas, prendem
o leitor (o PDV no mesmo PC não consegue usar) ou deixam a pessoa esperando
o dedo sem fim.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.biometria import leitor as leitor_mod  # noqa: E402
from agent.biometria.erros import (  # noqa: E402
    ErroLeitor,
    LeitorEmUso,
    LeitorOcupado,
    LeitorTravado,
    SdkIndisponivel,
)
from agent.biometria.leitor import Cancelamento, Leitor  # noqa: E402
from fake_idbio import BLOQUEAR, SdkFalso, tpl  # noqa: E402


def esperar(cond, timeout=3.0):
    fim = time.monotonic() + timeout
    while time.monotonic() < fim:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


@pytest.fixture
def fake():
    return SdkFalso()


@pytest.fixture
def leitor(fake):
    lt = Leitor(lambda: fake, ocioso_s=0.2)
    yield lt
    lt.parar()


def captura_identificar(sdk, ctx):
    return ctx.capturar(sdk.identificar)


# ── Init preguiçoso / fechar ────────────────────────────────────────────
def test_criar_nao_abre_o_leitor_nem_carrega_o_sdk():
    carregou = []
    lt = Leitor(lambda: carregou.append(1) or SdkFalso())
    assert lt.estado()["aberto"] is False
    assert carregou == []
    assert lt._thread is None
    lt.parar()


def test_init_uma_vez_e_fecha_depois_do_ocioso(leitor, fake):
    fake.dedo = tpl(1)
    fake.memoria[10] = tpl(1)
    assert leitor.executar(captura_identificar, usuario=True, nome="a", captura=True).device_id == 10
    assert leitor.executar(lambda sdk, ctx: sdk.ids(), usuario=True, nome="b") == [10]
    assert fake.inits == 1
    # Logo depois de uma operação do usuário o leitor continua aberto…
    assert leitor.aberto
    # …e é fechado sozinho depois do tempo ocioso (o PDV pode precisar dele).
    assert esperar(lambda: not leitor.aberto)
    assert fake.terminates == 1


def test_tarefa_de_fundo_devolve_o_leitor_na_hora(fake):
    lt = Leitor(lambda: fake, ocioso_s=30)
    try:
        lt.executar(lambda sdk, ctx: sdk.ids(), usuario=False, nome="espelho")
        assert esperar(lambda: fake.terminates == 1, timeout=1.0)
        assert not lt.aberto
    finally:
        lt.parar()


def test_fundo_logo_depois_do_usuario_respeita_a_janela_ociosa(fake):
    lt = Leitor(lambda: fake, ocioso_s=0.3)
    try:
        lt.executar(lambda sdk, ctx: sdk.ids(), usuario=True, nome="testar")
        lt.executar(lambda sdk, ctx: sdk.ids(), usuario=False, nome="espelho")
        assert lt.aberto and fake.terminates == 0  # cadastro em 4 passos não reabre
        assert esperar(lambda: not lt.aberto)
        assert fake.inits == 1
    finally:
        lt.parar()


# ── fila e ocupado ───────────────────────────────────────────────────────
def test_usuario_passa_na_frente_do_fundo(leitor):
    ordem = []
    solta = threading.Event()

    def fundo_lento(sdk, ctx):
        solta.wait(3)
        ordem.append("fundo1")

    f1 = leitor.enviar(fundo_lento, usuario=False, nome="espelho")
    assert esperar(lambda: leitor.estado()["operacao"] == "espelho")
    f2 = leitor.enviar(lambda s, c: ordem.append("fundo2"), usuario=False, nome="espelho")
    f3 = leitor.enviar(lambda s, c: ordem.append("usuario"), usuario=True, nome="identificar")
    solta.set()
    for f in (f1, f2, f3):
        f.result(3)
    assert ordem == ["fundo1", "usuario", "fundo2"]


def test_fundo_cede_quando_o_usuario_espera(leitor):
    feitos = []
    comecou = threading.Event()

    def fundo(sdk, ctx):
        comecou.set()
        for k in range(500):
            if feitos and ctx.ceder():
                break
            feitos.append(k)
            time.sleep(0.002)
        return len(feitos)

    f1 = leitor.enviar(fundo, usuario=False, nome="espelho")
    comecou.wait(2)
    f2 = leitor.enviar(lambda s, c: "ok", usuario=True, nome="testar")
    assert f2.result(3) == "ok"
    assert f1.result(3) < 500


def test_segunda_operacao_do_usuario_e_ocupado_com_pode_cancelar(leitor, fake):
    fake.identificacoes.append(BLOQUEAR)
    f = leitor.enviar(captura_identificar, usuario=True, nome="identificar", captura=True)
    assert fake.bloqueado.wait(2)
    with pytest.raises(LeitorOcupado) as exc:
        leitor.enviar(captura_identificar, usuario=True, nome="login", captura=True)
    assert exc.value.status == 409
    assert exc.value.corpo()["pode_cancelar"] is True
    assert leitor.estado()["ocupado"] is True
    assert leitor.cancelar() is True
    with pytest.raises(ErroLeitor) as e2:
        f.result(3)
    assert e2.value.codigo == -20
    assert leitor.estado()["ocupado"] is False


def test_ocupado_sem_captura_nao_pode_cancelar(leitor):
    solta = threading.Event()
    f = leitor.enviar(lambda s, c: solta.wait(3), usuario=True, nome="cadastro")
    assert esperar(lambda: leitor.estado()["operacao"] == "cadastro")
    with pytest.raises(LeitorOcupado) as exc:
        leitor.enviar(lambda s, c: None, usuario=True, nome="testar")
    assert exc.value.corpo()["pode_cancelar"] is False
    solta.set()
    f.result(3)


def test_tarefa_de_fundo_nunca_e_recusada_por_ocupado(leitor, fake):
    fake.identificacoes.append(BLOQUEAR)
    f = leitor.enviar(captura_identificar, usuario=True, nome="identificar", captura=True)
    assert fake.bloqueado.wait(2)
    fundo = leitor.enviar(lambda s, c: "fundo", usuario=False, nome="espelho")
    leitor.cancelar()
    with pytest.raises(ErroLeitor):
        f.result(3)
    assert fundo.result(3) == "fundo"


# ── cancelar ─────────────────────────────────────────────────────────────
def test_cancelar_de_outra_thread_chama_cancel_capture(leitor, fake):
    fake.identificacoes.append(BLOQUEAR)
    tok = Cancelamento()
    f = leitor.enviar(
        captura_identificar, usuario=True, nome="identificar", captura=True, cancelamento=tok
    )
    assert fake.bloqueado.wait(2)
    threading.Thread(target=tok.cancelar).start()
    with pytest.raises(ErroLeitor) as exc:
        f.result(3)
    assert exc.value.codigo == -20
    assert fake.cancelamentos >= 1


def test_cancelado_antes_de_comecar_nem_toca_no_leitor(leitor, fake):
    tok = Cancelamento()
    tok.cancelar()
    with pytest.raises(ErroLeitor) as exc:
        leitor.enviar(captura_identificar, usuario=True, nome="x", captura=True, cancelamento=tok)
    assert exc.value.codigo == -20
    assert fake.chamadas == []


def test_cancelado_na_fila_nao_chega_a_capturar(leitor, fake):
    solta = threading.Event()
    leitor.enviar(lambda s, c: solta.wait(3), usuario=False, nome="espelho")
    assert esperar(lambda: leitor.estado()["operacao"] == "espelho")
    f = leitor.enviar(captura_identificar, usuario=True, nome="identificar", captura=True)
    assert leitor.cancelar() is True
    solta.set()
    with pytest.raises(ErroLeitor) as exc:
        f.result(3)
    assert exc.value.codigo == -20
    assert "CaptureAndIdentify" not in fake.chamadas


def test_cancelar_sem_operacao_devolve_false(leitor):
    assert leitor.cancelar() is False


def test_cancel_capture_e_repetido_se_chegar_antes_da_espera(monkeypatch, leitor, fake):
    """O pedido pode chegar entre 'vai capturar' e 'esperando o dedo'."""
    monkeypatch.setattr(leitor_mod, "CANCEL_INTERVALO_S", 0.05)
    chegou = threading.Event()
    original = fake.identificar

    def identificar_atrasado():
        chegou.set()
        time.sleep(0.1)  # a captura ainda não está esperando quando o cancelar chega
        fake.identificacoes.insert(0, BLOQUEAR)
        return original()

    tok = Cancelamento()
    f = leitor.enviar(
        lambda sdk, ctx: ctx.capturar(identificar_atrasado),
        usuario=True,
        nome="identificar",
        captura=True,
        cancelamento=tok,
    )
    assert chegou.wait(2)
    tok.cancelar()
    with pytest.raises(ErroLeitor) as exc:
        f.result(3)
    assert exc.value.codigo == -20
    assert fake.cancelamentos >= 2


# ── desconexão / em uso / SDK ────────────────────────────────────────────
@pytest.mark.parametrize("codigo", [-2, -7, -8])
def test_leitor_arrancado_fecha_e_reabre_na_proxima(leitor, fake, codigo):
    # -8: o que a sessão aberta responde com o cabo puxado (fase 0).
    leitor.executar(lambda s, c: s.ids(), usuario=True, nome="a")
    fake.identificacoes.append(codigo)
    with pytest.raises(ErroLeitor) as exc:
        leitor.executar(captura_identificar, usuario=True, nome="b", captura=True)
    assert exc.value.codigo == codigo
    assert exc.value.status == 503
    assert not leitor.aberto
    assert fake.terminates == 1
    # O status e o heartbeat param de mostrar o leitor pronto (-2, -7 e -8).
    assert leitor.fatos().conectado is False
    leitor.executar(lambda s, c: s.ids(), usuario=True, nome="c")
    assert fake.inits == 2
    assert leitor.fatos().conectado is True


def test_sem_leitor_no_init_marca_desconectado(leitor, fake):
    fake.desconectar()
    with pytest.raises(ErroLeitor) as exc:
        leitor.executar(lambda s, c: s.ids(), usuario=True, nome="testar")
    assert exc.value.erro == "leitor_desconectado"
    assert leitor.estado()["conectado"] is False
    # Fase 0: o Init devolve -2 também com o leitor aberto por outro programa
    # (o PDV no mesmo PC) — a mensagem cita as duas causas.
    assert "cabo USB" in leitor.estado()["erro"]
    assert "outro programa" in leitor.estado()["erro"]


def test_init_ocupado_e_em_uso(leitor, fake):
    fake.init_erro = -19
    with pytest.raises(LeitorEmUso) as exc:
        leitor.executar(lambda s, c: s.ids(), usuario=True, nome="testar")
    assert exc.value.status == 503
    assert exc.value.erro == "leitor_em_uso"


def test_init_com_falha_de_comunicacao_fecha_e_marca_desconectado(leitor, fake):
    # -8 não é "outro programa" (este devolve -2): é a comunicação que falhou.
    fake.init_erro = -8
    with pytest.raises(ErroLeitor) as exc:
        leitor.executar(lambda s, c: s.ids(), usuario=True, nome="testar")
    assert exc.value.erro == "leitor_desconectado"
    assert not leitor.aberto
    assert leitor.fatos().conectado is False
    fake.init_erro = None
    assert leitor.executar(lambda s, c: s.ids(), usuario=True, nome="de novo") == []
    assert leitor.fatos().conectado is True


def test_sdk_ausente_vira_sdk_indisponivel_e_aparece_no_estado():
    def falha():
        raise SdkIndisponivel("DLL não encontrada")

    lt = Leitor(falha)
    try:
        with pytest.raises(SdkIndisponivel):
            lt.executar(lambda s, c: None, usuario=True, nome="testar")
        assert lt.sdk_estado() == {"carregado": False, "erro": "DLL não encontrada"}
    finally:
        lt.parar()


def test_nao_pro_fica_anotado(leitor, fake):
    fake.pro = False
    with pytest.raises(ErroLeitor) as exc:
        leitor.executar(captura_identificar, usuario=True, nome="x", captura=True)
    assert exc.value.erro == "leitor_nao_pro"
    assert leitor.fatos().pro is False


# ── vigia ────────────────────────────────────────────────────────────────
def test_vigia_marca_travado_e_recusa_ate_a_chamada_voltar(fake):
    agora = [1000.0]
    lt = Leitor(lambda: fake, relogio=lambda: agora[0])
    try:
        fake.identificacoes.append(BLOQUEAR)
        f = lt.enviar(captura_identificar, usuario=True, nome="identificar", captura=True)
        assert fake.bloqueado.wait(2)
        assert lt.estado()["travado"] is False
        agora[0] += leitor_mod.PRAZO_CAPTURA_S + 1
        assert lt.estado()["travado"] is True
        with pytest.raises(LeitorTravado):
            lt.enviar(lambda s, c: None, usuario=False, nome="espelho")
        # A chamada presa volta: o vigia solta e o leitor é fechado para reabrir.
        lt.cancelar()
        with pytest.raises(ErroLeitor):
            f.result(3)
        assert esperar(lambda: lt.estado()["travado"] is False)
        assert esperar(lambda: not lt.aberto)
        assert lt.executar(lambda s, c: "ok", usuario=True, nome="testar") == "ok"
    finally:
        lt.parar()


def test_executar_com_prazo_estourado_vira_travado(fake):
    lt = Leitor(lambda: fake)
    solta = threading.Event()
    try:
        with pytest.raises(LeitorTravado):
            lt.executar(lambda s, c: solta.wait(3), usuario=True, nome="lento", prazo_s=-29.9)
        assert lt.estado()["travado"] is True
    finally:
        solta.set()
        lt.parar()


# ── parar ────────────────────────────────────────────────────────────────
def test_parar_cancela_a_captura_fecha_o_leitor_e_recusa_novos(fake):
    lt = Leitor(lambda: fake, ocioso_s=60)
    fake.identificacoes.append(BLOQUEAR)
    f = lt.enviar(captura_identificar, usuario=True, nome="identificar", captura=True)
    assert fake.bloqueado.wait(2)
    lt.parar()
    with pytest.raises(ErroLeitor):
        f.result(3)
    assert not lt._thread.is_alive()
    assert fake.terminates >= 1 and not fake.iniciado
    with pytest.raises(Exception) as exc:
        lt.enviar(lambda s, c: None, usuario=True, nome="x")
    assert getattr(exc.value, "erro", "") == "biometria_desligada"
