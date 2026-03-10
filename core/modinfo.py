from __future__ import annotations

from dataclasses import dataclass
import html
import json
import re
import zipfile
from pathlib import Path
from typing import Any

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

_GLOBAL_MOD_INFO_CACHE = ModInfoCache()


@dataclass(slots=True)
class InfoJsonAnalysisResult:
    exists: bool
    path: str | None
    status: str
    parsed_data: Any | None
    raw_text: str | None
    error_text: str | None
    message_raw: str | None
    message_clean: str | None
    message_html: str | None
    source_mtime: float | None
    source_size: int | None
    summary_fields: dict[str, str] | None


@dataclass(slots=True)
class _ParseJsonResult:
    parsed_data: Any | None
    status: str
    error_text: str | None


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


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _parse_json_tolerant(raw: bytes) -> _ParseJsonResult:
    text = _strip_invalid_control_chars(_decode_utf8_text(raw))
    try:
        return _ParseJsonResult(parsed_data=json.loads(text), status="valid", error_text=None)
    except json.JSONDecodeError as exc:
        direct_error = str(exc)

    repaired = _repair_missing_property_commas(text)
    repaired = _repair_trailing_commas(repaired)
    repaired_error: str | None = None
    try:
        return _ParseJsonResult(parsed_data=json.loads(repaired), status="recovered", error_text=direct_error)
    except json.JSONDecodeError as exc:
        repaired_error = str(exc)

    try:
        decoder = json.JSONDecoder()
        stripped = repaired.lstrip()
        obj, end = decoder.raw_decode(stripped)
        tail = stripped[end:].strip()
        if isinstance(obj, dict) and tail and set(tail) <= set("}]"):
            return _ParseJsonResult(parsed_data=obj, status="recovered", error_text=repaired_error or direct_error)
    except json.JSONDecodeError as exc:
        repaired_error = str(exc)

    return _ParseJsonResult(parsed_data=None, status="invalid", error_text=repaired_error or direct_error)


def clean_info_message(raw_message: str) -> str:
    text = raw_message.replace("\\/", "/")
    text = _normalize_newlines(text)

    text = re.sub(
        r"\[ATTACH\](.*?)\[/ATTACH\]",
        lambda m: f"[Attachment: {m.group(1).strip()}]",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"\[USER=[^\]]+\](.*?)\[/USER\]",
        lambda m: m.group(1).strip(),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"\[URL\](.*?)\[/URL\]",
        lambda m: m.group(1).strip(),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    def _url_with_label(match: re.Match[str]) -> str:
        url = match.group(1).strip()
        label = match.group(2).strip()
        return url if not label else f"{label} ({url})"

    text = re.sub(
        r"\[URL=([^\]]+)\](.*?)\[/URL\]",
        _url_with_label,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"\[SPOILER\](.*?)\[/SPOILER\]",
        lambda m: f"[Spoiler]\n{m.group(1).strip()}\n[/Spoiler]",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"\[\*\]", "- ", text, flags=re.IGNORECASE)
    text = re.sub(r"\[/?LIST(?:=[^\]]+)?\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\[/?(?:B|I|U|SIZE|COLOR)(?:=[^\]]+)?\]", "", text, flags=re.IGNORECASE)

    text = html.unescape(text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _safe_color(color_raw: str) -> str | None:
    value = html.unescape(color_raw).strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", value):
        return value
    if re.fullmatch(r"[a-zA-Z]{3,20}", value):
        return value.lower()
    return None


def _safe_font_size(size_raw: str) -> int | None:
    value = html.unescape(size_raw).strip()
    match = re.search(r"\d{1,3}", value)
    if not match:
        return None
    return max(8, min(36, int(match.group(0))))


def _safe_href(url_raw: str) -> str | None:
    value = html.unescape(url_raw).strip()
    if re.match(r"^(https?://|mailto:)", value, flags=re.IGNORECASE):
        return html.escape(value, quote=True)
    return None


def _replace_until_stable(text: str, pattern: str, repl, max_iter: int = 8) -> str:
    out = text
    for _ in range(max_iter):
        next_text, count = re.subn(pattern, repl, out, flags=re.IGNORECASE | re.DOTALL)
        out = next_text
        if count == 0:
            break
    return out


def render_info_message_html(raw_message: str) -> str:
    text = raw_message.replace("\\/", "/")
    text = _normalize_newlines(text)
    text = html.escape(text, quote=True)

    text = _replace_until_stable(
        text,
        r"\[ATTACH\](.*?)\[/ATTACH\]",
        lambda m: f"<span>[Attachment: {m.group(1).strip()}]</span>",
    )
    text = _replace_until_stable(
        text,
        r"\[USER=[^\]]+\](.*?)\[/USER\]",
        lambda m: f"<span>{m.group(1).strip()}</span>",
    )

    def _url_inline(match: re.Match[str]) -> str:
        raw_target = match.group(1).strip()
        href = _safe_href(raw_target)
        label = raw_target
        if href is None:
            return label
        return f'<a href="{href}">{label}</a>'

    text = _replace_until_stable(text, r"\[URL\](.*?)\[/URL\]", _url_inline)

    def _url_labeled(match: re.Match[str]) -> str:
        raw_target = match.group(1).strip()
        label = match.group(2).strip()
        href = _safe_href(raw_target)
        if href is None:
            return f"{label} ({raw_target})" if label and label != raw_target else raw_target
        shown = label or raw_target
        return f'<a href="{href}">{shown}</a>'

    text = _replace_until_stable(text, r"\[URL=([^\]]+)\](.*?)\[/URL\]", _url_labeled)

    text = _replace_until_stable(
        text,
        r"\[SPOILER\](.*?)\[/SPOILER\]",
        lambda m: (
            '<div style="border:1px solid #8f8f8f; border-radius:4px; padding:6px; margin:4px 0;">'
            "<b>Spoiler</b><br/>"
            f"{m.group(1).strip()}"
            "</div>"
        ),
    )

    def _list_block(match: re.Match[str]) -> str:
        inner = match.group(1)
        parts = re.split(r"\[\*\]", inner, flags=re.IGNORECASE)
        items = [p.strip() for p in parts if p.strip()]
        if not items:
            return ""
        rendered = "".join(f"<li>{item}</li>" for item in items)
        return f"<ul>{rendered}</ul>"

    text = _replace_until_stable(text, r"\[LIST(?:=[^\]]+)?\](.*?)\[/LIST\]", _list_block)
    text = re.sub(r"\[\*\]", "• ", text, flags=re.IGNORECASE)

    text = _replace_until_stable(text, r"\[B\](.*?)\[/B\]", r"<b>\1</b>")
    text = _replace_until_stable(text, r"\[I\](.*?)\[/I\]", r"<i>\1</i>")
    text = _replace_until_stable(text, r"\[U\](.*?)\[/U\]", r"<u>\1</u>")

    def _color_block(match: re.Match[str]) -> str:
        color = _safe_color(match.group(1))
        body = match.group(2)
        if color is None:
            return body
        return f'<span style="color:{color};">{body}</span>'

    text = _replace_until_stable(text, r"\[COLOR=([^\]]+)\](.*?)\[/COLOR\]", _color_block)

    def _size_block(match: re.Match[str]) -> str:
        size = _safe_font_size(match.group(1))
        body = match.group(2)
        if size is None:
            return body
        return f'<span style="font-size:{size}px;">{body}</span>'

    text = _replace_until_stable(text, r"\[SIZE=([^\]]+)\](.*?)\[/SIZE\]", _size_block)

    text = re.sub(r"\[/?[A-Z]+(?:=[^\]]+)?\]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.replace("\n", "<br/>")
    return f"<div>{text}</div>"


def _extract_summary_fields(parsed_data: Any, selected: str) -> dict[str, str] | None:
    if not isinstance(parsed_data, dict):
        return None

    lower_data = {str(k).lower(): v for k, v in parsed_data.items()}
    category = _category_for_info_path(selected)
    line2_keys, line3_keys = _field_layout_for_category(category)
    result: dict[str, str] = {"__category": category, "__info_path": selected}
    for out_key in line2_keys + line3_keys:
        aliases = _FIELD_ALIASES.get(out_key, [out_key])
        value = _extract_field(lower_data, aliases)
        if value:
            result[out_key] = value
    return result


def _source_signature(zip_path: Path) -> tuple[float | None, int | None]:
    try:
        stat = zip_path.stat()
    except OSError:
        return None, None
    return float(stat.st_mtime), int(stat.st_size)


def analyze_info_json(zip_path: Path) -> InfoJsonAnalysisResult:
    source_mtime, source_size = _source_signature(zip_path)
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            selected = select_info_json_path(zf.namelist())
            if not selected:
                return InfoJsonAnalysisResult(
                    exists=False,
                    path=None,
                    status="missing",
                    parsed_data=None,
                    raw_text=None,
                    error_text=None,
                    message_raw=None,
                    message_clean=None,
                    message_html=None,
                    source_mtime=source_mtime,
                    source_size=source_size,
                    summary_fields=None,
                )
            raw = zf.read(selected)
    except (OSError, zipfile.BadZipFile, KeyError) as exc:
        return InfoJsonAnalysisResult(
            exists=False,
            path=None,
            status="invalid",
            parsed_data=None,
            raw_text=None,
            error_text=str(exc),
            message_raw=None,
            message_clean=None,
            message_html=None,
            source_mtime=source_mtime,
            source_size=source_size,
            summary_fields=None,
        )

    parse_result = _parse_json_tolerant(raw)
    parsed_data = parse_result.parsed_data
    message_raw: str | None = None
    message_clean: str | None = None
    message_html: str | None = None
    if isinstance(parsed_data, dict):
        raw_message = parsed_data.get("message")
        if isinstance(raw_message, str):
            message_raw = raw_message
            message_clean = clean_info_message(raw_message)
            message_html = render_info_message_html(raw_message)

    summary_fields = _extract_summary_fields(parsed_data, selected)
    return InfoJsonAnalysisResult(
        exists=True,
        path=selected,
        status=parse_result.status,
        parsed_data=parsed_data,
        raw_text=_normalize_newlines(_decode_utf8_text(raw)),
        error_text=parse_result.error_text,
        message_raw=message_raw,
        message_clean=message_clean,
        message_html=message_html,
        source_mtime=source_mtime,
        source_size=source_size,
        summary_fields=summary_fields,
    )


def get_info_json_analysis_cached(zip_path: Path, cache: ModInfoCache | None = None) -> InfoJsonAnalysisResult:
    active_cache = cache or _GLOBAL_MOD_INFO_CACHE
    cached = active_cache.get_analysis(zip_path)
    if cached is not _MISS:
        return cached  # type: ignore[return-value]
    analyzed = analyze_info_json(zip_path)
    active_cache.put_analysis(zip_path, analyzed, analyzed.summary_fields)
    return analyzed


def set_default_mod_info_cache(cache: ModInfoCache) -> None:
    global _GLOBAL_MOD_INFO_CACHE
    _GLOBAL_MOD_INFO_CACHE = cache


def parse_mod_info(zip_path: Path) -> dict[str, str] | None:
    analysis = get_info_json_analysis_cached(zip_path)
    return analysis.summary_fields


def parse_mod_info_raw(zip_path: Path) -> dict[str, Any] | None:
    analysis = get_info_json_analysis_cached(zip_path)
    if isinstance(analysis.parsed_data, dict):
        return analysis.parsed_data
    return None


def has_info_json(zip_path: Path) -> bool:
    analysis = get_info_json_analysis_cached(zip_path)
    return analysis.exists


def get_mod_info_cached(zip_path: Path, cache: ModInfoCache) -> dict[str, str] | None:
    analysis = get_info_json_analysis_cached(zip_path, cache)
    return analysis.summary_fields
