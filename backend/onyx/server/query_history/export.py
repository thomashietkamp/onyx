"""The query history CSV export job: generate, publish and clean up.

The start route registers a PENDING task row and sends the task. The worker
claims the row, uploads the CSV without a row lock, then locks the STARTED row
to publish, which deletes the row. While a row exists, it decides the status;
without a row, the file is the published export.
"""

import csv
import io
import tempfile
import time
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from onyx.background.task_utils import construct_query_history_report_name
from onyx.configs.app_configs import JOB_TIMEOUT
from onyx.configs.constants import (
    QUERY_HISTORY_TASK_NAME_PREFIX,
    USER_EXPORT_TASK_EXPIRES,
    USER_JOB_LOST_TASK_GRACE,
    FileOrigin,
    FileType,
    QueryHistoryType,
)
from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.models import TaskStatus
from onyx.db.query_history import (
    fetch_query_history_sessions_after,
    fetch_session_messages,
)
from onyx.db.tasks import (
    claim_task_with_id,
    cleanup_stale_tasks,
    complete_started_task__no_commit,
    fail_task_with_id,
    get_expired_failed_task_ids,
)
from onyx.file_store.file_store import get_default_file_store
from onyx.server.query_history.snapshots import (
    CSV_HEADER,
    anonymizes,
    csv_rows,
    to_snapshot,
)
from onyx.server.settings.store import load_settings
from onyx.utils.logger import setup_logger

logger = setup_logger()

_BATCH_SIZE = 100

# File metadata keys of a published export.
EXPORT_START_METADATA_KEY = "start"
EXPORT_END_METADATA_KEY = "end"
EXPORT_REQUESTED_AT_METADATA_KEY = "requested_at"


class QueryHistoryExportTimeoutError(RuntimeError):
    """The export ran past `JOB_TIMEOUT`. Celery cannot stop thread workers,
    so the job checks its own deadline."""


def write_query_history_csv(
    db_session: Session,
    out: io.TextIOBase,
    *,
    start: datetime,
    end: datetime,
    anonymize: bool,
    deadline: float,
) -> int:
    """Writes the sessions created in the range, oldest first. Returns the
    number of rows after the header."""
    writer = csv.writer(out)
    writer.writerow(CSV_HEADER)
    written = 0
    cursor = None
    while True:
        if time.monotonic() > deadline:
            raise QueryHistoryExportTimeoutError("Query history export timed out")
        batch = fetch_query_history_sessions_after(
            db_session, start=start, end=end, after=cursor, limit=_BATCH_SIZE
        )
        if not batch:
            return written
        messages = fetch_session_messages(
            db_session, [chat_session.id for chat_session, _, _ in batch]
        )
        for row in batch:
            chat_session = row[0]
            snapshot = to_snapshot(row, messages.get(chat_session.id, []), anonymize)
            for csv_row in csv_rows(snapshot):
                writer.writerow(csv_row)
                written += 1
        last = batch[-1][0]
        cursor = (last.time_created, last.id)
        # The batch is written; release its rows before the next one.
        db_session.expunge_all()


def run_query_history_export(request_id: str, start: datetime, end: datetime) -> None:
    with get_session_with_current_tenant() as db_session:
        if not claim_task_with_id(db_session, request_id):
            logger.info(
                "Query history export %s is not pending, so it does not run",
                request_id,
            )
            return

    report_name = construct_query_history_report_name(request_id)
    file_store = get_default_file_store()
    try:
        query_history_type = load_settings().query_history_type
        if query_history_type == QueryHistoryType.DISABLED:
            raise RuntimeError("Query history is disabled")
        deadline = time.monotonic() + JOB_TIMEOUT
        with (
            tempfile.TemporaryFile() as raw,
            get_session_with_current_tenant() as db_session,
        ):
            text = io.TextIOWrapper(raw, encoding="utf-8", newline="")
            rows = write_query_history_csv(
                db_session,
                text,
                start=start,
                end=end,
                anonymize=anonymizes(query_history_type),
                deadline=deadline,
            )
            text.flush()
            raw.seek(0)
            file_store.save_file(
                content=raw,
                display_name=report_name,
                file_origin=FileOrigin.QUERY_HISTORY_CSV,
                file_type=FileType.CSV,
                file_metadata={
                    EXPORT_START_METADATA_KEY: start.isoformat(),
                    EXPORT_END_METADATA_KEY: end.isoformat(),
                    EXPORT_REQUESTED_AT_METADATA_KEY: datetime.now(UTC).isoformat(),
                },
                file_id=report_name,
            )
            text.detach()
    except Exception:
        logger.exception("Query history export %s failed", request_id)
        with get_session_with_current_tenant() as db_session:
            fail_task_with_id(db_session, request_id, TaskStatus.STARTED)
        file_store.delete_file(report_name, error_on_missing=False)
        return

    with get_session_with_current_tenant() as db_session:
        published = complete_started_task__no_commit(db_session, request_id)
        db_session.commit()
    if not published:
        # The cleanup failed this job while it ran, so the file must not look
        # published.
        logger.warning(
            "Query history export %s was failed by cleanup; deleting its file",
            request_id,
        )
        file_store.delete_file(report_name, error_on_missing=False)
        return
    logger.info("Query history export %s published %s rows", request_id, rows)


def cleanup_query_history_exports() -> None:
    """Fails lost or stalled exports and purges failures older than a day.

    A failed job may have left a file. Its file is deleted before its row,
    because without the row the file looks published. A job whose file
    delete fails keeps its row for the next run.
    """
    file_store = get_default_file_store()
    with get_session_with_current_tenant() as db_session:
        expired = get_expired_failed_task_ids(
            db_session, QUERY_HISTORY_TASK_NAME_PREFIX
        )
        purgeable: list[str] = []
        for request_id in expired:
            try:
                file_store.delete_file(
                    construct_query_history_report_name(request_id),
                    error_on_missing=False,
                )
                purgeable.append(request_id)
            except Exception:
                logger.exception(
                    "Could not delete the file of failed query history export %s",
                    request_id,
                )
        cleanup_stale_tasks(
            db_session,
            QUERY_HISTORY_TASK_NAME_PREFIX,
            pending_timeout=USER_EXPORT_TASK_EXPIRES + USER_JOB_LOST_TASK_GRACE,
            started_timeout=JOB_TIMEOUT + USER_JOB_LOST_TASK_GRACE,
            expired_failed_task_ids=purgeable,
        )
