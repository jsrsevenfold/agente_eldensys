"""PDF printing via embedded SumatraPDF.

Política: o agente imprime PDFs **no tamanho original** (`noscale`) e **na
orientação original** (`disable-auto-rotation`). Toda a formatação (largura,
margens, escala, orientação) é responsabilidade do EldenSys que gera o PDF.
Aqui só relayamos pro Sumatra sem alterar nada.

Exceção deliberada (v0.4.0): `render_mode="label"`. O SumatraPDF NÃO emite
primitivas GDI vetoriais — ele renderiza a página com o MuPDF num pixmap RGB
**com anti-aliasing** na resolução do device e faz blit para o HDC da
impressora. Numa cabeça térmica de 1 bit por dot (203 dpi), todo pixel cinza de
borda de letra é resolvido pelo driver como halftone: o texto sai chuviscado em
vez de preto sólido. O `DisableAntiAlias` do Sumatra corta isso na raiz — ele
chega ao MuPDF por `fz_set_aa_level(ctx, 0)`, sem distinguir RenderTarget, e
portanto vale para a impressão (a doc oficial diz o contrário; o fonte, não).

Por que POR JOB e não global: esta mesma função imprime NFC-e, comanda de
cozinha, via do garçom, recibo, DANFE, DANFSE, OS e orçamento. Desligar o AA
para todos aplicaria uma decisão de térmica 203 dpi a documento fiscal A4 em
laser 600 dpi — e pioraria os PDFs raster do `printHtmlSmart`, que passariam a
ser ampliados por vizinho-mais-próximo.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .config import APP_DIR, load_config

log = logging.getLogger("eldensys.agent.pdf")

#: Perfil `-appdata` dedicado ao modo etiqueta. Mora em %APPDATA%\EldenSysAgent,
#: que é gravável — ao contrário de Program Files, onde o agente é instalado.
#: Isolado de propósito: não toca no Sumatra do usuário nem nos outros jobs.
LABEL_APPDATA_DIR = APP_DIR / "sumatra-label"

LABEL_SETTINGS_NAME = "SumatraPDF-settings.txt"

#: `PrintScale = noscale` aqui é redundante com o `-print-settings` da linha de
#: comando, e é assim de propósito: se um Sumatra futuro mudar a precedência,
#: o default do perfil ainda é o certo para etiqueta.
_LABEL_SETTINGS = """\
# Gerado pelo EldenSys Agent (modo etiqueta). Não edite à mão: é reescrito
# a cada inicialização do agente.
DisableAntiAlias = true
CheckForUpdates = false
RememberOpenedFiles = false
RememberStatePerDocument = false
RestoreSession = false
ReuseInstance = false

PrinterDefaults [
    PrintScale = noscale
]
"""


def ensure_label_appdata() -> Path | None:
    """Prepara a pasta `-appdata` do modo etiqueta. Chame UMA VEZ, no startup.

    Reescrever isto a cada job seria corrida garantida: o frontend dispara jobs
    sem `await`, as rotas `def` do FastAPI rodam em threadpool e o próprio
    Sumatra reescreve o arquivo ao sair. Nunca propaga exceção — nenhuma
    impressão pode falhar por causa de um arquivo de preferências.
    """
    try:
        LABEL_APPDATA_DIR.mkdir(parents=True, exist_ok=True)
        (LABEL_APPDATA_DIR / LABEL_SETTINGS_NAME).write_text(
            _LABEL_SETTINGS, encoding="utf-8"
        )
        return LABEL_APPDATA_DIR
    except OSError:
        log.warning("Falha ao preparar %s", LABEL_APPDATA_DIR, exc_info=True)
        return None


def _resource_dir() -> Path:
    """Return the dir where bundled resources live (PyInstaller compatible)."""
    if getattr(sys, "frozen", False):
        # PyInstaller onedir: sys._MEIPASS for onefile, else exe dir
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _bundled_sumatra_paths() -> list[Path]:
    return [
        _resource_dir() / "vendor" / "SumatraPDF.exe",
        _resource_dir() / "SumatraPDF.exe",
    ]


def find_sumatra() -> str | None:
    cfg = load_config()
    if cfg.sumatra_path and Path(cfg.sumatra_path).exists():
        return cfg.sumatra_path

    candidates = [
        *_bundled_sumatra_paths(),
        Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "SumatraPDF" / "SumatraPDF.exe",
        Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
        / "SumatraPDF"
        / "SumatraPDF.exe",
    ]
    for c in candidates:
        if c.exists():
            return str(c)

    found = shutil.which("SumatraPDF")
    return found


def is_bundled_sumatra(sumatra: str) -> bool:
    """True só para o SumatraPDF que NÓS empacotamos.

    `-appdata` só é seguro nele. Num Sumatra de sistema antigo — ou no
    `sumatra_path` que o usuário apontou — uma flag desconhecida NÃO é
    ignorada: vira nome de arquivo a abrir, o returncode sai != 0, `print_pdf`
    levanta RuntimeError e a loja para de imprimir **tudo**, não só etiqueta.
    """
    if not sumatra:
        return False
    try:
        resolved = Path(sumatra).resolve()
    except OSError:
        return False
    for c in _bundled_sumatra_paths():
        try:
            if c.exists() and c.resolve() == resolved:
                return True
        except OSError:
            continue
    return False


def _build_print_settings(
    copies: int,
    paper: str | None,
    duplex: bool,
) -> str:
    """Constrói a string -print-settings do Sumatra.

    Sempre inclui `noscale` e `disable-auto-rotation` — o agente nunca escala
    nem gira o PDF; o EldenSys gera o documento já no tamanho e orientação
    finais. Sem o disable, o Sumatra rotaciona 90° quando a orientação da
    página (ex. etiqueta 70×30, paisagem) difere da orientação do papel do
    driver — MESMO com noscale (noscale controla escala, não rotação). Era a
    causa das etiquetas saírem deitadas na Argox/Elgin.

    Sumatra antigo (fallback de sistema em find_sumatra) ignora tokens
    desconhecidos: degrada pro comportamento anterior, sem crash. O bundled
    (vendor/) é 3.6.1, que suporta o token.
    """
    parts: list[str] = ["noscale", "disable-auto-rotation"]
    if copies and copies > 1:
        parts.append(f"{copies}x")
    if paper:
        parts.append(f"paper={paper}")
    if duplex:
        parts.append("duplexlong")
    return ",".join(parts)


def _resolve_label_appdata(sumatra: str) -> Path | None:
    """Pasta `-appdata` a usar neste job, ou None para não passar a flag.

    Três portões, nesta ordem — qualquer um que falhe devolve None e o job
    imprime exatamente como imprimia antes da v0.4.0.
    """
    if not load_config().sumatra_label_disable_antialias:
        return None
    if not is_bundled_sumatra(sumatra):
        log.info(
            "render_mode=label sem -appdata: SumatraPDF não é o empacotado (%s)",
            sumatra,
        )
        return None
    if (LABEL_APPDATA_DIR / LABEL_SETTINGS_NAME).exists():
        return LABEL_APPDATA_DIR
    # Startup não rodou (import direto, teste, agente atualizado sem restart):
    # cria agora. É idempotente e não levanta.
    return ensure_label_appdata()


def print_pdf(
    printer_name: str,
    pdf_bytes: bytes,
    copies: int = 1,
    paper: str | None = None,
    duplex: bool = False,
    render_mode: str = "default",
) -> None:
    """Print a PDF document using SumatraPDF in silent mode, sem transformações.

    `render_mode="label"` acrescenta um perfil `-appdata` com anti-aliasing
    desligado (ver docstring do módulo). Qualquer outro valor imprime pelo
    caminho clássico — inclusive valores desconhecidos vindos de um EldenSys
    mais novo, de propósito: o modo de falha é "imprime como antes".
    """
    sumatra = find_sumatra()
    if not sumatra:
        raise RuntimeError(
            "SumatraPDF.exe não encontrado. Instale-o ou coloque em vendor/SumatraPDF.exe."
        )

    settings = _build_print_settings(copies, paper, duplex)
    appdata = _resolve_label_appdata(sumatra) if render_mode == "label" else None

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        tmp.write(pdf_bytes)
        tmp.flush()
        tmp.close()

        args = [sumatra]
        # Antes de tudo: o Sumatra lê as preferências cedo, e assim a flag fica
        # visível no log mesmo que o resto da linha mude.
        if appdata is not None:
            args += ["-appdata", str(appdata)]
        args += [
            "-print-to",
            printer_name,
            "-silent",
            "-exit-when-done",
            "-print-settings",
            settings,
            tmp.name,
        ]

        # Linha de comando completa no log: reproduzível por copia-e-cola e
        # denuncia quando find_sumatra caiu num Sumatra de sistema (antigo).
        log.info("SumatraPDF: %s", subprocess.list2cmdline(args))

        # CREATE_NO_WINDOW = 0x08000000 to suppress console flash on frozen builds
        creationflags = 0x08000000 if sys.platform == "win32" else 0
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=120,
            creationflags=creationflags,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"SumatraPDF falhou (code={result.returncode}): {result.stderr or result.stdout}"
            )
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass
