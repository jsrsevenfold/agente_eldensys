# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for EldenSys Agent (Windows, --onedir, no console)."""

import os
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

ROOT = Path(os.getcwd())

datas = []
# Bundle escpos package data (capabilities.json, etc.)
datas += collect_data_files('escpos')
# Bundle assets and SumatraPDF if present
if (ROOT / "assets").exists():
    datas.append((str(ROOT / "assets"), "assets"))
if (ROOT / "vendor" / "SumatraPDF.exe").exists():
    datas.append((str(ROOT / "vendor" / "SumatraPDF.exe"), "vendor"))

# Leitor biométrico (v0.6): DLL x64 do SDK do iDBio em _internal/vendor/idbio,
# onde agent/biometria/sdk.py procura. O build.ps1 confere presença, SHA-256 e
# PE x64 antes; com -SkipBiometria ele liga ELDENSYS_SEM_BIOMETRIA e a DLL não
# entra (o agente sobe e as rotas /biometria respondem sdk_ausente).
binaries = []
if os.environ.get("ELDENSYS_SEM_BIOMETRIA") != "1":
    for dll in sorted((ROOT / "vendor" / "idbio").glob("*.dll")):
        binaries.append((str(dll), "vendor/idbio"))

hiddenimports = [
    "win32print",
    "win32api",
    "win32event",
    "winerror",
    "pystray._win32",
    "PIL.Image",
    "PIL.ImageDraw",
    # Prévia da digital no cadastro (agent/biometria/cadastro.py).
    "PIL.ImageFilter",
    "PIL.PngImagePlugin",
    "escpos.printer",
    "tkinter",
    "tkinter.ttk",
    "tkinter.messagebox",
    "tkinter.filedialog",
    # Balança de checkout (agent/balanca/porta.py). O pyserial escolhe o
    # backend e a listagem de portas por os.name em tempo de execução: o
    # PyInstaller não enxerga esses imports sozinho.
    "serial",
    "serial.serialwin32",
    "serial.win32",
    "serial.tools.list_ports",
    "serial.tools.list_ports_windows",
    "serial.tools.list_ports_common",
]

a = Analysis(
    ["run_agent.py"],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["matplotlib", "scipy", "numpy.testing"],
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
