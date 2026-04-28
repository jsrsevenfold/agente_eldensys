"""ESC/POS rendering DSL → bytes via python-escpos Win32Raw backend."""

from __future__ import annotations

import base64
from io import BytesIO
from typing import Any, Literal

from escpos.printer import Win32Raw  # type: ignore
from PIL import Image
from pydantic import BaseModel, Field

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


def render(payload: EscposPayload) -> None:
    """Render commands directly to the Windows printer queue."""
    p = Win32Raw(payload.printer)
    try:
        for cmd in payload.commands:
            _apply(p, cmd, payload.profile)
    finally:
        try:
            p.close()
        except Exception:
            pass


def _apply(p: Any, cmd: EscposCommand, profile: Profile) -> None:
    t = cmd.type.lower()
    if t == "text":
        kwargs: dict[str, Any] = {}
        if cmd.align:
            kwargs["align"] = cmd.align
        if cmd.bold is not None:
            kwargs["bold"] = cmd.bold
        if cmd.underline is not None:
            kwargs["underline"] = cmd.underline
        if cmd.width:
            kwargs["width"] = cmd.width
        if cmd.height:
            kwargs["height"] = cmd.height
        if cmd.font:
            kwargs["font"] = cmd.font
        if kwargs:
            p.set(**kwargs)
        p.text((cmd.text or "") + "\n")
        # reset to defaults after styled lines
        p.set(align="left", bold=False, underline=0, width=1, height=1, font="a")
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
