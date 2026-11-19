"""Which permission-sync capabilities a source has in this fork.

A light module: it reads the sync registry only and imports no per-connector
check module, because the blocking-validation recorder resolves applicability
on the docfetching hot path. ``capability_checks/applicability.py`` reaches it
by name through ``fetch_ee_implementation_or_noop``.
"""

from onyx.configs.constants import DocumentSource
from onyx.connectors.capabilities import CredentialCapability
from onyx.external_permissions.sync_params import (
    source_requires_doc_sync,
    source_requires_external_group_sync,
)


def get_applicable_perm_sync_capabilities(
    source: DocumentSource,
) -> set[CredentialCapability]:
    capabilities: set[CredentialCapability] = set()
    if source_requires_doc_sync(source):
        capabilities.add(CredentialCapability.DOC_PERMISSION_SYNC)
    if source_requires_external_group_sync(source):
        capabilities.add(CredentialCapability.EXTERNAL_GROUP_SYNC)
    return capabilities
