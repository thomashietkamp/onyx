"""The Canvas ACL mappers and the Canvas group sync, against a mocked client."""

from collections.abc import Iterator
from typing import Any, cast
from unittest.mock import MagicMock

from onyx.access.models import ExternalAccess
from onyx.connectors.canvas.client import CanvasApiClient
from onyx.connectors.canvas.connector import (
    CanvasAnnouncement,
    CanvasAnnouncementSection,
    CanvasAssignment,
    CanvasAssignmentOverride,
)
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.external_permissions.canvas.access import (
    build_course_permission_context,
    get_announcement_permissions,
    get_assignment_permissions,
    get_course_permissions,
    get_page_permissions,
)
from onyx.external_permissions.canvas.group_sync import iter_canvas_groups

_COURSE = 7
_ROSTER: list[dict[str, Any]] = [
    {
        "id": 1,
        "email": "Teacher@School.edu",
        "enrollments": [
            {
                "type": "TeacherEnrollment",
                "course_section_id": 70,
                "enrollment_state": "active",
            }
        ],
    },
    {
        "id": 2,
        "email": "s2@school.edu",
        "enrollments": [
            {
                "type": "StudentEnrollment",
                "course_section_id": 70,
                "enrollment_state": "active",
            }
        ],
    },
    {
        "id": 3,
        "email": "s3@school.edu",
        "enrollments": [
            {
                "type": "StudentEnrollment",
                "course_section_id": 71,
                "enrollment_state": "invited",
            }
        ],
    },
    # No visible email: cannot be mapped to an Onyx user.
    {"id": 4, "enrollments": [{"type": "StudentEnrollment"}]},
]


def _client(
    *,
    is_public: bool = False,
    groups: list[dict[str, Any]] | None = None,
    group_users: dict[int, list[dict[str, Any]]] | None = None,
    account_users: list[dict[str, Any]] | None = None,
) -> MagicMock:
    client = MagicMock(spec=CanvasApiClient)
    client.get.return_value = ({"id": _COURSE, "is_public": is_public}, None)

    def paginate(endpoint: str, params: dict[str, Any] | None = None) -> Iterator:
        del params
        if endpoint == f"courses/{_COURSE}/users":
            yield _ROSTER
        elif endpoint == f"courses/{_COURSE}/groups":
            yield groups or []
        elif endpoint.startswith("groups/"):
            yield (group_users or {}).get(int(endpoint.split("/")[1]), [])
        elif endpoint == "accounts/self/users":
            if account_users is None:
                raise OnyxError(
                    OnyxErrorCode.INSUFFICIENT_PERMISSIONS,
                    "denied",
                    status_code_override=403,
                )
            yield account_users

    client.paginate.side_effect = paginate
    return client


def _course_access(*extra_groups: str) -> ExternalAccess:
    return ExternalAccess(
        external_user_emails=set(),
        external_user_group_ids={"canvas_canvas-course-7", *extra_groups},
        is_public=False,
    )


def _assignment(**kwargs: Any) -> CanvasAssignment:
    return CanvasAssignment(
        id=5, name="HW", html_url="https://x", course_id=_COURSE, **kwargs
    )


def test_page_maps_to_course_group_and_public_course_adds_all_users() -> None:
    private_context = build_course_permission_context(
        cast(CanvasApiClient, _client()), _COURSE
    )
    public_client = cast(CanvasApiClient, _client(is_public=True))

    assert get_page_permissions(private_context) == _course_access()
    assert get_course_permissions(public_client, _COURSE) == _course_access(
        "canvas_canvas-all-users"
    )


def test_assignment_visible_to_overrides_only() -> None:
    context = build_course_permission_context(cast(CanvasApiClient, _client()), _COURSE)
    assignment = _assignment(
        only_visible_to_overrides=True,
        assignment_visibility=[2],
        overrides=[
            CanvasAssignmentOverride(student_ids=[4]),
            CanvasAssignmentOverride(course_section_id=71),
            CanvasAssignmentOverride(group_id=900),
        ],
    )

    access = get_assignment_permissions(context, assignment)

    assert access == ExternalAccess(
        external_user_emails={"teacher@school.edu", "s2@school.edu"},
        external_user_group_ids={"canvas_canvas-section-71", "canvas_canvas-group-900"},
        is_public=False,
    )
    assert get_assignment_permissions(context, _assignment()) == _course_access()


def test_section_announcement_maps_to_sections_and_staff() -> None:
    context = build_course_permission_context(cast(CanvasApiClient, _client()), _COURSE)
    announcement = CanvasAnnouncement(
        id=9,
        title="Hi",
        html_url="https://x",
        course_id=_COURSE,
        is_section_specific=True,
        sections=[CanvasAnnouncementSection(id=70)],
    )

    access = get_announcement_permissions(context, announcement)

    assert access == ExternalAccess(
        external_user_emails={"teacher@school.edu"},
        external_user_group_ids={"canvas_canvas-section-70"},
        is_public=False,
    )


def test_group_sync_emits_course_section_and_canvas_groups() -> None:
    client = _client(
        groups=[{"id": 900}],
        group_users={900: [{"id": 3}, {"id": 4}]},
    )

    groups = {
        group.id: group.user_emails
        for group in iter_canvas_groups(cast(CanvasApiClient, client), [_COURSE])
    }

    assert groups == {
        "canvas-course-7": ["s2@school.edu", "s3@school.edu", "teacher@school.edu"],
        "canvas-section-70": ["s2@school.edu", "teacher@school.edu"],
        "canvas-section-71": ["s3@school.edu"],
        "canvas-group-900": ["s3@school.edu"],
    }


def test_group_sync_all_users_only_for_public_courses_and_when_listable() -> None:
    listable = _client(is_public=True, account_users=[{"email": "Any@School.edu"}])
    denied = _client(is_public=True)

    listable_groups = {
        g.id: g.user_emails
        for g in iter_canvas_groups(cast(CanvasApiClient, listable), [_COURSE])
    }
    denied_ids = {
        g.id for g in iter_canvas_groups(cast(CanvasApiClient, denied), [_COURSE])
    }

    assert listable_groups["canvas-all-users"] == ["any@school.edu"]
    assert "canvas-all-users" not in denied_ids
