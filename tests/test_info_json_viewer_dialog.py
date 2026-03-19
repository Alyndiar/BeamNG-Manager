from pathlib import Path
import ssl
import urllib.error

from PySide6.QtGui import QImage

from ui.info_json_viewer_dialog import (
    _gallery_item_dimensions_px,
    _gallery_tile_height_px,
    _gallery_tile_width_px,
    _inject_cached_image_previews,
    _is_remote_message_link,
    _message_image_cache_path,
    _should_retry_with_windows_cert_context,
    _write_message_image_cache,
)


def test_is_remote_message_link_accepts_http_urls() -> None:
    assert _is_remote_message_link("https://example.com/preview.png")
    assert _is_remote_message_link("http://example.com/thread/123")
    assert not _is_remote_message_link("https://pp.userapi.com/example.jpg")
    assert not _is_remote_message_link("mailto:test@example.com")


def test_inject_cached_image_previews_replaces_link_with_preview_markup(tmp_path: Path) -> None:
    cache_file = tmp_path / "cached.png"
    image = QImage(640, 480, QImage.Format_RGB32)
    image.fill(0x336699)
    assert image.save(str(cache_file), "PNG")

    html = '<div><a href="https://example.com/preview.png">preview</a></div>'
    rendered = _inject_cached_image_previews(
        html,
        {"https://example.com/preview.png": cache_file},
        columns=3,
        viewport_width=930,
    )

    assert 'href="https://example.com/preview.png"' in rendered
    assert '<img src="' in rendered
    assert Path(cache_file).name in rendered
    assert ">preview</a>" not in rendered
    assert '<table' in rendered
    assert f'width="{_gallery_tile_width_px(930, 3)}"' in rendered
    assert f'height="{_gallery_tile_height_px(_gallery_tile_width_px(930, 3))}"' in rendered
    assert 'width="228"' in rendered


def test_gallery_tile_width_px_changes_with_columns_and_viewport() -> None:
    assert _gallery_tile_width_px(930, 1) != _gallery_tile_width_px(930, 4)
    assert _gallery_tile_width_px(930, 4) != _gallery_tile_width_px(640, 4)


def test_gallery_item_dimensions_px_uses_image_aspect_ratio_without_upscaling(tmp_path: Path) -> None:
    cache_file = tmp_path / "cached.png"
    image = QImage(320, 200, QImage.Format_RGB32)
    image.fill(0x224466)
    assert image.save(str(cache_file), "PNG")

    assert _gallery_item_dimensions_px(cache_file, 500, 281) == (320, 200)
    assert _gallery_item_dimensions_px(cache_file, 160, 90) == (144, 90)


def test_message_image_cache_path_is_namespaced_by_source_key(tmp_path: Path) -> None:
    url = "https://example.com/preview.png"
    path_a = _message_image_cache_path(tmp_path, "W:/mods/a.zip", url)
    path_b = _message_image_cache_path(tmp_path, "W:/mods/b.zip", url)
    assert path_a != path_b


def test_should_retry_with_windows_cert_context_for_ssl_verify_error() -> None:
    err = urllib.error.URLError(
        ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed certificate in certificate chain")
    )
    assert _should_retry_with_windows_cert_context(err) is True


def test_write_message_image_cache_preserves_small_image_dimensions(tmp_path: Path) -> None:
    source_image = QImage(320, 180, QImage.Format_RGB32)
    source_image.fill(0x123456)
    source_path = tmp_path / "source.png"
    assert source_image.save(str(source_path), "PNG")

    cache_path = tmp_path / "cache.png"
    assert _write_message_image_cache(cache_path, source_path.read_bytes()) is True

    cached = QImage(str(cache_path))
    assert not cached.isNull()
    assert cached.width() == 320
    assert cached.height() == 180


def test_write_message_image_cache_scales_large_image_to_max_bounds(tmp_path: Path) -> None:
    source_image = QImage(1600, 900, QImage.Format_RGB32)
    source_image.fill(0x654321)
    source_path = tmp_path / "source-large.png"
    assert source_image.save(str(source_path), "PNG")

    cache_path = tmp_path / "cache-large.png"
    assert _write_message_image_cache(cache_path, source_path.read_bytes()) is True

    cached = QImage(str(cache_path))
    assert not cached.isNull()
    assert cached.width() == 896
    assert cached.height() == 504
