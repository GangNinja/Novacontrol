# NovaMarket — Stage C Acceptance Readiness Checklist

Date: 2026-11-27. **Prepared, not executed.** No live session, proposal, order or
fill was produced by this work. Ticking every box below authorizes nothing; the
operator must still authorize the run itself.

Scope: Indian exchanges only (XNSE, XBOM). NYSE remains deferred and untouched.

## 0. Non-negotiable invariants for the run

- [ ] Paper-only. No broker adapter, no order placement, no live execution.
- [ ] NM-08 risk limits and NM-10 paper execution are unmodified.
- [ ] Fail-closed behaviour is kept: insufficient history → no decision, never a
      synthetic bar, never a borrowed NSE price for BSE.
- [ ] 14-close SMA minimum and the 80-bar/140-bar training minimums are
      unchanged.
- [ ] Only the isolated acceptance stores are written. The normal user financial
      store and user configuration are never opened.
- [ ] No commit, push, deploy, reset, clean or stash as part of acceptance.
- [ ] Any ambiguous outcome is reported as ambiguous, never rounded up to PASS.

## 1. Engineering preconditions (all met)

- [ ] R-01 interval-scoped decision context (`5m` short_term, `1d` otherwise;
      newest-first, chronological, deduplicated, ≥21 complete bars or none).
- [ ] R-02 schema v5 `autonomy_control_events` with actor, sequence, refusals and
      transactional mode changes.
- [ ] R-03 one-close-per-session policy, official bhavcopy preferred, conflicts
      recorded and never overwritten.
- [ ] R-04 BSE daily history present (108 rows across 101 sessions, 2026-07-01 … 11-26).
- [ ] R-05 20m independently corroborated: 1000/1000 5m components and 250/250
      independent recomputes on both exchanges.
- [ ] R-06 20m grid anchored on the exchange session open.
- [ ] R-07 one transaction per provider page (696 ms → 31 ms peak loop lag).
- [ ] SC-R08-01 canonical horizon map; NSE forecast selectable by PI-12.
- [ ] 35 focused Stage C tests green; per-file suite matrix recorded in
      `STAGE_C_FINAL_TEST_REPORT.md`.
- [ ] Independent-recompute verifier tests green.
- [ ] API reference in sync (936 routes); `PRAGMA integrity_check` = ok on both
      isolated stores.

## 2. Data preconditions to re-check immediately before the run

Run read-only; do not repair anything silently.

- [ ] XNSE store: daily bars > 14, latest completed session is the previous
      trading day, `PRAGMA integrity_check` = ok.
- [ ] XBOM store: daily bars > 14, latest official bhavcopy is the previous
      trading day, `PRAGMA integrity_check` = ok.
- [ ] No duplicate session close in either store (`dedupe` report empty).
- [ ] `2026-11-27` (or the run date) has a published bhavcopy **after** the
      close; if not, the session is intraday and C7 cannot be evaluated.
- [ ] Exchange calendar says the run date is a trading day and shows any early
      close.
- [ ] XNSE model `bootstrap` still `active`; stored forecast
      `fc_42d151e421dd9f79a566` still present.
- [ ] XBOM is still blocked at 108 < 140 — record it again; do **not** train.

## 3. Pre-run captures (immutable evidence)

- [ ] SHA-256 of `novamarket.sqlite3` (+ `-wal`, `-shm`) for both isolated
      stores, written to `tmp/stage_c_remediation_evidence/acceptance_<date>/`.
- [ ] Row counts per interval per exchange.
- [ ] `PRAGMA integrity_check` output text.
- [ ] Git worktree state (`git status --porcelain`) captured, not changed.

## 4. Sequence (after-close only)

1. [ ] Start each runtime on its own isolated store and port; `auto_start=false`.
2. [ ] Verify `GET /status` reports the expected store, portfolio and strategy,
       and `model_configured` honestly.
3. [ ] Capture the close (official bhavcopy preferred for XBOM).
4. [ ] Run **one** decision evaluation per exchange; record the full context
       (bars used, interval, strategy signal, forecast id if any, risk bridge).
5. [ ] If a non-HOLD actionable signal appears, follow the paper path only:
       proposal → NM-08 risk assessment → NM-10 paper execution. Record every
       refusal verbatim.
6. [ ] Exercise pause → resume → stop through the API; read back the control
       event chain and confirm actor, sequence and no-op/refusal rows.
7. [ ] Restart one runtime and confirm lease/restart recovery (C6 regression).
8. [ ] Stop both runtimes; confirm no listener remains on the acceptance ports.
9. [ ] Post-run SHA-256, row counts and integrity check.

## 5. Evidence required per criterion

| Criterion | Evidence that must exist before it can be marked PASS |
|---|---|
| C1 | Complete NSE decision record: bars + interval, strategy evaluation, signal, decision, non-actionable reason or proposal; operator's interpretation (R-09 A or B) recorded |
| C2 | Same for XBOM on genuine BSE data; forecast leg either present or explicitly out of scope by operator decision |
| C3 | 20m verifier output re-run after the session; interval scoping still correct |
| C4 | Official bhavcopy provenance for the session; no synthesised bar |
| C5 | Control-event chain for pause/resume/stop incl. actor and refusals |
| C6 | Restart/lease recovery evidence from the run |
| C7 | A naturally eligible paper trade observed end to end, or an explicit statement that the market produced none — never manufactured |

## 6. Abort criteria

Stop and report, do not work around, if any of these occur:

- [ ] Store integrity check fails, or a store path resolves outside the isolated
      acceptance directories.
- [ ] A bar, price or fill would have to be synthesised to continue.
- [ ] The only way to get a signal is to change a threshold, strategy or the
      interpretation of "actionable".
- [ ] A control transition succeeds without an audit event, or an audit event
      exists without its transition.
- [ ] Any code path attempts a broker or live order.
- [ ] The run date has no published close.

## 7. Sign-off (operator)

| Decision | Needed for | State |
|---|---|---|
| R-09 C1 interpretation A (correctness) or B (actionable trade) | C1 | **OPEN** |
| C3 criterion confirmation (independent 5m, now satisfied) | C3 | recommended: accept as met |
| BSE forecast scope (wait 32 sessions / lower minimum / exclude leg) | C2 | **OPEN** |
| Authorization to run the after-close session | C2, C7 | **NOT GRANTED** |
| Authorization to start Stage D | Stage D | **NOT GRANTED** |

**Readiness verdict:** engineering and data are ready for an authorized
after-close run on both exchanges; **no criterion is advanced by this
checklist**, and C7 cannot be verified without a natural lifecycle.

## 8. What this checklist deliberately does not do

- It does not schedule or start a collector, runtime or session.
- It does not lower any minimum, relax any risk limit, or pre-approve a model.
- It does not decide R-09 or the BSE forecast scope.
- It does not claim C1, C2 or C7 as PASS.
