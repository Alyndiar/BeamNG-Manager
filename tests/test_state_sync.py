from __future__ import annotations

import json
import zipfile
from pathlib import Path

from core.cache import ModEntry, ScanIndex
from core.state_sync import extract_active_by_db_fullpath, load_beam_db, mod_db_fullpath, save_beam_db, sync_db_from_index


def _mk_zip(path: Path, size: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _mk_mod_zip_with_info(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mod_info/sample/info.json", json.dumps(payload))


def test_mod_db_fullpath_for_sources(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    idx = ScanIndex(beam_mods_root=beam, beam_repo_root=beam / "repo", library_root=lib)

    loose_mod = ModEntry(path=beam / "a.zip", size=1, source="loose")
    repo_mod = ModEntry(path=beam / "repo" / "r.zip", size=1, source="repo")
    pack_mod = ModEntry(path=lib / "career" / "p.zip", size=1, source="pack", pack_name="career")

    assert mod_db_fullpath(idx, loose_mod) == "/mods/a.zip"
    assert mod_db_fullpath(idx, repo_mod) == "/mods/repo/r.zip"
    assert mod_db_fullpath(idx, pack_mod) == "/mods/career/p.zip"


def test_sync_db_removes_disabled_pack_entries(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    db = beam / "db.json"
    _mk_zip(beam / "repo" / "repo_mod.zip")
    _mk_zip(beam / "loose.zip")
    _mk_zip(lib / "career" / "pack_mod.zip")
    _mk_zip(lib / "fun" / "fun_mod.zip")

    idx = ScanIndex(beam_mods_root=beam, beam_repo_root=beam / "repo", library_root=lib)
    idx.packs = ["career", "fun"]
    idx.active_packs = {"career": lib / "career"}
    idx.loose_mods = [ModEntry(path=beam / "loose.zip", size=1, source="loose")]
    idx.repo_mods = [ModEntry(path=beam / "repo" / "repo_mod.zip", size=1, source="repo")]
    idx.pack_mods = {
        "career": [ModEntry(path=lib / "career" / "pack_mod.zip", size=1, source="pack", pack_name="career")],
        "fun": [ModEntry(path=lib / "fun" / "fun_mod.zip", size=1, source="pack", pack_name="fun")],
    }

    db.write_text(
        '{"header":{"version":1.1},"mods":{"old_fun":{"active":true,"dirname":"/mods/fun/","fullpath":"/mods/fun/fun_mod.zip"}}}',
        encoding="utf-8",
    )

    payload = sync_db_from_index(idx, db, active_by_db_fullpath={})
    active_map = extract_active_by_db_fullpath(payload)

    assert "/mods/fun/fun_mod.zip" not in active_map
    assert "/mods/career/pack_mod.zip" in active_map
    assert "/mods/repo/repo_mod.zip" in active_map
    assert "/mods/loose.zip" in active_map


def test_save_beam_db_sorts_mod_keys(tmp_path: Path) -> None:
    db = tmp_path / "db.json"
    payload = {
        "header": {"version": 1.1},
        "mods": {
            "zeta_mod": {"fullpath": "/mods/zeta.zip"},
            "Alpha_mod": {"fullpath": "/mods/alpha.zip"},
            "beta_mod": {"fullpath": "/mods/beta.zip"},
        },
    }
    save_beam_db(db, payload)
    loaded = load_beam_db(db)
    assert list(loaded["mods"].keys()) == ["Alpha_mod", "beta_mod", "zeta_mod"]


def test_sync_db_rekeys_managed_entries_to_modname(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    db = beam / "db.json"
    _mk_zip(beam / "repo" / "My Car.zip")

    idx = ScanIndex(beam_mods_root=beam, beam_repo_root=beam / "repo", library_root=lib)
    idx.repo_mods = [ModEntry(path=beam / "repo" / "My Car.zip", size=1, source="repo")]

    db.write_text(
        '{"header":{"version":1.1},"mods":{"legacy_key":{"active":true,"modname":"my car","dirname":"/mods/repo/","fullpath":"/mods/repo/My Car.zip"}}}',
        encoding="utf-8",
    )

    payload = sync_db_from_index(idx, db, active_by_db_fullpath={})
    mods = payload["mods"]
    assert "my car" in mods
    assert "legacy_key" not in mods


def test_repo_entry_contains_moddata_when_info_json_exists(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    db = beam / "db.json"
    repo_mod = beam / "repo" / "repo_mod.zip"
    _mk_mod_zip_with_info(repo_mod, {"name": "Repo Mod", "version": 3, "tags": ["a", "b"]})

    idx = ScanIndex(beam_mods_root=beam, beam_repo_root=beam / "repo", library_root=lib)
    idx.repo_mods = [ModEntry(path=repo_mod, size=repo_mod.stat().st_size, source="repo")]

    payload = sync_db_from_index(idx, db, active_by_db_fullpath={})
    entry = payload["mods"]["repo_mod"]
    assert isinstance(entry.get("modData"), dict)
    assert entry["modData"]["name"] == "Repo Mod"
    assert entry["modData"]["version"] == 3


def test_modname_stays_equal_to_written_mods_key_on_collision(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    db = beam / "db.json"
    _mk_zip(beam / "car.zip")
    _mk_zip(beam / "repo" / "car.zip")

    idx = ScanIndex(beam_mods_root=beam, beam_repo_root=beam / "repo", library_root=lib)
    idx.loose_mods = [ModEntry(path=beam / "car.zip", size=1, source="loose")]
    idx.repo_mods = [ModEntry(path=beam / "repo" / "car.zip", size=1, source="repo")]

    payload = sync_db_from_index(idx, db, active_by_db_fullpath={})
    mods = payload["mods"]
    assert "car" in mods
    assert "car__2" in mods
    assert mods["car"]["modname"] == "car"
    assert mods["car__2"]["modname"] == "car__2"
