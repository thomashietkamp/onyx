"""The SCIM filters IdPs send in practice: one attribute compared with ``eq``,
for example ``userName eq "a@b.com"`` or ``externalId eq 'x'``. Anything else is
refused with ``invalidFilter`` rather than ignored, so an IdP never mistakes an
unfiltered list for a match."""

import re

from onyx.server.scim.models import ScimError

_FILTER_RE = re.compile(
    r"""^\s*(?P<attribute>[A-Za-z][\w.]*)\s+eq\s+(?:"(?P<dq>(?:[^"\\]|\\.)*)"|'(?P<sq>(?:[^'\\]|\\.)*)')\s*$""",
    re.IGNORECASE,
)


def parse_eq_filter(
    filter_expression: str | None, supported: set[str]
) -> tuple[str | None, str | None]:
    """(lower-cased attribute, value), or (None, None) without a filter."""
    if not filter_expression:
        return None, None
    match = _FILTER_RE.match(filter_expression)
    if match is None:
        raise ScimError(
            400, f"Unsupported filter: {filter_expression}", "invalidFilter"
        )
    attribute = match.group("attribute").lower()
    if attribute not in supported:
        raise ScimError(
            400,
            f"Filtering on {match.group('attribute')} is not supported.",
            "invalidFilter",
        )
    raw = match.group("dq") if match.group("dq") is not None else match.group("sq")
    return attribute, re.sub(r"\\(.)", r"\1", raw or "")


_MEMBER_PATH_RE = re.compile(
    r"""^members\[\s*value\s+eq\s+["'](?P<value>[^"']+)["']\s*\]$""", re.IGNORECASE
)


def parse_member_path(path: str) -> str | None:
    """The member id in ``members[value eq "<id>"]``, else None."""
    match = _MEMBER_PATH_RE.match(path.strip())
    return match.group("value") if match else None
