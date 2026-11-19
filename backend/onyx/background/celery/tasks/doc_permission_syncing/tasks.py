"""Doc permission sync: copies each document's ACL from the source.

The beat check opens an attempt row per due pair (the row is the per-pair
lock: a partial unique index allows one open attempt) and sends the generator
task. The generator streams the connector's perm-sync slim documents and
writes each changed ACL; the metadata sync then rewrites the changed chunks.
Documents the source no longer lists keep their ACL until pruning removes
them, so a partial listing never hides documents.
"""

import traceback
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from celery import Celery, Task, shared_task
from sqlalchemy.orm import Session

from onyx.background.celery.tasks.shared.lease_heartbeat import LeaseHeartbeat
from onyx.configs.constants import (
    PERMISSION_SYNC_HEARTBEAT_INTERVAL,
    PERMISSION_SYNC_LEASE_SELF_EXPIRY,
    PERMISSION_SYNC_LEASE_TIMEOUT,
    PERMISSION_SYNC_LOST_TASK_GRACE,
    PERMISSION_SYNC_STALL_TIMEOUT,
    PERMISSION_SYNC_TASK_EXPIRES,
    DocumentSource,
    OnyxCeleryPriority,
    OnyxCeleryQueues,
    OnyxCeleryTask,
)
from onyx.connectors.factory import instantiate_connector
from onyx.connectors.interfaces import SlimConnectorWithPermSync
from onyx.connectors.models import HierarchyNode, SlimDocument
from onyx.db.connector import mark_cc_pair_as_permissions_synced
from onyx.db.connector_credential_pair import get_connector_credential_pair_from_id
from onyx.db.engine.sql_engine import get_session_with_current_tenant
from onyx.db.external_perm import upsert_document_external_perms
from onyx.db.hierarchy import update_hierarchy_node_permissions
from onyx.db.models import ConnectorCredentialPair
from onyx.db.permission_sync_attempt import (
    DocPermissionSyncProgress,
    complete_doc_permission_sync_attempt,
    fail_expired_doc_permission_sync_attempts,
    get_doc_permission_sync_retry_after,
    mark_doc_permission_sync_attempt_failed,
    mark_doc_permission_sync_attempt_in_progress,
    renew_doc_permission_sync_attempt_lease,
    try_create_doc_permission_sync_attempt,
)
from onyx.db.permission_sync_schedule import (
    clear_caught_up_perm_sync_pending_marks,
    fetch_perm_synced_cc_pairs,
)
from onyx.external_permissions.sync_params import doc_permission_sync_seconds
from onyx.server.runtime.onyx_runtime import OnyxRuntime
from onyx.utils.logger import setup_logger

logger = setup_logger()

_COMMIT_EVERY = 100


def _is_due(cc_pair: ConnectorCredentialPair, now: datetime) -> bool:
    seconds = doc_permission_sync_seconds(
        cc_pair.connector.source,
        cc_pair.status,
        cc_pair.last_successful_index_time is not None,
    )
    if seconds is None:
        return False
    if cc_pair.last_time_perm_sync is None:
        return True
    interval = timedelta(
        seconds=seconds * OnyxRuntime.get_doc_permission_sync_multiplier()
    )
    return now - cc_pair.last_time_perm_sync >= interval


@shared_task(  # ty: ignore[invalid-argument-type]
    name=OnyxCeleryTask.CHECK_FOR_DOC_PERMISSIONS_SYNC,
    ignore_result=True,
    trail=False,
    bind=True,
)
def check_for_doc_permissions_sync(self: Task, *, tenant_id: str) -> None:
    now = datetime.now(timezone.utc)
    with get_session_with_current_tenant() as db_session:
        fail_expired_doc_permission_sync_attempts(
            db_session,
            lease_timeout=timedelta(seconds=PERMISSION_SYNC_LEASE_TIMEOUT),
            not_started_timeout=timedelta(
                seconds=PERMISSION_SYNC_TASK_EXPIRES + PERMISSION_SYNC_LOST_TASK_GRACE
            ),
            stall_timeout=timedelta(seconds=PERMISSION_SYNC_STALL_TIMEOUT),
        )
        clear_caught_up_perm_sync_pending_marks(db_session)
        for cc_pair in fetch_perm_synced_cc_pairs(db_session):
            if not _is_due(cc_pair, now):
                continue
            retry_after = get_doc_permission_sync_retry_after(cc_pair.id, db_session)
            if retry_after is not None and now < retry_after:
                continue
            send_doc_permission_sync(self.app, cc_pair.id, tenant_id, db_session)


def send_doc_permission_sync(
    app: Celery, cc_pair_id: int, tenant_id: str, db_session: Session
) -> int | None:
    """Opens an attempt and sends its generator task; None when one is open."""
    celery_task_id = f"doc_permission_sync_{cc_pair_id}_{uuid4()}"
    attempt_id = try_create_doc_permission_sync_attempt(
        cc_pair_id, celery_task_id, db_session
    )
    if attempt_id is None:
        return None
    app.send_task(
        OnyxCeleryTask.CONNECTOR_PERMISSION_SYNC_GENERATOR_TASK,
        kwargs={
            "cc_pair_id": cc_pair_id,
            "attempt_id": attempt_id,
            "tenant_id": tenant_id,
        },
        queue=OnyxCeleryQueues.CONNECTOR_DOC_PERMISSIONS_SYNC,
        task_id=celery_task_id,
        priority=OnyxCeleryPriority.MEDIUM,
        expires=PERMISSION_SYNC_TASK_EXPIRES,
    )
    return attempt_id


def _apply_batch(
    db_session: Session,
    batch: list[SlimDocument | HierarchyNode],
    source: DocumentSource,
) -> tuple[int, int]:
    synced = 0
    errors = 0
    for element in batch:
        if isinstance(element, HierarchyNode):
            if element.external_access is not None:
                update_hierarchy_node_permissions(
                    db_session,
                    element.raw_node_id,
                    source,
                    is_public=element.external_access.is_public,
                    external_user_emails=sorted(
                        element.external_access.external_user_emails
                    ),
                    external_user_group_ids=sorted(
                        element.external_access.external_user_group_ids
                    ),
                    commit=False,
                )
            continue
        if element.external_access is None:
            # A connector bug, not "nobody may read": keep the last ACL.
            errors += 1
            continue
        upsert_document_external_perms(
            db_session, element.id, element.external_access, source
        )
        synced += 1
    return synced, errors


@shared_task(
    name=OnyxCeleryTask.CONNECTOR_PERMISSION_SYNC_GENERATOR_TASK,
    ignore_result=True,
    trail=False,
)
def connector_permission_sync_generator_task(
    *,
    cc_pair_id: int,
    attempt_id: int,
    tenant_id: str,  # noqa: ARG001
) -> None:
    started_at = datetime.now(timezone.utc)
    with get_session_with_current_tenant() as db_session:
        try:
            mark_doc_permission_sync_attempt_in_progress(attempt_id, db_session)
        except Exception:
            logger.warning(
                "Doc permission sync attempt %s was not startable", attempt_id
            )
            return

    progress = DocPermissionSyncProgress(
        total_docs_synced=0, docs_with_permission_errors=0
    )

    def renew() -> bool:
        with get_session_with_current_tenant() as session:
            return renew_doc_permission_sync_attempt_lease(
                attempt_id, session, progress=progress
            )

    heartbeat = LeaseHeartbeat(
        name=f"doc_permission_sync attempt={attempt_id}",
        renew=renew,
        interval=PERMISSION_SYNC_HEARTBEAT_INTERVAL,
        max_silence=PERMISSION_SYNC_LEASE_SELF_EXPIRY,
        max_duration=None,
    )
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
            connector = instantiate_connector(
                db_session=db_session,
                source=source,
                input_type=cc_pair.connector.input_type,
                connector_specific_config=cc_pair.connector.connector_specific_config,
                credential=cc_pair.credential,
            )
            if not isinstance(connector, SlimConnectorWithPermSync):
                raise RuntimeError(
                    f"{source.value} cannot list documents with permissions"
                )

            pending = 0
            for batch in connector.retrieve_all_slim_docs_perm_sync():
                if heartbeat.is_lost():
                    raise RuntimeError("Lost the permission sync lease")
                synced, errors = _apply_batch(db_session, batch, source)
                progress.total_docs_synced += synced
                progress.docs_with_permission_errors += errors
                pending += synced
                if pending >= _COMMIT_EVERY:
                    db_session.commit()
                    pending = 0

            db_session.commit()

            mark_cc_pair_as_permissions_synced(db_session, cc_pair_id, started_at)
            complete_doc_permission_sync_attempt(
                db_session,
                attempt_id,
                total_docs_synced=progress.total_docs_synced,
                docs_with_permission_errors=progress.docs_with_permission_errors,
            )
    except Exception as e:
        logger.exception("Doc permission sync failed for cc-pair %s", cc_pair_id)
        with get_session_with_current_tenant() as db_session:
            mark_doc_permission_sync_attempt_failed(
                attempt_id,
                db_session,
                error_message=str(e)[:1000],
                full_exception_trace=traceback.format_exc(),
                total_docs_synced=progress.total_docs_synced,
                docs_with_permission_errors=progress.docs_with_permission_errors,
            )
