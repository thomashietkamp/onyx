import pytest

from onyx.server.scim.filtering import parse_eq_filter, parse_member_path
from onyx.server.scim.models import ScimError, ScimPatchRequest, ScimUserResource


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ('userName eq "a@b.com"', ("username", "a@b.com")),
        ("userName eq 'a@b.com'", ("username", "a@b.com")),
        ('externalId EQ "x\\"y"', ("externalid", 'x"y')),
        (None, (None, None)),
    ],
)
def test_eq_filters(
    expression: str | None, expected: tuple[str | None, str | None]
) -> None:
    assert parse_eq_filter(expression, {"username", "externalid"}) == expected


@pytest.mark.parametrize(
    "expression", ['userName co "a"', 'userName eq "a" and active eq true', "garbage"]
)
def test_unsupported_filters_are_refused_not_ignored(expression: str) -> None:
    with pytest.raises(ScimError) as error:
        parse_eq_filter(expression, {"username"})
    assert error.value.status == 400


def test_filter_on_unknown_attribute_is_refused() -> None:
    with pytest.raises(ScimError):
        parse_eq_filter('title eq "x"', {"username"})


def test_member_path() -> None:
    assert parse_member_path('members[value eq "abc-1"]') == "abc-1"
    assert parse_member_path("members[value eq 'abc-1']") == "abc-1"
    assert parse_member_path("displayName") is None


def test_entra_capitalized_ops_are_normalized() -> None:
    request = ScimPatchRequest.model_validate(
        {
            "schemas": ["urn:ietf:params:scim:api:messages:2.0:PatchOp"],
            "Operations": [{"op": "Replace", "path": "active", "value": "False"}],
        }
    )
    assert request.operations[0].op == "replace"


def test_unknown_op_is_rejected() -> None:
    with pytest.raises(ValueError):
        ScimPatchRequest.model_validate({"Operations": [{"op": "move"}]})


def test_string_booleans_from_entra() -> None:
    user = ScimUserResource.model_validate({"userName": "a@b.com", "active": "False"})
    assert user.active is False
