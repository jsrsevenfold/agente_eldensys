"""A versão do agente mora em três lugares; os três têm de bater.

``agent/__init__.py`` vai no ``/health`` e no heartbeat (a web libera a
biometria só a partir da 0.6.0), ``pyproject.toml`` é o pacote e o ``.iss``
dá o nome do instalador publicado no GitHub Releases.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import __version__  # noqa: E402

RAIZ = Path(__file__).resolve().parent.parent
#: Contrato da biometria: versão mínima do agente com as rotas /biometria.
VERSAO_MINIMA_BIOMETRIA = (0, 6, 0)


def _tupla(versao: str) -> tuple[int, ...]:
    return tuple(int(p) for p in versao.split("."))


def test_versao_igual_nos_tres_lugares():
    pyproject = tomllib.loads((RAIZ / "pyproject.toml").read_text(encoding="utf-8"))
    iss = (RAIZ / "installer" / "eldensys-agent.iss").read_text(encoding="utf-8")
    m = re.search(r'^#define MyAppVersion "([^"]+)"', iss, re.MULTILINE)
    assert m, "MyAppVersion não encontrado no .iss"
    assert __version__ == pyproject["project"]["version"] == m.group(1)


def test_versao_tem_a_biometria():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    assert _tupla(__version__) >= VERSAO_MINIMA_BIOMETRIA


def test_instalador_so_para_windows_64_bits():
    """A DLL do leitor é x64: instalar num Windows 32 bits quebraria a biometria."""
    iss = (RAIZ / "installer" / "eldensys-agent.iss").read_text(encoding="utf-8")
    assert re.search(r"^ArchitecturesAllowed=x64compatible\s*$", iss, re.MULTILINE)
