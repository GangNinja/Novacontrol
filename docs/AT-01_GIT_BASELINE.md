# AT-01 — Git Baseline Evidence

**Captured:** 10 November 2026, before AT-01 source changes.
**Purpose:** Make the starting state reproducible without committing, staging, resetting or discarding any user work.

## 1. Repository identity

```text
Worktree:  C:/Users/NARASIMHA/OneDrive/Desktop/NovaControl/novamarket
Git dir:   C:/Users/NARASIMHA/OneDrive/Desktop/NovaControl/.git/worktrees/novamarket
Branch:    novamarket/nm-01
HEAD:      afa5ff64c7673553b333fd3cff07cf5dc7c832b7
Commit:    afa5ff6 fix(novaforge): supersede a state read on a changed target, not a newer frame
```

The NovaMarket directory is a linked Git worktree. The shared parent exclude rule is:

```text
C:/Users/NARASIMHA/OneDrive/Desktop/NovaControl/.git/info/exclude:51:novamarket/
```

That rule covers several NovaMarket paths, including `src/novacontrol/novamarket/service.py` and the new autonomy package. The worktree nevertheless reports **785 tracked files**; some NovaMarket files are tracked, some are untracked, and some are excluded. This is why a clean commit baseline cannot be claimed for the whole subtree.

## 2. Exact dirty state

At capture:

```text
git status --porcelain=v1 | wc -l
160
```

The 16 modified **tracked** files were:

```text
.env.example
README.md
docs/API.md
docs/ARCHITECTURE.md
pyproject.toml
src/novacontrol/api/app.py
src/novacontrol/api/middleware.py
src/novacontrol/api/models.py
src/novacontrol/api/route_consumers.py
src/novacontrol/application.py
src/novacontrol/core/events.py
start.bat
start.ps1
tests/test_optimization.py
tests/test_security.py
tests/test_web_api.py
```

The remaining entries are untracked or ignored NovaMarket work, including the nine autonomy documents, the existing `service.py` and market-data modules, the React source, runtime logs, QA artifacts and the `.ui_audit/` evidence directory. The complete raw capture is:

```text
.ui_audit/at01_parent_git_status.txt
```

It contains 2,070 lines because `--untracked-files=all` expands untracked directories; the porcelain entry count is 160. It is a point-in-time record and is not a claim that the current tree still has exactly that count after AT-01.

No commit, push, reset, checkout, stash, stage or discard was performed. No user change was reverted.

## 3. Reproducible content manifest

Git alone is not a reproducible baseline for the ignored/untracked NovaMarket files. Before editing, a SHA-256 manifest was generated for the source, API, tests, docs and web source:

```text
.ui_audit/at01_baseline.sha256
```

It has 528 entries (including the relevant top-level project files). Regenerate it from `novamarket/` with:

```bash
find src/novacontrol/novamarket src/novacontrol/api tests docs web/src -type f \
  ! -path '*/__pycache__/*' ! -path '*/node_modules/*' ! -path '*/dist/*' \
  ! -name '*.pyc' -print0 |
  sort -z | xargs -0 sha256sum > .ui_audit/at01_baseline.sha256
```

The manifest is the authoritative content baseline for the parts of NovaMarket that Git does not expose cleanly. It includes the pre-AT-01 versions of `service.py`, `horizons.py`, `marketdata/calendar.py`, the React source and the nine autonomy documents.

## 4. What this baseline means for AT-01

- AT-01 edits were applied additively to the existing worktree; no baseline file was restored or overwritten wholesale.
- `src/novacontrol/novamarket/autonomy/` is new, so no pre-existing implementation could be overwritten.
- Existing tracked modifications in `api/app.py`, `api/models.py` and `api/route_consumers.py` were appended to, not rewound.
- The worktree's `service.py` was already untracked/ignored; its before-state is preserved in the SHA manifest rather than in a commit.
- A future implementation should use this manifest and the owner decisions, not a fabricated “clean” baseline.

## 5. Limits

This is an evidence record, not a certification. The repository owner must decide when to commit the pre-existing work and the AT-01 changes. No Git history was rewritten.
