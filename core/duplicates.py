from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from core.cache import ModEntry, ScanIndex
from core.utils import normalize_name


@dataclass(slots=True)
class DuplicateHit:
    signature: str
    source: str
    pack_name: str | None
    path: Path
    active: bool


@dataclass(slots=True)
class DuplicateGroup:
    signature: str
    hits: list[DuplicateHit] = field(default_factory=list)


def _entry_signature(entry: ModEntry) -> str:
    return normalize_name(entry.path.name)


def find_duplicates(
    index: ScanIndex,
    active_packs_only: bool = False,
    include_misc_sources: bool = False,
) -> list[DuplicateGroup]:
    hits_by_sig: dict[str, list[DuplicateHit]] = {}

    active_set = set(index.active_packs.keys())

    for pack_name, entries in index.pack_mods.items():
        if active_packs_only and pack_name not in active_set:
            continue
        for entry in entries:
            sig = _entry_signature(entry)
            hits_by_sig.setdefault(sig, []).append(
                DuplicateHit(
                    signature=sig,
                    source="pack",
                    pack_name=pack_name,
                    path=entry.path,
                    active=pack_name in active_set,
                )
            )

    if include_misc_sources:
        misc_entries: list[ModEntry] = []
        misc_entries.extend(index.loose_mods)
        misc_entries.extend(index.repo_mods)
        for mods in index.orphan_folders.values():
            misc_entries.extend(mods)
        for unknown in index.unknown_junctions.values():
            misc_entries.extend(unknown.mods)

        for entry in misc_entries:
            sig = _entry_signature(entry)
            hits_by_sig.setdefault(sig, []).append(
                DuplicateHit(
                    signature=sig,
                    source=entry.source,
                    pack_name=entry.pack_name,
                    path=entry.path,
                    active=False,
                )
            )

    groups = [
        DuplicateGroup(signature=sig, hits=sorted(hits, key=lambda h: str(h.path).lower()))
        for sig, hits in hits_by_sig.items()
        if len(hits) > 1
    ]
    return sorted(groups, key=lambda g: g.signature)
