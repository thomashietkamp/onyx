from typing import Any, cast
from unittest.mock import MagicMock

from onyx.connectors.models import BasicExpertInfo
from onyx.connectors.slack.access import get_channel_access
from onyx.connectors.slack.models import ChannelType
from onyx.connectors.slack.source_operations import (
    SlackChannelMembersPage,
    SlackUserInfoResponse,
)

_USERS: dict[str, dict[str, Any]] = {
    "U1": {"real_name": "Ann", "profile": {"email": "Ann@Example.com"}},
    "U2": {"real_name": "Bob", "profile": {"email": "bob@example.com"}},
    # A bot: no email.
    "B1": {"real_name": "Bot", "profile": {}},
}


def _client(members: list[str]) -> MagicMock:
    client = MagicMock()
    client.list_channel_members.return_value = iter(
        [
            SlackChannelMembersPage(ok=True, members=members[:2]),
            SlackChannelMembersPage(ok=True, members=members[2:]),
        ]
    )
    client.fetch_user_info.side_effect = lambda user_id: SlackUserInfoResponse(
        ok=user_id in _USERS, user=_USERS.get(user_id, {})
    )
    return client


def _channel(**fields: Any) -> ChannelType:
    return cast(ChannelType, {"id": "C1", "name": "general", **fields})


def test_public_channel_is_public() -> None:
    client = _client([])
    access = get_channel_access(client, _channel(is_private=False), {})
    assert access.is_public
    assert access.external_user_emails == set()
    assert access.external_user_group_ids == set()
    client.list_channel_members.assert_not_called()


def test_private_channel_has_member_emails() -> None:
    client = _client(["U1", "B1", "U2", "U404"])
    cache: dict[str, BasicExpertInfo | None] = {}
    access = get_channel_access(client, _channel(is_private=True), cache)
    assert not access.is_public
    assert access.external_user_emails == {"ann@example.com", "bob@example.com"}
    assert access.external_user_group_ids == set()
    client.list_channel_members.assert_called_once_with(channel_id="C1")
    assert "U1" in cache


def test_grid_public_channel_is_scoped_to_its_workspaces() -> None:
    client = _client([])
    team_emails = {"T1": {"a@x.com"}, "T2": {"B@x.com"}, "T3": {"c@x.com"}}
    access = get_channel_access(
        client,
        _channel(is_private=False, shared_team_ids=["T1", "T2"]),
        {},
        team_emails,
    )
    assert not access.is_public
    assert access.external_user_emails == {"a@x.com", "b@x.com"}


def test_grid_public_channel_home_team() -> None:
    access = get_channel_access(
        _client([]),
        _channel(is_private=False, context_team_id="T3"),
        {},
        {"T1": {"a@x.com"}, "T3": {"c@x.com"}},
    )
    assert access.external_user_emails == {"c@x.com"}


def test_grid_public_channel_without_team_falls_back_to_members() -> None:
    client = _client(["U2"])
    access = get_channel_access(
        client, _channel(is_private=False), {}, {"T1": {"a@x.com"}}
    )
    assert not access.is_public
    assert access.external_user_emails == {"bob@example.com"}
