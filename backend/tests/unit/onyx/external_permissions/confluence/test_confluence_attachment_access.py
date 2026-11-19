"""An attachment inherits the read restrictions of its page's ancestors in the
Confluence permission sync listing."""

from typing import Any
from unittest import mock

from onyx.access.models import ExternalAccess
from onyx.connectors.confluence.connector import ConfluenceConnector
from onyx.connectors.confluence.source_operations import _OnyxConfluence
from onyx.connectors.models import SlimDocument
from onyx.external_permissions.confluence import page_access
from tests.unit.onyx.connectors.confluence.confluence_gateway_fakes import (
    gateway_with_client,
)

_PAGE_CQL = "PAGE_CQL"
_ATTACHMENT_CQL = "ATTACHMENT_CQL"
_CREATED = {"createdDate": "2023-01-01T12:00:00.000+0000"}
_RESTRICTED_ANCESTOR = {
    "id": "100",
    "restrictions": {
        "read": {
            "restrictions": {
                "user": {"results": [{"accountId": "1", "email": "ann@x.com"}]},
                "group": {"results": []},
            }
        }
    },
}
_PAGE = {
    "id": "111",
    "_links": {"webui": "/spaces/X/pages/111/Page"},
    "restrictions": {},
    "space": {"key": "X"},
    "ancestors": [_RESTRICTED_ANCESTOR],
    "history": _CREATED,
}
_ATTACHMENT = {
    "title": "spec.pdf",
    "metadata": {"mediaType": "application/pdf"},
    "_links": {"webui": "/download/attachments/111/spec.pdf"},
    "restrictions": {},
    "space": {"key": "X"},
    "history": _CREATED,
}


def test_attachment_inherits_ancestor_restrictions() -> None:
    connector = ConfluenceConnector(
        wiki_base="https://fake-cloud.atlassian.net/wiki", is_cloud=True
    )

    def fake_paginate(**kwargs: Any) -> Any:
        cql = kwargs.get("cql")
        if cql == _PAGE_CQL:
            return iter([_PAGE])
        if cql == _ATTACHMENT_CQL:
            return iter([_ATTACHMENT])
        return iter([])

    fake_client = mock.Mock(spec=_OnyxConfluence)
    fake_client.cql_paginate_all_expansions.side_effect = fake_paginate

    with (
        mock.patch.object(
            ConfluenceConnector,
            "source_operations",
            new_callable=mock.PropertyMock,
            return_value=gateway_with_client(fake_client),
        ),
        mock.patch(
            "onyx.connectors.confluence.connector.get_all_space_permissions",
            return_value={"X": ExternalAccess.public()},
        ),
        mock.patch.object(page_access, "is_cloud_gateway", return_value=True),
        mock.patch.object(
            connector, "_yield_space_hierarchy_nodes", return_value=iter([])
        ),
        mock.patch.object(
            connector, "_yield_ancestor_hierarchy_nodes", return_value=iter([])
        ),
        mock.patch.object(
            connector, "_maybe_yield_page_hierarchy_node", return_value=None
        ),
        mock.patch.object(connector, "_get_parent_hierarchy_raw_id", return_value=None),
        mock.patch.object(
            connector, "_construct_page_cql_query", return_value=_PAGE_CQL
        ),
        mock.patch.object(
            connector, "_construct_attachment_query", return_value=_ATTACHMENT_CQL
        ),
    ):
        docs = [
            item
            for batch in connector.retrieve_all_slim_docs_perm_sync()
            for item in batch
            if isinstance(item, SlimDocument)
        ]

    expected = ExternalAccess(
        external_user_emails={"ann@x.com"},
        external_user_group_ids=set(),
        is_public=False,
    )
    assert len(docs) == 2
    for doc in docs:
        assert doc.external_access == expected, doc.id
