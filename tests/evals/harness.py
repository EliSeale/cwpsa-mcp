"""
Agentic evaluation harness (§13.4).

Drives the real MCP server through FastMCP's in-memory Client with Claude
as the eval agent.  Grades on backend state (ConnectWise staging), not prose.

Usage:
  python tests/evals/harness.py --task tasks/001_open_ticket.yaml

TODO: implement task loading, agent loop, and grader.
The skeleton shows the intended harness structure and integration points.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Any

import yaml

log = logging.getLogger(__name__)


class EvalTask:
    """A single evaluation scenario."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.id: str = data["id"]
        self.description: str = data["description"]
        self.prompt: str = data["prompt"]
        self.checks: list[dict[str, Any]] = data.get("checks", [])
        self.tags: list[str] = data.get("tags", [])

    @classmethod
    def from_yaml(cls, path: str) -> "EvalTask":
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return cls(data)


class EvalResult:
    def __init__(self, task: EvalTask) -> None:
        self.task = task
        self.passed: bool = False
        self.tool_calls: int = 0
        self.total_tokens: int = 0
        self.latency_ms: float = 0.0
        self.error: str | None = None
        self.transcript: list[dict[str, Any]] = []


async def run_eval(task: EvalTask, n_trials: int = 3) -> list[EvalResult]:
    """Run a task N times and return all results.

    TODO: implement using FastMCP in-memory Client + Anthropic SDK.
    """
    results = []
    for _ in range(n_trials):
        result = EvalResult(task)
        result.error = "TODO: harness not yet implemented"
        results.append(result)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Run agentic eval tasks.")
    parser.add_argument("--task", required=True, help="Path to task YAML file.")
    parser.add_argument("--trials", type=int, default=3, help="Number of trials per task.")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO)

    task = EvalTask.from_yaml(args.task)
    log.info("Running eval: %s (%d trials)", task.id, args.trials)

    results = asyncio.run(run_eval(task, n_trials=args.trials))

    passed = sum(1 for r in results if r.passed)
    log.info(
        "Results: %d/%d passed | avg tool calls: %.1f",
        passed,
        len(results),
        sum(r.tool_calls for r in results) / len(results) if results else 0,
    )
    print(json.dumps([{"passed": r.passed, "error": r.error} for r in results], indent=2))


if __name__ == "__main__":
    main()
