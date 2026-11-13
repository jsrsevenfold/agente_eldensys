"""Canal remoto: o agente busca trabalhos no EldenSys e executa nesta máquina.

Serve para o que o navegador local não alcança: quem dispara está em OUTRO
aparelho (celular, outro PC), e o trabalho tem que acontecer aqui —

    scale_file   grava o TXT de carga na pasta da balança
    label_pdf    imprime etiquetas na impressora de etiquetas deste PC

Uma thread daemon faz o ciclo `poll → executa → ack`. Regras do desenho:

- **Agente não pareado não faz rede nenhuma.** PCs que usam o agente só
  para imprimir pelo navegador continuam exatamente como na v0.4.
- **A config é relida a cada ciclo.** Parear, trocar a pasta ou a
  impressora vale no ciclo seguinte, sem reiniciar o agente.
- **O ritmo é do servidor** (`poll_interval`): devagar quando ocioso,
  rápido quando alguém está prestes a imprimir.
- **Nada daqui derruba a impressão local.** Toda exceção morre no ciclo;
  a thread só registra e tenta de novo.
- **Trabalho concluído não é refeito.** Se o servidor reentregar um job
  cujo ack se perdeu, só o ack é repetido — reimprimir um lote de
  etiquetas por causa de uma queda de rede custaria etiqueta.
"""

from __future__ import annotations

import base64
import binascii
import logging
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import __version__
from .config import AgentConfig, is_paired, load_config
from .pdf_printer import print_pdf
from .printers import list_printers
from .remote_client import (
    PairingCodeError,
    PairingRejected,
    RateLimited,
    RemoteClient,
    RemoteError,
    StoreBlocked,
)
from .scale_files import ScaleFileError, safe_filename, write_scale_file

log = logging.getLogger("eldensys.agent.remote")

# ── ritmo ────────────────────────────────────────────────────────────────
UNPAIRED_WAIT = 15.0  # só relê a config; nenhuma chamada de rede
DEFAULT_POLL = 10.0
MIN_POLL, MAX_POLL = 2.0, 60.0
REJECTED_WAIT = 300.0  # pareamento recusado: não martela o servidor
BLOCKED_WAIT = 300.0  # loja bloqueada: pode voltar sozinha
BACKOFF_START, BACKOFF_MAX = 5.0, 60.0
MORE_WAIT = 0.2  # servidor avisou que há mais trabalhos na fila
CRASH_WAIT = 30.0

#: A checagem de "pasta existe?" pode travar segundos numa unidade de rede
#: fora do ar; não vale repetir a cada poll.
CAPS_TTL = 30.0
#: Quantos trabalhos concluídos lembrar para não refazer numa reentrega.
DONE_MEMORY = 200

MAX_PDF_BYTES = 20 * 1024 * 1024
_PAPERS = {"A4", "A5", "Letter", "Legal"}

KIND_SCALE_FILE = "scale_file"
KIND_LABEL_PDF = "label_pdf"


class JobError(Exception):
    """Trabalho não pôde ser executado; a mensagem vai pro usuário no EldenSys."""


@dataclass(frozen=True)
class RemoteStatus:
    """Retrato do canal para a janela de configuração."""

    #: unpaired | connecting | online | error | rejected | blocked
    state: str = "unpaired"
    message: str = "Agente não pareado com uma loja."
    store_name: str = ""
    agent_name: str = ""
    last_ok: float | None = None  # epoch do último poll bem-sucedido
    last_job: str = ""  # resumo do último trabalho executado


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _decode(file_entry: dict) -> bytes:
    try:
        return base64.b64decode(file_entry.get("content_base64") or "", validate=True)
    except (binascii.Error, ValueError, TypeError) as e:
        raise JobError("Conteúdo do arquivo veio corrompido (base64 inválido).") from e


class RemoteJobsWorker:
    """Ciclo poll → executa → ack. `run_once` é o ciclo; `start` roda em thread.

    As dependências entram pelo construtor para o teste trocar rede, disco de
    config e impressora por dublês sem `monkeypatch` espalhado.
    """

    def __init__(
        self,
        load_config_fn: Callable[[], AgentConfig] = load_config,
        client_factory: Callable[[AgentConfig], Any] | None = None,
        print_pdf_fn: Callable[..., None] = print_pdf,
        list_printers_fn: Callable[[], list] = list_printers,
    ) -> None:
        self._load_config = load_config_fn
        self._client_factory = client_factory or self._default_client
        self._print_pdf = print_pdf_fn
        self._list_printers = list_printers_fn

        self._status = RemoteStatus()
        self._status_lock = threading.Lock()
        self._backoff = BACKOFF_START
        self._last_token = ""
        self._done: OrderedDict[str, dict] = OrderedDict()
        self._caps_cache: tuple[tuple[str, str], float, dict] | None = None

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def _default_client(cfg: AgentConfig) -> RemoteClient:
        return RemoteClient(cfg.server_url, cfg.server_tenant, cfg.server_token, cfg.instance_id)

    # ── ciclo de vida ─────────────────────────────────
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="agent-remote")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        """Encurta a espera atual (ex.: o usuário acabou de parear)."""
        self._caps_cache = None
        self._wake.set()

    def _loop(self) -> None:
        log.info("Canal remoto iniciado")
        while not self._stop.is_set():
            try:
                wait = self.run_once()
            except Exception:  # a thread não pode morrer
                log.exception("Falha inesperada no canal remoto")
                wait = CRASH_WAIT
            self._wake.wait(timeout=wait)
            self._wake.clear()
        log.info("Canal remoto encerrado")

    # ── status ────────────────────────────────────────
    def get_status(self) -> RemoteStatus:
        with self._status_lock:
            return self._status

    def _set_status(self, **changes: Any) -> None:
        with self._status_lock:
            self._status = replace(self._status, **changes)

    # ── um ciclo ──────────────────────────────────────
    def run_once(self) -> float:
        """Executa um ciclo e devolve quantos segundos esperar até o próximo."""
        cfg = self._load_config()
        if not is_paired(cfg):
            self._backoff = BACKOFF_START
            self._last_token = ""
            self._set_status(
                state="unpaired",
                message="Agente não pareado com uma loja.",
                store_name="",
                agent_name="",
            )
            return UNPAIRED_WAIT

        try:
            client = self._client_factory(cfg)
        except PairingCodeError as e:
            self._set_status(state="error", message=str(e))
            return BACKOFF_MAX

        if cfg.server_token != self._last_token:
            # Pareamento novo (ou trocado): o status antigo — inclusive um
            # "recusado" do código anterior — não vale mais.
            self._last_token = cfg.server_token
            self._backoff = BACKOFF_START
            self._set_status(
                state="connecting",
                message="Conectando ao EldenSys...",
                store_name="",
                agent_name="",
            )

        try:
            resp = client.poll(__version__, socket.gethostname(), self._capabilities(cfg))
        except PairingRejected as e:
            log.warning("Pareamento recusado pelo EldenSys (%s): %s", e.code or "-", e)
            self._set_status(
                state="rejected",
                message=f"{e} Gere um novo código de pareamento no EldenSys.",
            )
            return REJECTED_WAIT
        except StoreBlocked as e:
            log.warning("Loja bloqueada no EldenSys: %s", e)
            self._set_status(state="blocked", message=str(e))
            return BLOCKED_WAIT
        except RateLimited as e:
            return _clamp(e.retry_after or 30.0, 5.0, 300.0)
        except RemoteError as e:
            wait = self._backoff
            self._backoff = min(self._backoff * 2, BACKOFF_MAX)
            log.warning("Canal remoto: %s (nova tentativa em %.0fs)", e, wait)
            self._set_status(state="error", message=str(e))
            return wait

        self._backoff = BACKOFF_START
        agent_info = resp.get("agent") if isinstance(resp.get("agent"), dict) else {}
        self._set_status(
            state="online",
            message="Conectado ao EldenSys.",
            store_name=str(resp.get("store_name") or ""),
            agent_name=str(agent_info.get("name") or ""),
            last_ok=time.time(),
        )

        jobs = resp.get("jobs")
        for job in jobs if isinstance(jobs, list) else []:
            if isinstance(job, dict):
                self._handle_job(client, cfg, job)

        if resp.get("more"):
            return MORE_WAIT
        try:
            interval = float(resp.get("poll_interval") or DEFAULT_POLL)
        except (TypeError, ValueError):
            interval = DEFAULT_POLL
        return _clamp(interval, MIN_POLL, MAX_POLL)

    # ── capacidades (vão no heartbeat) ────────────────
    def _capabilities(self, cfg: AgentConfig) -> dict:
        folder = (cfg.scale_dir or "").strip()
        printer = (cfg.remote_label_printer or "").strip()
        key = (folder, printer)
        now = time.monotonic()
        cached = self._caps_cache
        if cached and cached[0] == key and now - cached[1] < CAPS_TTL:
            return cached[2]

        caps = {
            "scale": {
                "configured": bool(folder),
                "ok": bool(folder) and self._folder_ok(folder),
                "folder": folder,
            },
            "label": {
                "configured": bool(printer),
                "ok": bool(printer) and printer in self._printer_names(),
                "printer": printer,
            },
        }
        self._caps_cache = (key, now, caps)
        return caps

    @staticmethod
    def _folder_ok(folder: str) -> bool:
        try:
            return Path(folder).is_dir()
        except OSError:
            return False

    def _printer_names(self) -> set[str]:
        try:
            return {str(p.get("name") or "") for p in self._list_printers()}
        except Exception:  # pywin32 ausente, spooler parado
            log.debug("Não foi possível listar impressoras", exc_info=True)
            return set()

    # ── execução ──────────────────────────────────────
    def _handle_job(self, client: Any, cfg: AgentConfig, job: dict) -> None:
        job_id = str(job.get("id") or "")
        if not job_id:
            return
        kind = str(job.get("kind") or "")

        if job_id in self._done:
            # Reentrega de trabalho já concluído: o ack anterior se perdeu.
            log.info("Trabalho %s (%s) já concluído; repetindo só o ack", job_id, kind)
            status, detail, result = "done", "", self._done[job_id]
        else:
            try:
                result = self._execute(cfg, kind, job)
                status, detail = "done", ""
                self._remember(job_id, result)
                log.info("Trabalho %s (%s) concluído: %s", job_id, kind, result)
                self._set_status(last_job=self._describe(kind, result))
            except Exception as e:  # vira ack de erro
                if isinstance(e, (JobError, ScaleFileError)):
                    # Erro previsto (pasta ausente, nome recusado...): uma
                    # linha basta, sem pilha no log da loja.
                    log.warning("Trabalho %s (%s) falhou: %s", job_id, kind, e)
                else:
                    log.exception("Trabalho %s (%s) falhou", job_id, kind)
                # Erro NÃO entra em `_done`: se o servidor reentregar (carga de
                # balança), vale tentar de novo — a pasta pode ter voltado.
                status, detail, result = "error", str(e)[:500], {}
                self._set_status(last_job=f"Falha: {detail}")

        try:
            client.ack(job_id, status, detail, result)
        except RemoteError as e:
            # Sem ack o servidor reentrega (balança) ou dá o trabalho por
            # perdido (etiqueta). Nos dois casos não há o que fazer aqui.
            log.warning("Não foi possível confirmar o trabalho %s: %s", job_id, e)

    def _remember(self, job_id: str, result: dict) -> None:
        self._done[job_id] = result
        while len(self._done) > DONE_MEMORY:
            self._done.popitem(last=False)

    @staticmethod
    def _describe(kind: str, result: dict) -> str:
        hora = time.strftime("%H:%M")
        if kind == KIND_SCALE_FILE:
            nomes = ", ".join(Path(p).name for p in result.get("paths", []))
            return f"{hora} — carga da balança gravada ({nomes})"
        if kind == KIND_LABEL_PDF:
            return f"{hora} — etiquetas impressas em {result.get('printer', '')}"
        return f"{hora} — {kind}"

    def _execute(self, cfg: AgentConfig, kind: str, job: dict) -> dict:
        files = job.get("files")
        files = [f for f in files if isinstance(f, dict)] if isinstance(files, list) else []
        params = job.get("params") if isinstance(job.get("params"), dict) else {}
        if kind == KIND_SCALE_FILE:
            return self._do_scale_file(cfg, files)
        if kind == KIND_LABEL_PDF:
            return self._do_label_pdf(cfg, params, files)
        raise JobError(
            f"Este agente (v{__version__}) não sabe executar o trabalho "
            f"'{kind}'. Atualize o agente."
        )

    def _do_scale_file(self, cfg: AgentConfig, files: list[dict]) -> dict:
        folder = (cfg.scale_dir or "").strip()
        if not folder:
            raise JobError("Pasta da balança não configurada no agente.")
        if not files:
            raise JobError("A carga chegou sem arquivos.")
        # Valida e decodifica TUDO antes de gravar qualquer coisa: a Filizola
        # manda dois arquivos, e gravar só um deixaria a carga inconsistente.
        prontos = [
            (safe_filename(f.get("filename")), _decode(f), f.get("sha256") or None)
            for f in files
        ]
        paths = [str(write_scale_file(folder, name, data, sha)) for name, data, sha in prontos]
        return {"paths": paths}

    def _do_label_pdf(self, cfg: AgentConfig, params: dict, files: list[dict]) -> dict:
        printer = (cfg.remote_label_printer or "").strip()
        if not printer:
            raise JobError("Impressora de etiquetas não configurada no agente.")
        if len(files) != 1:
            raise JobError("O trabalho de etiqueta precisa ter exatamente um PDF.")
        data = _decode(files[0])
        if not data.startswith(b"%PDF-"):
            raise JobError("O arquivo recebido não é um PDF.")
        if len(data) > MAX_PDF_BYTES:
            raise JobError("PDF de etiquetas grande demais.")

        try:
            copies = int(params.get("copies") or 1)
        except (TypeError, ValueError):
            copies = 1
        paper = params.get("paper")
        self._print_pdf(
            printer_name=printer,
            pdf_bytes=data,
            copies=int(_clamp(copies, 1, 99)),
            paper=paper if paper in _PAPERS else None,
            duplex=False,
            # Sempre "label" por padrão: é o que desliga o anti-aliasing na
            # térmica. Valor desconhecido cai no caminho clássico do print_pdf.
            render_mode=str(params.get("render_mode") or "label"),
        )
        return {"printer": printer}


# ── instância do processo (a janela de configuração consulta o status) ────
_worker: RemoteJobsWorker | None = None


def set_worker(worker: RemoteJobsWorker | None) -> None:
    global _worker
    _worker = worker


def get_worker() -> RemoteJobsWorker | None:
    return _worker
