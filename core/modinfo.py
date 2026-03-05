from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path

from core.cache import ModInfoCache, _MISS
from core.utils import safe_rel_depth

_FIELD_ALIASES = {
    "Name": ["name"],
    "Brand": ["brand"],
    "Title": ["title"],
    "Author": ["author"],
    "Authors": ["authors"],
    "Country": ["country"],
    "Body Style": ["body style", "bodystyle", "body_style"],
    "Type": ["type"],
    "Years": ["years"],
    "Derby Class": ["derby class", "derbyclass", "derby_class"],
    "size": ["size"],
    "biome": ["biome"],
    "roads": ["roads"],
    "version_string": ["version_string"],
    "prefix_title": ["prefix_title"],
    "username": ["username"],
    "Description": ["description"],
    "Slogan": ["slogan"],
    "features": ["features"],
    "tagline": ["tagline"],
}

_VEHICLES_LINE2 = ["Name", "Brand", "Author", "Country", "Body Style", "Type", "Years", "Derby Class"]
_VEHICLES_LINE3 = ["Description", "Slogan"]

_LEVELS_LINE2 = ["title", "authors", "size", "biome", "roads"]
_LEVELS_LINE3 = ["description", "features"]

_MOD_INFO_LINE2 = ["title", "version_string", "prefix_title", "username"]
_MOD_INFO_LINE3 = ["description", "tagline"]

_OTHER_LINE2 = [
    "Name",
    "Brand",
    "Title",
    "Author",
    "Authors",
    "Country",
    "Body Style",
    "Type",
    "Years",
    "Derby Class",
    "size",
    "biome",
    "roads",
    "version_string",
    "prefix_title",
    "username",
]
_OTHER_LINE3 = ["Description", "Slogan", "features", "tagline"]


def _norm_zip_path(name: str) -> str:
    return name.replace("\\", "/").strip("/")


def select_info_json_path(zip_names: list[str]) -> str | None:
    names = [_norm_zip_path(n) for n in zip_names]
    infos = [n for n in names if n.lower().endswith("info.json")]
    if not infos:
        return None

    mod_info = [n for n in infos if n.lower().startswith("mod_info/") and safe_rel_depth(n) >= 3]
    if mod_info:
        return min(mod_info, key=lambda n: (safe_rel_depth(n), n.lower()))

    root = [n for n in infos if safe_rel_depth(n) == 1 and n.lower() == "info.json"]
    if root:
        return root[0]

    vehicles = [n for n in infos if n.lower().startswith("vehicles/") and safe_rel_depth(n) >= 3]
    if vehicles:
        return min(vehicles, key=lambda n: (safe_rel_depth(n), n.lower()))

    return min(infos, key=lambda n: (safe_rel_depth(n), n.lower()))


def _category_for_info_path(path_in_zip: str) -> str:
    value = _norm_zip_path(path_in_zip).lower()
    if value.startswith("vehicles/"):
        return "vehicles"
    if value.startswith("levels/"):
        return "levels"
    if value.startswith("mod_info/"):
        return "mod_info"
    return "other"


def _field_layout_for_category(category: str) -> tuple[list[str], list[str]]:
    if category == "vehicles":
        return _VEHICLES_LINE2, _VEHICLES_LINE3
    if category == "levels":
        return _LEVELS_LINE2, _LEVELS_LINE3
    if category == "mod_info":
        return _MOD_INFO_LINE2, _MOD_INFO_LINE3
    return _OTHER_LINE2, _OTHER_LINE3


def _extract_field(lower_data: dict[str, object], aliases: list[str]) -> str:
    for alias in aliases:
        key = alias.lower()
        if key in lower_data and lower_data[key] not in (None, ""):
            value = lower_data[key]
            if isinstance(value, list):
                return ", ".join(str(v) for v in value)
            return str(value)
    return ""


def _repair_missing_property_commas(text: str) -> str:
    # Repair a common malformed pattern:
    #   "key": "value"
    #   "next": ...
    # by inserting the missing comma before the next property line.
    pattern = re.compile(
        r'(:\s*(?:"(?:\\.|[^"\\])*"|[\d.+\-eE]+|true|false|null|\}|\]))(\s*\r?\n\s*")'
    )
    repaired = text
    while True:
        next_text, n = pattern.subn(r"\1,\2", repaired)
        if n == 0:
            return repaired
        repaired = next_text


def _repair_trailing_commas(text: str) -> str:
    # Remove trailing commas before object/array close.
    return re.sub(r",(\s*[\]}])", r"\1", text)


def _strip_invalid_control_chars(text: str) -> str:
    # Keep tab/newline/carriage return, strip other C0 controls.
    return re.sub(r"[\x00-\x08\x0B\x0C\x0E-\x1F]", "", text)


def _decode_utf8_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("utf-8", errors="replace")


def _parse_json_tolerant(raw: bytes) -> dict | None:
    text = _strip_invalid_control_chars(_decode_utf8_text(raw))
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        repaired = _repair_missing_property_commas(text)
        repaired = _repair_trailing_commas(repaired)
        try:
            parsed = json.loads(repaired)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            try:
                decoder = json.JSONDecoder()
                obj, end = decoder.raw_decode(repaired.lstrip())
                tail = repaired.lstrip()[end:].strip()
                if isinstance(obj, dict) and tail and set(tail) <= set("}]"):
                    return obj
            except json.JSONDecodeError:
                pass
            return None


def parse_mod_info(zip_path: Path) -> dict[str, str] | None:
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            selected = select_info_json_path(zf.namelist())
            if not selected:
                return None
            raw = zf.read(selected)
    except (OSError, zipfile.BadZipFile, KeyError):
        return None

    parsed = _parse_json_tolerant(raw)
    if parsed is None:
        return None

    lower_data = {str(k).lower(): v for k, v in parsed.items()}
    category = _category_for_info_path(selected)
    line2_keys, line3_keys = _field_layout_for_category(category)

    result: dict[str, str] = {"__category": category, "__info_path": selected}
    for out_key in line2_keys + line3_keys:
        aliases = _FIELD_ALIASES.get(out_key, [out_key])
        value = _extract_field(lower_data, aliases)
        if value:
            result[out_key] = value
    return result


def has_info_json(zip_path: Path) -> bool:
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            return select_info_json_path(zf.namelist()) is not None
    except (OSError, zipfile.BadZipFile):
        return False


def get_mod_info_cached(zip_path: Path, cache: ModInfoCache) -> dict[str, str] | None:
    cached = cache.get(zip_path)
    if cached is not _MISS:
        return cached  # type: ignore[return-value]
    data = parse_mod_info(zip_path)
    cache.put(zip_path, data)
    return data
