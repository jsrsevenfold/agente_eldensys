"""Sonda do leitor Control iD iDBio (Fase 0 da integração de biometria).

Fora do pacote do agente (o spec só analisa ``run_agent.py`` e o pytest só
coleta ``tests/``). Serve para medir, com o leitor ligado, o que a
documentação pública do SDK não garante, antes de fechar o contrato entre
servidor, agente e PDV offline:

- se a DLL é x64 e carrega;
- o texto do ``model`` do iDBio Pro e o firmware;
- se ids de 60 bits voltam intactos do ``GetTemplateIDs``;
- o que o -9 (já cadastrado) e o aviso 3 (sobrescrito) significam de verdade;
- se ``CancelCapture`` funciona chamado de outra thread;
- se o leitor aceita dois processos ao mesmo tempo;
- se ``SetParameter`` fica gravado no aparelho;
- tempos de ``SaveTemplate`` e ``CaptureAndIdentify``;
- o que acontece ao desconectar o cabo.

Uso (na raiz do repositório, com o leitor ligado):

    .\\.venv\\Scripts\\python.exe -I tools\\sonda_idbio.py
    .\\.venv\\Scripts\\python.exe -I tools\\sonda_idbio.py --etapas info,ids,identificar

Etapas: pe, info, ciclos, captura, ids, identificar, cancelar, tempos,
exclusivo, parametros, desconectar ("tudo" = todas menos desconectar).

Segurança:
- só usa ids da faixa reservada ``[RESERVA_INICIO, 2**60)`` e apaga o que
  gravou; confere a lista de ids antes e depois e PARA se algum id de fora
  da faixa mudar;
- nunca chama ``DeleteAllTemplates`` nem ``UpdateFirmware``;
- todo ``SetParameter`` é desfeito no ``finally``;
- imagens e templates ficam só na pasta ``--saida`` (apague-a depois: é
  dado biométrico).
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import struct
import subprocess
import sys
import threading
import time
from ctypes import POINTER, byref, c_char_p, c_int32, c_int64, c_uint32, c_void_p
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DLL_PADRAO = RAIZ / "vendor" / "idbio" / "libcidbio.dll"

RESERVA_FIM = 2**60  # exclusivo
RESERVA_INICIO = RESERVA_FIM - 5000

PARAM_MIN_VAR = 1
PARAM_SIMILARITY_THRESHOLD = 2
PARAM_BUZZER_ON = 4
PARAM_TEMPLATE_FORMAT = 5
PARAM_ROTATION = 6
PARAM_DETECT_TIMEOUT = 7
PARAMS = {
    PARAM_MIN_VAR: "MIN_VAR",
    PARAM_SIMILARITY_THRESHOLD: "SIMILARITY_THRESHOLD",
    PARAM_BUZZER_ON: "BUZZER_ON",
    PARAM_TEMPLATE_FORMAT: "TEMPLATE_FORMAT",
    PARAM_ROTATION: "ROTATION",
    PARAM_DETECT_TIMEOUT: "DETECT_TIMEOUT",
}

CODIGOS = {
    3: "WARNING_OVERWRITING_TEMPLATE",
    2: "WARNING_NO_IDS_ON_DEVICE",
    1: "WARNING_ALREADY_INIT",
    0: "SUCCESS",
    -1: "ERROR_UNKNOWN",
    -2: "ERROR_NO_DEVICE",
    -3: "ERROR_NULL_ARGUMENT",
    -4: "ERROR_INVALID_ARGUMENT",
    -5: "ERROR_CAPTURE",
    -6: "ERROR_CAPTURE_TIMEOUT",
    -7: "ERROR_COMM_USB",
    -8: "ERROR_IO_ON_HOST",
    -9: "ERROR_TEMPLATE_ALREADY_ENROLLED",
    -10: "ERROR_MERGING",
    -11: "ERROR_MATCHING",
    -12: "ERROR_INVALID_FW_FILE",
    -13: "ERROR_NO_SPACE_LEFT_ON_DEVICE",
    -14: "ERROR_NO_TEMPLATE_WITH_ID",
    -15: "ERROR_INVALID_ERRNO",
    -16: "ERROR_UNAVAILABLE_FEATURE",
    -17: "ERROR_PREVIOUS_FW_VERSION",
    -18: "ERROR_NOT_IDENTIFIED",
    -19: "ERROR_BUSY",
    -20: "ERROR_CAPTURE_CANCELED",
    -21: "ERROR_NO_FINGER_DETECTED",
    -22: "ERROR_INVALID_TEMPLATE",
}

ETAPAS_TUDO = [
    "pe",
    "info",
    "ciclos",
    "captura",
    "ids",
    "identificar",
    "cancelar",
    "tempos",
    "exclusivo",
    "parametros",
]


def nome(codigo: int) -> str:
    return f"{codigo} ({CODIGOS.get(codigo, '?')})"


# ── PE ────────────────────────────────────────────────────────────────────
def arquitetura_pe(caminho: Path) -> str:
    dados = caminho.read_bytes()
    pe = struct.unpack_from("<I", dados, 0x3C)[0]
    maquina = struct.unpack_from("<H", dados, pe + 4)[0]
    return {0x8664: "x64", 0x14C: "x86", 0xAA64: "arm64"}.get(maquina, hex(maquina))


# ── Binding mínimo ───────────────────────────────────────────────────────
class Sdk:
    def __init__(self, caminho: Path):
        os.add_dll_directory(str(caminho.parent))
        self.lib = ctypes.CDLL(str(caminho))
        L = self.lib

        def f(nome_func, argtypes):
            fn = getattr(L, nome_func)
            fn.argtypes = argtypes
            fn.restype = c_int32
            return fn

        pv = POINTER(c_void_p)
        self.Init = f("CIDBIO_Init", [])
        self.Terminate = f("CIDBIO_Terminate", [])
        self.GetDeviceInfo = f("CIDBIO_GetDeviceInfo", [pv, pv, pv])
        self.GetParameter = f("CIDBIO_GetParameter", [c_int32, pv])
        self.SetParameter = f("CIDBIO_SetParameter", [c_int32, c_char_p])
        self.CaptureImageAndTemplate = f(
            "CIDBIO_CaptureImageAndTemplate",
            [pv, pv, POINTER(c_uint32), POINTER(c_uint32), POINTER(c_int32)],
        )
        self.CaptureAndIdentify = f(
            "CIDBIO_CaptureAndIdentify", [POINTER(c_int64), POINTER(c_int32), POINTER(c_int32)]
        )
        self.CaptureAndEnroll = f("CIDBIO_CaptureAndEnroll", [c_int64])
        self.MergeTemplates = f("CIDBIO_MergeTemplates", [c_char_p, c_char_p, c_char_p, pv])
        self.MatchTemplates = f("CIDBIO_MatchTemplates", [c_char_p, c_char_p, POINTER(c_int32)])
        self.SaveTemplate = f("CIDBIO_SaveTemplate", [c_int64, c_char_p])
        self.GetTemplate = f("CIDBIO_GetTemplate", [c_int64, pv])
        self.GetTemplateIDs = f("CIDBIO_GetTemplateIDs", [POINTER(POINTER(c_int64)), POINTER(c_uint32)])
        self.DeleteTemplate = f("CIDBIO_DeleteTemplate", [c_int64])
        self.CancelCapture = f("CIDBIO_CancelCapture", [])
        self.CheckFingerprint = f(
            "CIDBIO_CheckFingerprint", [pv, POINTER(c_uint32), POINTER(c_uint32)]
        )
        self.GetErrorMessage = f("CIDBIO_GetErrorMessage", [c_int32, pv])
        self.FreeString = f("CIDBIO_FreeString", [c_void_p])
        self.FreeByteArray = f("CIDBIO_FreeByteArray", [c_void_p])
        self.FreeIDArray = f("CIDBIO_FreeIDArray", [POINTER(c_int64)])

    # ── helpers com liberação garantida ──
    def _str(self, p: c_void_p) -> str | None:
        if not p.value:
            return None
        try:
            return ctypes.string_at(p.value).decode("utf-8", "replace")
        finally:
            self.FreeString(p)

    def info(self):
        v, s, m = c_void_p(), c_void_p(), c_void_p()
        rc = self.GetDeviceInfo(byref(v), byref(s), byref(m))
        return rc, self._str(v), self._str(s), self._str(m)

    def parametro(self, p: int):
        out = c_void_p()
        rc = self.GetParameter(p, byref(out))
        return rc, self._str(out)

    def mensagem(self, codigo: int) -> str | None:
        out = c_void_p()
        rc = self.GetErrorMessage(codigo, byref(out))
        return self._str(out) if rc >= 0 else None

    def capturar(self):
        t, img = c_void_p(), c_void_p()
        w, h, q = c_uint32(), c_uint32(), c_int32()
        rc = self.CaptureImageAndTemplate(byref(t), byref(img), byref(w), byref(h), byref(q))
        template = self._str(t)
        imagem = None
        if img.value:
            try:
                imagem = ctypes.string_at(img.value, w.value * h.value)
            finally:
                self.FreeByteArray(img)
        return rc, template, imagem, w.value, h.value, q.value

    def identificar(self):
        i, s, q = c_int64(), c_int32(), c_int32()
        rc = self.CaptureAndIdentify(byref(i), byref(s), byref(q))
        return rc, i.value, s.value, q.value

    def mesclar(self, t1: str, t2: str, t3: str):
        out = c_void_p()
        rc = self.MergeTemplates(t1.encode(), t2.encode(), t3.encode(), byref(out))
        return rc, self._str(out)

    def comparar(self, t1: str, t2: str):
        s = c_int32()
        rc = self.MatchTemplates(t1.encode(), t2.encode(), byref(s))
        return rc, s.value

    def ids(self):
        arr = POINTER(c_int64)()
        n = c_uint32()
        rc = self.GetTemplateIDs(byref(arr), byref(n))
        lista = []
        if arr:
            try:
                lista = [arr[k] for k in range(n.value)]
            finally:
                self.FreeIDArray(arr)
        return rc, lista


# ── Saída ────────────────────────────────────────────────────────────────
class Relatorio:
    def __init__(self, saida: Path):
        self.saida = saida
        self.dados: dict = {"inicio": time.strftime("%Y-%m-%d %H:%M:%S"), "etapas": {}}

    def etapa(self, nome_etapa: str) -> dict:
        print(f"\n== {nome_etapa} ==")
        return self.dados["etapas"].setdefault(nome_etapa, {})

    def gravar(self):
        self.saida.mkdir(parents=True, exist_ok=True)
        caminho = self.saida / "resultado.json"
        caminho.write_text(json.dumps(self.dados, ensure_ascii=False, indent=2), "utf-8")
        print(f"\nResumo gravado em {caminho}")


def pedir(msg: str, *, enter: bool = True) -> None:
    print(f"\n>>> {msg}")
    if enter:
        try:
            input(">>> Pressione Enter para continuar...")
        except EOFError:
            print(">>> (sem teclado: seguindo em 3 s)")
            time.sleep(3)


def guardar_png(pasta: Path, nome_arq: str, imagem: bytes | None, w: int, h: int):
    if not imagem or not w or not h:
        return None
    try:
        from PIL import Image  # já está no venv do agente

        pasta.mkdir(parents=True, exist_ok=True)
        destino = pasta / nome_arq
        Image.frombytes("L", (w, h), imagem).save(destino)
        return str(destino)
    except Exception as e:  # noqa: BLE001
        return f"falhou: {e}"


# ── Etapas ───────────────────────────────────────────────────────────────
class Sonda:
    def __init__(self, sdk: Sdk, rel: Relatorio, args):
        self.sdk = sdk
        self.rel = rel
        self.args = args
        self.templates: list[str] = []
        self.mesclado: str | None = None
        self.gravados: set[int] = set()
        self.ids_fora_da_reserva: list[int] | None = None

    def init(self, r: dict | None = None) -> int:
        t0 = time.perf_counter()
        rc = self.sdk.Init()
        ms = round((time.perf_counter() - t0) * 1000)
        print(f"Init: {nome(rc)} em {ms} ms")
        if r is not None:
            r.setdefault("init", []).append({"rc": rc, "ms": ms})
        return rc

    def conferir_reserva(self, momento: str):
        rc, lista = self.sdk.ids()
        fora = sorted(i for i in lista if not (RESERVA_INICIO <= i < RESERVA_FIM))
        if self.ids_fora_da_reserva is None:
            self.ids_fora_da_reserva = fora
        elif fora != self.ids_fora_da_reserva:
            raise SystemExit(
                f"PARADA DE SEGURANÇA ({momento}): ids fora da faixa reservada mudaram "
                f"({len(self.ids_fora_da_reserva)} -> {len(fora)})."
            )
        return rc, lista

    def limpar_reserva(self):
        rc, lista = self.sdk.ids()
        meus = [i for i in lista if RESERVA_INICIO <= i < RESERVA_FIM]
        for i in meus:
            self.sdk.DeleteTemplate(i)
        if meus:
            print(f"Limpeza: {len(meus)} ids da faixa reservada apagados")

    # pe
    def etapa_pe(self):
        r = self.rel.etapa("pe")
        r["arquitetura"] = arquitetura_pe(self.args.dll)
        r["python_bits"] = struct.calcsize("P") * 8
        print(f"DLL: {self.args.dll} -> {r['arquitetura']} (Python {r['python_bits']} bits)")

    # info
    def etapa_info(self):
        r = self.rel.etapa("info")
        rc = self.init(r)
        if rc < 0:
            r["parado"] = "sem leitor"
            r["mensagens"] = {str(c): self.sdk.mensagem(c) for c in sorted(CODIGOS)}
            print("Sem leitor: registrando só a tabela de mensagens.")
            return False
        rc, ver, serie, modelo = self.sdk.info()
        r["device_info"] = {"rc": rc, "firmware": ver, "serie": serie, "modelo": modelo}
        print(f"GetDeviceInfo: {nome(rc)} firmware={ver!r} série={serie!r} modelo={modelo!r}")
        r["parametros"] = {}
        for p, n in PARAMS.items():
            prc, valor = self.sdk.parametro(p)
            r["parametros"][n] = {"rc": prc, "valor": valor}
            print(f"  {n} = {valor!r} ({nome(prc)})")
        r["mensagens"] = {str(c): self.sdk.mensagem(c) for c in sorted(CODIGOS)}
        rc, lista = self.conferir_reserva("info")
        r["ids_no_leitor"] = {"rc": rc, "total": len(lista)}
        print(f"Ids no leitor: {len(lista)} ({nome(rc)})")
        return True

    # ciclos
    def etapa_ciclos(self):
        r = self.rel.etapa("ciclos")
        self.sdk.Terminate()
        tempos, falhas = [], []
        for k in range(self.args.ciclos):
            t0 = time.perf_counter()
            rc1 = self.sdk.Init()
            rc2, *_ = self.sdk.info()
            rc3 = self.sdk.Terminate()
            tempos.append((time.perf_counter() - t0) * 1000)
            if rc1 < 0 or rc2 < 0 or rc3 < 0:
                falhas.append({"ciclo": k, "init": rc1, "info": rc2, "terminate": rc3})
        r["ciclos"] = self.args.ciclos
        r["ms_medio"] = round(sum(tempos) / len(tempos), 1) if tempos else None
        r["ms_max"] = round(max(tempos), 1) if tempos else None
        r["falhas"] = falhas
        print(f"{self.args.ciclos} ciclos Init/Info/Terminate: médio {r['ms_medio']} ms, "
              f"máx {r['ms_max']} ms, falhas {len(falhas)}")
        self.init()

    # captura (3 leituras do mesmo dedo + merge + comparação)
    def etapa_captura(self):
        r = self.rel.etapa("captura")
        leituras = []
        for k in range(3):
            pedir(f"CAPTURA {k + 1}/3: coloque o INDICADOR DIREITO no leitor quando aparecer "
                  "'aguardando dedo'. Tire o dedo depois do bip.")
            print("aguardando dedo...")
            t0 = time.perf_counter()
            rc, tpl, img, w, h, q = self.sdk.capturar()
            ms = round((time.perf_counter() - t0) * 1000)
            png = guardar_png(self.args.saida, f"captura_{k + 1}.png", img, w, h)
            leituras.append({
                "rc": rc, "ms": ms, "qualidade": q, "largura": w, "altura": h,
                "bytes_imagem": len(img) if img else 0,
                "imagem_ok": bool(img) and len(img) == w * h,
                "template_tam": len(tpl) if tpl else 0, "png": png,
            })
            print(f"  {nome(rc)} em {ms} ms, qualidade {q}, {w}x{h}, template {len(tpl or '')} chars")
            if rc == 0 and tpl:
                self.templates.append(tpl)
        r["leituras"] = leituras
        if len(self.templates) == 3:
            rc, m = self.sdk.mesclar(*self.templates)
            r["merge"] = {"rc": rc, "tam": len(m or "")}
            print(f"Merge: {nome(rc)}, {len(m or '')} chars")
            self.mesclado = m if rc == 0 else None
            rc, s = self.sdk.comparar(self.templates[0], self.templates[1])
            r["match_mesmo_dedo"] = {"rc": rc, "score": s}
            print(f"MatchTemplates (mesmo dedo): {nome(rc)} score {s}")
        pedir("Agora coloque OUTRO dedo (ex.: o polegar esquerdo) para comparar com o indicador.")
        print("aguardando dedo...")
        rc, tpl, img, w, h, q = self.sdk.capturar()
        if rc == 0 and tpl and self.templates:
            rc2, s = self.sdk.comparar(self.templates[0], tpl)
            r["match_outro_dedo"] = {"rc": rc2, "score": s}
            print(f"MatchTemplates (outro dedo): {nome(rc2)} score {s}")
            if self.mesclado:
                rc3, _ = self.sdk.mesclar(self.templates[0], self.templates[1], tpl)
                r["merge_dedo_trocado"] = {"rc": rc3}
                print(f"Merge com um dedo trocado: {nome(rc3)}")
        # CheckFingerprint sem dedo
        img_p, w, h = c_void_p(), c_uint32(), c_uint32()
        rc = self.sdk.CheckFingerprint(byref(img_p), byref(w), byref(h))
        if img_p.value:
            self.sdk.FreeByteArray(img_p)
        r["check_sem_dedo"] = {"rc": rc}
        print(f"CheckFingerprint sem dedo: {nome(rc)}")

    # ids (60 bits, -9, aviso 3)
    def etapa_ids(self):
        r = self.rel.etapa("ids")
        if not self.mesclado:
            r["pulada"] = "sem template mesclado (rode a etapa captura antes)"
            print(r["pulada"])
            return
        self.conferir_reserva("ids/antes")
        testes = []
        alvos = {
            "2^60-1": RESERVA_FIM - 1,
            "2^60-2": RESERVA_FIM - 2,
            "2^59 (fora da reserva, apagado em seguida)": 2**59,
            "2^62 (fora da reserva, apagado em seguida)": 2**62,
            "2^63-1 (fora da reserva, apagado em seguida)": 2**63 - 1,
            "123 (fora da reserva, apagado em seguida)": 123,
        }
        for rotulo, id_ in alvos.items():
            _, antes = self.sdk.ids()
            if id_ in antes:
                testes.append({"id": rotulo, "pulado": "id já existe no leitor"})
                continue
            rc = self.sdk.SaveTemplate(id_, self.mesclado.encode())
            _, depois = self.sdk.ids()
            volta = id_ in depois
            # Id que apareceu sem ser o pedido = o leitor cortou/alterou o id.
            # Registra e apaga na hora para não deixar lixo fora da reserva.
            novos = sorted(set(depois) - set(antes))
            estranhos = [i for i in novos if i != id_]
            testes.append({"id": rotulo, "valor": id_, "save_rc": nome(rc), "volta_intacto": volta,
                           "ids_que_apareceram": novos})
            print(f"SaveTemplate({rotulo}) = {nome(rc)}; aparece em GetTemplateIDs: {volta}"
                  + (f"; APARECEU OUTRO ID: {estranhos}" if estranhos else ""))
            for i in estranhos:
                self.sdk.DeleteTemplate(i)
            if volta and not (RESERVA_INICIO <= id_ < RESERVA_FIM):
                self.sdk.DeleteTemplate(id_)
            elif volta:
                self.gravados.add(id_)
            # Depois do 1º sucesso, o mesmo template em outro id pode dar -9:
            # é justamente o que queremos medir (ver "mesmo_template_outro_id").
        r["ids_grandes"] = testes
        # Mesmo id de novo (aviso 3?)
        rc = self.sdk.SaveTemplate(RESERVA_FIM - 1, self.mesclado.encode())
        r["mesmo_id_de_novo"] = nome(rc)
        print(f"SaveTemplate(mesmo id de novo) = {nome(rc)}")
        # Mesmo template em outro id da reserva (-9 = detecção de duplicado?)
        rc = self.sdk.SaveTemplate(RESERVA_FIM - 3, self.mesclado.encode())
        r["mesmo_template_outro_id"] = nome(rc)
        print(f"SaveTemplate(mesmo template, outro id) = {nome(rc)}")
        if rc >= 0:
            self.gravados.add(RESERVA_FIM - 3)
        # DeleteTemplate de id inexistente
        rc = self.sdk.DeleteTemplate(RESERVA_FIM - 4)
        r["apagar_inexistente"] = nome(rc)
        print(f"DeleteTemplate(inexistente) = {nome(rc)}")
        # GetTemplate devolve o mesmo texto?
        out = c_void_p()
        rc = self.sdk.GetTemplate(RESERVA_FIM - 1, byref(out))
        lido = self.sdk._str(out)
        r["get_template"] = {"rc": rc, "igual_ao_gravado": lido == self.mesclado,
                             "tam_lido": len(lido or "")}
        print(f"GetTemplate: {nome(rc)}; igual ao gravado: {lido == self.mesclado}")
        # CaptureAndEnroll com dedo já gravado (-9 aqui?)
        pedir("CaptureAndEnroll: coloque o INDICADOR DIREITO 3 vezes (o leitor apita a cada leitura).")
        print("aguardando 3 leituras...")
        rc = self.sdk.CaptureAndEnroll(RESERVA_FIM - 5)
        r["enroll_dedo_ja_gravado"] = nome(rc)
        print(f"CaptureAndEnroll(dedo já gravado) = {nome(rc)}")
        if rc >= 0:
            self.gravados.add(RESERVA_FIM - 5)
        self.conferir_reserva("ids/depois")

    # identificar
    def etapa_identificar(self):
        r = self.rel.etapa("identificar")
        _, lista = self.sdk.ids()
        if not any(RESERVA_INICIO <= i < RESERVA_FIM for i in lista):
            r["pulada"] = "nenhuma digital de teste gravada (rode captura + ids antes)"
            print(r["pulada"])
            return
        for rotulo, instr in (
            ("dedo_gravado", "Coloque o INDICADOR DIREITO (o que foi gravado)."),
            ("dedo_nao_gravado", "Coloque um dedo que NÃO foi gravado (ex.: o mínimo esquerdo)."),
        ):
            pedir(instr)
            print("aguardando dedo...")
            t0 = time.perf_counter()
            rc, id_, score, q = self.sdk.identificar()
            ms = round((time.perf_counter() - t0) * 1000)
            r[rotulo] = {"rc": nome(rc), "id": id_, "na_reserva": RESERVA_INICIO <= id_ < RESERVA_FIM,
                         "score": score, "qualidade": q, "ms": ms}
            print(f"  {nome(rc)} id={id_} score={score} qualidade={q} ({ms} ms)")
        # Sem dedo: com DETECT_TIMEOUT curto para não esperar 30 s
        rc0, original = self.sdk.parametro(PARAM_DETECT_TIMEOUT)
        try:
            self.sdk.SetParameter(PARAM_DETECT_TIMEOUT, b"5000")
            pedir("NÃO coloque o dedo: medindo o tempo esgotado (5 s).")
            t0 = time.perf_counter()
            rc, id_, score, q = self.sdk.identificar()
            ms = round((time.perf_counter() - t0) * 1000)
            r["sem_dedo"] = {"rc": nome(rc), "ms": ms}
            print(f"  sem dedo: {nome(rc)} em {ms} ms")
        finally:
            if rc0 >= 0 and original is not None:
                self.sdk.SetParameter(PARAM_DETECT_TIMEOUT, original.encode())

    # cancelar de outra thread
    def etapa_cancelar(self):
        r = self.rel.etapa("cancelar")
        pedir("NÃO coloque o dedo: a captura vai ser cancelada de outra thread "
              f"({self.args.cancelamentos} vezes, ~3 s cada).")
        resultados = []
        for k in range(self.args.cancelamentos):
            saida: dict = {}

            def captura():
                t0 = time.perf_counter()
                rc, *_ = self.sdk.identificar()
                saida["rc"] = rc
                saida["fim"] = time.perf_counter()
                saida["t0"] = t0

            th = threading.Thread(target=captura, daemon=True)
            th.start()
            time.sleep(2.0)
            t_cancel = time.perf_counter()
            rc_cancel = self.sdk.CancelCapture()
            th.join(timeout=35)
            vivo = th.is_alive()
            latencia = round((saida.get("fim", time.perf_counter()) - t_cancel) * 1000)
            resultados.append({"rc_cancel": nome(rc_cancel), "rc_captura": nome(saida.get("rc", 999)),
                               "latencia_ms": latencia, "travou": vivo})
            print(f"  {k + 1}: CancelCapture={nome(rc_cancel)} captura={nome(saida.get('rc', 999))} "
                  f"latência {latencia} ms{' TRAVOU' if vivo else ''}")
            if vivo:
                break
        r["resultados"] = resultados
        rc = self.sdk.CancelCapture()
        r["cancelar_sem_captura"] = nome(rc)
        print(f"CancelCapture sem captura em curso: {nome(rc)}")

    # tempos com muitas digitais
    def etapa_tempos(self):
        r = self.rel.etapa("tempos")
        if not self.mesclado:
            r["pulada"] = "sem template mesclado (rode a etapa captura antes)"
            print(r["pulada"])
            return
        n = self.args.n
        self.conferir_reserva("tempos/antes")
        base = RESERVA_INICIO + 100
        t0 = time.perf_counter()
        codigos: dict[str, int] = {}
        gravados = 0
        for k in range(n):
            rc = self.sdk.SaveTemplate(base + k, self.mesclado.encode())
            codigos[nome(rc)] = codigos.get(nome(rc), 0) + 1
            if rc >= 0:
                gravados += 1
                self.gravados.add(base + k)
            elif rc == -13:
                break
        dt = time.perf_counter() - t0
        r["save"] = {"tentativas": n, "gravados": gravados, "ms_por_save": round(dt * 1000 / max(n, 1), 1),
                     "codigos": codigos}
        print(f"SaveTemplate x{n}: {gravados} gravados, {r['save']['ms_por_save']} ms cada, códigos {codigos}")
        t0 = time.perf_counter()
        rc, lista = self.sdk.ids()
        r["get_ids"] = {"rc": nome(rc), "total": len(lista), "ms": round((time.perf_counter() - t0) * 1000)}
        print(f"GetTemplateIDs com {len(lista)}: {r['get_ids']['ms']} ms")
        pedir(f"Coloque o INDICADOR DIREITO para identificar com {len(lista)} digitais na memória.")
        t0 = time.perf_counter()
        rc, id_, score, q = self.sdk.identificar()
        r["identificar_cheio"] = {"rc": nome(rc), "id": id_, "score": score,
                                  "ms": round((time.perf_counter() - t0) * 1000)}
        print(f"  {nome(rc)} id={id_} score={score} ({r['identificar_cheio']['ms']} ms com o dedo)")
        t0 = time.perf_counter()
        apagados = 0
        for k in range(n):
            if base + k in self.gravados and self.sdk.DeleteTemplate(base + k) >= 0:
                apagados += 1
                self.gravados.discard(base + k)
        r["delete"] = {"apagados": apagados,
                       "ms_por_delete": round((time.perf_counter() - t0) * 1000 / max(apagados, 1), 1)}
        print(f"DeleteTemplate x{apagados}: {r['delete']['ms_por_delete']} ms cada")
        self.conferir_reserva("tempos/depois")

    # dois processos
    def etapa_exclusivo(self):
        r = self.rel.etapa("exclusivo")
        cmd = [sys.executable, "-I", str(Path(__file__).resolve()), "--dll", str(self.args.dll),
               "--etapas", "info", "--saida", str(self.args.saida / "exclusivo_filho"), "--sem-limpeza"]
        print("Este processo segura o leitor; um segundo processo tenta abrir...")
        t0 = time.perf_counter()
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        r["filho"] = {"exit": proc.returncode, "ms": round((time.perf_counter() - t0) * 1000),
                      "saida": proc.stdout[-1500:], "erro": proc.stderr[-800:]}
        print(proc.stdout[-600:])

    # parâmetro gravado no aparelho?
    def etapa_parametros(self):
        r = self.rel.etapa("parametros")
        rc0, original = self.sdk.parametro(PARAM_DETECT_TIMEOUT)
        r["original"] = original
        try:
            rc = self.sdk.SetParameter(PARAM_DETECT_TIMEOUT, b"15000")
            r["set"] = nome(rc)
            self.sdk.Terminate()
            self.init()
            rc, valor = self.sdk.parametro(PARAM_DETECT_TIMEOUT)
            r["depois_de_reabrir"] = valor
            r["fica_gravado"] = valor == "15000"
            print(f"DETECT_TIMEOUT original={original!r}, depois de Terminate/Init={valor!r} "
                  f"-> fica gravado no aparelho: {valor == '15000'}")
        finally:
            if rc0 >= 0 and original is not None:
                self.sdk.SetParameter(PARAM_DETECT_TIMEOUT, original.encode())
        rc, fmt = self.sdk.parametro(PARAM_TEMPLATE_FORMAT)
        r["template_format"] = fmt

    # cabo
    def etapa_desconectar(self):
        r = self.rel.etapa("desconectar")
        pedir("DESCONECTE o cabo USB do leitor agora (com ele ocioso).")
        rc, *_ = self.sdk.info()
        r["info_desconectado"] = nome(rc)
        print(f"GetDeviceInfo desconectado: {nome(rc)}")
        pedir("RECONECTE o cabo e espere 3 s.")
        self.sdk.Terminate()
        rc = self.init()
        r["reabrir"] = nome(rc)
        pedir("Agora vamos capturar e você DESCONECTA o cabo durante a espera do dedo "
              "(sem colocar o dedo). Se o processo travar, a etapa registra.")
        saida: dict = {}

        def captura():
            rc, *_ = self.sdk.identificar()
            saida["rc"] = rc

        th = threading.Thread(target=captura, daemon=True)
        th.start()
        th.join(timeout=45)
        r["desconectar_na_captura"] = "TRAVOU (sem retorno em 45 s)" if th.is_alive() else nome(saida.get("rc", 999))
        print(f"Captura com o cabo arrancado: {r['desconectar_na_captura']}")
        pedir("RECONECTE o cabo.")
        self.sdk.Terminate()
        r["reabrir_depois"] = nome(self.init())


def main() -> int:
    for fluxo in (sys.stdout, sys.stderr):
        try:
            fluxo.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dll", type=Path, default=DLL_PADRAO)
    ap.add_argument("--etapas", default="tudo")
    ap.add_argument("--saida", type=Path, default=Path(os.environ.get("TEMP", ".")) / "sonda_idbio")
    ap.add_argument("--ciclos", type=int, default=100)
    ap.add_argument("--cancelamentos", type=int, default=5)
    ap.add_argument("--n", type=int, default=300)
    ap.add_argument("--sem-limpeza", action="store_true")
    args = ap.parse_args()
    args.dll = args.dll.resolve()

    etapas = ETAPAS_TUDO if args.etapas == "tudo" else [e.strip() for e in args.etapas.split(",") if e.strip()]
    rel = Relatorio(args.saida)
    rel.dados["dll"] = str(args.dll)
    rel.dados["etapas_pedidas"] = etapas

    if "pe" in etapas:
        r = rel.etapa("pe")
        r["arquitetura"] = arquitetura_pe(args.dll)
        print(f"DLL {args.dll.name}: {r['arquitetura']}")

    sdk = Sdk(args.dll)
    sonda = Sonda(sdk, rel, args)
    try:
        com_leitor = sonda.etapa_info() if any(e != "pe" for e in etapas) else False
        if com_leitor:
            for e in etapas:
                if e in ("pe", "info"):
                    continue
                getattr(sonda, f"etapa_{e}")()
                rel.gravar()
    except KeyboardInterrupt:
        print("\nInterrompido.")
    finally:
        if not args.sem_limpeza:
            try:
                sonda.limpar_reserva()
            except Exception as e:  # noqa: BLE001
                print(f"Limpeza falhou: {e}")
        sdk.Terminate()
        rel.gravar()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
