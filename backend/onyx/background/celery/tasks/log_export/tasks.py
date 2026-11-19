"""Tasks of the admin log export (see onyx/server/log_export/api.py)."""

from datetime import UTC, datetime, timedelta

from celery import shared_task

from onyx.configs.constants import OnyxCeleryTask
from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.log_export import get_log_export_file_ids_created_before
from onyx.file_store.file_store import get_default_file_store
from onyx.server.log_export.collection import (
    WORKER_PIECE_QUEUES,
    collect_host_logs,
)
from onyx.server.log_export.configs import LOG_EXPORT_RETENTION_SECONDS
from onyx.utils.logger import setup_logger

logger = setup_logger()


@shared_task(
    name=OnyxCeleryTask.EXPORT_LOGS_COLLECT_TASK,
    ignore_result=True,
    trail=False,
    # One attempt only: a redelivered task would find the export done.
    acks_late=False,
)
def export_logs_collect_task(
    *,
    tenant_id: str,  # noqa: ARG001 - TenantAwareTask sets the tenant from it
    export_id: str,
    worker_name: str,
) -> None:
    """Collects the log files of the host of this worker for one export."""
    if worker_name not in WORKER_PIECE_QUEUES:
        logger.error("Log export %s: unknown worker name %r", export_id, worker_name)
        return
    receipt = collect_host_logs(export_id, worker_name)
    logger.info(
        "Log export %s: %s reported %s", export_id, worker_name, receipt.status.value
    )


@shared_task(
    name=OnyxCeleryTask.EXPORT_LOGS_CLEANUP_TASK,
    ignore_result=True,
    trail=False,
)
def export_logs_cleanup_task(*, tenant_id: str) -> None:  # noqa: ARG001
    """Deletes the uploaded pieces of exports older than the retention."""
    cutoff = datetime.now(UTC) - timedelta(seconds=LOG_EXPORT_RETENTION_SECONDS)
    with get_session_with_current_tenant() as db_session:
        file_ids = get_log_export_file_ids_created_before(db_session, cutoff)

    file_store = get_default_file_store()
    deleted = 0
    for file_id in file_ids:
        try:
            file_store.delete_file(file_id, error_on_missing=False)
        except Exception:
            # The next run tries again.
            logger.exception("Failed to delete log export file %s", file_id)
            continue
        deleted += 1
    if deleted:
        logger.info("Deleted %d old log export files", deleted)
