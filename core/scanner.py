from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Callable

from core import junctions
from core.cache import ModEntry, ScanIndex, ScanTotals, UnknownJunction
from core.utils import norm_path

_SPECIAL_MODS_DIRS = {"repo", "multiplayer", "mod_manifests", "modconflictresolutions"}


ProgressCallback = Callable[[dict[str, object]], None]


def _scan_zips_recursive(root: Path, source: str, pack_name: str | None = None) -> list[ModEntry]:
    if not root.exists() or not root.is_dir():
        return []
    mods: list[ModEntry] = []
    for path in root.rglob("*.zip"):
        if path.is_file():
            try:
                size = path.stat().st_size
            except OSError:
                continue
            mods.append(ModEntry(path=path, size=size, source=source, pack_name=pack_name))
    return mods


def _scan_zips_non_recursive(root: Path, source: str) -> list[ModEntry]:
    if not root.exists() or not root.is_dir():
        return []
    mods: list[ModEntry] = []
    for path in root.glob("*.zip"):
        if path.is_file():
            try:
                size = path.stat().st_size
            except OSError:
                continue
            mods.append(ModEntry(path=path, size=size, source=source))
    return mods


def _build_totals(index: ScanIndex) -> ScanTotals:
    active_mods = 0
    for pack_name in index.active_packs:
        active_mods += len(index.pack_mods.get(pack_name, []))

    total_mods = (
        sum(len(mods) for mods in index.pack_mods.values())
        + len(index.loose_mods)
        + len(index.repo_mods)
        + sum(len(mods) for mods in index.orphan_folders.values())
        + sum(len(item.mods) for item in index.unknown_junctions.values())
    )

    return ScanTotals(
        active_mods=active_mods,
        total_mods=total_mods,
        packs_active=len(index.active_packs),
        packs_total=len(index.packs),
        loose_mods=len(index.loose_mods),
        repo_mods=len(index.repo_mods),
    )


def _pack_dirs(library_root: Path) -> list[Path]:
    if not library_root.exists() or not library_root.is_dir():
        return []
    return sorted([p for p in library_root.iterdir() if p.is_dir()], key=lambda p: p.name.lower())


def build_full_index(
    beam_mods_root: str | Path,
    library_root: str | Path,
    progress_cb: ProgressCallback | None = None,
) -> ScanIndex:
    beam_mods = Path(beam_mods_root)
    library = Path(library_root)
    repo = beam_mods / "repo"

    def emit(message: str, current: int | None = None, total: int | None = None) -> None:
        if progress_cb is None:
            return
        payload: dict[str, object] = {"message": message}
        if current is not None:
            payload["current"] = int(current)
        if total is not None:
            payload["total"] = int(total)
        progress_cb(payload)

    index = ScanIndex(beam_mods_root=beam_mods, beam_repo_root=repo, library_root=library)

    emit("Scanning library packs...")
    pack_dirs = _pack_dirs(library)
    index.packs = [p.name for p in pack_dirs]
    total_packs = len(pack_dirs)
    for pack_index, pack in enumerate(pack_dirs, start=1):
        index.pack_mods[pack.name] = _scan_zips_recursive(pack, source="pack", pack_name=pack.name)
        emit(f"Scanning library packs ({pack.name})", current=pack_index, total=total_packs)

    emit("Scanning junctions...")
    junction_map = junctions.list_junctions(beam_mods)
    library_norm = {name: norm_path(str(library / name)) for name in index.packs}

    for pack_name in index.packs:
        target = junction_map.get(pack_name)
        if target is None:
            continue
        if norm_path(str(target)) == library_norm[pack_name]:
            index.active_packs[pack_name] = target

    unknown_items = [
        (name, target)
        for name, target in sorted(junction_map.items(), key=lambda i: i[0].lower())
        if name.lower() not in _SPECIAL_MODS_DIRS and name not in index.packs
    ]
    unknown_total = len(unknown_items)
    for unknown_index, (name, target) in enumerate(unknown_items, start=1):
        if name.lower() in _SPECIAL_MODS_DIRS:
            continue
        if name in index.packs:
            continue
        mods = _scan_zips_recursive(target, source="unknown_junction") if target.exists() else []
        index.unknown_junctions[name] = UnknownJunction(
            name=name,
            path=beam_mods / name,
            target=target,
            mods=mods,
        )
        emit(f"Scanning unknown links ({name})", current=unknown_index, total=unknown_total)

    emit("Scanning orphan folders...")
    orphan_children = [
        child
        for child in sorted(beam_mods.iterdir(), key=lambda p: p.name.lower())
        if child.is_dir() and child.name.lower() not in _SPECIAL_MODS_DIRS and not junctions.is_junction(child)
    ]
    orphan_total = len(orphan_children)
    for orphan_index, child in enumerate(orphan_children, start=1):
        if not child.is_dir() or child.name.lower() in _SPECIAL_MODS_DIRS:
            continue
        if junctions.is_junction(child):
            continue
        index.orphan_folders[child.name] = _scan_zips_recursive(child, source="orphan")
        emit(f"Scanning orphan folders ({child.name})", current=orphan_index, total=orphan_total)

    emit("Scanning loose mods...")
    index.loose_mods = _scan_zips_non_recursive(beam_mods, source="loose")
    emit("Scanning repo mods...")
    index.repo_mods = _scan_zips_recursive(repo, source="repo")
    emit("Finalizing scan results...")
    index.totals = _build_totals(index)
    emit("Scan complete.")
    return index


def refresh_after_toggle(current: ScanIndex) -> ScanIndex:
    next_index = replace(current)
    next_index.active_packs = {}

    junction_map = junctions.list_junctions(current.beam_mods_root)
    library_norm = {name: norm_path(str(current.library_root / name)) for name in current.packs}

    for pack_name in current.packs:
        target = junction_map.get(pack_name)
        if target and norm_path(str(target)) == library_norm[pack_name]:
            next_index.active_packs[pack_name] = target

    existing_unknown = current.unknown_junctions
    rebuilt_unknown: dict[str, UnknownJunction] = {}
    for name, target in sorted(junction_map.items(), key=lambda i: i[0].lower()):
        if name.lower() in _SPECIAL_MODS_DIRS:
            continue
        if name in current.packs:
            continue
        cached = existing_unknown.get(name)
        if cached and cached.target and norm_path(str(cached.target)) == norm_path(str(target)):
            rebuilt_unknown[name] = cached
            continue
        mods = _scan_zips_recursive(target, source="unknown_junction") if target.exists() else []
        rebuilt_unknown[name] = UnknownJunction(
            name=name,
            path=current.beam_mods_root / name,
            target=target,
            mods=mods,
        )

    next_index.unknown_junctions = rebuilt_unknown
    next_index.totals = _build_totals(next_index)
    return next_index
