from __future__ import annotations

import json
import zipfile
from pathlib import Path

from core.modinfo import parse_mod_info, select_info_json_path


def _write_zip(path: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)


def test_select_info_prefers_mod_info() -> None:
    selected = select_info_json_path([
        "vehicles/car/info.json",
        "mod_info/carpack/info.json",
        "info.json",
    ])
    assert selected == "mod_info/carpack/info.json"


def test_select_info_prefers_root_when_no_mod_info() -> None:
    selected = select_info_json_path([
        "a/b/info.json",
        "info.json",
        "vehicles/c/info.json",
    ])
    assert selected == "info.json"


def test_select_info_same_level_uses_alphanumeric_path() -> None:
    selected = select_info_json_path([
        "vehicles/z_pack/info.json",
        "vehicles/A_pack/info.json",
        "vehicles/b_pack/info.json",
    ])
    assert selected == "vehicles/A_pack/info.json"


def test_parse_mod_info_with_aliases(tmp_path: Path) -> None:
    mod_zip = tmp_path / "mod.zip"
    payload = {
        "name": "My Car",
        "brand": "Acme",
        "author": "Dev",
        "country": "CA",
        "BodyStyle": "Sedan",
        "type": "Car",
        "years": "1990-1995",
        "DerbyClass": "A",
        "description": "Fast",
        "slogan": "Drive it",
    }
    _write_zip(mod_zip, {"vehicles/mycar/info.json": json.dumps(payload)})

    info = parse_mod_info(mod_zip)
    assert info is not None
    assert info["__category"] == "vehicles"
    assert info["Name"] == "My Car"
    assert info["Body Style"] == "Sedan"
    assert info["Derby Class"] == "A"


def test_parse_mod_info_levels_fields(tmp_path: Path) -> None:
    mod_zip = tmp_path / "map.zip"
    payload = {
        "title": "Big Map",
        "authors": ["A", "B"],
        "size": "large",
        "biome": "forest",
        "roads": "paved",
        "description": "Test map",
        "features": "bridges",
    }
    _write_zip(mod_zip, {"levels/bigmap/info.json": json.dumps(payload)})
    info = parse_mod_info(mod_zip)
    assert info is not None
    assert info["__category"] == "levels"
    assert info["title"] == "Big Map"
    assert info["authors"] == "A, B"
    assert info["features"] == "bridges"


def test_parse_mod_info_returns_none_for_missing_info(tmp_path: Path) -> None:
    mod_zip = tmp_path / "mod.zip"
    _write_zip(mod_zip, {"vehicles/a/readme.txt": "no info"})
    assert parse_mod_info(mod_zip) is None
