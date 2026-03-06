from __future__ import annotations

import zipfile
from pathlib import Path

from core.modpreview import read_preview_image, read_preview_image_bytes, select_preview_image_path


def _write_zip(path: Path, files: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in files.items():
            zf.writestr(name, body)


def test_select_preview_prefers_mod_info_images() -> None:
    selected = select_preview_image_path(
        [
            "vehicles/car/default.png",
            "mod_info/zpack/images/c.jpg",
            "mod_info/apack/images/b.jpg",
        ]
    )
    assert selected == "mod_info/apack/images/b.jpg"


def test_select_preview_prefers_levels_before_vehicles() -> None:
    selected = select_preview_image_path(
        [
            "levels/map/default.jpg",
            "vehicles/car/default.png",
        ]
    )
    assert selected == "levels/map/default.jpg"


def test_select_preview_checks_default_and_preview_together_and_picks_shallower() -> None:
    selected = select_preview_image_path(
        [
            "levels/map/art/deep/default_albedo.jpg",
            "levels/map/LaserPlayground_preview.jpg",
        ]
    )
    assert selected == "levels/map/LaserPlayground_preview.jpg"


def test_select_preview_levels_priority_overrides_vehicles_depth() -> None:
    selected = select_preview_image_path(
        [
            "levels/map/art/deep/default_albedo.jpg",
            "vehicles/car/default.png",
        ]
    )
    assert selected == "levels/map/art/deep/default_albedo.jpg"


def test_select_preview_fallback_shortest_then_alpha() -> None:
    selected = select_preview_image_path(
        [
            "a/zzz/default.png",
            "a/aaa/default.jpg",
            "x/y/z/default.jpeg",
        ]
    )
    assert selected == "a/aaa/default.jpg"


def test_select_preview_uses_preview_fallback_when_default_rules_fail() -> None:
    selected = select_preview_image_path(["vehicles/car/image.webp", "ui/car_preview.webp", "readme.txt"])
    assert selected == "ui/car_preview.webp"


def test_select_preview_fallback_prefers_shortest_then_alpha_for_preview() -> None:
    selected = select_preview_image_path(
        [
            "a/z_preview.webp",
            "a/a_preview.png",
            "x/y/z_preview.webp",
        ]
    )
    assert selected == "a/a_preview.png"


def test_select_preview_returns_none_without_any_candidate() -> None:
    selected = select_preview_image_path(["vehicles/car/default.avif", "readme.txt"])
    assert selected is None


def test_select_preview_uses_mod_info_icon_as_last_priority() -> None:
    selected = select_preview_image_path(
        [
            "readme.txt",
            "mod_info/icon.jpg",
            "mod_info/pack/icon.png",
        ]
    )
    assert selected == "mod_info/icon.jpg"


def test_select_preview_keeps_preview_priority_over_mod_info_icon_last_fallback() -> None:
    selected = select_preview_image_path(
        [
            "mod_info/icon.jpg",
            "levels/map/splash_preview.webp",
        ]
    )
    assert selected == "levels/map/splash_preview.webp"


def test_select_preview_uses_main_as_extra_last_fallback() -> None:
    selected = select_preview_image_path(
        [
            "docs/readme.txt",
            "levels/map/mainSplash.webp",
            "a/deeper/mainImage.bmp",
        ]
    )
    assert selected == "levels/map/mainSplash.webp"


def test_select_preview_keeps_icon_priority_over_main_last_fallback() -> None:
    selected = select_preview_image_path(
        [
            "mod_info/icon.jpg",
            "levels/map/mainSplash.webp",
        ]
    )
    assert selected == "mod_info/icon.jpg"


def test_select_preview_main_fallback_ignores_non_image_extensions() -> None:
    selected = select_preview_image_path(
        [
            "levels/map/main.decals.json",
            "levels/map/mainn.jpg",
        ]
    )
    assert selected == "levels/map/mainn.jpg"


def test_select_preview_uses_last_resort_scope_priority_when_no_rule_matches() -> None:
    selected = select_preview_image_path(
        [
            "vehicles/car/aa.jpg",
            "levels/map/bb.jpg",
            "docs/cc.jpg",
        ]
    )
    assert selected == "levels/map/bb.jpg"


def test_select_preview_last_resort_uses_vehicles_then_depth_then_alpha() -> None:
    selected = select_preview_image_path(
        [
            "vehicles/z/deep/zz.jpg",
            "vehicles/a/aa.jpg",
            "vehicles/b/ab.jpg",
        ]
    )
    assert selected == "vehicles/a/aa.jpg"


def test_select_preview_never_uses_paths_with_texture() -> None:
    selected = select_preview_image_path(
        [
            "levels/map/default_texture.jpg",
            "vehicles/car/texture_preview.png",
            "levels/map/default.jpg",
        ]
    )
    assert selected == "levels/map/default.jpg"


def test_read_preview_image_bytes_uses_selected_path(tmp_path: Path) -> None:
    mod_zip = tmp_path / "mod.zip"
    _write_zip(
        mod_zip,
        {
            "vehicles/car/default.png": b"veh",
            "levels/map/default.jpg": b"lvl",
        },
    )
    assert read_preview_image_bytes(mod_zip) == b"lvl"


def test_read_preview_image_returns_selected_path_and_data(tmp_path: Path) -> None:
    mod_zip = tmp_path / "mod.zip"
    _write_zip(
        mod_zip,
        {
            "levels/map/LaserPlayground_preview.jpg": b"preview",
            "levels/map/art/deep/default_albedo.jpg": b"default",
        },
    )
    selected, data = read_preview_image(mod_zip)
    assert selected == "levels/map/LaserPlayground_preview.jpg"
    assert data == b"preview"
