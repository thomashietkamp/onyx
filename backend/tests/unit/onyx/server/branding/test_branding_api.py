"""The public branding routes: no login, and a logo that never runs as script."""

from unittest.mock import patch

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from onyx.error_handling.exceptions import register_onyx_exception_handlers
from onyx.server.auth_check import PUBLIC_ENDPOINT_SPECS, is_route_in_spec_list
from onyx.server.branding import api
from onyx.server.branding.models import BrandingSettingsResponse


def _client() -> TestClient:
    app = FastAPI()
    register_onyx_exception_handlers(app)
    app.include_router(api.public_router)
    app.include_router(api.admin_router)
    return TestClient(app)


def test_read_routes_are_public_and_writes_are_not() -> None:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(api.public_router)
    app.include_router(api.admin_router)
    public = {
        route.path
        for route in app.routes
        if isinstance(route, APIRoute)
        and is_route_in_spec_list(route, PUBLIC_ENDPOINT_SPECS)
    }
    assert public == {
        "/enterprise-settings",
        "/enterprise-settings/logo",
        "/enterprise-settings/logotype",
        "/enterprise-settings/custom-analytics-script",
    }


def test_settings_read_needs_no_login() -> None:
    with patch.object(
        api,
        "branding_response",
        return_value=BrandingSettingsResponse(application_name="Acme"),
    ):
        response = _client().get("/enterprise-settings")
    assert response.status_code == 200
    assert response.json()["application_name"] == "Acme"


def test_logo_is_served_as_an_inert_raster() -> None:
    with patch.object(api, "read_logo", return_value=(b"\x89PNG", "image/png")):
        response = _client().get("/enterprise-settings/logo")
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]


def test_a_stored_non_raster_type_is_not_served_inline() -> None:
    with patch.object(api, "read_logo", return_value=(b"<svg/>", "image/svg+xml")):
        response = _client().get("/enterprise-settings/logo")
    assert not response.headers["content-type"].startswith("image/svg")


def test_missing_logo_is_404() -> None:
    with patch.object(api, "read_logo", return_value=None):
        response = _client().get("/enterprise-settings/logotype")
    assert response.status_code == 404
