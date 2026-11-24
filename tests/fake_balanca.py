"""Dublês da porta serial da balança (sem COM de verdade).

``TransporteFalso`` faz o papel da balança do outro lado do cabo, com
roteiro: responde a um byte de pedido (ENQ/EOT) depois de um atraso, manda
um stream contínuo (Elgin), fica muda, manda lixo, ou tem o cabo puxado no
meio (as chamadas passam a levantar o ``SerialException`` que o pyserial do
Windows levanta).

``AbridorFalso`` é o ``abrir_fn`` do leitor: entrega um ``TransporteFalso``
por abertura ou levanta os erros de abertura com os textos reais do pyserial
no Windows em português.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable

import serial

ENQ = 0x05
EOT = 0x04
STX = 0x02
ETX = 0x03
ESC = 0x1B

#: Textos reais do pyserial 3.5 no Windows (serialwin32.open / ClearCommError).
ERRO_EM_USO = "could not open port 'COM3': PermissionError(13, 'Acesso negado.', None, 5)"
ERRO_EM_USO_EN = "could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)"
ERRO_INEXISTENTE = (
    "could not open port 'COM9': FileNotFoundError(2, 'O sistema não pode encontrar o "
    "arquivo especificado.', None, 2)"
)
ERRO_INEXISTENTE_EN = (
    "could not open port 'COM9': FileNotFoundError(2, 'The system cannot find the file "
    "specified.', None, 2)"
)
ERRO_DISPOSITIVO = (
    "could not open port 'COM3': OSError(22, 'Um dispositivo conectado ao sistema não está "
    "funcionando.', None, 31)"
)
ERRO_CABO_LEITURA = (
    "ClearCommError failed (PermissionError(13, 'O dispositivo não reconhece o comando.', "
    "None, 22))"
)
ERRO_CABO_ESCRITA = (
    "WriteFile failed (PermissionError(13, 'O dispositivo não reconhece o comando.', None, 22))"
)


def stx_etx(corpo: str) -> bytes:
    return bytes([STX]) + corpo.encode("latin-1") + bytes([ETX])


def urano12(peso: str = "   475", unidade: str = " g", estab: str = "*") -> bytes:
    """Frame Urano 12 de 46 bytes (UDC CO / CO-E), como no teste do Dart."""
    partes = [
        bytes([ESC]) + b"T1",
        bytes([ESC]) + b"A13",
        bytes([ESC]) + b"N0",
        bytes([ESC]) + b"S2",
        bytes([ESC]) + b"D4",
        bytes([ESC]) + b"Q193",
        bytes([ESC]) + b"B",
        f" {estab} PESO: {peso}{unidade}".encode("latin-1"),
        bytes([ESC]) + b"E",
        bytes([ESC]) + b"P01",
    ]
    return b"".join(partes)


#: Resposta a um byte de pedido: bytes de uma vez, ou pedaços com atraso
#: próprio [(atraso_s, bytes), ...], ou uma função que decide na hora.
Resposta = bytes | list[tuple[float, bytes]] | Callable[["TransporteFalso"], bytes | None]


class TransporteFalso:
    def __init__(
        self,
        porta: str = "COM3",
        baud: int = 9600,
        *,
        respostas: dict[int, Resposta] | None = None,
        atraso: float = 0.01,
        fluxo: tuple[float, bytes] | None = None,
    ) -> None:
        self.porta = porta
        self.baud = baud
        self.respostas: dict[int, Resposta] = dict(respostas or {})
        self.atraso = atraso
        self._lock = threading.Lock()
        self._chegando: list[tuple[float, bytes]] = []
        self._fluxo: tuple[float, bytes] | None = None
        self._fluxo_desde = 0.0
        self._fluxo_entregues = 0
        self.escritos: list[int] = []
        self.purgas = 0
        self.leituras = 0
        self.fechado = False
        self.cabo_puxado = False
        self.threads: set[int] = set()
        #: Bloqueia o escrever até ser solto (vigia).
        self.travar_escrita: threading.Event | None = None
        if fluxo is not None:
            self.mudar_fluxo(*fluxo)

    # ── roteiro ───────────────────────────────────────
    def mudar_fluxo(self, intervalo: float, frame: bytes) -> None:
        """Stream contínuo: ``frame`` a cada ``intervalo`` (Elgin, genérico)."""
        with self._lock:
            self._fluxo = (intervalo, frame)
            self._fluxo_desde = time.monotonic()
            self._fluxo_entregues = 0

    def parar_fluxo(self) -> None:
        with self._lock:
            self._fluxo = None

    def chegar(self, dados: bytes, atraso: float = 0.0) -> None:
        """Bytes que chegam sozinhos (ex.: peso velho parado no driver)."""
        with self._lock:
            self._chegando.append((time.monotonic() + atraso, dados))

    def puxar_cabo(self) -> None:
        self.cabo_puxado = True

    # ── interface do Transporte ───────────────────────
    def _anotar(self) -> None:
        self.threads.add(threading.get_ident())
        if self.fechado:
            raise serial.PortNotOpenError()

    def escrever(self, dados: bytes) -> None:
        self._anotar()
        if self.travar_escrita is not None:
            self.travar_escrita.wait(10)
        if self.cabo_puxado:
            raise serial.SerialException(ERRO_CABO_ESCRITA)
        agora = time.monotonic()
        for b in dados:
            self.escritos.append(b)
            resp = self.respostas.get(b)
            if callable(resp):
                resp = resp(self)
            if not resp:
                continue
            with self._lock:
                if isinstance(resp, (bytes, bytearray)):
                    self._chegando.append((agora + self.atraso, bytes(resp)))
                else:
                    for atraso, pedaco in resp:
                        self._chegando.append((agora + atraso, pedaco))

    def _fluxo_vencido(self, agora: float) -> bytes:
        if self._fluxo is None:
            return b""
        intervalo, frame = self._fluxo
        n = int((agora - self._fluxo_desde) / intervalo)
        novos = max(0, n - self._fluxo_entregues)
        self._fluxo_entregues = max(self._fluxo_entregues, n)
        return frame * novos

    def ler_disponivel(self) -> bytes:
        self._anotar()
        if self.cabo_puxado:
            raise serial.SerialException(ERRO_CABO_LEITURA)
        self.leituras += 1
        agora = time.monotonic()
        with self._lock:
            prontos = [d for quando, d in self._chegando if quando <= agora]
            self._chegando = [(q, d) for q, d in self._chegando if q > agora]
            fluxo = self._fluxo_vencido(agora)
        # Ordem de chegada aproximada: respostas e o stream, nesta ordem.
        return b"".join(prontos) + fluxo

    def purgar(self) -> None:
        self._anotar()
        if self.cabo_puxado:
            raise serial.SerialException(ERRO_CABO_LEITURA)
        self.purgas += 1
        agora = time.monotonic()
        with self._lock:
            self._chegando = [(q, d) for q, d in self._chegando if q > agora]
            self._fluxo_vencido(agora)  # o que já chegou do stream é descartado

    def fechar(self) -> None:
        self.fechado = True


class AbridorFalso:
    """``abrir_fn`` do leitor: um ``TransporteFalso`` novo por abertura."""

    def __init__(self, fabrica: Callable[[str, int], TransporteFalso] | None = None) -> None:
        self.fabrica = fabrica or (lambda porta, baud: TransporteFalso(porta, baud))
        self.aberturas: list[tuple[str, int]] = []
        self.transportes: list[TransporteFalso] = []
        #: Erros a levantar nas próximas aberturas (um por abertura).
        self.erros: list[BaseException] = []

    def __call__(self, porta: str, baud: int) -> TransporteFalso:
        self.aberturas.append((porta, baud))
        if self.erros:
            raise self.erros.pop(0)
        t = self.fabrica(porta, baud)
        self.transportes.append(t)
        return t

    @property
    def ultimo(self) -> TransporteFalso:
        return self.transportes[-1]


def balanca_toledo(**kw) -> AbridorFalso:
    """Toledo Prix: ENQ → ``STX 01234 ETX`` (1,234 kg)."""
    return AbridorFalso(
        lambda porta, baud: TransporteFalso(
            porta, baud, respostas={ENQ: kw.get("frame", stx_etx("01234"))}, **_kw(kw)
        )
    )


def balanca_urano_eot(**kw) -> AbridorFalso:
    """UDC 30000/5 do fórum do ACBr: só responde ao EOT."""
    return AbridorFalso(
        lambda porta, baud: TransporteFalso(
            porta, baud, respostas={EOT: kw.get("frame", urano12())}, **_kw(kw)
        )
    )


def balanca_urano_enq(**kw) -> AbridorFalso:
    """UDC CO-E do cliente: responde ao ENQ."""
    return AbridorFalso(
        lambda porta, baud: TransporteFalso(
            porta, baud, respostas={ENQ: kw.get("frame", urano12())}, **_kw(kw)
        )
    )


def balanca_muda() -> AbridorFalso:
    return AbridorFalso()


def balanca_lixo() -> AbridorFalso:
    return AbridorFalso(
        lambda porta, baud: TransporteFalso(porta, baud, respostas={ENQ: b"\xff\xfe#@! \r\n"})
    )


def balanca_elgin(frame: bytes = b"P000.500\r", intervalo: float = 0.03) -> AbridorFalso:
    return AbridorFalso(
        lambda porta, baud: TransporteFalso(porta, baud, fluxo=(intervalo, frame))
    )


def _kw(kw: dict) -> dict:
    return {k: v for k, v in kw.items() if k in ("atraso",)}
