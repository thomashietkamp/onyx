import io
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from pydantic import ValidationError

from onyx.error_handling.exceptions import OnyxError
from onyx.key_value_store.interface import KvKeyNotFoundError
from onyx.server.branding import store
from onyx.server.branding.models import (
    BRANDING_FIELD_MAX_LENGTHS,
    BrandingSettings,
    NavigationItem,
)


def _image(image_format: str, size: tuple[int, int] = (16, 16)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, "red").save(buffer, format=image_format)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("image_format", "mime_type"), [("PNG", "image/png"), ("JPEG", "image/jpeg")]
)
def test_png_and_jpeg_pass(image_format: str, mime_type: str) -> None:
    assert store._sniff_logo(_image(image_format)) == mime_type


@pytest.mark.parametrize(
    "data",
    [
        b"<html><script>alert(1)</script></html>",
        b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
        # PNG magic bytes with a broken body.
        b"\x89PNG\r\n\x1a\n" + b"\x00" * 64,
    ],
)
def test_non_raster_and_broken_files_fail(data: bytes) -> None:
    with pytest.raises(OnyxError, match="not a valid PNG or JPEG"):
        store._sniff_logo(data)


def test_other_raster_formats_fail() -> None:
    with pytest.raises(OnyxError, match="must be a PNG or JPEG"):
        store._sniff_logo(_image("GIF"))


def test_too_many_pixels_fail() -> None:
    with pytest.raises(OnyxError, match="pixels"):
        store._sniff_logo(_image("PNG", size=(4097, 4096)))


def test_too_many_bytes_fail_before_decode() -> None:
    oversized = io.BytesIO(b"\x00" * (store.MAX_LOGO_BYTES + 1))
    with patch.object(store, "_sniff_logo") as sniff:
        with pytest.raises(OnyxError, match="larger than"):
            store.upload_logo(store.LogoKind.LOGO, oversized, "logo.png")
    sniff.assert_not_called()


@pytest.mark.parametrize(
    "url",
    ["javascript:alert(1)", "data:text/html,x", "//evil.example", "example.com"],
)
def test_help_link_must_be_web_url(url: str) -> None:
    with pytest.raises(ValidationError):
        BrandingSettings(custom_help_link_url=url)


def test_nav_link_allows_paths_and_web_urls() -> None:
    assert NavigationItem(link="/app", title="App").link == "/app"
    assert (
        NavigationItem(link="https://x.example", title="X").link == "https://x.example"
    )
    with pytest.raises(ValidationError):
        NavigationItem(link="javascript:alert(1)", title="X")


def _kv_returning(value: object) -> MagicMock:
    kv = MagicMock()
    kv.load.return_value = value
    return kv


def test_stored_text_over_the_cap_is_trimmed_on_load() -> None:
    limit = BRANDING_FIELD_MAX_LENGTHS["application_name"]
    kv = _kv_returning({"application_name": "x" * (limit + 10)})
    with patch.object(store, "get_kv_store", return_value=kv):
        assert store.load_branding_settings().application_name == "x" * limit


def test_invalid_stored_blob_falls_back_to_defaults() -> None:
    kv = _kv_returning({"custom_help_link_url": "javascript:alert(1)"})
    with patch.object(store, "get_kv_store", return_value=kv):
        assert store.load_branding_settings() == BrandingSettings()


def test_nothing_stored_gives_defaults_and_default_name() -> None:
    kv = MagicMock()
    kv.load.side_effect = KvKeyNotFoundError
    with patch.object(store, "get_kv_store", return_value=kv):
        assert store.load_branding_settings() == BrandingSettings()
        assert store.application_name() == "Onyx"


def test_write_over_the_cap_is_rejected() -> None:
    limit = BRANDING_FIELD_MAX_LENGTHS["custom_greeting_message"]
    with patch.object(store, "get_kv_store") as kv:
        with pytest.raises(OnyxError, match="limit"):
            store.store_branding_settings(
                BrandingSettings(custom_greeting_message="x" * (limit + 1))
            )
    kv.return_value.store.assert_not_called()
