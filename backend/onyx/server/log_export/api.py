"""Admin log export for support.

Start sends a collect task to the queue of each worker type, starts the
Kubernetes collection in a thread when it is enabled, and collects the API
server's own logs inline. Each piece has LOG_EXPORT_DEADLINE_SECONDS. The
export is ready when every piece reported or the deadline passed.
"""

import contextvars
import os
import re
import threading
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

from onyx.auth.permissions import require_permission
from onyx.background.celery.versioned_apps.client import app as client_app
from onyx.cache.factory import get_cache_backend
from onyx.configs.constants import OnyxCeleryPriority, OnyxCeleryTask
from onyx.db.enums import Permission
from onyx.db.models import User
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.server.log_export.bundle import build_log_export_bundle
from onyx.server.log_export.collection import (
    API_SERVER_PIECE,
    KUBERNETES_PIECE,
    WORKER_PIECE_QUEUES,
    claim_host,
    collect_host_logs,
    load_export_status,
    release_active_export,
    save_export_record,
    short_error,
    try_claim_active_export,
    write_receipt,
)
from onyx.server.log_export.configs import LOG_EXPORT_DEADLINE_SECONDS
from onyx.server.log_export.kubernetes_collection import (
    KUBERNETES_COLLECTION_ENABLED,
    collect_kubernetes_logs,
)
from onyx.server.log_export.models import (
    LogExportReceipt,
    LogExportReceiptStatus,
    LogExportRecord,
    LogExportStartResponse,
    LogExportState,
    LogExportStatus,
)
from onyx.utils.logger import setup_logger
from shared_configs.configs import MULTI_TENANT
from shared_configs.contextvars import get_current_tenant_id

logger = setup_logger()

router = APIRouter(prefix="/admin/log-export")

_EXPORT_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _reject_if_multi_tenant() -> None:
    # The export reads container-local files, which cloud tenants share.
    if MULTI_TENANT:
        raise OnyxError(
            OnyxErrorCode.SINGLE_TENANT_ONLY,
            "Log export is not available on this deployment.",
        )


def _get_status_or_404(export_id: str) -> LogExportStatus:
    status = (
        load_export_status(get_cache_backend(), export_id)
        if _EXPORT_ID_PATTERN.match(export_id)
        else None
    )
    if status is None:
        raise OnyxError(OnyxErrorCode.NOT_FOUND, "Log export not found")
    return status


def _send_collect_tasks(export_id: str, tenant_id: str) -> None:
    cache = get_cache_backend()
    for worker_name, queue in WORKER_PIECE_QUEUES.items():
        try:
            client_app.send_task(
                OnyxCeleryTask.EXPORT_LOGS_COLLECT_TASK,
                kwargs={
                    "tenant_id": tenant_id,
                    "export_id": export_id,
                    "worker_name": worker_name,
                },
                queue=queue,
                priority=OnyxCeleryPriority.HIGHEST,
                # A task taken after the deadline is no longer useful.
                expires=LOG_EXPORT_DEADLINE_SECONDS,
            )
        except Exception as error:
            logger.exception(
                "Log export %s: failed to send to %s", export_id, worker_name
            )
            write_receipt(
                cache,
                export_id,
                LogExportReceipt(
                    worker_name=worker_name,
                    hostname="",
                    status=LogExportReceiptStatus.FAILED,
                    error=short_error(error),
                    reported_at=datetime.now(UTC),
                ),
            )


@router.post("")
def start_log_export(
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> LogExportStartResponse:
    _reject_if_multi_tenant()
    cache = get_cache_backend()
    export_id = uuid4().hex
    if not try_claim_active_export(cache, export_id):
        raise OnyxError(
            OnyxErrorCode.RATE_LIMITED, "A log export is already in progress"
        )

    try:
        started_at = datetime.now(UTC)
        expected = [API_SERVER_PIECE, *WORKER_PIECE_QUEUES]
        if KUBERNETES_COLLECTION_ENABLED:
            expected.append(KUBERNETES_PIECE)
        save_export_record(
            cache,
            LogExportRecord(
                export_id=export_id,
                started_at=started_at,
                deadline=started_at + timedelta(seconds=LOG_EXPORT_DEADLINE_SECONDS),
                expected_worker_names=expected,
            ),
        )
        # Claim this host before the workers start, so a worker on the same
        # host reports a duplicate instead of racing this process.
        host_claimed = claim_host(cache, export_id, API_SERVER_PIECE)
        _send_collect_tasks(export_id, get_current_tenant_id())
    except Exception:
        release_active_export(cache, export_id)
        raise

    if KUBERNETES_COLLECTION_ENABLED:
        context = contextvars.copy_context()
        threading.Thread(
            target=context.run,
            args=(collect_kubernetes_logs, export_id),
            name=f"log-export-kubernetes-{export_id}",
            daemon=True,
        ).start()

    collect_host_logs(export_id, API_SERVER_PIECE, host_claimed=host_claimed)
    return LogExportStartResponse(export_id=export_id)


@router.get("/{export_id}")
def get_log_export_status(
    export_id: str,
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> LogExportStatus:
    _reject_if_multi_tenant()
    return _get_status_or_404(export_id)


@router.get("/{export_id}/download")
def download_log_export(
    export_id: str,
    _: User = Depends(require_permission(Permission.FULL_ADMIN_PANEL_ACCESS)),
) -> FileResponse:
    _reject_if_multi_tenant()
    status = _get_status_or_404(export_id)
    if status.state != LogExportState.READY:
        raise OnyxError(OnyxErrorCode.CONFLICT, "The log export is still collecting")

    path = build_log_export_bundle(status)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=f"onyx_logs_{status.started_at:%Y%m%d_%H%M%S}.zip",
        background=BackgroundTask(os.unlink, path),
    )
