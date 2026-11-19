"""Job rows of manual usage reports.

A manual report has a `TaskQueueState` row while it is pending, started or
failed. The worker deletes the row in the transaction that records the
report, so a report and its job never both show, and never both miss.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy.orm import Session

from onyx.configs.app_configs import JOB_TIMEOUT
from onyx.configs.constants import (
    USER_EXPORT_TASK_EXPIRES,
    USER_JOB_LOST_TASK_GRACE,
    FileOrigin,
    OnyxCeleryTask,
)
from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.enums import TaskStatus
from onyx.db.tasks import (
    claim_task_with_id,
    cleanup_stale_tasks,
    complete_started_task__no_commit,
    fail_task_with_id,
    get_expired_failed_task_ids,
)
from onyx.db.usage_report import create_usage_report__no_commit
from onyx.file_store.file_store import get_default_file_store
from onyx.server.usage_report.generation import (
    generate_usage_report_zip,
    usage_report_download_name,
    usage_report_name,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()

USAGE_REPORT_TASK_NAME_PREFIX = OnyxCeleryTask.GENERATE_USAGE_REPORT_TASK


def usage_report_task_name(report_id: str) -> str:
    return f"{USAGE_REPORT_TASK_NAME_PREFIX}_{report_id}"


def cleanup_usage_report_jobs(db_session: Session) -> None:
    """Fails lost and stalled jobs and purges failures older than a day.

    Run hourly by the shared export cleanup. A pending job is lost when its
    task expired in the queue. A started job is stalled after the job timeout.
    """
    deleted_file_task_ids: list[str] = []
    for report_id in get_expired_failed_task_ids(
        db_session, USAGE_REPORT_TASK_NAME_PREFIX
    ):
        # Without its row, an uploaded file of a failed job looks published.
        try:
            get_default_file_store().delete_file(
                usage_report_name(report_id), error_on_missing=False
            )
        except Exception:
            logger.exception(
                "Failed to delete the file of failed usage report %s", report_id
            )
            continue
        deleted_file_task_ids.append(report_id)

    cleanup_stale_tasks(
        db_session,
        task_name_prefix=USAGE_REPORT_TASK_NAME_PREFIX,
        pending_timeout=USER_EXPORT_TASK_EXPIRES + USER_JOB_LOST_TASK_GRACE,
        started_timeout=JOB_TIMEOUT + USER_JOB_LOST_TASK_GRACE,
        expired_failed_task_ids=deleted_file_task_ids,
    )


_ZIP_CONTENT_TYPE = "application/zip"


def _parse_optional_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _delete_report_file(report_name: str) -> None:
    try:
        get_default_file_store().delete_file(report_name, error_on_missing=False)
    except Exception:
        logger.exception("Failed to delete usage report file %s", report_name)


def run_usage_report_job(
    report_id: str,
    requestor_user_id: str | None,
    period_from: str | None,
    period_to: str | None,
) -> None:
    """Claims the pending job, builds and uploads the ZIP, then publishes it.

    The ZIP is built in one session, which closes before the upload. The
    upload holds no row lock. Publishing locks the started row; a worker whose
    job the cleanup already failed deletes its file instead.
    """
    with get_session_with_current_tenant() as db_session:
        if not claim_task_with_id(db_session, report_id):
            # Another worker has the job, or the cleanup failed it.
            logger.info("Usage report job %s is not pending, skipping", report_id)
            return

    report_name = usage_report_name(report_id)
    from_dt = _parse_optional_datetime(period_from)
    to_dt = _parse_optional_datetime(period_to)
    uploaded = False
    try:
        with get_session_with_current_tenant() as db_session:
            report_zip = generate_usage_report_zip(db_session, from_dt, to_dt)
        with report_zip:
            get_default_file_store().save_file(
                content=report_zip,
                display_name=usage_report_download_name(from_dt, to_dt),
                file_origin=FileOrigin.GENERATED_REPORT,
                file_type=_ZIP_CONTENT_TYPE,
                file_id=report_name,
            )
        uploaded = True

        with get_session_with_current_tenant() as db_session:
            if not complete_started_task__no_commit(db_session, report_id):
                db_session.rollback()
                logger.warning(
                    "Usage report job %s is no longer started; dropping its file",
                    report_id,
                )
                _delete_report_file(report_name)
                return
            create_usage_report__no_commit(
                db_session,
                report_name=report_name,
                requestor_user_id=(
                    UUID(requestor_user_id) if requestor_user_id else None
                ),
                period_from=from_dt,
                period_to=to_dt,
            )
            db_session.commit()
    except Exception:
        logger.exception("Failed to generate usage report %s", report_id)
        with get_session_with_current_tenant() as db_session:
            fail_task_with_id(db_session, report_id, TaskStatus.STARTED)
        if uploaded:
            _delete_report_file(report_name)
        raise

    logger.info("Generated usage report %s", report_name)
