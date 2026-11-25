"""Interpretação PURA dos frames de balança serial: sem porta, sem I/O.

Porte fiel de ``eldensys_mobile/lib/core/services/balanca_protocolos.dart``
(o PDV offline lê as mesmas balanças há meses; os 70 casos do teste do Dart
estão traduzidos em ``tests/test_balanca_protocolos.py``). Mesmos nomes, em
``snake_case``, para o diff entre os dois ser mecânico.

Cada ``interpretar_x`` responde "estes bytes são do protocolo X?":

- ``None``: não são (lixo, outro protocolo, nada) e o chamador tenta outro;
- ``FrameBalanca``: são, com o estado lido (peso, zero, instável, negativo ou
  sobrecarga). Instável é RECONHECIDO, não ``None``: é o que deixa o modo
  ``auto`` parar no protocolo certo e o DETECTAR funcionar com a mão no prato.

Nenhum parser junta dígitos de campos diferentes. Foi isso que fez o frame da
Urano UDC-CO (Urano 12) virar 11302419347,501 kg no Toledo antigo: ele apagava
tudo que não era dígito e colava ``ESC T1 ESC A13 … ESC Q193`` com os 475 g e
o ``P01`` do fim do frame.

Armadilhas do porte Dart → Python (cada uma tem teste próprio):

- ``roundToDouble`` do Dart afasta a metade do zero; o ``round`` do Python
  arredonda a metade para o par (``arredondar_gramas``);
- o ``RegExp`` do Dart é o do JavaScript: ``\\d`` e ``\\b`` só ASCII (aqui
  ``re.ASCII``); ``\\s`` cobre TAB, LF, VT, FF, CR, espaço e NBSP (``_WS``,
  explícito: o ``\\s`` do Python com ``re.ASCII`` perde o NBSP e sem ele
  ganha 0x1C–0x1F e 0x85); ``.`` não casa CR nem LF (aqui ``[^\\r\\n]``); ``$``
  é só o fim do texto (aqui ``\\Z``: o ``$`` do Python também casa antes de um
  ``\\n`` final);
- ``trim()`` do Dart tira o White_Space do Unicode (0x85 e 0xA0 inclusive) e
  NÃO tira 0x1C–0x1F, que o ``str.strip()`` tira (``_trim_dart``);
- ``String.fromCharCodes`` de bytes = ``latin-1`` (um byte, um caractere);
- ``allMatches(...).last`` = o último do ``finditer``; ``double.tryParse`` =
  ``_double_try_parse`` (``None`` em vez de exceção).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import NamedTuple

Bytes = bytes | bytearray | Iterable[int]

#: Teto de quantidade de UM item de venda (``kQuantidadeMaxItem`` do PDV
#: offline): acima disso não é peso de balança, é campo colado ou lixo.
QUANTIDADE_MAX_ITEM = 99999


def _arredondar_dart(x: float) -> float:
    """``double.roundToDouble()`` do Dart: inteiro mais próximo, a metade
    AFASTA do zero (2,5 → 3; −2,5 → −3). O ``round`` do Python daria 2.

    Sem o ``floor(x + 0,5)`` ingênuo: 0,49999999999999994 + 0,5 vira 1,0 em
    ponto flutuante, e o Dart dá 0. A parte fracionária de um double é exata.
    """
    if not math.isfinite(x):
        return x
    a = abs(x)
    r = math.floor(a)
    if a - r >= 0.5:
        r += 1
    return math.copysign(float(r), x)


def arredondar_gramas(v: float) -> float:
    """``arredondarGramas`` do PDV offline: 3 casas (gramas), metade longe do zero."""
    return _arredondar_dart(v * 1000) / 1000


class EstadoFrame(str, Enum):
    """Estado de um frame reconhecido. O valor é o ``estado`` da rota local."""

    peso = "peso"
    zero = "zero"
    instavel = "instavel"
    negativo = "negativo"
    sobrecarga = "sobrecarga"


@dataclass(frozen=True)
class FrameBalanca:
    estado: EstadoFrame
    #: Peso em kg. Só tem sentido em ``peso`` e ``negativo`` (no negativo é o
    #: módulo, como no Dart).
    kg: float = 0.0
    #: Urano 12: o byte que o frame trouxe na posição da estabilidade (25). O
    #: manual diz ``*``; outro valor ainda conta como estável (regra do ACBr),
    #: mas vale um registro no log: o mesmo cabo mostrou ``18`` onde o manual
    #: diz ``1B``.
    byte_estabilidade: int | None = None


@dataclass(frozen=True)
class LeituraBalanca:
    """Uma leitura do leitor, para diagnóstico (status, TESTAR e log)."""

    #: Protocolo que fez o pedido (o configurado, ou a vez do ``auto``).
    protocolo: str
    #: Quem reconheceu o frame. Difere de ``protocolo`` na auto-correção
    #: (salvo ``toledo``, balança respondendo Urano 12). ``None`` = ninguém.
    lido_como: str | None
    frame: FrameBalanca | None
    #: Os bytes da leitura inteira (o ``bytes`` do Dart), não só o último
    #: pedaço que a porta entregou.
    dados: bytes = b""


_STX = 0x02
_ETX = 0x03
_CR = 0x0D
_LF = 0x0A

#: ``\s`` do JavaScript (e do Dart) dentro do latin-1: TAB, LF, VT, FF, CR,
#: espaço e NBSP. Nem o ``\s`` do Python com ``re.ASCII`` (sem NBSP) nem sem
#: ele (com 0x1C–0x1F e 0x85) é igual.
_WS = r"[\t\n\x0b\x0c\r \xa0]"

#: ``trim()`` do Dart dentro do latin-1 (White_Space do Unicode): o
#: ``str.strip()`` também tiraria 0x1C–0x1F, que o Dart deixa.
_TRIM_DART = "\t\n\x0b\x0c\r \x85\xa0"


def _trim_dart(s: str) -> str:
    return s.strip(_TRIM_DART)


def _bytes(b: Bytes) -> bytes:
    return b if isinstance(b, bytes) else bytes(b)


def _texto(b: Bytes) -> str:
    """``String.fromCharCodes`` de bytes: um byte, um caractere (latin-1)."""
    return _bytes(b).decode("latin-1")


#: Gramática do ``double.parse`` do Dart (sem NaN/Infinity, que nenhum campo
#: de balança traz): sinal, dígitos com ponto opcional, expoente opcional.
_DOUBLE_DART_RE = re.compile(r"[+\-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?\Z", re.ASCII)


def _double_try_parse(s: str) -> float | None:
    """``double.tryParse``: ``None`` em vez de exceção, espaços nas pontas
    ignorados. Recusa o que o ``float`` do Python aceita a mais (``1_000``,
    ``inf``, ``nan``)."""
    t = _trim_dart(s)
    if not _DOUBLE_DART_RE.match(t):
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _peso(kg: float, byte_estabilidade: int | None = None) -> FrameBalanca | None:
    """Peso positivo vira ``peso``; zero vira ``zero``. Acima do teto de um item
    de venda não é peso de balança, é campo colado ou lixo: ``None`` (não
    reconhecido), também no DETECTAR e no diagnóstico."""
    if kg == 0:
        return FrameBalanca(EstadoFrame.zero, byte_estabilidade=byte_estabilidade)
    if kg > QUANTIDADE_MAX_ITEM:
        return None
    return FrameBalanca(
        EstadoFrame.peso, kg=arredondar_gramas(kg), byte_estabilidade=byte_estabilidade
    )


# ─── Urano 12 ────────────────────────────────────────────────────────────

#: Urano 12 (UDC CO / CO-E / POP / 6000): frame fixo de 46 bytes montado com
#: comandos de impressora de etiqueta, sem STX/ETX/CR e sem checksum:
#: ``ESC T1 ESC A13 ESC N0 ESC S2 ESC D4 ESC Q193 ESC B`` + `` * PESO:`` +
#: sinal + peso (6 caracteres) + ``kg``|`` g`` + ``ESC E ESC P01``. O Urano C
#: troca ``PESO:`` por ``PESO(L):`` e acrescenta a tara, que não é capturada.
#:
#: A âncora é o ``ESC B`` antes do peso: sem ela uma balança genérica que
#: imprime "PESO: 1,234 kg" seria tomada por Urano (e sempre instável). ESC
#: ou CAN porque o PDV do cliente mostrou ``18`` onde o manual diz ``1B``. O
#: ``PESO L:`` do POP-S (USE-CB2) fica de fora de propósito.
_RE_URANO = re.compile(
    r"[\x18\x1B]B ([^\r\n]) PESO(?:\(L\))?:([ +\-]) *(\d{1,6}(?:[.,]\d{1,3})?) *([kK][gG]|[gG])",
    re.ASCII,
)
_RE_URANO_MARCA = re.compile(r"[\x18\x1B]B ([^\r\n]) PESO(?:\(L\))?:", re.ASCII)

#: Comando de impressora do Urano 12 (ESC/CAN + letra) no buffer: nenhum
#: protocolo de texto deve tirar número daí (o Elgin lia o "1" de ``ESC T1``).
_RE_COMANDO_URANO = re.compile(r"[\x18\x1B][A-Z]", re.ASCII)


def parece_urano(b: Bytes) -> bool:
    return _RE_COMANDO_URANO.search(_texto(b)) is not None


def interpretar_urano(b: Bytes) -> FrameBalanca | None:
    b = _bytes(b)
    if not b:
        return None
    txt = _texto(b)
    completos = list(_RE_URANO.finditer(txt))
    if not completos:
        # Cabeçalho do Urano sem peso utilizável: frame cortado no fim do prazo
        # ou visor com mensagem (HHHHHH = sobrecarga, LLLLLL = abaixo do
        # mínimo). É Urano, só não há o que pesar.
        marcas = list(_RE_URANO_MARCA.finditer(txt))
        if not marcas:
            return None
        ini = marcas[-1].end()
        campo = txt[ini : min(ini + 7, len(txt))]
        if "H" in campo:
            return FrameBalanca(EstadoFrame.sobrecarga)
        if "L" in campo:
            return FrameBalanca(EstadoFrame.negativo)
        return FrameBalanca(EstadoFrame.instavel)
    # O último frame completo: o buffer pode trazer uma resposta atrasada, um
    # frame pela metade no começo ou um cortado no fim.
    m = completos[-1]
    estab = ord(m.group(1))
    if estab == 0x20:
        return FrameBalanca(EstadoFrame.instavel, byte_estabilidade=estab)
    valor = _double_try_parse(m.group(3).replace(",", "."))
    if valor is None:
        return None
    # A unidade é o byte 39 do frame (``k`` ou espaço), não o separador: um
    # visor em kg sem casas manda "150kg", e isso são 150 kg.
    kg = valor if m.group(4).lower() == "kg" else valor / 1000
    if kg != 0 and m.group(2) == "-":
        return FrameBalanca(
            EstadoFrame.negativo, kg=arredondar_gramas(kg), byte_estabilidade=estab
        )
    return _peso(kg, byte_estabilidade=estab)


def urano_completo(b: Bytes) -> bool:
    """Fim de frame do Urano 12, que não tem terminador: peso e unidade
    completos, ou cabeçalho mais o campo inteiro (visor com mensagem)."""
    txt = _texto(b)
    if _RE_URANO.search(txt):
        return True
    marcas = list(_RE_URANO_MARCA.finditer(txt))
    return bool(marcas) and len(txt) - marcas[-1].end() >= 9


# ─── Toledo / Filizola (e Urano Std01–Std05) ────────────────────────────

_RE_CORPO_NUMERICO = re.compile(
    rf"\A([+\-])?{_WS}*(\d{{1,7}}|\d{{0,5}}[.,]\d{{1,3}})\Z", re.ASCII
)
_RE_CORPO_STATUS = re.compile(r"\A([+\-])?([INSCE])\2*(?:,\2+)?\Z", re.ASCII)
_RE_KG_FINAL = re.compile(rf"{_WS}*kg\Z", re.ASCII | re.IGNORECASE)


def _imprimivel(corpo: bytes) -> str | None:
    for c in corpo:
        if c < 0x20 or c > 0x7E:
            return None
    return _texto(corpo)


def _corpo_delimitado(b: bytes) -> str | None:
    """Corpo de um frame Toledo/Filizola, sem nunca juntar dois frames:

    1. o último STX que tem ETX depois dele, até o 1º ETX (como o
       ``PosLast(STX)`` do ACBr, mas pulando um STX cortado no fim do
       buffer): ``34 ETX STX 01234 ETX`` e ``STX 01234 ETX STX 012`` dão
       ``01234``;
    2. senão o último STX com CR/LF depois (Protocolo C / P05 do ACBr);
    3. senão a última linha completa (terminada em CR/LF) que for texto.

    Byte não imprimível no corpo = não é deste protocolo.
    """
    stxs = [i for i in range(len(b) - 1, -1, -1) if b[i] == _STX]
    for s in stxs:
        e = b.find(_ETX, s + 1)
        if e > s:
            return _imprimivel(b[s + 1 : e])
    for s in stxs:
        for e in range(s + 1, len(b)):
            if b[e] == _CR or b[e] == _LF:
                return _imprimivel(b[s + 1 : e])
    for linha in reversed(_linhas_completas(b)):
        texto = _imprimivel(linha)
        if texto is not None and _trim_dart(texto):
            return texto
    return None


def _linhas_completas(b: bytes) -> list[bytes]:
    """Linhas terminadas em CR/LF, do começo ao fim, sem a sobra depois do
    último terminador (ainda está chegando). Bytes de controle nas pontas da
    linha saem (eco do ENQ, NUL de ruído): o parser antigo os apagava."""
    linhas: list[bytes] = []
    ini = 0
    for i in range(len(b)):
        if b[i] != _CR and b[i] != _LF:
            continue
        a, z = ini, i
        while a < z and b[a] < 0x20:
            a += 1
        while z > a and b[z - 1] < 0x20:
            z -= 1
        if z > a:
            linhas.append(b[a:z])
        ini = i + 1
    return linhas


def _interpretar_corpo(corpo: str) -> FrameBalanca | None:
    """Corpo Toledo/Filizola/Urano Std: número (com separador é kg; inteiro são
    gramas, ÷ 1000) ou sequência de status (I instável, N negativo/subcarga,
    S sobrecarga, C/E balança não pronta). Aceita o preenchimento com espaços
    do Std01 ("  0.475", "- 0.100") mas não dígitos de campos diferentes: o
    teto de 7 dígitos barra status-word + peso + tara colados."""
    c = _RE_KG_FINAL.sub("", _trim_dart(corpo), count=1)
    if not c:
        return None
    n = _RE_CORPO_NUMERICO.search(c)
    if n is not None:
        digitos = n.group(2)
        kg = (
            _double_try_parse(digitos.replace(",", "."))
            if "." in digitos or "," in digitos
            else int(digitos) / 1000
        )
        if kg is None:
            return None
        if kg != 0 and n.group(1) == "-":
            return FrameBalanca(EstadoFrame.negativo, kg=arredondar_gramas(kg))
        return _peso(kg)
    s = _RE_CORPO_STATUS.search(c)
    if s is None:
        return None
    letra = s.group(2)
    if letra == "I":
        # ``-III,III`` do self-checkout Toledo é o "peso negativo" do ACBr.
        return FrameBalanca(
            EstadoFrame.negativo if s.group(1) == "-" else EstadoFrame.instavel
        )
    if letra == "N":
        return FrameBalanca(EstadoFrame.negativo)
    if letra == "S":
        return FrameBalanca(EstadoFrame.sobrecarga)
    # C = captura inicial de zero, E = erro de calibração: não pronta.
    return FrameBalanca(EstadoFrame.instavel)


def interpretar_toledo(b: Bytes) -> FrameBalanca | None:
    """Toledo Prix (``STX PPPPP ETX`` + IIIII/NNNNN/SSSSS), variantes com CR e
    os protocolos Std01–Std05 da Urano. Toledo protocolo A, P03 com
    status-word e Ethernet não são lidos (davam lixo com o parser antigo;
    agora dão ``None``)."""
    corpo = _corpo_delimitado(_bytes(b))
    return None if corpo is None else _interpretar_corpo(corpo)


def interpretar_filizola(b: Bytes) -> FrameBalanca | None:
    """Filizola: o ACBr lê no mesmo layout do Toledo (``STX PPPPP ETX`` +
    IIIII/NNNNN/SSSSS). Os cabeçalhos ``PS``/``ES`` (estável) e ``II``
    (instável) vêm de um comentário antigo do PDV offline, sem fonte: ficam só
    como tolerância."""
    corpo = _corpo_delimitado(_bytes(b))
    if corpo is None:
        return None
    c = _trim_dart(corpo)
    if c.startswith("II"):
        return FrameBalanca(EstadoFrame.instavel)
    if c.startswith("PS") or c.startswith("ES"):
        return _interpretar_corpo(c[2:])
    return _interpretar_corpo(c)


# ─── Passivos (stream contínuo) ─────────────────────────────────────────

#: Número com unidade opcional numa linha de texto ("000.500", "+00.500kg",
#: "475 g"). A unidade só vale se não vier colada em outra letra.
_RE_TOKEN_PESO = re.compile(
    rf"([+\-]?\d+(?:[.,]\d+)?)(?:{_WS}*([kK][gG]|[gG])(?![a-zA-Z]))?", re.ASCII
)
_RE_TOKEN_COM_UNIDADE = re.compile(
    rf"(\d+(?:[.,]\d+)?){_WS}*([kK][gG]|[gG])(?![a-zA-Z])", re.ASCII
)
_RE_LINHA_ELGIN = re.compile(r"(^|[\r\n])[ \t]*(P\d|ST,|US,|UN,)", re.ASCII)
_RE_DECIMAL = re.compile(r"\d+[.,]\d+", re.ASCII)
_RE_INTEIRO_GRAMAS = re.compile(r"\b\d{4,7}\b", re.ASCII)


def _kg_do_token(numero: str, unidade: str | None) -> float | None:
    """kg de um número de texto: unidade ``g`` são gramas e ``kg`` é kg; sem
    unidade, com separador é kg e inteiro são gramas (a convenção Toledo, a do
    parser antigo, que era o primeiro do ``auto``: "475 g" não pode virar
    475 kg)."""
    v = _double_try_parse(numero.replace(",", ".").replace("+", ""))
    if v is None:
        return None
    u = unidade.lower() if unidade is not None else None
    if u == "g":
        return v / 1000
    if u == "kg":
        return v
    return v if "." in numero or "," in numero else v / 1000


def _linhas_do_stream(b: bytes) -> list[str]:
    """Linhas de um stream: só as completas quando há terminador (a sobra
    depois do último CR/LF é um frame cortado no prazo — "P012" leria 12 kg);
    sem terminador nenhum, o buffer inteiro."""
    completas = _linhas_completas(b)
    if completas:
        return [_texto(linha) for linha in completas]
    if _CR in b or _LF in b:
        return []
    return [_texto(b)]


def interpretar_elgin(b: Bytes) -> FrameBalanca | None:
    """Elgin DP-1502 / SA-110: stream contínuo ("P000.000\\r" ou
    "ST,GS,+00.500kg\\r\\n"). Vale a última linha com peso > 0; zero conta
    como balança vazia; linhas US/UN (instável) são puladas."""
    b = _bytes(b)
    if not b or parece_urano(b):
        return None
    zero = False
    for linha in reversed(_linhas_do_stream(b)):
        texto = _trim_dart(linha)
        if not texto:
            continue
        if texto.startswith("US") or texto.startswith("UN"):
            continue
        m = _RE_TOKEN_PESO.search(texto)
        if m is None:
            continue
        kg = _kg_do_token(m.group(1), m.group(2))
        if kg is None or kg < 0:
            continue
        if kg == 0:
            zero = True
            continue
        if kg <= QUANTIDADE_MAX_ITEM:
            return FrameBalanca(EstadoFrame.peso, kg=arredondar_gramas(kg))
    return FrameBalanca(EstadoFrame.zero) if zero else None


def interpretar_generico(b: Bytes) -> FrameBalanca | None:
    """Genérico: número com unidade explícita primeiro ("475 g" = 0,475 kg),
    senão o último número decimal (kg), senão inteiro de 4 a 7 dígitos
    (gramas). Zero conta como balança vazia."""
    b = _bytes(b)
    if not b or parece_urano(b):
        return None
    txt = "\n".join(_linhas_do_stream(b))
    zero = False
    candidatos = [
        *(
            _kg_do_token(m.group(1), m.group(2))
            for m in reversed(list(_RE_TOKEN_COM_UNIDADE.finditer(txt)))
        ),
        *(_kg_do_token(m.group(0), "kg") for m in reversed(list(_RE_DECIMAL.finditer(txt)))),
    ]
    for kg in candidatos:
        if kg is None:
            continue
        if 0 < kg <= QUANTIDADE_MAX_ITEM:
            return FrameBalanca(EstadoFrame.peso, kg=arredondar_gramas(kg))
        if kg == 0:
            zero = True
    inteiro = _RE_INTEIRO_GRAMAS.search(txt)
    if inteiro is not None:
        n = int(inteiro.group(0))
        if n > 0:
            return FrameBalanca(EstadoFrame.peso, kg=n / 1000)
        zero = True
    return FrameBalanca(EstadoFrame.zero) if zero else None


# ─── Compatibilidade e mapeamento ───────────────────────────────────────


def _protocolo_de_texto(b: bytes) -> str:
    return "elgin" if _RE_LINHA_ELGIN.search(_texto(b)) else "generico"


class Compatibilidade(NamedTuple):
    frame: FrameBalanca
    protocolo: str


def interpretar_compatibilidade(b: Bytes) -> Compatibilidade | None:
    """Salvo ``toledo``/``filizola`` e frame que o parser estrito recusa: a
    leitura que continua segura, e qual protocolo a balança realmente fala. O
    DETECTAR antigo do PDV offline punha Toledo em quase tudo (o parser
    apagava as letras): a Urano UDC, lida errado, mas também Elgin e balanças
    de texto, lidas certo por sorte — essas não podem parar de ler."""
    b = _bytes(b)
    urano = interpretar_urano(b)
    if urano is not None:
        return Compatibilidade(urano, "urano")
    if not b or parece_urano(b):
        return None
    filizola = interpretar_filizola(b)
    if filizola is not None:
        return Compatibilidade(filizola, "filizola")
    if _STX in b or _ETX in b:
        return None
    # Linha de texto com UM número (ou um só com unidade): o que o Toledo
    # antigo lia apagando as letras. Dois números sem unidade ficam de fora:
    # foi colando campos assim que a Urano virou 11302419347,501 kg.
    for linha in reversed(_linhas_do_stream(b)):
        texto = _trim_dart(linha)
        if not texto:
            continue
        if texto.startswith("II") or texto.startswith("US") or texto.startswith("UN"):
            return Compatibilidade(FrameBalanca(EstadoFrame.instavel), _protocolo_de_texto(b))
        tokens = list(_RE_TOKEN_PESO.finditer(texto))
        com_unidade = [t for t in tokens if t.group(2) is not None]
        if len(tokens) == 1:
            token = tokens[0]
        elif len(com_unidade) == 1:
            token = com_unidade[0]
        else:
            return None
        kg = _kg_do_token(token.group(1), token.group(2))
        if kg is None:
            return None
        frame = (
            FrameBalanca(EstadoFrame.negativo, kg=arredondar_gramas(-kg)) if kg < 0 else _peso(kg)
        )
        if frame is None:
            return None
        return Compatibilidade(frame, _protocolo_de_texto(b))
    return None


class ResultadoFrame(NamedTuple):
    peso: float | None
    zero: bool


def resultado_do_frame(frame: FrameBalanca | None) -> ResultadoFrame:
    """O que ``lerPeso``/``lerPesoEstavel`` do PDV offline entregam de um frame:
    peso > 0, ou balança vazia (``zero``). Negativo conta como vazia: abaixo
    da tara o prato saiu — é o "retorno a zero" da refeição por quilo numa
    balança com tara. Instável, sobrecarga e frame não reconhecido não são
    leitura."""
    estado = frame.estado if frame is not None else None
    if estado is EstadoFrame.peso:
        return ResultadoFrame(frame.kg, False)  # type: ignore[union-attr]
    if estado in (EstadoFrame.zero, EstadoFrame.negativo):
        return ResultadoFrame(None, True)
    return ResultadoFrame(None, False)


# ─── Diagnóstico ─────────────────────────────────────────────────────────


def protocolo_do_frame(b: Bytes) -> str | None:
    """Qual protocolo estes bytes parecem, para o diagnóstico sugerir o certo.
    Elgin só com as linhas no formato dela ("P000.500", "ST,GS,…"); nunca
    ``generico``: esses tiram número de qualquer linha e a dica viraria chute."""
    b = _bytes(b)
    if not b:
        return None
    if interpretar_urano(b) is not None:
        return "urano"
    if interpretar_toledo(b) is not None:
        return "toledo"
    if interpretar_filizola(b) is not None:
        return "filizola"
    if _protocolo_de_texto(b) == "elgin" and interpretar_elgin(b) is not None:
        return "elgin"
    return None


#: Rótulos dos protocolos, usados pela tela e pelas mensagens (os mesmos do
#: PDV offline). A ordem é a dos seletores.
ROTULOS_PROTOCOLO: dict[str, str] = {
    "auto": "Auto-detectar",
    "urano": "Urano UDC / POP (Urano 12)",
    "toledo": "Toledo Prix",
    "filizola": "Filizola",
    "elgin": "Elgin DP",
    "generico": "Genérico ASCII",
}


def rotulo_protocolo(id_: str) -> str:
    return ROTULOS_PROTOCOLO.get(id_, id_)


def _to_fixed3(v: float) -> str:
    """``toStringAsFixed(3)`` do Dart: o valor binário exato, empate longe do
    zero (o ``format`` do Python arredonda o empate para o par)."""
    return str(Decimal(v).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP))


def descrever_frame(frame: FrameBalanca) -> str:
    """"0,475 kg", "prato vazio", "instável"… para mensagens e diagnóstico."""
    if frame.estado is EstadoFrame.peso:
        return f"{_to_fixed3(frame.kg).replace('.', ',')} kg"
    return {
        EstadoFrame.zero: "prato vazio",
        EstadoFrame.instavel: "instável",
        EstadoFrame.negativo: "peso negativo",
        EstadoFrame.sobrecarga: "sobrecarga",
    }[frame.estado]


_NOMES_CONTROLE: dict[int, str] = {
    0x02: "<STX>",
    0x03: "<ETX>",
    0x04: "<EOT>",
    0x05: "<ENQ>",
    0x06: "<ACK>",
    0x0A: "<LF>",
    0x0D: "<CR>",
    0x15: "<NAK>",
    0x18: "<CAN>",
    0x1B: "<ESC>",
}


class BytesDescritos(NamedTuple):
    texto: str
    hex: str


def descrever_bytes(b: Bytes, max: int = 96) -> BytesDescritos:  # noqa: A002 - nome do Dart
    """Frame legível para o diagnóstico: controles por nome (``CAN`` e ``ESC``
    não viram o mesmo ".") mais o HEX, dos últimos ``max`` bytes."""
    b = _bytes(b)
    if len(b) > max:
        b = b[len(b) - max :]
    texto: list[str] = []
    for c in b:
        nome = _NOMES_CONTROLE.get(c)
        if nome is not None:
            texto.append(nome)
        elif 0x20 <= c < 0x7F:
            texto.append(chr(c))
        else:
            texto.append(".")
    return BytesDescritos("".join(texto), " ".join(f"{c:02X}" for c in b))


# ─── Decisores do I/O (estado entre leituras, sem porta) ────────────────


class PedidoUrano:
    """Byte de pedido do Urano 12. Os manuais aceitam 04h ou 05h, mas uma UDC
    30000/5 do fórum do ACBr só respondeu 04h. Começa em ENQ (comprovado na
    CO-E do cliente, e é o que as outras balanças recebem no ``auto``) e
    lembra o byte que trouxe frame reconhecido. Com byte lembrado, silêncio é
    "sem resposta" (sobrecarga, cabo), não motivo para alternar; três silêncios
    seguidos esquecem o byte, o que também desfaz uma atribuição errada (a
    resposta ao ENQ chegando depois do EOT)."""

    ENQ = 0x05
    EOT = 0x04

    def __init__(self) -> None:
        self._lembrado: int | None = None
        self._silencios = 0

    @property
    def lembrado(self) -> int | None:
        return self._lembrado

    @property
    def primeiro(self) -> int:
        """Primeiro byte do pedido."""
        return self._lembrado if self._lembrado is not None else self.ENQ

    def deve_tentar_eot(self, *, permitido: bool) -> bool:
        """Sem frame Urano no prazo curto: tenta EOT? Só sem byte lembrado, e
        só onde for permitido (protocolo ``urano`` explícito e DETECTAR; no
        ``auto`` a balança pode ser de outra marca)."""
        return permitido and self._lembrado is None

    def registrar(self, *, silencio: bool, respondeu_com: int | None = None) -> None:
        """Fim do pedido: ``respondeu_com`` é o último byte enviado antes de um
        frame reconhecido; ``silencio`` = nenhum byte chegou."""
        if respondeu_com is not None:
            self._lembrado = respondeu_com
            self._silencios = 0
            return
        if not silencio:
            # Chegou resposta, só não reconhecida: a balança ouviu o pedido.
            self._silencios = 0
            return
        if self._lembrado is not None:
            self._silencios += 1
            if self._silencios >= 3:
                self.zerar()

    def zerar(self) -> None:
        self._lembrado = None
        self._silencios = 0


class OrdemAuto:
    """Ordem do modo ``auto``: do mais específico ao mais genérico, com a
    última estratégia que reconheceu um frame na frente ("grudenta"). Assim
    um cliente Toledo em ``auto`` não paga o pedido do Urano a cada leitura.
    Três leituras seguidas sem reconhecer soltam a preferida."""

    BASE: tuple[str, ...] = ("urano", "toledo", "filizola", "elgin", "generico")

    def __init__(self) -> None:
        self._preferida: str | None = None
        self._falhas = 0

    @property
    def preferida(self) -> str | None:
        return self._preferida

    @property
    def ordem(self) -> list[str]:
        if self._preferida is None:
            return list(self.BASE)
        return [self._preferida, *(id_ for id_ in self.BASE if id_ != self._preferida)]

    def registrar(self, reconheceu: str | None) -> None:
        if reconheceu is not None:
            self._preferida = reconheceu
            self._falhas = 0
            return
        if self._preferida is not None:
            self._falhas += 1
            if self._falhas >= 3:
                self.zerar()

    def zerar(self) -> None:
        self._preferida = None
        self._falhas = 0
