# -*- mode: python ; coding: utf-8 -*-
import os

from PyInstaller.utils.hooks import collect_all

# Portable 7zz path: match build.sh (export SEVEN_ZZ=... before running pyinstaller on this spec)
_seven_zz = os.environ.get("SEVEN_ZZ", "/Applications/7z2600-mac/7zz")

datas = []
binaries = [(_seven_zz, ".")]
hiddenimports = []
tmp_ret = collect_all("customtkinter")
datas += tmp_ret[0]
binaries += tmp_ret[1]
hiddenimports += tmp_ret[2]


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
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
    [],
    exclude_binaries=True,
    name="7Zip-Master-GUI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
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
    upx=True,
    upx_exclude=[],
    name="7Zip-Master-GUI",
)
app = BUNDLE(
    coll,
    name="7Zip-Master-GUI.app",
    icon='app_icon.icns',
    bundle_identifier="com.sevenzip.mastergui",
)
