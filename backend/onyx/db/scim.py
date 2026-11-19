"""SCIM provisioning storage: bearer tokens, and the mappings from the IdP's
users and groups to Onyx users and groups."""

import hashlib
import secrets
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from onyx.db.enums import AccountType
from onyx.db.models import ScimGroupMapping, ScimToken, ScimUserMapping, User, UserGroup
from onyx.db.users import (
    _generate_password_hash,
    assign_user_to_default_groups__no_commit,
)

SCIM_TOKEN_PREFIX = "onyx_scim_"


def hash_scim_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode()).hexdigest()


def create_scim_token__no_commit(
    db_session: Session, name: str, created_by: User
) -> tuple[ScimToken, str]:
    """Creates a token and revokes every earlier one: an IdP holds one token at
    a time, so rotation must cut the old one off. Returns (row, raw token); the
    raw token is shown once and only its hash is stored."""
    db_session.execute(
        update(ScimToken).where(ScimToken.is_active.is_(True)).values(is_active=False)
    )
    raw_token = SCIM_TOKEN_PREFIX + secrets.token_urlsafe(32)
    token = ScimToken(
        name=name,
        hashed_token=hash_scim_token(raw_token),
        token_display=f"{SCIM_TOKEN_PREFIX}****{raw_token[-4:]}",
        created_by_id=created_by.id,
        is_active=True,
    )
    db_session.add(token)
    db_session.flush()
    return token, raw_token


def fetch_active_scim_token(db_session: Session) -> ScimToken | None:
    return db_session.scalar(
        select(ScimToken)
        .where(ScimToken.is_active.is_(True))
        .order_by(ScimToken.created_at.desc())
        .limit(1)
    )


def fetch_scim_token_by_raw(db_session: Session, raw_token: str) -> ScimToken | None:
    return db_session.scalar(
        select(ScimToken).where(
            ScimToken.hashed_token == hash_scim_token(raw_token),
            ScimToken.is_active.is_(True),
        )
    )


def touch_scim_token__no_commit(db_session: Session, token: ScimToken) -> None:
    token.last_used_at = datetime.now(UTC)
    db_session.flush()


# --- users -------------------------------------------------------------------


def create_scim_user__no_commit(db_session: Session, email: str, active: bool) -> User:
    """A new standard account for an IdP user. It has a random password (sign-in
    is through SSO) and joins the default group like any new user."""
    user = User(
        email=email,
        hashed_password=_generate_password_hash(),
        account_type=AccountType.STANDARD,
        is_verified=True,
        is_active=active,
    )
    db_session.add(user)
    db_session.flush()
    assign_user_to_default_groups__no_commit(db_session, user)
    return user


def create_user_mapping__no_commit(
    db_session: Session, user_id: UUID
) -> ScimUserMapping:
    mapping = ScimUserMapping(user_id=user_id)
    db_session.add(mapping)
    db_session.flush()
    return mapping


def deprovision_scim_user__no_commit(
    db_session: Session, user: User, mapping: ScimUserMapping
) -> None:
    """Deactivates the account and drops the IdP link; the user's data stays."""
    user.is_active = False
    db_session.delete(mapping)
    db_session.flush()


def fetch_user_mapping(db_session: Session, user_id: UUID) -> ScimUserMapping | None:
    return db_session.scalar(
        select(ScimUserMapping).where(ScimUserMapping.user_id == user_id)
    )


def fetch_user_mapping_by_external_id(
    db_session: Session, external_id: str
) -> ScimUserMapping | None:
    return db_session.scalar(
        select(ScimUserMapping).where(ScimUserMapping.external_id == external_id)
    )


def fetch_user_mapping_by_username(
    db_session: Session, username: str
) -> ScimUserMapping | None:
    return db_session.scalar(
        select(ScimUserMapping).where(
            func.lower(ScimUserMapping.scim_username) == username.lower()
        )
    )


def list_user_mappings(
    db_session: Session,
    *,
    attribute: str | None,
    value: str | None,
    offset: int,
    limit: int,
) -> tuple[int, Sequence[ScimUserMapping]]:
    """Mapped users, optionally filtered by ``userName``, ``externalId`` or
    ``id`` equality. Returns (total, page)."""
    stmt = select(ScimUserMapping).join(User, User.id == ScimUserMapping.user_id)  # ty: ignore[invalid-argument-type]
    if attribute == "username" and value is not None:
        stmt = stmt.where(func.lower(ScimUserMapping.scim_username) == value.lower())
    elif attribute == "externalid" and value is not None:
        stmt = stmt.where(ScimUserMapping.external_id == value)
    elif attribute == "id" and value is not None:
        try:
            stmt = stmt.where(ScimUserMapping.user_id == UUID(value))
        except ValueError:
            return 0, []
    total = db_session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    page = db_session.scalars(
        stmt.order_by(ScimUserMapping.id).offset(offset).limit(limit)
    ).all()
    return int(total), page


# --- groups ------------------------------------------------------------------


def fetch_group_mapping(db_session: Session, group_id: int) -> ScimGroupMapping | None:
    return db_session.scalar(
        select(ScimGroupMapping).where(ScimGroupMapping.user_group_id == group_id)
    )


def fetch_group_mappings(
    db_session: Session, group_ids: Sequence[int]
) -> dict[int, ScimGroupMapping]:
    if not group_ids:
        return {}
    return {
        mapping.user_group_id: mapping
        for mapping in db_session.scalars(
            select(ScimGroupMapping).where(
                ScimGroupMapping.user_group_id.in_(group_ids)
            )
        ).all()
    }


def set_group_external_id__no_commit(
    db_session: Session, group_id: int, external_id: str | None
) -> None:
    mapping = fetch_group_mapping(db_session, group_id)
    if external_id is None:
        if mapping is not None:
            db_session.delete(mapping)
    elif mapping is None:
        db_session.add(
            ScimGroupMapping(external_id=external_id, user_group_id=group_id)
        )
    else:
        mapping.external_id = external_id
    db_session.flush()


def list_scim_groups(
    db_session: Session,
    *,
    attribute: str | None,
    value: str | None,
    offset: int,
    limit: int,
) -> tuple[int, Sequence[UserGroup]]:
    """Groups that are not up for deletion, optionally filtered by
    ``displayName``, ``externalId`` or ``id`` equality."""
    stmt = select(UserGroup).where(UserGroup.is_up_for_deletion.is_(False))
    if attribute == "displayname" and value is not None:
        stmt = stmt.where(func.lower(UserGroup.name) == value.lower())
    elif attribute == "externalid" and value is not None:
        stmt = stmt.join(
            ScimGroupMapping, ScimGroupMapping.user_group_id == UserGroup.id
        ).where(ScimGroupMapping.external_id == value)
    elif attribute == "id" and value is not None:
        if not value.isdigit():
            return 0, []
        stmt = stmt.where(UserGroup.id == int(value))
    total = db_session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    page = (
        db_session.scalars(stmt.order_by(UserGroup.id).offset(offset).limit(limit))
        .unique()
        .all()
    )
    return int(total), page
