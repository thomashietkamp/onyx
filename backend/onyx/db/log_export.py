"""Queries for the files of the admin log export."""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from onyx.configs.constants import FileOrigin
from onyx.db.models import FileRecord


def get_log_export_file_ids_created_before(
    db_session: Session, cutoff: datetime
) -> list[str]:
    return list(
        db_session.scalars(
            select(FileRecord.file_id).where(
                FileRecord.file_origin == FileOrigin.LOG_EXPORT,
                FileRecord.created_at < cutoff,
            )
        )
    )
