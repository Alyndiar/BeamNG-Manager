from __future__ import annotations

import json
import zipfile
from pathlib import Path

from core.cache import ModInfoCache
from core.modinfo import analyze_info_json, get_info_json_analysis_cached, parse_mod_info, select_info_json_path


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


def test_analyze_info_json_recovered_and_clean_message(tmp_path: Path) -> None:
    mod_zip = tmp_path / "broken.zip"
    malformed = '{"title":"Broken"\n"message":"[B]Hello[/B]\\n[ATTACH]751327[/ATTACH]"}'
    _write_zip(mod_zip, {"mod_info/test/info.json": malformed})

    analysis = analyze_info_json(mod_zip)
    assert analysis.exists is True
    assert analysis.status == "recovered"
    assert isinstance(analysis.parsed_data, dict)
    assert analysis.message_raw == "[B]Hello[/B]\n[ATTACH]751327[/ATTACH]"
    assert analysis.message_clean == "Hello\n[Attachment: 751327]"
    assert analysis.message_html is not None
    assert "<b>Hello</b>" in analysis.message_html
    assert "[Attachment: 751327]" in analysis.message_html


def test_analyze_info_json_message_html_formats_common_bbcode(tmp_path: Path) -> None:
    mod_zip = tmp_path / "message.zip"
    payload = {
        "message": (
            "[B]Bold[/B] [I]Italic[/I] [U]Under[/U]\n"
            "[COLOR=#ff0000]Red[/COLOR]\n"
            "[SIZE=20]Big[/SIZE]\n"
            "[URL=https://example.com]Example[/URL]\n"
            "[LIST][*]One[*]Two[/LIST]"
        )
    }
    _write_zip(mod_zip, {"info.json": json.dumps(payload)})

    analysis = analyze_info_json(mod_zip)
    assert analysis.message_html is not None
    assert "<b>Bold</b>" in analysis.message_html
    assert "<i>Italic</i>" in analysis.message_html
    assert "<u>Under</u>" in analysis.message_html
    assert 'color:#ff0000' in analysis.message_html
    assert 'font-size:20px' in analysis.message_html
    assert 'href="https://example.com"' in analysis.message_html
    assert "<ul><li>One</li><li>Two</li></ul>" in analysis.message_html


def test_analyze_info_json_invalid_keeps_raw_text(tmp_path: Path) -> None:
    mod_zip = tmp_path / "invalid.zip"
    _write_zip(mod_zip, {"info.json": '{"title":"oops",'})

    analysis = analyze_info_json(mod_zip)
    assert analysis.exists is True
    assert analysis.status == "invalid"
    assert analysis.parsed_data is None
    assert analysis.raw_text == '{"title":"oops",'
    assert analysis.error_text


def test_info_json_cached_by_mtime_and_size(tmp_path: Path) -> None:
    mod_zip = tmp_path / "cache.zip"
    _write_zip(mod_zip, {"info.json": json.dumps({"title": "v1"})})
    cache = ModInfoCache()

    first = get_info_json_analysis_cached(mod_zip, cache)
    second = get_info_json_analysis_cached(mod_zip, cache)
    assert first is second

    _write_zip(mod_zip, {"info.json": json.dumps({"title": "v2", "description": "changed-size"})})
    third = get_info_json_analysis_cached(mod_zip, cache)
    assert third is not second
    assert isinstance(third.parsed_data, dict)
    assert third.parsed_data.get("title") == "v2"


def test_mod_info_cache_persists_between_instances(tmp_path: Path) -> None:
    mod_zip = tmp_path / "persist.zip"
    _write_zip(mod_zip, {"info.json": json.dumps({"title": "Persisted", "message": "[I]ready[/I]"})})

    cache_file = tmp_path / "mod_info_cache.pkl"
    cache_a = ModInfoCache()
    first = get_info_json_analysis_cached(mod_zip, cache_a)
    cache_a.save_to_file(cache_file)

    cache_b = ModInfoCache()
    cache_b.load_from_file(cache_file)
    second = get_info_json_analysis_cached(mod_zip, cache_b)

    assert second.path == first.path
    assert second.status == first.status
    assert second.message_clean == "ready"
