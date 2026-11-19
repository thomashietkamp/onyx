"""Group sync for Jira.

Project permission schemes and project roles name Jira groups by name (see
``page_access.py``). This sync lists the site's groups (``groups/picker``) and
reads the active members of each (``group/member``). Project roles need no
group sync: the doc sync expands their user actors.

A group deleted between the listing and the member read is skipped. A member
without a visible email (or an app or customer account) is left out. Needs
Administer Jira or the Browse users and groups global permission.
"""

from collections.abc import Callable, Iterable, Iterator
from typing import Any

from sqlalchemy.orm import Session

from onyx.connectors.factory import instantiate_connector
from onyx.connectors.jira.connector import JiraConnector
from onyx.connectors.jira.source_operations import JiraApiError
from onyx.db.external_perm import ExternalUserGroup
from onyx.db.models import ConnectorCredentialPair
from onyx.utils.logger import setup_logger

logger = setup_logger()

_MEMBER_PAGE_SIZE = 50
_ATLASSIAN_ACCOUNT_TYPE = "atlassian"
_NOT_FOUND = 404

MemberPage = Callable[[str, int, int], dict[str, Any]]


def _member_email(member: dict[str, Any]) -> str | None:
    account_type = member.get("accountType")
    if account_type is not None and account_type != _ATLASSIAN_ACCOUNT_TYPE:
        return None
    email = member.get("emailAddress")
    return email.lower() if isinstance(email, str) and email else None


def _group_member_emails(group_name: str, member_page: MemberPage) -> set[str]:
    emails: set[str] = set()
    start_at = 0
    while True:
        page = member_page(group_name, start_at, _MEMBER_PAGE_SIZE)
        values = page.get("values") or []
        for member in values:
            if isinstance(member, dict) and (email := _member_email(member)):
                emails.add(email)
        if page.get("isLast", True) or not values:
            return emails
        start_at += len(values)


def iter_jira_groups(
    group_names: Iterable[str], member_page: MemberPage
) -> Iterator[ExternalUserGroup]:
    for group_name in group_names:
        try:
            emails = _group_member_emails(group_name, member_page)
        except JiraApiError as e:
            if e.status_code == _NOT_FOUND and "does not exist" in (e.body or ""):
                logger.info("Jira group %s was deleted during the sync", group_name)
                continue
            raise
        yield ExternalUserGroup(id=group_name, user_emails=sorted(emails))


def jira_group_sync(
    db_session: Session, cc_pair: ConnectorCredentialPair
) -> Iterator[ExternalUserGroup]:
    connector = instantiate_connector(
        db_session=db_session,
        source=cc_pair.connector.source,
        input_type=cc_pair.connector.input_type,
        connector_specific_config=cc_pair.connector.connector_specific_config,
        credential=cc_pair.credential,
    )
    if not isinstance(connector, JiraConnector):
        raise RuntimeError("Bug: a Jira pair built a non-Jira connector")
    ops = connector.source_operations
    page = ops.list_groups()
    if page.total is not None and page.total > len(page.group_names):
        # Members of the groups past the limit lose access in Onyx.
        logger.warning(
            "Jira matched %s groups but listed only %s; raise "
            "jira.ajax.autocomplete.limit to sync every group",
            page.total,
            len(page.group_names),
        )
    yield from iter_jira_groups(
        page.group_names,
        lambda group_name, start_at, max_results: ops.get_group_members_page(
            group_name=group_name, start_at=start_at, max_results=max_results
        ),
    )
