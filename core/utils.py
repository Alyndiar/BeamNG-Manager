from __future__ import annotations

import os
import re
import sys
from pathlib import Path


def human_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{int(size_bytes)} B"


def normalize_name(name: str) -> str:
    return name.strip().lower()


def normalize_signature(filename: str) -> str:
    stem = Path(filename).stem.lower()
    stem = re.sub(r"[\s._-]*v?\d+(?:\.\d+)*(?:[a-z])?$", "", stem)
    stem = re.sub(r"\s+", " ", stem).strip()
    return stem or Path(filename).name.lower()


def norm_path(path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def safe_rel_depth(path_in_zip: str) -> int:
    return len([p for p in path_in_zip.replace("\\", "/").split("/") if p])


def is_frozen_app() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root_dir() -> Path:
    if is_frozen_app():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def resource_root_dir() -> Path:
    bundled_root = getattr(sys, "_MEIPASS", None)
    if bundled_root:
        return Path(str(bundled_root))
    return Path(__file__).resolve().parents[1]


def ui_asset_path(*parts: str) -> Path:
    return resource_root_dir() / "ui" / "assets" / Path(*parts)
