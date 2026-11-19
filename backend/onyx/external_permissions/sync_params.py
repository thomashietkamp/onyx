"""Which sources support permission sync, and how often each sync runs.

Doc sync copies each document's ACL from the source (through the connector's
``retrieve_all_slim_docs_perm_sync``). Group sync copies the members of the
source's groups, which document ACLs name as ``external_group:`` entries. A
source is listed here only once its connector computes real ACLs in MIT code;
an unlisted source cannot be set to Sync, because its documents would show up
with an empty ACL and be hidden.

``onyx/utils/variable_functionality.py:fetch_ee_implementation_or_noop`` reaches
these functions by name from the pairing, scheduler and telemetry code.
"""

from collections.abc import Callable, Iterator
from importlib import import_module

from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from onyx.configs.constants import DocumentSource
from onyx.db.enums import ConnectorCredentialPairStatus
from onyx.db.external_perm import ExternalUserGroup
from onyx.db.models import ConnectorCredentialPair

DEFAULT_DOC_SYNC_SECONDS = 30 * 60
DEFAULT_GROUP_SYNC_SECONDS = 30 * 60

GroupSyncFn = Callable[[Session, ConnectorCredentialPair], Iterator[ExternalUserGroup]]


class SyncConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    doc_sync: bool = True
    doc_sync_seconds: int = DEFAULT_DOC_SYNC_SECONDS
    # Indexing already reads permissions, so the first doc sync waits for the
    # first successful index instead of running against no documents.
    initial_index_should_sync: bool = True
    # "module:function" of the group sync, loaded on first use so this module
    # imports no SDK.
    group_sync: str | None = None
    group_sync_seconds: int = DEFAULT_GROUP_SYNC_SECONDS
    # One group sync serves every pair of the source (one tenant directory).
    group_sync_is_cc_pair_agnostic: bool = False


_MICROSOFT = "onyx.external_permissions.microsoft.group_sync"

_SOURCE_TO_SYNC_CONFIG: dict[DocumentSource, SyncConfig] = {
    DocumentSource.SHAREPOINT: SyncConfig(
        group_sync=f"{_MICROSOFT}:sharepoint_group_sync"
    ),
    DocumentSource.ONEDRIVE: SyncConfig(group_sync=f"{_MICROSOFT}:onedrive_group_sync"),
    DocumentSource.TEAMS: SyncConfig(group_sync=f"{_MICROSOFT}:teams_group_sync"),
    # Mail readers are people, never groups.
    DocumentSource.OUTLOOK: SyncConfig(),
    DocumentSource.GMAIL: SyncConfig(),
    DocumentSource.GOOGLE_DRIVE: SyncConfig(
        group_sync="onyx.external_permissions.google_drive.group_sync:gdrive_group_sync"
    ),
    # Indexing does not read permissions (no CheckpointedConnectorWithPermSync).
    DocumentSource.CONFLUENCE: SyncConfig(
        initial_index_should_sync=False,
        group_sync="onyx.external_permissions.confluence.group_sync:confluence_group_sync",
    ),
    # Channel access resolves to single users.
    DocumentSource.SLACK: SyncConfig(),
    DocumentSource.GITHUB: SyncConfig(
        group_sync="onyx.external_permissions.github.group_sync:github_group_sync"
    ),
    # Indexing does not read permissions (a load and poll connector, not
    # CheckpointedConnectorWithPermSync).
    DocumentSource.GITLAB: SyncConfig(
        initial_index_should_sync=False,
        group_sync="onyx.external_permissions.gitlab.group_sync:gitlab_group_sync",
    ),
    DocumentSource.BOX: SyncConfig(
        group_sync="onyx.external_permissions.box.group_sync:box_group_sync"
    ),
    DocumentSource.CANVAS: SyncConfig(
        group_sync="onyx.external_permissions.canvas.group_sync:canvas_group_sync"
    ),
    DocumentSource.EGNYTE: SyncConfig(
        group_sync="onyx.external_permissions.egnyte.group_sync:egnyte_group_sync"
    ),
    DocumentSource.LINEAR: SyncConfig(
        initial_index_should_sync=False,
        group_sync="onyx.external_permissions.linear.group_sync:linear_group_sync",
    ),
    DocumentSource.YOUTRACK: SyncConfig(
        initial_index_should_sync=False,
        group_sync="onyx.external_permissions.youtrack.group_sync:youtrack_group_sync",
    ),
    DocumentSource.AZURE_DATALAKE: SyncConfig(
        initial_index_should_sync=False,
        group_sync=(
            "onyx.external_permissions.azure_datalake.group_sync:"
            "azure_datalake_group_sync"
        ),
    ),
    DocumentSource.ZOOM: SyncConfig(
        group_sync="onyx.external_permissions.zoom.group_sync:zoom_group_sync"
    ),
    DocumentSource.HUBSPOT: SyncConfig(initial_index_should_sync=False),
    # Not listed: JIRA (issue security levels are not read, so every issue
    # would get its project's access) and SALESFORCE (records need
    # post-query censoring, which this fork does not have).
}


def get_sync_config(source: DocumentSource) -> SyncConfig | None:
    return _SOURCE_TO_SYNC_CONFIG.get(source)


def check_if_valid_sync_source(source: DocumentSource) -> bool:
    return source in _SOURCE_TO_SYNC_CONFIG


def source_requires_doc_sync(source: DocumentSource) -> bool:
    config = get_sync_config(source)
    return config is not None and config.doc_sync


def source_requires_external_group_sync(source: DocumentSource) -> bool:
    config = get_sync_config(source)
    return config is not None and config.group_sync is not None


def source_should_fetch_permissions_during_indexing(source: DocumentSource) -> bool:
    config = get_sync_config(source)
    return config is not None and config.initial_index_should_sync


def source_group_sync_is_cc_pair_agnostic(source: DocumentSource) -> bool:
    config = get_sync_config(source)
    return config is not None and config.group_sync_is_cc_pair_agnostic


def get_all_cc_pair_agnostic_group_sync_sources() -> set[DocumentSource]:
    return {
        source
        for source, config in _SOURCE_TO_SYNC_CONFIG.items()
        if config.group_sync is not None and config.group_sync_is_cc_pair_agnostic
    }


def load_group_sync_fn(source: DocumentSource) -> GroupSyncFn | None:
    config = get_sync_config(source)
    if config is None or config.group_sync is None:
        return None
    module_name, _, attribute = config.group_sync.partition(":")
    function: GroupSyncFn = getattr(  # ods: ignore[getattr] registry by name
        import_module(module_name), attribute
    )
    return function


def _runs(status: ConnectorCredentialPairStatus) -> bool:
    return status in (
        ConnectorCredentialPairStatus.ACTIVE,
        ConnectorCredentialPairStatus.SCHEDULED,
        ConnectorCredentialPairStatus.INITIAL_INDEXING,
    )


def doc_permission_sync_seconds(
    source: DocumentSource,
    status: ConnectorCredentialPairStatus,
    has_succeeded: bool,
) -> int | None:
    """Seconds between doc syncs of a pair, or None when none runs (an
    unsupported source, a paused pair, or a pair still waiting for its first
    successful index)."""
    config = get_sync_config(source)
    if config is None or not config.doc_sync or not _runs(status):
        return None
    if config.initial_index_should_sync and not has_succeeded:
        return None
    return config.doc_sync_seconds


def external_group_sync_seconds(
    source: DocumentSource, status: ConnectorCredentialPairStatus
) -> int | None:
    config = get_sync_config(source)
    if config is None or config.group_sync is None or not _runs(status):
        return None
    return config.group_sync_seconds
