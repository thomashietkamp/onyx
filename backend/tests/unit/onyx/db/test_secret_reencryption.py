"""Re-encryption against an in-memory SQLite table with an encrypted column."""

import base64
import os
from collections.abc import Generator

import pytest
from sqlalchemy import (
    Column,
    Integer,
    LargeBinary,
    MetaData,
    Table,
    create_engine,
    select,
    type_coerce,
)
from sqlalchemy.orm import Session

from onyx.db.models import EncryptedString
from onyx.db.secret_reencryption import encrypted_columns, reencrypt_all
from onyx.utils import encryption
from onyx.utils.encryption_keyring import build_keyring, sealed_key_id

_metadata = MetaData()
_secrets = Table(
    "secrets",
    _metadata,
    Column("id", Integer, primary_key=True),
    Column("value", EncryptedString(), nullable=True),
)


@pytest.fixture
def db_session() -> Generator[Session, None, None]:
    engine = create_engine("sqlite://")
    _metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _raw_values(db_session: Session) -> dict[int, bytes | None]:
    rows = db_session.execute(
        select(_secrets.c.id, type_coerce(_secrets.c.value, LargeBinary))
    ).all()
    return {row[0]: row[1] for row in rows}


def _insert_raw(db_session: Session, values: dict[int, bytes | None]) -> None:
    for row_id, raw in values.items():
        db_session.execute(
            _secrets.insert().values(id=row_id, value=type_coerce(raw, LargeBinary))
        )
    db_session.commit()


def test_finds_the_encrypted_columns() -> None:
    assert [(t.name, c.name) for t, c in encrypted_columns(_metadata)] == [
        ("secrets", "value")
    ]


def test_plain_text_and_old_keys_move_to_the_write_key(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    old = build_keyring("old-secret", "", "")
    monkeypatch.setattr(encryption, "_KEYRING", old)
    old_sealed = encryption.encrypt_string_to_bytes("under-legacy")
    _insert_raw(db_session, {1: b"plain-secret", 2: old_sealed, 3: None})

    new_key = base64.b64encode(os.urandom(32)).decode()
    keyring = build_keyring("old-secret", f'{{"new": "{new_key}"}}', "new")
    monkeypatch.setattr(encryption, "_KEYRING", keyring)

    (report,) = reencrypt_all(db_session, _metadata, dry_run=False)
    assert (report.total, report.rewritten, report.up_to_date) == (2, 2, 0)

    raw = _raw_values(db_session)
    assert raw[3] is None
    for row_id, expected in {1: "plain-secret", 2: "under-legacy"}.items():
        stored = raw[row_id]
        assert stored is not None
        assert sealed_key_id(stored) == "new"
        assert encryption.decrypt_bytes_to_string(stored) == expected

    (second,) = reencrypt_all(db_session, _metadata, dry_run=False)
    assert (second.rewritten, second.up_to_date) == (0, 2)


def test_dry_run_writes_nothing(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(encryption, "_KEYRING", build_keyring("s", "", ""))
    _insert_raw(db_session, {1: b"plain-secret"})

    (report,) = reencrypt_all(db_session, _metadata, dry_run=True)
    assert report.rewritten == 1
    assert _raw_values(db_session)[1] == b"plain-secret"
