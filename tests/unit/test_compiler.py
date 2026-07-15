"""
Unit tests — filter DSL compiler (§13.1).

Golden tests: fixed DSL input → exact expected conditions strings.
Property tests: injection invariant, offset-datetime rejection, round-trip.

Run: pytest tests/unit/test_compiler.py -v
"""

from __future__ import annotations

import pytest
from cwpsa.filter import FilterDSL
from cwpsa.filter.compiler import compile_filter, _format_value


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------

class TestFormatValue:
    def test_none_is_null(self):
        assert _format_value(None) == "null"

    def test_true(self):
        assert _format_value(True) == "True"

    def test_false(self):
        assert _format_value(False) == "False"

    def test_integer(self):
        assert _format_value(42) == "42"

    def test_float(self):
        assert _format_value(1.5) == "1.5"

    def test_string_plain(self):
        assert _format_value("hello") == '"hello"'

    def test_string_with_double_quote(self):
        assert _format_value('say "hi"') == '"say \\"hi\\""'

    def test_string_newline_collapsed(self):
        assert "\n" not in _format_value("line1\nline2")

    def test_datetime_utc(self):
        assert _format_value("2026-06-01T00:00:00Z") == "[2026-06-01T00:00:00Z]"

    def test_datetime_with_offset_rejected(self):
        with pytest.raises(ValueError, match="offset"):
            _format_value("2026-06-01T00:00:00+00:00")

    def test_wildcard_string_not_treated_as_datetime(self):
        result = _format_value("acme*")
        assert result == '"acme*"'


# ---------------------------------------------------------------------------
# Golden tests — conditions compilation
# ---------------------------------------------------------------------------

class TestCompileConditions:
    def test_simple_equals(self):
        dsl = FilterDSL.model_validate({
            "conditions": {"field": "status/name", "op": "=", "value": "Open"}
        })
        q = compile_filter(dsl)
        assert q.params["conditions"] == 'status/name = "Open"'

    def test_and_group(self):
        dsl = FilterDSL.model_validate({
            "conditions": {
                "and": [
                    {"field": "closedFlag", "op": "=", "value": False},
                    {"field": "priority/name", "op": "=", "value": "High"},
                ]
            }
        })
        q = compile_filter(dsl)
        assert "closedFlag = False" in q.params["conditions"]
        assert 'priority/name = "High"' in q.params["conditions"]

    def test_in_operator(self):
        dsl = FilterDSL.model_validate({
            "conditions": {
                "field": "priority/name",
                "op": "in",
                "value": ["High", "Critical"],
            }
        })
        q = compile_filter(dsl)
        assert 'priority/name in ("High", "Critical")' == q.params["conditions"]

    def test_not_contains(self):
        dsl = FilterDSL.model_validate({
            "conditions": {"field": "summary", "op": "not_contains", "value": "Low Priority"}
        })
        q = compile_filter(dsl)
        assert 'summary not contains "Low Priority"' == q.params["conditions"]

    def test_like_wildcard(self):
        dsl = FilterDSL.model_validate({
            "conditions": {"field": "summary", "op": "like", "value": "outage*"}
        })
        q = compile_filter(dsl)
        assert 'summary like "outage*"' == q.params["conditions"]

    def test_datetime_bracketed(self):
        dsl = FilterDSL.model_validate({
            "conditions": {"field": "lastUpdated", "op": ">=", "value": "2026-06-01T00:00:00Z"}
        })
        q = compile_filter(dsl)
        assert "lastUpdated >= [2026-06-01T00:00:00Z]" == q.params["conditions"]

    def test_in_rejected_in_child_conditions(self):
        dsl = FilterDSL.model_validate({
            "child_conditions": {
                "field": "items/type",
                "op": "in",
                "value": ["A", "B"],
            }
        })
        with pytest.raises(ValueError, match="only valid in 'conditions'"):
            compile_filter(dsl)

    def test_order_by_compiled(self):
        dsl = FilterDSL.model_validate({
            "order_by": [{"field": "lastUpdated", "dir": "desc"}]
        })
        q = compile_filter(dsl)
        assert q.params["orderBy"] == "lastUpdated desc"

    def test_forward_only_rejects_order_by(self):
        with pytest.raises(ValueError, match="order_by"):
            FilterDSL.model_validate({
                "page_id": "abc123",
                "order_by": [{"field": "id", "dir": "asc"}],
            })

    def test_page_defaults(self):
        dsl = FilterDSL.model_validate({})
        q = compile_filter(dsl)
        assert q.params["page"] == 1
        assert q.params["pageSize"] == 25

    def test_fields_joined(self):
        dsl = FilterDSL.model_validate({"fields": ["id", "summary", "status/name"]})
        q = compile_filter(dsl)
        assert q.params["fields"] == "id,summary,status/name"

    def test_use_search_post_flagged_on_wildcard(self):
        dsl = FilterDSL.model_validate({
            "conditions": {"field": "summary", "op": "like", "value": "acme*"}
        })
        q = compile_filter(dsl)
        assert q.use_search_post is True


# ---------------------------------------------------------------------------
# Injection invariant — property test sketch
# ---------------------------------------------------------------------------

class TestInjectionInvariant:
    """User-supplied string values must never break out of their double quotes."""

    def test_sql_injection_attempt(self):
        """A classic injection string should be safely quoted."""
        payload = 'x" and 1=1 and summary="y'
        result = _format_value(payload)
        # Must be wrapped in quotes and internal quotes escaped
        assert result.startswith('"')
        assert result.endswith('"')
        # The raw unescaped payload must not appear
        assert 'and 1=1 and summary="y' not in result or '\\"' in result
