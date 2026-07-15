"""
Unit tests -- cw_follow_href validation rules (§4.9, §13.1).

Tests the validation layer without any network calls.
"""

from __future__ import annotations

import os
import pytest

# Note: env vars are set in conftest.py before any cwpsa imports

from cwpsa.tools.tier1.follow_href import (
    _validate_href,
    _strip_to_api_path,
    _sanitize_query_params,
    _map_to_entity,
    _API_PATH_SEGMENT,
)


CW_HOST = "connect.verveit.com"
VALID_BASE = f"https://{CW_HOST}/v4_6_release/apis/3.0"


class TestValidateHref:
    # --- Valid cases ---

    def test_valid_full_url(self):
        assert _validate_href(f"{VALID_BASE}/service/tickets") is None

    def test_valid_full_url_with_id(self):
        assert _validate_href(f"{VALID_BASE}/service/tickets/123") is None

    def test_valid_full_url_sub_resource(self):
        assert _validate_href(f"{VALID_BASE}/company/companies/456/teams") is None

    def test_valid_full_url_with_params(self):
        href = f"{VALID_BASE}/service/tickets?conditions=closedFlag%3Dfalse&pageSize=25"
        assert _validate_href(href) is None

    def test_valid_bare_full_path(self):
        """Bare path with the full versioned API prefix is accepted."""
        assert _validate_href(f"{_API_PATH_SEGMENT}/service/tickets") is None

    def test_bare_short_path_rejected(self):
        """Bare short path without the versioned prefix is NOT accepted.
        _info hrefs from ConnectWise are always full URLs; short paths are not a valid input."""
        err = _validate_href("/service/tickets")
        assert err is not None
        assert err.error.code == "validation_error"

    # --- Invalid cases: Rule 1 — host allowlist ---

    def test_external_url_rejected(self):
        err = _validate_href("https://evil.com/v4_6_release/apis/3.0/service/tickets")
        assert err is not None
        assert err.error.code == "validation_error"
        assert "host" in err.error.message.lower()

    def test_payment_link_rejected(self):
        err = _validate_href("https://wisepay.example.com/pay/invoice/123")
        assert err is not None
        assert err.error.code == "validation_error"

    def test_wrong_cw_subdomain_rejected(self):
        err = _validate_href("https://api-na.myconnectwise.net/v4_6_release/apis/3.0/service/tickets")
        assert err is not None
        assert err.error.code == "validation_error"
        assert "host" in err.error.message.lower()

    # --- Invalid cases: Rule 2 — API path allowlist ---

    def test_non_api_path_rejected(self):
        err = _validate_href(f"https://{CW_HOST}/some/other/path")
        assert err is not None
        assert err.error.code == "validation_error"
        assert "path" in err.error.message.lower()

    def test_bare_path_without_api_prefix_rejected(self):
        err = _validate_href("/not/an/api/path")
        assert err is not None
        assert err.error.code == "validation_error"

    # --- Invalid cases: empty/malformed ---

    def test_empty_href_rejected(self):
        assert _validate_href("") is not None

    def test_whitespace_only_rejected(self):
        assert _validate_href("   ") is not None

    def test_non_url_string_rejected(self):
        err = _validate_href("just some text")
        assert err is not None


class TestStripToApiPath:
    def test_strips_host_and_prefix(self):
        href = f"{VALID_BASE}/service/tickets/123"
        assert _strip_to_api_path(href) == "/service/tickets/123"

    def test_strips_sub_resource(self):
        href = f"{VALID_BASE}/company/companies/456/teams"
        assert _strip_to_api_path(href) == "/company/companies/456/teams"

    def test_handles_query_string(self):
        href = f"{VALID_BASE}/service/tickets?pageSize=25"
        path = _strip_to_api_path(href)
        assert "/service/tickets" in path
        assert "pageSize" not in path


class TestSanitizeQueryParams:
    def test_keeps_allowed_params(self):
        href = f"{VALID_BASE}/service/tickets?conditions=closedFlag%3Dfalse&pageSize=10&orderBy=id+asc"
        params = _sanitize_query_params(href)
        assert "conditions" in params
        assert "pageSize" in params
        assert "orderBy" in params

    def test_strips_unknown_params(self):
        href = f"{VALID_BASE}/service/tickets?maliciousParam=evil&clientId=injected&pageSize=5"
        params = _sanitize_query_params(href)
        assert "maliciousParam" not in params
        assert "clientId" not in params
        assert "pageSize" in params

    def test_empty_query_string(self):
        params = _sanitize_query_params(f"{VALID_BASE}/service/tickets")
        assert params == {}


class TestMapToEntity:
    def test_maps_collection_path(self):
        assert _map_to_entity("/service/tickets") == "service/tickets"

    def test_maps_by_id_path(self):
        assert _map_to_entity("/service/tickets/123") == "service/tickets"

    def test_maps_sub_resource_to_parent(self):
        result = _map_to_entity("/service/tickets/123/notes")
        # Either maps to service/tickets (parent) or returns None — both acceptable
        assert result in ("service/tickets", None)

    def test_maps_company_path(self):
        result = _map_to_entity("/company/companies/456")
        assert result == "company/companies"

    def test_maps_teams_href_to_parent(self):
        result = _map_to_entity("/company/companies/456/teams")
        assert result in ("company/companies", None)

    def test_unknown_path_returns_none(self):
        result = _map_to_entity("/totally/unknown/path")
        assert result is None
