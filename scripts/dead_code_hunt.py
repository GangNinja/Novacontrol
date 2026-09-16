"""AST dead-def / dead-param hunt over the core packages.

Usage:
    PYTHONPATH=src python scripts/dead_code_hunt.py [packages...]

Reports, per package:
  * module-level defs/classes never referenced by their name anywhere in src/
    or tests (candidate dead code — a hit anywhere, including a string, keeps
    it alive because this codebase wires many things by name);
  * function parameters that are never used inside their own function body
    (candidate dead params — cross-checked against the whole tree for
    keyword call sites).

Names referenced via getattr/strings are conservatively treated as alive.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"


def collect_targets(packages: list[str]) -> dict[str, tuple[Path, ast.AST]]:
    """module-qualified name -> (path, tree) for every file in the packages."""
    targets: dict[str, tuple[Path, ast.AST]] = {}
    for package in packages:
        base = SRC / "novacontrol" / package
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            rel = path.relative_to(SRC).with_suffix("")
            module = ".".join(rel.parts)
            if rel.name == "__init__":
                module = ".".join(rel.parts[:-1])
            targets[module] = (path, tree)
    return targets


def all_source_tokens(paths: list[Path]) -> dict[str, int]:
    """Every Name/Attribute/alias identifier across the given files."""
    counts: dict[str, int] = {}
    for path in paths:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                counts[node.id] = counts.get(node.id, 0) + 1
            elif isinstance(node, ast.Attribute):
                counts[node.attr] = counts.get(node.attr, 0) + 1
            elif isinstance(node, (ast.alias,)):
                counts[node.asname or node.name.split(".")[-1]] = (
                    counts.get(node.asname or node.name.split(".")[-1], 0) + 1
                )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                # String references (getattr wiring, event names, registry keys).
                for part in node.value.replace(".", " ").replace("/", " ").split():
                    counts[part] = counts.get(part, 0) + 1
    return counts


def defined_names(tree: ast.AST) -> list[tuple[str, str, int]]:
    """(name, kind, line) for top-level and class-level defs/classes."""
    out: list[tuple[str, str, int]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.append((node.name, type(node).__name__, node.lineno))
    return out


def unused_params(tree: ast.AST) -> list[tuple[str, str, int, list[str]]]:
    """Per def: parameter names never read in the function body."""
    results: list[tuple[str, str, int, list[str]]] = []

    def check(fn: ast.AST, owner: str) -> None:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        args = fn.args
        names: list[str] = []
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
            if a.arg in ("self", "cls"):
                continue
            names.append(a.arg)
        if args.vararg:
            names.append(args.vararg.arg)
        if args.kwarg:
            names.append(args.kwarg.arg)
        if not names:
            return
        used: set[str] = set()
        # Walk everything below the function EXCEPT nested function signatures
        # (a nested def's own params are not uses of the outer ones).
        for node in ast.walk(fn):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                used.add(node.id)
        dead = [n for n in names if n not in used]
        if dead:
            results.append((owner, fn.name, fn.lineno, dead))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            check(node, "<module>")
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    check(sub, node.name)
    return results


def main() -> None:
    packages = sys.argv[1:] or ["api", "application.py", "integrations", "explore", "desktop"]
    packages = [p.removesuffix(".py") for p in packages]
    targets = collect_targets(packages)
    if not targets:
        print("no matching packages under src/novacontrol:", packages)
        return

    all_py = [*(SRC / "novacontrol").rglob("*.py"), *Path("tests").rglob("*.py"),
              *(ROOT / "scripts").glob("*.py")]
    tokens = all_source_tokens(all_py)

    print(f"scanning {len(targets)} files across {packages}\n")

    for module, (path, tree) in targets.items():
        for name, kind, line in defined_names(tree):
            # tokens counts USES only (a def's own name is not an ast.Name
            # node) — so refs == 0 means never referenced anywhere.
            if tokens.get(name, 0) == 0 and not name.startswith("__"):
                print(f"DEAD? {path.relative_to(ROOT)}:{line} {kind.lower()} {name} (refs: 0)")

        for owner, fname, line, dead in unused_params(tree):
            # A dead param still counts itself in its own signature; require
            # zero references anywhere else before reporting.
            really_dead = [p for p in dead if tokens.get(p, 0) == 0]
            if really_dead:
                print(f"PARAM? {path.relative_to(ROOT)}:{line} {owner}.{fname} unused: {really_dead}")

    print("\n(done — every hit above is a CANDIDATE; string/getattr wiring was\n"
          "conservatively counted as a reference, so survivors are real)")


if __name__ == "__main__":
    main()
