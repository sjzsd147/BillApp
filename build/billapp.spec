# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for BillApp headless FastAPI backend sidecar (Win/macOS/Linux)."""

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT_DIR = Path(SPECPATH).resolve().parent
BACKEND_DIR = ROOT_DIR / "backend"

datas = [
    (str(ROOT_DIR / "version.json"), "."),
    (str(BACKEND_DIR / "billapp" / "core" / "schema.sql"), "billapp/core"),
    (str(BACKEND_DIR / "billapp" / "classify" / "builtin_rules.yaml"), "billapp/classify"),
]
datas += collect_data_files("certifi")

hiddenimports = (
    collect_submodules("billapp")
    + collect_submodules("uvicorn")
    + [
        "multipart",
        "python_multipart",
        "email_validator",
    ]
)

a = Analysis(
    [str(BACKEND_DIR / "billapp" / "launcher.py")],
    pathex=[str(BACKEND_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "matplotlib",
        "PIL.ImageQt",
        "PyQt5",
        "PyQt6",
        "PySide2",
        "PySide6",
        "pygame",
        "webview",
        "pythonnet",
        "clr_loader",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="billapp-backend",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="billapp-backend",
)
