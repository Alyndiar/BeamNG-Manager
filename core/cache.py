from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class ModEntry:
    path: Path
    size: int
    source: str
    pack_name: str | None = None


@dataclass(slots=True)
class UnknownJunction:
    name: str
    path: Path
    target: Path | None
    mods: list[ModEntry] = field(default_factory=list)


@dataclass(slots=True)
class ScanTotals:
    active_mods: int = 0
    total_mods: int = 0
    packs_active: int = 0
    packs_total: int = 0
    loose_mods: int = 0
    repo_mods: int = 0


@dataclass(slots=True)
class ScanIndex:
    beam_mods_root: Path
    beam_repo_root: Path
    library_root: Path
    packs: list[str] = field(default_factory=list)
    pack_mods: dict[str, list[ModEntry]] = field(default_factory=dict)
    active_packs: dict[str, Path] = field(default_factory=dict)
    unknown_junctions: dict[str, UnknownJunction] = field(default_factory=dict)
    orphan_folders: dict[str, list[ModEntry]] = field(default_factory=dict)
    loose_mods: list[ModEntry] = field(default_factory=list)
    repo_mods: list[ModEntry] = field(default_factory=list)
    totals: ScanTotals = field(default_factory=ScanTotals)


@dataclass(slots=True)
class ModInfoCacheItem:
    mtime_ns: int
    size: int
    data: dict[str, str] | None


class ModInfoCache:
    def __init__(self) -> None:
        self._cache: dict[str, ModInfoCacheItem] = {}

    def get(self, path: Path) -> dict[str, str] | None | object:
        key = str(path)
        stat = path.stat()
        item = self._cache.get(key)
        if item and item.mtime_ns == stat.st_mtime_ns and item.size == stat.st_size:
            return item.data
        return _MISS

    def put(self, path: Path, data: dict[str, str] | None) -> None:
        stat = path.stat()
        self._cache[str(path)] = ModInfoCacheItem(stat.st_mtime_ns, stat.st_size, data)


_MISS = object()
