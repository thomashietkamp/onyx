"""Group sync for Azure Data Lake: every Entra group of the tenant with the
emails of its users, nested groups included. Role assignments and ACL entries
name these groups by object id (see ``access.py``)."""

from collections.abc import Iterator

from sqlalchemy.orm import Session

from onyx.connectors.azure_datalake.connector import AzureDataLakeConnector
from onyx.connectors.azure_datalake.source_operations import (
    AzureDataLakeSourceOperations,
)
from onyx.connectors.factory import instantiate_connector
from onyx.connectors.microsoft_utils.entra import (
    EntraDirectoryObject,
    EntraDirectoryObjectType,
    iter_entra_items,
)
from onyx.db.external_perm import ExternalUserGroup
from onyx.db.models import ConnectorCredentialPair


def _member_email(member: EntraDirectoryObject) -> str | None:
    if member.odata_type not in (
        EntraDirectoryObjectType.USER,
        EntraDirectoryObjectType.USER.value,
    ):
        return None
    email = member.mail or member.user_principal_name
    return email.strip().lower() if email and "@" in email else None


def iter_azure_datalake_groups(
    ops: AzureDataLakeSourceOperations,
) -> Iterator[ExternalUserGroup]:
    for group in iter_entra_items(
        lambda link: ops.list_groups(next_link=link), "Entra groups"
    ):
        emails: set[str] = {
            email
            for member in iter_entra_items(
                lambda link, group_id=group.id: ops.list_transitive_group_members(
                    group_id=group_id, next_link=link
                ),
                f"members of Entra group {group.id}",
            )
            if (email := _member_email(member))
        }
        yield ExternalUserGroup(id=group.id.lower(), user_emails=sorted(emails))


def azure_datalake_group_sync(
    db_session: Session, cc_pair: ConnectorCredentialPair
) -> Iterator[ExternalUserGroup]:
    connector = instantiate_connector(
        db_session=db_session,
        source=cc_pair.connector.source,
        input_type=cc_pair.connector.input_type,
        connector_specific_config=cc_pair.connector.connector_specific_config,
        credential=cc_pair.credential,
    )
    if not isinstance(connector, AzureDataLakeConnector):
        raise RuntimeError("Bug: an Azure Data Lake pair built another connector")
    yield from iter_azure_datalake_groups(connector.ops)
