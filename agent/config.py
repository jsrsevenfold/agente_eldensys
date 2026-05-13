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

    # ── ESC/POS defaults (aplicados quando o comando não especifica) ──
    # Multiplicador de largura/altura do texto (1-8). Aumentar = texto maior.
    escpos_default_width: int = 1
    escpos_default_height: int = 1
    # Fonte padrão: "a" (12x24 dots, padrão) ou "b" (9x17 dots, menor).
    escpos_default_font: str = "a"
    # Multiplicadores aplicados POR CIMA do width/height vindo do comando.
    # Ex.: comando manda width=1, multiplier=2 → imprime com width=2.
    escpos_size_multiplier: float = 1.0
    # Negrito padrão para todo texto (deixa a impressão mais escura/visível).
    # Quando True, qualquer texto que não especifica bold sai em negrito.
    escpos_default_bold: bool = False
    # Margens do cupom térmico em mm. Aplicadas via comandos ESC/POS nativos:
    # - left:   GS L (set left margin)
    # - right:  GS W (set print area width) calculada a partir do perfil
    # - top:    ESC J (feed paper) no início
    # - bottom: ESC J (feed paper) antes do cut (ou no final, se não houver cut)
    escpos_left_margin_mm: float = 0.0
    escpos_right_margin_mm: float = 0.0
    escpos_top_margin_mm: float = 0.0
    escpos_bottom_margin_mm: float = 0.0

    # ── PDF print options (aplicados em /print/pdf) ──
    # Modo de ajuste do SumatraPDF: "fit", "noscale" ou "shrink".
    # - fit:     escala pra encher a página (default, melhor pra A4 e térmica)
    # - noscale: imprime 1:1 (pode cortar se PDF > papel)
    # - shrink:  só reduz se for maior que o papel
    pdf_fit_mode: str = "fit"
    # Escala aplicada ao conteúdo do PDF (1.0 = sem alteração).
    # >1.0 aumenta tudo; <1.0 reduz. Útil pra A4. Em térmica pode cortar.
    pdf_scale: float = 1.0
    # Margens adicionadas ao PDF (em mm). Empurram o conteúdo pra dentro.
    # Útil pra impressoras com margem mínima diferente do PDF de origem.
    pdf_margin_top_mm: float = 0.0
    pdf_margin_right_mm: float = 0.0
    pdf_margin_bottom_mm: float = 0.0
    pdf_margin_left_mm: float = 0.0


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
        needs_save = False
        if "*" not in cfg.allowed_origins:
            cfg.allowed_origins = ["*"]
            needs_save = True
        # Auto-migração: regrava o arquivo se faltar qualquer campo novo
        # (ex.: usuário atualizou o agente e o JSON antigo não tinha pdf_scale,
        # escpos_default_width, margens etc.).
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
