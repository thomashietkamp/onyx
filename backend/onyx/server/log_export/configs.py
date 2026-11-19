import os

from onyx.utils.logger import setup_logger

logger = setup_logger()


def _positive_int_env(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.error("%s=%r is not an integer; using %d", name, raw, default)
        return default
    if value <= 0:
        logger.error("%s=%r is not positive; using %d", name, raw, default)
        return default
    return value


# Each piece of an export (a worker, the API server, Kubernetes) has this long.
LOG_EXPORT_DEADLINE_SECONDS = 90
# A finished export and its files stay this long.
LOG_EXPORT_RETENTION_SECONDS = 24 * 60 * 60
# The log files of one host that one piece collects, newest first.
LOG_EXPORT_MAX_BYTES_PER_HOST = 512 * 1024 * 1024

# The Helm chart sets this when it binds the log export Role to the API server.
LOG_EXPORT_KUBERNETES_ENABLED = (
    os.environ.get("LOG_EXPORT_KUBERNETES_ENABLED", "").lower() == "true"
)
LOG_EXPORT_KUBERNETES_TAIL_LINES = _positive_int_env(
    "LOG_EXPORT_KUBERNETES_TAIL_LINES", 100_000
)
LOG_EXPORT_KUBERNETES_MAX_BYTES_PER_STREAM = _positive_int_env(
    "LOG_EXPORT_KUBERNETES_MAX_BYTES_PER_STREAM", 25 * 1024 * 1024
)
LOG_EXPORT_KUBERNETES_MAX_TOTAL_BYTES = _positive_int_env(
    "LOG_EXPORT_KUBERNETES_MAX_TOTAL_BYTES", 500 * 1024 * 1024
)
