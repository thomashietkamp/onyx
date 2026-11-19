"""The Kubernetes piece of a log export: the stdout of every container of the
Onyx release, the logs of crashed containers, pod status and events.

Runs only when LOG_EXPORT_KUBERNETES_ENABLED is true inside Kubernetes. The
Helm chart then binds a Role with get/list on pods, pods/log and events to
the API server's service account (templates/log-export-rbac.yaml).
"""

import json
import socket
import tempfile
import time
import zipfile
from datetime import UTC, datetime
from typing import Any

from kubernetes import client, config
from pydantic import BaseModel

from onyx.cache.factory import get_cache_backend
from onyx.server.log_export.collection import (
    KUBERNETES_PIECE,
    short_error,
    upload_piece,
    write_receipt,
)
from onyx.server.log_export.configs import (
    LOG_EXPORT_DEADLINE_SECONDS,
    LOG_EXPORT_KUBERNETES_ENABLED,
    LOG_EXPORT_KUBERNETES_MAX_BYTES_PER_STREAM,
    LOG_EXPORT_KUBERNETES_MAX_TOTAL_BYTES,
    LOG_EXPORT_KUBERNETES_TAIL_LINES,
)
from onyx.server.log_export.models import LogExportReceipt, LogExportReceiptStatus
from onyx.utils.logger import setup_logger
from onyx.utils.platform_utils import is_running_in_kubernetes

logger = setup_logger()

KUBERNETES_COLLECTION_ENABLED = (
    LOG_EXPORT_KUBERNETES_ENABLED and is_running_in_kubernetes()
)

_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
_INSTANCE_LABEL = "app.kubernetes.io/instance"
# (connect, read) for each Kubernetes API call.
_REQUEST_TIMEOUT_S: tuple[float, float] = (5.0, 30.0)
# Stop starting new reads this long before the deadline, to leave time to upload.
_UPLOAD_RESERVE_SECONDS = 15
_SPOOL_MAX_BYTES = 32 * 1024 * 1024


class _Budget(BaseModel):
    """The bytes and time left for this piece."""

    bytes_left: int
    stop_at: float
    truncated: bool = False

    def time_left(self) -> bool:
        return time.monotonic() < self.stop_at


def _read_namespace() -> str:
    with open(_NAMESPACE_FILE) as namespace_file:
        return namespace_file.read().strip()


def _release_selector(api: client.CoreV1Api, namespace: str) -> str | None:
    """The label selector of the pods of this release, from the API server's
    own pod. None selects every pod of the namespace."""
    own_pod = api.read_namespaced_pod(
        name=socket.gethostname(),
        namespace=namespace,
        _request_timeout=_REQUEST_TIMEOUT_S,
    )
    labels: dict[str, str] = own_pod.metadata.labels or {}
    instance = labels.get(_INSTANCE_LABEL)
    return f"{_INSTANCE_LABEL}={instance}" if instance else None


def _pod_summary(pod: Any) -> dict[str, Any]:
    statuses = pod.status.container_statuses or []
    return {
        "name": pod.metadata.name,
        "phase": pod.status.phase,
        "node": pod.spec.node_name,
        "start_time": str(pod.status.start_time),
        "conditions": [
            {"type": c.type, "status": c.status, "reason": c.reason}
            for c in (pod.status.conditions or [])
        ],
        "containers": [
            {
                "name": status.name,
                "ready": status.ready,
                "restart_count": status.restart_count,
                "state": str(status.state),
                "last_state": str(status.last_state),
            }
            for status in statuses
        ],
    }


def _write_container_log(
    api: client.CoreV1Api,
    archive: zipfile.ZipFile,
    budget: _Budget,
    namespace: str,
    pod_name: str,
    container_name: str,
    previous: bool,
) -> bool:
    if budget.bytes_left <= 0 or not budget.time_left():
        budget.truncated = True
        return False
    text: str = api.read_namespaced_pod_log(
        name=pod_name,
        namespace=namespace,
        container=container_name,
        previous=previous,
        timestamps=True,
        tail_lines=LOG_EXPORT_KUBERNETES_TAIL_LINES,
        limit_bytes=min(LOG_EXPORT_KUBERNETES_MAX_BYTES_PER_STREAM, budget.bytes_left),
        _request_timeout=_REQUEST_TIMEOUT_S,
    )
    data = text.encode()
    budget.bytes_left -= len(data)
    suffix = "previous" if previous else "current"
    archive.writestr(f"pods/{pod_name}/{container_name}.{suffix}.log", data)
    return True


def _collect(archive: zipfile.ZipFile, budget: _Budget) -> tuple[int, list[str]]:
    config.load_incluster_config()
    api = client.CoreV1Api()
    namespace = _read_namespace()
    selector = _release_selector(api, namespace)

    pods = api.list_namespaced_pod(
        namespace=namespace,
        label_selector=selector,
        _request_timeout=_REQUEST_TIMEOUT_S,
    ).items
    archive.writestr(
        "pods.json", json.dumps([_pod_summary(pod) for pod in pods], indent=2)
    )
    events = api.list_namespaced_event(
        namespace=namespace, _request_timeout=_REQUEST_TIMEOUT_S
    ).items
    archive.writestr(
        "events.json",
        json.dumps(
            [
                {
                    "time": str(event.last_timestamp or event.event_time),
                    "type": event.type,
                    "reason": event.reason,
                    "object": f"{event.involved_object.kind}/{event.involved_object.name}",
                    "count": event.count,
                    "message": event.message,
                }
                for event in events
            ],
            indent=2,
        ),
    )

    file_count = 2
    errors: list[str] = []
    for pod in pods:
        restarted = {
            status.name
            for status in (pod.status.container_statuses or [])
            if status.restart_count
        }
        for container in pod.spec.containers:
            streams = [False, True] if container.name in restarted else [False]
            for previous in streams:
                try:
                    if _write_container_log(
                        api,
                        archive,
                        budget,
                        namespace,
                        pod.metadata.name,
                        container.name,
                        previous,
                    ):
                        file_count += 1
                except Exception as error:
                    # A pod that is gone or not started yet has no log.
                    errors.append(
                        f"{pod.metadata.name}/{container.name}: {short_error(error)}"
                    )
    return file_count, errors


def collect_kubernetes_logs(export_id: str) -> None:
    """Collects the Kubernetes piece and writes its receipt. Never raises."""
    cache = get_cache_backend()
    hostname = socket.gethostname()
    budget = _Budget(
        bytes_left=LOG_EXPORT_KUBERNETES_MAX_TOTAL_BYTES,
        stop_at=time.monotonic()
        + LOG_EXPORT_DEADLINE_SECONDS
        - _UPLOAD_RESERVE_SECONDS,
    )
    try:
        with tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_BYTES) as output:
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                file_count, errors = _collect(archive, budget)
            output.seek(0)
            file_id = upload_piece(export_id, KUBERNETES_PIECE, output)
        warnings = []
        if budget.truncated:
            warnings.append("stopped at the size or time limit")
        if errors:
            warnings.append(f"{len(errors)} container logs failed ({errors[0]})")
        receipt = LogExportReceipt(
            worker_name=KUBERNETES_PIECE,
            hostname=hostname,
            status=LogExportReceiptStatus.UPLOADED,
            file_count=file_count,
            error=short_error("; ".join(warnings)) if warnings else None,
            file_id=file_id,
            reported_at=datetime.now(UTC),
        )
    except Exception as error:
        logger.exception("Log export %s: Kubernetes collection failed", export_id)
        receipt = LogExportReceipt(
            worker_name=KUBERNETES_PIECE,
            hostname=hostname,
            status=LogExportReceiptStatus.FAILED,
            error=short_error(error),
            reported_at=datetime.now(UTC),
        )
    write_receipt(cache, export_id, receipt)
