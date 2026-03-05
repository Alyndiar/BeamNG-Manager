from __future__ import annotations

from pathlib import Path

from core import scanner


def _mk_zip(path: Path, size: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def test_build_full_index_with_mixed_sources(tmp_path: Path, monkeypatch) -> None:
    beam = tmp_path / "beam"
    library = tmp_path / "library"
    other = tmp_path / "other_target"

    (beam / "repo").mkdir(parents=True)
    (beam / "career").mkdir(parents=True)
    (beam / "mystery").mkdir(parents=True)
    (beam / "orphan").mkdir(parents=True)

    (library / "career").mkdir(parents=True)
    (library / "fun").mkdir(parents=True)
    other.mkdir(parents=True)

    _mk_zip(beam / "loose.zip")
    _mk_zip(beam / "repo" / "deep" / "repo_mod.zip")
    _mk_zip(beam / "orphan" / "orphan_mod.zip")
    _mk_zip(library / "career" / "car1.zip")
    _mk_zip(library / "fun" / "car2.zip")
    _mk_zip(other / "unknown.zip")

    monkeypatch.setattr(
        scanner.junctions,
        "list_junctions",
        lambda root: {"career": library / "career", "mystery": other},
    )

    def fake_is_junction(path: Path) -> bool:
        return path.name.lower() in {"career", "mystery"}

    monkeypatch.setattr(scanner.junctions, "is_junction", fake_is_junction)

    index = scanner.build_full_index(beam, library)

    assert index.packs == ["career", "fun"]
    assert set(index.active_packs.keys()) == {"career"}
    assert "mystery" in index.unknown_junctions
    assert "orphan" in index.orphan_folders
    assert len(index.loose_mods) == 1
    assert len(index.repo_mods) == 1
    assert len(index.pack_mods["career"]) == 1
    assert index.totals.packs_active == 1


def test_refresh_after_toggle_reuses_pack_cache(tmp_path: Path, monkeypatch) -> None:
    beam = tmp_path / "beam"
    library = tmp_path / "library"
    (beam / "repo").mkdir(parents=True)
    (library / "career").mkdir(parents=True)
    (library / "fun").mkdir(parents=True)
    _mk_zip(library / "career" / "one.zip")
    _mk_zip(library / "fun" / "two.zip")

    monkeypatch.setattr(scanner.junctions, "list_junctions", lambda root: {})
    monkeypatch.setattr(scanner.junctions, "is_junction", lambda p: False)
    base = scanner.build_full_index(beam, library)

    monkeypatch.setattr(
        scanner.junctions,
        "list_junctions",
        lambda root: {"career": library / "career", "fun": library / "fun"},
    )

    refreshed = scanner.refresh_after_toggle(base)

    assert set(refreshed.active_packs.keys()) == {"career", "fun"}
    assert len(refreshed.pack_mods["career"]) == 1
    assert len(refreshed.pack_mods["fun"]) == 1
