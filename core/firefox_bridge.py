from __future__ import annotations

import json
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlparse

from core.utils import resource_root_dir


MarkersProvider = Callable[[], tuple[set[str], set[str], set[str], set[str]]]
DebugLogger = Callable[[str], None]
_BRIDGE_PROTOCOL_VERSION = 2


def _expected_extension_version_from_manifests() -> str:
    root = resource_root_dir()
    manifest_paths = (
        root / "integrations" / "manifest.base.json",
        root / "integrations" / "firefox-beamng-manager" / "manifest.json",
    )
    for manifest_path in manifest_paths:
        version = _read_extension_manifest_version(manifest_path)
        if version:
            return version
    return "unknown"


def _read_extension_manifest_version(manifest_path: Path) -> str:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return str(payload.get("version") or "").strip()


class FirefoxBridgeServer:
    def __init__(
        self,
        markers_provider: MarkersProvider,
        host: str = "127.0.0.1",
        port: int = 49441,
        debug_enabled: bool = False,
        debug_logger: DebugLogger | None = None,
        expected_extension_version: str | None = None,
    ) -> None:
        self._markers_provider = markers_provider
        self._host = host
        self._port = int(port)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._state_lock = threading.Lock()
        self._next_command_id = 1
        self._pending_commands: list[dict[str, object]] = []
        self._consumed_commands: list[dict[str, object]] = []
        self._markers_snapshot: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]] | None = None
        self._markers_revision = 0
        self._commands_revision = 0
        self._session_id = uuid.uuid4().hex
        self._debug_enabled = bool(debug_enabled)
        self._debug_logger = debug_logger
        self._expected_extension_version = (
            str(expected_extension_version or "").strip() or _expected_extension_version_from_manifests()
        )
        self._current_extension_version = ""

    def set_debug_enabled(self, enabled: bool) -> None:
        self._debug_enabled = bool(enabled)
        self._debug(f"Debug mode {'enabled' if self._debug_enabled else 'disabled'}.")

    def _debug(self, message: str) -> None:
        if not self._debug_enabled:
            return
        stamp = time.strftime("%H:%M:%S")
        text = f"[BridgeDebug {stamp}] {str(message or '').strip()}"
        logger = self._debug_logger
        if logger is not None:
            try:
                logger(text)
                return
            except Exception:
                pass
        print(text, flush=True)

    @staticmethod
    def _short_session_id(value: str) -> str:
        token = str(value or "").strip()
        if not token:
            return "-"
        return token[:8]

    @staticmethod
    def _short_url(value: str) -> str:
        text = str(value or "").strip()
        if len(text) <= 120:
            return text
        return f"{text[:117]}..."

    def extension_version_state(self) -> tuple[str, str]:
        with self._state_lock:
            return str(self._expected_extension_version), str(self._current_extension_version)

    def _record_extension_version(self, extension_version: str) -> None:
        value = str(extension_version or "").strip()
        if not value:
            return
        with self._state_lock:
            if value == self._current_extension_version:
                return
            self._current_extension_version = value

    def start(self) -> tuple[bool, str]:
        if self._server is not None:
            self._debug(f"start() ignored: already running on {self._host}:{self._port}")
            return True, f"Firefox bridge already running on http://{self._host}:{self._port}"

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = str(parsed.path or "")
                query = parse_qs(parsed.query, keep_blank_values=False)
                owner._record_extension_version(_qs_first(query, "extension_version"))
                owner._debug(f"GET {path} from {self.client_address[0]} query='{parsed.query}'")
                if path == "/health":
                    self._write_json(200, {"ok": True})
                    owner._debug("GET /health -> ok")
                    return
                if path == "/extension/version":
                    payload = owner._extension_version_payload()
                    self._write_json(200, payload)
                    owner._debug(
                        "GET /extension/version -> "
                        f"expected={payload.get('expected_extension_version')} "
                        f"current={payload.get('current_extension_version')} "
                        f"match={payload.get('version_match')}"
                    )
                    return
                if path == "/session/start":
                    payload = owner._session_start_payload()
                    self._write_json(200, payload)
                    owner._debug(
                        "GET /session/start -> "
                        f"session={owner._short_session_id(str(payload.get('session_id') or ''))} "
                        f"markers_rev={payload.get('markers_rev')} commands_rev={payload.get('commands_rev')}"
                    )
                    return
                if path == "/changes":
                    session_id = _qs_first(query, "session_id")
                    markers_rev = _qs_int(query, "markers_rev", default=-1)
                    commands_rev = _qs_int(query, "commands_rev", default=-1)
                    payload = owner._changes_payload(session_id, markers_rev, commands_rev)
                    self._write_json(200, payload)
                    owner._debug(
                        "GET /changes -> "
                        f"session={owner._short_session_id(session_id)} "
                        f"client_rev(m={markers_rev},c={commands_rev}) "
                        f"changed(session={payload.get('session_changed')}, "
                        f"markers={payload.get('markers_changed')}, commands={payload.get('commands_changed')}) "
                        f"pending={payload.get('commands_pending')}"
                    )
                    return
                if path == "/markers":
                    session_id = _qs_first(query, "session_id")
                    ok, payload = owner._markers_payload(session_id)
                    self._write_json(200 if ok else 409, payload)
                    owner._debug(
                        "GET /markers -> "
                        f"session={owner._short_session_id(session_id)} ok={ok} "
                        f"rev={payload.get('markers_rev')} "
                        f"counts(sub={len(payload.get('subscribed_tokens', []))}, "
                        f"manual={len(payload.get('manual_tokens', []))})"
                    )
                    return
                if path == "/commands/next":
                    session_id = _qs_first(query, "session_id")
                    ok, payload = owner._consume_next_command_payload(session_id)
                    self._write_json(200 if ok else 409, payload)
                    command = payload.get("command")
                    command_id = 0
                    command_url = ""
                    if isinstance(command, dict):
                        command_id = int(command.get("id") or 0)
                        command_url = owner._short_url(str(command.get("url") or ""))
                    owner._debug(
                        "GET /commands/next -> "
                        f"session={owner._short_session_id(session_id)} ok={ok} "
                        f"commands_rev={payload.get('commands_rev')} command_id={command_id} "
                        f"url={command_url}"
                    )
                    return
                # Backward compatibility endpoint used by older add-ons.
                if path == "/installed-markers":
                    payload = owner._legacy_installed_markers_payload()
                    self._write_json(200, payload)
                    legacy_cmd = payload.get("open_url_command")
                    legacy_cmd_id = 0
                    if isinstance(legacy_cmd, dict):
                        legacy_cmd_id = int(legacy_cmd.get("id") or 0)
                    owner._debug(f"GET /installed-markers -> open_url_command_id={legacy_cmd_id}")
                    return
                self._write_json(404, {"ok": False, "error": "Not found"})
                owner._debug(f"GET {path} -> 404")

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.end_headers()

            def _write_json(self, status: int, payload: dict[str, object]) -> None:
                encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(int(status))
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _fmt: str, *_args) -> None:
                return

        try:
            server = ThreadingHTTPServer((self._host, self._port), Handler)
        except OSError as exc:
            self._debug(f"start() failed on {self._host}:{self._port}: {exc}")
            return False, str(exc)

        thread = threading.Thread(target=server.serve_forever, name="FirefoxBridgeServer", daemon=True)
        thread.start()
        self._server = server
        self._thread = thread
        self._debug(
            f"Server started on {self._host}:{self._port} "
            f"session={self._short_session_id(self._session_id)}"
        )
        return True, f"Firefox bridge listening on http://{self._host}:{self._port}"

    def stop(self) -> None:
        server = self._server
        thread = self._thread
        self._server = None
        self._thread = None
        if server is None:
            return
        self._debug(f"Server stopping on {self._host}:{self._port}")
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass
        if thread is not None:
            thread.join(timeout=1.0)
        self._debug("Server stopped.")

    def queue_open_url(self, url: str) -> tuple[bool, str]:
        value = str(url or "").strip()
        if not value:
            return False, "Empty URL."
        if not (value.startswith("https://") or value.startswith("http://")):
            return False, "Only HTTP(S) URLs are supported."
        with self._state_lock:
            command_id = int(self._next_command_id)
            self._next_command_id += 1
            self._pending_commands.append({"id": command_id, "url": value})
            self._commands_revision += 1
        self._debug(
            f"Queued open-url command id={command_id} rev={self._commands_revision} "
            f"url={self._short_url(value)}"
        )
        return True, f"Open-in-browser command queued (id={command_id})."

    def drain_consumed_commands(self) -> list[dict[str, object]]:
        with self._state_lock:
            if not self._consumed_commands:
                return []
            out = [dict(item) for item in self._consumed_commands]
            self._consumed_commands.clear()
            self._debug(f"Drained {len(out)} consumed command event(s).")
            return out

    def _refresh_markers_state_locked(self) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
        subscribed_tokens, manual_tokens, subscribed_tag_ids, manual_tag_ids = self._markers_provider()
        snapshot = (
            tuple(sorted(str(v).strip().lower() for v in subscribed_tokens if str(v).strip())),
            tuple(sorted(str(v).strip().lower() for v in manual_tokens if str(v).strip())),
            tuple(sorted(str(v).strip().lower() for v in subscribed_tag_ids if str(v).strip())),
            tuple(sorted(str(v).strip().lower() for v in manual_tag_ids if str(v).strip())),
        )
        if snapshot != self._markers_snapshot:
            self._markers_snapshot = snapshot
            self._markers_revision += 1
        return snapshot

    def _session_start_payload(self) -> dict[str, object]:
        with self._state_lock:
            self._refresh_markers_state_locked()
            current = str(self._current_extension_version)
            expected = str(self._expected_extension_version)
            return {
                "ok": True,
                "protocol_version": _BRIDGE_PROTOCOL_VERSION,
                "session_id": self._session_id,
                "markers_rev": int(self._markers_revision),
                "commands_rev": int(self._commands_revision),
                "expected_extension_version": expected,
                "current_extension_version": current,
                "version_match": bool(expected and current and expected == current),
            }

    def _extension_version_payload(self) -> dict[str, object]:
        with self._state_lock:
            expected = str(self._expected_extension_version)
            current = str(self._current_extension_version)
        return {
            "ok": True,
            "protocol_version": _BRIDGE_PROTOCOL_VERSION,
            "expected_extension_version": expected,
            "current_extension_version": current,
            "version_match": bool(expected and current and expected == current),
        }

    def _changes_payload(self, session_id: str, markers_rev: int, commands_rev: int) -> dict[str, object]:
        with self._state_lock:
            self._refresh_markers_state_locked()
            current_markers_rev = int(self._markers_revision)
            current_commands_rev = int(self._commands_revision)
            commands_pending = bool(self._pending_commands)
            if str(session_id or "") != self._session_id:
                return {
                    "ok": True,
                    "protocol_version": _BRIDGE_PROTOCOL_VERSION,
                    "session_id": self._session_id,
                    "markers_rev": current_markers_rev,
                    "commands_rev": current_commands_rev,
                    "session_changed": True,
                    "markers_changed": True,
                    "commands_changed": True,
                    "commands_pending": commands_pending,
                }
            return {
                "ok": True,
                "protocol_version": _BRIDGE_PROTOCOL_VERSION,
                "session_id": self._session_id,
                "markers_rev": current_markers_rev,
                "commands_rev": current_commands_rev,
                "session_changed": False,
                "markers_changed": int(markers_rev) != current_markers_rev,
                "commands_changed": int(commands_rev) != current_commands_rev,
                "commands_pending": commands_pending,
            }

    def _markers_payload(self, session_id: str) -> tuple[bool, dict[str, object]]:
        with self._state_lock:
            self._refresh_markers_state_locked()
            if str(session_id or "") != self._session_id:
                return False, self._invalid_session_payload_locked()
            subscribed_tokens, manual_tokens, subscribed_tag_ids, manual_tag_ids = self._markers_snapshot or (
                tuple(),
                tuple(),
                tuple(),
                tuple(),
            )
            return True, {
                "ok": True,
                "protocol_version": _BRIDGE_PROTOCOL_VERSION,
                "session_id": self._session_id,
                "markers_rev": int(self._markers_revision),
                "commands_rev": int(self._commands_revision),
                "subscribed_tokens": list(subscribed_tokens),
                "manual_tokens": list(manual_tokens),
                "subscribed_tag_ids": list(subscribed_tag_ids),
                "manual_tag_ids": list(manual_tag_ids),
            }

    def _consume_next_command_payload(self, session_id: str) -> tuple[bool, dict[str, object]]:
        with self._state_lock:
            if str(session_id or "") != self._session_id:
                return False, self._invalid_session_payload_locked()
            command: dict[str, object] | None = None
            if self._pending_commands:
                command = dict(self._pending_commands.pop(0))
                self._commands_revision += 1
                consumed_event = dict(command)
                consumed_event["consumed_at"] = float(time.time())
                self._consumed_commands.append(consumed_event)
                self._debug(
                    f"Consumed command id={int(command.get('id') or 0)} "
                    f"new_rev={self._commands_revision} pending={len(self._pending_commands)}"
                )
            return True, {
                "ok": True,
                "protocol_version": _BRIDGE_PROTOCOL_VERSION,
                "session_id": self._session_id,
                "commands_rev": int(self._commands_revision),
                "command": command,
            }

    def _legacy_installed_markers_payload(self) -> dict[str, object]:
        with self._state_lock:
            self._refresh_markers_state_locked()
            subscribed_tokens, manual_tokens, subscribed_tag_ids, manual_tag_ids = self._markers_snapshot or (
                tuple(),
                tuple(),
                tuple(),
                tuple(),
            )
            payload: dict[str, object] = {
                "ok": True,
                "bridge_session_id": self._session_id,
                "subscribed_tokens": list(subscribed_tokens),
                "manual_tokens": list(manual_tokens),
                "subscribed_tag_ids": list(subscribed_tag_ids),
                "manual_tag_ids": list(manual_tag_ids),
            }
            if self._pending_commands:
                payload["open_url_command"] = dict(self._pending_commands[0])
            return payload

    def _invalid_session_payload_locked(self) -> dict[str, object]:
        return {
            "ok": False,
            "error": "invalid_session",
            "protocol_version": _BRIDGE_PROTOCOL_VERSION,
            "session_id": self._session_id,
            "markers_rev": int(self._markers_revision),
            "commands_rev": int(self._commands_revision),
        }


def _qs_first(query: dict[str, list[str]], key: str) -> str:
    values = query.get(str(key), [])
    if not values:
        return ""
    return str(values[0] or "").strip()


def _qs_int(query: dict[str, list[str]], key: str, default: int = 0) -> int:
    raw = _qs_first(query, key)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return int(default)
