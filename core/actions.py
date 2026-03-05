from __future__ import annotations

import subprocess
from pathlib import Path

from core import junctions
from core.utils import norm_path


def beamng_is_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq BeamNG.drive.exe"],
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    return "BeamNG.drive.exe" in result.stdout


def enable_pack(pack_name: str, beam_mods_root: str | Path, library_root: str | Path) -> tuple[bool, str]:
    beam_mods = Path(beam_mods_root)
    library = Path(library_root)
    src = library / pack_name
    dest = beam_mods / pack_name

    if beamng_is_running():
        return False, "BeamNG is running. Close BeamNG.drive.exe first."

    if not src.exists() or not src.is_dir():
        return False, f"Pack folder not found: {src}"

    if dest.exists():
        if not junctions.is_junction(dest):
            return False, f"Destination exists and is not a junction: {dest}"
        target = junctions.get_junction_target(dest)
        if target is None:
            return False, f"Destination junction target is unreadable: {dest}"
        if norm_path(str(target)) != norm_path(str(src)):
            return False, f"Junction points elsewhere: {dest} -> {target}"
        return True, f"Pack already enabled: {pack_name}"

    cmd = ["cmd", "/c", "mklink", "/J", str(dest), str(src)]
    result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or "mklink failed"
        return False, msg

    return True, f"Enabled pack: {pack_name}"


def disable_pack(pack_name: str, beam_mods_root: str | Path, library_root: str | Path | None = None) -> tuple[bool, str]:
    del library_root
    beam_mods = Path(beam_mods_root)
    dest = beam_mods / pack_name

    if beamng_is_running():
        return False, "BeamNG is running. Close BeamNG.drive.exe first."

    if not dest.exists():
        return True, f"Pack already disabled: {pack_name}"

    if not junctions.is_junction(dest):
        return False, f"Refusing to remove non-junction: {dest}"

    result = subprocess.run(["cmd", "/c", "rmdir", str(dest)], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        msg = result.stderr.strip() or result.stdout.strip() or "rmdir failed"
        return False, msg

    return True, f"Disabled pack: {pack_name}"
