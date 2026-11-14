#!/usr/bin/env python3
"""Check a Grafana dashboard for the mistakes that have actually broken real deployments.

A provisioned dashboard is only exercised when a human opens the page, so these
are the invariants worth asserting in CI. Each one corresponds to a failure that
happened in practice, not to a general style preference.

Usage: python ci/check_dashboard.py grafana/aximote-vehicle.json
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def walk(panels: list[dict[str, Any]]):
    """Yield every panel, including those nested inside collapsed rows."""
    for panel in panels:
        yield panel
        yield from walk(panel.get("panels", []))


def check(path: Path) -> list[str]:
    problems: list[str] = []
    dash = json.loads(path.read_text(encoding="utf-8"))

    if not dash.get("uid"):
        problems.append("dashboard has no uid, so provisioning cannot update in place")
    if not dash.get("title"):
        problems.append("dashboard has no title")

    variables = dash.get("templating", {}).get("list", [])
    names = {v.get("name") for v in variables}

    for var in variables:
        name = var.get("name", "<unnamed>")
        # A query variable with no datasource inherits Grafana's default. When the
        # default here was Loki, label_values() returned 404 and two dashboards
        # rendered completely blank while Prometheus held every metric.
        if var.get("type") == "query" and not var.get("datasource"):
            problems.append(f"variable {name!r} is a query but names no datasource")

    panels = list(walk(dash.get("panels", [])))
    if not panels:
        problems.append("dashboard has no panels")

    seen_ids: dict[int, str] = {}
    for panel in panels:
        title = panel.get("title", "<untitled>")
        pid = panel.get("id")
        if pid is None:
            problems.append(f"panel {title!r} has no id")
        elif pid in seen_ids:
            problems.append(f"panel id {pid} used twice: {seen_ids[pid]!r} and {title!r}")
        else:
            seen_ids[pid] = title

        if panel.get("type") == "row":
            continue

        if not panel.get("datasource"):
            problems.append(f"panel {title!r} names no datasource")

        for target in panel.get("targets", []):
            ref = target.get("refId", "?")
            if not target.get("datasource"):
                problems.append(f"panel {title!r} target {ref} names no datasource")

            expr = target.get("expr")
            if isinstance(expr, str) and "$" in expr:
                # Substituting a multi-value variable into an exact matcher yields
                # label="(a|b)", which matches nothing. The matcher must be =~.
                for bad in _exact_matchers_on_variables(expr):
                    problems.append(
                        f"panel {title!r} target {ref} uses an exact matcher on a "
                        f"variable ({bad}); a multi-value variable expands to an "
                        f"alternation and will match nothing — use =~"
                    )

            sql = target.get("rawSql")
            if isinstance(sql, str):
                for referenced in _variables_in(sql):
                    base = referenced.split(":")[0]
                    if base and base not in names:
                        problems.append(
                            f"panel {title!r} target {ref} references undefined "
                            f"variable ${base}"
                        )

    return problems


def _exact_matchers_on_variables(expr: str) -> list[str]:
    """Find label="$var" matchers, which should be label=~"$var"."""
    found = []
    for piece in expr.split(","):
        if "=" not in piece or "=~" in piece or "!=" in piece:
            continue
        label, _, value = piece.partition("=")
        if "$" in value:
            found.append(f"{label.strip().lstrip('{')}={value.strip().rstrip('}')}")
    return found


def _variables_in(sql: str) -> set[str]:
    """Extract ${name} and ${name:format} references from a SQL string."""
    names: set[str] = set()
    index = 0
    while True:
        start = sql.find("${", index)
        if start == -1:
            return names
        end = sql.find("}", start)
        if end == -1:
            return names
        names.add(sql[start + 2 : end])
        index = end + 1


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2

    path = Path(argv[1])
    problems = check(path)
    if problems:
        print(f"{path}: {len(problems)} problem(s)", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1

    print(f"{path}: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
