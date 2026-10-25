# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = []
datas += collect_data_files('sv_ttk')
datas += [('gui/assets', 'gui/assets'), ('gui/assets', 'assets')]

hiddenimports = []
hiddenimports += collect_submodules('core')
hiddenimports += collect_submodules('gui')
hiddenimports += ['sv_ttk', 'PIL', 'PIL.Image', 'PIL.ImageTk']

a = Analysis(
    ['diskrescue_gui.pyw'],
    pathex=['.'],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter.test', 'pytest', 'unittest'],
    noarchive=False,
    optimize=1,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='DiskRescue',
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
    uac_admin=True,
    icon='gui/assets/diskrescue.ico',
)
