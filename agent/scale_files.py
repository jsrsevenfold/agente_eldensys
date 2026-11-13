"""Gravação dos arquivos de carga da balança na pasta local configurada.

O conteúdo chega pronto do EldenSys (bytes latin-1 com CRLF) e é gravado
SEM transformação: qualquer reencode aqui quebraria o layout de largura fixa
que o software da balança (Toledo MGV, Filizola, Urano) espera.

Duas garantias que este módulo existe para dar:

1. **O servidor nunca escolhe onde gravar.** A pasta é a do `config.json`
   desta máquina; do servidor vem só um nome de arquivo, e o nome passa por
   `safe_filename` — sem separador, sem `..`, só `.txt`. Um servidor (ou
   uma conta) comprometido não consegue escrever fora da pasta da balança.
2. **A balança nunca lê um arquivo pela metade.** Grava num temporário na
   mesma pasta e troca com `os.replace` (atômico no mesmo volume). O
   temporário termina em `.tmp`, que os importadores de carga ignoram.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import time
import uuid
from pathlib import Path

log = logging.getLogger("eldensys.agent.scale")

#: Carga de balança é texto pequeno (dezenas de bytes por produto). O teto
#: existe só para um job malformado não encher o disco da loja.
MAX_SCALE_FILE_BYTES = 10 * 1024 * 1024

# Começa com letra/dígito; depois só letra, dígito, ponto, hífen e sublinhado.
# Cobre txitens.txt, CADTXT.TXT, SETORTXT.TXT e produtos_urano_AAAAMMDD.txt.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$")

# Nomes de dispositivo do Windows: "CON.txt" abre o console, não um arquivo.
_RESERVED = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class ScaleFileError(Exception):
    """Falha ao validar ou gravar um arquivo de balança (mensagem pro usuário)."""


def safe_filename(name: object) -> str:
    """Valida o nome vindo do servidor. Devolve o próprio nome ou levanta.

    Não "conserta" nada (não corta caminho, não troca caractere): um nome
    fora do padrão é sinal de job errado e deve falhar com mensagem clara.
    """
    if not isinstance(name, str) or not _NAME_RE.match(name) or ".." in name:
        raise ScaleFileError(f"Nome de arquivo não permitido: {name!r}")
    stem, dot, ext = name.rpartition(".")
    if not dot or ext.lower() != "txt":
        raise ScaleFileError(f"Só arquivos .txt são aceitos (recebido: {name!r})")
    if stem.split(".")[0].upper() in _RESERVED:
        raise ScaleFileError(f"Nome de arquivo reservado pelo Windows: {name!r}")
    return name


def write_scale_file(
    folder: str | os.PathLike[str],
    filename: str,
    data: bytes,
    sha256: str | None = None,
    retries: int = 5,
    retry_delay: float = 0.4,
) -> Path:
    """Grava `data` em `<folder>/<filename>`, sobrescrevendo de forma atômica.

    `sha256` (hex), quando informado, é conferido antes de tocar no disco.
    Levanta `ScaleFileError` com mensagem pronta pra aparecer no EldenSys.
    """
    name = safe_filename(filename)

    if len(data) > MAX_SCALE_FILE_BYTES:
        raise ScaleFileError(f"Arquivo {name} grande demais ({len(data)} bytes).")
    if sha256 and hashlib.sha256(data).hexdigest() != sha256.strip().lower():
        raise ScaleFileError(f"Arquivo {name} chegou corrompido (sha256 não confere).")

    # Path("") vira "." (pasta atual) e passaria no is_dir — por isso o teste
    # de vazio vem antes.
    folder_str = os.fspath(folder).strip()
    if not folder_str:
        raise ScaleFileError("Pasta da balança não configurada no agente.")
    base = Path(folder_str)
    if not base.is_dir():
        raise ScaleFileError(f"Pasta da balança não encontrada: {folder_str}")

    target = base / name
    # Cinto e suspensório: mesmo com o nome validado, o destino resolvido
    # (seguindo link simbólico, se houver) tem que morar dentro da pasta.
    try:
        if target.resolve().parent != base.resolve():
            raise ScaleFileError(f"Destino fora da pasta da balança: {name!r}")
    except OSError as e:
        raise ScaleFileError(f"Pasta da balança inacessível: {e}") from e

    tmp = base / f".{name}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as e:
            raise ScaleFileError(f"Não foi possível gravar na pasta da balança: {e}") from e

        # O software da balança pode estar com o arquivo antigo aberto bem
        # na hora da troca (PermissionError no Windows): tenta de novo.
        last_error: OSError | None = None
        for attempt in range(max(1, retries)):
            try:
                os.replace(tmp, target)
                log.info("Arquivo de balança gravado: %s (%d bytes)", target, len(data))
                return target
            except PermissionError as e:
                last_error = e
                if attempt < retries - 1:
                    time.sleep(retry_delay)
            except OSError as e:
                raise ScaleFileError(f"Não foi possível gravar {name}: {e}") from e
        raise ScaleFileError(
            f"Não foi possível substituir {name}: o arquivo está em uso "
            f"(o programa da balança está com ele aberto?). {last_error}"
        )
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass  # já virou o arquivo final (os.replace) ou nunca existiu
