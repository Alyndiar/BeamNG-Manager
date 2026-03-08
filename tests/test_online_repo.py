from __future__ import annotations

import errno
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError

from core.online_repo import (
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


def test_direct_download_targets_destination(tmp_path: Path) -> None:
    beam = tmp_path / "beam"
    lib = tmp_path / "lib"
    beam.mkdir()
    lib.mkdir()
    client = OnlineRepoClient(beam, lib, cache_root=tmp_path / ".cache")

    source_file = tmp_path / "source_mod.zip"
    source_file.write_bytes(b"zip-data")
    source_url = source_file.as_uri()
    dest_dir = tmp_path / "dest"

    result = client.direct_download(source_url, dest_dir, overwrite=False)
    assert result.ok
    assert result.file_path is not None
    assert result.file_path.parent == dest_dir


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
