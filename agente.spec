# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for EldenSys Agent (Windows, --onedir, no console)."""

import os
from pathlib import Path

block_cipher = None

ROOT = Path(os.getcwd())

datas = []
# Bundle assets and SumatraPDF if present
if (ROOT / "assets").exists():
    datas.append((str(ROOT / "assets"), "assets"))
if (ROOT / "vendor" / "SumatraPDF.exe").exists():
    datas.append((str(ROOT / "vendor" / "SumatraPDF.exe"), "vendor"))

hiddenimports = [
    "win32print",
    "win32api",
    "win32event",
    "winerror",
    "pystray._win32",
    "PIL.Image",
    "PIL.ImageDraw",
    "escpos.printer",
]

a = Analysis(
    ["agent/__main__.py"],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "scipy", "numpy.testing"],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="EldenSysAgent",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # no console window
    disable_windowed_traceback=False,
    icon=str(ROOT / "assets" / "icon.ico") if (ROOT / "assets" / "icon.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="EldenSysAgent",
)
