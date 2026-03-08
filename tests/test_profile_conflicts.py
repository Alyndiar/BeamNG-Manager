from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication

from core.cache import ModEntry, ScanIndex
from core.state_sync import mod_db_fullpath
from ui.main_window import MainWindow, ProfileDbConflictDialog


def _ensure_app() -> QApplication:
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def test_profile_db_conflict_dialog_defaults_and_all_buttons() -> None:
    _ensure_app()
    dialog = ProfileDbConflictDialog(
        [
            ("/mods/a.zip", True, False),
            ("/mods/repo/b.zip", False, True),
        ]
    )
    assert dialog.selected_source_by_mod_fullpath() == {
        "/mods/a.zip": True,
        "/mods/repo/b.zip": True,
    }

    dialog._set_all_choices(use_profile=False)
    assert dialog.selected_source_by_mod_fullpath() == {
        "/mods/a.zip": False,
        "/mods/repo/b.zip": False,
    }

    dialog._set_all_choices(use_profile=True)
    assert dialog.selected_source_by_mod_fullpath() == {
        "/mods/a.zip": True,
        "/mods/repo/b.zip": True,
    }


def test_load_active_states_from_db_keeps_previous_when_row_is_missing(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    repo = beam / "repo"
    lib = tmp_path / "lib"
    (lib / "pack").mkdir(parents=True)
    repo.mkdir(parents=True)
    beam.mkdir(exist_ok=True)

    loose_mod = ModEntry(path=beam / "a.zip", size=1, source="loose")
    repo_mod = ModEntry(path=repo / "b.zip", size=1, source="repo")
    pack_mod = ModEntry(path=lib / "pack" / "c.zip", size=1, source="pack", pack_name="pack")

    index = ScanIndex(beam_mods_root=beam, beam_repo_root=repo, library_root=lib)
    index.loose_mods = [loose_mod]
    index.repo_mods = [repo_mod]
    index.pack_mods = {"pack": [pack_mod]}
    index.packs = ["pack"]
    index.active_packs = {"pack": lib / "pack"}

    db_path = beam / "db.json"
    loose_fp = mod_db_fullpath(index, loose_mod)
    db_path.write_text(
        json.dumps(
            {
                "header": {"version": 1.1},
                "mods": {
                    "loose": {
                        "fullpath": loose_fp,
                        "active": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    class DummyWindow:
        def __init__(self) -> None:
            self.index = index
            self.db_path = db_path
            self.active_by_db_fullpath = {
                loose_fp: False,
                mod_db_fullpath(index, repo_mod): False,
                mod_db_fullpath(index, pack_mod): False,
            }

        def _all_scanned_mod_entries(self) -> list[ModEntry]:
            return [loose_mod, repo_mod, pack_mod]

    window = DummyWindow()
    MainWindow._load_active_states_from_db(window)  # type: ignore[arg-type]

    assert window.active_by_db_fullpath[loose_fp] is True
    assert window.active_by_db_fullpath[mod_db_fullpath(index, repo_mod)] is False
    assert window.active_by_db_fullpath[mod_db_fullpath(index, pack_mod)] is False


def test_effective_profile_states_assumes_missing_profile_entry_is_inactive_for_db_rows() -> None:
    class DummyWindow:
        pass

    available = {"/mods/a.zip", "/mods/b.zip", "/mods/c.zip", "/mods/new_only_on_disk.zip"}
    profile_states = {
        "/mods/a.zip": True,
    }
    db_states = {
        "/mods/a.zip": True,
        "/mods/b.zip": True,
        "/mods/c.zip": False,
    }

    effective, conflicts = MainWindow._effective_profile_states_and_conflicts(  # type: ignore[arg-type]
        DummyWindow(),
        available,
        profile_states,
        db_states,
    )

    assert effective["/mods/a.zip"] is True
    assert effective["/mods/b.zip"] is False
    assert effective["/mods/c.zip"] is False
    assert effective["/mods/new_only_on_disk.zip"] is False
    assert conflicts == [
        ("/mods/b.zip", False, True),
    ]


def test_db_listed_pack_names_extracts_pack_names_from_db_rows() -> None:
    class DummyWindow:
        pass

    payload = {
        "header": {"version": 1.1},
        "mods": {
            "loose": {"fullpath": "/mods/loose_mod.zip"},
            "repo": {"fullpath": "/mods/repo/repo_mod.zip"},
            "pack1": {"fullpath": "/mods/CarsRL/some_mod.zip"},
            "pack2": {"dirname": "/mods/Traffic/"},
        },
    }
    packs = MainWindow._db_listed_pack_names(DummyWindow(), payload)  # type: ignore[arg-type]
    assert packs == {"CarsRL", "Traffic"}
