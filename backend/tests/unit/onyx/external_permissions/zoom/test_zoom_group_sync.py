from unittest.mock import MagicMock, patch

import pytest

from onyx.access.utils import build_domain_group_id
from onyx.connectors.zoom.models import ZoomUser, ZoomUserPage
from onyx.external_permissions.zoom import group_sync
from onyx.external_permissions.zoom.group_sync import (
    iter_zoom_domain_groups,
    zoom_group_sync,
)


def _user(email: str, user_id: str | None = "u") -> ZoomUser:
    return ZoomUser(email=email, type=1, first_name="", last_name="", id=user_id)


def _pages(*pages: ZoomUserPage) -> MagicMock:
    tokens = {None: pages[0]}
    for index, page in enumerate(pages[1:], start=1):
        tokens[f"t{index}"] = page
    return MagicMock(side_effect=lambda token: tokens[token])


def test_groups_users_by_domain_across_pages() -> None:
    list_users = _pages(
        ZoomUserPage(
            users=[_user("Ann@Example.com"), _user("bob@other.org")],
            next_page_token="t1",
            total_records=4,
        ),
        ZoomUserPage(
            users=[_user("cy@example.com"), _user("pending@example.com", None)]
        ),
    )

    groups = list(iter_zoom_domain_groups(list_users))

    assert [(g.id, g.user_emails) for g in groups] == [
        (build_domain_group_id("example.com"), ["ann@example.com", "cy@example.com"]),
        (build_domain_group_id("other.org"), ["bob@other.org"]),
    ]


def test_skips_addresses_without_a_domain() -> None:
    list_users = _pages(ZoomUserPage(users=[_user("not-an-email"), _user("@x.com")]))

    assert list(iter_zoom_domain_groups(list_users)) == []


def test_a_failed_page_yields_no_group() -> None:
    first = ZoomUserPage(users=[_user("ann@example.com")], next_page_token="t1")
    list_users = MagicMock(side_effect=[first, RuntimeError("Zoom is down")])

    with pytest.raises(RuntimeError):
        list(iter_zoom_domain_groups(list_users))


def test_a_listing_that_never_ends_raises() -> None:
    page = ZoomUserPage(users=[_user("ann@example.com")], next_page_token="again")
    with patch.object(group_sync, "MAX_LISTING_PAGES", 3):
        with pytest.raises(RuntimeError, match="did not end"):
            list(iter_zoom_domain_groups(MagicMock(return_value=page)))


def test_group_sync_uses_the_pair_client() -> None:
    connector = MagicMock(spec=group_sync.ZoomConnector)
    connector.client = MagicMock()
    connector.client.list_users.return_value = ZoomUserPage(
        users=[_user("ann@example.com")]
    )
    with patch.object(group_sync, "instantiate_connector", return_value=connector):
        groups = list(zoom_group_sync(MagicMock(), MagicMock()))

    assert [g.id for g in groups] == [build_domain_group_id("example.com")]
    connector.client.list_users.assert_called_once_with(page_token=None)
