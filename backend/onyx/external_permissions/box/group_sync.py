"""Group sync for Box: every enterprise group with its members' logins, and the
enterprise's all-users group, which company shared links and all-users
collaborations name. Needs the app's 'Manage groups' and 'Manage users'
scopes."""

from collections.abc import Iterator

from box_sdk_gen import BoxClient
from sqlalchemy.orm import Session

from onyx.connectors.box.connector import (
    BoxConnector,
    box_all_enterprise_users_group_id,
    box_group_id,
    iter_box_enterprise_users,
    normalize_box_login,
)
from onyx.connectors.factory import instantiate_connector
from onyx.db.external_perm import ExternalUserGroup
from onyx.db.models import ConnectorCredentialPair

_PAGE_SIZE = 1000
_MAX_PAGES = 10_000


def _iter_group_ids(client: BoxClient) -> Iterator[str]:
    offset = 0
    for _ in range(_MAX_PAGES):
        page = client.groups.get_groups(limit=_PAGE_SIZE, offset=offset)
        entries = page.entries or []
        for group in entries:
            yield group.id
        offset += len(entries)
        if not entries or page.total_count is None or offset >= page.total_count:
            return
    raise RuntimeError("Box group pagination did not terminate")


def _group_member_logins(client: BoxClient, group_id: str) -> set[str]:
    logins: set[str] = set()
    offset = 0
    for _ in range(_MAX_PAGES):
        page = client.memberships.get_group_memberships(
            group_id, limit=_PAGE_SIZE, offset=offset
        )
        entries = page.entries or []
        for membership in entries:
            if membership.user is not None and membership.user.login:
                logins.add(normalize_box_login(membership.user.login))
        offset += len(entries)
        if not entries or page.total_count is None or offset >= page.total_count:
            return logins
    raise RuntimeError(
        f"Box membership pagination of group {group_id} did not terminate"
    )


def iter_box_groups(
    client: BoxClient, enterprise_id: str
) -> Iterator[ExternalUserGroup]:
    for group_id in _iter_group_ids(client):
        yield ExternalUserGroup(
            id=box_group_id(group_id),
            user_emails=sorted(_group_member_logins(client, group_id)),
        )
    yield ExternalUserGroup(
        id=box_all_enterprise_users_group_id(enterprise_id),
        user_emails=sorted(
            {
                normalize_box_login(user.login)
                for user in iter_box_enterprise_users(client)
                if user.login
            }
        ),
    )


def box_group_sync(
    db_session: Session, cc_pair: ConnectorCredentialPair
) -> Iterator[ExternalUserGroup]:
    connector = instantiate_connector(
        db_session=db_session,
        source=cc_pair.connector.source,
        input_type=cc_pair.connector.input_type,
        connector_specific_config=cc_pair.connector.connector_specific_config,
        credential=cc_pair.credential,
    )
    if not isinstance(connector, BoxConnector):
        raise RuntimeError("Bug: a Box pair built a non-Box connector")
    yield from iter_box_groups(connector.enterprise_client, connector.enterprise_id)
