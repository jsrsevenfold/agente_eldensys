"""Gravacao da carga de balanca: o servidor nao escolhe onde gravar.

O que se garante aqui: (1) o nome que vem do EldenSys nunca escapa da pasta
configurada nesta maquina; (2) os bytes chegam ao disco identicos (latin-1 +
CRLF de largura fixa); (3) a troca do arquivo e atomica.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import scale_files  # noqa: E402
from agent.scale_files import ScaleFileError, safe_filename, write_scale_file  # noqa: E402

# Uma linha Toledo e uma com caractere latin-1 (0xC7 = C cedilha), com CRLF.
CARGA = b"0101000000100129900030PICANHA\r\n01010000002000599000A\xc7UCAR\r\n"


@pytest.mark.parametrize(
    "nome",
    ["txitens.txt", "CADTXT.TXT", "SETORTXT.TXT", "produtos_urano_20261113.txt", "a-b.c.txt"],
)
def test_nomes_reais_das_balancas_sao_aceitos(nome):
    assert safe_filename(nome) == nome


@pytest.mark.parametrize(
    "nome",
    [
        "..\\txitens.txt",
        "../txitens.txt",
        "sub/txitens.txt",
        "sub\\txitens.txt",
        "C:\\Windows\\txitens.txt",
        "\\\\servidor\\share\\txitens.txt",
        "a..txt",
        "..txt",
        ".oculto.txt",
        "txitens.exe",
        "txitens.txt.exe",
        "txitens",
        "txitens.txt ",
        "com espaco.txt",
        "CON.txt",
        "nul.TXT",
        "lpt1.txt",
        "acentuação.txt",
        "a" * 90 + ".txt",
        "",
        None,
        123,
    ],
)
def test_nomes_perigosos_sao_recusados(nome):
    with pytest.raises(ScaleFileError):
        safe_filename(nome)


def test_grava_os_bytes_identicos(tmp_path):
    destino = write_scale_file(tmp_path, "txitens.txt", CARGA)
    assert destino == tmp_path / "txitens.txt"
    # read_bytes: nada de newline traduzido nem reencode.
    assert destino.read_bytes() == CARGA


def test_sobrescreve_a_carga_anterior_sem_deixar_temporario(tmp_path):
    (tmp_path / "txitens.txt").write_bytes(b"carga antiga\r\n")
    write_scale_file(tmp_path, "txitens.txt", CARGA)
    assert (tmp_path / "txitens.txt").read_bytes() == CARGA
    assert [p.name for p in tmp_path.iterdir()] == ["txitens.txt"]


def test_sha256_correto_passa_e_errado_nao_toca_no_disco(tmp_path):
    ok = hashlib.sha256(CARGA).hexdigest()
    write_scale_file(tmp_path, "txitens.txt", CARGA, sha256=ok.upper())

    with pytest.raises(ScaleFileError, match="sha256"):
        write_scale_file(tmp_path, "outro.txt", CARGA, sha256="0" * 64)
    assert not (tmp_path / "outro.txt").exists()


@pytest.mark.parametrize("pasta", ["", "   "])
def test_pasta_vazia_nao_vira_pasta_atual(pasta, tmp_path, monkeypatch):
    # Path("") e "." — sem a guarda, o arquivo cairia na pasta de trabalho.
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ScaleFileError, match="não configurada"):
        write_scale_file(pasta, "txitens.txt", CARGA)
    assert list(tmp_path.iterdir()) == []


def test_pasta_inexistente_falha_sem_criar(tmp_path):
    alvo = tmp_path / "nao-existe"
    with pytest.raises(ScaleFileError, match="não encontrada"):
        write_scale_file(alvo, "txitens.txt", CARGA)
    assert not alvo.exists()


def test_nome_perigoso_nao_grava_fora_da_pasta(tmp_path):
    pasta = tmp_path / "balanca"
    pasta.mkdir()
    with pytest.raises(ScaleFileError):
        write_scale_file(pasta, "..\\fora.txt", CARGA)
    assert not (tmp_path / "fora.txt").exists()
    assert list(pasta.iterdir()) == []


def test_arquivo_grande_demais_e_recusado(tmp_path, monkeypatch):
    monkeypatch.setattr(scale_files, "MAX_SCALE_FILE_BYTES", 10)
    with pytest.raises(ScaleFileError, match="grande demais"):
        write_scale_file(tmp_path, "txitens.txt", CARGA)
    assert list(tmp_path.iterdir()) == []


def test_arquivo_em_uso_tenta_de_novo_e_consegue(tmp_path, monkeypatch):
    """A balanca pode estar lendo o arquivo antigo na hora da troca."""
    real_replace = scale_files.os.replace
    tentativas = {"n": 0}

    def replace_ocupado(src, dst):
        tentativas["n"] += 1
        if tentativas["n"] < 3:
            raise PermissionError("em uso")
        return real_replace(src, dst)

    monkeypatch.setattr(scale_files.os, "replace", replace_ocupado)
    write_scale_file(tmp_path, "txitens.txt", CARGA, retry_delay=0)
    assert tentativas["n"] == 3
    assert (tmp_path / "txitens.txt").read_bytes() == CARGA


def test_arquivo_sempre_em_uso_falha_com_mensagem_e_limpa_o_temporario(tmp_path, monkeypatch):
    (tmp_path / "txitens.txt").write_bytes(b"antiga")

    def sempre_ocupado(src, dst):
        raise PermissionError("em uso")

    monkeypatch.setattr(scale_files.os, "replace", sempre_ocupado)
    with pytest.raises(ScaleFileError, match="em uso"):
        write_scale_file(tmp_path, "txitens.txt", CARGA, retries=2, retry_delay=0)
    # Carga antiga intacta e nenhum .tmp esquecido na pasta da balanca.
    assert (tmp_path / "txitens.txt").read_bytes() == b"antiga"
    assert [p.name for p in tmp_path.iterdir()] == ["txitens.txt"]
