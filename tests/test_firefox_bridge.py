from __future__ import annotations

import json
import socket
import urllib.request
from urllib.parse import quote

from core.firefox_bridge import FirefoxBridgeServer


def _free_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def test_firefox_bridge_health_and_markers() -> None:
    port = _free_port()
    server = FirefoxBridgeServer(
        markers_provider=lambda: ({"123"}, {"456"}, {"abc"}, {"def"}),
        port=port,
    )
    ok, _message = server.start()
    assert ok
    try:
        health_raw = urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2).read().decode("utf-8")
        health = json.loads(health_raw)
        assert health.get("ok") is True

        session_raw = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/session/start",
            timeout=2,
        ).read().decode("utf-8")
        session_payload = json.loads(session_raw)
        assert session_payload.get("ok") is True
        session_id = str(session_payload.get("session_id") or "")
        assert session_id
        markers_rev = int(session_payload.get("markers_rev") or 0)
        commands_rev = int(session_payload.get("commands_rev") or 0)

        markers_raw = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/markers?session_id={quote(session_id, safe='')}",
            timeout=2,
        ).read().decode("utf-8")
        markers = json.loads(markers_raw)
        assert markers.get("ok") is True
        assert markers.get("session_id") == session_id
        assert markers.get("subscribed_tokens") == ["123"]
        assert markers.get("manual_tokens") == ["456"]
        assert markers.get("subscribed_tag_ids") == ["abc"]
        assert markers.get("manual_tag_ids") == ["def"]

        changes_raw = urllib.request.urlopen(
            (
                f"http://127.0.0.1:{port}/changes"
                f"?session_id={quote(session_id, safe='')}"
                f"&markers_rev={markers_rev}&commands_rev={commands_rev}"
            ),
            timeout=2,
        ).read().decode("utf-8")
        changes = json.loads(changes_raw)
        assert changes.get("ok") is True
        assert changes.get("session_changed") is False
        assert changes.get("markers_changed") is False
        assert changes.get("commands_changed") is False
        assert changes.get("commands_pending") is False
    finally:
        server.stop()


def test_firefox_bridge_commands_are_consumed_once_requested() -> None:
    port = _free_port()
    server = FirefoxBridgeServer(
        markers_provider=lambda: (set(), set(), set(), set()),
        port=port,
    )
    ok, _message = server.start()
    assert ok
    try:
        queued1, _message1 = server.queue_open_url("https://www.beamng.com/resources/example.123/")
        queued2, _message2 = server.queue_open_url("https://www.beamng.com/resources/example.456/")
        assert queued1 and queued2

        session_raw = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/session/start",
            timeout=2,
        ).read().decode("utf-8")
        session = json.loads(session_raw)
        session_id = str(session.get("session_id") or "")
        assert session_id

        cmd1_raw = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/commands/next?session_id={quote(session_id, safe='')}",
            timeout=2,
        ).read().decode("utf-8")
        cmd1 = json.loads(cmd1_raw)
        assert cmd1.get("ok") is True
        assert isinstance(cmd1.get("command"), dict)
        assert cmd1["command"].get("id") == 1
        assert cmd1["command"].get("url") == "https://www.beamng.com/resources/example.123/"

        cmd2_raw = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/commands/next?session_id={quote(session_id, safe='')}",
            timeout=2,
        ).read().decode("utf-8")
        cmd2 = json.loads(cmd2_raw)
        assert cmd2.get("ok") is True
        assert isinstance(cmd2.get("command"), dict)
        assert cmd2["command"].get("id") == 2
        assert cmd2["command"].get("url") == "https://www.beamng.com/resources/example.456/"

        cmd3_raw = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/commands/next?session_id={quote(session_id, safe='')}",
            timeout=2,
        ).read().decode("utf-8")
        cmd3 = json.loads(cmd3_raw)
        assert cmd3.get("ok") is True
        assert cmd3.get("command") is None

        consumed = server.drain_consumed_commands()
        assert len(consumed) == 2
        assert consumed[0].get("id") == 1
        assert consumed[1].get("id") == 2
        assert isinstance(consumed[0].get("consumed_at"), float)
        assert server.drain_consumed_commands() == []
    finally:
        server.stop()


def test_firefox_bridge_changes_reports_pending_commands() -> None:
    port = _free_port()
    server = FirefoxBridgeServer(
        markers_provider=lambda: (set(), set(), set(), set()),
        port=port,
    )
    ok, _message = server.start()
    assert ok
    try:
        session_raw = urllib.request.urlopen(
            f"http://127.0.0.1:{port}/session/start",
            timeout=2,
        ).read().decode("utf-8")
        session_payload = json.loads(session_raw)
        session_id = str(session_payload.get("session_id") or "")
        markers_rev = int(session_payload.get("markers_rev") or 0)
        commands_rev = int(session_payload.get("commands_rev") or 0)
        assert session_id

        queued, _message = server.queue_open_url("https://www.beamng.com/resources/example.777/")
        assert queued

        changes_raw = urllib.request.urlopen(
            (
                f"http://127.0.0.1:{port}/changes"
                f"?session_id={quote(session_id, safe='')}"
                f"&markers_rev={markers_rev}&commands_rev={commands_rev}"
            ),
            timeout=2,
        ).read().decode("utf-8")
        changes = json.loads(changes_raw)
        assert changes.get("ok") is True
        assert changes.get("session_changed") is False
        assert changes.get("markers_changed") is False
        assert changes.get("commands_changed") is True
        assert changes.get("commands_pending") is True
    finally:
        server.stop()
