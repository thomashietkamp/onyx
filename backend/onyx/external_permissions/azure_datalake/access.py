"""Who may read the paths of one Azure Data Lake Storage Gen2 container.

The connector reaches this through ``onyx/connectors/azure_datalake/access.py``.
Azure grants a data read in this order:

1. Azure RBAC: a role assignment at the container or any scope above it whose
   role holds the blob read data action. Deny assignments remove principals.
   An assignment with an ABAC condition is skipped, because Onyx cannot
   evaluate the condition.
2. Anonymous access: a container with a public access level, in an account
   that permits anonymous reads, is public.
3. POSIX ACLs: a file needs read on the file and execute on every directory
   above it, the container's root included. A directory listing needs read
   and execute on the directory and execute above it. Each path holds its own
   access ACL: default ACL entries only seed new children, so they are not
   read here.

Entra users are emails. Entra groups stay group ids (bare object ids, which
the group sync fills), unless a user must be left out of a group, as with a
named-user entry without the bit or a deny assignment. Then the group is
expanded into its members. "Other" ACL entries reach every signed-in
principal, which maps to public.
"""

import fnmatch
from collections.abc import Callable, Iterable

from pydantic import BaseModel, ConfigDict, Field

from onyx.access.models import ExternalAccess
from onyx.connectors.azure_datalake.access import AzureDataLakeAccessWalker
from onyx.connectors.azure_datalake.models import (
    AzureDataLakeAccessControl,
    AzureDataLakePath,
    AzureDenyAssignment,
    AzurePrincipal,
    AzurePrincipalType,
    AzureRoleAssignment,
    AzureRoleDefinition,
    AzureRolePermission,
    AzureStorageAccountResource,
)
from onyx.connectors.azure_datalake.source_operations import (
    AzureDataLakeSourceOperations,
)
from onyx.connectors.microsoft_utils.entra import (
    EntraDirectoryObject,
    EntraDirectoryObjectType,
    EntraUser,
    iter_entra_items,
)
from onyx.utils.logger import setup_logger

logger = setup_logger()

BLOB_READ_DATA_ACTION = (
    "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read"
)
_SUPERUSER = "$superuser"
_READ = "r"
_EXECUTE = "x"
_MAX_ARM_PAGES = 1_000


class Readers(BaseModel):
    """A set of users. ``everyone`` is every principal except
    ``excluded_users``; otherwise the set is ``users`` plus the members of
    ``groups``. Ids are Entra object ids, lowercase."""

    model_config = ConfigDict(frozen=True)

    everyone: bool = False
    excluded_users: frozenset[str] = Field(default_factory=frozenset)
    users: frozenset[str] = Field(default_factory=frozenset)
    groups: frozenset[str] = Field(default_factory=frozenset)


_NOBODY = Readers()
_EVERYONE = Readers(everyone=True)


class ReaderAlgebra:
    """Set operations on ``Readers``, expanding groups only when a result
    cannot be written with group ids."""

    def __init__(self, group_users: Callable[[str], frozenset[str]]) -> None:
        self._group_users = group_users

    def expand(self, readers: Readers) -> frozenset[str]:
        if readers.everyone:
            raise RuntimeError("Bug: an everyone set has no finite expansion")
        users: set[str] = set(readers.users)
        for group in readers.groups:
            users |= self._group_users(group)
        return frozenset(users)

    def minus_users(self, readers: Readers, removed: frozenset[str]) -> Readers:
        if not removed:
            return readers
        if readers.everyone:
            return Readers(
                everyone=True, excluded_users=readers.excluded_users | removed
            )
        kept_groups: set[str] = set()
        users: set[str] = set(readers.users - removed)
        for group in readers.groups:
            members = self._group_users(group)
            if members & removed:
                users |= members - removed
            else:
                kept_groups.add(group)
        return Readers(users=frozenset(users), groups=frozenset(kept_groups))

    def union(self, a: Readers, b: Readers) -> Readers:
        if a.everyone and b.everyone:
            return Readers(
                everyone=True, excluded_users=a.excluded_users & b.excluded_users
            )
        if b.everyone:
            a, b = b, a
        if a.everyone:
            if not a.excluded_users:
                return a
            return Readers(
                everyone=True, excluded_users=a.excluded_users - self.expand(b)
            )
        return Readers(users=a.users | b.users, groups=a.groups | b.groups)

    def intersect(self, a: Readers, b: Readers) -> Readers:
        if a.everyone and b.everyone:
            return Readers(
                everyone=True, excluded_users=a.excluded_users | b.excluded_users
            )
        if a.everyone:
            return self.minus_users(b, a.excluded_users)
        if b.everyone:
            return self.minus_users(a, b.excluded_users)
        if a == b:
            return a
        return Readers(users=self.expand(a) & self.expand(b))


class _AclEntry(BaseModel):
    kind: str  # "user", "group", "mask" or "other"
    principal: str  # "" for the owning user, the owning group, mask and other
    permissions: str


def parse_acl(access_control: AzureDataLakeAccessControl) -> list[_AclEntry]:
    """The access ACL entries. Without an ACL string, the short form gives
    the owner, owning group and other entries."""
    entries: list[_AclEntry] = []
    for raw in access_control.acl.split(","):
        raw = raw.strip()
        if not raw or raw.startswith("default:"):
            continue
        parts: list[str] = raw.split(":")
        if len(parts) != 3:
            logger.warning("Skipping an unknown ACL entry %s", raw)
            continue
        entries.append(
            _AclEntry(kind=parts[0], principal=parts[1].lower(), permissions=parts[2])
        )
    if not entries and len(access_control.permissions) >= 9:
        short: str = access_control.permissions
        entries = [
            _AclEntry(kind="user", principal="", permissions=short[0:3]),
            _AclEntry(kind="group", principal="", permissions=short[3:6]),
            _AclEntry(kind="other", principal="", permissions=short[6:9]),
        ]
    return entries


def acl_readers(
    access_control: AzureDataLakeAccessControl,
    bits: str,
    algebra: ReaderAlgebra,
) -> Readers:
    """Who the path's ACL gives every one of ``bits``. POSIX takes the first
    matching class: the owner, a named user, the groups (any one that grants
    after the mask), and other only when no earlier class matches."""
    entries = parse_acl(access_control)
    owner: str | None = (
        access_control.owner.lower()
        if access_control.owner and access_control.owner != _SUPERUSER
        else None
    )
    owning_group: str | None = (
        access_control.group.lower()
        if access_control.group and access_control.group != _SUPERUSER
        else None
    )
    mask: str | None = next(
        (entry.permissions for entry in entries if entry.kind == "mask"), None
    )

    def has(permissions: str, masked: bool) -> bool:
        return all(
            bit in permissions and (not masked or mask is None or bit in mask)
            for bit in bits
        )

    owner_grants = False
    named_users: dict[str, bool] = {}
    groups: dict[str, bool] = {}
    other_grants = False
    for entry in entries:
        if entry.kind == "user" and not entry.principal:
            owner_grants = has(entry.permissions, masked=False)
        elif entry.kind == "user":
            named_users[entry.principal] = has(entry.permissions, masked=True)
        elif entry.kind == "group":
            principal = entry.principal or owning_group
            if principal is not None:
                groups[principal] = groups.get(principal, False) or has(
                    entry.permissions, masked=True
                )
        elif entry.kind == "other":
            other_grants = has(entry.permissions, masked=False)

    # Users whose own entry decides, before any group.
    decided: dict[str, bool] = dict(named_users)
    if owner is not None:
        decided[owner] = owner_grants
    granted_users = frozenset(user for user, ok in decided.items() if ok)
    denied_users = frozenset(user for user, ok in decided.items() if not ok)
    granting_groups = frozenset(group for group, ok in groups.items() if ok)

    if other_grants:
        # Group members are decided by their groups, not by other.
        group_denied: set[str] = set()
        for group, ok in groups.items():
            if not ok:
                group_denied |= algebra.expand(Readers(groups=frozenset({group})))
        if group_denied and granting_groups:
            group_denied -= algebra.expand(Readers(groups=granting_groups))
        excluded = (denied_users | frozenset(group_denied)) - granted_users
        return Readers(everyone=True, excluded_users=excluded)

    through_groups = algebra.minus_users(Readers(groups=granting_groups), denied_users)
    return algebra.union(Readers(users=granted_users), through_groups)


def _action_matches(pattern: str, action: str) -> bool:
    return fnmatch.fnmatchcase(action.lower(), pattern.lower())


def permissions_allow(permissions: Iterable[AzureRolePermission], action: str) -> bool:
    return any(
        any(_action_matches(p, action) for p in permission.data_actions)
        and not any(_action_matches(p, action) for p in permission.not_data_actions)
        for permission in permissions
    )


def rbac_readers(
    assignments: list[AzureRoleAssignment],
    role_definition: Callable[[str], AzureRoleDefinition],
    denies: list[AzureDenyAssignment],
    all_users: Callable[[], frozenset[str]],
    algebra: ReaderAlgebra,
    container_scope: str,
) -> Readers:
    """The users and groups whose role assignments grant blob reads, without
    the denied ones."""
    users: set[str] = set()
    groups: set[str] = set()
    for assignment in assignments:
        if assignment.condition:
            logger.info(
                "Skipping role assignment %s: its ABAC condition cannot be evaluated",
                assignment.id,
            )
            continue
        definition = role_definition(assignment.role_definition_id)
        if not permissions_allow(definition.permissions, BLOB_READ_DATA_ACTION):
            continue
        principal = assignment.principal_id.lower()
        if assignment.principal_type == AzurePrincipalType.USER:
            users.add(principal)
        elif assignment.principal_type == AzurePrincipalType.GROUP:
            groups.add(principal)
    granted = Readers(users=frozenset(users), groups=frozenset(groups))

    denied: set[str] = set()
    for deny in denies:
        if not permissions_allow(deny.permissions, BLOB_READ_DATA_ACTION):
            continue
        if (
            deny.do_not_apply_to_child_scopes
            and deny.scope.rstrip("/").lower() != container_scope.lower()
        ):
            continue
        excluded = algebra.expand(_principals(deny.exclude_principals))
        if any(
            p.type in (AzurePrincipalType.EVERYONE, AzurePrincipalType.SYSTEM_DEFINED)
            for p in deny.principals
        ):
            denied |= all_users() - excluded
        else:
            denied |= algebra.expand(_principals(deny.principals)) - excluded
    return algebra.minus_users(granted, frozenset(denied))


def _principals(principals: Iterable[AzurePrincipal]) -> Readers:
    users: set[str] = set()
    groups: set[str] = set()
    for principal in principals:
        if principal.type == AzurePrincipalType.USER:
            users.add(principal.id.lower())
        elif principal.type == AzurePrincipalType.GROUP:
            groups.add(principal.id.lower())
    return Readers(users=frozenset(users), groups=frozenset(groups))


def _user_email(user: EntraUser) -> str | None:
    email = user.mail or user.user_principal_name
    return email.strip().lower() if email and "@" in email else None


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


class AzureDataLakeAclWalker(AzureDataLakeAccessWalker):
    """Reads each path's ACL once. Directories are read before their
    children, so the execute set of each parent is cached."""

    def __init__(
        self,
        ops: AzureDataLakeSourceOperations,
        rbac: Readers,
        anonymous_read: bool,
        algebra: ReaderAlgebra,
        emails: dict[str, str],
    ) -> None:
        self._ops = ops
        self._rbac = rbac
        self._anonymous_read = anonymous_read
        self._algebra = algebra
        self._emails = emails
        # Directory path ("" is the root) to who may traverse it and every
        # directory above it.
        self._traverse: dict[str, Readers] = {}
        self._directory_acls: dict[str, AzureDataLakeAccessControl] = {}

    def _acl(self, path: str, is_directory: bool) -> AzureDataLakeAccessControl:
        if not is_directory:
            return self._ops.get_access_control(path=path, is_directory=False)
        if path not in self._directory_acls:
            self._directory_acls[path] = self._ops.get_access_control(
                path=path, is_directory=True
            )
        return self._directory_acls[path]

    def _traversal(self, directory: str) -> Readers:
        if directory not in self._traverse:
            own = acl_readers(self._acl(directory, True), _EXECUTE, self._algebra)
            above = (
                _EVERYONE if directory == "" else self._traversal(_parent(directory))
            )
            self._traverse[directory] = self._algebra.intersect(own, above)
        return self._traverse[directory]

    def _to_access(self, acl: Readers) -> ExternalAccess:
        if self._anonymous_read:
            return ExternalAccess.public()
        readers = self._algebra.union(self._rbac, acl)
        if readers.everyone and not readers.excluded_users:
            return ExternalAccess.public()
        if readers.everyone:
            users = frozenset(self._emails) - readers.excluded_users
            groups: frozenset[str] = frozenset()
        else:
            users, groups = readers.users, readers.groups
        return ExternalAccess(
            external_user_emails={
                self._emails[user] for user in users if user in self._emails
            },
            external_user_group_ids=set(groups),
            is_public=False,
        )

    def _listing(self, directory: str) -> Readers:
        own = acl_readers(self._acl(directory, True), _READ, self._algebra)
        return self._algebra.intersect(own, self._traversal(directory))

    def container_access(self) -> ExternalAccess:
        return self._to_access(self._listing(""))

    def directory_access(self, path: str) -> ExternalAccess:
        return self._to_access(self._listing(path))

    def page_access(self, paths: list[AzureDataLakePath]) -> dict[str, ExternalAccess]:
        result: dict[str, ExternalAccess] = {}
        for path in paths:
            if path.is_directory:
                result[path.name] = self.directory_access(path.name)
                continue
            own = acl_readers(self._acl(path.name, False), _READ, self._algebra)
            readers = self._algebra.intersect(own, self._traversal(_parent(path.name)))
            result[path.name] = self._to_access(readers)
        return result


def _is_user(member: EntraDirectoryObject) -> bool:
    return member.odata_type in (
        EntraDirectoryObjectType.USER,
        EntraDirectoryObjectType.USER.value,
    )


def _find_storage_account(
    ops: AzureDataLakeSourceOperations, account_name: str
) -> AzureStorageAccountResource:
    """The account's ARM resource, searched in every subscription the app
    holds a role in."""
    wanted = account_name.lower()
    subscription_link: str | None = None
    for _ in range(_MAX_ARM_PAGES):
        subscriptions = ops.list_subscriptions(next_link=subscription_link)
        for subscription in subscriptions.subscriptions:
            account_link: str | None = None
            for _ in range(_MAX_ARM_PAGES):
                accounts = ops.list_storage_accounts(
                    subscription_id=subscription.subscription_id,
                    next_link=account_link,
                )
                for account in accounts.accounts:
                    if account.name.lower() == wanted:
                        return account
                if not accounts.next_link or accounts.next_link == account_link:
                    break
                account_link = accounts.next_link
        if not subscriptions.next_link or subscriptions.next_link == subscription_link:
            break
        subscription_link = subscriptions.next_link
    raise RuntimeError(
        f"The app registration cannot see the storage account '{account_name}' in "
        "Azure Resource Manager. Permission sync needs a role that reads it, "
        "such as Reader on the account."
    )


def build_access_walker(
    ops: AzureDataLakeSourceOperations,
    account_name: str,
    container_name: str,
) -> AzureDataLakeAccessWalker:
    group_cache: dict[str, frozenset[str]] = {}

    def group_users(group_id: str) -> frozenset[str]:
        if group_id not in group_cache:
            group_cache[group_id] = frozenset(
                member.id.lower()
                for member in iter_entra_items(
                    lambda link: ops.list_transitive_group_members(
                        group_id=group_id, next_link=link
                    ),
                    f"members of Entra group {group_id}",
                )
                if _is_user(member)
            )
        return group_cache[group_id]

    users: list[EntraUser] = list(
        iter_entra_items(lambda link: ops.list_users(next_link=link), "Entra users")
    )
    emails: dict[str, str] = {
        user.id.lower(): email for user in users if (email := _user_email(user))
    }
    algebra = ReaderAlgebra(group_users)

    account = _find_storage_account(ops, account_name)
    scope = f"{account.id}/blobServices/default/containers/{container_name}"
    definitions: dict[str, AzureRoleDefinition] = {}

    def role_definition(role_definition_id: str) -> AzureRoleDefinition:
        if role_definition_id not in definitions:
            definitions[role_definition_id] = ops.get_role_definition(
                role_definition_id=role_definition_id
            )
        return definitions[role_definition_id]

    rbac = rbac_readers(
        ops.list_role_assignments(scope=scope),
        role_definition,
        ops.list_deny_assignments(scope=scope),
        lambda: frozenset(user.id.lower() for user in users),
        algebra,
        scope,
    )
    anonymous_read = (
        account.permits_anonymous_reads and ops.get_container_access().anonymous_read
    )
    return AzureDataLakeAclWalker(ops, rbac, anonymous_read, algebra, emails)
