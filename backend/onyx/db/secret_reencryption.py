"""Re-encrypt stored secrets under the current write key.

Reads the raw bytes of every encrypted column, and rewrites each value that is
plain text or under another key id. Each write is a compare-and-swap on the
old bytes, so a value that changed during the run is left alone. A second run
continues where an interrupted one stopped.
"""

from pydantic import BaseModel
from sqlalchemy import (
    Column,
    LargeBinary,
    MetaData,
    Table,
    and_,
    literal,
    select,
    type_coerce,
)
from sqlalchemy.orm import Session

from onyx.db.models import EncryptedJson, EncryptedString
from onyx.utils.encryption import (
    current_write_key_id,
    decrypt_bytes_to_string,
    encrypt_string_to_bytes,
)
from onyx.utils.encryption_keyring import sealed_key_id
from onyx.utils.logger import setup_logger

logger = setup_logger()

_COMMIT_EVERY = 200


class ColumnReencryptionReport(BaseModel):
    table: str
    column: str
    total: int = 0
    up_to_date: int = 0
    rewritten: int = 0
    # Changed by another writer between the read and the write.
    changed_during_run: int = 0


def encrypted_columns(metadata: MetaData) -> list[tuple[Table, Column]]:
    """Every column of an encrypted type, in a stable order."""
    found: list[tuple[Table, Column]] = []
    for table in sorted(metadata.tables.values(), key=lambda t: t.fullname):
        found.extend(
            (table, column)
            for column in table.columns
            if isinstance(column.type, (EncryptedString, EncryptedJson))
        )
    return found


def _raw(column: Column) -> Column:
    # Bypass the encrypted type, which would decrypt on read and encrypt on write.
    return type_coerce(column, LargeBinary)  # ty: ignore[invalid-return-type]


def reencrypt_column(
    db_session: Session, table: Table, column: Column, dry_run: bool
) -> ColumnReencryptionReport:
    report = ColumnReencryptionReport(table=table.fullname, column=column.name)
    pk_columns = list(table.primary_key.columns)
    if not pk_columns:
        logger.warning("Skipping %s.%s: the table has no primary key", table, column)
        return report

    write_key_id = current_write_key_id()
    rows = db_session.execute(
        select(*pk_columns, _raw(column)).where(column.is_not(None))
    ).all()

    pending = 0
    for row in rows:
        report.total += 1
        *pk_values, old = row
        if write_key_id is not None and sealed_key_id(old) == write_key_id:
            report.up_to_date += 1
            continue

        new = encrypt_string_to_bytes(decrypt_bytes_to_string(old))
        if dry_run:
            report.rewritten += 1
            continue

        result = db_session.execute(
            table.update()
            .where(
                and_(
                    *(
                        pk == value
                        for pk, value in zip(pk_columns, pk_values, strict=True)
                    ),
                    _raw(column) == literal(old, LargeBinary),
                )
            )
            .values({column.name: literal(new, LargeBinary)})
        )
        if result.rowcount == 1:  # ty: ignore[unresolved-attribute]
            report.rewritten += 1
        else:
            report.changed_during_run += 1

        pending += 1
        if pending >= _COMMIT_EVERY:
            db_session.commit()
            pending = 0

    if not dry_run:
        db_session.commit()
    return report


def reencrypt_all(
    db_session: Session, metadata: MetaData, dry_run: bool
) -> list[ColumnReencryptionReport]:
    return [
        reencrypt_column(db_session, table, column, dry_run)
        for table, column in encrypted_columns(metadata)
    ]
