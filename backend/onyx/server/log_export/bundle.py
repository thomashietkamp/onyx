"""The downloaded log export bundle: README.txt, manifest.json and the files
of each uploaded piece under a folder named for the piece."""

import os
import shutil
import tempfile
import zipfile
from datetime import UTC, datetime
from typing import IO

from onyx.file_store.file_store import get_default_file_store
from onyx.server.log_export.models import (
    LogExportManifest,
    LogExportReceiptStatus,
    LogExportStatus,
)

_STATUS_TEXT: dict[LogExportReceiptStatus, str] = {
    LogExportReceiptStatus.UPLOADED: "uploaded",
    LogExportReceiptStatus.NO_LOGS: "no log files found",
    LogExportReceiptStatus.DUPLICATE_HOST: "covered by another worker on the same host",
    LogExportReceiptStatus.FAILED: "failed",
}


def _readme(status: LogExportStatus, generated_at: datetime) -> str:
    lines = [
        "Onyx log export",
        "",
        f"Export id: {status.export_id}",
        f"Started: {status.started_at.isoformat()}",
        f"Generated: {generated_at.isoformat()}",
        "",
        "Each folder holds the log files that one piece of the export collected:",
        "the API server, one replica of each background worker type, and, when",
        "enabled on Kubernetes, the stdout and events of every pod.",
        "Pieces on the same host share one log directory, so only one of them",
        "collects it. manifest.json lists what each piece reported.",
        "",
        "Log files can include user emails, document titles, search queries and",
        "error payloads. Review them before you share them outside your",
        "organization.",
        "",
        "Pieces:",
    ]
    for receipt in status.receipts:
        line = f"- {receipt.worker_name} ({receipt.hostname}): "
        line += _STATUS_TEXT[receipt.status]
        if receipt.status == LogExportReceiptStatus.UPLOADED:
            line += f", {receipt.file_count} files"
        if receipt.error:
            line += f" ({receipt.error})"
        lines.append(line)
    lines.extend(
        f"- {worker_name}: did not report before the deadline"
        for worker_name in status.pending_worker_names
    )
    return "\n".join(lines) + "\n"


def build_log_export_bundle(status: LogExportStatus) -> str:
    """Writes the bundle to a temporary file and returns its path. The caller
    deletes the file."""
    generated_at = datetime.now(UTC)
    manifest = LogExportManifest(
        export_id=status.export_id,
        started_at=status.started_at,
        deadline=status.deadline,
        generated_at=generated_at,
        receipts=status.receipts,
        pending_worker_names=status.pending_worker_names,
    )
    with tempfile.NamedTemporaryFile(
        prefix="onyx_log_export_", suffix=".zip", delete=False
    ) as output:
        path = output.name
        try:
            _write_bundle(output, status, manifest, generated_at)
        except BaseException:
            output.close()
            os.unlink(path)
            raise
    return path


def _write_bundle(
    output: IO[bytes],
    status: LogExportStatus,
    manifest: LogExportManifest,
    generated_at: datetime,
) -> None:
    file_store = get_default_file_store()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("README.txt", _readme(status, generated_at))
        bundle.writestr("manifest.json", manifest.model_dump_json(indent=2))
        for receipt in status.receipts:
            if receipt.file_id is None:
                continue
            piece_io = file_store.read_file(
                receipt.file_id, mode="b", use_tempfile=True
            )
            with piece_io, zipfile.ZipFile(piece_io) as piece:
                for info in piece.infolist():
                    if info.is_dir():
                        continue
                    with (
                        piece.open(info) as source,
                        bundle.open(
                            f"{receipt.worker_name}/{info.filename}", "w"
                        ) as target,
                    ):
                        shutil.copyfileobj(source, target)
