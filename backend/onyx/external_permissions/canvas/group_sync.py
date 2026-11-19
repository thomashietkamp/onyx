"""Group sync for Canvas. Per course of the pair: the course group (every
active or invited member), one group per section, and one per Canvas group of
the course. When a course of the pair is public, the all-users group holds
every user of the account, which needs a token that can list account users;
without one the group stays empty and public courses stay limited to their
rosters."""

from collections.abc import Iterator

from sqlalchemy.orm import Session

from onyx.connectors.canvas.client import CanvasApiClient
from onyx.connectors.canvas.connector import (
    CanvasConnector,
    canvas_all_users_group_id,
    canvas_course_group_id,
    canvas_group_group_id,
    canvas_section_group_id,
)
from onyx.connectors.factory import instantiate_connector
from onyx.db.external_perm import ExternalUserGroup
from onyx.db.models import ConnectorCredentialPair
from onyx.error_handling.exceptions import OnyxError
from onyx.external_permissions.canvas.access import (
    CanvasRosterMember,
    build_course_permission_context,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()

_DENIED_STATUSES = (401, 403)


def _course_canvas_groups(
    client: CanvasApiClient, course_id: int, email_by_user_id: dict[int, str]
) -> Iterator[ExternalUserGroup]:
    try:
        group_ids: list[int] = [
            group["id"]
            for page in client.paginate(
                f"courses/{course_id}/groups", params={"per_page": "100"}
            )
            for group in page
            if isinstance(group, dict) and isinstance(group.get("id"), int)
        ]
    except OnyxError as e:
        if e.status_code not in _DENIED_STATUSES:
            raise
        logger.warning(
            "Canvas token cannot list the groups of course %s; group-only "
            "assignments stay hidden from the group members",
            course_id,
        )
        return
    for group_id in group_ids:
        emails: set[str] = set()
        for page in client.paginate(
            f"groups/{group_id}/users", params={"per_page": "100"}
        ):
            for user in page:
                if not isinstance(user, dict):
                    continue
                user_id = user.get("id")
                if isinstance(user_id, int) and user_id in email_by_user_id:
                    emails.add(email_by_user_id[user_id])
        yield ExternalUserGroup(
            id=canvas_group_group_id(client.base_url, group_id),
            user_emails=sorted(emails),
        )


def _account_user_emails(client: CanvasApiClient) -> set[str] | None:
    emails: set[str] = set()
    try:
        for page in client.paginate(
            "accounts/self/users", params={"per_page": "100", "include[]": "email"}
        ):
            for user in page:
                email = user.get("email") if isinstance(user, dict) else None
                if isinstance(email, str) and email:
                    emails.add(email.strip().lower())
    except OnyxError as e:
        if e.status_code not in _DENIED_STATUSES:
            raise
        logger.warning(
            "Canvas token cannot list account users; public courses stay "
            "limited to their rosters"
        )
        return None
    return emails


def iter_course_groups(
    client: CanvasApiClient, course_id: int, members: list[CanvasRosterMember]
) -> Iterator[ExternalUserGroup]:
    email_by_user_id = {m.user_id: m.email for m in members if m.email is not None}
    yield ExternalUserGroup(
        id=canvas_course_group_id(client.base_url, course_id),
        user_emails=sorted(set(email_by_user_id.values())),
    )
    section_emails: dict[int, set[str]] = {}
    for member in members:
        if member.email is None:
            continue
        for section_id in member.section_ids:
            section_emails.setdefault(section_id, set()).add(member.email)
    for section_id, emails in sorted(section_emails.items()):
        yield ExternalUserGroup(
            id=canvas_section_group_id(client.base_url, section_id),
            user_emails=sorted(emails),
        )
    yield from _course_canvas_groups(client, course_id, email_by_user_id)


def iter_canvas_groups(
    client: CanvasApiClient, course_ids: list[int]
) -> Iterator[ExternalUserGroup]:
    any_public = False
    for course_id in course_ids:
        context = build_course_permission_context(client, course_id)
        any_public = any_public or context.is_public
        yield from iter_course_groups(client, course_id, context.members)
    if any_public:
        emails = _account_user_emails(client)
        if emails is not None:
            yield ExternalUserGroup(
                id=canvas_all_users_group_id(client.base_url),
                user_emails=sorted(emails),
            )


def canvas_group_sync(
    db_session: Session, cc_pair: ConnectorCredentialPair
) -> Iterator[ExternalUserGroup]:
    connector = instantiate_connector(
        db_session=db_session,
        source=cc_pair.connector.source,
        input_type=cc_pair.connector.input_type,
        connector_specific_config=cc_pair.connector.connector_specific_config,
        credential=cc_pair.credential,
    )
    if not isinstance(connector, CanvasConnector):
        raise RuntimeError("Bug: a Canvas pair built a non-Canvas connector")
    course_ids = [course.id for course in connector._list_courses()]
    yield from iter_canvas_groups(connector.canvas_client, course_ids)
