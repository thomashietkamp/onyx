"""Query history CSV export, and the hourly cleanup of its jobs. Both run on
the heavy worker."""

from datetime import datetime

from celery import shared_task

from onyx.configs.constants import OnyxCeleryTask
from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.server.query_history.export import (
    cleanup_query_history_exports,
    run_query_history_export,
)
from onyx.server.usage_report.jobs import cleanup_usage_report_jobs


@shared_task(
    name=OnyxCeleryTask.EXPORT_QUERY_HISTORY_TASK, ignore_result=True, trail=False
)
def export_query_history_task(
    *,
    request_id: str,
    start: str,
    end: str,
    tenant_id: str,  # noqa: ARG001
) -> None:
    run_query_history_export(
        request_id, datetime.fromisoformat(start), datetime.fromisoformat(end)
    )


@shared_task(
    name=OnyxCeleryTask.EXPORT_QUERY_HISTORY_CLEANUP_TASK,
    ignore_result=True,
    trail=False,
)
def export_query_history_cleanup_task(*, tenant_id: str) -> None:  # noqa: ARG001
    """The shared hourly export cleanup: query history and usage report jobs."""
    cleanup_query_history_exports()
    with get_session_with_current_tenant() as db_session:
        cleanup_usage_report_jobs(db_session)
