# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import ssl

# Algunos runtimes incluyen una lista adicional de certificados rechazados
# que SSLContext carga junto a ssl.py. Conservarla al empaquetar; el Python
# oficial no necesita este recurso.
ssl_resource = Path(ssl.__file__).with_name('codex-rejected-cas.pem')
ssl_data = [(str(ssl_resource), '.')] if ssl_resource.is_file() else []

a = Analysis(
    ['TFT AUTO-ACCEPT.py'],
    pathex=[],
    binaries=[],
    datas=ssl_data,
    hiddenimports=['pystray._win32'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='TFT AUTO-ACCEPT',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
