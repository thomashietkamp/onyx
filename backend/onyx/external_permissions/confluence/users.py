"""Maps a Confluence user, as restrictions and space permissions return it, to
an email. Cloud returns the email inline when the user's profile shows it;
Data Center needs a lookup by userKey or username."""

from typing import Any

from onyx.connectors.confluence.source_operations import (
    ConfluenceSourceOperations,
    ConfluenceUserEmailVariant,
    ConfluenceUserListVariant,
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
