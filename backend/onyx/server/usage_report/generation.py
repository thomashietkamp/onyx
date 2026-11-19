"""Builds the usage report ZIP: the usage PDF and the CSVs."""

import csv
import io
import tempfile
import zipfile
from datetime import UTC, datetime, timedelta
from typing import IO

from sqlalchemy.orm import Session

from onyx.db.system_usage import get_system_usage_export
from onyx.db.usage_report import (
    get_usage_report_user_rows,
    iter_usage_report_chat_message_rows,
)
from onyx.db.user_usage import get_usage_export
from onyx.server.usage_report.pdf import (
    build_usage_report_data,
    render_usage_report_pdf,
)
from onyx.utils.csv_utils import sanitize_csv_cell
from onyx.utils.logger import setup_logger

logger = setup_logger()

USAGE_REPORT_NAME_PREFIX = "usage_report_"
USAGE_REPORT_PDF_NAME = "usage_report.pdf"
# The start of an all-time report. No usage is older.
_ALL_TIME_START = datetime(2000, 1, 1, tzinfo=UTC)
# The ZIP stays in memory up to this size, then moves to disk.
_SPOOL_MAX_BYTES = 32 * 1024 * 1024


def usage_report_name(report_id: str) -> str:
    """The file id of a manual report. The page finds a report by its id."""
    return f"{USAGE_REPORT_NAME_PREFIX}{report_id}.zip"


def usage_report_download_name(
    period_from: datetime | None, period_to: datetime | None
) -> str:
    if period_from is None or period_to is None:
        return "usage_report_all_time.zip"
    return f"usage_report_{period_from:%Y-%m-%d}_{period_to:%Y-%m-%d}.zip"


def usage_report_bounds(
    period_from: datetime | None, period_to: datetime | None, now: datetime
) -> tuple[datetime, datetime]:
    """The [start, end) of the report. `period_to` is the start of the last
    day, so the end is one day later."""
    if period_from is None or period_to is None:
        return _ALL_TIME_START, now
    return period_from, period_to + timedelta(days=1)


def _period_label(period_from: datetime | None, period_to: datetime | None) -> str:
    if period_from is None or period_to is None:
        return "All time"
    return f"{period_from:%Y-%m-%d} to {period_to:%Y-%m-%d}"


def _sanitized(cells: list[str]) -> list[str]:
    # Emails, agent and model names come from users. Neutralize formulas.
    return [sanitize_csv_cell(cell) for cell in cells]


def _write_csv(
    archive: zipfile.ZipFile, name: str, header: list[str], rows: list[list[str]]
) -> None:
    with archive.open(name, "w") as raw:
        with io.TextIOWrapper(raw, encoding="utf-8", newline="") as text:
            writer = csv.writer(text)
            writer.writerow(header)
            writer.writerows(_sanitized(row) for row in rows)


def _write_chat_messages_csv(
    archive: zipfile.ZipFile, db_session: Session, start: datetime, end: datetime
) -> None:
    # Streamed: this file has a row for each message, so it can be large.
    with archive.open("chat_messages.csv", "w") as raw:
        with io.TextIOWrapper(raw, encoding="utf-8", newline="") as text:
            writer = csv.writer(text)
            writer.writerow(
                [
                    "chat_session_id",
                    "user_id",
                    "user_email",
                    "time_sent",
                    "agent",
                    "message_tokens",
                    "response_tokens",
                    "model",
                ]
            )
            for row in iter_usage_report_chat_message_rows(db_session, start, end):
                writer.writerow(
                    _sanitized(
                        [
                            str(row.chat_session_id),
                            str(row.user_id) if row.user_id else "",
                            row.user_email,
                            row.time_sent.isoformat(),
                            row.agent_name or "",
                            str(row.message_tokens),
                            ""
                            if row.response_tokens is None
                            else str(row.response_tokens),
                            row.model or "",
                        ]
                    )
                )


def generate_usage_report_zip(
    db_session: Session,
    period_from: datetime | None,
    period_to: datetime | None,
) -> IO[bytes]:
    """Returns the ZIP, positioned at its start. The caller closes it."""
    now = datetime.now(UTC)
    start, end = usage_report_bounds(period_from, period_to, now)

    # Read once: the usage CSVs and the PDF use the same rows, so they agree.
    user_rows = get_usage_export(db_session, start=start, end=end)
    system_rows = get_system_usage_export(db_session, start, end)
    users = get_usage_report_user_rows(db_session)

    output: IO[bytes] = tempfile.SpooledTemporaryFile(max_size=_SPOOL_MAX_BYTES)
    try:
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            _write_chat_messages_csv(archive, db_session, start, end)
            _write_csv(
                archive,
                "users.csv",
                ["user_id", "is_active"],
                [[str(user.user_id), str(user.is_active).lower()] for user in users],
            )
            _write_csv(
                archive,
                "usage_by_user.csv",
                [
                    "user_email",
                    "day",
                    "model",
                    "feature",
                    "provider",
                    "incognito",
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_creation_tokens",
                    "cost_cents",
                ],
                [
                    [
                        row.email,
                        row.day,
                        row.model,
                        row.flow,
                        row.provider,
                        str(row.incognito).lower(),
                        str(row.input_tokens),
                        str(row.output_tokens),
                        str(row.cache_read_tokens),
                        str(row.cache_creation_tokens),
                        f"{row.cost_cents:.4f}",
                    ]
                    for row in user_rows
                ],
            )
            _write_csv(
                archive,
                "usage_by_system.csv",
                [
                    "attribution",
                    "day",
                    "model",
                    "feature",
                    "provider",
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_creation_tokens",
                    "cost_cents",
                ],
                [
                    [
                        row.attribution.value,
                        row.day,
                        row.model,
                        row.flow,
                        row.provider,
                        str(row.input_tokens),
                        str(row.output_tokens),
                        str(row.cache_read_tokens),
                        str(row.cache_creation_tokens),
                        f"{row.cost_cents:.4f}",
                    ]
                    for row in system_rows
                ],
            )
            try:
                pdf = render_usage_report_pdf(
                    build_usage_report_data(
                        user_rows=user_rows,
                        system_rows=system_rows,
                        users=users,
                        period_label=_period_label(period_from, period_to),
                        generated_at=now,
                    )
                )
            except Exception:
                # The CSVs are the record. The ZIP ships without the summary.
                logger.exception("Failed to render the usage report PDF")
            else:
                archive.writestr(USAGE_REPORT_PDF_NAME, pdf)
    except BaseException:
        output.close()
        raise
    output.seek(0)
    return output
