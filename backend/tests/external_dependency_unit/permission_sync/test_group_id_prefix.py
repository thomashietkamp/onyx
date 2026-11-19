"""A source group whose name starts with the Onyx prefix must not take the id
of another group."""

from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from onyx.access.utils import build_ext_group_name_for_onyx
from onyx.configs.constants import DocumentSource
from onyx.db.enums import AccessType
from onyx.db.external_perm import ExternalUserGroup, upsert_external_groups
from onyx.db.models import User__ExternalUserGroupId
from tests.external_dependency_unit.permission_sync.conftest import (
    create_test_connector_credential_pair,
)


@pytest.mark.usefixtures("tenant_context")
def test_a_group_named_like_a_prefixed_id_gets_its_own_id(
    db_session: Session,
) -> None:
    cc_pair = create_test_connector_credential_pair(
        db_session, source=DocumentSource.GOOGLE_DRIVE, access_type=AccessType.SYNC
    )
    real_group = f"finance-{uuid4().hex[:6]}@corp.com"
    # A user made a group whose own id is the prefixed id of the real group.
    lookalike = build_ext_group_name_for_onyx(real_group, DocumentSource.GOOGLE_DRIVE)
    attacker = f"mallory-{uuid4().hex[:6]}@corp.com"

    upsert_external_groups(
        db_session,
        cc_pair.id,
        DocumentSource.GOOGLE_DRIVE,
        [ExternalUserGroup(id=lookalike, user_emails=[attacker])],
    )

    stored = set(
        db_session.scalars(
            select(User__ExternalUserGroupId.external_user_group_id).where(
                User__ExternalUserGroupId.cc_pair_id == cc_pair.id
            )
        ).all()
    )
    assert stored == {
        build_ext_group_name_for_onyx(lookalike, DocumentSource.GOOGLE_DRIVE)
    }
    assert lookalike not in stored
