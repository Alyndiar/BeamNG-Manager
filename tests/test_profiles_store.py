from __future__ import annotations

from pathlib import Path

from core import profiles as profile_store


def test_ensure_default_profile_and_list(tmp_path: Path) -> None:
    snapshot = {"packs": {"career": True}, "mods": {"/mods/a.zip": False}}
    created = profile_store.ensure_default_profile(tmp_path, snapshot)
    assert created.name == "default.json"
    assert created.exists()
    listed = profile_store.list_profiles(tmp_path)
    assert listed and listed[0].name == "default.json"


def test_save_and_load_profile(tmp_path: Path) -> None:
    path = profile_store.profiles_dir(tmp_path) / "my_profile.json"
    snapshot = {"packs": {"career": True, "fun": False}, "mods": {"/mods/a.zip": True}}
    profile_store.save_profile(path, snapshot, profile_name="my_profile")
    loaded = profile_store.load_profile(path)
    assert loaded is not None
    assert loaded["name"] == "my_profile"
    assert loaded["packs"]["career"] is True
    assert loaded["mods"]["/mods/a.zip"] is True


def test_sanitize_profile_name() -> None:
    raw = 'bad<>:"/\\|?*name'
    assert profile_store.sanitize_profile_name(raw) == "bad_________name"
