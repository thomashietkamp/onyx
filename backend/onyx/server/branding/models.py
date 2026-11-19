"""Branding settings.

The fields and their JSON names match `EnterpriseSettings` in
`web/src/lib/settings/types.ts`, which the web app already renders. The read
route is public (the login page needs it before sign-in), so nothing here may
be sensitive.
"""

from enum import Enum
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

# Free-text caps in characters. The admin page reads them from
# `GET /admin/enterprise-settings/limits`, so this is the only copy.
BRANDING_FIELD_MAX_LENGTHS: dict[str, int] = {
    "application_name": 50,
    "custom_header_content": 300,
    "custom_lower_disclaimer_content": 1000,
    "custom_popup_header": 100,
    "custom_popup_content": 5000,
    "consent_screen_prompt": 2000,
    "custom_greeting_message": 200,
    "custom_login_subtitle": 300,
    "custom_help_link_url": 2048,
    "custom_help_link_label": 50,
}

MAX_NAV_ITEMS = 20
_NAV_TITLE_MAX = 60
_NAV_LINK_MAX = 2048
_NAV_ICON_MAX = 100
_NAV_SVG_MAX = 20_000


def _require_web_link(value: str, allow_relative: bool) -> str:
    """Links render as `href` for every visitor, so only web schemes pass:
    `javascript:` or `data:` would run script on the app origin."""
    stripped = value.strip()
    if allow_relative and stripped.startswith("/") and not stripped.startswith("//"):
        return stripped
    parsed = urlparse(stripped)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Use an http:// or https:// URL.")
    return stripped


class LogoDisplayStyle(str, Enum):
    LOGO_AND_NAME = "logo_and_name"
    LOGO_ONLY = "logo_only"
    NAME_ONLY = "name_only"


class NavigationItem(BaseModel):
    link: str = Field(max_length=_NAV_LINK_MAX)
    title: str = Field(max_length=_NAV_TITLE_MAX)
    icon: str | None = Field(default=None, max_length=_NAV_ICON_MAX)
    svg_logo: str | None = Field(default=None, max_length=_NAV_SVG_MAX)

    @field_validator("link")
    @classmethod
    def _link_is_web(cls, value: str) -> str:
        return _require_web_link(value, allow_relative=True)


class BrandingSettings(BaseModel):
    """What the admin sets. Every field has a default, because stored blobs
    from an older build lack newer keys."""

    application_name: str | None = None
    logo_display_style: LogoDisplayStyle | None = None

    custom_nav_items: list[NavigationItem] = Field(
        default_factory=list, max_length=MAX_NAV_ITEMS
    )

    custom_lower_disclaimer_content: str | None = None
    custom_header_content: str | None = None
    two_lines_for_chat_header: bool | None = None
    custom_popup_header: str | None = None
    custom_popup_content: str | None = None
    enable_consent_screen: bool | None = None
    consent_screen_prompt: str | None = None
    show_first_visit_notice: bool | None = None
    custom_greeting_message: str | None = None
    custom_login_subtitle: str | None = None

    custom_help_link_url: str | None = None
    custom_help_link_label: str | None = None

    @field_validator("custom_help_link_url")
    @classmethod
    def _help_link_is_web(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return _require_web_link(value, allow_relative=False)


class BrandingSettingsResponse(BrandingSettings):
    """What every caller reads. The logo flags follow the stored files, so an
    admin cannot point the UI at a logo that does not exist."""

    use_custom_logo: bool = False
    use_custom_logotype: bool = False


class BrandingLimits(BaseModel):
    field_max_lengths: dict[str, int]
    max_logo_bytes: int
    max_nav_items: int
