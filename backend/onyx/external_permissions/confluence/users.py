"""Maps a Confluence user, as restrictions and space permissions return it, to
an email. Cloud returns the email inline when the user's profile shows it;
Data Center needs a lookup by userKey or username.

Also maps a Confluence group name to its raw group id. Group names such as
``confluence-users`` repeat across sites, and a user's external groups are
matched across every pair, so the id carries a hash of the site URL."""

from typing import Any

from onyx.access.utils import build_instance_scoped_group_id
from onyx.connectors.confluence.source_operations import (
    ConfluenceSourceOperations,
    ConfluenceUserEmailVariant,
    ConfluenceUserListVariant,
    bound_site_url,
    user_list_variant,
)


def confluence_user_email(
    source_operations: ConfluenceSourceOperations,
    user: dict[str, Any],
    *,
    is_cloud: bool,
) -> str | None:
    """The lowercased email of a user entry, or None when none is visible."""
    email = user.get("email")
    if isinstance(email, str) and email:
        return email.lower()
    if is_cloud:
        return None
    user_key = user.get("userKey")
    if isinstance(user_key, str) and user_key:
        email = source_operations.get_user_email(
            variant=ConfluenceUserEmailVariant.USERKEY, user=user_key
        )
    else:
        username = user.get("username")
        if not isinstance(username, str) or not username:
            return None
        email = source_operations.get_user_email(
            variant=ConfluenceUserEmailVariant.USERNAME, user=username
        )
    return email.lower() if email else None


def is_cloud_gateway(source_operations: ConfluenceSourceOperations) -> bool:
    return user_list_variant(source_operations) == ConfluenceUserListVariant.CLOUD


def confluence_group_id(site_url: str, group_name: str) -> str:
    """The raw id of a Confluence group of the site at ``site_url``."""
    return build_instance_scoped_group_id(site_url, group_name)


def confluence_group_ids(
    source_operations: ConfluenceSourceOperations, group_names: set[str]
) -> set[str]:
    """The raw ids of groups of the gateway's bound site."""
    site_url = bound_site_url(source_operations)
    return {confluence_group_id(site_url, name) for name in group_names}
