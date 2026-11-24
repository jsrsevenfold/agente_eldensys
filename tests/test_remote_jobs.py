"""Ciclo do canal remoto: poll -> executa -> ack.

Nada de rede, disco de config ou impressora de verdade: o worker recebe os
tres por injecao. O que se verifica sao as decisoes que custam caro na loja:
agente nao pareado nao faz rede, etiqueta nao e impressa duas vezes, erro
vira aviso (ack) em vez de derrubar a thread.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import __version__, remote_jobs  # noqa: E402
from agent.config import AgentConfig  # noqa: E402
from agent.remote_client import (  # noqa: E402
    PairingRejected,
    RateLimited,
    StoreBlocked,
    TransportError,
)
from agent.remote_jobs import RemoteJobsWorker  # noqa: E402

CARGA = b"0101000000100129900030PICANHA\r\n01010000002000599000A\xc7UCAR\r\n"
PDF = b"%PDF-1.4 etiqueta"


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class FakeClient:
    """Dublê do RemoteClient: respostas de poll em fila; guarda os acks."""

    def __init__(self, *respostas):
        self.respostas = list(respostas)
        self.polls: list[tuple] = []
        self.acks: list[tuple] = []
        self.ack_erro: Exception | None = None

    def poll(self, version, hostname, capabilities):
        self.polls.append((version, hostname, capabilities))
        r = self.respostas.pop(0) if self.respostas else {"jobs": []}
        if isinstance(r, Exception):
            raise r
        return r

    def ack(self, job_id, status, detail="", result=None):
        if self.ack_erro:
            raise self.ack_erro
        self.acks.append((job_id, status, detail, result))
        return {}


def montar(tmp_path, *respostas, **cfg_kw):
    """Worker pareado, com pasta da balanca e impressora de etiquetas."""
    pasta = tmp_path / "balanca"
    pasta.mkdir(exist_ok=True)
    base = {
        "server_url": "https://api.eldensys.com.br/api",
        "server_tenant": "loja",
        "server_token": "esa_" + "x" * 40,
        "instance_id": "inst-1",
        "scale_dir": str(pasta),
        "remote_label_printer": "Argox OS-214",
    }
    base.update(cfg_kw)
    estado = {"cfg": AgentConfig(**base)}
    client = FakeClient(*respostas)
    impressos: list[dict] = []
    worker = RemoteJobsWorker(
        load_config_fn=lambda: estado["cfg"],
        client_factory=lambda cfg: client,
        print_pdf_fn=lambda **kw: impressos.append(kw),
        list_printers_fn=lambda: [{"name": "Argox OS-214"}, {"name": "EPSON TM-T20"}],
    )
    return worker, client, impressos, pasta, estado


def job_balanca(job_id="j1", arquivos=None):
    arquivos = arquivos or [("txitens.txt", CARGA)]
    return {
        "id": job_id,
        "kind": "scale_file",
        "params": {"model": "toledo"},
        "files": [{"filename": n, "content_base64": b64(d)} for n, d in arquivos],
    }


def job_etiqueta(job_id="e1", **params):
    return {
        "id": job_id,
        "kind": "label_pdf",
        "params": {"copies": 1, "render_mode": "label", **params},
        "files": [{"filename": "etiquetas.pdf", "content_base64": b64(PDF)}],
    }


# ── pareamento / rede ────────────────────────────────────────────────────
def test_agente_nao_pareado_nao_faz_rede(tmp_path):
    """PC que so imprime pelo navegador continua sem falar com o servidor."""
    chamadas = []
    worker = RemoteJobsWorker(
        load_config_fn=lambda: AgentConfig(),
        client_factory=lambda cfg: chamadas.append(cfg),
    )
    espera = worker.run_once()
    assert chamadas == []
    assert espera == remote_jobs.UNPAIRED_WAIT
    assert worker.get_status().state == "unpaired"


def test_heartbeat_reporta_versao_e_capacidades(tmp_path):
    worker, client, _, pasta, _ = montar(tmp_path, {"jobs": [], "store_name": "Loja Centro"})
    worker.run_once()
    versao, hostname, caps = client.polls[0]
    assert versao == __version__
    assert hostname
    assert caps == {
        "scale": {"configured": True, "ok": True, "folder": str(pasta)},
        "label": {"configured": True, "ok": True, "printer": "Argox OS-214"},
    }
    st = worker.get_status()
    assert (st.state, st.store_name) == ("online", "Loja Centro")
    assert st.last_ok is not None


BIO = {
    "configured": True,
    "ok": True,
    "sdk": True,
    "pro": True,
    "modelo": "iDBio Pro",
    "serie": "SN-1",
    "firmware": "1.2.0",
    "digitais": 12,
    "espelho_versao": "3:2026",
    "espelho_completo": True,
}


def montar_com_biometria(tmp_path, *respostas, caps=None, ao_poll=None):
    worker, client, impressos, pasta, estado = montar(tmp_path, *respostas)
    vistos: list[dict] = []
    worker._capacidades_biometria = caps or (lambda: dict(BIO))
    worker._ao_poll = ao_poll or vistos.append
    return worker, client, vistos, pasta


def test_heartbeat_leva_o_bloco_da_biometria(tmp_path):
    worker, client, _, pasta = montar_com_biometria(tmp_path, {"jobs": []})
    worker.run_once()
    caps = client.polls[0][2]
    assert caps == {
        "scale": {"configured": True, "ok": True, "folder": str(pasta)},
        "label": {"configured": True, "ok": True, "printer": "Argox OS-214"},
        "biometria": BIO,
    }


def test_construtor_aceita_as_funcoes_da_biometria(tmp_path):
    vistos: list = []
    worker = RemoteJobsWorker(
        load_config_fn=lambda: AgentConfig(),
        client_factory=lambda cfg: None,
        capacidades_biometria_fn=lambda: {"configured": False},
        ao_poll_fn=vistos.append,
    )
    assert worker.run_once() == remote_jobs.UNPAIRED_WAIT  # não pareado: nada muda
    assert vistos == []


def test_resposta_do_poll_vai_para_a_biometria(tmp_path):
    resposta = {"jobs": [], "biometria": {"habilitado": True, "versao": "7:2026"}}
    worker, _, vistos, _ = montar_com_biometria(tmp_path, resposta)
    worker.run_once()
    assert vistos == [resposta]


def test_biometria_quebrada_nao_derruba_o_canal(tmp_path):
    def caps():
        raise RuntimeError("dll sumiu")

    def ao_poll(resp):
        raise RuntimeError("espelho quebrou")

    worker, client, _, pasta = montar_com_biometria(
        tmp_path, {"jobs": [job_balanca()]}, caps=caps, ao_poll=ao_poll
    )
    worker.run_once()
    assert "biometria" not in client.polls[0][2]
    assert worker.get_status().state == "online"
    assert client.acks == [("j1", "done", "", {"paths": [str(pasta / "txitens.txt")]})]


def test_cache_das_capacidades_nao_congela_a_biometria(tmp_path):
    """Balança/etiqueta ficam em cache 30 s; o leitor é lido a cada poll."""
    valores = iter([{"digitais": 1}, {"digitais": 2}])
    worker, client, _, _ = montar_com_biometria(
        tmp_path, {"jobs": []}, {"jobs": []}, caps=lambda: next(valores)
    )
    worker.run_once()
    worker.run_once()
    assert [p[2]["biometria"]["digitais"] for p in client.polls] == [1, 2]
    assert "biometria" not in worker._caps_cache[2]


BAL = {"configured": True, "ok": True, "porta": "COM3", "protocolo": "toledo"}


def test_heartbeat_leva_o_bloco_da_balanca_de_checkout(tmp_path):
    """``balanca_checkout`` é a balança que pesa no caixa; ``scale`` continua
    sendo a pasta da carga (os dois convivem)."""
    worker, client, _, pasta = montar_com_biometria(tmp_path, {"jobs": []})
    worker._capacidades_balanca = lambda: dict(BAL)
    worker.run_once()
    assert client.polls[0][2] == {
        "scale": {"configured": True, "ok": True, "folder": str(pasta)},
        "label": {"configured": True, "ok": True, "printer": "Argox OS-214"},
        "biometria": BIO,
        "balanca_checkout": BAL,
    }
    assert "balanca_checkout" not in worker._caps_cache[2]


def test_construtor_aceita_a_funcao_da_balanca(tmp_path):
    worker, client, *_ = montar(tmp_path, {"jobs": []})
    worker2 = RemoteJobsWorker(
        load_config_fn=worker._load_config,
        client_factory=lambda cfg: client,
        list_printers_fn=lambda: [{"name": "Argox OS-214"}],
        capacidades_balanca_fn=lambda: dict(BAL),
    )
    worker2.run_once()
    caps = client.polls[0][2]
    assert caps["balanca_checkout"] == BAL
    assert "biometria" not in caps


def test_balanca_quebrada_nao_derruba_o_canal_nem_a_biometria(tmp_path):
    def caps_balanca():
        raise RuntimeError("pyserial sumiu")

    worker, client, _, pasta = montar_com_biometria(tmp_path, {"jobs": [job_balanca()]})
    worker._capacidades_balanca = caps_balanca
    worker.run_once()
    caps = client.polls[0][2]
    assert "balanca_checkout" not in caps
    assert caps["biometria"] == BIO
    assert worker.get_status().state == "online"
    assert client.acks == [("j1", "done", "", {"paths": [str(pasta / "txitens.txt")]})]


def test_biometria_quebrada_nao_apaga_a_balanca(tmp_path):
    def caps_bio():
        raise RuntimeError("dll sumiu")

    worker, client, _, _ = montar_com_biometria(tmp_path, {"jobs": []}, caps=caps_bio)
    worker._capacidades_balanca = lambda: dict(BAL)
    worker.run_once()
    caps = client.polls[0][2]
    assert "biometria" not in caps
    assert caps["balanca_checkout"] == BAL


def test_capacidades_denunciam_pasta_e_impressora_ausentes(tmp_path):
    worker, client, *_ = montar(
        tmp_path,
        {"jobs": []},
        scale_dir=str(tmp_path / "nao-existe"),
        remote_label_printer="Impressora Removida",
    )
    worker.run_once()
    caps = client.polls[0][2]
    assert caps["scale"]["configured"] is True and caps["scale"]["ok"] is False
    assert caps["label"]["configured"] is True and caps["label"]["ok"] is False


def test_sem_pasta_nem_impressora_reporta_nao_configurado(tmp_path):
    worker, client, *_ = montar(tmp_path, {"jobs": []}, scale_dir="", remote_label_printer="")
    worker.run_once()
    caps = client.polls[0][2]
    assert caps["scale"] == {"configured": False, "ok": False, "folder": ""}
    assert caps["label"] == {"configured": False, "ok": False, "printer": ""}


def test_intervalo_vem_do_servidor_dentro_dos_limites(tmp_path):
    worker, *_ = montar(
        tmp_path,
        {"jobs": [], "poll_interval": 2},
        {"jobs": [], "poll_interval": 0.1},
        {"jobs": [], "poll_interval": 9999},
        {"jobs": [], "poll_interval": "lixo"},
        {"jobs": []},
        {"jobs": [], "more": True, "poll_interval": 10},
    )
    assert worker.run_once() == 2
    assert worker.run_once() == remote_jobs.MIN_POLL
    assert worker.run_once() == remote_jobs.MAX_POLL
    assert worker.run_once() == remote_jobs.DEFAULT_POLL
    assert worker.run_once() == remote_jobs.DEFAULT_POLL
    # Fila com mais trabalhos: busca de novo em seguida.
    assert worker.run_once() == remote_jobs.MORE_WAIT


def test_pareamento_recusado_para_de_martelar(tmp_path):
    worker, *_ = montar(tmp_path, PairingRejected("Token revogado.", "agent_token_invalid"))
    assert worker.run_once() == remote_jobs.REJECTED_WAIT
    st = worker.get_status()
    assert st.state == "rejected"
    assert "novo código" in st.message


def test_loja_bloqueada_espera_e_tenta_depois(tmp_path):
    worker, *_ = montar(tmp_path, StoreBlocked("Loja bloqueada."))
    assert worker.run_once() == remote_jobs.BLOCKED_WAIT
    assert worker.get_status().state == "blocked"


def test_rate_limit_respeita_retry_after(tmp_path):
    worker, *_ = montar(tmp_path, RateLimited("calma", 42), RateLimited("calma", None))
    assert worker.run_once() == 42
    assert worker.run_once() == 30


def test_falha_de_rede_faz_backoff_e_zera_quando_volta(tmp_path):
    worker, *_ = montar(
        tmp_path,
        TransportError("sem rede"),
        TransportError("sem rede"),
        TransportError("sem rede"),
        {"jobs": [], "poll_interval": 10},
        TransportError("sem rede"),
    )
    assert worker.run_once() == 5
    assert worker.run_once() == 10
    assert worker.run_once() == 20
    assert worker.get_status().state == "error"
    assert worker.run_once() == 10
    assert worker.get_status().state == "online"
    assert worker.run_once() == 5  # backoff recomeça do início


def test_backoff_tem_teto(tmp_path):
    worker, *_ = montar(tmp_path, *[TransportError("x")] * 8)
    esperas = [worker.run_once() for _ in range(8)]
    assert max(esperas) == remote_jobs.BACKOFF_MAX
    assert esperas[-1] == remote_jobs.BACKOFF_MAX


def test_novo_pareamento_limpa_o_status_recusado(tmp_path):
    worker, client, _, _, estado = montar(tmp_path, PairingRejected("Token revogado."))
    worker.run_once()
    assert worker.get_status().state == "rejected"

    estado["cfg"] = AgentConfig(**{**estado["cfg"].__dict__, "server_token": "esa_" + "n" * 40})
    client.respostas.append({"jobs": [], "store_name": "Loja Centro"})
    worker.run_once()
    assert worker.get_status().state == "online"


def test_desparear_volta_ao_silencio(tmp_path):
    worker, client, _, _, estado = montar(tmp_path, {"jobs": []})
    worker.run_once()
    estado["cfg"] = AgentConfig()
    assert worker.run_once() == remote_jobs.UNPAIRED_WAIT
    assert len(client.polls) == 1
    assert worker.get_status().state == "unpaired"


# ── carga de balança ─────────────────────────────────────────────────────
def test_carga_da_balanca_e_gravada_e_confirmada(tmp_path):
    worker, client, _, pasta, _ = montar(tmp_path, {"jobs": [job_balanca()]})
    worker.run_once()
    destino = pasta / "txitens.txt"
    assert destino.read_bytes() == CARGA
    assert client.acks == [("j1", "done", "", {"paths": [str(destino)]})]
    assert "txitens.txt" in worker.get_status().last_job


def test_filizola_grava_os_dois_arquivos(tmp_path):
    job = job_balanca(arquivos=[("CADTXT.TXT", b"cad\r\n"), ("SETORTXT.TXT", b"setor\r\n")])
    worker, client, _, pasta, _ = montar(tmp_path, {"jobs": [job]})
    worker.run_once()
    assert (pasta / "CADTXT.TXT").read_bytes() == b"cad\r\n"
    assert (pasta / "SETORTXT.TXT").read_bytes() == b"setor\r\n"
    assert client.acks[0][1] == "done"
    assert len(client.acks[0][3]["paths"]) == 2


def test_nome_perigoso_reprova_o_lote_inteiro_sem_gravar_nada(tmp_path):
    """Valida tudo antes de gravar: nao deixa meia carga na pasta."""
    job = job_balanca(arquivos=[("CADTXT.TXT", b"cad\r\n"), ("..\\fora.txt", b"x")])
    worker, client, _, pasta, _ = montar(tmp_path, {"jobs": [job]})
    worker.run_once()
    assert list(pasta.iterdir()) == []
    assert not (tmp_path / "fora.txt").exists()
    job_id, status, detail, result = client.acks[0]
    assert (job_id, status, result) == ("j1", "error", {})
    assert "não permitido" in detail


@pytest.mark.parametrize(
    ("cfg_kw", "trecho"),
    [
        ({"scale_dir": ""}, "não configurada"),
        ({"scale_dir": "Z:\\pasta\\que\\nao\\existe"}, "não encontrada"),
    ],
)
def test_pasta_da_balanca_ausente_vira_ack_de_erro(tmp_path, cfg_kw, trecho):
    worker, client, *_ = montar(tmp_path, {"jobs": [job_balanca()]}, **cfg_kw)
    worker.run_once()
    assert client.acks[0][1] == "error"
    assert trecho in client.acks[0][2]


def test_base64_corrompido_vira_ack_de_erro(tmp_path):
    job = job_balanca()
    job["files"][0]["content_base64"] = "@@@ não é base64 @@@"
    worker, client, _, pasta, _ = montar(tmp_path, {"jobs": [job]})
    worker.run_once()
    assert client.acks[0][1] == "error"
    assert list(pasta.iterdir()) == []


def test_reentrega_de_carga_concluida_so_repete_o_ack(tmp_path):
    worker, client, _, pasta, _ = montar(
        tmp_path, {"jobs": [job_balanca()]}, {"jobs": [job_balanca()]}
    )
    worker.run_once()
    (pasta / "txitens.txt").write_bytes(b"a balanca ja consumiu e alguem mexeu")
    worker.run_once()
    # Nao regravou: o arquivo segue como estava depois da primeira entrega.
    assert (pasta / "txitens.txt").read_bytes() == b"a balanca ja consumiu e alguem mexeu"
    assert [a[:2] for a in client.acks] == [("j1", "done"), ("j1", "done")]


def test_carga_que_falhou_e_tentada_de_novo_na_reentrega(tmp_path):
    """Erro nao entra na memoria de concluidos: a pasta pode ter voltado."""
    worker, client, _, pasta, estado = montar(
        tmp_path,
        {"jobs": [job_balanca()]},
        {"jobs": [job_balanca()]},
        scale_dir=str(tmp_path / "ainda-nao-existe"),
    )
    worker.run_once()
    assert client.acks[0][1] == "error"

    estado["cfg"] = AgentConfig(**{**estado["cfg"].__dict__, "scale_dir": str(pasta)})
    worker.run_once()
    assert client.acks[1][1] == "done"
    assert (pasta / "txitens.txt").read_bytes() == CARGA


# ── etiquetas ────────────────────────────────────────────────────────────
def test_etiqueta_imprime_na_impressora_configurada_no_agente(tmp_path):
    worker, client, impressos, *_ = montar(tmp_path, {"jobs": [job_etiqueta(copies=3)]})
    worker.run_once()
    assert impressos == [
        {
            "printer_name": "Argox OS-214",
            "pdf_bytes": PDF,
            "copies": 3,
            "paper": None,
            "duplex": False,
            "render_mode": "label",
        }
    ]
    assert client.acks == [("e1", "done", "", {"printer": "Argox OS-214"})]


def test_servidor_nao_escolhe_a_impressora(tmp_path):
    """Mesmo que o job traga um nome de impressora, vale a do agente."""
    job = job_etiqueta(printer="Impressora Do Chefe")
    worker, _, impressos, *_ = montar(tmp_path, {"jobs": [job]})
    worker.run_once()
    assert impressos[0]["printer_name"] == "Argox OS-214"


def test_etiqueta_sem_render_mode_usa_label_e_copias_tem_limites(tmp_path):
    job = job_etiqueta(copies=5000)
    del job["params"]["render_mode"]
    worker, _, impressos, *_ = montar(tmp_path, {"jobs": [job, job_etiqueta("e2", copies="x")]})
    worker.run_once()
    assert impressos[0]["render_mode"] == "label"
    assert impressos[0]["copies"] == 99
    assert impressos[1]["copies"] == 1


def test_etiqueta_sem_impressora_configurada_nao_imprime(tmp_path):
    worker, client, impressos, *_ = montar(
        tmp_path, {"jobs": [job_etiqueta()]}, remote_label_printer=""
    )
    worker.run_once()
    assert impressos == []
    assert client.acks[0][1] == "error"
    assert "Impressora de etiquetas não configurada" in client.acks[0][2]


def test_arquivo_que_nao_e_pdf_nao_vai_para_a_impressora(tmp_path):
    job = job_etiqueta()
    job["files"][0]["content_base64"] = b64(b"MZ isto e um executavel")
    worker, client, impressos, *_ = montar(tmp_path, {"jobs": [job]})
    worker.run_once()
    assert impressos == []
    assert client.acks[0][1] == "error"


def test_falha_na_impressao_vira_ack_de_erro_com_o_motivo(tmp_path):
    worker, client, _, _, _ = montar(tmp_path, {"jobs": [job_etiqueta()]})

    def quebra(**kw):
        raise RuntimeError("SumatraPDF falhou (code=1): impressora offline")

    worker._print_pdf = quebra
    worker.run_once()
    assert client.acks == [("e1", "error", "SumatraPDF falhou (code=1): impressora offline", {})]


def test_etiqueta_reentregue_nao_e_impressa_duas_vezes(tmp_path):
    """Queda de rede no ack nao pode custar um lote de etiquetas."""
    worker, client, impressos, *_ = montar(
        tmp_path, {"jobs": [job_etiqueta()]}, {"jobs": [job_etiqueta()]}
    )
    client.ack_erro = TransportError("caiu no ack")
    worker.run_once()  # imprimiu, mas o ack se perdeu
    client.ack_erro = None
    worker.run_once()  # servidor reentregou
    assert len(impressos) == 1
    assert client.acks == [("e1", "done", "", {"printer": "Argox OS-214"})]


# ── robustez ─────────────────────────────────────────────────────────────
def test_tipo_desconhecido_vira_ack_de_erro_pedindo_atualizacao(tmp_path):
    job = {"id": "x1", "kind": "holograma_3d", "params": {}, "files": []}
    worker, client, *_ = montar(tmp_path, {"jobs": [job]})
    worker.run_once()
    assert client.acks[0][:2] == ("x1", "error")
    assert "Atualize o agente" in client.acks[0][2]


def test_um_trabalho_com_erro_nao_impede_os_seguintes(tmp_path):
    ruim = job_balanca("ruim", [("..\\x.txt", b"x")])
    worker, client, impressos, pasta, _ = montar(
        tmp_path, {"jobs": [ruim, job_balanca("bom"), job_etiqueta("etq")]}
    )
    worker.run_once()
    assert [a[:2] for a in client.acks] == [("ruim", "error"), ("bom", "done"), ("etq", "done")]
    assert (pasta / "txitens.txt").exists()
    assert len(impressos) == 1


@pytest.mark.parametrize(
    "resposta",
    [
        {"jobs": None},
        {"jobs": "lixo"},
        {"jobs": [None, "x", 3, {}, {"kind": "scale_file"}]},
        {"jobs": [{"id": "s", "kind": "scale_file", "files": "lixo", "params": []}]},
        {},
    ],
)
def test_resposta_malformada_nao_derruba_o_ciclo(tmp_path, resposta):
    worker, client, impressos, pasta, _ = montar(tmp_path, resposta)
    assert worker.run_once() > 0
    assert impressos == []
    assert list(pasta.iterdir()) == []
    assert all(a[1] == "error" for a in client.acks)


def test_memoria_de_concluidos_e_limitada(tmp_path, monkeypatch):
    monkeypatch.setattr(remote_jobs, "DONE_MEMORY", 3)
    jobs = [job_etiqueta(f"e{i}") for i in range(5)]
    worker, *_ = montar(tmp_path, {"jobs": jobs})
    worker.run_once()
    assert list(worker._done) == ["e2", "e3", "e4"]


def test_thread_inicia_processa_e_para(tmp_path):
    worker, client, _, pasta, _ = montar(tmp_path, {"jobs": [job_balanca()], "poll_interval": 60})
    worker.start()
    try:
        for _ in range(200):
            if client.acks:
                break
            worker._stop.wait(0.01)
        assert client.acks and client.acks[0][1] == "done"
        assert (pasta / "txitens.txt").read_bytes() == CARGA
    finally:
        worker.stop()
        worker._thread.join(timeout=5)
    assert not worker._thread.is_alive()
