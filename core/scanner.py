from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from core import junctions
from core.cache import ModEntry, ScanIndex, ScanTotals, UnknownJunction
from core.utils import norm_path

_SPECIAL_MODS_DIRS = {"repo", "multiplayer", "mod_manifests", "modconflictresolutions"}


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


def build_full_index(beam_mods_root: str | Path, library_root: str | Path) -> ScanIndex:
    beam_mods = Path(beam_mods_root)
    library = Path(library_root)
    repo = beam_mods / "repo"

    index = ScanIndex(beam_mods_root=beam_mods, beam_repo_root=repo, library_root=library)

    pack_dirs = _pack_dirs(library)
    index.packs = [p.name for p in pack_dirs]
    for pack in pack_dirs:
        index.pack_mods[pack.name] = _scan_zips_recursive(pack, source="pack", pack_name=pack.name)

    junction_map = junctions.list_junctions(beam_mods)
    library_norm = {name: norm_path(str(library / name)) for name in index.packs}

    for pack_name in index.packs:
        target = junction_map.get(pack_name)
        if target is None:
            continue
        if norm_path(str(target)) == library_norm[pack_name]:
            index.active_packs[pack_name] = target

    for name, target in sorted(junction_map.items(), key=lambda i: i[0].lower()):
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

    for child in sorted(beam_mods.iterdir(), key=lambda p: p.name.lower()):
        if not child.is_dir() or child.name.lower() in _SPECIAL_MODS_DIRS:
            continue
        if junctions.is_junction(child):
            continue
        index.orphan_folders[child.name] = _scan_zips_recursive(child, source="orphan")

    index.loose_mods = _scan_zips_non_recursive(beam_mods, source="loose")
    index.repo_mods = _scan_zips_recursive(repo, source="repo")
    index.totals = _build_totals(index)
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
