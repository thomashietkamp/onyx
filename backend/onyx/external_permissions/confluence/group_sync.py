"""Group sync for Confluence.

Restrictions and space permissions name Confluence groups by name, and both
sides scope the name to the site (``users.confluence_group_id``). Confluence
lists the groups of a user (``rest/api/user/memberof``), not the members of a
group for every deployment, so this sync lists the site's users, reads each
user's groups, and inverts the result.

A user whose groups cannot be read is skipped. When more than half of the
users cannot be read, the sync fails, so the stale sweep does not remove most
memberships after a broken credential.

``CONFLUENCE_USE_ONYX_USERS_FOR_GROUP_SYNC`` (Data Center only) works around
a user list that misses users (CONFSERVER-95999): the sync looks up each Onyx
user by username (the full email, then its local part) instead of listing
users. The failure cap does not apply then, because many Onyx users can have
no Confluence account.
"""

from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator

from pydantic import BaseModel
from requests import HTTPError
from sqlalchemy.orm import Session

from onyx.configs.app_configs import CONFLUENCE_USE_ONYX_USERS_FOR_GROUP_SYNC
from onyx.connectors.confluence.connector import ConfluenceConnector
from onyx.connectors.confluence.models import ConfluenceUser
from onyx.connectors.confluence.source_operations import (
    ConfluenceSourceOperations,
    bound_site_url,
    user_list_variant,
)
from onyx.connectors.factory import instantiate_connector
from onyx.db.external_perm import ExternalUserGroup
from onyx.db.models import ConnectorCredentialPair
from onyx.db.users import get_all_users
from onyx.external_permissions.confluence.users import (
    confluence_group_id,
    is_cloud_gateway,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()

_APP_USER_TYPE = "app"


class ConfluenceGroupMember(BaseModel):
    """A user to read groups for: the id ``memberof`` takes (accountId on
    Cloud, userKey on Data Center) and the user's email."""

    user_id: str
    email: str


def iter_groups_from_members(
    members: Iterable[ConfluenceGroupMember],
    list_user_groups: Callable[[str], Iterable[str]],
    *,
    site_url: str,
    enforce_failure_cap: bool,
) -> Iterator[ExternalUserGroup]:
    group_to_emails: dict[str, set[str]] = defaultdict(set)
    total = 0
    failed = 0
    for member in members:
        total += 1
        try:
            group_names = list(list_user_groups(member.user_id))
        except HTTPError as e:
            failed += 1
            logger.warning(
                "Cannot read the Confluence groups of %s: %s", member.email, e
            )
            continue
        for name in group_names:
            group_to_emails[name].add(member.email)

    if enforce_failure_cap and failed and failed * 2 > total:
        raise RuntimeError(
            f"Could not read the groups of {failed} of {total} Confluence users"
        )
    for name, emails in group_to_emails.items():
        yield ExternalUserGroup(
            id=confluence_group_id(site_url, name), user_emails=sorted(emails)
        )


def _user_groups(ops: ConfluenceSourceOperations, user_id: str) -> Iterator[str]:
    for group in ops.list_user_groups(user_id=user_id):
        name = group.get("name")
        if isinstance(name, str) and name:
            yield name


def _listed_members(
    ops: ConfluenceSourceOperations,
) -> Iterator[ConfluenceGroupMember]:
    is_cloud = is_cloud_gateway(ops)
    for user in ops.list_users(variant=user_list_variant(ops)):
        if user.type == _APP_USER_TYPE:
            continue
        email = _email_of(ops, user, is_cloud=is_cloud)
        if email:
            yield ConfluenceGroupMember(user_id=user.user_id, email=email)


def _email_of(
    ops: ConfluenceSourceOperations, user: ConfluenceUser, *, is_cloud: bool
) -> str | None:
    if user.email:
        return user.email.lower()
    if is_cloud or not user.username:
        return None
    email = ops.get_listed_user_email(username=user.username)
    return email.lower() if email else None


def _onyx_user_members(
    ops: ConfluenceSourceOperations, onyx_emails: Iterable[str]
) -> Iterator[ConfluenceGroupMember]:
    for email in onyx_emails:
        lowered = email.lower()
        for username in dict.fromkeys((lowered, lowered.split("@", 1)[0])):
            try:
                details = ops.get_user_by_username(username=username)
            except Exception as e:
                logger.debug("No Confluence user %s: %s", username, e)
                continue
            user_key = details.get("userKey")
            if not isinstance(user_key, str) or not user_key:
                continue
            listed_email = ops.get_listed_user_email(username=username)
            if listed_email and listed_email.lower() != lowered:
                # Another person with the same local part.
                continue
            yield ConfluenceGroupMember(user_id=user_key, email=lowered)
            break


def confluence_group_sync(
    db_session: Session, cc_pair: ConnectorCredentialPair
) -> Iterator[ExternalUserGroup]:
    connector = instantiate_confluence_connector(db_session, cc_pair)
    ops = connector.source_operations
    use_onyx_users = CONFLUENCE_USE_ONYX_USERS_FOR_GROUP_SYNC and not is_cloud_gateway(
        ops
    )
    members: Iterable[ConfluenceGroupMember]
    if use_onyx_users:
        onyx_emails = [user.email for user in get_all_users(db_session)]
        members = _onyx_user_members(ops, onyx_emails)
    else:
        members = _listed_members(ops)
    yield from iter_groups_from_members(
        members,
        lambda user_id: _user_groups(ops, user_id),
        site_url=bound_site_url(ops),
        enforce_failure_cap=not use_onyx_users,
    )


def instantiate_confluence_connector(
    db_session: Session, cc_pair: ConnectorCredentialPair
) -> ConfluenceConnector:
    connector = instantiate_connector(
        db_session=db_session,
        source=cc_pair.connector.source,
        input_type=cc_pair.connector.input_type,
        connector_specific_config=cc_pair.connector.connector_specific_config,
        credential=cc_pair.credential,
    )
    if not isinstance(connector, ConfluenceConnector):
        raise RuntimeError("Bug: a Confluence pair built a non-Confluence connector")
    return connector
