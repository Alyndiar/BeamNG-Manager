from __future__ import annotations

import zipfile
from pathlib import PurePosixPath

from core.utils import safe_rel_depth

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


def _norm_zip_path(name: str) -> str:
    return name.replace("\\", "/").strip("/")


def _is_image(path_in_zip: str) -> bool:
    return PurePosixPath(path_in_zip).suffix.lower() in _IMAGE_EXTS


def _is_forbidden_texture_path(path_in_zip: str) -> bool:
    return "texture" in path_in_zip.lower()


def _basename(path_in_zip: str) -> str:
    return PurePosixPath(path_in_zip).name


def _default_name(path_in_zip: str) -> bool:
    return _basename(path_in_zip).lower().startswith("default")


def _preview_name(path_in_zip: str) -> bool:
    base = _basename(path_in_zip).lower()
    return bool(base) and "preview" in base


def _mod_info_icon_name(path_in_zip: str) -> bool:
    p = PurePosixPath(path_in_zip)
    parts = p.parts
    if not parts or parts[0].lower() != "mod_info":
        return False
    if len(parts) not in {2, 3}:
        return False
    name = p.name.lower()
    return name.startswith("icon.") and _is_image(path_in_zip)


def _main_name(path_in_zip: str) -> bool:
    p = PurePosixPath(path_in_zip)
    name = p.name.lower()
    return bool(name) and name.startswith("main") and _is_image(path_in_zip)


def _best_path(paths: list[str]) -> str | None:
    if not paths:
        return None
    return min(paths, key=lambda p: (max(0, safe_rel_depth(p) - 1), p.lower()))


def _best_named_in_scope(paths: list[str]) -> str | None:
    if not paths:
        return None
    named = [p for p in paths if _default_name(p) or _preview_name(p)]
    return _best_path(named)


def select_preview_image_path(zip_names: list[str]) -> str | None:
    names = [_norm_zip_path(name) for name in zip_names]
    image_paths = [
        name
        for name in names
        if _is_image(name) and not _is_forbidden_texture_path(name)
    ]

    mod_info_images = [
        name
        for name in image_paths
        if name.lower().startswith("mod_info/") and "/images/" in name.lower()
    ]
    if mod_info_images:
        return min(mod_info_images, key=lambda p: p.lower())

    vehicles_images = [name for name in image_paths if name.lower().startswith("vehicles/")]
    levels_images = [name for name in image_paths if name.lower().startswith("levels/")]
    best_vehicles = _best_named_in_scope(vehicles_images)
    best_levels = _best_named_in_scope(levels_images)
    if best_levels is not None:
        return best_levels
    if best_vehicles is not None:
        return best_vehicles

    fallback_named = _best_named_in_scope(image_paths)
    if fallback_named is not None:
        return fallback_named

    preview_candidates = [name for name in image_paths if _preview_name(name)]
    best_preview = _best_path(preview_candidates)
    if best_preview is not None:
        return best_preview

    icon_candidates = [name for name in image_paths if _mod_info_icon_name(name)]
    best_icon = _best_path(icon_candidates)
    if best_icon is not None:
        return best_icon

    main_candidates = [name for name in image_paths if _main_name(name)]
    best_main = _best_path(main_candidates)
    if best_main is not None:
        return best_main

    # Last-resort fallback: pick the first valid image by scope priority.
    for scope in ("mod_info/", "levels/", "vehicles/"):
        scoped = [name for name in image_paths if name.lower().startswith(scope)]
        best_scoped = _best_path(scoped)
        if best_scoped is not None:
            return best_scoped

    return _best_path(image_paths)


def read_preview_image(zip_path) -> tuple[str | None, bytes | None]:
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            selected = select_preview_image_path(zf.namelist())
            if not selected:
                return None, None
            return selected, zf.read(selected)
    except (OSError, zipfile.BadZipFile, KeyError):
        return None, None


def read_preview_image_bytes(zip_path) -> bytes | None:
    _selected, data = read_preview_image(zip_path)
    return data
