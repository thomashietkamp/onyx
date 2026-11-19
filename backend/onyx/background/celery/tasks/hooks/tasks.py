"""Deletes old hook execution logs. Only failures are logged, so the table
grows only while an endpoint fails."""

from celery import shared_task

from onyx.configs.constants import OnyxCeleryTask
from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.hook import cleanup_old_execution_logs__no_commit
from onyx.utils.logger import setup_logger

logger = setup_logger()

HOOK_EXECUTION_LOG_MAX_AGE_DAYS = 30


@shared_task(
    name=OnyxCeleryTask.HOOK_EXECUTION_LOG_CLEANUP_TASK,
    ignore_result=True,
    trail=False,
)
def hook_execution_log_cleanup_task(*, tenant_id: str) -> None:  # noqa: ARG001
    with get_session_with_current_tenant() as db_session:
        deleted = cleanup_old_execution_logs__no_commit(
            db_session=db_session, max_age_days=HOOK_EXECUTION_LOG_MAX_AGE_DAYS
        )
        db_session.commit()
    if deleted:
        logger.info("Deleted %s old hook execution logs", deleted)
