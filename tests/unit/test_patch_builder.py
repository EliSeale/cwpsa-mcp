"""Unit tests — ConnectWise patch dialect builder (§13.1)."""

from __future__ import annotations

import pytest
from cwpsa.integration.patch_builder import build_patch, custom_fields, reference, scalar


class TestScalar:
    def test_replace(self):
        op = scalar("summary", "New title")
        assert op == {"op": "replace", "path": "summary", "value": "New title"}

    def test_add(self):
        op = scalar("notes", "text", op="add")
        assert op["op"] == "add"

    def test_remove(self):
        op = scalar("someField", None, op="remove")
        assert "value" not in op

    def test_invalid_op(self):
        with pytest.raises(ValueError, match="Invalid patch op"):
            scalar("x", "y", op="update")


class TestReference:
    def test_by_identifier(self):
        op = reference("company", identifier="ACME")
        assert op == {"op": "replace", "path": "company", "value": {"identifier": "ACME"}}

    def test_by_id(self):
        op = reference("status", id_=7)
        assert op == {"op": "replace", "path": "status", "value": {"id": 7}}

    def test_both_raises(self):
        with pytest.raises(ValueError, match="not both"):
            reference("company", identifier="X", id_=1)

    def test_neither_raises(self):
        with pytest.raises(ValueError, match="either"):
            reference("company")


class TestBuildPatch:
    def test_sub_path_rejected(self):
        bad_op = {"op": "replace", "path": "company/identifier", "value": "ACME"}
        with pytest.raises(ValueError, match="sub-path"):
            build_patch(bad_op)

    def test_valid_ops_pass_through(self):
        ops = build_patch(scalar("summary", "x"), reference("status", id_=1))
        assert len(ops) == 2

    def test_custom_fields(self):
        cf = custom_fields([{"id": 1, "caption": "Test", "value": "Y"}])
        assert cf["path"] == "customFields"
        assert isinstance(cf["value"], list)
