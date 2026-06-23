# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller spec for the ZD Ultimate Legend wrapper.

Build:
    .venv-zd\Scripts\python.exe -m PyInstaller --noconfirm pyinstaller_main_zd.spec

Output: dist/ZDUltimateLegend/  (zip this folder for distribution)
"""

from pathlib import Path


block_cipher = None

datas = [
    ("assets/fonts/*.ttf", "assets/fonts"),
    ("assets/fonts/*.otf", "assets/fonts"),
    ("assets/licenses/*.txt", "assets/licenses"),
    ("zd_app/i18n/locales/*.json", "zd_app/i18n/locales"),
    ("zd_app/protocol/probe_official_connection_state.ps1", "zd_app/protocol"),
    ("LICENSE", "."),
    ("NOTICE", "."),
]

hiddenimports = [
    # Most imports are auto-detected. Add here only if PyInstaller misses one.
]

a = Analysis(
    ["main_zd.py"],
    pathex=[str(Path(".").resolve())],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "tkinter",
        "unittest",
        "pytest",
        "frida",
        "IPython",
        "jupyter",
    ],
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
    name="ZD Ultimate Legend",
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
    version="version_info.txt",
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ZDUltimateLegend",
)
