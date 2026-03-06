from __future__ import annotations

import errno
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

from core.online_repo import (
    DownloadResult,
    OnlineRepoClient,
    is_beamng_resource_download_url,
    parse_beamng_protocol_uri,
)


def test_parse_beamng_protocol_uri() -> None:
    assert parse_beamng_protocol_uri("beamng:v1/subscriptionMod/M8HUFSQ9P") == ("subscriptionMod", "M8HUFSQ9P")
    assert parse_beamng_protocol_uri("beamng:v1/showMod/M8HUFSQ9P") == ("showMod", "M8HUFSQ9P")
    assert parse_beamng_protocol_uri("beamng://v1/showMod/M8HUFSQ9P") is None
    assert parse_beamng_protocol_uri("https://www.beamng.com/resources/") is None


def test_is_beamng_resource_download_url() -> None:
    assert is_beamng_resource_download_url("https://www.beamng.com/resources/foo.123/download?version=456")
    assert is_beamng_resource_download_url("https://beamng.com/resources/foo.123/download?version=456")
    assert not is_beamng_resource_download_url("https://www.beamng.com/resources/foo.123/")
    assert not is_beamng_resource_download_url("https://modland.net/resources/foo.zip")
    assert not is_beamng_resource_download_url("beamng:v1/subscriptionMod/M8HUFSQ9P")


def test_subscribe_from_protocol_always_targets_repo(tmp_path: Path, monkeypatch) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")

    monkeypatch.setattr(
        client,
        "resolve_subscription",
        lambda mod_id, source_page_url: {
            "mod_id": mod_id,
            "resource_url": source_page_url,
            "download_url": "https://www.beamng.com/resources/test.1/download?version=2",
            "version_id": "2",
            "title": "Test Mod",
        },
    )

    targets: list[Path] = []

    def _fake_download(download_url: str, destination_dir: Path, overwrite: bool = False) -> DownloadResult:
        del download_url, overwrite
        targets.append(destination_dir)
        destination_dir.mkdir(parents=True, exist_ok=True)
        mod_file = destination_dir / "test_mod.zip"
        mod_file.write_bytes(b"zip")
        return DownloadResult(True, "ok", file_path=mod_file, file_name=mod_file.name)

    monkeypatch.setattr(client, "download_url_to_path", _fake_download)

    result = client.subscribe_from_protocol("M8HUFSQ9P", "https://www.beamng.com/resources/test.1/")
    assert result.ok
    assert targets == [beam / "repo"]

    subs = client.load_subscriptions()
    assert "M8HUFSQ9P" in subs
    assert subs["M8HUFSQ9P"]["destination_kind"] == "repo"
    assert Path(subs["M8HUFSQ9P"]["installed_path"]).name == "test_mod.zip"


def test_download_url_to_path_overwrite_behavior(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")

    source_file = tmp_path / "source_mod.zip"
    source_file.write_bytes(b"zip-data")
    source_url = source_file.as_uri()
    dest_dir = tmp_path / "dest"

    first = client.download_url_to_path(source_url, dest_dir, overwrite=False)
    assert first.ok
    assert first.file_path is not None
    assert first.file_path.read_bytes() == b"zip-data"

    second = client.download_url_to_path(source_url, dest_dir, overwrite=False)
    assert not second.ok
    assert "Destination already exists" in second.message

    source_file.write_bytes(b"zip-data-new")
    third = client.download_url_to_path(source_url, dest_dir, overwrite=True)
    assert third.ok
    assert third.file_path is not None
    assert third.file_path.read_bytes() == b"zip-data-new"


def test_download_url_to_path_cross_device_replace_fallback(tmp_path: Path, monkeypatch) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")

    source_file = tmp_path / "source_mod.zip"
    source_file.write_bytes(b"zip-data-xdev")
    source_url = source_file.as_uri()
    dest_dir = tmp_path / "dest"

    original_replace = Path.replace

    def _fake_replace(self: Path, target: Path) -> Path:
        if self.name.startswith("tmp_"):
            raise OSError(errno.EXDEV, "Cross-device link")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", _fake_replace)

    result = client.download_url_to_path(source_url, dest_dir, overwrite=True)
    assert result.ok
    assert result.file_path is not None
    assert result.file_path.read_bytes() == b"zip-data-xdev"
    assert not any(p.name.startswith("tmp_") for p in dest_dir.glob("tmp_*"))


def test_parse_resource_html_extracts_protocol_and_download(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")

    html = """
    <html>
      <body>
        <h1><span class="prefix">Alpha</span> Test Mod <span>1.0</span></h1>
        <a href="beamng:v1/subscriptionMod/M8HUFSQ9P">Subscribe</a>
        <a href="resources/test-mod.123/download?version=45142">Download Now</a>
      </body>
    </html>
    """
    parsed = client._parse_resource_html(html, "https://www.beamng.com/resources/test-mod.123/")
    assert parsed["mod_id"] == "M8HUFSQ9P"
    assert parsed["version_id"] == "45142"
    assert parsed["download_url"] == "https://www.beamng.com/resources/test-mod.123/download?version=45142"


def test_parse_resource_html_download_normalizes_against_base_for_numeric_page_url(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")

    html = """
    <html>
      <body>
        <a href="resources/offroad-mega-pack.830/download?version=10767">Download Now</a>
      </body>
    </html>
    """
    parsed = client._parse_resource_html(html, "https://www.beamng.com/resources/830/")
    assert parsed["download_url"] == "https://www.beamng.com/resources/offroad-mega-pack.830/download?version=10767"
    assert "/resources/830/resources/" not in parsed["download_url"]


def test_unsubscribe_can_remove_file(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")

    repo = beam / "repo"
    repo.mkdir()
    mod_file = repo / "a.zip"
    mod_file.write_bytes(b"x")
    client.save_subscriptions(
        {
            "MID": {
                "mod_id": "MID",
                "destination_kind": "repo",
                "installed_path": str(mod_file),
            }
        }
    )

    ok, msg = client.unsubscribe("MID", remove_file=True)
    assert ok
    assert "Unsubscribed" in msg
    assert not mod_file.exists()
    assert "MID" not in client.load_subscriptions()


def test_list_subscriptions_includes_repo_mod_ids_from_db(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    repo = beam / "repo"
    repo.mkdir()
    (repo / "db_only.zip").write_bytes(b"x")
    (beam / "db.json").write_text(
        """
{
  "header": {"version": 1.1},
  "mods": {
    "db_only": {
      "filename": "db_only.zip",
      "dirname": "/mods/repo/",
      "fullpath": "/mods/repo/db_only.zip",
      "modID": "DBONLY01"
    }
  }
}
""".strip(),
        encoding="utf-8",
    )

    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")
    listed = dict(client.list_subscriptions())
    assert "DBONLY01" in listed
    assert listed["DBONLY01"]["installed_name"] == "db_only.zip"
    assert listed["DBONLY01"]["destination_kind"] == "repo"


def test_unsubscribe_db_only_entry_removes_db_row_and_file(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    repo = beam / "repo"
    repo.mkdir()
    mod_file = repo / "db_only.zip"
    mod_file.write_bytes(b"x")
    db_path = beam / "db.json"
    db_path.write_text(
        """
{
  "header": {"version": 1.1},
  "mods": {
    "db_only": {
      "filename": "db_only.zip",
      "dirname": "/mods/repo/",
      "fullpath": "/mods/repo/db_only.zip",
      "modID": "DBONLY01"
    }
  }
}
""".strip(),
        encoding="utf-8",
    )

    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")
    ok, _msg = client.unsubscribe("DBONLY01", remove_file=True)
    assert ok
    assert not mod_file.exists()
    assert "DBONLY01" not in dict(client.list_subscriptions())
    assert "DBONLY01" not in db_path.read_text(encoding="utf-8")


def test_list_subscriptions_accepts_nested_moddata_tagid(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    repo = beam / "repo"
    repo.mkdir()
    (repo / "tag_only.zip").write_bytes(b"x")
    (beam / "db.json").write_text(
        """
{
  "header": {"version": 1.1},
  "mods": {
    "tag_only": {
      "filename": "tag_only.zip",
      "dirname": "/mods/repo/",
      "fullpath": "/mods/repo/tag_only.zip",
      "modData": {"tagid": "TAGONLY01"}
    }
  }
}
""".strip(),
        encoding="utf-8",
    )

    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")
    listed = dict(client.list_subscriptions())
    assert "TAGONLY01" in listed
    assert listed["TAGONLY01"]["installed_name"] == "tag_only.zip"


def test_unsubscribe_by_nested_moddata_tagid_removes_db_row_and_file(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    repo = beam / "repo"
    repo.mkdir()
    mod_file = repo / "tag_only.zip"
    mod_file.write_bytes(b"x")
    db_path = beam / "db.json"
    db_path.write_text(
        """
{
  "header": {"version": 1.1},
  "mods": {
    "tag_only": {
      "filename": "tag_only.zip",
      "dirname": "/mods/repo/",
      "fullpath": "/mods/repo/tag_only.zip",
      "modData": {"tagid": "TAGONLY01"}
    }
  }
}
""".strip(),
        encoding="utf-8",
    )

    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")
    ok, _msg = client.unsubscribe("TAGONLY01", remove_file=True)
    assert ok
    assert not mod_file.exists()
    assert "TAGONLY01" not in dict(client.list_subscriptions())
    assert "tag_only" not in db_path.read_text(encoding="utf-8")


def test_db_tagid_subscription_includes_resource_url_and_version(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    (beam / "repo").mkdir()
    (beam / "repo" / "tag_only.zip").write_bytes(b"x")
    (beam / "db.json").write_text(
        """
{
  "header": {"version": 1.1},
  "mods": {
    "tag_only": {
      "filename": "tag_only.zip",
      "dirname": "/mods/repo/",
      "fullpath": "/mods/repo/tag_only.zip",
      "modData": {
        "tagid": "TAGONLY01",
        "resource_id": 26315,
        "current_version_id": 45888
      }
    }
  }
}
""".strip(),
        encoding="utf-8",
    )
    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")
    listed = dict(client.list_subscriptions())
    entry = listed["TAGONLY01"]
    assert entry["resource_url"] == "https://www.beamng.com/resources/26315/"
    assert entry["version_id"] == "45888"
    assert entry["download_url"] == "https://www.beamng.com/resources/26315/download?version=45888"


def test_download_http_error_includes_status_code(tmp_path: Path, monkeypatch) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")

    def _raise_http_error(*_args, **_kwargs):
        raise HTTPError(
            url="https://www.beamng.com/resources/a/download?version=1",
            code=404,
            msg="Not Found",
            hdrs=None,
            fp=BytesIO(b""),
        )

    monkeypatch.setattr("core.online_repo.urlopen", _raise_http_error)
    result = client.download_url_to_path("https://www.beamng.com/resources/a/download?version=1", tmp_path / "out")
    assert not result.ok
    assert "HTTP 404" in result.message


def test_download_http_error_can_cancel_via_request_error_handler(tmp_path: Path, monkeypatch) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")
    client.set_request_error_handler(lambda _message: False)

    def _raise_http_error(*_args, **_kwargs):
        raise HTTPError(
            url="https://www.beamng.com/resources/a/download?version=1",
            code=429,
            msg="Too Many Requests",
            hdrs=None,
            fp=BytesIO(b""),
        )

    monkeypatch.setattr("core.online_repo.urlopen", _raise_http_error)
    result = client.download_url_to_path("https://www.beamng.com/resources/a/download?version=1", tmp_path / "out")
    assert not result.ok
    assert result.message == "Cancelled by user."
    assert client.cancel_requested()


def test_check_updates_can_cancel_after_request_error_prompt(tmp_path: Path, monkeypatch) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")
    client.set_request_error_handler(lambda _message: False)
    client.save_subscriptions(
        {
            "MID1": {
                "provider": "beamng",
                "mod_id": "MID1",
                "resource_url": "https://www.beamng.com/resources/a.1/",
                "destination_kind": "repo",
            },
            "MID2": {
                "provider": "beamng",
                "mod_id": "MID2",
                "resource_url": "https://www.beamng.com/resources/b.2/",
                "destination_kind": "repo",
            },
        }
    )

    def _raise_http_error(*_args, **_kwargs):
        raise HTTPError(
            url="https://www.beamng.com/resources/a.1/",
            code=429,
            msg="Too Many Requests",
            hdrs=None,
            fp=BytesIO(b""),
        )

    monkeypatch.setattr("core.online_repo.urlopen", _raise_http_error)
    updates = client.check_updates()
    cancelled_rows = [item for item in updates if isinstance(item, dict) and item.get("cancelled")]
    assert len(cancelled_rows) == 1
    assert cancelled_rows[0]["mod_id"] == "MID1"
    assert cancelled_rows[0]["message"] == "Cancelled by user."
    assert not any(isinstance(item, dict) and item.get("mod_id") == "MID2" for item in updates)


def test_update_subscriptions_filters_selected_mod_ids(tmp_path: Path, monkeypatch) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")

    repo = beam / "repo"
    repo.mkdir()
    existing = repo / "first.zip"
    existing.write_bytes(b"old")
    client.save_subscriptions(
        {
            "MID1": {
                "provider": "beamng",
                "mod_id": "MID1",
                "destination_kind": "repo",
                "installed_path": str(existing),
                "installed_name": existing.name,
                "version_id": "1",
            },
            "MID2": {
                "provider": "beamng",
                "mod_id": "MID2",
                "destination_kind": "repo",
                "installed_name": "second.zip",
                "version_id": "1",
            },
        }
    )

    monkeypatch.setattr(
        client,
        "check_updates",
        lambda: [
            {
                "mod_id": "MID1",
                "ok": True,
                "update_available": True,
                "download_url": "https://example.com/first.zip",
                "latest_version": "2",
                "resource_url": "https://www.beamng.com/resources/1/",
                "title": "First",
            },
            {
                "mod_id": "MID2",
                "ok": True,
                "update_available": True,
                "download_url": "https://example.com/second.zip",
                "latest_version": "2",
                "resource_url": "https://www.beamng.com/resources/2/",
                "title": "Second",
            },
        ],
    )

    def _fake_download(download_url: str, destination_dir: Path, overwrite: bool = False) -> DownloadResult:
        del overwrite
        destination_dir.mkdir(parents=True, exist_ok=True)
        if download_url.endswith("first.zip"):
            target = destination_dir / "first.zip"
            target.write_bytes(b"new-first")
            return DownloadResult(True, "ok", file_path=target, file_name=target.name)
        target = destination_dir / "second.zip"
        target.write_bytes(b"new-second")
        return DownloadResult(True, "ok", file_path=target, file_name=target.name)

    monkeypatch.setattr(client, "download_url_to_path", _fake_download)

    updated, failed, messages = client.update_subscriptions(mod_ids=["MID2"], overwrite=True)
    assert updated == 1
    assert failed == 0
    assert messages == []
    saved = client.load_subscriptions()
    assert saved["MID2"]["version_id"] == "2"
    assert saved["MID1"]["version_id"] == "1"
    assert (repo / "first.zip").read_bytes() == b"old"
    assert (repo / "second.zip").exists()


def test_update_subscriptions_reports_unknown_selected_id(tmp_path: Path, monkeypatch) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")

    monkeypatch.setattr(client, "check_updates", lambda: [])
    updated, failed, messages = client.update_subscriptions(mod_ids=["UNKNOWN"], overwrite=True)
    assert updated == 0
    assert failed == 1
    assert messages == ["UNKNOWN: Subscription not found."]


def test_update_subscriptions_pauses_between_download_requests(tmp_path: Path, monkeypatch) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")

    client.save_subscriptions(
        {
            "MID1": {"provider": "beamng", "mod_id": "MID1", "destination_kind": "repo"},
            "MID2": {"provider": "beamng", "mod_id": "MID2", "destination_kind": "repo"},
        }
    )

    monkeypatch.setattr(
        client,
        "check_updates",
        lambda: [
            {
                "mod_id": "MID1",
                "ok": True,
                "update_available": True,
                "download_url": "https://example.com/1.zip",
                "latest_version": "2",
                "resource_url": "https://www.beamng.com/resources/1/",
                "title": "One",
            },
            {
                "mod_id": "MID2",
                "ok": True,
                "update_available": True,
                "download_url": "https://example.com/2.zip",
                "latest_version": "3",
                "resource_url": "https://www.beamng.com/resources/2/",
                "title": "Two",
            },
        ],
    )

    call_order: list[str] = []

    def _fake_sleep(seconds: float) -> None:
        call_order.append(f"sleep:{seconds}")

    monkeypatch.setattr("core.online_repo.time.sleep", _fake_sleep)

    def _fake_download(download_url: str, destination_dir: Path, overwrite: bool = False) -> DownloadResult:
        del overwrite
        destination_dir.mkdir(parents=True, exist_ok=True)
        target = destination_dir / Path(download_url).name
        target.write_bytes(b"x")
        call_order.append(f"download:{download_url}")
        return DownloadResult(True, "ok", file_path=target, file_name=target.name)

    monkeypatch.setattr(client, "download_url_to_path", _fake_download)

    updated, failed, _messages = client.update_subscriptions(mod_ids=["MID1", "MID2"], overwrite=True)
    assert updated == 2
    assert failed == 0
    assert call_order == [
        "download:https://example.com/1.zip",
        "sleep:1.0",
        "download:https://example.com/2.zip",
    ]


def test_update_subscriptions_cancelled_download_has_single_cancellation_message(tmp_path: Path, monkeypatch) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")

    client.save_subscriptions({"MID1": {"provider": "beamng", "mod_id": "MID1", "destination_kind": "repo"}})
    monkeypatch.setattr(
        client,
        "check_updates",
        lambda: [
            {
                "mod_id": "MID1",
                "ok": True,
                "update_available": True,
                "download_url": "https://example.com/1.zip",
                "latest_version": "2",
                "resource_url": "https://www.beamng.com/resources/1/",
                "title": "One",
            }
        ],
    )

    def _cancelled_download(_download_url: str, _destination_dir: Path, overwrite: bool = False) -> DownloadResult:
        del overwrite
        return DownloadResult(False, "Cancelled by user.")

    monkeypatch.setattr(client, "download_url_to_path", _cancelled_download)

    updated, failed, messages = client.update_subscriptions(mod_ids=None, overwrite=True)
    assert updated == 0
    assert failed == 1
    assert messages == ["MID1: Cancelled by user."]
