"""Regenerate the REST section of docs/API.md from the shared registries.

Single source of truth: ``novacontrol.api.route_consumers.ROUTE_CONSUMERS``
plus ``ApiSurface.default()``. The generator splices a marked block in
``docs/API.md`` so the human-readable API reference can never drift from the
wire contract or the frontend ownership map:

  * add a route to ``ApiSurface`` and its consumer here -> run this script
    (or let CI's ``--check`` step tell you to) and the docs update themselves.

Usage::

    python scripts/generate_api_reference.py           # rewrite docs/API.md
    python scripts/generate_api_reference.py --check   # exit 1 if out of sync
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from novacontrol.api import ApiSurface
from novacontrol.api.route_consumers import NON_RENDER_ROLES, ROUTE_CONSUMERS

DOCS = Path("docs/API.md")
BEGIN_MARKER = "<!-- BEGIN GENERATED: api-reference (scripts/generate_api_reference.py -- do not edit inside) -->"
END_MARKER = "<!-- END GENERATED: api-reference -->"


def _consumer_note(consumer: str) -> str:
    if consumer in NON_RENDER_ROLES:
        return "no web panel - API/CLI or infrastructure"
    if consumer == "load-settings":
        return "fills the settings form"
    return f"web panel `{consumer}`"


def build_section() -> str:
    """The generated block, markers included, in ApiSurface declaration order."""
    lines = [BEGIN_MARKER, ""]
    for route in ApiSurface.default().routes:
        key = (route.method, route.path)
        consumer = ROUTE_CONSUMERS.get(key)
        if consumer is None:
            raise SystemExit(
                f"route {key} has no ROUTE_CONSUMERS entry - add one in "
                "src/novacontrol/api/route_consumers.py"
            )
        note = _consumer_note(consumer)
        suffix = f" *(frontend: {note})*" if note else ""
        lines.append(f"- `{route.method} {route.path}`: {route.description}{suffix}")
    lines.extend(["", END_MARKER])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if docs/API.md is out of sync instead of rewriting it",
    )
    args = parser.parse_args(argv)

    text = DOCS.read_text(encoding="utf-8")
    begin = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER)
    if begin == -1 or end == -1 or end < begin:
        print(f"{DOCS}: generated block markers missing - add {BEGIN_MARKER} / {END_MARKER}", file=sys.stderr)
        return 2

    updated = text[:begin] + build_section() + text[end + len(END_MARKER):]
    if updated == text:
        if args.check:
            print(f"{DOCS}: in sync")
        else:
            print(f"{DOCS}: already up to date")
        return 0
    if args.check:
        print(f"{DOCS}: out of sync with ROUTE_CONSUMERS - run scripts/generate_api_reference.py", file=sys.stderr)
        return 1
    DOCS.write_text(updated, encoding="utf-8")
    print(f"{DOCS}: regenerated ({len(ROUTE_CONSUMERS)} routes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
