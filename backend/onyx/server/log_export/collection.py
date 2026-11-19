"""State and collection of the admin log export.

An export has pieces: the API server, one replica of each Celery worker
type, and the Kubernetes API when it is enabled. Each piece uploads one ZIP
to the file store and writes a receipt to the cache. Pieces on the same host
share the log directory, so the first piece to claim a host collects it.
"""

import os
import socket
import tempfile
import zipfile
from datetime import UTC, datetime
from typing import IO

from onyx.cache.factory import get_cache_backend
from onyx.cache.interface import CacheBackend
from onyx.configs.constants import FileOrigin, OnyxCeleryQueues
from onyx.file_store.file_store import get_default_file_store
from onyx.server.log_export.configs import (
    LOG_EXPORT_DEADLINE_SECONDS,
    LOG_EXPORT_MAX_BYTES_PER_HOST,
    LOG_EXPORT_RETENTION_SECONDS,
)
from onyx.server.log_export.models import (
    LogExportReceipt,
    LogExportReceiptStatus,
    LogExportRecord,
    LogExportState,
    LogExportStatus,
)
from onyx.utils.logger import LOG_FILE_DIRECTORY, setup_logger

logger = setup_logger()

API_SERVER_PIECE = "api_server"
KUBERNETES_PIECE = "kubernetes"
# A queue that only that worker type reads, so one replica takes the task.
WORKER_PIECE_QUEUES: dict[str, str] = {
    "primary": OnyxCeleryQueues.PRIMARY,
    "light": OnyxCeleryQueues.VESPA_METADATA_SYNC,
    "heavy": OnyxCeleryQueues.CSV_GENERATION,
    "docfetching": OnyxCeleryQueues.CONNECTOR_DOC_FETCHING,
    "docprocessing": OnyxCeleryQueues.DOCPROCESSING,
    "user_file_processing": OnyxCeleryQueues.USER_FILE_PROCESSING,
    "monitoring": OnyxCeleryQueues.MONITORING,
    "scheduled_tasks": OnyxCeleryQueues.SCHEDULED_TASKS,
}

_ACTIVE_EXPORT_KEY = "log_export:active"
# The active key outlives the deadline a little, so a stuck export cannot
# block new exports for long.
_ACTIVE_EXPORT_GRACE_SECONDS = 30
_SPOOL_MAX_BYTES = 32 * 1024 * 1024
_MAX_ERROR_CHARS = 500
ZIP_CONTENT_TYPE = "application/zip"


def _record_key(export_id: str) -> str:
    return f"log_export:record:{export_id}"


def _receipt_key(export_id: str, worker_name: str) -> str:
    return f"log_export:receipt:{export_id}:{worker_name}"


def _host_key(export_id: str, hostname: str) -> str:
    return f"log_export:host:{export_id}:{hostname}"


def piece_file_id(export_id: str, worker_name: str) -> str:
    return f"log_export_{export_id}_{worker_name}.zip"


def short_error(error: BaseException | str) -> str:
    return str(error)[:_MAX_ERROR_CHARS] or type(error).__name__


def try_claim_active_export(cache: CacheBackend, export_id: str) -> bool:
    """One export runs at a time in a tenant, across API server replicas."""
    return cache.set_if_absent(
        _ACTIVE_EXPORT_KEY,
        export_id,
        ex=LOG_EXPORT_DEADLINE_SECONDS + _ACTIVE_EXPORT_GRACE_SECONDS,
    )


def release_active_export(cache: CacheBackend, export_id: str) -> None:
    cache.delete_if_value(_ACTIVE_EXPORT_KEY, export_id.encode())


def save_export_record(cache: CacheBackend, record: LogExportRecord) -> None:
    cache.set(
        _record_key(record.export_id),
        record.model_dump_json(),
        ex=LOG_EXPORT_RETENTION_SECONDS,
    )


def load_export_record(cache: CacheBackend, export_id: str) -> LogExportRecord | None:
    raw = cache.get(_record_key(export_id))
    return LogExportRecord.model_validate_json(raw) if raw else None


def write_receipt(
    cache: CacheBackend, export_id: str, receipt: LogExportReceipt
) -> None:
    cache.set(
        _receipt_key(export_id, receipt.worker_name),
        receipt.model_dump_json(),
        ex=LOG_EXPORT_RETENTION_SECONDS,
    )


def load_export_status(cache: CacheBackend, export_id: str) -> LogExportStatus | None:
    record = load_export_record(cache, export_id)
    if record is None:
        return None
    receipts: list[LogExportReceipt] = []
    pending: list[str] = []
    for worker_name in record.expected_worker_names:
        raw = cache.get(_receipt_key(export_id, worker_name))
        if raw:
            receipts.append(LogExportReceipt.model_validate_json(raw))
        else:
            pending.append(worker_name)
    ready = not pending or datetime.now(UTC) >= record.deadline
    if ready:
        release_active_export(cache, export_id)
    return LogExportStatus(
        export_id=export_id,
        state=LogExportState.READY if ready else LogExportState.COLLECTING,
        started_at=record.started_at,
        deadline=record.deadline,
        receipts=receipts,
        pending_worker_names=pending,
    )


def claim_host(cache: CacheBackend, export_id: str, worker_name: str) -> bool:
    """The first piece on a host collects its log directory."""
    return cache.set_if_absent(
        _host_key(export_id, socket.gethostname()),
        worker_name,
        ex=LOG_EXPORT_RETENTION_SECONDS,
    )


def _log_files_newest_first(log_directory: str) -> list[str]:
    """Relative paths of the files under the log directory."""
    if not os.path.isdir(log_directory):
        return []
    entries: list[tuple[float, str]] = []
    for root, _dirs, files in os.walk(log_directory):
        for file_name in files:
            path = os.path.join(root, file_name)
            try:
                entries.append((os.path.getmtime(path), path))
            except OSError:
                # Rotated away between the listing and the stat.
                continue
    entries.sort(reverse=True)
    return [os.path.relpath(path, log_directory) for _, path in entries]


def zip_log_directory(
    log_directory: str, output: IO[bytes], max_bytes: int
) -> tuple[int, str | None]:
    """Writes the newest log files that fit in `max_bytes` to `output`.
    Returns the file count and a warning when files were left out."""
    file_count = 0
    total_bytes = 0
    skipped: list[str] = []
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative_path in _log_files_newest_first(log_directory):
            path = os.path.join(log_directory, relative_path)
            try:
                size = os.path.getsize(path)
                if total_bytes + size > max_bytes:
                    skipped.append(relative_path)
                    continue
                archive.write(path, arcname=relative_path)
            except OSError:
                skipped.append(relative_path)
                continue
            total_bytes += size
            file_count += 1
    warning = (
        f"left out {len(skipped)} files over the size limit or unreadable"
        if skipped
        else None
    )
    return file_count, warning


def collect_host_logs(
    export_id: str, worker_name: str, host_claimed: bool = False
) -> LogExportReceipt:
    """Collects the log directory of this host for one piece and writes its
    receipt. Never raises: a failure is the receipt of the piece.
    `host_claimed` means the caller already claimed the host for this piece."""
    cache = get_cache_backend()
    hostname = socket.gethostname()
    try:
        receipt = _collect_host_logs(
            cache, export_id, worker_name, hostname, host_claimed
        )
    except Exception as error:
        logger.exception("Log export %s: piece %s failed", export_id, worker_name)
        receipt = LogExportReceipt(
            worker_name=worker_name,
            hostname=hostname,
            status=LogExportReceiptStatus.FAILED,
            error=short_error(error),
            reported_at=datetime.now(UTC),
        )
    write_receipt(cache, export_id, receipt)
    return receipt


def _collect_host_logs(
    cache: CacheBackend,
    export_id: str,
    worker_name: str,
    hostname: str,
    host_claimed: bool,
) -> LogExportReceipt:
    if not host_claimed and not claim_host(cache, export_id, worker_name):
        return LogExportReceipt(
            worker_name=worker_name,
            hostname=hostname,
            status=LogExportReceiptStatus.DUPLICATE_HOST,
            reported_at=datetime.now(UTC),
        )

    with tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_BYTES) as output:
        file_count, warning = zip_log_directory(
            LOG_FILE_DIRECTORY, output, LOG_EXPORT_MAX_BYTES_PER_HOST
        )
        if file_count == 0:
            return LogExportReceipt(
                worker_name=worker_name,
                hostname=hostname,
                status=LogExportReceiptStatus.NO_LOGS,
                error=warning,
                reported_at=datetime.now(UTC),
            )
        output.seek(0)
        file_id = upload_piece(export_id, worker_name, output)

    return LogExportReceipt(
        worker_name=worker_name,
        hostname=hostname,
        status=LogExportReceiptStatus.UPLOADED,
        file_count=file_count,
        error=warning,
        file_id=file_id,
        reported_at=datetime.now(UTC),
    )


def upload_piece(export_id: str, worker_name: str, content: IO[bytes]) -> str:
    return get_default_file_store().save_file(
        content=content,
        display_name=piece_file_id(export_id, worker_name),
        file_origin=FileOrigin.LOG_EXPORT,
        file_type=ZIP_CONTENT_TYPE,
        file_metadata={"export_id": export_id, "worker_name": worker_name},
        file_id=piece_file_id(export_id, worker_name),
    )
