"""Thread dona da porta serial da balança de checkout.

Porte do I/O de ``eldensys_mobile/lib/core/services/scale_service.dart`` (o
PDV offline): as estratégias por protocolo, ``_ler_exclusivo``
(``_lerPesoExclusivo``), ``_detectar_exclusivo`` e o fim de frame
(``_lerDoBuffer``), com os mesmos tempos. O que os bytes significam fica em
``protocolos.py``, puro e testado.

Regras do desenho:

- **uma thread dona da porta** ("balanca-leitor"), que atende uma fila FIFO.
  Duas leituras simultâneas (o PDV pesando e a tela de configuração
  testando) roubariam frames uma da outra; aqui uma espera a outra. Mais de
  ``FILA_MAX`` pedidos na fila → ``ocupada`` (409).
- **abre no uso, fecha cedo.** A COM é exclusiva no Windows e o PDV offline
  no mesmo computador pode precisar dela: a porta abre na primeira leitura e
  fecha depois de ``balanca_ocioso_s`` (5 s) sem uso. ``liberar`` fecha na
  hora.
- **porta|velocidade mudou → fecha** (e esquece o byte do Urano e a ordem do
  ``auto``: pode ser outra balança). Protocolo mudou → zera a ordem do
  ``auto``.
- **porta que cai na leitura** (adaptador USB arrancado) → fecha, anota o
  erro e responde ``porta_falhou``; a próxima leitura tenta reabrir.
- **vigia.** Quem pediu espera no máximo o prazo da tarefa + 5 s depois que
  ela começou; passou disso, ``balanca_sem_resposta`` (503): a thread está
  presa no driver.
- **log curto.** Uma linha por assunto a cada 60 s (a web repete leituras a
  cada ~80 ms enquanto pesa).

Divergências deliberadas do Dart (lá a porta fica aberta para sempre):

- fechar por ociosidade NÃO zera ``PedidoUrano`` nem ``OrdemAuto`` (nem a
  última leitura): a porta fecha a cada 5 s parada, e esquecer faria toda
  pesagem pagar de novo o ENQ → EOT do Urano e a volta inteira do ``auto``;
- leitura de protocolo passivo (Elgin, genérico) depois de mais de 1 s sem
  leitura começa com purga: com a porta aberta e ninguém lendo, o stream
  contínuo acumula no driver, e o peso do produto anterior sairia como o
  atual. No Dart o laço do PDV lê sem parar e o buffer está sempre fresco.
"""

from __future__ import annotations

import concurrent.futures
import logging
import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from . import erros
from .erros import ErroBalanca
from .porta import Transporte, traduzir_erro_abertura
from .protocolos import (
    FrameBalanca,
    LeituraBalanca,
    OrdemAuto,
    PedidoUrano,
    descrever_bytes,
    descrever_frame,
    interpretar_compatibilidade,
    interpretar_elgin,
    interpretar_filizola,
    interpretar_generico,
    interpretar_toledo,
    interpretar_urano,
    parece_urano,
    protocolo_do_frame,
    urano_completo,
)

log = logging.getLogger("eldensys.agent.balanca.leitor")

#: Protocolos que o operador escolhe; qualquer outro valor (o ``auto`` de
#: fábrica) testa todos, do mais específico ao mais genérico.
PROTOCOLOS_FIXOS = frozenset({"urano", "toledo", "filizola", "elgin", "generico"})
#: Passivo = não pede nada, só escuta o stream contínuo da balança.
PASSIVOS = frozenset({"elgin", "generico"})

# ── tempos do scale_service.dart ─────────────────────────────────────────
#: Prazo curto do Urano até a resposta COMEÇAR antes de tentar o EOT.
CURTO_URANO_S = 0.25
#: Fim de frame: terminador (CR/LF/ETX ou Urano completo) + este silêncio.
SILENCIO_FIM_S = 0.06
#: DETECTAR: prazo de cada tentativa e quantos reconhecimentos seguidos.
PRAZO_DETECTAR_S = 0.7
TENTATIVAS_DETECTAR = 2

# ── tempos do agente ─────────────────────────────────────────────────────
#: Intervalo do laço que olha a porta (aberta com ``timeout=0``).
POLL_S = 0.005
#: Passivo parado há mais que isto começa com purga (ver docstring).
PURGA_PASSIVA_APOS_S = 1.0
FILA_MAX = 8
#: Vigia: além do prazo da tarefa, a partir de quando ela começou.
FOLGA_VIGIA_S = 5.0
OCIOSO_PADRAO_S = 5.0
OCIOSO_MIN_S, OCIOSO_MAX_S = 0.05, 600.0
#: Uma linha de log por assunto a cada tanto (a primeira sai na hora).
LOG_INTERVALO_S = 60.0


class _PortaCaiu(Exception):
    """Erro de I/O numa porta que estava aberta (adaptador removido, driver)."""


@dataclass(eq=False)
class _Tarefa:
    fn: Callable[[], Any]
    nome: str
    prazo_s: float
    future: concurrent.futures.Future = field(default_factory=concurrent.futures.Future)
    iniciada_em: float | None = None


@dataclass(frozen=True)
class Deteccao:
    """Resultado do DETECTAR. Sem protocolo reconhecido, ``frame`` é None e
    ``dados`` diz se algo chegou ("respondeu num formato desconhecido")."""

    protocolo: str | None
    frame: FrameBalanca | None
    dados: bytes


class LeitorBalanca:
    def __init__(
        self,
        abrir_fn: Callable[[str, int], Transporte],
        *,
        ocioso_s: Callable[[], float] | float = OCIOSO_PADRAO_S,
        relogio: Callable[[], float] = time.monotonic,
    ) -> None:
        """Construir não abre a porta nem cria a thread: tudo é preguiçoso."""
        self._abrir_fn = abrir_fn
        self._ocioso_fn = ocioso_s if callable(ocioso_s) else (lambda: float(ocioso_s))
        self._relogio = relogio

        self._lock = threading.RLock()
        self._fila: queue.Queue[_Tarefa | None] = queue.Queue()
        self._pendentes = 0
        self._thread: threading.Thread | None = None
        self._parando = False
        self._atual: _Tarefa | None = None

        # Estado da porta: só a thread do leitor escreve; status() lê com lock.
        self._porta: Transporte | None = None
        self._nome_porta = ""
        self._baud = 0
        self._chave = ""
        self._protocolo: str | None = None
        self._erro_porta: str | None = None
        self._fechar_em: float | None = None
        self._ultima_leitura: LeituraBalanca | None = None

        self._buffer = bytearray()
        self._ultimo_rx = 0.0
        #: Bytes da última chamada a ``_ler_do_buffer``: o frame inteiro.
        self._ultimo_frame = b""
        #: Fim da última leitura com a porta aberta (purga do passivo).
        self._ultimo_tick_em: float | None = None
        #: Byte de pedido do Urano 12 e ordem do ``auto``: vivem entre leituras.
        self._pedido_urano = PedidoUrano()
        self._ordem_auto = OrdemAuto()

        self._log_quando: dict[str, float] = {}
        self._erro_logado: str | None = None
        self._fallback_logado = False
        self._byte_estab_logado = False

    # ── consulta (nunca toca na porta) ────────────────
    def estado_porta(self) -> dict:
        with self._lock:
            return {"aberta": self._porta is not None, "erro": self._erro_porta}

    def ultima_leitura(self) -> LeituraBalanca | None:
        with self._lock:
            return self._ultima_leitura

    @property
    def aberta(self) -> bool:
        with self._lock:
            return self._porta is not None

    # ── pedidos ───────────────────────────────────────
    def ler(self, *, porta: str, baud: int, protocolo: str, timeout_s: float) -> LeituraBalanca:
        """Uma leitura curta (o "tick" do PDV offline). No ``auto`` cada
        estratégia tem o prazo inteiro, como no Dart: balança muda leva até
        5 × ``timeout_s``; a que responde volta logo."""
        voltas = 1 if protocolo in PROTOCOLOS_FIXOS else len(OrdemAuto.BASE)
        prazo = voltas * (timeout_s + CURTO_URANO_S + SILENCIO_FIM_S) + 1.0
        return self._executar(
            lambda: self._tarefa_ler(porta, baud, protocolo, timeout_s), "leitura", prazo
        )

    def detectar(
        self, *, porta: str, baud: int, cancelamento: threading.Event | None = None
    ) -> Deteccao:
        """Tenta cada protocolo e devolve o primeiro que RECONHECER o frame em
        2 pedidos seguidos (peso, prato vazio ou instável contam)."""
        prazo = (
            len(OrdemAuto.BASE)
            * TENTATIVAS_DETECTAR
            * (PRAZO_DETECTAR_S + CURTO_URANO_S + SILENCIO_FIM_S)
            + 1.0
        )
        return self._executar(
            lambda: self._tarefa_detectar(porta, baud, cancelamento), "detectar", prazo
        )

    def liberar(self) -> None:
        """Fecha a porta agora (o PDV offline neste computador pode precisar)."""
        with self._lock:
            if self._porta is None and self._pendentes == 0:
                return
        self._executar(lambda: self._fechar(zerar=False), "liberar", 1.0)

    def parar(self, timeout: float = 3.0) -> None:
        """Sair do agente: termina a thread e solta a COM."""
        with self._lock:
            self._parando = True
            thread = self._thread
        if thread is not None and thread.is_alive():
            self._fila.put(None)
            thread.join(timeout)
        else:
            self._fechar(zerar=False)
        self._esvaziar_fila()

    # ── fila ──────────────────────────────────────────
    def _executar(self, fn: Callable[[], Any], nome: str, prazo_s: float) -> Any:
        tarefa = self._enviar(fn, nome, prazo_s)
        return self._esperar(tarefa)

    def _enviar(self, fn: Callable[[], Any], nome: str, prazo_s: float) -> _Tarefa:
        with self._lock:
            if self._parando:
                raise erros.agente_encerrando()
            if self._pendentes >= FILA_MAX:
                raise erros.ocupada()
            tarefa = _Tarefa(fn, nome, float(prazo_s))
            self._pendentes += 1
            self._fila.put(tarefa)
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._loop, daemon=True, name="balanca-leitor"
                )
                self._thread.start()
        return tarefa

    def _esperar(self, tarefa: _Tarefa) -> Any:
        while True:
            try:
                return tarefa.future.result(timeout=0.05)
            except concurrent.futures.TimeoutError:
                pass
            if self._vigia_disparou():
                raise erros.balanca_sem_resposta()
            thread = self._thread
            if (thread is None or not thread.is_alive()) and not tarefa.future.done():
                raise erros.agente_encerrando()

    def _vigia_disparou(self) -> bool:
        with self._lock:
            atual = self._atual
            if atual is None or atual.iniciada_em is None:
                return False
            atrasada = self._relogio() - atual.iniciada_em > atual.prazo_s + FOLGA_VIGIA_S
        if atrasada:
            self._log_balanca(
                "vigia",
                lambda: f"tarefa '{atual.nome}' presa há mais de "
                f"{atual.prazo_s + FOLGA_VIGIA_S:.0f} s na porta {self._nome_porta}",
                nivel=logging.ERROR,
            )
        return atrasada

    def _esvaziar_fila(self) -> None:
        sentinela = False
        while True:
            try:
                tarefa = self._fila.get_nowait()
            except queue.Empty:
                break
            if tarefa is None:
                sentinela = True
            elif not tarefa.future.done():
                tarefa.future.set_exception(erros.agente_encerrando())
        if sentinela:
            # A thread ainda presa numa tarefa precisa achar o fim da fila
            # quando voltar (senão fica parada para sempre no get()).
            self._fila.put(None)

    # ── thread ────────────────────────────────────────
    def _loop(self) -> None:
        try:
            while True:
                try:
                    tarefa = self._fila.get(timeout=self._tempo_ate_fechar())
                except queue.Empty:
                    self._fechar_se_vencido()
                    continue
                if tarefa is None:
                    break
                self._rodar(tarefa)
        except Exception:  # pragma: no cover - a thread não pode morrer calada
            log.exception("Falha inesperada na thread da balança")
        finally:
            self._fechar(zerar=False)

    def _tempo_ate_fechar(self) -> float | None:
        with self._lock:
            if self._porta is None:
                return None
            if self._fechar_em is None:
                return 0.0
            return max(0.0, self._fechar_em - self._relogio())

    def _fechar_se_vencido(self) -> None:
        with self._lock:
            vencido = (
                self._porta is not None
                and self._atual is None
                and (self._fechar_em is None or self._relogio() >= self._fechar_em)
            )
        if vencido:
            log.debug("Porta %s da balança fechada por ociosidade", self._nome_porta)
            self._fechar(zerar=False)

    def _ocioso_s(self) -> float:
        try:
            return min(max(float(self._ocioso_fn()), OCIOSO_MIN_S), OCIOSO_MAX_S)
        except (TypeError, ValueError):
            return OCIOSO_PADRAO_S

    def _rodar(self, tarefa: _Tarefa) -> None:
        fut = tarefa.future
        if not fut.set_running_or_notify_cancel():
            with self._lock:
                self._pendentes -= 1
            return
        with self._lock:
            self._atual = tarefa
            tarefa.iniciada_em = self._relogio()
            self._fechar_em = None
            parando = self._parando
        try:
            if parando:
                raise erros.agente_encerrando()
            resultado = tarefa.fn()
        except Exception as e:  # noqa: BLE001 - vai para o Future
            if not isinstance(e, ErroBalanca):
                log.exception("Falha inesperada na tarefa '%s' da balança", tarefa.nome)
            fut.set_exception(e)
        else:
            fut.set_result(resultado)
        finally:
            ocioso = self._ocioso_s()
            with self._lock:
                self._atual = None
                self._pendentes -= 1
                if self._porta is not None:
                    self._fechar_em = self._relogio() + ocioso

    # ── tarefas (rodam na thread do leitor) ───────────
    def _tarefa_ler(self, porta: str, baud: int, protocolo: str, timeout_s: float) -> LeituraBalanca:
        self._preparar(porta, baud, protocolo)
        self._garantir_aberta()
        return self._ler_exclusivo(timeout_s)

    def _tarefa_detectar(
        self, porta: str, baud: int, cancelamento: threading.Event | None
    ) -> Deteccao:
        if cancelamento is not None and cancelamento.is_set():
            raise erros.cancelado()
        self._preparar(porta, baud, None)
        return self._detectar_exclusivo(cancelamento)

    # ── porta ─────────────────────────────────────────
    def _preparar(self, porta: str, baud: int, protocolo: str | None) -> None:
        chave = f"{porta}|{baud}"
        if chave != self._chave:
            # Outra porta ou velocidade: pode ser outra balança.
            self._fechar(zerar=True)
            with self._lock:
                self._chave = chave
                self._nome_porta = porta
                self._baud = baud
                self._erro_porta = None
                self._ultima_leitura = None
        if protocolo is not None and protocolo != self._protocolo:
            self._protocolo = protocolo
            self._ordem_auto.zerar()
            self._fallback_logado = False

    def _garantir_aberta(self) -> None:
        if self._porta is not None:
            return
        porta, baud = self._nome_porta, self._baud
        try:
            transporte = self._abrir_fn(porta, baud)
        except ErroBalanca as e:
            erro = e
        except Exception as e:  # noqa: BLE001 - SerialException, OSError, ValueError
            erro = traduzir_erro_abertura(porta, e)
        else:
            with self._lock:
                self._porta = transporte
                self._erro_porta = None
            self._buffer.clear()
            self._ultimo_tick_em = None
            self._log_erro_porta(None)
            # 8N1 de propósito: a Urano pede 8N2, mas o receptor 8N1 só vê o
            # 2º stop bit como linha parada (o ACBr leu assim no campo).
            self._log_balanca(
                "abertura",
                lambda: f"porta {porta} aberta ({baud} 8N1), protocolo '{self._protocolo}'",
            )
            return
        with self._lock:
            self._erro_porta = erro.mensagem
        cru = f" ({erro.detail})" if erro.detail != erro.mensagem else ""
        self._log_erro_porta(erro.mensagem + cru)
        raise erro

    def _fechar(self, *, zerar: bool) -> None:
        """Fecha a porta. ``zerar`` também esquece o byte do Urano, a ordem do
        ``auto`` e a última leitura (troca de porta/velocidade, porta que
        caiu); a ociosidade e o ``liberar`` guardam tudo."""
        with self._lock:
            transporte = self._porta
            self._porta = None
            self._fechar_em = None
            if zerar:
                self._ultima_leitura = None
        self._buffer.clear()
        self._ultimo_frame = b""
        self._ultimo_tick_em = None
        if zerar:
            self._pedido_urano.zerar()
            self._ordem_auto.zerar()
            self._fallback_logado = False
            self._byte_estab_logado = False
        if transporte is not None:
            try:
                transporte.fechar()
            except Exception:  # noqa: BLE001
                log.debug("Falha ao fechar a porta da balança", exc_info=True)

    def _ao_cair(self, e: BaseException, onde: str) -> ErroBalanca:
        causa = e.__cause__ or e
        erro = erros.porta_caiu(self._nome_porta, str(causa)[:300] or type(causa).__name__)
        self._log_balanca(
            "erro_porta",
            lambda: f"porta {self._nome_porta} caiu {onde}: {causa!r}",
            nivel=logging.WARNING,
        )
        # Fecha para a próxima leitura reabrir — ou registrar o erro de abertura.
        self._fechar(zerar=True)
        with self._lock:
            self._erro_porta = erro.mensagem
        return erro

    def _purgar(self) -> None:
        """Descarta os bytes antigos (os já lidos e os parados no driver)."""
        self._buffer.clear()
        if self._porta is None:
            raise _PortaCaiu("porta fechada")
        try:
            self._porta.purgar()
        except Exception as e:
            raise _PortaCaiu(str(e)) from e

    def _escrever(self, dados: bytes) -> None:
        if self._porta is None:
            raise _PortaCaiu("porta fechada")
        try:
            self._porta.escrever(dados)
        except Exception as e:
            raise _PortaCaiu(str(e)) from e

    def _bombear(self) -> None:
        if self._porta is None:
            raise _PortaCaiu("porta fechada")
        try:
            pedaco = self._porta.ler_disponivel()
        except Exception as e:
            raise _PortaCaiu(str(e)) from e
        if pedaco:
            self._buffer += pedaco
            self._ultimo_rx = self._relogio()

    def _aguardar(self, ate: Callable[[bytearray], bool], timeout_s: float) -> bool:
        """Espera ``ate`` ficar verdadeiro sobre o buffer SEM consumi-lo — o
        ``_ler_do_buffer`` seguinte lê o frame inteiro, mesmo que a resposta
        tenha começado a chegar no fim do prazo. True = veio no prazo."""
        fim = self._relogio() + max(timeout_s, 0.0)
        while True:
            self._bombear()
            if ate(self._buffer):
                return True
            if self._relogio() >= fim:
                return False
            time.sleep(POLL_S)

    def _ler_do_buffer(
        self, timeout_s: float, fim_de_frame: Callable[[bytearray], bool] | None = None
    ) -> bytes:
        """O que chegar até ``timeout_s``. Termina mais cedo no fim de frame
        (CR/LF/ETX, ou o ``fim_de_frame`` do protocolo) seguido de 60 ms sem
        bytes novos. O ``fim_de_frame`` SOMA aos terminadores de sempre (o
        Urano 12 não tem nenhum): uma resposta Toledo ao pedido do Urano no
        ``auto`` ainda encerra a leitura em ~60 ms."""
        fim = self._relogio() + max(timeout_s, 0.0)
        while True:
            self._bombear()
            agora = self._relogio()
            buf = self._buffer
            tem_fim = (
                0x0A in buf
                or 0x0D in buf
                or 0x03 in buf
                or (fim_de_frame is not None and fim_de_frame(buf))
            )
            if tem_fim and agora - self._ultimo_rx >= SILENCIO_FIM_S:
                break
            if agora >= fim:
                break
            time.sleep(POLL_S)
        dados = bytes(self._buffer)
        self._buffer.clear()
        self._ultimo_frame = dados
        return dados

    # ── estratégias (só o I/O: pedido + leitura) ──────
    def _estrategia(self, id_: str, *, permitir_eot: bool, timeout_s: float) -> FrameBalanca | None:
        """``None`` = os bytes (ou a falta deles) não são deste protocolo."""
        if id_ == "urano":
            return self._ler_urano(timeout_s, permitir_eot=permitir_eot)
        if id_ == "toledo":
            return self._ler_com_enq(timeout_s, interpretar_toledo)
        if id_ == "filizola":
            return self._ler_com_enq(timeout_s, interpretar_filizola)
        if id_ == "elgin":
            return interpretar_elgin(self._ler_do_buffer(timeout_s))
        return interpretar_generico(self._ler_do_buffer(timeout_s))

    def _ler_urano(self, timeout_s: float, *, permitir_eot: bool) -> FrameBalanca | None:
        """Urano 12 (UDC CO / CO-E / POP). O frame não tem terminador (ver
        ``interpretar_urano``) e o pedido é ENQ ou EOT (ver ``PedidoUrano``).
        EOT como reserva só no ``urano`` explícito e no DETECTAR: no ``auto`` a
        balança pode ser de outra marca."""
        self._purgar()
        inicio = self._relogio()
        pedido = self._pedido_urano
        enviado = pedido.primeiro
        self._escrever(bytes([enviado]))
        if pedido.deve_tentar_eot(permitido=permitir_eot):
            # Prazo curto, sem consumir o buffer, até a resposta Urano COMEÇAR
            # (``ESC T1`` já no 2º byte): esperar o frame inteiro mandava EOT
            # com a resposta ao ENQ chegando (baud baixo, porta lenta).
            if not self._aguardar(parece_urano, min(timeout_s, CURTO_URANO_S)):
                enviado = PedidoUrano.EOT
                self._escrever(bytes([enviado]))
        restante = timeout_s - (self._relogio() - inicio)
        dados = self._ler_do_buffer(max(restante, 0.0), fim_de_frame=urano_completo)
        frame = interpretar_urano(dados)
        # Resposta Urano começada (mesmo cortada no prazo) já prova o byte: a
        # próxima leitura pede com ele e tem a janela inteira.
        pedido.registrar(
            respondeu_com=enviado if frame is not None or parece_urano(dados) else None,
            silencio=not dados,
        )
        return frame

    def _ler_com_enq(
        self, timeout_s: float, interpretar: Callable[[bytes], FrameBalanca | None]
    ) -> FrameBalanca | None:
        """Toledo Prix / Urano Std e Filizola: ENQ (0x05) e a resposta. Também
        encerra num frame Urano 12 completo: é o que a auto-correção lê quando
        o salvo é toledo e a balança é Urano."""
        self._purgar()
        self._escrever(b"\x05")
        return interpretar(self._ler_do_buffer(timeout_s, fim_de_frame=urano_completo))

    # ── leitura e DETECTAR ────────────────────────────
    def _ler_exclusivo(self, timeout_s: float) -> LeituraBalanca:
        """Porte de ``_lerPesoExclusivo``. Reconhecido — mesmo instável ou
        negativo — encerra a leitura: o protocolo é este, só não há peso ainda
        (no ``auto`` isso poupa até 4 pedidos)."""
        protocolo = self._protocolo or "auto"
        auto = protocolo not in PROTOCOLOS_FIXOS
        ids = self._ordem_auto.ordem if auto else [protocolo]
        frame: FrameBalanca | None = None
        lido_como: str | None = None
        pedido = ids[0]
        dados = b""
        parado = (
            self._ultimo_tick_em is None
            or self._relogio() - self._ultimo_tick_em > PURGA_PASSIVA_APOS_S
        )
        try:
            for id_ in ids:
                if id_ in PASSIVOS and ((auto and id_ != ids[0]) or (id_ == ids[0] and parado)):
                    # No ``auto``, um passivo depois de um pedido não pode
                    # herdar a resposta atrasada dele: pedaço de frame Urano
                    # sem ESC leria "193" como peso. E o primeiro, parado há
                    # mais de 1 s, não lê peso velho do driver.
                    self._purgar()
                # O frame velho não pode sobrar: a auto-correção abaixo o
                # releria como peso atual se a estratégia sair antes de ler.
                self._ultimo_frame = b""
                try:
                    f = self._estrategia(id_, permitir_eot=not auto, timeout_s=timeout_s)
                except _PortaCaiu:
                    raise
                except Exception:  # noqa: BLE001 - estratégia quebrada: tenta a próxima
                    log.exception("Falha na leitura da balança (%s)", id_)
                    continue
                quem = None if f is None else id_
                if f is None and not auto and id_ in ("toledo", "filizola"):
                    # Auto-correção: o DETECTAR antigo do PDV offline punha
                    # Toledo em quase tudo. Só formatos que não deixam dúvida
                    # (ver interpretar_compatibilidade). Não grava config.
                    compat = interpretar_compatibilidade(self._ultimo_frame)
                    if compat is not None:
                        f, quem = compat.frame, compat.protocolo
                        if not self._fallback_logado:
                            self._fallback_logado = True
                            amostra = self._ultimo_frame
                            self._log_balanca(
                                "fallback",
                                lambda: f"protocolo salvo '{id_}', mas a balança responde "
                                f"'{compat.protocolo}' — lendo assim; rode DETECTAR e SALVE: "
                                f"{descrever_bytes(amostra).hex}",
                                sempre=True,
                            )
                # Diagnóstico: vale o frame reconhecido; sem nenhum, o último
                # que trouxe bytes (um passivo vazio no fim do ``auto`` não
                # apaga a resposta que o Urano/Toledo recebeu).
                if f is not None or self._ultimo_frame:
                    pedido = id_
                    dados = self._ultimo_frame
                if f is not None:
                    frame, lido_como = f, quem
                    break
        except _PortaCaiu as e:
            raise self._ao_cair(e, "na leitura") from None
        self._ultimo_tick_em = self._relogio()
        if auto:
            self._ordem_auto.registrar(lido_como)
        leitura = LeituraBalanca(protocolo=pedido, lido_como=lido_como, frame=frame, dados=dados)
        with self._lock:
            self._ultima_leitura = leitura
        self._log_da_leitura(protocolo, frame, dados)
        return leitura

    def _log_da_leitura(self, protocolo: str, frame: FrameBalanca | None, dados: bytes) -> None:
        if frame is None:
            if not dados:
                # Silêncio: cabo, porta, byte de pedido — ou a Urano em sobrecarga.
                self._log_balanca(
                    f"sem_resposta:{protocolo}",
                    lambda: f"sem resposta da balança (protocolo '{protocolo}')",
                )
            else:

                def msg() -> str:
                    dica = protocolo_do_frame(dados)
                    extra = "" if dica is None else f", parece '{dica}'"
                    return (
                        f"frame não reconhecido (protocolo '{protocolo}'{extra}): "
                        f"{descrever_bytes(dados).hex}"
                    )

                self._log_balanca(f"nao_reconhecido:{protocolo}", msg)
            return
        estab = frame.byte_estabilidade
        if estab is not None and estab not in (0x2A, 0x20) and not self._byte_estab_logado:
            self._byte_estab_logado = True
            self._log_balanca(
                "byte_estab",
                lambda: f"Urano 12 com byte de estabilidade {estab:02x} (manual: 2A) — "
                f"tratado como estável: {descrever_bytes(dados).hex}",
                sempre=True,
            )

    def _detectar_exclusivo(self, cancelamento: threading.Event | None) -> Deteccao:
        """Porte de ``_detectarExclusivo``: cada protocolo, do mais específico
        ao mais genérico, 2 pedidos de 700 ms; vale o primeiro que reconhecer
        os dois. Antes bastava o 1º número > 0 de um pedido só, e a Urano UDC
        saía como "toledo"."""
        # Pergunta de novo, do zero: um byte de pedido lembrado de outra
        # balança (porta aberta há dias, balança trocada) pularia o EOT.
        self._pedido_urano.zerar()
        self._ordem_auto.zerar()
        self._garantir_aberta()
        amostra: bytes | None = None
        try:
            for id_ in OrdemAuto.BASE:
                frame: FrameBalanca | None = None
                reconhecidos = 0
                for _ in range(TENTATIVAS_DETECTAR):
                    if cancelamento is not None and cancelamento.is_set():
                        log.info("DETECTAR da balança cancelado (o navegador saiu)")
                        raise erros.cancelado()
                    # Os passivos não purgam sozinhos: sem isto a resposta
                    # atrasada ao pedido da estratégia anterior seria
                    # creditada a eles.
                    if id_ in PASSIVOS:
                        self._purgar()
                    self._ultimo_frame = b""
                    try:
                        frame = self._estrategia(
                            id_, permitir_eot=True, timeout_s=PRAZO_DETECTAR_S
                        )
                    except _PortaCaiu:
                        raise
                    except Exception:  # noqa: BLE001
                        log.exception("Falha no DETECTAR da balança (%s)", id_)
                        frame = None
                    if self._ultimo_frame:
                        amostra = self._ultimo_frame
                    if frame is None:
                        break
                    reconhecidos += 1
                if reconhecidos == TENTATIVAS_DETECTAR and frame is not None:
                    dados = self._ultimo_frame
                    achado = frame
                    self._log_balanca(
                        "deteccao",
                        lambda: f"DETECTAR: '{id_}' ({descrever_frame(achado)}) — "
                        f"{descrever_bytes(dados).hex}",
                        sempre=True,
                    )
                    return Deteccao(id_, frame, dados)
        except _PortaCaiu as e:
            raise self._ao_cair(e, "no DETECTAR") from None
        finally:
            self._ultimo_tick_em = self._relogio()
        self._log_balanca(
            "deteccao",
            lambda: "DETECTAR: nenhum protocolo reconheceu "
            + ("(nenhum byte)" if amostra is None else descrever_bytes(amostra).hex),
            sempre=True,
        )
        return Deteccao(None, None, amostra or b"")

    # ── log ───────────────────────────────────────────
    def _log_balanca(
        self,
        chave: str,
        msg: Callable[[], str],
        *,
        sempre: bool = False,
        nivel: int = logging.INFO,
    ) -> None:
        """Uma linha por ``chave`` a cada 60 s (a primeira sai na hora;
        ``sempre`` ignora o limite). ``msg`` só é montada quando a linha vai
        sair (o hex e a dica de protocolo custam a cada leitura)."""
        agora = self._relogio()
        antes = self._log_quando.get(chave)
        if not sempre and antes is not None and agora - antes < LOG_INTERVALO_S:
            return
        self._log_quando[chave] = agora
        log.log(nivel, "[balanca] %s", msg())

    def _log_erro_porta(self, erro: str | None) -> None:
        """Erro de abertura vai ao log só na transição (ok→erro, erro→ok,
        mensagem mudou): com a porta ocupada a web tenta de novo sem parar."""
        if erro == self._erro_logado:
            return
        anterior = self._erro_logado
        self._erro_logado = erro
        if erro is None:
            if anterior is not None:
                log.info("[balanca] porta %s voltou a abrir", self._nome_porta)
            return
        log.warning("[balanca] %s", erro)
