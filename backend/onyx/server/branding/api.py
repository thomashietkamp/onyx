"""Branding routes.

The paths match what the MIT web app already calls
(`web/src/lib/settings/hooks.ts`, `web/src/lib/settings/svcSS.ts`). The read
routes are public: the login page needs branding before anyone signs in.
"""

from fastapi import APIRouter, Depends, Response, UploadFile

from onyx.auth.permissions import require_permission
from onyx.db.enums import Permission
from onyx.db.models import User
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.file_store.serving import resolve_inline_disposition
from onyx.server.branding.models import (
    BRANDING_FIELD_MAX_LENGTHS,
    MAX_NAV_ITEMS,
    BrandingLimits,
    BrandingSettings,
    BrandingSettingsResponse,
)
from onyx.server.branding.store import (
    MAX_LOGO_BYTES,
    LogoKind,
    branding_response,
    delete_logo,
    read_logo,
    store_branding_settings,
    upload_logo,
)
from onyx.utils.audit import (
    AuditAction,
    AuditOutcome,
    actor_from_user,
    emit_audit_event,
)

public_router = APIRouter(prefix="/enterprise-settings")
admin_router = APIRouter(prefix="/admin/enterprise-settings")

_LOGO_TYPES = frozenset({"image/png", "image/jpeg"})

_require_admin = require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)


def _logo_response(kind: LogoKind) -> Response:
    stored = read_logo(kind)
    if stored is None:
        raise OnyxError(OnyxErrorCode.NOT_FOUND, f"No custom {kind.value} is set.")
    data, mime_type = stored
    # Serve only the raster types, never what a sniffer guesses.
    media_type, headers = resolve_inline_disposition(
        mime_type, inline_types=_LOGO_TYPES
    )
    # The URL never changes, so the browser must revalidate after an upload.
    headers["Cache-Control"] = "no-cache"
    return Response(content=data, media_type=media_type, headers=headers)


@public_router.get("")
def get_branding() -> BrandingSettingsResponse:
    return branding_response()


@public_router.get("/logo")
def get_logo() -> Response:
    return _logo_response(LogoKind.LOGO)


@public_router.get("/logotype")
def get_logotype() -> Response:
    return _logo_response(LogoKind.LOGOTYPE)


@admin_router.get("/limits")
def get_branding_limits(
    _: User = Depends(_require_admin),
) -> BrandingLimits:
    return BrandingLimits(
        field_max_lengths=BRANDING_FIELD_MAX_LENGTHS,
        max_logo_bytes=MAX_LOGO_BYTES,
        max_nav_items=MAX_NAV_ITEMS,
    )


def _audit(user: User, change: str) -> None:
    emit_audit_event(
        AuditAction.BRANDING_CHANGE,
        AuditOutcome.SUCCESS,
        actor=actor_from_user(user),
        resource_type="settings",
        extra={"change": change},
    )


@admin_router.put("")
def put_branding(
    settings: BrandingSettings,
    user: User = Depends(_require_admin),
) -> BrandingSettingsResponse:
    """Replaces the whole settings object. Send every field you want to keep."""
    store_branding_settings(settings)
    _audit(user, "settings")
    return branding_response()


@admin_router.put("/{kind}")
def put_logo(
    kind: LogoKind,
    file: UploadFile,
    user: User = Depends(_require_admin),
) -> BrandingSettingsResponse:
    upload_logo(kind, file.file, file.filename)
    _audit(user, f"{kind.value}_upload")
    return branding_response()


@admin_router.delete("/{kind}")
def remove_logo(
    kind: LogoKind,
    user: User = Depends(_require_admin),
) -> BrandingSettingsResponse:
    delete_logo(kind)
    _audit(user, f"{kind.value}_delete")
    return branding_response()
