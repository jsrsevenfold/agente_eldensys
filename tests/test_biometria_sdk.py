"""Binding ctypes da libcidbio.dll, sem o leitor e sem a DLL.

A "DLL" aqui é um objeto com uma função C falsa (``CFUNCTYPE``) por entrada
de ``ASSINATURAS``: o ctypes converte os argumentos de verdade, os ponteiros
de saída recebem memória alocada pelo dublê e cada ``CIDBIO_Free*`` é
conferido — a memória da DLL tem de voltar com o Free certo, UMA vez,
inclusive quando a chamada dá erro.
"""

from __future__ import annotations

import ctypes
import sys
import uuid
from ctypes import POINTER, c_int32, c_int64, c_void_p
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.biometria import sdk as sdk_mod  # noqa: E402
from agent.biometria.erros import ErroLeitor, SdkIndisponivel  # noqa: E402
from agent.biometria.sdk import ASSINATURAS, Sdk, device_id_de  # noqa: E402
from fake_idbio import tpl  # noqa: E402

TEXTO, BYTES, IDS = "FreeString", "FreeByteArray", "FreeIDArray"


class LibFalsa:
    """Funções C falsas montadas a partir das próprias ``ASSINATURAS``."""

    def __init__(self) -> None:
        self.vivos: dict[int, tuple[str, object]] = {}
        self.liberados: list[tuple[str, int]] = []
        self.free_errado: list[tuple[str, object]] = []
        self.rc: dict[str, int] = {}
        self.chamadas: list[tuple] = []
        self.template = tpl(7).encode()
        self.imagem = bytes(range(12))
        self.ids = [5, 2**60 - 1, 123456789]
        self.alocar_em_erro = True
        for nome, (restype, argtypes) in ASSINATURAS.items():
            impl = getattr(self, "_" + nome.removeprefix("CIDBIO_"))
            setattr(self, nome, ctypes.CFUNCTYPE(restype, *argtypes)(impl))

    # ── memória "da DLL" ──
    def _guardar(self, tipo: str, buf: object) -> int:
        endereco = ctypes.addressof(buf)
        self.vivos[endereco] = (tipo, buf)
        return endereco

    def _texto(self, valor: bytes) -> int:
        return self._guardar(TEXTO, ctypes.create_string_buffer(valor))

    def _liberar(self, tipo: str, endereco: object) -> int:
        item = self.vivos.get(endereco)  # type: ignore[arg-type]
        if item is None or item[0] != tipo:
            self.free_errado.append((tipo, endereco))
            return -3
        del self.vivos[endereco]  # type: ignore[arg-type]
        self.liberados.append((tipo, endereco))  # type: ignore[arg-type]
        return 0

    def _rc(self, nome: str) -> int:
        return self.rc.get(nome, 0)

    # ── funções ──
    def _Init(self):
        self.chamadas.append(("Init",))
        return self._rc("Init")

    def _Terminate(self):
        self.chamadas.append(("Terminate",))
        return 0

    def _GetDeviceInfo(self, versao, serie, modelo):
        rc = self._rc("GetDeviceInfo")
        if rc >= 0 or self.alocar_em_erro:
            versao[0] = self._texto(b"1.2.3")
            serie[0] = self._texto(b"SN-42")
            modelo[0] = self._texto(b"iDBio Pro")
        return rc

    def _CaptureImageAndTemplate(self, t, img, w, h, q):
        rc = self._rc("CaptureImageAndTemplate")
        if rc >= 0 or self.alocar_em_erro:
            t[0] = self._texto(self.template)
            buf = (ctypes.c_ubyte * len(self.imagem)).from_buffer_copy(self.imagem)
            img[0] = self._guardar(BYTES, buf)
            w[0], h[0], q[0] = 4, 3, 77
        return rc

    def _CaptureAndIdentify(self, id_, score, q):
        id_[0], score[0], q[0] = 2**60 - 1, 12345, 88
        return self._rc("CaptureAndIdentify")

    def _MergeTemplates(self, t1, t2, t3, final):
        self.chamadas.append(("Merge", t1, t2, t3))
        rc = self._rc("MergeTemplates")
        if rc >= 0 or self.alocar_em_erro:
            final[0] = self._texto(self.template)
        return rc

    def _SaveTemplate(self, id_, t):
        self.chamadas.append(("Save", id_, t))
        return self._rc("SaveTemplate")

    def _DeleteTemplate(self, id_):
        self.chamadas.append(("Delete", id_))
        return self._rc("DeleteTemplate")

    def _GetTemplateIDs(self, ids, n):
        rc = self._rc("GetTemplateIDs")
        if self.ids:
            buf = (c_int64 * len(self.ids))(*self.ids)
            self._guardar(IDS, buf)
            ids[0] = ctypes.cast(buf, POINTER(c_int64))
        n[0] = len(self.ids)
        return rc

    def _CancelCapture(self):
        self.chamadas.append(("Cancel",))
        return 0

    def _GetErrorMessage(self, codigo, msg):
        msg[0] = self._texto(f"erro {codigo}".encode())
        return 0

    def _GetParameter(self, parametro, valor):
        valor[0] = self._texto(b"30000")
        return 0

    def _SetParameter(self, parametro, valor):
        self.chamadas.append(("Set", parametro, valor))
        return 0

    def _FreeString(self, p):
        return self._liberar(TEXTO, p)

    def _FreeByteArray(self, p):
        return self._liberar(BYTES, p)

    def _FreeIDArray(self, p):
        return self._liberar(IDS, ctypes.cast(p, c_void_p).value)


@pytest.fixture
def lib():
    return LibFalsa()


@pytest.fixture
def sdk(lib):
    return Sdk(lib)


def tudo_liberado(lib: LibFalsa) -> bool:
    return not lib.vivos and not lib.free_errado


# ── assinaturas ──────────────────────────────────────────────────────────
#: Aridade de cada função em cidbiolib.h (SDK 1.7.0), conferida à mão.
ARIDADE_DO_HEADER = {
    "CIDBIO_Init": 0,
    "CIDBIO_Terminate": 0,
    "CIDBIO_GetDeviceInfo": 3,
    "CIDBIO_CaptureImageAndTemplate": 5,
    "CIDBIO_CaptureAndIdentify": 3,
    "CIDBIO_MergeTemplates": 4,
    "CIDBIO_SaveTemplate": 2,
    "CIDBIO_DeleteTemplate": 1,
    "CIDBIO_GetTemplateIDs": 2,
    "CIDBIO_CancelCapture": 0,
    "CIDBIO_GetErrorMessage": 2,
    "CIDBIO_GetParameter": 2,
    "CIDBIO_SetParameter": 2,
    "CIDBIO_FreeString": 1,
    "CIDBIO_FreeByteArray": 1,
    "CIDBIO_FreeIDArray": 1,
}


def test_assinaturas_batem_com_o_header():
    assert set(ASSINATURAS) == set(ARIDADE_DO_HEADER)
    for nome, (restype, argtypes) in ASSINATURAS.items():
        assert restype is c_int32, nome
        assert len(argtypes) == ARIDADE_DO_HEADER[nome], nome
    # ids são int64_t: por valor no Save/Delete, por ponteiro no Identify.
    assert ASSINATURAS["CIDBIO_SaveTemplate"][1][0] is c_int64
    assert ASSINATURAS["CIDBIO_DeleteTemplate"][1][0] is c_int64
    assert ASSINATURAS["CIDBIO_CaptureAndIdentify"][1][0] is POINTER(c_int64)


def test_funcoes_perigosas_nem_estao_no_binding():
    assert "CIDBIO_DeleteAllTemplates" not in ASSINATURAS
    assert "CIDBIO_UpdateFirmware" not in ASSINATURAS


def test_binding_aplica_argtypes_e_restype(sdk):
    for nome, (restype, argtypes) in ASSINATURAS.items():
        assert sdk._f[nome].restype is restype
        assert list(sdk._f[nome].argtypes) == list(argtypes)


def test_dll_sem_uma_funcao_e_sdk_indisponivel(lib):
    del lib.CIDBIO_CancelCapture
    with pytest.raises(SdkIndisponivel, match="CIDBIO_CancelCapture"):
        Sdk(lib)


# ── liberação ────────────────────────────────────────────────────────────
def test_info_le_e_libera_os_tres_textos(sdk, lib):
    info = sdk.info()
    assert (info.firmware, info.serie, info.modelo) == ("1.2.3", "SN-42", "iDBio Pro")
    assert [t for t, _ in lib.liberados] == [TEXTO] * 3
    assert tudo_liberado(lib)


def test_info_com_erro_libera_mesmo_assim(sdk, lib):
    lib.rc["GetDeviceInfo"] = -7
    with pytest.raises(ErroLeitor) as exc:
        sdk.info()
    assert exc.value.codigo == -7
    assert len(lib.liberados) == 3
    assert tudo_liberado(lib)


def test_captura_libera_template_e_imagem_uma_vez(sdk, lib):
    cap = sdk.capturar()
    assert cap.template == tpl(7)
    assert cap.imagem == lib.imagem
    assert (cap.largura, cap.altura, cap.qualidade) == (4, 3, 77)
    assert sorted(t for t, _ in lib.liberados) == [BYTES, TEXTO]
    assert tudo_liberado(lib)
    # O repr nunca mostra o template nem a imagem (vai parar em log).
    assert tpl(7) not in repr(cap) and "imagem" not in repr(cap)


@pytest.mark.parametrize("rc", [-6, -20, -21, -5])
def test_captura_com_erro_libera_o_que_a_dll_alocou(sdk, lib, rc):
    lib.rc["CaptureImageAndTemplate"] = rc
    with pytest.raises(ErroLeitor) as exc:
        sdk.capturar()
    assert exc.value.codigo == rc
    assert len(lib.liberados) == 2
    assert tudo_liberado(lib)


def test_captura_com_template_estranho_e_recusada_e_liberada(sdk, lib):
    lib.template = b"isto nao e base64 de template"
    with pytest.raises(ErroLeitor) as exc:
        sdk.capturar()
    assert exc.value.codigo == -22
    assert tudo_liberado(lib)


def test_ids_le_a_lista_e_libera_com_free_id_array(sdk, lib):
    assert sdk.ids() == [5, 2**60 - 1, 123456789]
    assert lib.liberados and lib.liberados[0][0] == IDS
    assert tudo_liberado(lib)


def test_ids_vazio_sem_ponteiro_nao_chama_free(sdk, lib):
    lib.ids = []
    lib.rc["GetTemplateIDs"] = 2  # aviso: leitor sem digitais
    assert sdk.ids() == []
    assert lib.liberados == []
    assert tudo_liberado(lib)


def test_ids_com_erro_libera_a_lista(sdk, lib):
    lib.rc["GetTemplateIDs"] = -16
    with pytest.raises(ErroLeitor) as exc:
        sdk.ids()
    assert exc.value.codigo == -16
    assert tudo_liberado(lib)


def test_mescla_confere_entradas_antes_da_dll(sdk, lib):
    with pytest.raises(ErroLeitor) as exc:
        sdk.mesclar(tpl(1), "curto", tpl(3))
    assert exc.value.codigo == -22
    assert not any(c[0] == "Merge" for c in lib.chamadas)
    assert sdk.mesclar(tpl(1), tpl(2), tpl(3)) == tpl(7)
    merge = [c for c in lib.chamadas if c[0] == "Merge"][0]
    assert merge[1:] == (tpl(1).encode(), tpl(2).encode(), tpl(3).encode())
    assert tudo_liberado(lib)


def test_mescla_com_erro_libera(sdk, lib):
    lib.rc["MergeTemplates"] = -10
    with pytest.raises(ErroLeitor) as exc:
        sdk.mesclar(tpl(1), tpl(2), tpl(3))
    assert exc.value.ident == "merge"
    assert tudo_liberado(lib)


def test_mensagem_e_parametro_liberam(sdk, lib):
    assert sdk.mensagem(-6) == "erro -6"
    assert sdk.parametro(sdk_mod.PARAM_DETECT_TIMEOUT) == "30000"
    assert sdk.definir_parametro(sdk_mod.PARAM_DETECT_TIMEOUT, "15000") == 0
    assert ("Set", 7, b"15000") in lib.chamadas
    assert tudo_liberado(lib)


# ── ids e templates conferidos antes da DLL ──────────────────────────────
@pytest.mark.parametrize("ruim", [True, 0, -1, 2**60, 2**64, 1.5, "12", None])
def test_salvar_recusa_id_fora_da_faixa_sem_chamar_a_dll(sdk, lib, ruim):
    with pytest.raises(ErroLeitor) as exc:
        sdk.salvar(ruim, tpl(1))
    assert exc.value.codigo == -4
    assert not any(c[0] == "Save" for c in lib.chamadas)


@pytest.mark.parametrize("ruim", ["", "a" * 63, "a" * 8193, "!!!" * 30, None, tpl(1) + "\n"])
def test_salvar_recusa_template_invalido_sem_chamar_a_dll(sdk, lib, ruim):
    with pytest.raises(ErroLeitor) as exc:
        sdk.salvar(5, ruim)
    assert exc.value.codigo == -22
    assert not any(c[0] == "Save" for c in lib.chamadas)


def test_salvar_passa_id_de_60_bits_e_template_intactos(sdk, lib):
    lib.rc["SaveTemplate"] = 3
    assert sdk.salvar(2**60 - 1, tpl(9)) == 3
    assert ("Save", 2**60 - 1, tpl(9).encode()) in lib.chamadas
    lib.rc["SaveTemplate"] = -13
    with pytest.raises(ErroLeitor) as exc:
        sdk.salvar(10, tpl(9))
    assert exc.value.erro == "memoria_cheia"


def test_apagar_aceita_id_de_outro_programa_mas_nao_estoura_int64(sdk, lib):
    sdk.apagar(123)
    sdk.apagar(2**62)
    assert ("Delete", 2**62) in lib.chamadas
    with pytest.raises(ErroLeitor):
        sdk.apagar(2**63)
    with pytest.raises(ErroLeitor):
        sdk.apagar(True)
    lib.rc["DeleteTemplate"] = -14
    with pytest.raises(ErroLeitor) as exc:
        sdk.apagar(5)
    assert exc.value.ident == "id_inexistente"


def test_identificar_devolve_id_de_60_bits(sdk, lib):
    ident = sdk.identificar()
    assert (ident.device_id, ident.score, ident.qualidade) == (2**60 - 1, 12345, 88)


@pytest.mark.parametrize(("rc", "codigo"), [(-18, -18), (2, -18), (-16, -16), (-20, -20)])
def test_identificar_sem_resultado_vira_erro(sdk, lib, rc, codigo):
    lib.rc["CaptureAndIdentify"] = rc
    with pytest.raises(ErroLeitor) as exc:
        sdk.identificar()
    assert exc.value.codigo == codigo


def test_init_terminate_e_cancelar(sdk, lib):
    assert sdk.iniciar() == 0
    lib.rc["Init"] = 1
    assert sdk.iniciar() == 1  # já estava aberto: aviso, não erro
    lib.rc["Init"] = -2
    with pytest.raises(ErroLeitor) as exc:
        sdk.iniciar()
    assert exc.value.erro == "leitor_desconectado"
    assert sdk.terminar() == 0
    assert sdk.cancelar_captura() == 0
    assert ("Cancel",) in lib.chamadas


# ── device_id (vetores obrigatórios do contrato, iguais nos 4 lados) ────
@pytest.mark.parametrize(
    ("digital", "esperado"),
    [
        ("00000000-0000-4000-8000-000000000000", 1024),
        ("123e4567-e89b-42d3-a456-426614174000", 82160278364075053),
        ("ffffffff-ffff-4fff-bfff-ffffffffffff", 1152921504606844159),
        ("9F3A2B1C-0D4E-4F5A-8B6C-7D8E9F0A1B2C", 717095650010129653),
    ],
)
def test_device_id_vetores_do_contrato(digital, esperado):
    assert device_id_de(digital) == esperado
    assert device_id_de(uuid.UUID(digital)) == esperado
    assert 0 < esperado < 2**60


# ── carga da DLL ─────────────────────────────────────────────────────────
def test_dll_ausente_e_sdk_indisponivel(tmp_path):
    with pytest.raises(SdkIndisponivel) as exc:
        sdk_mod.carregar(tmp_path / "libcidbio.dll")
    assert exc.value.erro == "sdk_ausente"
    assert exc.value.status == 503


@pytest.mark.skipif(sys.platform != "win32", reason="LoadLibrary do Windows")
def test_arquivo_que_nao_e_dll_x64_e_recusado(tmp_path):
    falsa = tmp_path / "libcidbio.dll"
    falsa.write_bytes(b"MZ" + b"\0" * 200)
    with pytest.raises(SdkIndisponivel) as exc:
        sdk_mod.carregar(falsa)
    assert "64 bits" in exc.value.mensagem or "carregar" in exc.value.mensagem


@pytest.mark.parametrize(
    ("winerror", "trecho"), [(193, "64 bits"), (126, "dependência"), (5, "carregar")]
)
def test_mensagens_de_falha_do_loadlibrary(winerror, trecho):
    e = OSError("falhou")
    e.winerror = winerror  # type: ignore[attr-defined]
    assert trecho in sdk_mod.motivo_falha_carga(e)


def test_caminho_da_dll_fica_nos_recursos(monkeypatch, tmp_path):
    monkeypatch.setattr(sdk_mod, "resource_dir", lambda: tmp_path)
    assert sdk_mod.caminho_dll() == tmp_path / "vendor" / "idbio" / "libcidbio.dll"
    assert sdk_mod.dll_presente() is False


DLL_REAL = Path(__file__).resolve().parent.parent / "vendor" / "idbio" / "libcidbio.dll"


@pytest.mark.skipif(not DLL_REAL.is_file(), reason="DLL do iDBio fora do repositório (git)")
def test_dll_real_exporta_todas_as_funcoes_do_binding():
    """Só carrega e confere os nomes: nenhuma função é chamada (nem Init)."""
    sdk = sdk_mod.carregar(DLL_REAL)
    assert set(sdk._f) == set(ASSINATURAS)
