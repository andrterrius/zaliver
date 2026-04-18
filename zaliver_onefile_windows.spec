# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onefile GUI bundle for Windows (theme.qss)."""

APP_NAME = "Zaliver"

block_cipher = None

a = Analysis(
    ["src/zaliver/__main__.py"],
    pathex=["src"],
    binaries=[],
    datas=[("src/zaliver/ui/theme.qss", "zaliver/ui")],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
