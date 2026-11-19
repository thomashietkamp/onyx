"""Group sync for Zoom.

A recording shared under a domain sign-in rule names the group
``build_domain_group_id(domain)``. Its members are the account's active users
whose email is at that domain. Zoom also admits signed-in people from outside
the account, but it cannot list them, so they get no access in Onyx.
"""

from collections import defaultdict
from collections.abc import Callable, Iterator

from sqlalchemy.orm import Session

from onyx.access.utils import build_domain_group_id
from onyx.connectors.factory import instantiate_connector
from onyx.connectors.models import ConnectorMissingCredentialError
from onyx.connectors.zoom.client import MAX_LISTING_PAGES
from onyx.connectors.zoom.connector import ZoomConnector
from onyx.connectors.zoom.models import ZoomUserPage
from onyx.db.external_perm import ExternalUserGroup
from onyx.db.models import ConnectorCredentialPair
from onyx.utils.logger import setup_logger

logger = setup_logger()

ListUsers = Callable[[str | None], ZoomUserPage]


def _domain_of(email: str) -> str | None:
    local, at, domain = email.strip().lower().rpartition("@")
    if not at or not local or not domain:
        return None
    return domain


def iter_zoom_domain_groups(list_users: ListUsers) -> Iterator[ExternalUserGroup]:
    """One group per email domain of the account's users. The whole listing is
    read before any group is yielded, so a failed page yields nothing and the
    last cycle's memberships stay."""
    members: dict[str, set[str]] = defaultdict(set)
    listed = 0
    total: int | None = None
    page_token: str | None = None
    for _ in range(MAX_LISTING_PAGES):
        page = list_users(page_token)
        if total is None:
            total = page.total_records
        for user in page.users:
            listed += 1
            # Zoom withholds the id until a user accepts the invitation, and
            # such a user cannot sign in yet.
            if not user.id:
                continue
            domain = _domain_of(user.email)
            if domain is None:
                continue
            members[domain].add(user.email.strip().lower())
        page_token = page.next_page_token
        if not page_token:
            break
    else:
        raise RuntimeError(
            f"The Zoom user listing did not end after {MAX_LISTING_PAGES} pages"
        )

    if total is not None and listed < total:
        logger.warning(
            "The Zoom user listing returned %s of the %s users it reported",
            listed,
            total,
        )

    for domain in sorted(members):
        yield ExternalUserGroup(
            id=build_domain_group_id(domain), user_emails=sorted(members[domain])
        )


def zoom_group_sync(
    db_session: Session, cc_pair: ConnectorCredentialPair
) -> Iterator[ExternalUserGroup]:
    connector = instantiate_connector(
        db_session=db_session,
        source=cc_pair.connector.source,
        input_type=cc_pair.connector.input_type,
        connector_specific_config=cc_pair.connector.connector_specific_config,
        credential=cc_pair.credential,
    )
    if not isinstance(connector, ZoomConnector):
        raise RuntimeError("Bug: a Zoom pair built a non-Zoom connector")
    client = connector.client
    if client is None:
        raise ConnectorMissingCredentialError("Zoom")
    yield from iter_zoom_domain_groups(
        lambda page_token: client.list_users(page_token=page_token)
    )
