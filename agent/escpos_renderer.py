"""ESC/POS rendering DSL → bytes via python-escpos Win32Raw backend."""

from __future__ import annotations

import base64
from io import BytesIO
from typing import Any, Literal

from escpos.printer import Win32Raw  # type: ignore
from PIL import Image
from pydantic import BaseModel, Field

from .config import AgentConfig, load_config

Profile = Literal["58mm", "80mm"]


class EscposCommand(BaseModel):
    type: str
    # text/qrcode/barcode payload
    text: str | None = None
    # formatting
    align: Literal["left", "center", "right"] | None = None
    bold: bool | None = None
    underline: int | None = Field(default=None, ge=0, le=2)
    width: int | None = Field(default=None, ge=1, le=8)
    height: int | None = Field(default=None, ge=1, le=8)
    font: Literal["a", "b"] | None = None
    # qrcode
    size: int | None = Field(default=None, ge=1, le=16)
    # barcode
    code: str | None = None
    bc: str | None = None  # e.g. EAN13, CODE39, CODE128
    # image
    image_base64: str | None = None
    # cashdrawer
    pin: Literal[2, 5] | None = None
    # cut mode
    mode: Literal["FULL", "PART"] | None = None
    # raw bytes (base64)
    data_base64: str | None = None
    # newlines count
    count: int | None = Field(default=None, ge=1, le=20)


class EscposPayload(BaseModel):
    printer: str
    profile: Profile = "80mm"
    commands: list[EscposCommand]


def _profile_width(profile: Profile) -> int:
    return 32 if profile == "58mm" else 48


def _clamp_size(v: int) -> int:
    return max(1, min(8, int(v)))


def _effective_size(cmd_value: int | None, default: int, mult: float) -> int:
    base = cmd_value if cmd_value is not None else default
    return _clamp_size(round(base * mult))


def _mm_to_units(mm: float) -> int:
    """Converte mm pra unidades de movimento ESC/POS (1/180 polegada)."""
    return max(0, min(65535, round(mm / 25.4 * 180)))


def _emit_left_margin(p: Any, mm: float) -> None:
    """GS L nL nH — define margem esquerda."""
    if mm <= 0:
        return
    units = _mm_to_units(mm)
    try:
        p._raw(bytes([0x1D, 0x4C, units & 0xFF, (units >> 8) & 0xFF]))
    except Exception:
        pass


def _emit_print_area_width(
    p: Any, profile: Profile, left_mm: float, right_mm: float
) -> None:
    """GS W nL nH — define largura da área de impressão (subtrai margem direita).

    Largura útil = perfil_mm - left_mm - right_mm, em unidades de 1/180 pol.
    """
    if right_mm <= 0:
        return
    profile_mm = 58.0 if profile == "58mm" else 80.0
    width_mm = max(10.0, profile_mm - left_mm - right_mm)
    units = _mm_to_units(width_mm)
    try:
        p._raw(bytes([0x1D, 0x57, units & 0xFF, (units >> 8) & 0xFF]))
    except Exception:
        pass


def _emit_feed_mm(p: Any, mm: float) -> None:
    """ESC J n — avança o papel em n × (1/180 pol). Limite n=255 (≈ 36mm por chamada)."""
    if mm <= 0:
        return
    units = _mm_to_units(mm)
    try:
        while units > 0:
            chunk = min(255, units)
            p._raw(bytes([0x1B, 0x4A, chunk]))
            units -= chunk
    except Exception:
        pass


def render(payload: EscposPayload, cfg: AgentConfig | None = None) -> None:
    """Render commands directly to the Windows printer queue."""
    cfg = cfg or load_config()
    p = Win32Raw(payload.printer)
    try:
        # Margens horizontais (esquerda e direita)
        _emit_left_margin(p, cfg.escpos_left_margin_mm or 0.0)
        _emit_print_area_width(
            p,
            payload.profile,
            cfg.escpos_left_margin_mm or 0.0,
            cfg.escpos_right_margin_mm or 0.0,
        )
        # Margem superior
        _emit_feed_mm(p, cfg.escpos_top_margin_mm or 0.0)

        # Margem inferior: emitida antes do primeiro cut.
        bottom_mm = cfg.escpos_bottom_margin_mm or 0.0
        bottom_emitted = False
        for cmd in payload.commands:
            if (
                bottom_mm > 0
                and not bottom_emitted
                and cmd.type.lower() == "cut"
            ):
                _emit_feed_mm(p, bottom_mm)
                bottom_emitted = True
            _apply(p, cmd, payload.profile, cfg)
        # Sem cut no payload → emite no final mesmo assim.
        if bottom_mm > 0 and not bottom_emitted:
            _emit_feed_mm(p, bottom_mm)
    finally:
        try:
            p.close()
        except Exception:
            pass


def _apply(p: Any, cmd: EscposCommand, profile: Profile, cfg: AgentConfig) -> None:
    t = cmd.type.lower()
    mult = cfg.escpos_size_multiplier or 1.0
    default_w = cfg.escpos_default_width or 1
    default_h = cfg.escpos_default_height or 1
    default_font = cfg.escpos_default_font or "a"
    if t == "text":
        kwargs: dict[str, Any] = {}
        if cmd.align:
            kwargs["align"] = cmd.align
        # Bold: comando tem prioridade; senão usa default da config.
        kwargs["bold"] = cmd.bold if cmd.bold is not None else bool(cfg.escpos_default_bold)
        if cmd.underline is not None:
            kwargs["underline"] = cmd.underline
        kwargs["width"] = _effective_size(cmd.width, default_w, mult)
        kwargs["height"] = _effective_size(cmd.height, default_h, mult)
        kwargs["font"] = cmd.font or default_font
        p.set(**kwargs)
        p.text((cmd.text or "") + "\n")
        # reset to defaults after styled lines (respeitando config)
        p.set(
            align="left",
            bold=bool(cfg.escpos_default_bold),
            underline=0,
            width=_clamp_size(round(default_w * mult)),
            height=_clamp_size(round(default_h * mult)),
            font=default_font,
        )
    elif t == "raw_text":
        p.text(cmd.text or "")
    elif t == "newline":
        p.text("\n" * (cmd.count or 1))
    elif t == "line":
        width = _profile_width(profile)
        p.text(("-" * width) + "\n")
    elif t == "align":
        p.set(align=cmd.align or "left")
    elif t == "qrcode":
        p.qr(cmd.text or "", size=cmd.size or 6, native=True)
    elif t == "barcode":
        p.barcode(cmd.code or cmd.text or "", cmd.bc or "CODE128", width=2, height=64)
    elif t == "image":
        if not cmd.image_base64:
            return
        img = Image.open(BytesIO(base64.b64decode(cmd.image_base64)))
        p.image(img)
    elif t == "cut":
        p.cut(mode=cmd.mode or "FULL")
    elif t == "cashdrawer":
        p.cashdraw(cmd.pin or 2)
    elif t == "raw":
        if cmd.data_base64:
            p._raw(base64.b64decode(cmd.data_base64))
    else:
        raise ValueError(f"Comando desconhecido: {cmd.type}")
