"""Group sync for GitHub.

For each configured repository, yields the group its documents name (see
``utils.py``): the collaborators of a private repository, or the members of
the organization of an internal one. GitHub users map to Onyx users by the
public email of their profile; a user without one gets no access.
"""

from collections.abc import Callable, Iterator
from functools import partial
from typing import TypeVar

from sqlalchemy.orm import Session

from onyx.connectors.factory import instantiate_connector
from onyx.connectors.github.connector import GithubConnector
from onyx.connectors.github.rate_limit_utils import sleep_after_rate_limit_exception
from onyx.connectors.github.source_operations import (
    GitHubApiError,
    GitHubRateLimitError,
    GitHubSourceOperations,
)
from onyx.db.external_perm import ExternalUserGroup
from onyx.db.models import ConnectorCredentialPair
from onyx.external_permissions.github.utils import (
    VISIBILITY_INTERNAL,
    is_collaborator_scoped,
    organization_members_group_id,
    repository_collaborators_group_id,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()

_MAX_RATE_LIMIT_RETRIES = 5

_T = TypeVar("_T")


def _rate_limited(ops: GitHubSourceOperations, operation: Callable[[], _T]) -> _T:
    attempt = 0
    while True:
        try:
            return operation()
        except GitHubRateLimitError:
            if attempt >= _MAX_RATE_LIMIT_RETRIES:
                raise
            attempt += 1
            sleep_after_rate_limit_exception(ops)


class _EmailResolver:
    def __init__(self, ops: GitHubSourceOperations) -> None:
        self._ops = ops
        self._cache: dict[str, str | None] = {}

    def email(self, login: str) -> str | None:
        if login not in self._cache:
            try:
                user = _rate_limited(
                    self._ops, partial(self._ops.get_user, login=login)
                )
                self._cache[login] = user.email.lower() if user.email else None
            except GitHubRateLimitError:
                raise
            except GitHubApiError as e:
                logger.warning("Cannot read GitHub profile %s: %s", login, e)
                self._cache[login] = None
        return self._cache[login]

    def emails(self, logins: list[str]) -> list[str]:
        return sorted({email for login in logins if (email := self.email(login))})


def iter_github_groups(connector: GithubConnector) -> Iterator[ExternalUserGroup]:
    ops = connector.source_operations
    resolver = _EmailResolver(ops)
    seen_orgs: set[int] = set()
    for repo in _rate_limited(ops, connector.fetch_configured_repos):
        if is_collaborator_scoped(repo):
            logins = _rate_limited(
                ops,
                partial(
                    ops.list_repository_collaborators, repo_full_name=repo.full_name
                ),
            )
            yield ExternalUserGroup(
                id=repository_collaborators_group_id(repo.id),
                user_emails=resolver.emails(logins),
            )
        elif (
            repo.visibility == VISIBILITY_INTERNAL
            and repo.organization is not None
            and repo.organization.id not in seen_orgs
        ):
            org = repo.organization
            seen_orgs.add(org.id)
            logins = _rate_limited(
                ops, partial(ops.list_organization_members, org_login=org.login)
            )
            yield ExternalUserGroup(
                id=organization_members_group_id(org.id),
                user_emails=resolver.emails(logins),
            )


def github_group_sync(
    db_session: Session, cc_pair: ConnectorCredentialPair
) -> Iterator[ExternalUserGroup]:
    connector = instantiate_connector(
        db_session=db_session,
        source=cc_pair.connector.source,
        input_type=cc_pair.connector.input_type,
        connector_specific_config=cc_pair.connector.connector_specific_config,
        credential=cc_pair.credential,
    )
    if not isinstance(connector, GithubConnector):
        raise RuntimeError("Bug: a GitHub pair built a non-GitHub connector")
    yield from iter_github_groups(connector)
