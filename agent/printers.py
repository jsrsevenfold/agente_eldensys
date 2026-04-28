"""Windows printer enumeration via pywin32."""

from __future__ import annotations

from typing import TypedDict

try:
    import win32print  # type: ignore
except ImportError:  # pragma: no cover - allow import on non-Windows for tooling
    win32print = None  # type: ignore


class PrinterInfo(TypedDict):
    name: str
    is_default: bool
    status: int
    port: str
    driver: str


def _ensure_win32() -> None:
    if win32print is None:
        raise RuntimeError("pywin32 não disponível: o agente requer Windows.")


def get_default_printer() -> str | None:
    _ensure_win32()
    try:
        return win32print.GetDefaultPrinter()
    except Exception:
        return None


def list_printers() -> list[PrinterInfo]:
    _ensure_win32()
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    default = get_default_printer()
    result: list[PrinterInfo] = []
    for p in win32print.EnumPrinters(flags, None, 2):
        # Level 2 returns dicts with rich info
        name = p.get("pPrinterName", "")
        result.append(
            PrinterInfo(
                name=name,
                is_default=(name == default),
                status=int(p.get("Status", 0)),
                port=p.get("pPortName", ""),
                driver=p.get("pDriverName", ""),
            )
        )
    return result


def write_raw(printer_name: str, data: bytes, doc_name: str = "EldenSys Job") -> int:
    """Send raw bytes directly to the Windows print spooler."""
    _ensure_win32()
    handle = win32print.OpenPrinter(printer_name)
    try:
        job_id = win32print.StartDocPrinter(handle, 1, (doc_name, None, "RAW"))
        try:
            win32print.StartPagePrinter(handle)
            win32print.WritePrinter(handle, data)
            win32print.EndPagePrinter(handle)
        finally:
            win32print.EndDocPrinter(handle)
        return int(job_id)
    finally:
        win32print.ClosePrinter(handle)
