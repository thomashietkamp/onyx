"""The admin log export against the real cache and file store: fan-out, the
single-export lock, host de-duplication, the deadline, and the bundle."""

import json
import os
import zipfile
from collections.abc import Generator
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from onyx.background.celery.tasks.log_export.tasks import export_logs_collect_task
from onyx.cache.factory import get_cache_backend
from onyx.error_handling.error_codes import OnyxErrorCode
from onyx.error_handling.exceptions import OnyxError
from onyx.file_store.file_store import get_default_file_store
from onyx.server.log_export.api import (
    download_log_export,
    get_log_export_status,
    start_log_export,
)
from onyx.server.log_export.collection import (
    WORKER_PIECE_QUEUES,
    load_export_record,
    piece_file_id,
    release_active_export,
    save_export_record,
)
from onyx.server.log_export.configs import LOG_EXPORT_DEADLINE_SECONDS
from onyx.server.log_export.models import LogExportReceiptStatus, LogExportState
from shared_configs.contextvars import get_current_tenant_id

_COLLECTION = "onyx.server.log_export.collection"


@pytest.fixture
def log_directory(tmp_path: Path) -> Generator[Path, None, None]:
    (tmp_path / "api_server_debug.log").write_text("api line\n")
    rotated = tmp_path / "nested"
    rotated.mkdir()
    (rotated / "celery_worker_light_debug.log.1").write_text("light line\n")
    with patch(f"{_COLLECTION}.LOG_FILE_DIRECTORY", str(tmp_path)):
        yield tmp_path


def _start(admin: MagicMock) -> tuple[str, MagicMock]:
    with (
        patch("onyx.server.log_export.api.client_app") as client_app,
        patch("onyx.server.log_export.api.KUBERNETES_COLLECTION_ENABLED", False),
    ):
        export_id = start_log_export(_=admin).export_id
    return export_id, client_app


def _expire_deadline(export_id: str) -> None:
    cache = get_cache_backend()
    record = load_export_record(cache, export_id)
    assert record is not None
    save_export_record(cache, record.model_copy(update={"deadline": record.started_at}))


def test_log_export_flow(
    db_session: Session,  # noqa: ARG001 - initializes the engine for the file store
    tenant_context: None,  # noqa: ARG001
    log_directory: Path,  # noqa: ARG001
) -> None:
    admin = MagicMock()
    export_id, client_app = _start(admin)
    try:
        # One collect task for each worker type, each to its own queue, and
        # each expiring at the deadline.
        sends = client_app.send_task.call_args_list
        assert {send.kwargs["queue"] for send in sends} == set(
            WORKER_PIECE_QUEUES.values()
        )
        assert len(sends) == len(WORKER_PIECE_QUEUES)
        assert all(
            send.kwargs["expires"] == LOG_EXPORT_DEADLINE_SECONDS for send in sends
        )

        # A second start while the first collects is refused.
        with pytest.raises(OnyxError) as conflict:
            _start(admin)
        assert conflict.value.error_code == OnyxErrorCode.RATE_LIMITED

        status = get_log_export_status(export_id, _=admin)
        assert status.state == LogExportState.COLLECTING
        api_receipt = next(r for r in status.receipts if r.worker_name == "api_server")
        assert api_receipt.status == LogExportReceiptStatus.UPLOADED
        assert api_receipt.file_count == 2

        # A worker on the API server's host does not collect the same files.
        export_logs_collect_task(
            tenant_id=get_current_tenant_id(),
            export_id=export_id,
            worker_name="primary",
        )
        # A worker on its own host collects its log directory.
        with patch(f"{_COLLECTION}.socket.gethostname", return_value="light-pod"):
            export_logs_collect_task(
                tenant_id=get_current_tenant_id(),
                export_id=export_id,
                worker_name="light",
            )
        status = get_log_export_status(export_id, _=admin)
        by_worker = {r.worker_name: r for r in status.receipts}
        assert by_worker["primary"].status == LogExportReceiptStatus.DUPLICATE_HOST
        assert by_worker["light"].status == LogExportReceiptStatus.UPLOADED
        assert by_worker["light"].hostname == "light-pod"

        with pytest.raises(OnyxError) as not_ready:
            download_log_export(export_id, _=admin)
        assert not_ready.value.error_code == OnyxErrorCode.CONFLICT

        # At the deadline the export is ready with the silent workers pending,
        # and a new export can start.
        _expire_deadline(export_id)
        status = get_log_export_status(export_id, _=admin)
        assert status.state == LogExportState.READY
        assert set(status.pending_worker_names) == set(WORKER_PIECE_QUEUES) - {
            "primary",
            "light",
        }

        response = download_log_export(export_id, _=admin)
        assert response.media_type == "application/zip"
        try:
            with zipfile.ZipFile(response.path) as bundle:
                names = set(bundle.namelist())
                assert {
                    "README.txt",
                    "manifest.json",
                    "api_server/api_server_debug.log",
                    "api_server/nested/celery_worker_light_debug.log.1",
                    "light/api_server_debug.log",
                } <= names
                assert not any(name.startswith("primary/") for name in names)
                manifest = json.loads(bundle.read("manifest.json"))
                assert manifest["export_id"] == export_id
                assert "did not report" in bundle.read("README.txt").decode()
        finally:
            os.unlink(response.path)

        second_id, _ = _start(admin)
        assert second_id != export_id
        release_active_export(get_cache_backend(), second_id)
        for worker_name in ("api_server",):
            get_default_file_store().delete_file(
                piece_file_id(second_id, worker_name), error_on_missing=False
            )
    finally:
        release_active_export(get_cache_backend(), export_id)
        for worker_name in ("api_server", "light"):
            get_default_file_store().delete_file(
                piece_file_id(export_id, worker_name), error_on_missing=False
            )


def test_unknown_export_is_not_found(
    tenant_context: None,  # noqa: ARG001
) -> None:
    for export_id in ("0" * 32, "not-an-id"):
        with pytest.raises(OnyxError) as missing:
            get_log_export_status(export_id, _=MagicMock())
        assert missing.value.error_code == OnyxErrorCode.NOT_FOUND
