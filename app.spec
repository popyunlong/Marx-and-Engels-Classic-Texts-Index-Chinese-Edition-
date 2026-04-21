# -*- mode: python ; coding: utf-8 -*-
from __future__ import annotations

import sys
from pathlib import Path


APP_NAME = "马恩文集全集检索程序"
APP_VERSION = "1.0.0"
BUNDLE_ID = "cn.marxengels.search"

ROOT = Path(SPECPATH).resolve()
WINDOWS_ICON = ROOT / "marx_multisize.ico"
MACOS_ICON = ROOT / "build" / "icons" / "marx_multisize.icns"

datas = [
    (str(ROOT / "templates"), "templates"),
    (str(ROOT / "static"), "static"),
    (str(ROOT / "data"), "data"),
    (str(ROOT / "config"), "config"),
]

icon_path = WINDOWS_ICON
if sys.platform == "darwin" and MACOS_ICON.exists():
    icon_path = MACOS_ICON

a = Analysis(
    [str(ROOT / "app.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(icon_path) if icon_path.exists() else None,
)

if sys.platform == "darwin":
    app = BUNDLE(
        exe,
        name=f"{APP_NAME}.app",
        icon=str(icon_path) if icon_path.exists() else None,
        bundle_identifier=BUNDLE_ID,
        info_plist={
            "CFBundleDisplayName": APP_NAME,
            "CFBundleName": APP_NAME,
            "CFBundleShortVersionString": APP_VERSION,
            "CFBundleVersion": APP_VERSION,
        },
    )
