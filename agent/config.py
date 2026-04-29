"""Agent configuration loaded from %APPDATA%\\EldenSysAgent\\config.json."""

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


def load_config() -> AgentConfig:
    if not CONFIG_PATH.exists():
        cfg = AgentConfig()
        save_config(cfg)
        return cfg
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        cfg = AgentConfig()
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        # Auto-migração: garante "*" em allowed_origins (loopback só, sem risco
        # de CORS). Resolve configs antigas que não cobrem o domínio do tenant.
        if "*" not in cfg.allowed_origins:
            cfg.allowed_origins = ["*"]
            save_config(cfg)
        return cfg
    except (json.JSONDecodeError, OSError):
        return AgentConfig()


def save_config(cfg: AgentConfig) -> None:
    CONFIG_PATH.write_text(
        json.dumps(asdict(cfg), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
