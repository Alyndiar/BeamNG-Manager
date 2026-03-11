# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import os


project_root = Path(SPECPATH).resolve().parent
build_variant = os.environ.get("BEAMNG_BUILD_VARIANT", "gui").strip().lower()
if build_variant not in {"gui", "debug"}:
    raise SystemExit(f"Unsupported BEAMNG_BUILD_VARIANT: {build_variant}")

console_mode = build_variant == "debug"
exe_name = "BeamNG-Manager-debug" if console_mode else "BeamNG-Manager"
icon_file = project_root / "ui" / "assets" / "icons" / "BeamNG-Manager.ico"
icon_path = str(icon_file) if icon_file.is_file() else None

datas = [
    (str(project_root / "ui" / "assets" / "no_preview.png"), "ui/assets"),
    (str(project_root / "ui" / "assets" / "icons" / "BeamNG-Manager.png"), "ui/assets/icons"),
    (str(project_root / "ui" / "assets" / "icons" / "BeamNG-Manager.ico"), "ui/assets/icons"),
]

a = Analysis(
    [str(project_root / "app" / "main.py")],
    pathex=[str(project_root)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
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
    name=exe_name,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=console_mode,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_path,
)
