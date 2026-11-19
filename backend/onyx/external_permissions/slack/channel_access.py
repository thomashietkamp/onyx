"""Who may read the threads of a Slack channel. The Slack connector reaches this
through ``onyx/connectors/slack/access.py``.

A private channel is readable by its members. A public channel is readable by
everyone in the workspace, so it is public in Onyx. On Enterprise Grid, a
public channel is readable only in the workspaces it is shared into, so its
readers are the users of those workspaces. Slack user groups are never named:
access resolves to single users, so Slack needs no group sync.
"""

from onyx.access.models import ExternalAccess
from onyx.connectors.models import BasicExpertInfo
from onyx.connectors.slack.connector import channel_team_ids
from onyx.connectors.slack.models import ChannelType
from onyx.connectors.slack.source_operations import SlackSourceOperations
from onyx.connectors.slack.utils import expert_info_from_slack_id


def _member_emails(
    slack_client: SlackSourceOperations,
    channel_id: str,
    user_cache: dict[str, BasicExpertInfo | None],
) -> set[str]:
    """Emails of the channel members. Bots and users without a visible email
    get no access."""
    emails: set[str] = set()
    for page in slack_client.list_channel_members(channel_id=channel_id):
        for member_id in page.members:
            expert = expert_info_from_slack_id(
                member_id, slack_client.fetch_user_info, user_cache
            )
            if expert is not None and expert.email:
                emails.add(expert.email.lower())
    return emails


def get_channel_access(
    slack_client: SlackSourceOperations,
    channel: ChannelType,
    user_cache: dict[str, BasicExpertInfo | None],
    team_id_to_user_emails: dict[str, set[str]] | None = None,
) -> ExternalAccess:
    is_private = channel.get("is_private", False) or channel.get("is_group", False)
    if not is_private:
        if not team_id_to_user_emails:
            return ExternalAccess(
                external_user_emails=set(),
                external_user_group_ids=set(),
                is_public=True,
            )
        team_ids = channel_team_ids(channel)
        if team_ids:
            return ExternalAccess(
                external_user_emails={
                    email.lower()
                    for team_id in team_ids
                    for email in team_id_to_user_emails.get(team_id, set())
                },
                external_user_group_ids=set(),
                is_public=False,
            )
        # A Grid channel with no known workspace: only its members, never
        # the whole organization.

    return ExternalAccess(
        external_user_emails=_member_emails(slack_client, channel["id"], user_cache),
        external_user_group_ids=set(),
        is_public=False,
    )
