from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import pickle
import threading


@dataclass(slots=True)
class ModEntry:
    path: Path
    size: int
    source: str
    mtime_ns: int | None = None
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
    data: dict[str, str] | None = None
    analysis: object | None = None


class ModInfoCache:
    def __init__(self) -> None:
        self._cache: dict[str, ModInfoCacheItem] = {}
        self._lock = threading.Lock()
        self._index_signatures: dict[str, tuple[int, int]] = {}

    def _signature(self, path: Path) -> tuple[int, int] | None:
        key = str(path)
        with self._lock:
            indexed = self._index_signatures.get(key)
        if indexed is not None:
            return indexed
        try:
            stat = path.stat()
        except OSError:
            return None
        return int(stat.st_mtime_ns), int(stat.st_size)

    def update_index_signatures(self, signatures: dict[str, tuple[int, int]]) -> None:
        normalized = {str(path): (int(mtime_ns), int(size)) for path, (mtime_ns, size) in signatures.items()}
        with self._lock:
            self._index_signatures = normalized
            stale_keys: list[str] = []
            for key, item in self._cache.items():
                sig = self._index_signatures.get(key)
                if sig is None:
                    stale_keys.append(key)
                    continue
                if item.mtime_ns != sig[0] or item.size != sig[1]:
                    stale_keys.append(key)
            for key in stale_keys:
                self._cache.pop(key, None)

    def load_from_file(self, cache_file: Path) -> None:
        if not cache_file.is_file():
            return
        try:
            raw = cache_file.read_bytes()
            payload = pickle.loads(raw)
        except (OSError, pickle.PickleError, AttributeError, ValueError, EOFError):
            return
        if not isinstance(payload, dict):
            return
        if payload.get("version") != 1:
            return
        entries = payload.get("entries")
        if not isinstance(entries, dict):
            return
        loaded: dict[str, ModInfoCacheItem] = {}
        for key, item in entries.items():
            if not isinstance(key, str) or not isinstance(item, ModInfoCacheItem):
                continue
            loaded[key] = item
        with self._lock:
            self._cache.update(loaded)

    def save_to_file(self, cache_file: Path) -> None:
        with self._lock:
            payload = {"version": 1, "entries": dict(self._cache)}
        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(pickle.dumps(payload, protocol=pickle.HIGHEST_PROTOCOL))
        except OSError:
            return

    def get(self, path: Path) -> dict[str, str] | None | object:
        key = str(path)
        sig = self._signature(path)
        if sig is None:
            return _MISS
        mtime_ns, size = sig
        with self._lock:
            item = self._cache.get(key)
        if item and item.mtime_ns == mtime_ns and item.size == size:
            return item.data
        return _MISS

    def put(self, path: Path, data: dict[str, str] | None) -> None:
        sig = self._signature(path)
        if sig is None:
            return
        mtime_ns, size = sig
        key = str(path)
        with self._lock:
            existing = self._cache.get(key)
            analysis = existing.analysis if existing is not None else None
            self._cache[key] = ModInfoCacheItem(mtime_ns, size, data, analysis)

    def get_analysis(self, path: Path) -> object:
        key = str(path)
        sig = self._signature(path)
        if sig is None:
            return _MISS
        mtime_ns, size = sig
        with self._lock:
            item = self._cache.get(key)
        if item and item.mtime_ns == mtime_ns and item.size == size and item.analysis is not None:
            return item.analysis
        return _MISS

    def put_analysis(self, path: Path, analysis: object, summary: dict[str, str] | None) -> None:
        sig = self._signature(path)
        if sig is None:
            return
        mtime_ns, size = sig
        with self._lock:
            self._cache[str(path)] = ModInfoCacheItem(mtime_ns, size, summary, analysis)


_MISS = object()
