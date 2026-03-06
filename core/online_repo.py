from __future__ import annotations

import errno
import hashlib
import html
import json
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse
from urllib.error import HTTPError
from urllib.request import Request, urlopen


USER_AGENT = "BeamNG-Manager/1.0 (+https://www.beamng.com/)"
DEFAULT_TTL_HOURS = 12
DEFAULT_CACHE_MAX_MB = 512

BEAMNG_BASE = "https://www.beamng.com/"
BEAMNG_RESOURCE_SEARCH = "https://www.beamng.com/resources/?q={query}"


_BEAMNG_URI_RE = re.compile(r"^beamng:v1/([A-Za-z0-9_]+)/([A-Za-z0-9]+)$")
_DOWNLOAD_LINK_RE = re.compile(r'href="([^"]*resources/[^"]*/download[^"]*)"')
_RESOURCE_LINK_RE = re.compile(r'href="(resources/[^"]+\.\d+/)"')
_RESOURCE_TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_LOCAL_REPO_PREFIX = "repo_file:"


def parse_beamng_protocol_uri(uri: str) -> tuple[str, str] | None:
    value = uri.strip()
    match = _BEAMNG_URI_RE.match(value)
    if not match:
        return None
    return match.group(1), match.group(2)


def is_beamng_resource_download_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.netloc.lower() not in {"www.beamng.com", "beamng.com"}:
        return False
    if "/resources/" not in parsed.path:
        return False
    if not parsed.path.rstrip("/").endswith("/download"):
        return False
    return True


def _safe_filename(name: str, fallback: str = "download.zip") -> str:
    value = (name or "").strip().replace("\\", "_").replace("/", "_")
    value = re.sub(r"[^A-Za-z0-9._ -]+", "_", value)
    if not value:
        return fallback
    return value


def _strip_html(text: str) -> str:
    value = _TAG_RE.sub(" ", text)
    value = html.unescape(value)
    return re.sub(r"\s+", " ", value).strip()


def _extract_download_version(download_url: str) -> str | None:
    parsed = urlparse(download_url)
    query = parse_qs(parsed.query)
    values = query.get("version")
    if not values:
        return None
    return values[0]


def _extract_filename_from_content_disposition(header_value: str | None) -> str | None:
    if not header_value:
        return None
    match = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', header_value, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip()


@dataclass(slots=True)
class DownloadResult:
    ok: bool
    message: str
    file_path: Path | None = None
    file_name: str | None = None
    source_url: str | None = None


class OnlineRepoClient:
    def __init__(
        self,
        beam_mods_root: str | Path,
        library_root: str | Path,
        cache_root: Path | None = None,
        ttl_hours: int = DEFAULT_TTL_HOURS,
        cache_max_mb: int = DEFAULT_CACHE_MAX_MB,
    ) -> None:
        self.beam_mods_root = Path(beam_mods_root)
        self.library_root = Path(library_root)
        self.cache_root = cache_root or (Path(__file__).resolve().parents[1] / ".cache" / "online")
        self.pages_cache_dir = self.cache_root / "pages"
        self.download_cache_dir = self.cache_root / "downloads"
        self.tmp_dir = self.cache_root / "tmp"
        self.metadata_index_file = self.cache_root / "metadata_index.json"
        self.cache_ttl_hours = max(1, int(ttl_hours))
        self.cache_max_mb = max(64, int(cache_max_mb))
        self._metadata_index: dict[str, dict[str, Any]] = {}
        self._debug_enabled = False
        self._debug_logger: Callable[[str], None] | None = None
        self._request_error_handler: Callable[[str], bool] | None = None
        self._cancel_requested = False
        self._ensure_cache_dirs()
        self._load_metadata_index()

    def update_cache_policy(self, ttl_hours: int, cache_max_mb: int) -> None:
        self.cache_ttl_hours = max(1, int(ttl_hours))
        self.cache_max_mb = max(64, int(cache_max_mb))
        self._enforce_cache_limit()

    def set_debug_logging(self, enabled: bool, logger: Callable[[str], None] | None = None) -> None:
        self._debug_enabled = bool(enabled)
        self._debug_logger = logger if self._debug_enabled else None

    def set_request_error_handler(self, handler: Callable[[str], bool] | None = None) -> None:
        self._request_error_handler = handler

    def clear_cancel_request(self) -> None:
        self._cancel_requested = False

    def request_cancel(self) -> None:
        self._cancel_requested = True

    def cancel_requested(self) -> bool:
        return bool(self._cancel_requested)

    def _should_continue_after_request_error(self, message: str) -> bool:
        if self._request_error_handler is None:
            return True
        try:
            decision = bool(self._request_error_handler(message))
        except Exception:
            decision = True
        if not decision:
            self._cancel_requested = True
        return decision

    def _log_debug(self, message: str) -> None:
        if not self._debug_enabled or self._debug_logger is None:
            return
        try:
            self._debug_logger(str(message))
        except Exception:
            pass

    @property
    def subscriptions_file(self) -> Path:
        return self.beam_mods_root / "mod_manifests" / "beamng_manager_subscriptions.json"

    @property
    def beam_db_file(self) -> Path:
        return self.beam_mods_root / "db.json"

    def load_subscriptions(self) -> dict[str, dict[str, Any]]:
        path = self.subscriptions_file
        if not path.exists():
            return {}
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(parsed, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for mod_id, value in parsed.items():
            if isinstance(mod_id, str) and isinstance(value, dict):
                out[mod_id] = value
        return out

    def save_subscriptions(self, data: dict[str, dict[str, Any]]) -> None:
        path = self.subscriptions_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")

    def list_subscriptions(self) -> list[tuple[str, dict[str, Any]]]:
        data = self._merged_subscriptions()
        return sorted(data.items(), key=lambda kv: str(kv[1].get("display_id") or kv[0]).lower())

    def unsubscribe(self, mod_id: str, remove_file: bool = False) -> tuple[bool, str]:
        all_entries = self._merged_subscriptions()
        entry = all_entries.get(mod_id)
        if entry is None:
            return False, f"Subscription not found: {mod_id}"

        data = self.load_subscriptions()
        manifest_entry = data.pop(mod_id, None)
        self.save_subscriptions(data)

        removed_db_paths: list[Path] = []
        if str(mod_id).lower().startswith(_LOCAL_REPO_PREFIX):
            removed_db_paths = self._remove_db_rows_for_local_repo_entry(entry)
        else:
            removed_db_paths = self._remove_db_subscription_rows(mod_id)

        if remove_file:
            remove_candidates: set[Path] = set()
            for candidate in (entry, manifest_entry):
                if not isinstance(candidate, dict):
                    continue
                file_path_value = str(candidate.get("installed_path") or "").strip()
                if file_path_value:
                    remove_candidates.add(Path(file_path_value))
            remove_candidates.update(removed_db_paths)
            for file_path in sorted(remove_candidates, key=lambda p: str(p).lower()):
                if not file_path.exists():
                    continue
                try:
                    file_path.unlink()
                except OSError as exc:
                    return False, f"Unsubscribed but failed to remove file: {exc}"
        return True, f"Unsubscribed: {mod_id}"

    def resolve_subscription(self, mod_id: str, source_page_url: str) -> dict[str, Any]:
        source_url = source_page_url.strip()
        if source_url and self._looks_like_resource_page(source_url):
            parsed = self._parse_resource_page(source_url)
            if parsed.get("download_url") and (parsed.get("mod_id") == mod_id or not parsed.get("mod_id")):
                parsed["mod_id"] = mod_id
                return parsed

        if source_url:
            html_text = self.fetch_text(source_url)
            parsed_from_source = self._parse_resource_html(html_text, source_url)
            if parsed_from_source.get("download_url") and parsed_from_source.get("mod_id") == mod_id:
                return parsed_from_source

        search_url = BEAMNG_RESOURCE_SEARCH.format(query=quote_plus(mod_id))
        search_html = self.fetch_text(search_url)
        match = _RESOURCE_LINK_RE.search(search_html)
        if not match:
            return {}
        resource_url = urljoin(BEAMNG_BASE, match.group(1))
        parsed = self._parse_resource_page(resource_url)
        if parsed:
            parsed["mod_id"] = parsed.get("mod_id") or mod_id
        return parsed

    def subscribe_from_protocol(self, mod_id: str, source_page_url: str, overwrite: bool = True) -> DownloadResult:
        metadata = self.resolve_subscription(mod_id, source_page_url)
        download_url = str(metadata.get("download_url") or "")
        if not download_url:
            return DownloadResult(False, f"Could not resolve download URL for subscription: {mod_id}")

        repo_dir = self.beam_mods_root / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)
        result = self.download_url_to_path(download_url, repo_dir, overwrite=overwrite)
        if not result.ok or result.file_path is None:
            return result

        subs = self.load_subscriptions()
        subs[mod_id] = {
            "provider": "beamng",
            "mod_id": mod_id,
            "destination_kind": "repo",
            "resource_url": str(metadata.get("resource_url") or source_page_url),
            "resource_title": str(metadata.get("title") or mod_id),
            "download_url": download_url,
            "version_id": str(metadata.get("version_id") or ""),
            "installed_path": str(result.file_path),
            "installed_name": result.file_name or result.file_path.name,
            "subscribed_at": int(time.time()),
            "last_checked_at": int(time.time()),
            "last_updated_at": int(time.time()),
        }
        self.save_subscriptions(subs)
        return DownloadResult(
            True,
            f"Subscribed to {mod_id} in Repo.",
            file_path=result.file_path,
            file_name=result.file_name,
            source_url=download_url,
        )

    def direct_download(
        self,
        download_url: str,
        destination_dir: Path,
        overwrite: bool = False,
    ) -> DownloadResult:
        destination_dir.mkdir(parents=True, exist_ok=True)
        return self.download_url_to_path(download_url, destination_dir, overwrite=overwrite)

    def check_updates(self) -> list[dict[str, Any]]:
        self.clear_cancel_request()
        out: list[dict[str, Any]] = []
        subs = self._merged_subscriptions()
        for mod_id, entry in sorted(subs.items(), key=lambda kv: kv[0].lower()):
            if self._cancel_requested:
                out.append(
                    {
                        "mod_id": "",
                        "ok": False,
                        "message": "Cancelled by user.",
                        "update_available": False,
                        "cancelled": True,
                    }
                )
                break
            if str(entry.get("provider") or "beamng").strip().lower() != "beamng":
                continue
            resource_url = str(entry.get("resource_url") or "").strip()
            try:
                meta = self._parse_resource_page(resource_url) if resource_url else {}
                if not str(meta.get("download_url") or "").strip():
                    meta = self.resolve_subscription(mod_id, resource_url)
            except OSError as exc:
                cancelled = bool(self._cancel_requested or "Cancelled by user." in str(exc))
                out.append(
                    {
                        "mod_id": mod_id,
                        "ok": False,
                        "message": "Cancelled by user." if cancelled else f"Failed to fetch metadata: {exc}",
                        "update_available": False,
                        **({"cancelled": True} if cancelled else {}),
                    }
                )
                if cancelled:
                    break
                continue

            download_url = str(meta.get("download_url") or "").strip()
            if not download_url:
                out.append(
                    {
                        "mod_id": mod_id,
                        "ok": False,
                        "message": "Missing download URL for subscription.",
                        "update_available": False,
                    }
                )
                continue

            current_version = str(entry.get("version_id") or "")
            latest_version = str(meta.get("version_id") or "")
            out.append(
                {
                    "mod_id": mod_id,
                    "ok": True,
                    "resource_url": str(meta.get("resource_url") or resource_url),
                    "download_url": download_url,
                    "current_version": current_version,
                    "latest_version": latest_version,
                    "update_available": bool(latest_version and latest_version != current_version),
                    "title": str(meta.get("title") or entry.get("resource_title") or mod_id),
                }
            )
        return out

    def update_subscriptions(self, mod_ids: list[str] | tuple[str, ...] | set[str] | None = None, overwrite: bool = True) -> tuple[int, int, list[str]]:
        updates = self.check_updates()
        subs = self.load_subscriptions()
        updated = 0
        failed = 0
        messages: list[str] = []
        selected_ids: set[str] | None = None
        seen_selected: set[str] = set()
        issued_download_request = False

        if mod_ids is not None:
            selected_ids = {str(mod_id).strip() for mod_id in mod_ids if str(mod_id).strip()}
            if not selected_ids:
                return 0, 0, []

        repo_dir = self.beam_mods_root / "repo"
        repo_dir.mkdir(parents=True, exist_ok=True)

        for item in updates:
            if self._cancel_requested:
                messages.append("Cancelled by user.")
                break
            mod_id = str(item.get("mod_id") or "").strip()
            if not mod_id:
                continue
            if selected_ids is not None and mod_id not in selected_ids:
                continue
            if selected_ids is not None:
                seen_selected.add(mod_id)
            if not item.get("ok"):
                failed += 1
                messages.append(f"{mod_id}: {item.get('message')}")
                continue
            if not item.get("update_available"):
                continue
            if self._cancel_requested:
                messages.append("Cancelled by user.")
                break
            if issued_download_request:
                time.sleep(1.0)
            result = self.download_url_to_path(str(item.get("download_url") or ""), repo_dir, overwrite=overwrite)
            issued_download_request = True
            if not result.ok or result.file_path is None:
                failed += 1
                result_message = str(result.message)
                messages.append(f"{mod_id}: {result_message}")
                if self._cancel_requested or "Cancelled by user." in result_message:
                    break
                continue

            entry = subs.get(mod_id, {})
            entry["version_id"] = str(item.get("latest_version") or entry.get("version_id") or "")
            entry["download_url"] = str(item.get("download_url") or entry.get("download_url") or "")
            entry["resource_url"] = str(item.get("resource_url") or entry.get("resource_url") or "")
            entry["resource_title"] = str(item.get("title") or entry.get("resource_title") or mod_id)
            entry["installed_path"] = str(result.file_path)
            entry["installed_name"] = result.file_name or result.file_path.name
            entry["last_checked_at"] = int(time.time())
            entry["last_updated_at"] = int(time.time())
            entry["destination_kind"] = "repo"
            entry["provider"] = "beamng"
            entry["mod_id"] = mod_id
            subs[mod_id] = entry
            updated += 1

        if selected_ids is not None:
            for missing_id in sorted(selected_ids - seen_selected, key=str.lower):
                failed += 1
                messages.append(f"{missing_id}: Subscription not found.")

        self.save_subscriptions(subs)
        return updated, failed, messages

    def update_all_subscriptions(self, overwrite: bool = True) -> tuple[int, int, list[str]]:
        return self.update_subscriptions(mod_ids=None, overwrite=overwrite)

    def _load_beam_db(self) -> dict[str, Any]:
        path = self.beam_db_file
        if not path.exists():
            return {"header": {"version": 1.1}, "mods": {}}
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"header": {"version": 1.1}, "mods": {}}
        if not isinstance(parsed, dict):
            return {"header": {"version": 1.1}, "mods": {}}
        mods = parsed.get("mods")
        if not isinstance(mods, dict):
            parsed["mods"] = {}
        return parsed

    def _save_beam_db(self, payload: dict[str, Any]) -> None:
        self.beam_db_file.parent.mkdir(parents=True, exist_ok=True)
        self.beam_db_file.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def _db_fullpath_to_fs(self, fullpath: str) -> Path | None:
        value = (fullpath or "").strip().replace("\\", "/")
        if not value.startswith("/mods/"):
            return None
        rel = value[len("/mods/") :].strip("/")
        if not rel:
            return None
        return self.beam_mods_root / Path(rel)

    def _db_entry_repo_id(self, value: dict[str, Any]) -> str:
        direct = str(value.get("modID") or value.get("tagid") or "").strip()
        if direct:
            return direct
        mod_data = value.get("modData")
        if isinstance(mod_data, dict):
            nested = str(mod_data.get("tagid") or mod_data.get("modID") or "").strip()
            if nested:
                return nested
        return ""

    def _db_entry_resource_meta(self, value: dict[str, Any]) -> tuple[str, str]:
        mod_data = value.get("modData")
        if not isinstance(mod_data, dict):
            return "", ""
        resource_id = str(mod_data.get("resource_id") or "").strip()
        version_id = str(mod_data.get("current_version_id") or mod_data.get("resource_version_id") or "").strip()
        return resource_id, version_id

    def _subscriptions_from_db(self) -> dict[str, dict[str, Any]]:
        payload = self._load_beam_db()
        mods_payload = payload.get("mods", {})
        if not isinstance(mods_payload, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for value in mods_payload.values():
            if not isinstance(value, dict):
                continue
            mod_id = self._db_entry_repo_id(value)
            if not mod_id:
                continue
            fullpath = str(value.get("fullpath") or "")
            dirname = str(value.get("dirname") or "")
            if not (fullpath.startswith("/mods/repo/") or dirname.startswith("/mods/repo/")):
                continue
            installed_fs = self._db_fullpath_to_fs(fullpath)
            installed_name = str(value.get("filename") or (installed_fs.name if installed_fs else "")).strip()
            resource_id, version_id = self._db_entry_resource_meta(value)
            resource_url = f"https://www.beamng.com/resources/{resource_id}/" if resource_id else ""
            download_url = (
                f"https://www.beamng.com/resources/{resource_id}/download?version={version_id}"
                if resource_id and version_id
                else ""
            )
            out.setdefault(
                mod_id,
                {
                    "provider": "beamng",
                    "mod_id": mod_id,
                    "destination_kind": "repo",
                    "resource_url": resource_url,
                    "resource_title": str(value.get("modname") or mod_id),
                    "download_url": download_url,
                    "version_id": version_id,
                    "installed_path": str(installed_fs) if installed_fs else "",
                    "installed_name": installed_name,
                },
            )
        return out

    def _subscriptions_from_db_repo_without_modid(self) -> dict[str, dict[str, Any]]:
        payload = self._load_beam_db()
        mods_payload = payload.get("mods", {})
        if not isinstance(mods_payload, dict):
            return {}
        out: dict[str, dict[str, Any]] = {}
        for db_key, value in mods_payload.items():
            if not isinstance(value, dict):
                continue
            fullpath = str(value.get("fullpath") or "")
            dirname = str(value.get("dirname") or "")
            if not (fullpath.startswith("/mods/repo/") or dirname.startswith("/mods/repo/")):
                continue
            mod_id = self._db_entry_repo_id(value)
            if mod_id:
                continue
            installed_fs = self._db_fullpath_to_fs(fullpath)
            installed_name = str(value.get("filename") or (installed_fs.name if installed_fs else "")).strip()
            if not installed_name:
                continue
            entry_id = f"{_LOCAL_REPO_PREFIX}{installed_name.lower()}"
            display = str(value.get("modname") or Path(installed_name).stem or installed_name)
            out.setdefault(
                entry_id,
                {
                    "provider": "local_repo",
                    "mod_id": entry_id,
                    "display_id": display,
                    "destination_kind": "repo",
                    "resource_url": "",
                    "resource_title": display,
                    "download_url": "",
                    "version_id": "",
                    "installed_path": str(installed_fs) if installed_fs else "",
                    "installed_name": installed_name,
                    "db_key": str(db_key),
                },
            )
        return out

    def _subscriptions_from_repo_files(self) -> dict[str, dict[str, Any]]:
        repo_dir = self.beam_mods_root / "repo"
        if not repo_dir.exists() or not repo_dir.is_dir():
            return {}
        out: dict[str, dict[str, Any]] = {}
        for path in sorted(repo_dir.rglob("*.zip"), key=lambda p: str(p).lower()):
            try:
                rel = path.relative_to(repo_dir).as_posix()
            except ValueError:
                rel = path.name
            entry_id = f"{_LOCAL_REPO_PREFIX}{rel.lower()}"
            display = Path(path.name).stem
            out[entry_id] = {
                "provider": "local_repo",
                "mod_id": entry_id,
                "display_id": display,
                "destination_kind": "repo",
                "resource_url": "",
                "resource_title": display,
                "download_url": "",
                "version_id": "",
                "installed_path": str(path),
                "installed_name": path.name,
            }
        return out

    def _merged_subscriptions(self) -> dict[str, dict[str, Any]]:
        merged = self.load_subscriptions()
        for mod_id, db_entry in self._subscriptions_from_db().items():
            if mod_id not in merged:
                merged[mod_id] = db_entry
                continue
            current = merged[mod_id]
            if not str(current.get("installed_path") or "").strip() and str(db_entry.get("installed_path") or "").strip():
                current["installed_path"] = db_entry["installed_path"]
            if not str(current.get("installed_name") or "").strip() and str(db_entry.get("installed_name") or "").strip():
                current["installed_name"] = db_entry["installed_name"]
            if not str(current.get("destination_kind") or "").strip():
                current["destination_kind"] = "repo"
            merged[mod_id] = current
        local_from_db = self._subscriptions_from_db_repo_without_modid()
        for local_id, local_entry in local_from_db.items():
            if local_id not in merged:
                merged[local_id] = local_entry
        local_from_files = self._subscriptions_from_repo_files()
        existing_paths = {
            str(v.get("installed_path") or "").strip().lower()
            for v in merged.values()
            if isinstance(v, dict) and str(v.get("installed_path") or "").strip()
        }
        for local_id, local_entry in local_from_files.items():
            local_path = str(local_entry.get("installed_path") or "").strip().lower()
            if local_id in merged or (local_path and local_path in existing_paths):
                continue
            merged[local_id] = local_entry
        return merged

    def _remove_db_subscription_rows(self, mod_id: str) -> list[Path]:
        payload = self._load_beam_db()
        mods_payload = payload.get("mods", {})
        if not isinstance(mods_payload, dict):
            return []
        removed_paths: list[Path] = []
        next_mods: dict[str, Any] = {}
        changed = False
        for mod_key, value in mods_payload.items():
            if not isinstance(value, dict):
                next_mods[str(mod_key)] = value
                continue
            row_mod_id = self._db_entry_repo_id(value)
            row_fullpath = str(value.get("fullpath") or "")
            row_dirname = str(value.get("dirname") or "")
            is_repo_row = row_fullpath.startswith("/mods/repo/") or row_dirname.startswith("/mods/repo/")
            if is_repo_row and row_mod_id == mod_id:
                changed = True
                fs_path = self._db_fullpath_to_fs(row_fullpath)
                if fs_path is not None:
                    removed_paths.append(fs_path)
                continue
            next_mods[str(mod_key)] = value
        if changed:
            payload["mods"] = next_mods
            payload["header"] = {"version": 1.1}
            self._save_beam_db(payload)
        return removed_paths

    def _remove_db_rows_for_local_repo_entry(self, entry: dict[str, Any]) -> list[Path]:
        payload = self._load_beam_db()
        mods_payload = payload.get("mods", {})
        if not isinstance(mods_payload, dict):
            return []
        installed_name = str(entry.get("installed_name") or "").strip().lower()
        installed_path_raw = str(entry.get("installed_path") or "").strip()
        installed_path_norm = str(Path(installed_path_raw)).lower() if installed_path_raw else ""

        removed_paths: list[Path] = []
        next_mods: dict[str, Any] = {}
        changed = False
        for mod_key, value in mods_payload.items():
            if not isinstance(value, dict):
                next_mods[str(mod_key)] = value
                continue
            row_fullpath = str(value.get("fullpath") or "")
            row_dirname = str(value.get("dirname") or "")
            is_repo_row = row_fullpath.startswith("/mods/repo/") or row_dirname.startswith("/mods/repo/")
            if not is_repo_row:
                next_mods[str(mod_key)] = value
                continue
            row_filename = str(value.get("filename") or "").strip().lower()
            row_fs = self._db_fullpath_to_fs(row_fullpath)
            row_fs_norm = str(row_fs).lower() if row_fs is not None else ""
            matches = False
            if installed_path_norm and row_fs_norm and row_fs_norm == installed_path_norm:
                matches = True
            elif installed_name and row_filename and row_filename == installed_name:
                matches = True
            if matches:
                changed = True
                if row_fs is not None:
                    removed_paths.append(row_fs)
                continue
            next_mods[str(mod_key)] = value
        if changed:
            payload["mods"] = next_mods
            payload["header"] = {"version": 1.1}
            self._save_beam_db(payload)
        return removed_paths

    def fetch_text(self, url: str) -> str:
        self._ensure_cache_dirs()
        self._log_debug(f"HTTP GET page start: {url}")
        key = hashlib.sha1(url.encode("utf-8")).hexdigest()
        now = int(time.time())
        ttl_seconds = self.cache_ttl_hours * 3600
        entry = self._metadata_index.get(key, {})
        cache_file = self.pages_cache_dir / f"{key}.html"

        headers = {"User-Agent": USER_AGENT}
        if entry.get("etag"):
            headers["If-None-Match"] = str(entry["etag"])
        if entry.get("last_modified"):
            headers["If-Modified-Since"] = str(entry["last_modified"])

        still_fresh = cache_file.is_file() and int(entry.get("expires_at", 0)) > now
        if still_fresh:
            try:
                self._log_debug(f"HTTP GET page cache-hit: {url}")
                return cache_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass

        request = Request(url, headers=headers)
        try:
            with urlopen(request, timeout=30) as response:
                status = getattr(response, "status", None) or response.getcode()
                final_url = response.geturl() or url
                content = response.read().decode("utf-8", errors="replace")
                etag = response.headers.get("ETag")
                last_modified = response.headers.get("Last-Modified")
                cache_file.write_text(content, encoding="utf-8")
                self._metadata_index[key] = {
                    "url": url,
                    "etag": etag,
                    "last_modified": last_modified,
                    "updated_at": now,
                    "expires_at": now + ttl_seconds,
                }
                self._save_metadata_index()
                self._enforce_cache_limit()
                self._log_debug(f"HTTP GET page ok: {url} status={status} final={final_url}")
                return content
        except HTTPError as exc:
            self._log_debug(f"HTTP GET page error: {url} status={exc.code} reason={exc.reason}")
            if cache_file.is_file():
                self._log_debug(f"HTTP GET page fallback-cache: {url}")
                return cache_file.read_text(encoding="utf-8", errors="replace")
            if not self._should_continue_after_request_error(f"HTTP {exc.code} while requesting {url}: {exc.reason}"):
                raise OSError("Cancelled by user.")
            raise
        except OSError as exc:
            self._log_debug(f"HTTP GET page error: {url}")
            if cache_file.is_file():
                self._log_debug(f"HTTP GET page fallback-cache: {url}")
                return cache_file.read_text(encoding="utf-8", errors="replace")
            if not self._should_continue_after_request_error(f"Request failed for {url}: {exc}"):
                raise OSError("Cancelled by user.")
            raise

    def download_url_to_path(self, download_url: str, destination_dir: Path, overwrite: bool = False) -> DownloadResult:
        if self._cancel_requested:
            return DownloadResult(False, "Cancelled by user.")
        if not download_url:
            return DownloadResult(False, "Empty download URL.")
        destination_dir.mkdir(parents=True, exist_ok=True)
        self._log_debug(f"HTTP GET download start: {download_url} -> {destination_dir}")

        req = Request(download_url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(req, timeout=60) as response:
                status = getattr(response, "status", None) or response.getcode()
                final_url = response.geturl() or download_url
                file_name = _extract_filename_from_content_disposition(response.headers.get("Content-Disposition"))
                if not file_name:
                    parsed = urlparse(final_url)
                    file_name = Path(parsed.path).name or "download.zip"
                file_name = _safe_filename(file_name, fallback="download.zip")
                target = destination_dir / file_name
                if target.exists() and not overwrite:
                    self._log_debug(f"HTTP GET download exists: {target}")
                    return DownloadResult(False, f"Destination already exists: {target.name}", file_path=target, file_name=file_name)

                tmp_name = f"tmp_{int(time.time())}_{os.getpid()}_{file_name}"
                # Stage in destination directory to keep final replace atomic and same-volume.
                tmp_path = destination_dir / tmp_name
                try:
                    with tmp_path.open("wb") as fh:
                        shutil.copyfileobj(response, fh)

                    try:
                        tmp_path.replace(target)
                    except OSError as exc:
                        if getattr(exc, "errno", None) != errno.EXDEV:
                            raise
                        # Cross-volume fallback: copy then remove staged file.
                        if target.exists():
                            target.unlink()
                        shutil.copy2(tmp_path, target)
                        if tmp_path.exists():
                            tmp_path.unlink()
                finally:
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except OSError:
                            pass
                self._log_debug(f"HTTP GET download ok: {download_url} status={status} final={final_url} file={target.name}")
                return DownloadResult(True, f"Downloaded: {target.name}", file_path=target, file_name=file_name, source_url=final_url)
        except HTTPError as exc:
            self._log_debug(f"HTTP GET download error: {download_url} status={exc.code} reason={exc.reason}")
            if not self._should_continue_after_request_error(f"HTTP {exc.code} while downloading {download_url}: {exc.reason}"):
                return DownloadResult(False, "Cancelled by user.")
            return DownloadResult(False, f"Download failed (HTTP {exc.code}): {exc.reason}")
        except OSError as exc:
            self._log_debug(f"HTTP GET download error: {download_url} error={exc}")
            if not self._should_continue_after_request_error(f"Download request failed for {download_url}: {exc}"):
                return DownloadResult(False, "Cancelled by user.")
            return DownloadResult(False, f"Download failed: {exc}")

    def _parse_resource_page(self, resource_url: str) -> dict[str, Any]:
        html_text = self.fetch_text(resource_url)
        return self._parse_resource_html(html_text, resource_url)

    def _parse_resource_html(self, html_text: str, page_url: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "resource_url": page_url,
            "title": "",
            "download_url": "",
            "version_id": "",
            "mod_id": "",
        }

        proto = re.search(r'beamng:v1/subscriptionMod/([A-Za-z0-9]+)', html_text, re.IGNORECASE)
        if proto:
            out["mod_id"] = proto.group(1)

        download_match = _DOWNLOAD_LINK_RE.search(html_text)
        if download_match:
            dl = html.unescape(download_match.group(1))
            dl_norm = dl.strip()
            if dl_norm.startswith("resources/"):
                out["download_url"] = urljoin(BEAMNG_BASE, dl_norm)
            elif dl_norm.startswith("/resources/"):
                out["download_url"] = urljoin(BEAMNG_BASE, dl_norm.lstrip("/"))
            else:
                out["download_url"] = urljoin(page_url, dl_norm)
            version_id = _extract_download_version(out["download_url"])
            if version_id:
                out["version_id"] = version_id

        title_match = _RESOURCE_TITLE_RE.search(html_text)
        if title_match:
            out["title"] = _strip_html(title_match.group(1))
        return out

    def _looks_like_resource_page(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.netloc.lower() in {"www.beamng.com", "beamng.com"} and "/resources/" in parsed.path

    def _ensure_cache_dirs(self) -> None:
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self.pages_cache_dir.mkdir(parents=True, exist_ok=True)
        self.download_cache_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def _load_metadata_index(self) -> None:
        if not self.metadata_index_file.is_file():
            self._metadata_index = {}
            return
        try:
            parsed = json.loads(self.metadata_index_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._metadata_index = {}
            return
        if not isinstance(parsed, dict):
            self._metadata_index = {}
            return
        entries = parsed.get("entries", {})
        if isinstance(entries, dict):
            self._metadata_index = {str(k): v for k, v in entries.items() if isinstance(v, dict)}
        else:
            self._metadata_index = {}

    def _save_metadata_index(self) -> None:
        payload = {"entries": self._metadata_index}
        self.metadata_index_file.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")

    def _enforce_cache_limit(self) -> None:
        max_bytes = self.cache_max_mb * 1024 * 1024
        files: list[Path] = []
        for folder in (self.pages_cache_dir, self.download_cache_dir, self.tmp_dir):
            if not folder.exists():
                continue
            files.extend([p for p in folder.rglob("*") if p.is_file()])
        total = 0
        for path in files:
            try:
                total += path.stat().st_size
            except OSError:
                continue
        if total <= max_bytes:
            return

        sortable: list[tuple[float, Path]] = []
        for path in files:
            try:
                sortable.append((path.stat().st_mtime, path))
            except OSError:
                continue
        sortable.sort(key=lambda item: item[0])

        for _mtime, path in sortable:
            if total <= max_bytes:
                break
            try:
                size = path.stat().st_size
            except OSError:
                continue
            try:
                path.unlink()
                total -= size
            except OSError:
                continue
