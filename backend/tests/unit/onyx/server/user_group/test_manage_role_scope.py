"""GATE 2 on a group's connector rows: create and update need the same rights."""

from collections.abc import Iterator
from typing import cast
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.orm import Session

from onyx.db.enums import ConnectorManageRole
from onyx.db.models import User
from onyx.error_handling.exceptions import OnyxError
from onyx.server.user_group import api
from onyx.server.user_group.models import UserGroupCreate

EDITOR = ConnectorManageRole.EDITOR
OPERATOR = ConnectorManageRole.OPERATOR
_SESSION = cast(Session, MagicMock())
_USER = cast(User, MagicMock())


@pytest.fixture
def operator_on_pair_1() -> Iterator[None]:
    """The caller is no connector admin. They can operate pair 1, not edit it."""

    def pairs_with_access(
        _user: User, _db: Session, pair_ids: set[int], level: object
    ) -> set[int]:
        return set() if level is api.CCPairAccessLevel.EDIT else pair_ids & {1}

    with (
        patch.object(api, "load_cc_pairs"),
        patch.object(api, "_is_connector_admin", return_value=False),
        patch.object(api, "_pairs_with_access", side_effect=pairs_with_access),
    ):
        yield


@pytest.mark.usefixtures("operator_on_pair_1")
def test_create_needs_editor_rights_on_each_connector() -> None:
    with patch.object(api, "insert_user_group__no_commit") as insert:
        with pytest.raises(OnyxError):
            api.create_user_group(
                UserGroupCreate(name="g", user_ids=[], cc_pair_ids=[1]),
                user=_USER,
                db_session=_SESSION,
            )
    insert.assert_not_called()


@pytest.mark.usefixtures("operator_on_pair_1")
def test_an_operator_cannot_lower_an_editor_row() -> None:
    with pytest.raises(OnyxError):
        api._scope_manage_roles(_USER, _SESSION, {1: EDITOR}, {1: OPERATOR})


@pytest.mark.usefixtures("operator_on_pair_1")
def test_an_operator_cannot_remove_an_editor_row() -> None:
    assert api._scope_manage_roles(_USER, _SESSION, {1: EDITOR}, {}) == {1: EDITOR}


@pytest.mark.usefixtures("operator_on_pair_1")
def test_an_operator_still_keeps_and_sets_operator_rows() -> None:
    assert api._scope_manage_roles(_USER, _SESSION, {}, {1: OPERATOR}) == {1: OPERATOR}
    assert api._scope_manage_roles(_USER, _SESSION, {1: OPERATOR}, {}) == {}
