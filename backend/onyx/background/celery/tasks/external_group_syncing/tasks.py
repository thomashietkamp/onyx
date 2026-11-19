"""External group sync: copies the members of the source's groups.

The beat check opens an attempt row per due pair and sends the generator task.
The generator marks the pair's memberships stale, writes what the source
returns in batches, then deletes what stayed stale. A failed run deletes
nothing, so users keep last cycle's access.
"""

import time
import traceback
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from celery import Celery, Task, shared_task
from sqlalchemy.orm import Session

from onyx.background.celery.tasks.shared.lease_heartbeat import LeaseHeartbeat
from onyx.configs.app_configs import PERMISSION_SYNC_TIMEOUT
from onyx.configs.constants import (
    PERMISSION_SYNC_HEARTBEAT_INTERVAL,
    PERMISSION_SYNC_LEASE_OVERRUN,
    PERMISSION_SYNC_LEASE_SELF_EXPIRY,
    PERMISSION_SYNC_LEASE_TIMEOUT,
    PERMISSION_SYNC_LOST_TASK_GRACE,
    PERMISSION_SYNC_TASK_EXPIRES,
    OnyxCeleryPriority,
    OnyxCeleryQueues,
    OnyxCeleryTask,
)
from onyx.db.connector import mark_cc_pair_as_external_group_synced
from onyx.db.connector_credential_pair import get_connector_credential_pair_from_id
from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.external_perm import (
    ExternalUserGroup,
    mark_old_external_groups_as_stale,
    remove_stale_external_groups,
    upsert_external_groups,
)
from onyx.db.models import ConnectorCredentialPair
from onyx.db.permission_sync_attempt import (
    ExternalGroupSyncProgress,
    complete_external_group_sync_attempt,
    fail_expired_external_group_sync_attempts,
    get_external_group_sync_retry_after,
    mark_external_group_sync_attempt_failed,
    mark_external_group_sync_attempt_in_progress,
    renew_external_group_sync_attempt_lease,
    try_create_external_group_sync_attempt,
)
from onyx.db.permission_sync_schedule import fetch_perm_synced_cc_pairs
from onyx.external_permissions.sync_params import (
    external_group_sync_seconds,
    load_group_sync_fn,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()

_BATCH_SIZE = 100


def _is_due(cc_pair: ConnectorCredentialPair, now: datetime) -> bool:
    seconds = external_group_sync_seconds(cc_pair.connector.source, cc_pair.status)
    if seconds is None:
        return False
    if cc_pair.last_time_external_group_sync is None:
        return True
    return now - cc_pair.last_time_external_group_sync >= timedelta(seconds=seconds)


@shared_task(  # ty: ignore[invalid-argument-type]
    name=OnyxCeleryTask.CHECK_FOR_EXTERNAL_GROUP_SYNC,
    ignore_result=True,
    trail=False,
    bind=True,
)
def check_for_external_group_sync(self: Task, *, tenant_id: str) -> None:
    now = datetime.now(timezone.utc)
    with get_session_with_current_tenant() as db_session:
        fail_expired_external_group_sync_attempts(
            db_session,
            lease_timeout=timedelta(seconds=PERMISSION_SYNC_LEASE_TIMEOUT),
            not_started_timeout=timedelta(
                seconds=PERMISSION_SYNC_TASK_EXPIRES + PERMISSION_SYNC_LOST_TASK_GRACE
            ),
        )
        for cc_pair in fetch_perm_synced_cc_pairs(db_session):
            if not _is_due(cc_pair, now):
                continue
            retry_after = get_external_group_sync_retry_after(cc_pair.id, db_session)
            if retry_after is not None and now < retry_after:
                continue
            send_external_group_sync(self.app, cc_pair.id, tenant_id, db_session)


def send_external_group_sync(
    app: Celery, cc_pair_id: int, tenant_id: str, db_session: Session
) -> int | None:
    """Opens an attempt and sends its generator task; None when one is open."""
    celery_task_id = f"external_group_sync_{cc_pair_id}_{uuid4()}"
    attempt_id = try_create_external_group_sync_attempt(
        cc_pair_id, celery_task_id, db_session
    )
    if attempt_id is None:
        return None
    app.send_task(
        OnyxCeleryTask.CONNECTOR_EXTERNAL_GROUP_SYNC_GENERATOR_TASK,
        kwargs={
            "cc_pair_id": cc_pair_id,
            "attempt_id": attempt_id,
            "tenant_id": tenant_id,
        },
        queue=OnyxCeleryQueues.CONNECTOR_EXTERNAL_GROUP_SYNC,
        task_id=celery_task_id,
        priority=OnyxCeleryPriority.MEDIUM,
        expires=PERMISSION_SYNC_TASK_EXPIRES,
    )
    return attempt_id


@shared_task(
    name=OnyxCeleryTask.CONNECTOR_EXTERNAL_GROUP_SYNC_GENERATOR_TASK,
    ignore_result=True,
    trail=False,
)
def connector_external_group_sync_generator_task(
    *,
    cc_pair_id: int,
    attempt_id: int,
    tenant_id: str,  # noqa: ARG001
) -> None:
    with get_session_with_current_tenant() as db_session:
        try:
            mark_external_group_sync_attempt_in_progress(attempt_id, db_session)
        except Exception:
            logger.warning("Group sync attempt %s was not startable", attempt_id)
            return

    progress = ExternalGroupSyncProgress()

    def renew() -> bool:
        with get_session_with_current_tenant() as session:
            return renew_external_group_sync_attempt_lease(
                attempt_id, session, progress=progress
            )

    heartbeat = LeaseHeartbeat(
        name=f"external_group_sync attempt={attempt_id}",
        renew=renew,
        interval=PERMISSION_SYNC_HEARTBEAT_INTERVAL,
        max_silence=PERMISSION_SYNC_LEASE_SELF_EXPIRY,
        max_duration=PERMISSION_SYNC_TIMEOUT + PERMISSION_SYNC_LEASE_OVERRUN,
    )
    deadline = time.monotonic() + PERMISSION_SYNC_TIMEOUT
    try:
        with heartbeat, get_session_with_current_tenant() as db_session:
            cc_pair = get_connector_credential_pair_from_id(
                db_session,
                cc_pair_id,
                eager_load_connector=True,
                eager_load_credential=True,
            )
            if cc_pair is None:
                raise RuntimeError(f"cc-pair {cc_pair_id} no longer exists")
            source = cc_pair.connector.source
            group_sync = load_group_sync_fn(source)
            if group_sync is None:
                raise RuntimeError(f"{source.value} has no group sync")

            mark_old_external_groups_as_stale(db_session, cc_pair_id)
            batch: list[ExternalUserGroup] = []

            def flush() -> None:
                progress.total_group_memberships_synced += upsert_external_groups(
                    db_session, cc_pair_id, source, batch
                )
                progress.total_groups_processed += len(batch)
                progress.total_users_processed += sum(
                    len(group.user_emails) for group in batch
                )
                batch.clear()

            for group in group_sync(db_session, cc_pair):
                if heartbeat.is_lost():
                    raise RuntimeError("Lost the group sync lease")
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f"Group sync ran longer than {PERMISSION_SYNC_TIMEOUT} seconds"
                    )
                batch.append(group)
                if len(batch) >= _BATCH_SIZE:
                    flush()
            flush()

            remove_stale_external_groups(db_session, cc_pair_id)
            mark_cc_pair_as_external_group_synced(db_session, cc_pair_id)
            complete_external_group_sync_attempt(
                db_session,
                attempt_id,
                total_users_processed=progress.total_users_processed,
                total_groups_processed=progress.total_groups_processed,
                total_group_memberships_synced=progress.total_group_memberships_synced,
            )
    except Exception as e:
        logger.exception("Group sync failed for cc-pair %s", cc_pair_id)
        with get_session_with_current_tenant() as db_session:
            mark_external_group_sync_attempt_failed(
                attempt_id,
                db_session,
                error_message=str(e)[:1000],
                full_exception_trace=traceback.format_exc(),
            )
