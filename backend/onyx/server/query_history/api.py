"""Admin query history: read users' chat sessions and export them to CSV.

The workspace `query_history_type` setting applies to every route: DISABLED
refuses them, ANONYMIZED removes user emails, NORMAL shows them.
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from onyx.auth.permissions import require_permission
from onyx.background.celery.versioned_apps.client import app as client_app
from onyx.background.task_utils import (
    construct_query_history_report_name,
    extract_task_id_from_query_history_report_name,
)
from onyx.configs.constants import (
    PUBLIC_API_TAGS,
    QUERY_HISTORY_TASK_NAME_PREFIX,
    USER_EXPORT_TASK_EXPIRES,
    OnyxCeleryPriority,
    OnyxCeleryQueues,
    OnyxCeleryTask,
    QAFeedbackType,
    QueryHistoryType,
)
from onyx.db.engine.sql_engine import get_session
from onyx.db.enums import Permission
from onyx.db.file_record import (
    get_filerecord_by_file_id_optional,
    get_query_history_export_files,
)
from onyx.db.models import FileRecord, TaskQueueState, TaskStatus, User
from onyx.db.query_history import (
    fetch_first_messages,
    fetch_query_history_page,
    fetch_query_history_session,
    fetch_query_history_sessions_for_user,
    fetch_session_messages,
)
from onyx.db.tasks import (
    fail_task_with_id,
    get_all_tasks_with_prefix,
    get_task_with_id,
    register_task,
)
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.file_store.file_store import get_default_file_store
from onyx.server.documents.models import PaginatedReturn
from onyx.server.query_history.export import (
    EXPORT_END_METADATA_KEY,
    EXPORT_REQUESTED_AT_METADATA_KEY,
    EXPORT_START_METADATA_KEY,
)
from onyx.server.query_history.models import (
    ChatSessionMinimal,
    ChatSessionsForUser,
    ChatSessionSnapshot,
    QueryHistoryExport,
    QueryHistoryExportStatus,
    QueryHistoryExportStatusResponse,
    StartQueryHistoryExportResponse,
)
from onyx.server.query_history.snapshots import (
    anonymizes,
    export_range_from_task_name,
    export_task_name,
    to_minimal,
    to_snapshot,
)
from onyx.server.settings.store import load_settings
from onyx.utils.logger import setup_logger
from shared_configs.contextvars import get_current_tenant_id

logger = setup_logger()

router = APIRouter()

_reader = require_permission(Permission.READ_QUERY_HISTORY)

MAX_PAGE_SIZE = 100
MAX_SESSIONS_FOR_USER = 1000


def _query_history_type() -> QueryHistoryType:
    query_history_type = load_settings().query_history_type
    if query_history_type == QueryHistoryType.DISABLED:
        raise OnyxError(
            OnyxErrorCode.FEATURE_DISABLED,
            "Query history is disabled in this workspace",
        )
    return query_history_type or QueryHistoryType.NORMAL


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _check_range(start: datetime | None, end: datetime | None) -> None:
    if start is not None and end is not None and _as_utc(start) > _as_utc(end):
        raise OnyxError(OnyxErrorCode.INVALID_INPUT, "The start is after the end")


@router.get("/admin/chat-session-history")
def get_chat_session_history(
    page_num: int = Query(0, ge=0),
    page_size: int = Query(10, ge=1, le=MAX_PAGE_SIZE),
    feedback_type: QAFeedbackType | None = None,
    start_time: datetime | None = None,
    end_time: datetime | None = None,
    _: User = Depends(_reader),
    query_history_type: QueryHistoryType = Depends(_query_history_type),
    db_session: Session = Depends(get_session),
) -> PaginatedReturn[ChatSessionMinimal]:
    _check_range(start_time, end_time)
    rows, total = fetch_query_history_page(
        db_session,
        start=_as_utc(start_time) if start_time else None,
        end=_as_utc(end_time) if end_time else None,
        feedback_type=feedback_type,
        page_num=page_num,
        page_size=page_size,
    )
    first = fetch_first_messages(db_session, [row[0].id for row in rows])
    anonymize = anonymizes(query_history_type)
    return PaginatedReturn(
        items=[to_minimal(row, first.get(row[0].id, {}), anonymize) for row in rows],
        total_items=total,
    )


@router.get("/admin/chat-session-history/{chat_session_id}")
def get_chat_session_admin(
    chat_session_id: UUID,
    _: User = Depends(_reader),
    query_history_type: QueryHistoryType = Depends(_query_history_type),
    db_session: Session = Depends(get_session),
) -> ChatSessionSnapshot:
    row = fetch_query_history_session(db_session, chat_session_id)
    if row is None:
        raise OnyxError(OnyxErrorCode.SESSION_NOT_FOUND, "Chat session not found")
    messages = fetch_session_messages(db_session, [chat_session_id])
    return to_snapshot(
        row, messages.get(chat_session_id, []), anonymizes(query_history_type)
    )


@router.get("/admin/chat-sessions")
def get_chat_sessions_for_user(
    user_id: UUID,
    _: User = Depends(_reader),
    query_history_type: QueryHistoryType = Depends(_query_history_type),
    db_session: Session = Depends(get_session),
) -> ChatSessionsForUser:
    rows = fetch_query_history_sessions_for_user(
        db_session, user_id, MAX_SESSIONS_FOR_USER
    )
    first = fetch_first_messages(db_session, [row[0].id for row in rows])
    anonymize = anonymizes(query_history_type)
    return ChatSessionsForUser(
        sessions=[to_minimal(row, first.get(row[0].id, {}), anonymize) for row in rows]
    )


def _task_status(task: TaskQueueState) -> QueryHistoryExportStatus:
    if task.status == TaskStatus.FAILURE:
        return QueryHistoryExportStatus.FAILURE
    if task.status == TaskStatus.SUCCESS:
        return QueryHistoryExportStatus.SUCCESS
    return QueryHistoryExportStatus.PENDING


def _metadata_time(record: FileRecord, key: str) -> datetime | None:
    metadata = record.file_metadata if isinstance(record.file_metadata, dict) else {}
    value = metadata.get(key)
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@router.get("/admin/query-history/list")
def list_query_history_exports(
    _: User = Depends(_reader),
    __: QueryHistoryType = Depends(_query_history_type),
    db_session: Session = Depends(get_session),
) -> list[QueryHistoryExport]:
    exports: dict[str, QueryHistoryExport] = {}
    # A task row hides a file with the same id, so rows go first.
    for task in get_all_tasks_with_prefix(db_session, QUERY_HISTORY_TASK_NAME_PREFIX):
        export_range = export_range_from_task_name(task.task_name)
        exports[task.task_id] = QueryHistoryExport(
            request_id=task.task_id,
            status=_task_status(task),
            start=export_range[0] if export_range else None,
            end=export_range[1] if export_range else None,
            time_requested=task.register_time,
        )
    for record in get_query_history_export_files(db_session):
        request_id = extract_task_id_from_query_history_report_name(record.file_id)
        if request_id in exports:
            continue
        exports[request_id] = QueryHistoryExport(
            request_id=request_id,
            status=QueryHistoryExportStatus.SUCCESS,
            start=_metadata_time(record, EXPORT_START_METADATA_KEY),
            end=_metadata_time(record, EXPORT_END_METADATA_KEY),
            time_requested=_metadata_time(record, EXPORT_REQUESTED_AT_METADATA_KEY)
            or record.created_at,
        )
    return sorted(exports.values(), key=lambda e: e.time_requested, reverse=True)


@router.post("/admin/query-history/start-export", tags=PUBLIC_API_TAGS)
def start_query_history_export(
    start: datetime | None = None,
    end: datetime | None = None,
    _: User = Depends(_reader),
    __: QueryHistoryType = Depends(_query_history_type),
    db_session: Session = Depends(get_session),
) -> StartQueryHistoryExportResponse:
    _check_range(start, end)
    range_end = _as_utc(end) if end else datetime.now(UTC)
    range_start = _as_utc(start) if start else datetime.fromtimestamp(0, UTC)
    request_id = str(uuid4())
    register_task(
        export_task_name(request_id, range_start, range_end),
        db_session,
        task_id=request_id,
    )
    try:
        # The task row is keyed by the request ID, not the Celery task ID.
        client_app.send_task(
            OnyxCeleryTask.EXPORT_QUERY_HISTORY_TASK,
            kwargs={
                "request_id": request_id,
                "start": range_start.isoformat(),
                "end": range_end.isoformat(),
                "tenant_id": get_current_tenant_id(),
            },
            queue=OnyxCeleryQueues.CSV_GENERATION,
            priority=OnyxCeleryPriority.MEDIUM,
            expires=USER_EXPORT_TASK_EXPIRES,
        )
    except Exception:
        logger.exception("Could not send query history export %s", request_id)
        fail_task_with_id(db_session, request_id, TaskStatus.PENDING)
        raise OnyxError(
            OnyxErrorCode.SERVICE_UNAVAILABLE,
            "Could not queue the export. Try again later.",
        )
    return StartQueryHistoryExportResponse(request_id=request_id)


def _export_status(
    db_session: Session, request_id: str
) -> QueryHistoryExportStatus | None:
    task = get_task_with_id(db_session, request_id)
    if task is not None:
        return _task_status(task)
    record = get_filerecord_by_file_id_optional(
        construct_query_history_report_name(request_id), db_session
    )
    return QueryHistoryExportStatus.SUCCESS if record is not None else None


@router.get("/admin/query-history/export-status", tags=PUBLIC_API_TAGS)
def get_query_history_export_status(
    request_id: str,
    _: User = Depends(_reader),
    __: QueryHistoryType = Depends(_query_history_type),
    db_session: Session = Depends(get_session),
) -> QueryHistoryExportStatusResponse:
    status = _export_status(db_session, request_id)
    if status is None:
        raise OnyxError(OnyxErrorCode.NOT_FOUND, "No export with this request ID")
    return QueryHistoryExportStatusResponse(status=status)


@router.get("/admin/query-history/download", tags=PUBLIC_API_TAGS)
def download_query_history_csv(
    request_id: str,
    _: User = Depends(_reader),
    __: QueryHistoryType = Depends(_query_history_type),
    db_session: Session = Depends(get_session),
) -> StreamingResponse:
    status = _export_status(db_session, request_id)
    if status is None:
        raise OnyxError(OnyxErrorCode.NOT_FOUND, "No export with this request ID")
    if status != QueryHistoryExportStatus.SUCCESS:
        raise OnyxError(
            OnyxErrorCode.CONFLICT, f"The export is not ready. Its status is {status}."
        )
    report_name = construct_query_history_report_name(request_id)
    content = get_default_file_store().read_file(report_name, mode="b")
    return StreamingResponse(
        content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{report_name}"'},
    )
