"""
OKF (Open Knowledge Format) loader + MCP wiring for the ConnectWise PSA server.

Loads a bundle of markdown concept files (YAML frontmatter + body), injects a
compact concept index into the server instructions, and exposes the concepts
through an MCP tool so the agent fetches the exact recipe on demand instead of
guessing filter values.

Bundle path comes from OKF_BUNDLE_PATH (default: ./business-knowledge), so the
same code works locally and in the Azure Container App as long as the directory
ships with the image (see the Dockerfile COPY line in the README).

    pip install pyyaml      # add to your requirements
"""

from __future__ import annotations

import difflib
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

RESERVED = {"index.md", "log.md"}


@dataclass
class Concept:
    concept_id: str            # path within the bundle, no .md (e.g. "metrics/active_users")
    meta: dict                 # parsed YAML frontmatter
    body: str                  # markdown body
    reserved: bool = False

    @property
    def type(self) -> str:
        return str(self.meta.get("type", "")).strip()

    @property
    def title(self) -> str:
        return str(self.meta.get("title", self.concept_id)).strip()

    @property
    def aliases(self) -> list[str]:
        return [str(x).lower() for x in (self.meta.get("aliases") or [])]


@dataclass
class Bundle:
    root: Path
    concepts: dict[str, Concept] = field(default_factory=dict)

    def selectable(self) -> list[Concept]:
        """Non-reserved concepts the agent can look up."""
        return [c for c in self.concepts.values() if not c.reserved]


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Split a markdown file into (frontmatter dict, body). Permissive: a file
    with no/invalid frontmatter returns ({}, body) rather than raising."""
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                meta = {}
            return (meta if isinstance(meta, dict) else {}), parts[2].lstrip("\n")
    return {}, text


def load_okf_bundle(path: str | os.PathLike) -> Bundle:
    root = Path(path)
    bundle = Bundle(root=root)
    if not root.exists():
        print(f"[okf] bundle path not found: {root} (no business knowledge loaded)")
        return bundle
    for md in sorted(root.rglob("*.md")):
        rel = md.relative_to(root)
        try:
            meta, body = _split_frontmatter(md.read_text(encoding="utf-8"))
        except Exception as exc:
            print(f"[okf] skipped {rel}: {exc}")
            continue
        cid = rel.with_suffix("").as_posix()
        bundle.concepts[cid] = Concept(
            concept_id=cid, meta=meta, body=body, reserved=md.name in RESERVED,
        )
    print(f"[okf] loaded {len(bundle.selectable())} concept(s) from {root}")
    return bundle


def instructions_block(bundle: Bundle) -> str:
    """Compact, always-in-context index of available concepts (names + aliases),
    so the model knows what exists and when to fetch the full recipe."""
    rows = []
    for c in sorted(bundle.selectable(), key=lambda c: c.concept_id):
        alias = f" (aka: {', '.join(c.aliases)})" if c.aliases else ""
        desc = str(c.meta.get("description", "")).strip()
        rows.append(f"- {c.title} [{c.type}] — id: `{c.concept_id}`{alias}. {desc}")
    if not rows:
        return ""
    return (
        "\n\nBUSINESS CONCEPTS (tenant-specific definitions)\n"
        "For any metric, count, or 'how many / list' question, FIRST call "
        "`get_business_concept` with the matching id or name and follow its recipe "
        "EXACTLY — do not invent conditions/childconditions. Available concepts:\n"
        + "\n".join(rows)
    )


def _find(bundle: Bundle, name: str) -> Concept | None:
    q = (name or "").strip().lower()
    if not q:
        return None
    by_id = {c.concept_id.lower(): c for c in bundle.selectable()}
    if q in by_id:
        return by_id[q]
    for c in bundle.selectable():                       # exact title / alias
        if q == c.title.lower() or q in c.aliases:
            return c
    for c in bundle.selectable():                       # substring on title / alias
        if any(q in h or h in q for h in (c.title.lower(), *c.aliases)):
            return c
    titles = {c.title.lower(): c for c in bundle.selectable()}  # fuzzy fallback
    close = difflib.get_close_matches(q, list(titles), n=1, cutoff=0.6)
    return titles[close[0]] if close else None


def register_business_knowledge(mcp, bundle: Bundle) -> None:
    """Expose the bundle through MCP as on-demand lookup tools."""

    @mcp.tool(annotations={"readOnlyHint": True})
    def get_business_concept(name: str) -> dict:
        """Fetch a tenant business concept (metric/entity/rule) by id, title, or
        alias — e.g. 'active users', 'how many users', or 'metrics/active_users'.
        Returns the full definition INCLUDING the exact recipe (endpoints,
        conditions, childconditions). Call this before building any filter for a
        metric or count/list question, then follow the returned recipe exactly."""
        c = _find(bundle, name)
        if c is None:
            return {
                "found": False,
                "query": name,
                "available": [
                    {"id": x.concept_id, "title": x.title, "aliases": x.aliases}
                    for x in bundle.selectable()
                ],
            }
        return {"found": True, "id": c.concept_id, "meta": c.meta, "body": c.body}

    @mcp.tool(annotations={"readOnlyHint": True})
    def list_business_concepts() -> list[dict]:
        """List every tenant business concept available for lookup."""
        return [
            {
                "id": c.concept_id,
                "type": c.type,
                "title": c.title,
                "aliases": c.aliases,
                "description": c.meta.get("description", ""),
            }
            for c in bundle.selectable()
        ]