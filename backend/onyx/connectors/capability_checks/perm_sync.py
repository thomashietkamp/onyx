"""Named permission-sync checks per source. They run only for a source that
the sync registry supports, and only for permission-synced access types
(``CapabilityCheck.access_types``)."""

from collections.abc import Callable

from onyx.configs.constants import DocumentSource
from onyx.connectors.capability_checks.models import CapabilityCheck
from onyx.connectors.confluence.capability_checks import (
    build_confluence_doc_permission_sync_checks,
    build_confluence_group_sync_checks,
)
from onyx.connectors.github.capability_checks import (
    build_github_doc_permission_sync_checks,
    build_github_group_sync_checks,
)
from onyx.connectors.google_drive.capability_checks import (
    build_google_drive_doc_permission_sync_checks,
    build_google_drive_group_sync_checks,
)
from onyx.connectors.hubspot.capability_checks import (
    build_hubspot_doc_permission_sync_checks,
)
from onyx.connectors.onedrive.capability_checks import (
    build_onedrive_doc_permission_sync_checks,
    build_onedrive_group_sync_checks,
)
from onyx.connectors.outlook.capability_checks import (
    build_outlook_doc_permission_sync_checks,
)
from onyx.connectors.sharepoint.capability_checks import (
    build_sharepoint_doc_permission_sync_checks,
    build_sharepoint_group_sync_checks,
)
from onyx.connectors.slack.capability_checks import (
    build_slack_doc_permission_sync_checks,
)
from onyx.external_permissions.sync_params import check_if_valid_sync_source

_BUILDERS: dict[DocumentSource, list[Callable[[], list[CapabilityCheck]]]] = {
    DocumentSource.SLACK: [build_slack_doc_permission_sync_checks],
    DocumentSource.OUTLOOK: [build_outlook_doc_permission_sync_checks],
    DocumentSource.HUBSPOT: [build_hubspot_doc_permission_sync_checks],
    DocumentSource.GOOGLE_DRIVE: [
        build_google_drive_doc_permission_sync_checks,
        build_google_drive_group_sync_checks,
    ],
    DocumentSource.SHAREPOINT: [
        build_sharepoint_doc_permission_sync_checks,
        build_sharepoint_group_sync_checks,
    ],
    DocumentSource.CONFLUENCE: [
        build_confluence_doc_permission_sync_checks,
        build_confluence_group_sync_checks,
    ],
    DocumentSource.ONEDRIVE: [
        build_onedrive_doc_permission_sync_checks,
        build_onedrive_group_sync_checks,
    ],
    DocumentSource.GITHUB: [
        build_github_doc_permission_sync_checks,
        build_github_group_sync_checks,
    ],
}


def get_perm_sync_capability_checks(source: DocumentSource) -> list[CapabilityCheck]:
    # A source that cannot be set to Sync (for example Jira) gets no
    # permission-sync rows.
    if not check_if_valid_sync_source(source):
        return []
    return [check for build in _BUILDERS.get(source, []) for check in build()]
