from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from typing import Any

from core.cache import ModEntry, ScanIndex
from core.modinfo import parse_mod_info_raw

_DB_CACHE: dict[str, tuple[int, int, dict[str, Any]]] = {}
_MODDATA_CACHE: dict[str, tuple[int, int, dict[str, Any] | None]] = {}


def _default_db_payload() -> dict[str, Any]:
    return {"header": {"version": 1.1}, "mods": {}}


def _normalize_payload(payload: dict[str, Any]) -> dict[str, Any]:
    header = payload.get("header")
    mods = payload.get("mods")
    if not isinstance(header, dict):
        header = {"version": 1.1}
    if not isinstance(mods, dict):
        mods = {}
    sorted_mods = {str(k): mods[k] for k in sorted(mods.keys(), key=lambda v: str(v).lower())}
    normalized: dict[str, Any] = {"header": header, "mods": sorted_mods}
    for key, value in payload.items():
        if key in {"header", "mods"}:
            continue
        normalized[str(key)] = value
    return normalized


def _cached_repo_mod_data(mod_path: Path) -> dict[str, Any] | None:
    key = str(mod_path.resolve())
    try:
        st = mod_path.stat()
        sig = (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        sig = (-1, -1)
    cached = _MODDATA_CACHE.get(key)
    if cached is not None:
        cached_mtime, cached_size, cached_data = cached
        if (cached_mtime, cached_size) == sig:
            return copy.deepcopy(cached_data) if isinstance(cached_data, dict) else None
    parsed = parse_mod_info_raw(mod_path)
    _MODDATA_CACHE[key] = (sig[0], sig[1], copy.deepcopy(parsed) if isinstance(parsed, dict) else None)
    return parsed


def load_beam_db(db_path: Path) -> dict[str, Any]:
    if not db_path.exists():
        return _default_db_payload()
    key = str(db_path.resolve())
    try:
        st = db_path.stat()
        cache_sig = (int(st.st_mtime_ns), int(st.st_size))
    except OSError:
        cache_sig = (-1, -1)
    cached = _DB_CACHE.get(key)
    if cached is not None:
        cached_mtime, cached_size, cached_payload = cached
        if (cached_mtime, cached_size) == cache_sig:
            return copy.deepcopy(cached_payload)
    try:
        parsed = json.loads(db_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default_db_payload()
    if not isinstance(parsed, dict):
        return _default_db_payload()
    normalized = _normalize_payload(parsed)
    _DB_CACHE[key] = (cache_sig[0], cache_sig[1], copy.deepcopy(normalized))
    return normalized


def save_beam_db(db_path: Path, payload: dict[str, Any]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_payload(payload)
    db_path.write_text(json.dumps(normalized, ensure_ascii=True, indent=2), encoding="utf-8")
    key = str(db_path.resolve())
    try:
        st = db_path.stat()
        _DB_CACHE[key] = (int(st.st_mtime_ns), int(st.st_size), copy.deepcopy(normalized))
    except OSError:
        pass


def db_modname_from_filename(filename: str) -> str:
    return Path(filename).stem.lower().strip()


def mod_db_fullpath(index: ScanIndex, mod: ModEntry) -> str:
    if mod.source == "repo":
        try:
            rel = mod.path.relative_to(index.beam_repo_root).as_posix()
        except ValueError:
            rel = mod.path.name
        return f"/mods/repo/{rel}"
    if mod.source == "pack" and mod.pack_name:
        pack_root = index.library_root / mod.pack_name
        try:
            rel = mod.path.relative_to(pack_root).as_posix()
        except ValueError:
            rel = mod.path.name
        return f"/mods/{mod.pack_name}/{rel}"
    # Default loose style.
    return f"/mods/{mod.path.name}"


def mod_db_dirname(fullpath: str) -> str:
    parent = str(Path(fullpath).parent).replace("\\", "/")
    if not parent.endswith("/"):
        parent += "/"
    return parent


def extract_active_by_db_fullpath(payload: dict[str, Any]) -> dict[str, bool]:
    mods = payload.get("mods", {})
    out: dict[str, bool] = {}
    if not isinstance(mods, dict):
        return out
    for entry in mods.values():
        if not isinstance(entry, dict):
            continue
        fp = str(entry.get("fullpath") or "").strip()
        if not fp:
            continue
        out[fp] = bool(entry.get("active", False))
    return out


def _stat_payload(path: Path) -> dict[str, Any]:
    try:
        st = path.stat()
        readonly = not os.access(path, os.W_OK)
        return {
            "accesstime": int(st.st_atime),
            "createtime": int(st.st_ctime),
            "filesize": int(st.st_size),
            "filetype": "file",
            "modtime": int(st.st_mtime),
            "readonly": bool(readonly),
        }
    except OSError:
        return {
            "accesstime": int(time.time()),
            "createtime": int(time.time()),
            "filesize": 0,
            "filetype": "file",
            "modtime": int(time.time()),
            "readonly": True,
        }


def build_db_entry(
    mod: ModEntry,
    fullpath: str,
    active: bool,
    existing: dict[str, Any] | None = None,
    repo_mod_id_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    entry = dict(existing or {})
    now = int(time.time())
    entry["active"] = bool(active)
    entry["dateAdded"] = int(entry.get("dateAdded", now))
    entry["dirname"] = mod_db_dirname(fullpath)
    entry["filename"] = mod.path.name
    entry["fullpath"] = fullpath
    entry["modType"] = str(entry.get("modType", "unknown"))
    entry["modname"] = str(entry.get("modname") or db_modname_from_filename(mod.path.name))
    entry["stat"] = _stat_payload(mod.path)

    if mod.source == "repo":
        mod_data = _cached_repo_mod_data(mod.path)
        if isinstance(mod_data, dict):
            entry["modData"] = mod_data
        elif "modData" in entry and not isinstance(entry.get("modData"), dict):
            entry.pop("modData", None)
        if repo_mod_id_map:
            mod_id = repo_mod_id_map.get(mod.path.name.lower())
            if mod_id:
                entry["modID"] = mod_id
                entry["modInfoPath"] = f"/mod_info/{mod_id}/"
    else:
        entry.pop("modData", None)
    return entry


def collect_profile_snapshot(index: ScanIndex, active_by_db_fullpath: dict[str, bool]) -> dict[str, Any]:
    packs = {name: name in index.active_packs for name in index.packs}
    mods: dict[str, bool] = {}

    for mod in index.loose_mods:
        fp = mod_db_fullpath(index, mod)
        mods[fp] = bool(active_by_db_fullpath.get(fp, True))
    for mod in index.repo_mods:
        fp = mod_db_fullpath(index, mod)
        mods[fp] = bool(active_by_db_fullpath.get(fp, True))
    for pack_name, mod_list in index.pack_mods.items():
        for mod in mod_list:
            fp = mod_db_fullpath(index, mod)
            mods[fp] = bool(active_by_db_fullpath.get(fp, True))

    return {"packs": packs, "mods": mods}


def _pick_mod_key(keep_mods: dict[str, dict[str, Any]], preferred: str, fullpath: str) -> str:
    base = preferred.strip().lower() or "unknown_mod"
    candidate = base
    index = 2
    while True:
        existing = keep_mods.get(candidate)
        if existing is None:
            return candidate
        existing_fullpath = str(existing.get("fullpath") or "")
        if existing_fullpath == fullpath:
            return candidate
        candidate = f"{base}__{index}"
        index += 1


def sync_db_from_index(
    index: ScanIndex,
    db_path: Path,
    active_by_db_fullpath: dict[str, bool],
    repo_mod_id_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    payload = load_beam_db(db_path)
    existing_mods = payload.get("mods", {})
    if not isinstance(existing_mods, dict):
        existing_mods = {}

    existing_by_fullpath: dict[str, tuple[str, dict[str, Any]]] = {}
    for mod_key, value in existing_mods.items():
        if not isinstance(value, dict):
            continue
        fp = str(value.get("fullpath") or "").strip()
        if not fp:
            continue
        existing_by_fullpath[fp] = (str(mod_key), value)

    active_packs = set(index.active_packs.keys())
    included: list[ModEntry] = []
    included.extend(index.loose_mods)
    included.extend(index.repo_mods)
    for pack_name in index.packs:
        if pack_name not in active_packs:
            continue
        included.extend(index.pack_mods.get(pack_name, []))
    included_fullpaths = {mod_db_fullpath(index, mod) for mod in included}

    keep_mods: dict[str, dict[str, Any]] = {}

    # Keep unmanaged entries but remove entries that belong to disabled packs.
    for mod_key, value in existing_mods.items():
        if not isinstance(value, dict):
            continue
        dirname = str(value.get("dirname") or "")
        fullpath = str(value.get("fullpath") or "")
        removed_for_disabled_pack = False
        for pack_name in index.packs:
            if pack_name in active_packs:
                continue
            prefix = f"/mods/{pack_name}/"
            if dirname.startswith(prefix) or fullpath.startswith(prefix):
                removed_for_disabled_pack = True
                break
        if fullpath in included_fullpaths:
            # Rebuild managed entries keyed by their modname.
            continue
        if not removed_for_disabled_pack:
            keep_mods[str(mod_key)] = value

    # Add/update entries for included mods.
    for mod in included:
        fp = mod_db_fullpath(index, mod)
        existing_pair = existing_by_fullpath.get(fp)
        existing_value = existing_pair[1] if existing_pair else None
        active = bool(active_by_db_fullpath.get(fp, existing_value.get("active", True) if existing_value else True))
        built = build_db_entry(mod, fp, active, existing=existing_value, repo_mod_id_map=repo_mod_id_map)
        preferred_key = str(built.get("modname") or db_modname_from_filename(mod.path.name))
        target_key = _pick_mod_key(keep_mods, preferred_key, fp)
        # Keep BeamNG key and entry.modname aligned.
        built["modname"] = target_key
        keep_mods[target_key] = built

    payload["mods"] = keep_mods
    payload["header"] = {"version": 1.1}
    save_beam_db(db_path, payload)
    return payload
