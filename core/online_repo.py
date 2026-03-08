from __future__ import annotations

import errno
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


USER_AGENT = "BeamNG-Manager/1.0 (+https://www.beamng.com/)"
DEFAULT_TTL_HOURS = 12
DEFAULT_CACHE_MAX_MB = 512

_BEAMNG_URI_RE = re.compile(r"^beamng:v1/([A-Za-z0-9_]+)/([A-Za-z0-9]+)$")


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
        self.cache_ttl_hours = max(1, int(ttl_hours))
        self.cache_max_mb = max(64, int(cache_max_mb))
        self._debug_enabled = False
        self._debug_logger: Callable[[str], None] | None = None
        self._request_error_handler: Callable[[str], bool] | None = None
        self._cancel_requested = False

    def update_cache_policy(self, ttl_hours: int, cache_max_mb: int) -> None:
        self.cache_ttl_hours = max(1, int(ttl_hours))
        self.cache_max_mb = max(64, int(cache_max_mb))

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

    def direct_download(
        self,
        download_url: str,
        destination_dir: Path,
        overwrite: bool = False,
    ) -> DownloadResult:
        destination_dir.mkdir(parents=True, exist_ok=True)
        return self.download_url_to_path(download_url, destination_dir, overwrite=overwrite)

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
                tmp_path = destination_dir / tmp_name
                try:
                    with tmp_path.open("wb") as fh:
                        shutil.copyfileobj(response, fh)

                    try:
                        tmp_path.replace(target)
                    except OSError as exc:
                        if getattr(exc, "errno", None) != errno.EXDEV:
                            raise
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
