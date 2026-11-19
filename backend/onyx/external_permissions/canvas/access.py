"""Who may read a Canvas course item. The Canvas connector reaches this through
``onyx/connectors/canvas/access.py``.

The crawl lists only published items, which every member of the course can
read: pages, announcements for the whole course, and assignments visible to
everyone. Those map to the course group. A public course (open to everyone, or
to every signed-in user) also adds the all-users group. A section announcement
maps to its sections' groups, and an assignment that only its overrides can
see maps to the students it names (by email), its sections and its Canvas
groups. Staff (teachers, TAs, designers) read every item of their course.

The group sync (``group_sync.py``) fills the groups from the same roster.
Group ids carry a hash of the instance URL (see the id functions in the
connector), and are returned with the Onyx source prefix, because indexing
stores them as given. The group sync yields the same ids without the prefix.
"""

from collections.abc import Iterator
from typing import Any

from pydantic import BaseModel, Field

from onyx.access.models import ExternalAccess
from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.connectors.canvas.client import CanvasApiClient
from onyx.connectors.canvas.connector import (
    CanvasAnnouncement,
    CanvasAssignment,
    canvas_all_users_group_id,
    canvas_course_group_id,
    canvas_group_group_id,
    canvas_section_group_id,
)

# The enrollment states whose users read the course, as the connector's
# validation probe uses them.
ACTIVE_ENROLLMENT_STATES = ("active", "invited")
_STAFF_ENROLLMENT_TYPES = frozenset(
    {"TeacherEnrollment", "TaEnrollment", "DesignerEnrollment"}
)


class CanvasRosterMember(BaseModel):
    user_id: int
    email: str | None = None
    section_ids: set[int] = Field(default_factory=set)
    is_staff: bool = False


class CanvasCoursePermissionContext(BaseModel):
    """What the item mappers of one course need, read once per course."""

    # ``CanvasApiClient.base_url``, which scopes the group ids.
    instance_url: str
    course_id: int
    is_public: bool
    members: list[CanvasRosterMember]

    def email_by_user_id(self) -> dict[int, str]:
        return {m.user_id: m.email for m in self.members if m.email is not None}

    def staff_emails(self) -> set[str]:
        return {m.email for m in self.members if m.is_staff and m.email is not None}


def _onyx_group_id(raw_group_id: str) -> str:
    return build_ext_group_name_for_onyx(raw_group_id, DocumentSource.CANVAS)


def _member_from_api(user: dict[str, Any]) -> CanvasRosterMember | None:
    user_id = user.get("id")
    if not isinstance(user_id, int):
        return None
    email = user.get("email")
    member = CanvasRosterMember(
        user_id=user_id,
        email=email.strip().lower() if isinstance(email, str) and email else None,
    )
    for enrollment in user.get("enrollments") or []:
        if not isinstance(enrollment, dict):
            continue
        state = enrollment.get("enrollment_state")
        if state is not None and state not in ACTIVE_ENROLLMENT_STATES:
            continue
        section_id = enrollment.get("course_section_id")
        if isinstance(section_id, int):
            member.section_ids.add(section_id)
        if enrollment.get("type") in _STAFF_ENROLLMENT_TYPES:
            member.is_staff = True
    return member


def iter_course_roster(
    canvas_client: CanvasApiClient, course_id: int
) -> Iterator[CanvasRosterMember]:
    """The course's users with an active or invited enrollment."""
    for page in canvas_client.paginate(
        f"courses/{course_id}/users",
        params={
            "per_page": "100",
            "include[]": ["email", "enrollments"],
            "enrollment_state[]": list(ACTIVE_ENROLLMENT_STATES),
        },
    ):
        for user in page:
            if isinstance(user, dict) and (member := _member_from_api(user)):
                yield member


def _course_is_public(canvas_client: CanvasApiClient, course_id: int) -> bool:
    course, _next_url = canvas_client.get(f"courses/{course_id}")
    if not isinstance(course, dict):
        raise RuntimeError(f"Canvas returned no course object for course {course_id}")
    return bool(course.get("is_public") or course.get("is_public_to_auth_users"))


def _course_access(
    instance_url: str, course_id: int, is_public: bool
) -> ExternalAccess:
    group_ids = {_onyx_group_id(canvas_course_group_id(instance_url, course_id))}
    if is_public:
        group_ids.add(_onyx_group_id(canvas_all_users_group_id(instance_url)))
    return ExternalAccess(
        external_user_emails=set(),
        external_user_group_ids=group_ids,
        is_public=False,
    )


def get_course_permissions(
    canvas_client: CanvasApiClient,
    course_id: int,
) -> ExternalAccess | None:
    return _course_access(
        canvas_client.base_url,
        course_id,
        _course_is_public(canvas_client, course_id),
    )


def build_course_permission_context(
    canvas_client: CanvasApiClient,
    course_id: int,
) -> CanvasCoursePermissionContext:
    return CanvasCoursePermissionContext(
        instance_url=canvas_client.base_url,
        course_id=course_id,
        is_public=_course_is_public(canvas_client, course_id),
        members=list(iter_course_roster(canvas_client, course_id)),
    )


def get_page_permissions(
    course_context: CanvasCoursePermissionContext,
) -> ExternalAccess:
    return _course_access(
        course_context.instance_url,
        course_context.course_id,
        course_context.is_public,
    )


def get_assignment_permissions(
    course_context: CanvasCoursePermissionContext,
    assignment: CanvasAssignment,
) -> ExternalAccess:
    if not assignment.only_visible_to_overrides:
        return _course_access(
            course_context.instance_url,
            course_context.course_id,
            course_context.is_public,
        )
    email_by_user_id = course_context.email_by_user_id()
    student_ids: set[int] = set(assignment.assignment_visibility)
    group_ids: set[str] = set()
    for override in assignment.overrides:
        student_ids.update(override.student_ids)
        if override.course_section_id is not None:
            group_ids.add(
                _onyx_group_id(
                    canvas_section_group_id(
                        course_context.instance_url, override.course_section_id
                    )
                )
            )
        if override.group_id is not None:
            group_ids.add(
                _onyx_group_id(
                    canvas_group_group_id(
                        course_context.instance_url, override.group_id
                    )
                )
            )
    emails = course_context.staff_emails() | {
        email_by_user_id[student_id]
        for student_id in student_ids
        if student_id in email_by_user_id
    }
    return ExternalAccess(
        external_user_emails=emails,
        external_user_group_ids=group_ids,
        is_public=False,
    )


def get_announcement_permissions(
    course_context: CanvasCoursePermissionContext,
    announcement: CanvasAnnouncement,
) -> ExternalAccess:
    if not announcement.is_section_specific:
        return _course_access(
            course_context.instance_url,
            course_context.course_id,
            course_context.is_public,
        )
    return ExternalAccess(
        external_user_emails=course_context.staff_emails(),
        external_user_group_ids={
            _onyx_group_id(
                canvas_section_group_id(course_context.instance_url, section.id)
            )
            for section in announcement.sections
        },
        is_public=False,
    )
