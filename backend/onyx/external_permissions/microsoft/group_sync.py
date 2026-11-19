"""Group sync for the Microsoft sources.

SharePoint and OneDrive ACLs name Entra groups by id, so their group sync lists
every Entra group with its user members (nested groups expanded). Teams ACLs
name channel groups (one per team for standard channels, one per private or
shared channel), so Teams lists those with their members.
"""

from collections.abc import Callable, Iterator

from sqlalchemy.orm import Session

from onyx.connectors.credentials_provider import build_db_credentials_provider
from onyx.connectors.factory import instantiate_connector
from onyx.connectors.microsoft_utils.entra import (
    EntraDirectoryObject,
    EntraDirectoryObjectType,
    EntraGroup,
    EntraPage,
)
from onyx.connectors.onedrive.source_operations import OneDriveSourceOperations
from onyx.connectors.sharepoint.source_operations import SharepointSourceOperations
from onyx.connectors.teams.connector import TeamsConnector
from onyx.db.external_perm import ExternalUserGroup
from onyx.db.models import ConnectorCredentialPair
from onyx.utils.logger import setup_logger

logger = setup_logger()

GroupPage = Callable[[str | None], EntraPage[EntraGroup]]
MemberPage = Callable[[str, str | None], EntraPage[EntraDirectoryObject]]


def _member_email(member: EntraDirectoryObject) -> str | None:
    email = member.mail or member.user_principal_name
    return email.lower() if email else None


def _is_group(member: EntraDirectoryObject) -> bool:
    return member.odata_type == EntraDirectoryObjectType.GROUP or (
        member.odata_type == EntraDirectoryObjectType.GROUP.value
    )


def _all_pages(fetch: Callable[[str | None], EntraPage]) -> Iterator:
    next_link: str | None = None
    while True:
        page = fetch(next_link)
        yield from page.items
        next_link = page.next_link
        if not next_link:
            return


def iter_entra_groups(
    list_groups: GroupPage,
    list_members: MemberPage,
    *,
    members_are_transitive: bool,
) -> Iterator[ExternalUserGroup]:
    """Every Entra group with the emails of its users. When the member listing
    is direct, nested groups are expanded here (each group is read once, and a
    cycle stops)."""
    direct_cache: dict[str, tuple[set[str], set[str]]] = {}

    def direct(group_id: str) -> tuple[set[str], set[str]]:
        if group_id not in direct_cache:
            users: set[str] = set()
            groups: set[str] = set()
            for member in _all_pages(lambda link: list_members(group_id, link)):
                if _is_group(member):
                    groups.add(member.id)
                elif email := _member_email(member):
                    users.add(email)
            direct_cache[group_id] = (users, groups)
        return direct_cache[group_id]

    def expand(group_id: str) -> set[str]:
        users, nested = direct(group_id)
        if members_are_transitive:
            return set(users)
        seen = {group_id}
        pending = list(nested)
        result = set(users)
        while pending:
            child = pending.pop()
            if child in seen:
                continue
            seen.add(child)
            child_users, child_groups = direct(child)
            result |= child_users
            pending.extend(child_groups - seen)
        return result

    for group in _all_pages(list_groups):
        yield ExternalUserGroup(id=group.id, user_emails=sorted(expand(group.id)))


def _gateway_kwargs(cc_pair: ConnectorCredentialPair) -> dict:
    return {
        "credentials_provider": build_db_credentials_provider(
            cc_pair.connector.source, cc_pair.credential.id
        ),
        "connector_specific_config": cc_pair.connector.connector_specific_config,
    }


def sharepoint_group_sync(
    db_session: Session,  # noqa: ARG001
    cc_pair: ConnectorCredentialPair,
) -> Iterator[ExternalUserGroup]:
    ops = SharepointSourceOperations(**_gateway_kwargs(cc_pair))
    yield from iter_entra_groups(
        lambda link: ops.list_entra_groups(next_link=link),
        lambda group_id, link: ops.list_entra_group_member_page(
            group_id=group_id, next_link=link
        ),
        members_are_transitive=False,
    )


def onedrive_group_sync(
    db_session: Session,  # noqa: ARG001
    cc_pair: ConnectorCredentialPair,
) -> Iterator[ExternalUserGroup]:
    ops = OneDriveSourceOperations(**_gateway_kwargs(cc_pair))
    yield from iter_entra_groups(
        lambda link: ops.list_groups(next_link=link),
        lambda group_id, link: ops.list_transitive_group_members(
            group_id=group_id, next_link=link
        ),
        members_are_transitive=True,
    )


def teams_group_sync(
    db_session: Session, cc_pair: ConnectorCredentialPair
) -> Iterator[ExternalUserGroup]:
    connector = instantiate_connector(
        db_session=db_session,
        source=cc_pair.connector.source,
        input_type=cc_pair.connector.input_type,
        connector_specific_config=cc_pair.connector.connector_specific_config,
        credential=cc_pair.credential,
    )
    if not isinstance(connector, TeamsConnector):
        raise RuntimeError("Bug: a Teams pair built a non-Teams connector")
    for group_id, emails in connector.channel_member_groups():
        yield ExternalUserGroup(id=group_id, user_emails=sorted(set(emails)))
