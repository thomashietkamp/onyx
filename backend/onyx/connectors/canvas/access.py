from collections.abc import Callable
from typing import Any, cast

from onyx.access.models import ExternalAccess
from onyx.connectors.canvas.client import CanvasApiClient
from onyx.utils.variable_functionality import fetch_versioned_implementation

_ACCESS_MODULE = "onyx.external_permissions.canvas.access"


def get_course_permissions(
    canvas_client: CanvasApiClient,
    course_id: int,
) -> ExternalAccess | None:
    impl = cast(
        Callable[[CanvasApiClient, int], ExternalAccess | None],
        fetch_versioned_implementation(_ACCESS_MODULE, "get_course_permissions"),
    )
    return impl(canvas_client, course_id)


def build_course_permission_context(
    canvas_client: CanvasApiClient,
    course_id: int,
) -> Any | None:
    impl = cast(
        Callable[[CanvasApiClient, int], Any],
        fetch_versioned_implementation(
            _ACCESS_MODULE, "build_course_permission_context"
        ),
    )
    return impl(canvas_client, course_id)


def get_page_permissions(course_context: Any) -> ExternalAccess | None:
    impl = cast(
        Callable[[Any], ExternalAccess],
        fetch_versioned_implementation(_ACCESS_MODULE, "get_page_permissions"),
    )
    return impl(course_context)


def get_assignment_permissions(
    course_context: Any,
    assignment: Any,
) -> ExternalAccess | None:
    impl = cast(
        Callable[[Any, Any], ExternalAccess],
        fetch_versioned_implementation(_ACCESS_MODULE, "get_assignment_permissions"),
    )
    return impl(course_context, assignment)


def get_announcement_permissions(
    course_context: Any,
    announcement: Any,
) -> ExternalAccess | None:
    impl = cast(
        Callable[[Any, Any], ExternalAccess],
        fetch_versioned_implementation(_ACCESS_MODULE, "get_announcement_permissions"),
    )
    return impl(course_context, announcement)
