"""Group sync for Egnyte: every custom group with the emails of its active
members, and each default group with the active users of its user types. The
ids match the ones ``access.py:folder_access`` puts on folders."""

from collections.abc import Iterator

from sqlalchemy.orm import Session

from onyx.connectors.egnyte.connector import EgnyteConnector
from onyx.connectors.egnyte.models import EgnyteUser
from onyx.connectors.egnyte.source_operations import EgnyteSourceOperations
from onyx.connectors.factory import instantiate_connector
from onyx.db.external_perm import ExternalUserGroup
from onyx.db.models import ConnectorCredentialPair
from onyx.external_permissions.egnyte.access import (
    DEFAULT_GROUP_USER_TYPES,
    active_user_emails,
    domain_group_id,
    normalize_username,
)


def iter_egnyte_groups(
    ops: EgnyteSourceOperations, domain: str
) -> Iterator[ExternalUserGroup]:
    users: list[EgnyteUser] = ops.list_users()
    emails_by_username = active_user_emails(users)
    for group_id, user_types in DEFAULT_GROUP_USER_TYPES.items():
        yield ExternalUserGroup(
            id=domain_group_id(domain, group_id),
            user_emails=sorted(
                {
                    emails_by_username[normalize_username(user.user_name)]
                    for user in users
                    if user.user_type in user_types
                    and normalize_username(user.user_name) in emails_by_username
                }
            ),
        )
    for ref in ops.list_groups():
        group = ops.get_group(group_id=ref.id)
        yield ExternalUserGroup(
            id=domain_group_id(domain, group.id),
            user_emails=sorted(
                {
                    email
                    for member in group.members
                    if (
                        email := emails_by_username.get(
                            normalize_username(member.username)
                        )
                    )
                }
            ),
        )


def egnyte_group_sync(
    db_session: Session, cc_pair: ConnectorCredentialPair
) -> Iterator[ExternalUserGroup]:
    connector = instantiate_connector(
        db_session=db_session,
        source=cc_pair.connector.source,
        input_type=cc_pair.connector.input_type,
        connector_specific_config=cc_pair.connector.connector_specific_config,
        credential=cc_pair.credential,
    )
    if not isinstance(connector, EgnyteConnector):
        raise RuntimeError("Bug: an Egnyte pair built a non-Egnyte connector")
    yield from iter_egnyte_groups(connector.ops, connector.domain)
