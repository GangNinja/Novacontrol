# Autonomous Paper Trading — Owner Decisions

**Status:** AUTHORITATIVE. Owner-approved 10 November 2026. This document supersedes every “proposed”, “open question” or alternative statement in the earlier autonomy documents where they conflict.

**Approved implementation scope:** **AT-01 — Runtime and Safety Shell only.** AT-02 through AT-06 are not approved, not started, and must not be described as implemented.

**Unchanged product boundary:** NovaMarket remains paper-only. `paper_only=true`, `live_trading=false`; there is no broker, live-order path, real-money execution or alternate accounting. NC = ₹1. NM-08, NM-10, NM-03 and NM-07 remain the financial authorities. BUG-QA-001, BUG-QA-002 and BUG-QA-003 remain preserved.

---

## 1. Approved decisions

| ID | Decision | Status |
|---|---|---|
| OD-01 | Horizon mapping is `short_term → day_trading`, `medium_term → swing_trading`, `long_term → long_term`. The persisted portfolio keys remain NM-07-compatible; strategy configuration, autonomy, API contracts and UI terminology use the trading-style names. | **Owner-approved** |
| OD-02 | Paused mode blocks new entries but keeps protective exits armed. `paused_exit_policy` is fixed to `protective`. `monitor_only` is not a supported paused policy and is not an active alternative. | **Owner-approved** |
| OD-03 | v1 does not implement target-weight autonomous rebalancing through `PortfolioEngine.apply()`. The only eventual order path is an approved strategy proposal → NM-08 → NM-10. | **Owner-approved** |
| OD-04 | Autonomy may use only verified exchange-calendar coverage. The current verified year is **2026**; coverage is explicit and must never be extrapolated. Outside verified coverage, autonomy fails closed. | **Owner-approved** |
| OD-05 | Start/stop-style autonomy controls require a dedicated **server-side autonomy-operator permission**, separate from ordinary API authentication. A deployment without that credential fails closed; ordinary `require_auth` is insufficient. | **Owner-approved** |
| OD-06 | No automatic autonomous resume. A restart comes up **disabled** and requires an authorized operator to start the shell again. Auto-start and auto-resume are not available. | **Owner-approved** |
| PENDING-1 | Whether day-trading positions must be forcibly flattened before the close. This is **PENDING_OWNER_DECISION**; it is not implemented or invented by AT-01. | **Pending owner decision** |

---

## 2. AT-01-specific reconciliation

- The runtime may persist `disabled`, `monitoring`, `autonomous` and `paused`. `autonomous` is a **state-only shell** at AT-01: it cannot generate a signal, proposal, paper order, fill or exit, and it cannot mutate cash, positions, holdings or the ledger.
- `trading_implemented=false`, `orders_possible=false` and `release_gate="closed"` are reported by the server. No route under `/market/autonomy` creates an order.
- The only implemented autonomy routes are contracts, status, health, start, pause, resume and stop. Configuration editing, scanning, cycles, decisions, orders, exits, kill and learning promotion are deferred to later milestones.
- The API `409 autonomy_not_released` rule from the earlier roadmap is superseded for the **mode shell**: AT-01 may enter and persist `autonomous` as a non-trading state. The order/scan gate remains closed until AT-04/AT-06 and is not part of AT-01.
- A second live runtime owner is detected through the durable lease and reported as `degraded`; it cannot start a duplicate runtime. One active cycle row is enforced by SQLite as well.
- The runtime-use lease is durable and single-owner. There is one mode/lease owner and at most one active cycle record; AT-01 does not run a cycle.
- AT-01 has no automatic start at application boot. Opening the store is a recovery point, not a resume point.
- The calendar contract reports 2026 as the only verified year, with source and maintenance information; no 2027 coverage is claimed.
- The UI is additive and minimal: one Autonomy page, operator-token field, authoritative status, mode/lease controls, and explicit “trading not implemented” messaging.

---

## 3. Baseline and change control

The worktree is the linked `novamarket/nm-01` branch at commit `afa5ff64c7673553b333fd3cff07cf5dc7c832b7`. It contains pre-existing tracked modifications and many untracked NovaMarket files; there is no clean Git baseline for this subtree. `AT-01_GIT_BASELINE.md` records the exact status and the SHA-256 manifest captured before AT-01. No commit, push, reset, checkout, stash, stage or discard was performed.

AT-02 must not start until AT-01's no-order, no-duplicate, lease/recovery and fail-closed checks are accepted. Any later milestone must update this decision record rather than silently reopening an owner decision.
