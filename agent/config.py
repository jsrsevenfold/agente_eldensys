"""Agent configuration loaded from %APPDATA%\\EldenSysAgent\\config.json.

A partir da v0.2, o agente é um **intermediário puro** — ele apenas relaya
comandos de impressão do EldenSys pra impressora. Toda a configuração de
formato de cupom (largura, margens, escala, fonte) é feita no próprio
EldenSys e embutida no PDF/ESC-POS enviado.

Esta configuração mantém só o essencial: host/port do servidor local,
CORS, log e caminho do SumatraPDF.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path


def _appdata_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    p = Path(base) / "EldenSysAgent"
    p.mkdir(parents=True, exist_ok=True)
    return p


APP_DIR = _appdata_dir()
LOG_DIR = APP_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
CONFIG_PATH = APP_DIR / "config.json"

DEFAULT_ALLOWED_ORIGINS = ["*"]

# Regex que casa qualquer subdomínio dos domínios oficiais (multi-tenant).
# Ex.: https://cliente1.eldensys.com.br, https://acme.eldensys.up.railway.app
DEFAULT_ALLOWED_ORIGIN_REGEX = (
    r"^https://([a-zA-Z0-9-]+\.)*(eldensys\.com\.br|eldensys\.up\.railway\.app)$"
)


@dataclass
class AgentConfig:
    host: str = "127.0.0.1"
    port: int = 17777
    allowed_origins: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_ORIGINS))
    allowed_origin_regex: str = DEFAULT_ALLOWED_ORIGIN_REGEX
    log_level: str = "INFO"
    sumatra_path: str = ""  # auto-detected if empty


# Lista de chaves de config que existiam em versões anteriores e foram
# removidas. Quando encontradas no JSON, são ignoradas silenciosamente
# (assim instalações antigas não quebram após atualização).
_DEPRECATED_KEYS = {
    "escpos_default_width",
    "escpos_default_height",
    "escpos_default_font",
    "escpos_size_multiplier",
    "escpos_default_bold",
    "escpos_left_margin_mm",
    "escpos_right_margin_mm",
    "escpos_top_margin_mm",
    "escpos_bottom_margin_mm",
    "pdf_fit_mode",
    "pdf_scale",
    "pdf_margin_top_mm",
    "pdf_margin_right_mm",
    "pdf_margin_bottom_mm",
    "pdf_margin_left_mm",
    "pdf_thermal_shift_left_mm",
}


def load_config() -> AgentConfig:
    if not CONFIG_PATH.exists():
        cfg = AgentConfig()
        save_config(cfg)
        return cfg
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg = AgentConfig()
        # Carrega apenas chaves que existem no dataclass atual; ignora as
        # deprecated (vindas de versões antigas do agente).
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        # Auto-migração: garante "*" em allowed_origins (loopback só, sem risco
        # de CORS). Resolve configs antigas que não cobrem o domínio do tenant.
        needs_save = False
        if "*" not in cfg.allowed_origins:
            cfg.allowed_origins = ["*"]
            needs_save = True
        # Auto-migração: reescreve o arquivo se ele tem chaves deprecated,
        # purgando-as e mantendo o JSON enxuto.
        if any(k in _DEPRECATED_KEYS for k in data.keys()):
            needs_save = True
        # Auto-migração: regrava se faltar qualquer campo novo do dataclass.
        expected_keys = set(asdict(AgentConfig()).keys())
        if not expected_keys.issubset(data.keys()):
            needs_save = True
        if needs_save:
            save_config(cfg)
        return cfg
    except (json.JSONDecodeError, OSError):
        return AgentConfig()


def save_config(cfg: AgentConfig) -> None:
    CONFIG_PATH.write_text(
        json.dumps(asdict(cfg), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
