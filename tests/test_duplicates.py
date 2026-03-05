from __future__ import annotations

from pathlib import Path

from core.cache import ModEntry, ScanIndex
from core.duplicates import find_duplicates


def _index() -> ScanIndex:
    index = ScanIndex(
        beam_mods_root=Path("C:/mods"),
        beam_repo_root=Path("C:/mods/repo"),
        library_root=Path("C:/library"),
    )
    index.packs = ["a", "b"]
    index.active_packs = {"a": Path("C:/library/a")}
    index.pack_mods = {
        "a": [ModEntry(path=Path("C:/library/a/car.zip"), size=10, source="pack", pack_name="a")],
        "b": [ModEntry(path=Path("C:/library/b/car.zip"), size=12, source="pack", pack_name="b")],
    }
    index.loose_mods = [ModEntry(path=Path("C:/mods/car.zip"), size=9, source="loose")]
    return index


def test_duplicates_across_packs() -> None:
    groups = find_duplicates(_index())
    assert len(groups) == 1
    assert groups[0].signature == "car.zip"
    assert len(groups[0].hits) == 2


def test_duplicates_active_only() -> None:
    groups = find_duplicates(_index(), active_packs_only=True)
    assert groups == []


def test_duplicates_with_misc() -> None:
    groups = find_duplicates(_index(), include_misc_sources=True)
    assert len(groups) == 1
    assert len(groups[0].hits) == 3
