"""Chat retention: deletes chat sessions whose last activity is older than the
workspace's ``maximum_chat_retention_days``. Unset means keep everything."""

import time

from celery import shared_task

from onyx.configs.constants import OnyxCeleryTask
from onyx.db.chat import delete_chat_session, get_chat_sessions_older_than
from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.server.settings.store import load_settings
from onyx.utils.logger import setup_logger

logger = setup_logger()

_BATCH_SIZE = 200
# Celery runs tasks in threads, so its time limits do not apply; the task stops
# itself and the next run continues with the rest of the backlog.
_TIME_BUDGET_SECONDS = 240


@shared_task(
    name=OnyxCeleryTask.CHECK_TTL_MANAGEMENT_TASK, ignore_result=True, trail=False
)
def check_ttl_management_task(*, tenant_id: str) -> None:  # noqa: ARG001
    retention_days = load_settings().maximum_chat_retention_days
    if not retention_days:
        return

    deadline = time.monotonic() + _TIME_BUDGET_SECONDS
    deleted = 0
    while time.monotonic() < deadline:
        with get_session_with_current_tenant() as db_session:
            sessions = get_chat_sessions_older_than(
                retention_days, db_session, limit=_BATCH_SIZE
            )
            if not sessions:
                break
            deleted_before = deleted
            for user_id, chat_session_id in sessions:
                try:
                    delete_chat_session(
                        user_id,
                        chat_session_id,
                        db_session,
                        include_deleted=True,
                        hard_delete=True,
                    )
                    deleted += 1
                except Exception:
                    db_session.rollback()
                    logger.exception(
                        "Could not delete chat session %s past retention",
                        chat_session_id,
                    )
            # A batch that deleted nothing would come back unchanged.
            if len(sessions) < _BATCH_SIZE or deleted == deleted_before:
                break
    if deleted:
        logger.info(
            "Deleted %s chat session(s) older than %s day(s)", deleted, retention_days
        )
