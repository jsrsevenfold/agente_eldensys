"""Binding ctypes da ``libcidbio.dll`` (SDK 1.7.0 do Control iD iDBio).

Assinaturas tiradas de ``cidbiolib.h``: toda função devolve ``int32_t``, ids
são ``int64_t`` e o que a DLL aloca (texto, imagem, lista de ids) volta por
ponteiro de saída e TEM de ser devolvido com o ``CIDBIO_Free*`` certo, uma vez
só. Por isso toda saída alocada passa por ``_Saidas``: o ``finally`` libera
tudo, inclusive quando a chamada dá erro ou a leitura do resultado falha.

Antes de chamar a DLL, template e id são conferidos aqui: um ``int`` fora do
``int64`` estoura no ctypes, e um texto estranho no ``SaveTemplate`` vira
lixo gravado no leitor.

A DLL x64 (MinGW, importa só ADVAPI32/KERNEL32/msvcrt/SETUPAPI) mora em
``<recursos>/vendor/idbio/libcidbio.dll``; ``os.add_dll_directory`` cobre uma
eventual dependência na mesma pasta. Nunca chamamos ``DeleteAllTemplates``
nem ``UpdateFirmware``: nem estão no binding.
"""

from __future__ import annotations

import ctypes
import os
import re
import uuid
from ctypes import POINTER, byref, c_char_p, c_int32, c_int64, c_uint32, c_void_p
from dataclasses import dataclass, field
from pathlib import Path

from ..recursos import resource_dir
from .erros import NAO_IDENTIFICADO, TEMPLATE_INVALIDO, ErroLeitor, SdkIndisponivel

NOME_DLL = "libcidbio.dll"

_PV = POINTER(c_void_p)
_PIDS = POINTER(POINTER(c_int64))

#: nome → (restype, argtypes). O binding e o dublê dos testes saem daqui.
#: ``char**``/``byte**`` de saída são ``void**``: o ponteiro é lido com
#: ``string_at`` e devolvido ao ``Free*`` sem conversão pelo ctypes.
ASSINATURAS: dict[str, tuple[type, list[type]]] = {
    "CIDBIO_Init": (c_int32, []),
    "CIDBIO_Terminate": (c_int32, []),
    "CIDBIO_GetDeviceInfo": (c_int32, [_PV, _PV, _PV]),
    "CIDBIO_CaptureImageAndTemplate": (
        c_int32,
        [_PV, _PV, POINTER(c_uint32), POINTER(c_uint32), POINTER(c_int32)],
    ),
    "CIDBIO_CaptureAndIdentify": (
        c_int32,
        [POINTER(c_int64), POINTER(c_int32), POINTER(c_int32)],
    ),
    "CIDBIO_MergeTemplates": (c_int32, [c_char_p, c_char_p, c_char_p, _PV]),
    "CIDBIO_SaveTemplate": (c_int32, [c_int64, c_char_p]),
    "CIDBIO_DeleteTemplate": (c_int32, [c_int64]),
    "CIDBIO_GetTemplateIDs": (c_int32, [_PIDS, POINTER(c_uint32)]),
    "CIDBIO_CancelCapture": (c_int32, []),
    "CIDBIO_GetErrorMessage": (c_int32, [c_int32, _PV]),
    "CIDBIO_GetParameter": (c_int32, [c_int32, _PV]),
    "CIDBIO_SetParameter": (c_int32, [c_int32, c_char_p]),
    "CIDBIO_FreeString": (c_int32, [c_void_p]),
    "CIDBIO_FreeByteArray": (c_int32, [c_void_p]),
    "CIDBIO_FreeIDArray": (c_int32, [POINTER(c_int64)]),
}

PARAM_BUZZER_ON = 4
PARAM_TEMPLATE_FORMAT = 5
PARAM_DETECT_TIMEOUT = 7

# ── Regras do contrato (seção 0) ─────────────────────────────────────────
_TEMPLATE_RE = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")
TEMPLATE_MIN = 64
TEMPLATE_MAX = 8192
#: ``device_id`` das digitais da loja: ``0 < id < 2**60`` (UUID v4).
ID_LIMITE = 2**60
_INT64_MIN, _INT64_MAX = -(2**63), 2**63 - 1

#: Tetos contra valor absurdo vindo da DLL (largura × altura, quantidade de ids).
_IMAGEM_MAX = 4 * 1024 * 1024
_IDS_MAX = 100_000


def device_id_de(valor: uuid.UUID | str) -> int:
    """Id da digital na memória do leitor: ``int(uuid.hex[:15], 16)`` (contrato).

    A mesma função existe no servidor (``apps/biometria/dedos.py``) e no PDV;
    os vetores de teste são os mesmos nos três repositórios.
    """
    u = valor if isinstance(valor, uuid.UUID) else uuid.UUID(str(valor))
    return int(u.hex[:15], 16)


def template_valido(template: object) -> bool:
    return (
        isinstance(template, str)
        and TEMPLATE_MIN <= len(template) <= TEMPLATE_MAX
        and bool(_TEMPLATE_RE.fullmatch(template))
    )


def id_da_loja_valido(valor: object) -> bool:
    return type(valor) is int and 0 < valor < ID_LIMITE


def _exigir_template(template: object, operacao: str) -> bytes:
    if not template_valido(template):
        raise ErroLeitor(TEMPLATE_INVALIDO, f"{operacao}: recusado antes da DLL")
    return template.encode("ascii")  # type: ignore[union-attr]


def _exigir_id(valor: object, operacao: str, *, da_loja: bool) -> int:
    # bool é int em Python: True viraria o id 1.
    if type(valor) is not int:
        raise ErroLeitor(-4, f"{operacao}: id inválido")
    if da_loja and not (0 < valor < ID_LIMITE):
        raise ErroLeitor(-4, f"{operacao}: id fora da faixa da loja")
    if not (_INT64_MIN <= valor <= _INT64_MAX):
        raise ErroLeitor(-4, f"{operacao}: id fora do int64")
    return valor


# ── Resultados ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class InfoLeitor:
    firmware: str
    serie: str
    modelo: str

    def como_dict(self) -> dict:
        return {"modelo": self.modelo, "serie": self.serie, "firmware": self.firmware}


@dataclass(frozen=True)
class Captura:
    """Uma leitura. ``repr`` NUNCA mostra o template nem a imagem (log)."""

    template: str = field(repr=False)
    imagem: bytes = field(repr=False)
    largura: int
    altura: int
    qualidade: int


@dataclass(frozen=True)
class Identificacao:
    device_id: int
    score: int
    qualidade: int


# ── Liberação garantida ──────────────────────────────────────────────────
class _Saidas:
    """Ponteiros de saída alocados pela DLL; ``__exit__`` libera cada um 1×."""

    def __init__(self, sdk: Sdk) -> None:
        self._sdk = sdk
        self._itens: list[tuple[object, str]] = []

    def texto(self) -> c_void_p:
        p = c_void_p()
        self._itens.append((p, "CIDBIO_FreeString"))
        return p

    def bytes_(self) -> c_void_p:
        p = c_void_p()
        self._itens.append((p, "CIDBIO_FreeByteArray"))
        return p

    def ids(self):
        p = POINTER(c_int64)()
        self._itens.append((p, "CIDBIO_FreeIDArray"))
        return p

    def __enter__(self) -> _Saidas:
        return self

    def __exit__(self, *exc: object) -> None:
        for ponteiro, liberar in self._itens:
            if not ponteiro:  # NULL: a DLL não alocou nada
                continue
            try:
                self._sdk._f[liberar](ponteiro)
            except Exception:  # noqa: BLE001 - liberar o resto mesmo assim
                pass
        self._itens.clear()


def _ler_texto(p: c_void_p) -> str | None:
    if not p.value:
        return None
    return ctypes.string_at(p.value).decode("ascii", errors="replace")


# ── Binding ──────────────────────────────────────────────────────────────
class Sdk:
    """Funções do iDBio com tipos Python. Erro (< 0) vira ``ErroLeitor``.

    Não é thread-safe: só a thread dona do leitor (``leitor.Leitor``) chama,
    com UMA exceção prevista pelo SDK — ``cancelar_captura`` de outra thread.
    """

    def __init__(self, lib: object, *, caminho: Path | None = None, dll_dir: object = None) -> None:
        self.caminho = caminho
        self._dll_dir = dll_dir  # mantém o os.add_dll_directory valendo
        self._f: dict[str, object] = {}
        for nome, (restype, argtypes) in ASSINATURAS.items():
            try:
                fn = getattr(lib, nome)
            except AttributeError:
                raise SdkIndisponivel(
                    "A biblioteca do leitor biométrico instalada é de outra versão "
                    f"(falta {nome}). Reinstale o EldenSys Agent."
                ) from None
            fn.restype = restype
            fn.argtypes = argtypes
            self._f[nome] = fn

    # ── abrir/fechar ──
    def iniciar(self) -> int:
        """``Init``: 0 ou 1 (já estava aberto). Erro levanta ``ErroLeitor``."""
        rc = self._f["CIDBIO_Init"]()
        if rc < 0:
            raise ErroLeitor(rc, "Init")
        return rc

    def terminar(self) -> int:
        """``Terminate``. Nunca levanta: fechar é sempre "melhor esforço"."""
        try:
            return int(self._f["CIDBIO_Terminate"]())
        except Exception:  # noqa: BLE001
            return -1

    # ── informação ──
    def info(self) -> InfoLeitor:
        with _Saidas(self) as s:
            versao, serie, modelo = s.texto(), s.texto(), s.texto()
            rc = self._f["CIDBIO_GetDeviceInfo"](byref(versao), byref(serie), byref(modelo))
            if rc < 0:
                raise ErroLeitor(rc, "GetDeviceInfo")
            return InfoLeitor(
                firmware=(_ler_texto(versao) or "").strip(),
                serie=(_ler_texto(serie) or "").strip(),
                modelo=(_ler_texto(modelo) or "").strip(),
            )

    def ids(self) -> list[int]:
        """Ids gravados no leitor (vazio quando o leitor avisa que não há)."""
        n = c_uint32()
        with _Saidas(self) as s:
            arr = s.ids()
            rc = self._f["CIDBIO_GetTemplateIDs"](byref(arr), byref(n))
            if rc < 0:
                raise ErroLeitor(rc, "GetTemplateIDs")
            if not arr or n.value == 0:
                return []
            return [int(arr[k]) for k in range(min(n.value, _IDS_MAX))]

    # ── capturas ──
    def capturar(self) -> Captura:
        """``CaptureImageAndTemplate``: bloqueia até o dedo ou o tempo do leitor."""
        w, h, q = c_uint32(), c_uint32(), c_int32()
        with _Saidas(self) as s:
            tpl, img = s.texto(), s.bytes_()
            rc = self._f["CIDBIO_CaptureImageAndTemplate"](
                byref(tpl), byref(img), byref(w), byref(h), byref(q)
            )
            if rc < 0:
                raise ErroLeitor(rc, "CaptureImageAndTemplate")
            template = _ler_texto(tpl) or ""
            total = w.value * h.value
            imagem = (
                ctypes.string_at(img.value, total) if img.value and 0 < total <= _IMAGEM_MAX else b""
            )
        if not template_valido(template):
            raise ErroLeitor(TEMPLATE_INVALIDO, "CaptureImageAndTemplate")
        return Captura(template, imagem, int(w.value), int(h.value), int(q.value))

    def identificar(self) -> Identificacao:
        """``CaptureAndIdentify`` (1:N dentro do leitor, só no Pro)."""
        i, s, q = c_int64(), c_int32(), c_int32()
        rc = self._f["CIDBIO_CaptureAndIdentify"](byref(i), byref(s), byref(q))
        if rc < 0:
            raise ErroLeitor(rc, "CaptureAndIdentify")
        if rc == 2:
            # Aviso "leitor sem digitais": não há com quem comparar.
            raise ErroLeitor(NAO_IDENTIFICADO, "CaptureAndIdentify: memória vazia")
        return Identificacao(int(i.value), int(s.value), int(q.value))

    def cancelar_captura(self) -> int:
        """``CancelCapture``: chamado de OUTRA thread enquanto a captura espera."""
        return int(self._f["CIDBIO_CancelCapture"]())

    # ── templates ──
    def mesclar(self, t1: str, t2: str, t3: str) -> str:
        brutos = [_exigir_template(t, "MergeTemplates") for t in (t1, t2, t3)]
        with _Saidas(self) as s:
            final = s.texto()
            rc = self._f["CIDBIO_MergeTemplates"](*brutos, byref(final))
            if rc < 0:
                raise ErroLeitor(rc, "MergeTemplates")
            texto = _ler_texto(final) or ""
        if not template_valido(texto):
            raise ErroLeitor(TEMPLATE_INVALIDO, "MergeTemplates")
        return texto

    def salvar(self, device_id: int, template: str) -> int:
        """``SaveTemplate``: 0 ou 3 (sobrescreveu o mesmo id)."""
        id_ = _exigir_id(device_id, "SaveTemplate", da_loja=True)
        bruto = _exigir_template(template, "SaveTemplate")
        rc = self._f["CIDBIO_SaveTemplate"](id_, bruto)
        if rc < 0:
            raise ErroLeitor(rc, "SaveTemplate")
        return int(rc)

    def apagar(self, device_id: int) -> int:
        """``DeleteTemplate``. Aceita qualquer ``int64``: um id gravado por outro
        programa (fora da faixa da loja) também precisa poder sair."""
        id_ = _exigir_id(device_id, "DeleteTemplate", da_loja=False)
        rc = self._f["CIDBIO_DeleteTemplate"](id_)
        if rc < 0:
            raise ErroLeitor(rc, "DeleteTemplate")
        return int(rc)

    # ── diversos ──
    def mensagem(self, codigo: int) -> str | None:
        """Texto do fabricante (inglês) para um código; só para log."""
        with _Saidas(self) as s:
            out = s.texto()
            rc = self._f["CIDBIO_GetErrorMessage"](int(codigo), byref(out))
            return _ler_texto(out) if rc >= 0 else None

    def parametro(self, parametro: int) -> str | None:
        with _Saidas(self) as s:
            out = s.texto()
            rc = self._f["CIDBIO_GetParameter"](int(parametro), byref(out))
            if rc < 0:
                raise ErroLeitor(rc, "GetParameter")
            return _ler_texto(out)

    def definir_parametro(self, parametro: int, valor: str) -> int:
        rc = self._f["CIDBIO_SetParameter"](int(parametro), str(valor).encode("ascii"))
        if rc < 0:
            raise ErroLeitor(rc, "SetParameter")
        return int(rc)


# ── Carga da DLL ─────────────────────────────────────────────────────────
def caminho_dll() -> Path:
    return resource_dir() / "vendor" / "idbio" / NOME_DLL


def dll_presente() -> bool:
    try:
        return caminho_dll().is_file()
    except OSError:
        return False


def motivo_falha_carga(e: OSError) -> str:
    """Mensagem em português para a falha do ``LoadLibrary``."""
    winerror = getattr(e, "winerror", None)
    if winerror == 193:
        return (
            "A biblioteca do leitor biométrico não é de 64 bits (x64) ou está corrompida. "
            "Reinstale o EldenSys Agent."
        )
    if winerror == 126:
        return (
            "Falta uma dependência da biblioteca do leitor biométrico (libcidbio.dll) "
            "neste Windows. Reinstale o EldenSys Agent."
        )
    return "Não foi possível carregar a biblioteca do leitor biométrico."


def carregar(caminho: Path | None = None) -> Sdk:
    """Carrega a DLL e devolve o binding. Falha vira ``SdkIndisponivel``."""
    caminho = Path(caminho) if caminho else caminho_dll()
    if not caminho.is_file():
        raise SdkIndisponivel(
            "A biblioteca do leitor biométrico (libcidbio.dll) não está nesta instalação "
            "do EldenSys Agent. Reinstale o agente."
        )
    dll_dir = None
    try:
        if hasattr(os, "add_dll_directory"):
            dll_dir = os.add_dll_directory(str(caminho.parent))
        lib = ctypes.CDLL(str(caminho))
    except OSError as e:
        if dll_dir is not None:
            dll_dir.close()
        raise SdkIndisponivel(motivo_falha_carga(e)) from None
    return Sdk(lib, caminho=caminho, dll_dir=dll_dir)
