import io
from enum import Enum
from typing import IO

from PIL import Image, UnidentifiedImageError
from pydantic import ValidationError

from onyx.configs.constants import (
    KV_BRANDING_SETTINGS_KEY,
    KV_CUSTOM_ANALYTICS_SCRIPT_KEY,
    ONYX_DEFAULT_APPLICATION_NAME,
    FileOrigin,
)
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.file_store.file_store import get_default_file_store
from onyx.key_value_store.factory import get_kv_store
from onyx.key_value_store.interface import KvKeyNotFoundError
from onyx.server.branding.models import (
    BRANDING_FIELD_MAX_LENGTHS,
    BrandingSettings,
    BrandingSettingsResponse,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()

MAX_LOGO_BYTES = 5 * 1024 * 1024
# Checked from the image header before any decode: a small compressed file
# can decode to a huge bitmap, and emails decode the logo each time they send.
MAX_LOGO_PIXELS = 4096 * 4096

# Only inert rasters. The logo is served without auth from the app origin, so
# an SVG or HTML body would run as script there.
_ALLOWED_FORMATS: dict[str, str] = {"PNG": "image/png", "JPEG": "image/jpeg"}


class LogoKind(str, Enum):
    # The square mark next to the name.
    LOGO = "logo"
    # A wide image that replaces the name.
    LOGOTYPE = "logotype"

    @property
    def file_id(self) -> str:
        return f"__fork_branding_{self.value}__"


def _clamp(settings: BrandingSettings) -> BrandingSettings:
    """Trims stored text to the current caps. A lower cap must never make the
    public route fail for a blob that an older build stored."""
    updates: dict[str, str] = {}
    for field, limit in BRANDING_FIELD_MAX_LENGTHS.items():
        value = settings.model_dump().get(field)
        if isinstance(value, str) and len(value) > limit:
            updates[field] = value[:limit]
    return settings.model_copy(update=updates) if updates else settings


def load_branding_settings() -> BrandingSettings:
    try:
        stored = get_kv_store().load(KV_BRANDING_SETTINGS_KEY)
    except KvKeyNotFoundError:
        return BrandingSettings()
    try:
        return _clamp(BrandingSettings.model_validate(stored))
    except ValidationError:
        # Defaults keep the login page working while an admin fixes it.
        logger.exception("Stored branding settings are invalid; using defaults")
        return BrandingSettings()


def validate_lengths(settings: BrandingSettings) -> None:
    dumped = settings.model_dump()
    for field, limit in BRANDING_FIELD_MAX_LENGTHS.items():
        value = dumped.get(field)
        if isinstance(value, str) and len(value) > limit:
            raise OnyxError(
                OnyxErrorCode.INVALID_INPUT,
                f"{field} has {len(value)} characters; the limit is {limit}.",
            )


def store_branding_settings(settings: BrandingSettings) -> None:
    validate_lengths(settings)
    get_kv_store().store(KV_BRANDING_SETTINGS_KEY, settings.model_dump(mode="json"))


def _has_logo(kind: LogoKind) -> bool:
    store = get_default_file_store()
    return any(
        store.has_file(kind.file_id, FileOrigin.OTHER, mime)
        for mime in _ALLOWED_FORMATS.values()
    )


def branding_response() -> BrandingSettingsResponse:
    return BrandingSettingsResponse(
        **load_branding_settings().model_dump(),
        use_custom_logo=_has_logo(LogoKind.LOGO),
        use_custom_logotype=_has_logo(LogoKind.LOGOTYPE),
    )


def application_name() -> str:
    """The name for emails and reports: the custom name, else the default."""
    name = load_branding_settings().application_name
    return name.strip() if name and name.strip() else ONYX_DEFAULT_APPLICATION_NAME


def _sniff_logo(data: bytes) -> str:
    """The MIME type of a valid PNG or JPEG, judged by content alone."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image_format = image.format
            width, height = image.size
            if image_format not in _ALLOWED_FORMATS:
                raise OnyxError(
                    OnyxErrorCode.INVALID_INPUT, "The logo must be a PNG or JPEG."
                )
            if width * height > MAX_LOGO_PIXELS:
                raise OnyxError(
                    OnyxErrorCode.INVALID_INPUT,
                    f"The logo is {width}x{height} pixels; the limit is "
                    f"{MAX_LOGO_PIXELS} pixels in total.",
                )
            image.verify()
    except (
        UnidentifiedImageError,
        Image.DecompressionBombError,
        SyntaxError,
        OSError,
    ) as e:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT, "The logo is not a valid PNG or JPEG."
        ) from e
    return _ALLOWED_FORMATS[image_format]


def upload_logo(kind: LogoKind, content: IO[bytes], display_name: str | None) -> None:
    data = content.read(MAX_LOGO_BYTES + 1)
    if len(data) > MAX_LOGO_BYTES:
        raise OnyxError(
            OnyxErrorCode.INVALID_INPUT,
            f"The logo is larger than {MAX_LOGO_BYTES // (1024 * 1024)} MB.",
        )
    mime_type = _sniff_logo(data)
    store = get_default_file_store()
    store.delete_file(kind.file_id, error_on_missing=False)
    store.save_file(
        content=io.BytesIO(data),
        display_name=display_name,
        file_origin=FileOrigin.OTHER,
        file_type=mime_type,
        file_id=kind.file_id,
    )


def delete_logo(kind: LogoKind) -> None:
    get_default_file_store().delete_file(kind.file_id, error_on_missing=False)


def read_logo(kind: LogoKind) -> tuple[bytes, str] | None:
    """(bytes, MIME type) of the stored logo, or None."""
    if not _has_logo(kind):
        return None
    stored = get_default_file_store().get_file_with_mime_type(kind.file_id)
    if stored is None:
        return None
    return stored.data, stored.mime_type


def logo_file_id(kind: LogoKind) -> str | None:
    """The file store id of the stored logo, for callers that read it directly."""
    return kind.file_id if _has_logo(kind) else None


def load_custom_analytics_script() -> str | None:
    try:
        stored = get_kv_store().load(KV_CUSTOM_ANALYTICS_SCRIPT_KEY)
    except KvKeyNotFoundError:
        return None
    return stored if isinstance(stored, str) and stored else None


def store_custom_analytics_script(script: str) -> None:
    get_kv_store().store(KV_CUSTOM_ANALYTICS_SCRIPT_KEY, script)
