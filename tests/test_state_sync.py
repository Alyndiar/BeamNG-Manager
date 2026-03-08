from __future__ import annotations

from pathlib import Path

from core.cache import ModEntry, ScanIndex
from core.state_sync import extract_active_by_db_fullpath, load_beam_db, mod_db_fullpath, save_beam_db, sync_db_from_index


def _mk_zip(path: Path, size: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


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


def test_sync_db_updates_only_active_for_existing_managed_rows(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    db = beam / "db.json"
    _mk_zip(beam / "loose.zip")
    _mk_zip(beam / "repo" / "repo_mod.zip")
    _mk_zip(lib / "career" / "pack_mod.zip")

    idx = ScanIndex(beam_mods_root=beam, beam_repo_root=beam / "repo", library_root=lib)
    idx.packs = ["career"]
    idx.active_packs = {"career": lib / "career"}
    idx.loose_mods = [ModEntry(path=beam / "loose.zip", size=1, source="loose")]
    idx.repo_mods = [ModEntry(path=beam / "repo" / "repo_mod.zip", size=1, source="repo")]
    idx.pack_mods = {
        "career": [ModEntry(path=lib / "career" / "pack_mod.zip", size=1, source="pack", pack_name="career")],
    }

    db.write_text(
        """
{
  "header": {"version": 1.1},
  "mods": {
    "loose_key": {"active": true, "fullpath": "/mods/loose.zip", "custom": "keep"},
    "repo_key": {"active": false, "fullpath": "/mods/repo/repo_mod.zip", "modData": {"name": "keep"}},
    "pack_key": {"active": true, "fullpath": "/mods/career/pack_mod.zip"},
    "external": {"active": true, "fullpath": "/mods/other/unmanaged.zip", "custom": 123}
  }
}
""".strip(),
        encoding="utf-8",
    )

    payload = sync_db_from_index(
        idx,
        db,
        active_by_db_fullpath={
            "/mods/loose.zip": False,
            "/mods/repo/repo_mod.zip": True,
            "/mods/career/pack_mod.zip": False,
            "/mods/other/unmanaged.zip": False,
        },
    )
    active_map = extract_active_by_db_fullpath(payload)

    assert active_map["/mods/loose.zip"] is False
    assert active_map["/mods/repo/repo_mod.zip"] is True
    assert active_map["/mods/career/pack_mod.zip"] is False
    assert active_map["/mods/other/unmanaged.zip"] is True

    mods = payload["mods"]
    assert mods["loose_key"]["custom"] == "keep"
    assert mods["repo_key"]["modData"] == {"name": "keep"}
    assert set(mods.keys()) == {"loose_key", "repo_key", "pack_key", "external"}


def test_sync_db_does_not_add_rows_for_missing_mods(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    db = beam / "db.json"
    _mk_zip(beam / "repo" / "repo_mod.zip")

    idx = ScanIndex(beam_mods_root=beam, beam_repo_root=beam / "repo", library_root=lib)
    idx.repo_mods = [ModEntry(path=beam / "repo" / "repo_mod.zip", size=1, source="repo")]

    db.write_text('{"header":{"version":1.1},"mods":{}}', encoding="utf-8")
    payload = sync_db_from_index(idx, db, active_by_db_fullpath={"/mods/repo/repo_mod.zip": False})
    assert payload["mods"] == {}


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


def test_sync_db_keeps_disabled_pack_rows_and_only_updates_active(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    db = beam / "db.json"
    beam.mkdir()
    _mk_zip(lib / "fun" / "fun_mod.zip")

    idx = ScanIndex(beam_mods_root=beam, beam_repo_root=beam / "repo", library_root=lib)
    idx.packs = ["fun"]
    idx.active_packs = {}
    idx.pack_mods = {
        "fun": [ModEntry(path=lib / "fun" / "fun_mod.zip", size=1, source="pack", pack_name="fun")],
    }

    db.write_text(
        '{"header":{"version":1.1},"mods":{"old_fun":{"active":true,"dirname":"/mods/fun/","fullpath":"/mods/fun/fun_mod.zip"}}}',
        encoding="utf-8",
    )
    payload = sync_db_from_index(idx, db, active_by_db_fullpath={"/mods/fun/fun_mod.zip": False})
    mods = payload["mods"]
    assert "old_fun" in mods
    assert mods["old_fun"]["active"] is False
