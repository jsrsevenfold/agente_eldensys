"""Thread dona do leitor biométrico.

A DLL do iDBio não é thread-safe e o leitor é um só. Toda chamada passa por
UMA thread ("biometria-leitor"), que atende uma fila de prioridade:

- **usuário na frente do fundo.** Uma pessoa esperando o dedo não fica atrás
  da carga do espelho; a tarefa de fundo consulta ``ctx.ceder()`` entre um
  template e outro e devolve o leitor.
- **uma operação do usuário por vez.** A segunda recebe ``LeitorOcupado``
  (409 ``ocupado``) com ``pode_cancelar`` — a web oferece cancelar a outra.
- **``Init`` preguiçoso; ``Terminate`` cedo.** O PDV offline no mesmo PC usa o
  mesmo leitor: depois de uma operação do usuário o leitor fica aberto por
  ``biometria_ocioso_s`` (20 s — o cadastro são 4 leituras seguidas) e depois
  de uma tarefa de fundo é fechado na hora.
- **cancelar de fora.** ``CancelCapture`` é a única função chamada de outra
  thread (o SDK prevê isso); é repetida algumas vezes porque o pedido pode
  chegar um instante antes de a captura começar a esperar o dedo.
- **leitor arrancado.** -2, -7 e -8 fecham o leitor (``Terminate``), anotam
  ``conectado=False`` e a próxima operação reabre. Fase 0 (24/11/2026): com o
  cabo puxado a sessão aberta responde -8; religado, o ``Init`` só volta a dar
  certo uns 18 s depois (no mesmo processo, sem reiniciar nada); uma captura
  esperando o dedo não percebe o cabo puxado e volta com -6 no fim do
  ``DETECT_TIMEOUT``.
- **fatos sem sondar.** O status e o heartbeat não tocam no leitor: leem o
  que as operações provaram. Uma identificação que respondeu (achando ou não)
  prova o iDBio Pro conectado (``Contexto.identificar``). Quem torna a olhar
  um leitor "sem leitor" é o poll (``ServicoBiometria.sondar``).
- **vigia.** Uma chamada que passa muito do prazo marca ``travado``: a thread
  está presa na DLL e nada mais é aceito até ela voltar (aí o leitor é
  fechado e reaberto).
"""

from __future__ import annotations

import concurrent.futures
import itertools
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from .erros import (
    CANCELADO,
    CAPTURA_SEM_DEDO,
    IO_HOST,
    NAO_IDENTIFICADO,
    NAO_PRO,
    OCUPADO,
    SEM_LEITOR,
    USB,
    ErroBiometria,
    ErroLeitor,
    LeitorEmUso,
    LeitorOcupado,
    LeitorTravado,
    SdkIndisponivel,
)
from .sdk import id_da_loja_valido

log = logging.getLogger("eldensys.agent.biometria.leitor")

PRIORIDADE_PARAR = -1
PRIORIDADE_USUARIO = 0
PRIORIDADE_FUNDO = 1

#: A captura espera o dedo até ~30 s no próprio leitor (DETECT_TIMEOUT).
PRAZO_CAPTURA_S = 45.0
PRAZO_OPERACAO_S = 20.0
PRAZO_FUNDO_S = 120.0
#: Espera além do prazo da tarefa, para cobrir uma tarefa de fundo na frente.
FOLGA_FILA_S = 30.0
OCIOSO_PADRAO_S = 20.0
OCIOSO_MIN_S, OCIOSO_MAX_S = 0.05, 600.0

#: Fecha o leitor e reabre na próxima operação (-8 = sessão aberta com o cabo
#: puxado, medido na fase 0).
CODIGOS_REABRIR = frozenset({SEM_LEITOR, USB, IO_HOST})
#: ``Init`` com este código = leitor ocupado. Fase 0: com o leitor aberto por
#: OUTRO programa (o PDV no mesmo PC) o ``Init`` devolve -2, igual a cabo solto
#: — por isso a mensagem do -2 cita as duas causas.
CODIGOS_INIT_EM_USO = frozenset({OCUPADO})

#: Repetições do ``CancelCapture`` (o pedido pode chegar antes da espera).
CANCEL_REPETICOES = 3
CANCEL_INTERVALO_S = 0.4


@dataclass(frozen=True)
class FatosLeitor:
    """O que se sabe do leitor SEM tocar nele (status, heartbeat, janela)."""

    conectado: bool | None = None
    pro: bool | None = None
    modelo: str | None = None
    serie: str | None = None
    firmware: str | None = None
    digitais: int | None = None
    erro: str | None = None


class Cancelamento:
    """Ficha de cancelamento de UMA operação do usuário.

    A rota cria uma por pedido e chama ``cancelar()`` se o navegador sair; o
    serviço confere ``cancelado`` antes de cada etapa demorada.
    """

    def __init__(self) -> None:
        self._evento = threading.Event()
        self._leitor: Leitor | None = None

    @property
    def cancelado(self) -> bool:
        return self._evento.is_set()

    def cancelar(self) -> None:
        self._evento.set()
        leitor = self._leitor
        if leitor is not None:
            leitor._cancelar_ficha(self)

    def exigir_ativo(self, etapa: str = "") -> None:
        if self.cancelado:
            raise ErroLeitor(CANCELADO, etapa)


@dataclass(eq=False)
class _Tarefa:
    fn: Callable[[Any, Contexto], Any]
    usuario: bool
    nome: str
    captura: bool
    cancelamento: Cancelamento | None
    prazo_s: float
    future: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)


class Contexto:
    """Segundo argumento da função de uma tarefa."""

    def __init__(self, leitor: Leitor, tarefa: _Tarefa) -> None:
        self._leitor = leitor
        self._tarefa = tarefa

    @property
    def cancelado(self) -> bool:
        tok = self._tarefa.cancelamento
        return tok is not None and tok.cancelado

    def ceder(self) -> bool:
        """Tarefa de fundo: True quando deve parar já e devolver o leitor."""
        return self._leitor._usuario_esperando() or self._leitor._parando

    def capturar(self, fn: Callable[[], Any]) -> Any:
        """Roda ``fn`` (uma captura) com o cancelamento ligado ao ``CancelCapture``."""
        leitor = self._leitor
        with leitor._lock:
            if self.cancelado:
                raise ErroLeitor(CANCELADO, self._tarefa.nome)
            leitor._em_captura = True
        try:
            return fn()
        finally:
            with leitor._lock:
                leitor._em_captura = False

    def identificar(self, sdk: Any) -> Any:
        """``CaptureAndIdentify`` como as operações usam (identificar, login,
        verificação do cadastro).

        - Respondeu, achando alguém ou não (-18), só um iDBio Pro conectado
          responde: anota ``conectado`` e ``pro``. Sem isso, um agente que
          reiniciou sem sondar o leitor seguiria "leitor não encontrado" na web.
        - Id fora da faixa da loja (``0 < id < 2**60``) foi gravado por outro
          sistema (o leitor veio de outro lugar): vale como "não reconhecida",
          porque o servidor recusaria o id com 400.
        """
        try:
            ident = self.capturar(sdk.identificar)
        except ErroLeitor as e:
            if e.codigo == NAO_IDENTIFICADO:
                # Termina em exceção: o "deu certo" do _rodar não roda, então o
                # erro antigo (ex.: cabo USB) sai aqui.
                self._leitor.anotar(conectado=True, pro=True, erro=None)
            raise
        self._leitor.anotar(conectado=True, pro=True, erro=None)
        if not id_da_loja_valido(ident.device_id):
            log.warning(
                "O leitor reconheceu a digital de id %s, fora da faixa das digitais da "
                "loja (gravada por outro sistema): tratada como não reconhecida",
                ident.device_id,
            )
            raise ErroLeitor(NAO_IDENTIFICADO, "CaptureAndIdentify: id de outro sistema")
        return ident


class Leitor:
    def __init__(
        self,
        carregar_sdk: Callable[[], Any],
        *,
        ocioso_s: Callable[[], float] | float = OCIOSO_PADRAO_S,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        self._carregar_sdk = carregar_sdk
        self._ocioso_fn = ocioso_s if callable(ocioso_s) else (lambda: float(ocioso_s))
        self._relogio = relogio

        self._lock = threading.RLock()
        self._fila: queue.PriorityQueue = queue.PriorityQueue()
        self._seq = itertools.count()
        self._thread: threading.Thread | None = None
        self._parando = False

        self._sdk: Any = None
        self._sdk_erro: str | None = None
        self._aberto = False
        self._fechar_em: float | None = None
        self._ultimo_usuario: float | None = None
        self._atual: _Tarefa | None = None
        self._inicio_atual = 0.0
        self._usuario: _Tarefa | None = None
        self._em_captura = False
        self._travado = False
        self._fatos = FatosLeitor()

    # ── consulta (nunca toca no leitor) ───────────────
    @property
    def aberto(self) -> bool:
        return self._aberto

    def fatos(self) -> FatosLeitor:
        with self._lock:
            return self._fatos

    def anotar(self, **campos: Any) -> None:
        with self._lock:
            self._fatos = replace(self._fatos, **campos)

    def sdk_estado(self) -> dict:
        with self._lock:
            return {"carregado": self._sdk is not None, "erro": self._sdk_erro}

    def estado(self) -> dict:
        travado = self._verificar_travado()
        with self._lock:
            f = self._fatos
            usuario = self._usuario
            return {
                "conectado": bool(f.conectado),
                "pro": f.pro,
                "modelo": f.modelo,
                "serie": f.serie,
                "firmware": f.firmware,
                "ocupado": usuario is not None and not usuario.future.done(),
                "operacao": self._atual.nome if self._atual else None,
                "digitais": f.digitais,
                "erro": f.erro,
                "travado": travado,
                "aberto": self._aberto,
            }

    def ocupado_por_usuario(self) -> bool:
        with self._lock:
            u = self._usuario
            return u is not None and not u.future.done()

    # ── pedidos ───────────────────────────────────────
    def enviar(
        self,
        fn: Callable[[Any, Contexto], Any],
        *,
        usuario: bool,
        nome: str,
        captura: bool = False,
        cancelamento: Cancelamento | None = None,
        prazo_s: float | None = None,
    ) -> concurrent.futures.Future:
        """Põe uma tarefa na fila e devolve o ``Future`` do resultado."""
        if self._verificar_travado():
            raise LeitorTravado()
        if prazo_s is None:
            prazo_s = PRAZO_CAPTURA_S if captura else (PRAZO_OPERACAO_S if usuario else PRAZO_FUNDO_S)
        with self._lock:
            if self._parando:
                raise ErroBiometria(
                    "biometria_desligada", "O EldenSys Agent está encerrando.", status=503
                )
            if usuario:
                atual = self._usuario
                if atual is not None and not atual.future.done():
                    raise LeitorOcupado(pode_cancelar=atual.captura, operacao=atual.nome)
                if cancelamento is None:
                    cancelamento = Cancelamento()
            if cancelamento is not None:
                cancelamento._leitor = self
                if cancelamento.cancelado:
                    raise ErroLeitor(CANCELADO, nome)
            tarefa = _Tarefa(fn, usuario, nome, captura, cancelamento, float(prazo_s))
            if usuario:
                self._usuario = tarefa
            prioridade = PRIORIDADE_USUARIO if usuario else PRIORIDADE_FUNDO
            self._fila.put((prioridade, next(self._seq), tarefa))
            self._garantir_thread()
        return tarefa.future

    def executar(self, fn: Callable[[Any, Contexto], Any], **kw: Any) -> Any:
        """``enviar`` + esperar o resultado (levanta o erro da tarefa)."""
        fut = self.enviar(fn, **kw)
        prazo = kw.get("prazo_s")
        if prazo is None:
            captura, usuario = kw.get("captura", False), kw.get("usuario", False)
            prazo = PRAZO_CAPTURA_S if captura else (PRAZO_OPERACAO_S if usuario else PRAZO_FUNDO_S)
        try:
            return fut.result(timeout=prazo + FOLGA_FILA_S)
        except concurrent.futures.TimeoutError:
            self._verificar_travado(forcar=True)
            raise LeitorTravado() from None

    def cancelar(self) -> bool:
        """Cancela a operação do usuário em andamento (ou na fila)."""
        with self._lock:
            tarefa = self._usuario
            if tarefa is None or tarefa.future.done():
                return False
            tok = tarefa.cancelamento
        if tok is not None:
            tok.cancelar()
        return True

    def parar(self, timeout: float = 3.0) -> None:
        """Encerra a thread e fecha o leitor (sair do agente)."""
        with self._lock:
            self._parando = True
            thread = self._thread
        self.cancelar()
        if thread is not None:
            self._fila.put((PRIORIDADE_PARAR, -1, None))
            thread.join(timeout)
        else:
            self._fechar()
        self._esvaziar_fila()

    # ── thread ────────────────────────────────────────
    def _garantir_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, daemon=True, name="biometria-leitor")
            self._thread.start()

    def _loop(self) -> None:
        try:
            while True:
                try:
                    _, _, tarefa = self._fila.get(timeout=self._tempo_ate_fechar())
                except queue.Empty:
                    self._fechar_se_vencido()
                    continue
                if tarefa is None:
                    break
                self._rodar(tarefa)
        except Exception:  # pragma: no cover - a thread não pode morrer calada
            log.exception("Falha inesperada na thread do leitor biométrico")
        finally:
            self._fechar()

    def _tempo_ate_fechar(self) -> float | None:
        with self._lock:
            if not self._aberto:
                return None
            if self._fechar_em is None:
                return 0.0
            return max(0.0, self._fechar_em - self._relogio())

    def _fechar_se_vencido(self) -> None:
        with self._lock:
            vencido = (
                self._aberto
                and self._atual is None
                and (self._fechar_em is None or self._relogio() >= self._fechar_em)
            )
        if vencido:
            self._fechar()

    def _rodar(self, tarefa: _Tarefa) -> None:
        fut = tarefa.future
        if not fut.set_running_or_notify_cancel():
            return
        tok = tarefa.cancelamento
        if tok is not None and tok.cancelado:
            fut.set_exception(ErroLeitor(CANCELADO, tarefa.nome))
            return
        with self._lock:
            self._atual = tarefa
            self._inicio_atual = self._relogio()
            self._fechar_em = None
        reabrir = False
        try:
            sdk = self._abrir()
            resultado = tarefa.fn(sdk, Contexto(self, tarefa))
        except Exception as e:  # noqa: BLE001 - vai para o Future
            self._apos_erro(e)
            fut.set_exception(e)
        else:
            self.anotar(conectado=True, erro=None)
            fut.set_result(resultado)
        finally:
            with self._lock:
                self._atual = None
                self._em_captura = False
                if self._travado:
                    # A chamada presa voltou: fecha e reabre na próxima.
                    log.warning("Leitor biométrico voltou a responder (%s)", tarefa.nome)
                    self._travado = False
                    reabrir = True
            if reabrir:
                self._fechar()
            self._depois_da_tarefa(tarefa)

    def _abrir(self) -> Any:
        if self._sdk is None:
            try:
                sdk = self._carregar_sdk()
            except SdkIndisponivel as e:
                with self._lock:
                    self._sdk_erro = e.mensagem
                raise
            except Exception:
                log.exception("Falha ao carregar o SDK do leitor biométrico")
                with self._lock:
                    self._sdk_erro = "Não foi possível carregar a biblioteca do leitor biométrico."
                raise SdkIndisponivel(self._sdk_erro) from None
            with self._lock:
                self._sdk, self._sdk_erro = sdk, None
        if not self._aberto:
            try:
                self._sdk.iniciar()
            except ErroLeitor as e:
                self._sdk.terminar()
                if e.codigo in CODIGOS_INIT_EM_USO:
                    raise LeitorEmUso(e.codigo) from None
                raise
            with self._lock:
                self._aberto = True
        return self._sdk

    def _fechar(self) -> None:
        with self._lock:
            sdk = self._sdk if self._aberto else None
            self._aberto = False
            self._fechar_em = None
        if sdk is not None:
            sdk.terminar()

    def _apos_erro(self, e: Exception) -> None:
        if isinstance(e, ErroLeitor):
            if e.codigo in CODIGOS_REABRIR:
                self._fechar()
                self.anotar(conectado=False, erro=e.mensagem)
            elif e.codigo == NAO_PRO:
                self.anotar(pro=False, erro=e.mensagem)
            elif e.codigo not in CAPTURA_SEM_DEDO and e.codigo != NAO_IDENTIFICADO:
                self.anotar(erro=e.mensagem)
        elif isinstance(e, ErroBiometria):
            self.anotar(erro=e.mensagem)
        else:
            log.error("Tarefa do leitor falhou: %s", type(e).__name__)

    def _depois_da_tarefa(self, tarefa: _Tarefa) -> None:
        agora = self._relogio()
        ocioso = min(max(float(self._ocioso_fn()), OCIOSO_MIN_S), OCIOSO_MAX_S)
        with self._lock:
            if tarefa.usuario:
                self._ultimo_usuario = agora
                self._fechar_em = agora + ocioso
                return
            pendente = not self._fila.empty()
            ultimo = self._ultimo_usuario
            na_janela = ultimo is not None and agora - ultimo < ocioso
            if na_janela:
                self._fechar_em = ultimo + ocioso  # type: ignore[operator]
        if not pendente and not na_janela:
            # Fundo: devolve o leitor já (o PDV no mesmo PC pode precisar dele).
            self._fechar()

    def _esvaziar_fila(self) -> None:
        while True:
            try:
                _, _, tarefa = self._fila.get_nowait()
            except queue.Empty:
                return
            if tarefa is not None and not tarefa.future.done():
                tarefa.future.set_exception(
                    ErroBiometria("biometria_desligada", "O EldenSys Agent está encerrando.", status=503)
                )

    # ── cancelar / vigia ──────────────────────────────
    def _usuario_esperando(self) -> bool:
        with self._lock:
            u = self._usuario
            return u is not None and not u.future.done() and u is not self._atual

    def _cancelar_ficha(self, tok: Cancelamento) -> None:
        with self._lock:
            tarefa = self._atual
            if tarefa is None or tarefa.cancelamento is not tok or not self._em_captura:
                return
            sdk = self._sdk
        if sdk is None:
            return
        self._cancel_capture(sdk)

        def repetir() -> None:
            for _ in range(CANCEL_REPETICOES):
                time.sleep(CANCEL_INTERVALO_S)
                with self._lock:
                    if self._atual is not tarefa or not self._em_captura:
                        return
                self._cancel_capture(sdk)

        threading.Thread(target=repetir, daemon=True, name="biometria-cancelar").start()

    @staticmethod
    def _cancel_capture(sdk: Any) -> None:
        try:
            sdk.cancelar_captura()
        except Exception:  # noqa: BLE001
            log.warning("CancelCapture falhou", exc_info=True)

    def _verificar_travado(self, *, forcar: bool = False) -> bool:
        with self._lock:
            tarefa = self._atual
            if (
                tarefa is not None
                and not self._travado
                and (forcar or self._relogio() - self._inicio_atual > tarefa.prazo_s)
            ):
                log.error(
                    "Leitor biométrico sem resposta há mais de %.0f s (%s)",
                    tarefa.prazo_s,
                    tarefa.nome,
                )
                self._travado = True
                self._fatos = replace(self._fatos, erro=LeitorTravado().mensagem)
            return self._travado
