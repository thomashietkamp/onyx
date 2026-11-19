"""SCIM 2.0 messages (RFC 7643, RFC 7644).

IdPs differ in small ways: Entra ID capitalizes PATCH ops ("Replace"), sends
booleans as strings ("False"), and patches without a path by sending a value
object. These models accept both forms and normalize them.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
ENTERPRISE_USER_SCHEMA = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"
LIST_RESPONSE_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_OP_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"
ERROR_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:Error"

SCIM_MEDIA_TYPE = "application/scim+json"


class ScimError(Exception):
    """Raised by SCIM handlers; rendered as a SCIM Error message."""

    def __init__(self, status: int, detail: str, scim_type: str | None = None):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.scim_type = scim_type


class _Lenient(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class ScimName(_Lenient):
    given_name: str | None = Field(default=None, alias="givenName")
    family_name: str | None = Field(default=None, alias="familyName")
    formatted: str | None = None


class ScimEmail(_Lenient):
    value: str
    type: str | None = None
    primary: bool | None = None


class ScimManagerRef(_Lenient):
    value: str | None = None


class ScimEnterpriseUser(_Lenient):
    department: str | None = None
    manager: ScimManagerRef | None = None


class ScimUserResource(_Lenient):
    """A User as the IdP sends it (POST/PUT)."""

    user_name: str = Field(alias="userName")
    external_id: str | None = Field(default=None, alias="externalId")
    name: ScimName | None = None
    display_name: str | None = Field(default=None, alias="displayName")
    emails: list[ScimEmail] = Field(default_factory=list)
    active: bool = True
    enterprise: ScimEnterpriseUser | None = Field(
        default=None, alias=ENTERPRISE_USER_SCHEMA
    )

    @field_validator("active", mode="before")
    @classmethod
    def _bool_from_string(cls, value: Any) -> Any:
        return parse_scim_bool(value)


class ScimMember(_Lenient):
    value: str
    display: str | None = None


class ScimGroupResource(_Lenient):
    """A Group as the IdP sends it (POST/PUT)."""

    display_name: str = Field(alias="displayName")
    external_id: str | None = Field(default=None, alias="externalId")
    members: list[ScimMember] = Field(default_factory=list)


class ScimPatchOperation(_Lenient):
    op: str
    path: str | None = None
    value: Any = None

    @field_validator("op")
    @classmethod
    def _normalize_op(cls, value: str) -> str:
        op = value.strip().lower()
        if op not in ("add", "remove", "replace"):
            raise ValueError(f"Unsupported PATCH op: {value}")
        return op


class ScimPatchRequest(_Lenient):
    operations: list[ScimPatchOperation] = Field(alias="Operations")


def parse_scim_bool(value: Any) -> Any:
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return value


def list_response(
    resources: list[dict[str, Any]], total: int, start_index: int
) -> dict[str, Any]:
    return {
        "schemas": [LIST_RESPONSE_SCHEMA],
        "totalResults": total,
        "startIndex": start_index,
        "itemsPerPage": len(resources),
        "Resources": resources,
    }
