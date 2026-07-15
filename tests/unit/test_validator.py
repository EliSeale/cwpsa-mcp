"""
Unit tests — filter validator against the real registry (§13.1).

Tests rely on registry.json being present (produced by scripts/build_registry.py).
Skipped automatically if the registry hasn't been built yet.
"""

from __future__ import annotations

import pytest
from cwpsa.filter import FilterDSL
from cwpsa.filter.validator import validate_filter
from cwpsa.registry.loader import get_registry


def _get_tickets():
    try:
        return get_registry().get_entity("service/tickets")
    except Exception:
        return None


@pytest.fixture
def tickets():
    e = _get_tickets()
    if e is None or not e.fields:
        pytest.skip("registry.json not built or service/tickets has no fields.")
    return e


class TestValidatorWithRegistry:
    def test_valid_filter_returns_none(self, tickets):
        dsl = FilterDSL.model_validate({
            "conditions": {"field": "closedFlag", "op": "=", "value": False}
        })
        result = validate_filter(dsl, tickets)
        assert result is None

    def test_unknown_field_returns_error(self, tickets):
        dsl = FilterDSL.model_validate({
            "conditions": {"field": "unknownXYZField", "op": "=", "value": "x"}
        })
        result = validate_filter(dsl, tickets)
        assert result is not None
        assert result.error.code == "validation_error"
        assert "unknownXYZField" in result.error.message

    def test_did_you_mean_suggestion(self, tickets):
        # "summry" is close to "summary"
        dsl = FilterDSL.model_validate({
            "conditions": {"field": "summry", "op": "=", "value": "x"}
        })
        result = validate_filter(dsl, tickets)
        # Should suggest "summary"
        assert result is not None
        suggestions = (result.error.details.suggestions or [])
        assert any("summary" in s for s in suggestions)

    def test_non_filterable_text_field_rejected(self, tickets):
        dsl = FilterDSL.model_validate({
            "conditions": {"field": "initialDescription", "op": "contains", "value": "error"}
        })
        result = validate_filter(dsl, tickets)
        assert result is not None
        assert "not filterable" in result.error.message

    def test_valid_ref_field_filter(self, tickets):
        dsl = FilterDSL.model_validate({
            "conditions": {"field": "company", "op": "=", "value": "ACME"}
        })
        # company is a ref field and is filterable — should pass
        result = validate_filter(dsl, tickets)
        assert result is None

    def test_valid_enum_value(self, tickets):
        dsl = FilterDSL.model_validate({
            "conditions": {"field": "billTime", "op": "=", "value": "Billable"}
        })
        result = validate_filter(dsl, tickets)
        assert result is None

    def test_invalid_enum_value(self, tickets):
        dsl = FilterDSL.model_validate({
            "conditions": {"field": "billTime", "op": "=", "value": "YesPlease"}
        })
        result = validate_filter(dsl, tickets)
        assert result is not None
        assert result.error.code == "validation_error"
        allowed = result.error.details.allowed_values or []
        assert "Billable" in allowed
