from __future__ import annotations

from dataclasses import dataclass
import html
import json
import re
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

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
_DISCARDED_MESSAGE_HOSTS = {"pp.userapi.com"}


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
        r"\[ATTACH(?:=[^\]]+)?\](.*?)\[/ATTACH\]",
        lambda m: _clean_attachment_text(m.group(1)),
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
        lambda m: "" if _should_discard_message_url(m.group(1)) else m.group(1).strip(),
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )

    def _url_with_label(match: re.Match[str]) -> str:
        url = match.group(1).strip()
        label = match.group(2).strip()
        if _should_discard_message_url(url):
            return ""
        return url if not label else f"{label} ({url})"

    text = re.sub(
        r"\[URL=([^\]]+)\](.*?)\[/URL\]",
        _url_with_label,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(
        r"\[IMG(?:=[^\]]+)?\](.*?)\[/IMG\]",
        lambda m: "" if _should_discard_message_url(m.group(1)) else m.group(1).strip(),
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
    text = re.sub(
        r"(?<![\"'=])((?:https?://|mailto:)[^\s<]+)",
        lambda m: "" if _should_discard_message_url(m.group(1)) else m.group(1),
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _safe_color(color_raw: str) -> str | None:
    value = html.unescape(color_raw).strip()
    if re.fullmatch(r"#[0-9a-fA-F]{3,8}", value):
        return value
    if re.fullmatch(r"[a-zA-Z]{3,20}", value):
        return value.lower()
    return None


def _format_css_number(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _safe_font_size(size_raw: str) -> str | None:
    value = html.unescape(size_raw).strip().lower()
    if not value:
        return None

    # Common BBCode size presets are relative levels rather than pixel values.
    if re.fullmatch(r"[1-7]", value):
        bbcode_scale = {
            "1": 0.625,
            "2": 0.8125,
            "3": 1.0,
            "4": 1.125,
            "5": 1.5,
            "6": 2.0,
            "7": 2.25,
        }
        return f'{_format_css_number(bbcode_scale[value])}em'

    percent_match = re.search(r"(\d{1,3}(?:\.\d+)?)\s*%", value)
    if percent_match:
        size = max(50.0, min(225.0, float(percent_match.group(1))))
        return f'{_format_css_number(size)}%'

    relative_match = re.search(r"(\d{1,3}(?:\.\d+)?)\s*(em|rem)", value)
    if relative_match:
        size = max(0.5, min(2.25, float(relative_match.group(1))))
        unit = relative_match.group(2)
        return f'{_format_css_number(size)}{unit}'

    match = re.search(r"\d{1,3}(?:\.\d+)?", value)
    if not match:
        return None
    # Most message sizes are authored as px-like values; convert them to em so
    # QTextBrowser scaling can resize the content with the document base font.
    px_size = max(8.0, min(36.0, float(match.group(0))))
    return f'{_format_css_number(px_size / 16.0)}em'


def _safe_href(url_raw: str) -> str | None:
    value = html.unescape(url_raw).strip()
    if _should_discard_message_url(value):
        return None
    if re.match(r"^(https?://|mailto:)", value, flags=re.IGNORECASE):
        return html.escape(value, quote=True)
    return None


def _attachment_id(value: str) -> str | None:
    text = html.unescape(str(value or "")).strip()
    match = re.search(r"\d{3,12}", text)
    if not match:
        return None
    return match.group(0)


def _attachment_image_url(value: str) -> str | None:
    attachment_id = _attachment_id(value)
    if attachment_id is None:
        return None
    return f"https://www.beamng.com/attachments/image-png.{attachment_id}/"


def _clean_attachment_text(value: str) -> str:
    attachment_id = _attachment_id(value)
    if attachment_id is None:
        return ""
    return f"[Attachment: {attachment_id}]"


def _render_attachment_html(value: str) -> str:
    attachment_id = _attachment_id(value)
    if attachment_id is None:
        return ""
    href = _safe_href(_attachment_image_url(attachment_id) or "")
    label = f"[Attachment: {attachment_id}]"
    if href is None:
        return f"<span>{label}</span>"
    return f'<a href="{href}" data-linked-image="1">{label}</a>'


def _should_discard_message_url(url_raw: str) -> bool:
    value = html.unescape(str(url_raw or "")).strip()
    if not value:
        return False
    trimmed = _trim_url_suffix(value)[0] if "://" in value else value
    try:
        parsed = urlparse(trimmed)
    except ValueError:
        return False
    host = (parsed.hostname or "").strip().lower()
    return host in _DISCARDED_MESSAGE_HOSTS


def _replace_until_stable(text: str, pattern: str, repl, max_iter: int = 8) -> str:
    out = text
    for _ in range(max_iter):
        next_text, count = re.subn(pattern, repl, out, flags=re.IGNORECASE | re.DOTALL)
        out = next_text
        if count == 0:
            break
    return out


def _normalize_supported_html(raw_message: str) -> str:
    text = raw_message
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)

    def _html_anchor(match: re.Match[str]) -> str:
        raw_target = html.unescape(match.group(1)).strip()
        inner_html = match.group(2)
        label = re.sub(r"<[^>]+>", "", html.unescape(inner_html)).strip()
        if _should_discard_message_url(raw_target):
            return ""
        href = _safe_href(raw_target)
        if href is None:
            return label or raw_target
        shown = label or raw_target
        return f'[URL={html.unescape(href)}]{shown}[/URL]'

    text = re.sub(
        r"<a\b[^>]*\bhref\s*=\s*[\"']([^\"']+)[\"'][^>]*>(.*?)</a>",
        _html_anchor,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return text


def _trim_url_suffix(url_text: str) -> tuple[str, str]:
    trimmed = url_text
    suffix = ""
    while trimmed and trimmed[-1] in ".,;:!?":
        suffix = trimmed[-1] + suffix
        trimmed = trimmed[:-1]
    while trimmed.endswith(")") and trimmed.count("(") < trimmed.count(")"):
        suffix = ")" + suffix
        trimmed = trimmed[:-1]
    return trimmed, suffix


def _auto_link_plain_urls(text: str) -> str:
    if "<a " not in text.lower():
        placeholders: dict[str, str] = {}
        protected = text
    else:
        placeholders = {}

        def _stash_anchor(match: re.Match[str]) -> str:
            token = f"__ANCHOR_PLACEHOLDER_{len(placeholders)}__"
            placeholders[token] = match.group(0)
            return token

        protected = re.sub(
            r"<a\b[^>]*>.*?</a>",
            _stash_anchor,
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )

    def _replace_plain_url(match: re.Match[str]) -> str:
        raw = match.group(1)
        url_text, suffix = _trim_url_suffix(raw)
        if _should_discard_message_url(url_text):
            return ""
        href = _safe_href(url_text)
        if href is None:
            return raw
        return f'<a href="{href}">{url_text}</a>{suffix}'

    linked = re.sub(
        r"(?<![\"'=])((?:https?://|mailto:)[^\s<]+)",
        _replace_plain_url,
        protected,
        flags=re.IGNORECASE,
    )

    for token, anchor in placeholders.items():
        linked = linked.replace(token, anchor)
    return linked


def render_info_message_html(raw_message: str) -> str:
    text = _normalize_supported_html(raw_message.replace("\\/", "/"))
    text = _normalize_newlines(text)
    text = html.escape(text, quote=True)

    text = _replace_until_stable(
        text,
        r"\[ATTACH(?:=[^\]]+)?\](.*?)\[/ATTACH\]",
        lambda m: _render_attachment_html(m.group(1)),
    )
    text = _replace_until_stable(
        text,
        r"\[USER=[^\]]+\](.*?)\[/USER\]",
        lambda m: f"<span>{m.group(1).strip()}</span>",
    )

    def _url_inline(match: re.Match[str]) -> str:
        raw_target = match.group(1).strip()
        if _should_discard_message_url(raw_target):
            return ""
        href = _safe_href(raw_target)
        label = raw_target
        if href is None:
            return label
        return f'<a href="{href}">{label}</a>'

    text = _replace_until_stable(text, r"\[URL\](.*?)\[/URL\]", _url_inline)

    def _url_labeled(match: re.Match[str]) -> str:
        raw_target = match.group(1).strip()
        label = match.group(2).strip()
        if _should_discard_message_url(raw_target):
            return ""
        href = _safe_href(raw_target)
        if href is None:
            return f"{label} ({raw_target})" if label and label != raw_target else raw_target
        shown = label or raw_target
        return f'<a href="{href}">{shown}</a>'

    text = _replace_until_stable(text, r"\[URL=([^\]]+)\](.*?)\[/URL\]", _url_labeled)

    def _img_block(match: re.Match[str]) -> str:
        raw_target = match.group(1).strip()
        if _should_discard_message_url(raw_target):
            return ""
        href = _safe_href(raw_target)
        if href is None:
            return raw_target
        return f'<a href="{href}" data-linked-image="1">{raw_target}</a>'

    text = _replace_until_stable(text, r"\[IMG(?:=[^\]]+)?\](.*?)\[/IMG\]", _img_block)

    text = _replace_until_stable(
        text,
        r"\[SPOILER\](.*?)\[/SPOILER\]",
        lambda m: (
            '<div style="border:0.0625em solid #8f8f8f; border-radius:0.25em; padding:0.375em; margin:0.25em 0;">'
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
        return f'<span style="font-size:{size};">{body}</span>'

    text = _replace_until_stable(text, r"\[SIZE=([^\]]+)\](.*?)\[/SIZE\]", _size_block)

    text = re.sub(r"\[/?[A-Z]+(?:=[^\]]+)?\]", "", text, flags=re.IGNORECASE)
    text = _auto_link_plain_urls(text)
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
    if cached is not _MISS and isinstance(cached, InfoJsonAnalysisResult):
        raw_message = str(cached.message_raw or "")
        rendered_html = str(cached.message_html or "")
        cleaned_message = str(cached.message_clean or "")
        cache_is_stale = False
        if "[IMG" in raw_message.upper() and 'data-linked-image="1"' not in rendered_html:
            cache_is_stale = True
        elif "[ATTACH" in raw_message.upper() and "beamng.com/attachments/image-png." not in rendered_html.lower():
            cache_is_stale = True
        elif "<a " in raw_message.lower() and "&lt;a " in rendered_html.lower():
            cache_is_stale = True
        elif re.search(r"https?://", raw_message, flags=re.IGNORECASE) and "<a href=" not in rendered_html.lower():
            cache_is_stale = True
        elif "pp.userapi.com" in raw_message.lower() and (
            "pp.userapi.com" in rendered_html.lower() or "pp.userapi.com" in cleaned_message.lower()
        ):
            cache_is_stale = True
        if not cache_is_stale:
            return cached
    elif cached is not _MISS:
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
