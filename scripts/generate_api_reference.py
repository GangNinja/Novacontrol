"""Regenerate the REST section of docs/API.md from the real route surface.

The block is built from the routes ``src/novacontrol/api/app.py`` actually
serves -- its ``@app.<verb>("…")`` registrations -- joined to
``ApiSurface.default()`` for descriptions and
``novacontrol.api.route_consumers.ROUTE_CONSUMERS`` for frontend ownership.

ApiSurface alone is not enough.  A handler registered in ``api/app.py`` but
never declared was served, undocumented, and a regeneration built only from the
registry silently dropped it from the reference (185 documented market routes
would have gone that way).  Now every served route is listed whether or not it
is declared, and a declared route with no handler is still listed with that gap
called out.

The script documents the checkout it lives in: ``<this repo>/src`` is put first
on ``sys.path`` and the app/doc paths resolve from the script's own directory.
Otherwise an editable install of ANOTHER checkout on the venv (or a stray
``PYTHONPATH``) makes the import resolve to a different ``novacontrol`` - 446
routes instead of this tree's 933 - and ``--check`` reports hundreds of routes
out of sync with this tree's own routes, forecast cycles included, missing.

Usage::

    python scripts/generate_api_reference.py           # rewrite docs/API.md
    python scripts/generate_api_reference.py --check   # exit 1 if out of sync
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SRC = REPO / "src"
# The reference must describe THIS tree's package, whatever ``novacontrol`` the
# interpreter would otherwise import (a venv editable install of another
# checkout is the usual shadow).
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from novacontrol.api import ApiSurface  # noqa: E402
from novacontrol.api.route_consumers import (  # noqa: E402
    NON_RENDER_ROLES,
    ROUTE_CONSUMERS,
)

DOCS = REPO / "docs" / "API.md"
APP = SRC / "novacontrol" / "api" / "app.py"
BEGIN_MARKER = "<!-- BEGIN GENERATED: api-reference (scripts/generate_api_reference.py -- do not edit inside) -->"
END_MARKER = "<!-- END GENERATED: api-reference -->"

#: The registration form every endpoint in ``api/app.py`` uses.  Parsing it is
#: what the sync test treats as "the routes the app serves", so the reference
#: and the test agree by construction.
_ROUTE_DECORATOR = re.compile(
    r'@app\.(get|post|put|delete|patch)\(\s*["\']([^"\']+)["\']'
)

UNDECLARED_DESCRIPTION = (
    "Registered in `api/app.py` without an `ApiSurface` declaration; listed "
    "here because the app serves it."
)


def _consumer_note(consumer: str) -> str:
    if consumer in NON_RENDER_ROLES:
        return "no web panel - API/CLI or infrastructure"
    if consumer == "load-settings":
        return "fills the settings form"
    return f"web panel `{consumer}`"


def _app_source() -> Path | None:
    """This script's own app source, never whatever the cwd happens to hold."""

    return APP if APP.is_file() else None


def served_routes() -> list[tuple[str, str]]:
    """``(METHOD, path)`` for every handler ``api/app.py`` registers, in order."""

    source = _app_source()
    if source is None:
        return []
    text = source.read_text(encoding="utf-8")
    return [(method.upper(), path) for method, path in _ROUTE_DECORATOR.findall(text)]


def reference_routes() -> list[tuple[str, str]]:
    """Every route the reference must list: served, then declared only.

    Declared routes come first in their ``ApiSurface`` order (so an unchanged
    tree regenerates byte-identically), then served-but-undeclared handlers in
    registration order, then declarations the app cannot serve so that gap is
    visible in the reference instead of disappearing from it.
    """

    served = served_routes()
    served_set = set(served)
    declared = [(route.method, route.path) for route in ApiSurface.default().routes]
    declared_set = set(declared)
    return (
        [key for key in declared if key in served_set]
        + [key for key in served if key not in declared_set]
        + [key for key in declared if key not in served_set]
    )


def build_section() -> str:
    """The generated block, markers included, in reference order."""

    descriptions = {
        (route.method, route.path): route.description
        for route in ApiSurface.default().routes
    }
    served_set = set(served_routes())
    lines = [BEGIN_MARKER, ""]
    for method, path in reference_routes():
        key = (method, path)
        consumer = ROUTE_CONSUMERS.get(key)
        note = _consumer_note(consumer) if consumer is not None else "no consumer declared"
        if key not in served_set:
            note = f"{note}; declared, no handler in api/app.py"
        description = descriptions.get(key, UNDECLARED_DESCRIPTION)
        lines.append(f"- `{method} {path}`: {description} *(frontend: {note})*")
    lines.extend(["", END_MARKER])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if docs/API.md is out of sync instead of rewriting it",
    )
    parser.add_argument(
        "--docs",
        type=Path,
        default=DOCS,
        help="reference file to check or rewrite (defaults to this repo's docs/API.md)",
    )
    args = parser.parse_args(argv)
    docs = args.docs

    text = docs.read_text(encoding="utf-8")
    begin = text.find(BEGIN_MARKER)
    end = text.find(END_MARKER)
    if begin == -1 or end == -1 or end < begin:
        print(f"{docs}: generated block markers missing - add {BEGIN_MARKER} / {END_MARKER}", file=sys.stderr)
        return 2

    updated = text[:begin] + build_section() + text[end + len(END_MARKER):]
    if updated == text:
        if args.check:
            print(f"{docs}: in sync ({len(reference_routes())} routes)")
        else:
            print(f"{docs}: already up to date ({len(reference_routes())} routes)")
        return 0
    if args.check:
        print(f"{docs}: out of sync with the registered routes - run scripts/generate_api_reference.py", file=sys.stderr)
        return 1
    docs.write_text(updated, encoding="utf-8")
    print(
        f"{docs}: regenerated ({len(served_routes())} served, "
        f"{len(ApiSurface.default().routes)} declared, "
        f"{len(reference_routes())} documented routes)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
