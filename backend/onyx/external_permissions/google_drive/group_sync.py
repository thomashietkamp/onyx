"""Group sync for Google Drive.

Drive ACLs name Google groups by email and domain-wide shares by
``build_domain_group_id(domain)`` (see ``doc_sync.py``). This sync reads both
from the Admin SDK Directory API as the primary admin: every group of the
Workspace domain with its users (nested groups expanded), and one group per
domain with every user of the domain.

A group whose members (or a nested group's members) cannot be read is skipped
whole, never yielded with part of its members. When more than half of the
groups cannot be read, the sync fails, so the stale sweep does not remove most
groups after a broken credential.
"""

from collections.abc import Callable, Iterable, Iterator

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from onyx.access.utils import build_domain_group_id
from onyx.connectors.factory import instantiate_connector
from onyx.connectors.google_drive.connector import GoogleDriveConnector
from onyx.connectors.google_drive.models import GoogleGroupMember
from onyx.connectors.google_drive.source_operations import (
    GoogleDriveHttpError,
    GoogleDriveRefreshError,
)
from onyx.db.external_perm import ExternalUserGroup
from onyx.db.models import ConnectorCredentialPair
from onyx.utils.logger import setup_logger

logger = setup_logger()

# Directory API member types.
_MEMBER_TYPE_USER = "USER"
_MEMBER_TYPE_GROUP = "GROUP"
# Every user of the customer's account.
_MEMBER_TYPE_CUSTOMER = "CUSTOMER"


class _GroupUnreadableError(Exception):
    def __init__(self, group_email: str) -> None:
        super().__init__(f"Cannot read the members of {group_email}")
        self.group_email = group_email


class _DirectMembers(BaseModel):
    """The direct users and nested groups of one group."""

    users: set[str] = Field(default_factory=set)
    groups: set[str] = Field(default_factory=set)
    includes_whole_domain: bool = False


def iter_google_groups(
    group_emails: Iterable[str],
    list_members: Callable[[str], Iterable[GoogleGroupMember]],
    list_domain_users: Callable[[], Iterable[str]],
    domain: str,
) -> Iterator[ExternalUserGroup]:
    """The domain group, then every group with the emails of its users."""
    domain_users = sorted({email.lower() for email in list_domain_users()})
    yield ExternalUserGroup(id=build_domain_group_id(domain), user_emails=domain_users)

    direct_cache: dict[str, _DirectMembers] = {}
    unreadable: set[str] = set()

    def direct(group_email: str) -> _DirectMembers:
        if group_email in unreadable:
            raise _GroupUnreadableError(group_email)
        cached = direct_cache.get(group_email)
        if cached is not None:
            return cached
        members = _DirectMembers()
        try:
            for member in list_members(group_email):
                email = member.email.lower() if member.email else None
                if member.type == _MEMBER_TYPE_CUSTOMER:
                    members.includes_whole_domain = True
                elif not email:
                    continue
                elif member.type == _MEMBER_TYPE_GROUP:
                    members.groups.add(email)
                elif member.type == _MEMBER_TYPE_USER:
                    members.users.add(email)
        except (GoogleDriveHttpError, GoogleDriveRefreshError) as e:
            if isinstance(e, GoogleDriveHttpError) and e.transient:
                raise
            logger.warning("Cannot read the members of group %s: %s", group_email, e)
            unreadable.add(group_email)
            raise _GroupUnreadableError(group_email) from e
        direct_cache[group_email] = members
        return members

    def expand(group_email: str) -> set[str]:
        result: set[str] = set()
        seen: set[str] = set()
        pending = [group_email]
        while pending:
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            members = direct(current)
            result |= members.users
            if members.includes_whole_domain:
                result.update(domain_users)
            pending.extend(members.groups - seen)
        return result

    total = 0
    failed = 0
    for raw_email in group_emails:
        group_email = raw_email.lower()
        total += 1
        try:
            emails = expand(group_email)
        except _GroupUnreadableError:
            failed += 1
            continue
        yield ExternalUserGroup(id=group_email, user_emails=sorted(emails))

    if failed and failed * 2 > total:
        raise RuntimeError(
            f"Could not read the members of {failed} of {total} Google groups"
        )


def gdrive_group_sync(
    db_session: Session, cc_pair: ConnectorCredentialPair
) -> Iterator[ExternalUserGroup]:
    connector = instantiate_connector(
        db_session=db_session,
        source=cc_pair.connector.source,
        input_type=cc_pair.connector.input_type,
        connector_specific_config=cc_pair.connector.connector_specific_config,
        credential=cc_pair.credential,
    )
    if not isinstance(connector, GoogleDriveConnector):
        raise RuntimeError("Bug: a Google Drive pair built a non-Drive connector")
    ops = connector.ops
    yield from iter_google_groups(
        group_emails=ops.list_groups(),
        list_members=lambda group_email: ops.list_group_members(
            group_email=group_email
        ),
        list_domain_users=ops.list_user_emails,
        domain=connector.google_domain,
    )
