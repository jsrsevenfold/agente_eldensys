"""Espelho: a memória do leitor acompanha as digitais vivas da loja.

As regras que protegem o PDV offline no mesmo PC: só lápide sai; id
desconhecido só sai com memória cheia ou leitor/loja trocados; sem rede nada
é apagado; versão igual não baixa nada.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.biometria import espelho as espelho_mod  # noqa: E402
from agent.biometria.erros import ErroLeitor  # noqa: E402
from agent.biometria.espelho import Espelho, ler_estado  # noqa: E402
from agent.biometria.leitor import Leitor  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.remote_client import TransportError  # noqa: E402
from fake_idbio import ServidorFalso, SdkFalso, tpl  # noqa: E402

U1, U2, C1, C2, D, X, B = 11, 12, 21, 22, 31, 77, 88


def esperar(cond, timeout=3.0):
    fim = time.monotonic() + timeout
    while time.monotonic() < fim:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


@pytest.fixture(autouse=True)
def sem_pausa(monkeypatch):
    monkeypatch.setattr(espelho_mod, "PAUSA_ENTRE_LOTES_S", 0)


@pytest.fixture
def mundo(tmp_path):
    fake = SdkFalso()
    servidor = ServidorFalso()
    estado = {
        "cfg": AgentConfig(
            server_url="https://api.eldensys.com.br/api",
            server_tenant="loja_centro",
            server_token="esa_" + "x" * 43,
            instance_id="inst-1",
        )
    }
    leitor = Leitor(lambda: fake, ocioso_s=0.05)
    esp = Espelho(
        leitor=leitor,
        cliente_fn=lambda cfg: servidor,
        load_config_fn=lambda: estado["cfg"],
        caminho_estado=tmp_path / "biometria.json",
    )
    yield fake, servidor, esp, estado, tmp_path / "biometria.json"
    esp.parar()
    leitor.parar()


def gravar_estado_antigo(caminho: Path, **campos) -> None:
    base = {
        "versao": "velha",
        "tenant": "loja_centro",
        "api": "https://api.eldensys.com.br/api",
        "serie": "SN-0001",
        "completo": True,
        "faltando": 0,
        "sincronizado_em": None,
    }
    base.update(campos)
    caminho.write_text(json.dumps(base), encoding="utf-8")


def test_primeira_carga_grava_usuarios_e_clientes(mundo):
    fake, servidor, esp, _, caminho = mundo
    servidor.vivas({U1: tpl(1), U2: tpl(2)}, {C1: tpl(3)})
    esp.sincronizar_agora()
    assert fake.memoria == {U1: tpl(1), U2: tpl(2), C1: tpl(3)}
    st = esp.estado()
    assert (st["versao"], st["completo"], st["faltando"]) == (servidor.versao, True, 0)
    assert st["sincronizado_em"]
    # Carga de fundo devolve o leitor no fim (o PDV pode precisar dele).
    assert esperar(lambda: not fake.iniciado)


def test_carga_anota_o_leitor_pro_conectado(mundo):
    """``GetTemplateIDs`` respondeu: só o iDBio Pro responde (o comum dá -16).
    Sem isto o status e o heartbeat ficavam com ``pro`` desconhecido."""
    fake, servidor, esp, _, _ = mundo
    servidor.vivas({U1: tpl(1)})
    esp.sincronizar_agora()
    f = esp._leitor.fatos()
    assert (f.conectado, f.pro, f.digitais) == (True, True, 1)


def test_so_as_lapides_saem_e_o_desconhecido_fica(mundo):
    """X pode ser uma digital que o PDV cadastrou sem rede: não é apagada."""
    fake, servidor, esp, _, _ = mundo
    fake.memoria.update({U1: tpl(1), B: tpl(9), X: tpl(8)})
    servidor.vivas({U1: tpl(1)})
    servidor.removidos = [B]
    esp.sincronizar_agora()
    assert set(fake.memoria) == {U1, X}


def test_versao_igual_nao_baixa_nada_nem_abre_o_leitor(mundo):
    fake, servidor, esp, _, _ = mundo
    servidor.vivas({U1: tpl(1)})
    esp.sincronizar_agora()
    assert esperar(lambda: not fake.iniciado)
    fake.chamadas.clear()
    servidor.chamadas.clear()
    esp.sincronizar_agora()
    assert servidor.chamadas == [("digitais", servidor.versao)]
    assert fake.chamadas == []


def test_sem_rede_nada_e_apagado(mundo):
    fake, servidor, esp, _, caminho = mundo
    gravar_estado_antigo(caminho, tenant="outra_loja")  # até com a loja trocada
    fake.memoria.update({B: tpl(9), X: tpl(8)})
    servidor.removidos = [B]
    servidor.falhas["digitais"] = TransportError("sem rede")
    esp.sincronizar()
    assert esperar(lambda: not esp.estado()["em_andamento"])
    assert set(fake.memoria) == {B, X}
    assert fake.chamadas == []
    st = esp.estado()
    assert st["versao"] == "velha"
    assert "conexão" in st["erro"]


def test_falha_no_meio_da_busca_nao_marca_a_versao(mundo):
    fake, servidor, esp, _, _ = mundo
    servidor.vivas({U1: tpl(1)})
    servidor.falhas["buscar"] = TransportError("caiu")
    with pytest.raises(TransportError):
        esp.sincronizar_agora()
    assert esp.estado()["versao"] is None
    servidor.falhas.clear()
    esp.sincronizar_agora()
    assert fake.memoria == {U1: tpl(1)}


def test_memoria_cheia_tira_um_desconhecido_para_caber(mundo):
    fake, servidor, esp, _, _ = mundo
    fake.capacidade = 2
    fake.memoria.update({U1: tpl(1), X: tpl(8)})
    servidor.vivas({U1: tpl(1)}, {C1: tpl(3)})
    esp.sincronizar_agora()
    assert fake.memoria == {U1: tpl(1), C1: tpl(3)}
    assert esp.estado()["completo"] is True


def test_memoria_cheia_sem_desconhecido_para_e_conta_o_que_faltou(mundo):
    fake, servidor, esp, _, _ = mundo
    fake.capacidade = 1
    servidor.vivas({U1: tpl(1)}, {C1: tpl(3), C2: tpl(4)})
    esp.sincronizar_agora()
    assert fake.memoria == {U1: tpl(1)}
    st = esp.estado()
    assert (st["completo"], st["faltando"]) == (False, 2)
    # A versão é marcada: não fica tentando de novo a cada poll até mudar algo.
    assert st["versao"] == servidor.versao
    assert "cheia" in st["erro"]


@pytest.mark.parametrize("campo", [{"tenant": "outra_loja"}, {"serie": "SN-ANTIGA"}])
def test_leitor_de_outra_loja_ou_outro_aparelho_perde_os_desconhecidos(mundo, campo):
    fake, servidor, esp, _, caminho = mundo
    gravar_estado_antigo(caminho, **campo)
    fake.memoria.update({U1: tpl(1), X: tpl(8)})
    servidor.vivas({U1: tpl(1)})
    esp.sincronizar_agora()
    assert set(fake.memoria) == {U1}
    # A versão anterior era de outra loja: a lista veio inteira (sem ?versao=).
    if "tenant" in campo:
        assert servidor.chamadas[0] == ("digitais", None)


def test_carga_grande_vai_em_lotes_de_200(mundo):
    fake, servidor, esp, _, _ = mundo
    servidor.vivas({1000 + k: tpl(1000 + k) for k in range(450)})
    esp.sincronizar_agora()
    buscas = [c for c in servidor.chamadas if c[0] == "buscar"]
    assert [len(c[1]) for c in buscas] == [200, 200, 50]
    assert len(fake.memoria) == 450


def test_limite_da_loja_corta_a_lista_pela_prioridade(mundo):
    fake, servidor, esp, _, _ = mundo
    servidor.vivas({U1: tpl(1)}, {C1: tpl(3), C2: tpl(4)})
    servidor.limite = 2
    esp.sincronizar_agora()
    assert set(fake.memoria) == {U1, C1}  # usuários primeiro, cliente mais recente depois


def test_ids_invalidos_da_lista_sao_ignorados(mundo):
    fake, servidor, esp, _, _ = mundo
    servidor.vivas({U1: tpl(1)})
    servidor.usuarios += ["x", -5, 2**61, True, None]
    esp.sincronizar_agora()
    assert fake.memoria == {U1: tpl(1)}


def test_arquivo_de_estado_nao_tem_template_nem_ids(mundo):
    fake, servidor, esp, _, caminho = mundo
    u, c, x = 987654321012345, 876543210123456, 765432101234567
    servidor.vivas({u: tpl(1)}, {c: tpl(3)})
    fake.memoria[x] = tpl(8)
    esp.sincronizar_agora()
    texto = caminho.read_text(encoding="utf-8")
    for t in (tpl(1), tpl(3), tpl(8)):
        assert t not in texto
    for i in (u, c, x):
        assert str(i) not in texto
    assert set(json.loads(texto)) == {
        "versao",
        "tenant",
        "api",
        "serie",
        "completo",
        "faltando",
        "sincronizado_em",
        # Último agente visto no poll (o status usa enquanto não houver poll).
        "agent_id",
        "loja",
        "pareamento",
    }


def test_agente_do_poll_fica_no_estado_sem_atrapalhar_o_espelho(mundo, monkeypatch):
    fake, servidor, esp, estado, caminho = mundo
    gravacoes = []
    original = espelho_mod.gravar_estado

    def contar(c, e):
        gravacoes.append(e)
        original(c, e)

    monkeypatch.setattr(espelho_mod, "gravar_estado", contar)
    esp.guardar_agente("ag-1", "Loja Centro", "p1")
    esp.guardar_agente("ag-1", "Loja Centro", "p1")  # poll a cada poucos segundos: não regrava
    assert len(gravacoes) == 1
    servidor.vivas({U1: tpl(1)})
    esp.sincronizar_agora()  # a carga reescreve o arquivo sem perder o agente
    salvo = json.loads(caminho.read_text(encoding="utf-8"))
    assert (salvo["agent_id"], salvo["loja"], salvo["pareamento"]) == ("ag-1", "Loja Centro", "p1")
    assert salvo["versao"] == servidor.versao
    # Processo novo: vale o que está no disco, e só para o mesmo pareamento.
    novo = Espelho(
        leitor=esp._leitor,
        cliente_fn=lambda cfg: servidor,
        load_config_fn=lambda: estado["cfg"],
        caminho_estado=caminho,
    )
    assert novo.agente_salvo("p1") == ("ag-1", "Loja Centro")
    assert novo.agente_salvo("p2") is None
    assert novo.estado()["versao"] == servidor.versao


def test_estado_corrompido_vale_como_vazio(tmp_path):
    caminho = tmp_path / "biometria.json"
    for conteudo in ("{ quebrado", "[1,2]", '{"versao": 3, "faltando": true}'):
        caminho.write_text(conteudo, encoding="utf-8")
        e = ler_estado(caminho)
        assert e.versao is None and e.faltando == 0


def test_ao_poll_sincroniza_so_quando_a_versao_muda(mundo):
    fake, servidor, esp, _, _ = mundo
    servidor.vivas({U1: tpl(1)})
    esp.ao_poll(servidor.versao)
    assert esperar(lambda: esp.estado()["versao"] == servidor.versao and not esp.estado()["em_andamento"])
    servidor.chamadas.clear()
    esp.ao_poll(servidor.versao)
    time.sleep(0.05)
    assert servidor.chamadas == []
    servidor.vivas({U2: tpl(2)})
    servidor.versao = "2:2026-11-24T11:00:00+00:00"
    esp.ao_poll(servidor.versao)
    assert esperar(lambda: U2 in fake.memoria)


def test_falha_espera_antes_de_tentar_de_novo(mundo):
    fake, servidor, esp, _, _ = mundo
    servidor.falhas["digitais"] = TransportError("sem rede")
    esp.ao_poll("1")
    assert esperar(lambda: not esp.estado()["em_andamento"])
    servidor.chamadas.clear()
    esp.ao_poll("1")  # dentro da espera: não martela o servidor
    time.sleep(0.05)
    assert servidor.chamadas == []


def test_sincronizar_em_segundo_plano_mostra_andamento(mundo):
    fake, servidor, esp, _, _ = mundo
    servidor.vivas({1000 + k: tpl(1000 + k) for k in range(30)})
    resp = esp.sincronizar()
    assert resp["em_andamento"] is True
    assert esperar(lambda: not esp.estado()["em_andamento"])
    assert len(fake.memoria) == 30
    assert esp.estado()["progresso"] is None


# ── dentro de uma identificação ─────────────────────────────────────────
def test_garantir_grava_o_alvo_e_tira_o_cliente_menos_prioritario(mundo):
    fake, servidor, esp, _, _ = mundo
    servidor.vivas({U1: tpl(1)}, {C1: tpl(3), C2: tpl(4)})
    esp.sincronizar_agora()
    fake.capacidade = 3
    alvo = 41
    esp._leitor.executar(
        lambda sdk, ctx: esp.garantir(sdk, [(alvo, tpl(41))]), usuario=True, nome="identificar"
    )
    assert set(fake.memoria) == {U1, C1, alvo}  # C2 (menos prioritário) saiu
    assert esp.estado()["completo"] is False


def test_garantir_prefere_tirar_o_desconhecido(mundo):
    fake, servidor, esp, _, _ = mundo
    servidor.vivas({U1: tpl(1)}, {C1: tpl(3)})
    esp.sincronizar_agora()
    fake.memoria[X] = tpl(8)
    fake.capacidade = 3
    esp._leitor.executar(
        lambda sdk, ctx: esp.garantir(sdk, [(41, tpl(41))]), usuario=True, nome="identificar"
    )
    assert set(fake.memoria) == {U1, C1, 41}


def test_garantir_sem_ninguem_para_tirar_e_memoria_cheia(mundo):
    fake, servidor, esp, _, _ = mundo
    fake.capacidade = 0
    with pytest.raises(ErroLeitor) as exc:
        esp._leitor.executar(
            lambda sdk, ctx: esp.garantir(sdk, [(41, tpl(41))]), usuario=True, nome="identificar"
        )
    assert exc.value.erro == "memoria_cheia"
