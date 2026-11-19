"""The license and billing status this fork reports. The fork has no license
and no subscription: it runs at a fixed tier (``onyx/utils/tier.py``). The
admin pages still ask for both, so these routes answer instead of a 404."""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from onyx.auth.permissions import require_permission
from onyx.db.enums import Permission
from onyx.db.models import User

router = APIRouter()

_admin = require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)


class LicenseStatusResponse(BaseModel):
    has_license: bool = False
    seats: int = 0
    used_seats: int = 0
    plan_type: str | None = None
    issued_at: datetime | None = None
    expires_at: datetime | None = None
    grace_period_end: datetime | None = None
    status: str | None = None
    expiry_warning_stage: str = "none"
    source: str | None = None
    trial_end: datetime | None = None


class SubscriptionStatusResponse(BaseModel):
    subscribed: bool = False


@router.get("/license")
def get_license_status(_: User = Depends(_admin)) -> LicenseStatusResponse:
    return LicenseStatusResponse()


@router.get("/admin/billing/billing-information")
def get_billing_information(
    _: User = Depends(_admin),
) -> SubscriptionStatusResponse:
    return SubscriptionStatusResponse()
