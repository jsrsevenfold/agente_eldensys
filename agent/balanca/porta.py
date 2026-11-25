"""Porta serial da balança (pyserial) e a tradução dos erros do Windows.

A porta abre em 8N1 sem controle de fluxo; só a velocidade muda. A Urano
pede 8N2, mas o receptor 8N1 só vê o 2º stop bit como linha parada (o ACBr
e o PDV offline leem assim no campo). ``timeout=0`` deixa a leitura sem
bloqueio: quem espera é o ``leitor`` (``ler_disponivel`` em laço curto), que
assim confere prazo e fim de frame sozinho.

A interface mínima (``escrever``/``ler_disponivel``/``purgar``/``fechar``) é
o que o leitor usa: os testes trocam a porta por um dublê com roteiro.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

import serial
import serial.tools.list_ports

from . import erros

log = logging.getLogger("eldensys.agent.balanca.porta")

#: Escrever um byte numa porta sem fluxo não espera nada; mais que isso é
#: driver preso — vira erro de porta em vez de prender a thread.
WRITE_TIMEOUT_S = 0.5
#: Buffers do driver (Windows): o Elgin manda o peso sem parar.
BUFFER_BYTES = 4096


class Transporte(Protocol):
    def escrever(self, dados: bytes) -> None: ...

    def ler_disponivel(self) -> bytes:
        """O que chegou, sem esperar (``b""`` = nada). Erro = porta caiu."""
        ...

    def purgar(self) -> None: ...

    def fechar(self) -> None: ...


class PortaSerial:
    """``serial.Serial`` aberto, com a interface do ``Transporte``."""

    def __init__(self, s: serial.SerialBase) -> None:
        self._s = s

    def escrever(self, dados: bytes) -> None:
        # Sem flush(): no Windows ele dorme 50 ms enquanto houver byte na
        # saída, e o prazo curto do Urano (250 ms) é contado daqui. O write
        # já espera o byte sair (write_timeout).
        self._s.write(dados)

    def ler_disponivel(self) -> bytes:
        # in_waiting chama ClearCommError: com o adaptador USB arrancado ele
        # levanta SerialException — é assim que a queda aparece.
        n = self._s.in_waiting
        return self._s.read(n) if n else b""

    def purgar(self) -> None:
        self._s.reset_input_buffer()

    def fechar(self) -> None:
        try:
            self._s.close()
        except Exception:  # noqa: BLE001 - fechar nunca derruba quem chama
            log.debug("Falha ao fechar a porta da balança", exc_info=True)


def abrir_porta(porta: str, baud: int) -> PortaSerial:
    """Abre ``porta`` (``COM3``) em ``baud`` 8N1 sem fluxo.

    Levanta o erro cru do pyserial (``SerialException``/``ValueError``): quem
    chama traduz com ``traduzir_erro_abertura``. ``serial_for_url`` aceita
    também ``loop://`` (só o teste usa; a rota só aceita ``COMn``).
    """
    s = serial.serial_for_url(
        porta,
        baudrate=baud,
        bytesize=serial.EIGHTBITS,
        parity=serial.PARITY_NONE,
        stopbits=serial.STOPBITS_ONE,
        timeout=0,
        write_timeout=WRITE_TIMEOUT_S,
        xonxoff=False,
        rtscts=False,
        dsrdtr=False,
        do_not_open=True,
    )
    s.open()
    if hasattr(s, "set_buffer_size"):  # só no Windows
        try:
            s.set_buffer_size(rx_size=BUFFER_BYTES, tx_size=BUFFER_BYTES)
        except Exception:  # noqa: BLE001 - o padrão do driver também serve
            log.debug("set_buffer_size recusado na %s", porta, exc_info=True)
    return PortaSerial(s)


_EM_USO = ("permissionerror", "acesso negado", "access is denied", "access denied")
_INEXISTENTE = (
    "filenotfounderror",
    "cannot find",
    "file specified",
    "arquivo especificado",
    "não pode encontrar",
    "nao pode encontrar",
)


def traduzir_erro_abertura(porta: str, e: BaseException) -> erros.ErroBalanca:
    """Erro do pyserial ao abrir → erro da rota local.

    O pyserial do Windows monta o texto com o ``WinError`` no idioma do
    sistema: ``could not open port 'COM3': PermissionError(13, 'Acesso
    negado.', None, 5)`` (outro programa segura a porta) ou
    ``FileNotFoundError(2, 'O sistema não pode encontrar o arquivo
    especificado.', None, 2)`` (porta que não existe). O resto é ``porta_falhou``
    com o texto cru no ``detail`` (vai para o suporte).
    """
    texto = str(e) or type(e).__name__
    baixo = texto.lower()
    detail = texto[:300]
    if isinstance(e, PermissionError) or any(t in baixo for t in _EM_USO):
        return erros.porta_em_uso(porta, detail)
    if isinstance(e, FileNotFoundError) or any(t in baixo for t in _INEXISTENTE):
        return erros.porta_inexistente(porta, detail)
    return erros.porta_falhou(porta, detail)


_NUMERO_COM_RE = re.compile(r"^COM(\d+)$", re.IGNORECASE)


def _ordem_da_porta(nome: str) -> tuple[int, int, str]:
    m = _NUMERO_COM_RE.match(nome)
    return (0, int(m.group(1)), "") if m else (1, 0, nome)


def listar_portas() -> list[dict]:
    """Portas COM deste computador, pelo número (COM2 antes de COM10)."""
    portas = [
        {"porta": str(p.device), "descricao": str(p.description or "")}
        for p in serial.tools.list_ports.comports()
        if p.device
    ]
    portas.sort(key=lambda p: _ordem_da_porta(p["porta"]))
    return portas
