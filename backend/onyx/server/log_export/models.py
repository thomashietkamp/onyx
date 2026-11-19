from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class LogExportState(str, Enum):
    COLLECTING = "collecting"
    READY = "ready"


class LogExportReceiptStatus(str, Enum):
    UPLOADED = "uploaded"
    # The piece found no log files.
    NO_LOGS = "no_logs"
    # Another piece on the same host collected the shared log directory.
    DUPLICATE_HOST = "duplicate_host"
    FAILED = "failed"


class LogExportReceipt(BaseModel):
    """What one piece of an export reported."""

    worker_name: str
    hostname: str
    status: LogExportReceiptStatus
    file_count: int = 0
    # The error of a failed piece, or why an uploaded piece is incomplete.
    error: str | None = None
    # The file store id of the piece. Only an uploaded piece has one.
    file_id: str | None = None
    reported_at: datetime


class LogExportRecord(BaseModel):
    """The state of an export, kept in the cache."""

    export_id: str
    started_at: datetime
    deadline: datetime
    # Every piece the export waits for, the API server included.
    expected_worker_names: list[str]


class LogExportStartResponse(BaseModel):
    export_id: str


class LogExportStatus(BaseModel):
    export_id: str
    state: LogExportState
    started_at: datetime
    deadline: datetime
    receipts: list[LogExportReceipt]
    # The pieces with no receipt. Once the export is ready, these missed the
    # deadline.
    pending_worker_names: list[str]


class LogExportManifest(BaseModel):
    """`manifest.json` of the downloaded bundle."""

    export_id: str
    started_at: datetime
    deadline: datetime
    generated_at: datetime
    receipts: list[LogExportReceipt]
    pending_worker_names: list[str]
