"""
Contract tests — registry drift detection (§13.2).

When the OpenAPI spec is updated, this test diffs the old vs new registry
and fails CI on breaking changes: removed/renamed fields, changed types,
enum values added/removed, filterability changes, entities that disappeared.

Run:
  pytest tests/contract/test_registry_drift.py --spec-old registry.json --spec-new registry-new.json
"""

from __future__ import annotations

import json
import pytest
from pathlib import Path


def load_registry(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        pytest.skip(f"Registry file not found: {path}")
    return json.loads(p.read_text(encoding="utf-8"))


class TestRegistryDrift:
    """Placeholder contract tests — implement when CI produces two registry versions."""

    def test_no_entities_removed(self):
        """Ensure no entities present in the old registry were removed in the new one.

        TODO: parameterize with --spec-old and --spec-new paths.
        """
        pytest.skip("Implement when CI produces old/new registry pairs for diffing.")

    def test_no_fields_removed_from_default_projection(self):
        """Fields in default_projection must not be silently removed."""
        pytest.skip("Implement when CI produces old/new registry pairs for diffing.")

    def test_enum_values_not_removed(self):
        """Enum field values must not silently disappear (would break existing filters)."""
        pytest.skip("Implement when CI produces old/new registry pairs for diffing.")
