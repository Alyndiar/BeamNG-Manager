from __future__ import annotations

from pathlib import Path

from core import actions


def test_create_pack_and_delete_empty_pack(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()

    ok, _ = actions.create_pack("career", lib)
    assert ok
    assert (lib / "career").is_dir()

    ok, _ = actions.delete_empty_pack("career", beam, lib)
    assert ok
    assert not (lib / "career").exists()


def test_rename_pack_inactive(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    (lib / "oldpack").mkdir()

    ok, _ = actions.rename_pack("oldpack", "newpack", beam, lib)
    assert ok
    assert not (lib / "oldpack").exists()
    assert (lib / "newpack").exists()


def test_move_mod_to_pack_and_back_to_root(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    pack = lib / "career"
    beam.mkdir()
    lib.mkdir()
    pack.mkdir()

    mod = beam / "mycar.zip"
    mod.write_bytes(b"zip")

    ok, _ = actions.move_mod_to_pack(mod, "career", lib)
    assert ok
    moved = pack / "mycar.zip"
    assert moved.exists()
    assert not mod.exists()

    ok, _ = actions.move_mod_to_mods_root(moved, beam)
    assert ok
    assert (beam / "mycar.zip").exists()


def test_delete_mod_file(tmp_path: Path) -> None:
    mod = tmp_path / "delete_me.zip"
    mod.write_bytes(b"zip")
    ok, _ = actions.delete_mod_file(mod)
    assert ok
    assert not mod.exists()


def test_delete_mod_file_rejects_non_zip(tmp_path: Path) -> None:
    file_path = tmp_path / "notes.txt"
    file_path.write_text("x", encoding="utf-8")
    ok, _ = actions.delete_mod_file(file_path)
    assert not ok
    assert file_path.exists()


def test_rename_pack_active_migrates_junction(tmp_path: Path, monkeypatch) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    old_pack = lib / "oldpack"
    old_pack.mkdir()

    old_link = beam / "oldpack"

    monkeypatch.setattr(actions.junctions, "is_junction", lambda p: Path(p) == old_link)
    monkeypatch.setattr(actions.junctions, "get_junction_target", lambda p: old_pack if Path(p) == old_link else None)
    monkeypatch.setattr(actions, "beamng_is_running", lambda: False)

    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(cmd, check, capture_output, text):
        calls.append(cmd)
        return Result()

    monkeypatch.setattr(actions.subprocess, "run", fake_run)

    ok, _ = actions.rename_pack("oldpack", "newpack", beam, lib)
    assert ok
    assert (lib / "newpack").exists()
    assert ["cmd", "/c", "rmdir", str(old_link)] in calls
    assert any(cmd[:4] == ["cmd", "/c", "mklink", "/J"] for cmd in calls)
