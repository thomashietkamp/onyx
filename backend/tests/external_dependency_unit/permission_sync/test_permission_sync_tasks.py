"""The doc permission sync and group sync generator tasks, run against a real
database with a fake source."""

from collections.abc import Iterator
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from onyx.access.models import ExternalAccess
from onyx.background.celery.tasks.doc_permission_syncing import tasks as doc_tasks
from onyx.background.celery.tasks.external_group_syncing import (
    tasks as group_tasks,
)
from onyx.configs.constants import DocumentSource
from onyx.connectors.interfaces import (
    GenerateSlimDocumentOutput,
    SecondsSinceUnixEpoch,
    SlimConnectorWithPermSync,
)
from onyx.connectors.models import SlimDocument
from onyx.db.enums import AccessType, PermissionSyncStatus
from onyx.db.external_perm import ExternalUserGroup, prefixed_group_id
from onyx.db.models import (
    ConnectorCredentialPair,
    DocPermissionSyncAttempt,
    DocumentByConnectorCredentialPair,
    ExternalGroupPermissionSyncAttempt,
    User,
    User__ExternalUserGroupId,
)
from onyx.db.models import Document as DbDocument
from onyx.db.permission_sync_attempt import (
    try_create_doc_permission_sync_attempt,
    try_create_external_group_sync_attempt,
)
from onyx.indexing.indexing_heartbeat import IndexingHeartbeatInterface
from tests.external_dependency_unit.permission_sync.conftest import (
    create_test_connector_credential_pair,
)

TENANT = "public"


class _FakeSource(SlimConnectorWithPermSync):
    def __init__(self, docs: list[SlimDocument]) -> None:
        self.docs = docs

    def load_credentials(self, credentials: dict[str, Any]) -> dict[str, Any] | None:  # noqa: ARG002
        return None

    def retrieve_all_slim_docs_perm_sync(
        self,
        start: SecondsSinceUnixEpoch | None = None,  # noqa: ARG002
        end: SecondsSinceUnixEpoch | None = None,  # noqa: ARG002
        callback: IndexingHeartbeatInterface | None = None,  # noqa: ARG002
    ) -> GenerateSlimDocumentOutput:
        yield list(self.docs)


def _sync_pair(db_session: Session) -> ConnectorCredentialPair:
    return create_test_connector_credential_pair(
        db_session, source=DocumentSource.SHAREPOINT, access_type=AccessType.SYNC
    )


def _add_documents(
    db_session: Session, cc_pair: ConnectorCredentialPair, count: int
) -> list[str]:
    doc_ids = [f"perm-sync-{uuid4().hex[:10]}" for _ in range(count)]
    for doc_id in doc_ids:
        db_session.add(DbDocument(id=doc_id, semantic_id=doc_id))
    db_session.flush()
    for doc_id in doc_ids:
        db_session.add(
            DocumentByConnectorCredentialPair(
                id=doc_id,
                connector_id=cc_pair.connector_id,
                credential_id=cc_pair.credential_id,
                has_been_indexed=True,
            )
        )
    db_session.commit()
    return doc_ids


@pytest.mark.usefixtures("tenant_context")
def test_doc_sync_writes_acls_and_keeps_unreadable_documents(
    db_session: Session,
) -> None:
    cc_pair = _sync_pair(db_session)
    readable, unreadable = _add_documents(db_session, cc_pair, 2)
    source = _FakeSource(
        [
            SlimDocument(
                id=readable,
                external_access=ExternalAccess(
                    external_user_emails={"Ann@Example.com"},
                    external_user_group_ids={"Finance-Group"},
                    is_public=False,
                ),
            ),
            # The connector could not read this one: its ACL stays as it was.
            SlimDocument(id=unreadable, external_access=None),
        ]
    )
    attempt_id = try_create_doc_permission_sync_attempt(
        cc_pair.id, f"test-{uuid4()}", db_session
    )
    assert attempt_id is not None

    with patch.object(doc_tasks, "instantiate_connector", return_value=source):
        doc_tasks.connector_permission_sync_generator_task(
            cc_pair_id=cc_pair.id, attempt_id=attempt_id, tenant_id=TENANT
        )

    db_session.expire_all()
    document = db_session.get(DbDocument, readable)
    assert document is not None
    assert document.external_user_emails == ["ann@example.com"]
    assert document.external_user_group_ids == [
        prefixed_group_id("Finance-Group", DocumentSource.SHAREPOINT)
    ]
    untouched = db_session.get(DbDocument, unreadable)
    assert untouched is not None and not untouched.external_user_emails

    attempt = db_session.get(DocPermissionSyncAttempt, attempt_id)
    assert attempt is not None
    assert attempt.status == PermissionSyncStatus.COMPLETED_WITH_ERRORS
    assert attempt.total_docs_synced == 1
    assert attempt.docs_with_permission_errors == 1
    db_session.refresh(cc_pair)
    assert cc_pair.last_time_perm_sync is not None


def _memberships(db_session: Session, cc_pair_id: int) -> set[tuple[str, str]]:
    rows = db_session.scalars(
        select(User__ExternalUserGroupId).where(
            User__ExternalUserGroupId.cc_pair_id == cc_pair_id
        )
    ).all()
    result: set[tuple[str, str]] = set()
    for row in rows:
        user = db_session.get(User, row.user_id)
        assert user is not None
        result.add((user.email, row.external_user_group_id))
    return result


def _run_group_sync(
    db_session: Session,
    cc_pair: ConnectorCredentialPair,
    groups: Iterator[ExternalUserGroup],
) -> ExternalGroupPermissionSyncAttempt:
    attempt_id = try_create_external_group_sync_attempt(
        cc_pair.id, f"test-{uuid4()}", db_session
    )
    assert attempt_id is not None
    with patch.object(
        group_tasks, "load_group_sync_fn", return_value=lambda _db, _pair: groups
    ):
        group_tasks.connector_external_group_sync_generator_task(
            cc_pair_id=cc_pair.id, attempt_id=attempt_id, tenant_id=TENANT
        )
    db_session.expire_all()
    attempt = db_session.get(ExternalGroupPermissionSyncAttempt, attempt_id)
    assert attempt is not None
    return attempt


@pytest.mark.usefixtures("tenant_context")
def test_group_sync_replaces_memberships_and_a_failed_run_keeps_them(
    db_session: Session,
) -> None:
    cc_pair = _sync_pair(db_session)
    suffix = uuid4().hex[:8]
    ann = f"ann_{suffix}@example.com"
    bob = f"bob_{suffix}@example.com"
    finance = prefixed_group_id("finance", DocumentSource.SHAREPOINT)
    sales = prefixed_group_id("sales", DocumentSource.SHAREPOINT)

    first = _run_group_sync(
        db_session,
        cc_pair,
        iter(
            [
                ExternalUserGroup(id="finance", user_emails=[ann, bob]),
                ExternalUserGroup(id="sales", user_emails=[bob]),
            ]
        ),
    )
    assert first.status == PermissionSyncStatus.SUCCESS
    assert _memberships(db_session, cc_pair.id) == {
        (ann, finance),
        (bob, finance),
        (bob, sales),
    }

    second = _run_group_sync(
        db_session, cc_pair, iter([ExternalUserGroup(id="finance", user_emails=[ann])])
    )
    assert second.status == PermissionSyncStatus.SUCCESS
    assert _memberships(db_session, cc_pair.id) == {(ann, finance)}

    def failing() -> Iterator[ExternalUserGroup]:
        yield ExternalUserGroup(id="sales", user_emails=[bob])
        raise RuntimeError("directory read failed")

    third = _run_group_sync(db_session, cc_pair, failing())
    assert third.status == PermissionSyncStatus.FAILED
    # Last cycle's memberships stay; nothing was removed.
    assert (ann, finance) in _memberships(db_session, cc_pair.id)
