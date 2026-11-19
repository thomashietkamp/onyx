"""SCIM 2.0 server for IdP provisioning (Entra ID, Okta, ...): Users and Groups
under /scim/v2, and the admin routes that issue the IdP's bearer token.

Users are matched to Onyx users by email; an existing Onyx user is adopted on
first provisioning. Deactivation keeps the user's data. Groups map to Onyx user
groups; the seeded Admin and Basic groups are listed but Onyx owns them, so
writes to them are refused.
"""

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from onyx.auth.permissions import require_permission
from onyx.db.engine.sql_engine import get_session
from onyx.db.enums import AccountType, Permission
from onyx.db.models import ScimToken, ScimUserMapping, User, UserGroup
from onyx.db.scim import (
    create_scim_token__no_commit,
    create_scim_user__no_commit,
    create_user_mapping__no_commit,
    deprovision_scim_user__no_commit,
    fetch_active_scim_token,
    fetch_group_mapping,
    fetch_group_mappings,
    fetch_user_mapping,
    fetch_user_mapping_by_external_id,
    fetch_user_mapping_by_username,
    list_scim_groups,
    list_user_mappings,
    set_group_external_id__no_commit,
)
from onyx.db.user_group import fetch_user_group
from onyx.db.user_group_management import (
    fetch_member_ids,
    insert_user_group__no_commit,
    rename_user_group__no_commit,
    request_user_group_deletion__no_commit,
    set_user_group_members_from_idp__no_commit,
)
from onyx.db.users import (
    DEFAULT_ADMIN_GROUP_NAME,
    DEFAULT_BASIC_GROUP_NAME,
    fetch_user_by_id,
    fetch_users_by_ids,
    get_user_by_email,
    promote_placeholder_to_web_login__no_commit,
    reconcile_user_email__no_commit,
)
from onyx.error_handling.exceptions import OnyxError
from onyx.server.scim.auth import verify_scim_token
from onyx.server.scim.filtering import parse_eq_filter, parse_member_path
from onyx.server.scim.models import (
    ENTERPRISE_USER_SCHEMA,
    ERROR_SCHEMA,
    GROUP_SCHEMA,
    SCIM_MEDIA_TYPE,
    USER_SCHEMA,
    ScimError,
    ScimGroupResource,
    ScimPatchOperation,
    ScimPatchRequest,
    ScimUserResource,
    list_response,
    parse_scim_bool,
)

router = APIRouter(prefix="/scim/v2")
admin_router = APIRouter(prefix="/admin/enterprise-settings/scim")

_MAX_PAGE = 200
_RESERVED_GROUP_NAMES = {
    DEFAULT_ADMIN_GROUP_NAME.lower(),
    DEFAULT_BASIC_GROUP_NAME.lower(),
}
_PLACEHOLDER_ACCOUNTS = (AccountType.EXT_PERM_USER, AccountType.BOT)


def scim_error_response(error: ScimError) -> JSONResponse:
    body: dict[str, Any] = {
        "schemas": [ERROR_SCHEMA],
        "status": str(error.status),
        "detail": error.detail,
    }
    if error.scim_type:
        body["scimType"] = error.scim_type
    return JSONResponse(body, status_code=error.status, media_type=SCIM_MEDIA_TYPE)


def _scim(body: dict[str, Any], status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, media_type=SCIM_MEDIA_TYPE)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _page(start_index: int, count: int) -> tuple[int, int]:
    return max(start_index, 1) - 1, max(0, min(count, _MAX_PAGE))


def _onyx_to_scim(error: OnyxError) -> ScimError:
    status = error.status_code
    # SCIM reports a request naming something that does not exist as invalid.
    if status == 404:
        status = 400
    return ScimError(status, error.detail, "uniqueness" if status == 409 else None)


# --- discovery (no auth: an IdP probes these before it has a token) ----------


@router.get("/ServiceProviderConfig")
def service_provider_config() -> JSONResponse:
    return _scim(
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ServiceProviderConfig"],
            "patch": {"supported": True},
            "bulk": {"supported": False, "maxOperations": 0, "maxPayloadSize": 0},
            "filter": {"supported": True, "maxResults": _MAX_PAGE},
            "changePassword": {"supported": False},
            "sort": {"supported": False},
            "etag": {"supported": False},
            "authenticationSchemes": [
                {
                    "type": "oauthbearertoken",
                    "name": "Bearer token",
                    "description": "The token an Onyx admin generates for the IdP.",
                    "primary": True,
                }
            ],
        }
    )


@router.get("/ResourceTypes")
def resource_types() -> JSONResponse:
    resources = [
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
            "id": "User",
            "name": "User",
            "endpoint": "/Users",
            "schema": USER_SCHEMA,
            "schemaExtensions": [{"schema": ENTERPRISE_USER_SCHEMA, "required": False}],
        },
        {
            "schemas": ["urn:ietf:params:scim:schemas:core:2.0:ResourceType"],
            "id": "Group",
            "name": "Group",
            "endpoint": "/Groups",
            "schema": GROUP_SCHEMA,
        },
    ]
    return _scim(list_response(resources, len(resources), 1))


def _attribute(name: str, value_type: str = "string", **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "type": value_type,
        "multiValued": False,
        "required": False,
        "mutability": "readWrite",
        "returned": "default",
        **extra,
    }


@router.get("/Schemas")
def schemas() -> JSONResponse:
    resources = [
        {
            "id": USER_SCHEMA,
            "name": "User",
            "attributes": [
                _attribute("userName", required=True, uniqueness="server"),
                _attribute("externalId"),
                _attribute("displayName"),
                _attribute("active", "boolean"),
                _attribute("name", "complex"),
                {**_attribute("emails", "complex"), "multiValued": True},
            ],
        },
        {
            "id": GROUP_SCHEMA,
            "name": "Group",
            "attributes": [
                _attribute("displayName", required=True, uniqueness="server"),
                _attribute("externalId"),
                {**_attribute("members", "complex"), "multiValued": True},
            ],
        },
        {
            "id": ENTERPRISE_USER_SCHEMA,
            "name": "EnterpriseUser",
            "attributes": [_attribute("department"), _attribute("manager", "complex")],
        },
    ]
    return _scim(list_response(resources, len(resources), 1))


# --- users -------------------------------------------------------------------


def _user_resource(user: User, mapping: ScimUserMapping) -> dict[str, Any]:
    emails = (
        json.loads(mapping.scim_emails_json)
        if mapping.scim_emails_json
        else [{"value": user.email, "primary": True, "type": "work"}]
    )
    resource: dict[str, Any] = {
        "schemas": [USER_SCHEMA],
        "id": str(user.id),
        "externalId": mapping.external_id,
        "userName": mapping.scim_username or user.email,
        "name": {
            "givenName": mapping.given_name,
            "familyName": mapping.family_name,
            "formatted": user.personal_name,
        },
        "displayName": user.personal_name,
        "emails": emails,
        "active": user.is_active,
        "meta": {
            "resourceType": "User",
            "created": _iso(mapping.created_at),
            "lastModified": _iso(mapping.updated_at),
            "location": f"/scim/v2/Users/{user.id}",
        },
    }
    if mapping.department or mapping.manager:
        resource["schemas"].append(ENTERPRISE_USER_SCHEMA)
        resource[ENTERPRISE_USER_SCHEMA] = {
            "department": mapping.department,
            "manager": {"value": mapping.manager} if mapping.manager else None,
        }
    return resource


def _primary_email(resource: ScimUserResource) -> str:
    primary = next((e.value for e in resource.emails if e.primary), None)
    email = primary or (resource.emails[0].value if resource.emails else None)
    if email is None and "@" in resource.user_name:
        email = resource.user_name
    if not email or "@" not in email:
        raise ScimError(400, "The user needs an email address.", "invalidValue")
    return email.strip().lower()


def _apply_user_resource__no_commit(
    db_session: Session,
    user: User,
    mapping: ScimUserMapping,
    resource: ScimUserResource,
) -> None:
    email = _primary_email(resource)
    if email != user.email.lower():
        other = get_user_by_email(email, db_session)
        if other is not None and other.id != user.id:
            raise ScimError(409, f"{email} belongs to another user.", "uniqueness")
        reconcile_user_email__no_commit(user.id, email, db_session)
    mapping.scim_username = resource.user_name
    mapping.external_id = resource.external_id
    mapping.given_name = resource.name.given_name if resource.name else None
    mapping.family_name = resource.name.family_name if resource.name else None
    mapping.scim_emails_json = (
        json.dumps([e.model_dump(exclude_none=True) for e in resource.emails])
        if resource.emails
        else None
    )
    if resource.enterprise is not None:
        mapping.department = resource.enterprise.department
        mapping.manager = (
            resource.enterprise.manager.value if resource.enterprise.manager else None
        )
    full_name = " ".join(
        part for part in (mapping.given_name, mapping.family_name) if part
    )
    user.personal_name = resource.display_name or full_name or user.personal_name
    user.is_active = resource.active
    db_session.flush()


def _load_mapped_user(
    db_session: Session, user_id: str
) -> tuple[User, ScimUserMapping]:
    try:
        uid = UUID(user_id)
    except ValueError:
        raise ScimError(404, f"User {user_id} not found.") from None
    mapping = fetch_user_mapping(db_session, uid)
    user = fetch_user_by_id(db_session, uid) if mapping else None
    if mapping is None or user is None:
        raise ScimError(404, f"User {user_id} not found.")
    return user, mapping


@router.get("/Users")
def list_users(
    filter: str | None = Query(default=None),
    start_index: int = Query(default=1, alias="startIndex"),
    count: int = Query(default=100),
    _: ScimToken = Depends(verify_scim_token),
    db_session: Session = Depends(get_session),
) -> JSONResponse:
    attribute, value = parse_eq_filter(filter, {"username", "externalid", "id"})
    offset, limit = _page(start_index, count)
    total, mappings = list_user_mappings(
        db_session, attribute=attribute, value=value, offset=offset, limit=limit
    )
    users = {
        u.id: u for u in fetch_users_by_ids(db_session, [m.user_id for m in mappings])
    }
    resources = [
        _user_resource(users[m.user_id], m) for m in mappings if m.user_id in users
    ]
    return _scim(list_response(resources, total, offset + 1))


@router.get("/Users/{user_id}")
def get_user(
    user_id: str,
    _: ScimToken = Depends(verify_scim_token),
    db_session: Session = Depends(get_session),
) -> JSONResponse:
    user, mapping = _load_mapped_user(db_session, user_id)
    return _scim(_user_resource(user, mapping))


@router.post("/Users")
def create_user(
    resource: ScimUserResource,
    _: ScimToken = Depends(verify_scim_token),
    db_session: Session = Depends(get_session),
) -> JSONResponse:
    email = _primary_email(resource)
    if fetch_user_mapping_by_username(db_session, resource.user_name) is not None:
        raise ScimError(409, f"User {resource.user_name} already exists.", "uniqueness")
    if resource.external_id and fetch_user_mapping_by_external_id(
        db_session, resource.external_id
    ):
        raise ScimError(
            409, f"externalId {resource.external_id} is taken.", "uniqueness"
        )

    user = get_user_by_email(email, db_session)
    if user is not None:
        # An existing Onyx account (signed up, or a placeholder from permission
        # sync) is adopted rather than duplicated.
        if fetch_user_mapping(db_session, user.id) is not None:
            raise ScimError(409, f"User {email} already exists.", "uniqueness")
        if user.account_type in _PLACEHOLDER_ACCOUNTS:
            promote_placeholder_to_web_login__no_commit(
                db_session, user, is_verified=True
            )
    else:
        user = create_scim_user__no_commit(db_session, email, resource.active)

    mapping = create_user_mapping__no_commit(db_session, user.id)
    _apply_user_resource__no_commit(db_session, user, mapping, resource)
    db_session.commit()
    db_session.refresh(mapping)
    return _scim(_user_resource(user, mapping), status=201)


@router.put("/Users/{user_id}")
def replace_user(
    user_id: str,
    resource: ScimUserResource,
    _: ScimToken = Depends(verify_scim_token),
    db_session: Session = Depends(get_session),
) -> JSONResponse:
    user, mapping = _load_mapped_user(db_session, user_id)
    _apply_user_resource__no_commit(db_session, user, mapping, resource)
    db_session.commit()
    db_session.refresh(mapping)
    return _scim(_user_resource(user, mapping))


_EMAIL_PATH = 'emails[type eq "work"].value'


def _apply_user_attribute(state: dict[str, Any], path: str, value: Any) -> None:
    key = path.strip()
    lowered = key.lower()
    enterprise_prefix = ENTERPRISE_USER_SCHEMA.lower() + ":"
    if lowered.startswith(enterprise_prefix):
        enterprise = dict(state.get(ENTERPRISE_USER_SCHEMA) or {})
        attribute = key[len(enterprise_prefix) :]
        if attribute.lower() == "manager":
            enterprise["manager"] = (
                value if isinstance(value, dict) else {"value": value}
            )
        else:
            enterprise[attribute] = value
        state[ENTERPRISE_USER_SCHEMA] = enterprise
        return
    if lowered.startswith("name."):
        name = dict(state.get("name") or {})
        name[key.split(".", 1)[1]] = value
        state["name"] = name
        return
    if lowered.replace("'", '"') == _EMAIL_PATH.lower() or lowered == "emails.value":
        emails = [e for e in state.get("emails") or [] if e.get("type") != "work"]
        state["emails"] = [{"value": value, "type": "work", "primary": True}, *emails]
        return
    canonical = {
        "active": "active",
        "username": "userName",
        "displayname": "displayName",
        "externalid": "externalId",
        "emails": "emails",
        "name": "name",
    }.get(lowered)
    if canonical is None:
        raise ScimError(400, f"Unsupported PATCH path: {path}", "invalidPath")
    state[canonical] = parse_scim_bool(value) if canonical == "active" else value


def _apply_user_patch(state: dict[str, Any], operation: ScimPatchOperation) -> None:
    if operation.op == "remove":
        if not operation.path:
            raise ScimError(400, "A remove needs a path.", "noTarget")
        _apply_user_attribute(state, operation.path, None)
        return
    if operation.path:
        _apply_user_attribute(state, operation.path, operation.value)
        return
    if not isinstance(operation.value, dict):
        raise ScimError(
            400, "A PATCH without a path needs an object value.", "invalidValue"
        )
    for path, value in operation.value.items():
        _apply_user_attribute(state, path, value)


@router.patch("/Users/{user_id}")
def patch_user(
    user_id: str,
    request: ScimPatchRequest,
    _: ScimToken = Depends(verify_scim_token),
    db_session: Session = Depends(get_session),
) -> JSONResponse:
    user, mapping = _load_mapped_user(db_session, user_id)
    state = _user_resource(user, mapping)
    for operation in request.operations:
        _apply_user_patch(state, operation)
    try:
        resource = ScimUserResource.model_validate(state)
    except ValueError as e:
        raise ScimError(400, str(e), "invalidValue") from e
    _apply_user_resource__no_commit(db_session, user, mapping, resource)
    db_session.commit()
    db_session.refresh(mapping)
    return _scim(_user_resource(user, mapping))


@router.delete("/Users/{user_id}")
def delete_user(
    user_id: str,
    _: ScimToken = Depends(verify_scim_token),
    db_session: Session = Depends(get_session),
) -> Response:
    """Deprovisioning deactivates the account and drops the IdP link. The user's
    chats and documents stay for the workspace's retention rules."""
    user, mapping = _load_mapped_user(db_session, user_id)
    deprovision_scim_user__no_commit(db_session, user, mapping)
    db_session.commit()
    return Response(status_code=204)


# --- groups ------------------------------------------------------------------


def _group_resource(
    db_session: Session, group: UserGroup, external_id: str | None
) -> dict[str, Any]:
    members = fetch_users_by_ids(
        db_session, list(fetch_member_ids(db_session, group.id))
    )
    return {
        "schemas": [GROUP_SCHEMA],
        "id": str(group.id),
        "displayName": group.name,
        "externalId": external_id,
        "members": [
            {"value": str(user.id), "display": user.email}
            for user in sorted(members, key=lambda u: u.email)
        ],
        "meta": {
            "resourceType": "Group",
            "lastModified": _iso(group.time_last_modified_by_user),
            "location": f"/scim/v2/Groups/{group.id}",
        },
    }


def _load_group(db_session: Session, group_id: str) -> UserGroup:
    group = fetch_user_group(db_session, int(group_id)) if group_id.isdigit() else None
    if group is None or group.is_up_for_deletion:
        raise ScimError(404, f"Group {group_id} not found.")
    return group


def _assert_not_reserved(group: UserGroup | None, new_name: str | None) -> None:
    if group is not None and group.is_default:
        raise ScimError(409, f"'{group.name}' is a reserved Onyx group.", "mutability")
    if new_name is not None and new_name.strip().lower() in _RESERVED_GROUP_NAMES:
        raise ScimError(409, f"'{new_name}' is a reserved group name.", "uniqueness")


def _member_ids(db_session: Session, values: list[str]) -> set[UUID]:
    ids: set[UUID] = set()
    for value in values:
        try:
            ids.add(UUID(value))
        except ValueError:
            raise ScimError(400, f"Member {value} not found.", "invalidValue") from None
    found = {user.id for user in fetch_users_by_ids(db_session, list(ids))}
    if missing := ids - found:
        raise ScimError(
            400,
            f"Member(s) not found: {', '.join(sorted(str(i) for i in missing))}",
            "invalidValue",
        )
    return ids


def _set_members__no_commit(
    db_session: Session, group: UserGroup, member_ids: set[UUID]
) -> None:
    try:
        set_user_group_members_from_idp__no_commit(db_session, group, member_ids)
    except OnyxError as e:
        raise _onyx_to_scim(e) from e


def _rename__no_commit(db_session: Session, group: UserGroup, name: str) -> None:
    if name == group.name:
        return
    _assert_not_reserved(group, name)
    if not group.is_up_to_date:
        # Document ACLs carry the name; a rename waits for the running sync.
        raise ScimError(409, f"Group '{group.name}' is still syncing. Retry later.")
    try:
        rename_user_group__no_commit(db_session, group, name)
    except OnyxError as e:
        raise _onyx_to_scim(e) from e


@router.get("/Groups")
def list_groups(
    filter: str | None = Query(default=None),
    start_index: int = Query(default=1, alias="startIndex"),
    count: int = Query(default=100),
    _: ScimToken = Depends(verify_scim_token),
    db_session: Session = Depends(get_session),
) -> JSONResponse:
    attribute, value = parse_eq_filter(filter, {"displayname", "externalid", "id"})
    offset, limit = _page(start_index, count)
    total, groups = list_scim_groups(
        db_session, attribute=attribute, value=value, offset=offset, limit=limit
    )
    mappings = fetch_group_mappings(db_session, [group.id for group in groups])
    resources = [
        _group_resource(
            db_session,
            group,
            mappings[group.id].external_id if group.id in mappings else None,
        )
        for group in groups
    ]
    return _scim(list_response(resources, total, offset + 1))


@router.get("/Groups/{group_id}")
def get_group(
    group_id: str,
    _: ScimToken = Depends(verify_scim_token),
    db_session: Session = Depends(get_session),
) -> JSONResponse:
    group = _load_group(db_session, group_id)
    mapping = fetch_group_mapping(db_session, group.id)
    return _scim(
        _group_resource(db_session, group, mapping.external_id if mapping else None)
    )


@router.post("/Groups")
def create_group(
    resource: ScimGroupResource,
    _: ScimToken = Depends(verify_scim_token),
    db_session: Session = Depends(get_session),
) -> JSONResponse:
    _assert_not_reserved(None, resource.display_name)
    member_ids = _member_ids(db_session, [m.value for m in resource.members])
    try:
        group = insert_user_group__no_commit(
            db_session, resource.display_name, member_ids, []
        )
    except OnyxError as e:
        raise _onyx_to_scim(e) from e
    set_group_external_id__no_commit(db_session, group.id, resource.external_id)
    db_session.commit()
    return _scim(_group_resource(db_session, group, resource.external_id), status=201)


@router.put("/Groups/{group_id}")
def replace_group(
    group_id: str,
    resource: ScimGroupResource,
    _: ScimToken = Depends(verify_scim_token),
    db_session: Session = Depends(get_session),
) -> JSONResponse:
    group = _load_group(db_session, group_id)
    _assert_not_reserved(group, resource.display_name)
    member_ids = _member_ids(db_session, [m.value for m in resource.members])
    _rename__no_commit(db_session, group, resource.display_name)
    _set_members__no_commit(db_session, group, member_ids)
    set_group_external_id__no_commit(db_session, group.id, resource.external_id)
    db_session.commit()
    return _scim(_group_resource(db_session, group, resource.external_id))


def _member_values(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    values: list[str] = []
    for item in items:
        if isinstance(item, dict) and "value" in item:
            values.append(str(item["value"]))
        elif isinstance(item, str):
            values.append(item)
        else:
            raise ScimError(400, "Members are objects with a value.", "invalidValue")
    return values


@router.patch("/Groups/{group_id}")
def patch_group(
    group_id: str,
    request: ScimPatchRequest,
    _: ScimToken = Depends(verify_scim_token),
    db_session: Session = Depends(get_session),
) -> JSONResponse:
    group = _load_group(db_session, group_id)
    mapping = fetch_group_mapping(db_session, group.id)
    external_id = mapping.external_id if mapping else None
    name = group.name
    members = fetch_member_ids(db_session, group.id)

    for operation in request.operations:
        path = (operation.path or "").strip()
        lowered = path.lower()
        if not path and isinstance(operation.value, dict):
            for key, value in operation.value.items():
                if key.lower() == "displayname":
                    name = str(value)
                elif key.lower() == "externalid":
                    external_id = str(value) if value is not None else None
                elif key.lower() == "members":
                    members = _member_ids(db_session, _member_values(value))
                else:
                    raise ScimError(400, f"Unsupported attribute: {key}", "invalidPath")
        elif lowered == "displayname":
            name = str(operation.value)
        elif lowered == "externalid":
            external_id = None if operation.op == "remove" else str(operation.value)
        elif lowered == "members":
            values = _member_ids(db_session, _member_values(operation.value))
            if operation.op == "add":
                members |= values
            elif operation.op == "replace":
                members = values
            else:
                # A remove with no filter and no value removes every member.
                members = members - values if operation.value else set()
        elif (member := parse_member_path(path)) is not None:
            if operation.op != "remove":
                raise ScimError(400, f"Unsupported op on {path}.", "invalidPath")
            members.discard(_member_ids(db_session, [member]).pop())
        else:
            raise ScimError(400, f"Unsupported PATCH path: {path}", "invalidPath")

    if group.is_default:
        _assert_not_reserved(group, None)
    _rename__no_commit(db_session, group, name)
    _set_members__no_commit(db_session, group, members)
    set_group_external_id__no_commit(db_session, group.id, external_id)
    db_session.commit()
    return _scim(_group_resource(db_session, group, external_id))


@router.delete("/Groups/{group_id}")
def delete_group(
    group_id: str,
    _: ScimToken = Depends(verify_scim_token),
    db_session: Session = Depends(get_session),
) -> Response:
    group = _load_group(db_session, group_id)
    _assert_not_reserved(group, None)
    request_user_group_deletion__no_commit(db_session, group, None)
    db_session.commit()
    return Response(status_code=204)


# --- admin: the IdP's token --------------------------------------------------


class ScimTokenResponse(BaseModel):
    id: int
    name: str
    token_display: str
    is_active: bool
    created_at: datetime
    last_used_at: datetime | None
    idp_domain: str | None = None

    @classmethod
    def from_model(cls, token: ScimToken) -> "ScimTokenResponse":
        return cls(
            id=token.id,
            name=token.name,
            token_display=token.token_display,
            is_active=token.is_active,
            created_at=token.created_at,
            last_used_at=token.last_used_at,
        )


class ScimTokenCreatedResponse(ScimTokenResponse):
    raw_token: str


class ScimTokenCreateRequest(BaseModel):
    name: str


_full_admin = require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)


@admin_router.get("/token")
def get_scim_token(
    _: User = Depends(_full_admin),
    db_session: Session = Depends(get_session),
) -> ScimTokenResponse | None:
    token = fetch_active_scim_token(db_session)
    return ScimTokenResponse.from_model(token) if token else None


@admin_router.post("/token")
def create_scim_token(
    request: ScimTokenCreateRequest,
    user: User = Depends(_full_admin),
    db_session: Session = Depends(get_session),
) -> ScimTokenCreatedResponse:
    """Issues a new token and revokes the old one. The raw token is shown once."""
    token, raw_token = create_scim_token__no_commit(db_session, request.name, user)
    db_session.commit()
    db_session.refresh(token)
    return ScimTokenCreatedResponse(
        **ScimTokenResponse.from_model(token).model_dump(), raw_token=raw_token
    )


def register_scim_exception_handler(app: Any) -> None:
    async def handle(_: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, ScimError):
            raise error
        return scim_error_response(error)

    app.add_exception_handler(ScimError, handle)
