from __future__ import annotations

import shutil
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


def _valid_pack_name(name: str) -> bool:
    bad_chars = set('<>:"/\\|?*')
    value = name.strip()
    if not value or value.lower() == "repo":
        return False
    return not any(c in bad_chars for c in value)


def create_pack(pack_name: str, library_root: str | Path) -> tuple[bool, str]:
    if not _valid_pack_name(pack_name):
        return False, f"Invalid pack name: {pack_name!r}"

    pack_path = Path(library_root) / pack_name
    if pack_path.exists():
        return False, f"Pack already exists: {pack_path}"

    try:
        pack_path.mkdir(parents=False, exist_ok=False)
    except OSError as exc:
        return False, f"Failed to create pack: {exc}"
    return True, f"Created pack: {pack_name}"


def delete_empty_pack(pack_name: str, beam_mods_root: str | Path, library_root: str | Path) -> tuple[bool, str]:
    if not _valid_pack_name(pack_name):
        return False, f"Invalid pack name: {pack_name!r}"

    pack_path = Path(library_root) / pack_name
    link_path = Path(beam_mods_root) / pack_name
    if not pack_path.exists() or not pack_path.is_dir():
        return False, f"Pack does not exist: {pack_path}"

    if any(pack_path.iterdir()):
        return False, f"Pack is not empty: {pack_name}"

    if link_path.exists():
        if not junctions.is_junction(link_path):
            return False, f"Refusing to delete because destination is not a junction: {link_path}"
        target = junctions.get_junction_target(link_path)
        if target is None or norm_path(str(target)) != norm_path(str(pack_path)):
            return False, f"Junction target mismatch for active pack: {link_path}"
        if beamng_is_running():
            return False, "BeamNG is running. Close BeamNG.drive.exe first."
        result = subprocess.run(["cmd", "/c", "rmdir", str(link_path)], check=False, capture_output=True, text=True)
        if result.returncode != 0:
            msg = result.stderr.strip() or result.stdout.strip() or "rmdir failed"
            return False, msg

    try:
        pack_path.rmdir()
    except OSError as exc:
        return False, f"Failed to delete pack folder: {exc}"
    return True, f"Deleted empty pack: {pack_name}"


def rename_pack(old_name: str, new_name: str, beam_mods_root: str | Path, library_root: str | Path) -> tuple[bool, str]:
    if not _valid_pack_name(old_name) or not _valid_pack_name(new_name):
        return False, "Invalid pack name."
    if old_name == new_name:
        return True, "No rename needed."

    beam_mods = Path(beam_mods_root)
    library = Path(library_root)
    old_pack = library / old_name
    new_pack = library / new_name
    old_link = beam_mods / old_name
    new_link = beam_mods / new_name

    if not old_pack.exists() or not old_pack.is_dir():
        return False, f"Source pack does not exist: {old_pack}"
    if new_pack.exists():
        return False, f"Target pack name already exists: {new_pack}"
    if new_link.exists():
        return False, f"Target link path already exists in mods root: {new_link}"

    old_link_exists = old_link.exists()
    old_link_is_junction = junctions.is_junction(old_link)
    was_active = False
    if old_link_exists and not old_link_is_junction:
        return False, f"Cannot rename: {old_link} exists and is not a junction."
    if old_link_is_junction:
        target = junctions.get_junction_target(old_link)
        if target is None or norm_path(str(target)) != norm_path(str(old_pack)):
            return False, f"Cannot rename: junction does not point to pack path: {old_link}"
        was_active = True

    if was_active and beamng_is_running():
        return False, "BeamNG is running. Close BeamNG.drive.exe first."

    if was_active:
        result = subprocess.run(["cmd", "/c", "rmdir", str(old_link)], check=False, capture_output=True, text=True)
        if result.returncode != 0:
            msg = result.stderr.strip() or result.stdout.strip() or "rmdir failed"
            return False, msg

    try:
        old_pack.rename(new_pack)
    except OSError as exc:
        return False, f"Failed to rename pack folder: {exc}"

    if was_active:
        cmd = ["cmd", "/c", "mklink", "/J", str(new_link), str(new_pack)]
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            msg = result.stderr.strip() or result.stdout.strip() or "mklink failed"
            return False, f"Pack renamed but failed to restore active junction: {msg}"

    return True, f"Renamed pack: {old_name} -> {new_name}"


def move_mod_to_pack(mod_zip_path: str | Path, pack_name: str, library_root: str | Path) -> tuple[bool, str]:
    src = Path(mod_zip_path)
    pack_path = Path(library_root) / pack_name
    if not src.exists() or not src.is_file():
        return False, f"Mod not found: {src}"
    if src.suffix.lower() != ".zip":
        return False, "Only .zip mod files can be moved."
    if not pack_path.exists() or not pack_path.is_dir():
        return False, f"Pack not found: {pack_path}"

    dest = pack_path / src.name
    if norm_path(str(src.parent)) == norm_path(str(pack_path)):
        return True, "Mod is already in this pack."
    if dest.exists():
        return False, f"Destination already exists: {dest}"

    try:
        shutil.move(str(src), str(dest))
    except OSError as exc:
        return False, f"Move failed: {exc}"
    return True, f"Moved mod to pack '{pack_name}': {src.name}"


def move_mod_to_mods_root(mod_zip_path: str | Path, beam_mods_root: str | Path) -> tuple[bool, str]:
    src = Path(mod_zip_path)
    mods_root = Path(beam_mods_root)
    if not src.exists() or not src.is_file():
        return False, f"Mod not found: {src}"
    if src.suffix.lower() != ".zip":
        return False, "Only .zip mod files can be moved."
    if not mods_root.exists() or not mods_root.is_dir():
        return False, f"Mods root not found: {mods_root}"

    dest = mods_root / src.name
    if norm_path(str(src.parent)) == norm_path(str(mods_root)):
        return True, "Mod is already in Mods root."
    if dest.exists():
        return False, f"Destination already exists: {dest}"

    try:
        shutil.move(str(src), str(dest))
    except OSError as exc:
        return False, f"Move failed: {exc}"
    return True, f"Moved mod to Mods root: {src.name}"
