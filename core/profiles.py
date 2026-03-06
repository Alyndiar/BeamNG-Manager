from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def profiles_dir(project_root: Path) -> Path:
    return project_root / "Profiles"


def list_profiles(project_root: Path) -> list[Path]:
    folder = profiles_dir(project_root)
    if not folder.exists():
        return []
    return sorted([p for p in folder.glob("*.json") if p.is_file()], key=lambda p: p.name.lower())


def load_profile(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    packs = parsed.get("packs")
    mods = parsed.get("mods")
    if not isinstance(packs, dict) or not isinstance(mods, dict):
        return None
    return parsed


def save_profile(path: Path, snapshot: dict[str, Any], profile_name: str | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "name": profile_name or path.stem,
        "saved_at": int(time.time()),
        "packs": snapshot.get("packs", {}),
        "mods": snapshot.get("mods", {}),
    }
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def sanitize_profile_name(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    bad = '<>:"/\\|?*'
    for ch in bad:
        value = value.replace(ch, "_")
    return value


def ensure_default_profile(project_root: Path, snapshot: dict[str, Any]) -> Path:
    folder = profiles_dir(project_root)
    folder.mkdir(parents=True, exist_ok=True)
    existing = list_profiles(project_root)
    if existing:
        return existing[0]
    default_path = folder / "default.json"
    save_profile(default_path, snapshot, profile_name="default")
    return default_path
