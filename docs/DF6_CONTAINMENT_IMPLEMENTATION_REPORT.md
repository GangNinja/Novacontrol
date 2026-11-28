# DF-6 Containment Implementation Report

**Scope:** the DF-6-L1 paste-tail defect — a `SendKeys('^v')` chord that injected
four input events into whatever window held the foreground, including after the
bound target had died.
**Date:** 27 November 2026.
**Status:** implementation complete and verified offline. **No live desktop input
was sent during this work, and no live run was performed.** DF-6 remains
**PARTIAL**; Group 1 remains **NOT CLOSED**.

---

## 1. What was wrong, in one paragraph

`_stage_and_paste_windows` built one PowerShell script that staged the text and
called `[System.Windows.Forms.SendKeys]::SendWait('^v')`. That single call is
**four** input events (Ctrl-down, V-down, V-up, Ctrl-up) dispatched by the OS with
no opportunity for this process to check anything between them, and the first
identity check (`_verify_type_windows`) ran only **after** all four had gone out.
The 27 November run recorded the tail landing in an unrelated application
(`[15729620, 4192]`) while the objective bound `[7670084, 33624]`. Nothing on
Windows can recall a dispatched event, so no post-hoc check could have prevented
it; only removing the chord, or never injecting keyboard input at all, does.

## 2. What the implementation does now

### 2.1 Profiles (the claim each one may make)

| Profile | How input reaches the app | Containment | May carry a DF-6 PASS |
| --- | --- | --- | --- |
| `uia` | UI Automation pattern on one element (`ValuePattern`, `InvokePattern`, `SelectionItemPattern`, `TogglePattern`) | **Structural** — no keyboard event exists to leak | yes, only for controls that expose the pattern |
| `isolated_session` | A transport into a dedicated session or VM | **Structural** — the user's session is not an input surface | yes, when a transport exists (none configured here) |
| `best_effort` | Individually gated keyboard edges in the interactive session | **Bounded, not guaranteed** — ≤1 input event per identity check, with the race recorded | **no** |

`ContainmentProfile.guaranteed` is a property of the construction, not of a check
at runtime, and `_containment_profile()` refuses to upgrade an unknown label: it
falls back to `best_effort`.

### 2.2 The paste chord, split and gated

`desktop/containment.py :: GatedChord` dispatches `ctrl_down, v_down, v_up,
ctrl_up` one at a time. Before **every** edge it reads the live foreground
identity and compares it with the objective-bound `(handle, pid)`. On a mismatch
it dispatches no further **input** event and withholds the rest of the sequence.

Two edge classes are distinguished, because they carry different risk:

* **input** (`v_down`) — can produce text or activate a control. It is never sent
  to a window that is not the bound identity.
* **auxiliary** (modifier releases, `ctrl_up`) — cannot produce text, but leaving
  Ctrl held down would modify the *user's* next keystrokes, so a modifier release
  is still sent when the destination is no longer bound. It is recorded as
  `unbound_releases`, never presented as gated.

A held non-modifier key whose destination was lost is **not** released (that would
be an event aimed at a foreign window) and is recorded in `stuck_keys`.

### 2.3 What the record now carries

Every `TYPE_TEXT` attempt — success **or** failure — reports:

| Field | Meaning |
| --- | --- |
| `containment` / `guaranteed` | profile label and whether it may claim a PASS |
| `bound_identity` / `destination_identity` | the objective-bound `(handle, pid)` and the last identity actually observed |
| `identity_checks` | how many foreground reads gated the attempt |
| `events_dispatched`, `input_events`, `auxiliary_events` | what went out, split by class |
| `events_after_last_identity_check`, `input_events_after_last_identity_check` | the residual exposure, counted |
| `aborted`, `abort_reason`, `stuck_keys`, `unbound_releases` | why it stopped and what it could not free |
| `integrity`, `integrity_verified`, `residual_risk` | UIPI preflight and the risks that remain |
| `clipboard` | the clipboard lifecycle result (§2.5) |
| `verification.expected_identity` / `observed_identity` | the pair M2.8 reads, now present on failures too |

Failure now raises `PasteContainmentError`, which is **both** a
`VerificationError` (existing callers keep working) and a `ContainmentError` (it
carries `dispatch_record`). `AgenticOrchestrator._dispatch` copies that record into
`ActionOutcome.output`, which is why a refused paste now records its destination
instead of reporting only `{status, approval_id}`.

### 2.4 UIPI preflight

`compare_integrity(ours, target)` returns `same_or_lower`, `target_higher` or
`unknown`. A higher-integrity target is **refused before injection** — Windows
drops such input silently, so a "sent" report would be a lie. An unreadable level
is `unknown`: `best_effort` proceeds and records `integrity_verified: false`; the
guaranteed profiles refuse, because their claim cannot be demonstrated without it.

### 2.5 Clipboard lifecycle

`desktop/clipboard.py :: plan_restore` decides, in this order:

1. snapshot unavailable → `unavailable` (nothing to preserve);
2. sequence number unreadable → `skipped_concurrent_change` (cannot prove safety);
3. sequence changed since our write → `skipped_concurrent_change` — **another
   application's value is never overwritten**;
4. the clipboard held a non-text format → `skipped_non_text` (text is the only
   format this path preserves, and the loss is reported);
5. text present → `restored`; empty → `cleared`.

A write Windows refuses is reported as `denied`, never as a restore. The restore
runs on the **success and the failure path**: a refused paste is exactly when the
staged text must not be left behind. Because the clipboard is restored, the
post-hoc probe can no longer see the text, so `_type_text` uses the **staging**
result (`clipboard_staged`) as its clipboard evidence — checked before dispatch,
not after.

### 2.6 UI Automation profile

`desktop/uia.py :: UiaAdapter` resolves an element from the objective-bound window,
then refuses:

* `ambiguous_element` (more than one match),
* `ownership_mismatch` (element pid/window ≠ bound pid/window),
* `element_unavailable` (disabled/offscreen),
* `unsupported_capability` (the control does not expose the needed pattern — the
  canvas/game case, reported by name),
* `stale_element` (gone, or its runtime id changed between resolution and the
  pre-interaction re-check),
* `verification_failed` (a `SetValue` whose read-back differs).

`keyboard_events` is always `0` and there is **no** code path from a refusal to
keyboard injection. `ComtypesUiaBackend` is the real COM transport
(`UIAutomationCore` typelib); it is marked `verified_live = False` because no live
UIA interaction was performed in this stage.

### 2.7 Isolated-session profile

`computer/session.py :: IsolatedSessionAdapter` owns lifecycle (`blocked → ready →
running → stopping → closed/failed`), session identity, dispatch accounting,
emergency stop, teardown and an audit trail. It refuses a transport whose identity
reports the interactive session (`transport_not_isolated`) rather than relabelling
it. **No transport is configured on this host**, so `feasibility()` reports
`no_session_transport` with the dependency spelled out; see
`DF6_WINDOWS_SESSION_ARCHITECTURE.md`.

## 3. Files changed

| File | Change |
| --- | --- |
| `src/novacontrol/desktop/containment.py` | **new** — profiles, `Identity`, integrity comparison, `GatedChord`, residual-risk notes |
| `src/novacontrol/desktop/clipboard.py` | **new** — snapshot/restore decision |
| `src/novacontrol/desktop/uia.py` | **new** — UIA adapter, refusals, comtypes transport |
| `src/novacontrol/computer/session.py` | **new** — isolated-session boundary, feasibility, blocker |
| `src/novacontrol/desktop/controller.py` | gated paste chord, per-event identity checks, integrity preflight, clipboard capture/restore, UIA dispatch, `PasteContainmentError`, containment fields |
| `src/novacontrol/agentcore/orchestrator.py` | failed dispatch carries `dispatch_record` into `ActionOutcome.output` |
| `tests/test_df6_dispatch_ledger.py` | **new** — 43 tests: 12 scenarios, UIA, session, clipboard, integrity |
| `tests/test_desktop.py`, `tests/test_d12_targeting.py` | paste stubs accept the `bound` keyword (the only test-signature change) |

No file outside `src/novacontrol/{desktop,agentcore,computer}` and `tests/` was
modified. `NovaMarket`/`NovaForge` were not touched. The preserved failed evidence
`tmp/group1/midaction_crash_evidence_20261127T010222.json` is unchanged
(sha256 `2dfb0a4e18ecf0c50916e107e75fd91c8d3c47bb2d46413719f2e58d0e31cf34`).

## 4. Honest status by profile

| Profile | Implemented | Offline provenance | Live-verified | Status |
| --- | --- | --- | --- | --- |
| `best_effort` keyboard | yes | 15 ledger scenarios + integrity + clipboard tests | no (not authorised in this stage) | **BEST_EFFORT** |
| `uia` | yes (adapter + COM transport) | 11 adapter tests with a simulated backend; the COM class creates on this host and the production backend reports available | **no element was acted on** | **not yet verified live** — harness prepared, awaiting authorization |
| `isolated_session` | boundary + feasibility | 7 boundary tests | **no** | **BLOCKED — no session/VM transport configured** |

Updated 2026-11-27 (second pass): see
[DF6_LIVE_UIA_VERIFICATION_REPORT.md](DF6_LIVE_UIA_VERIFICATION_REPORT.md) for the prepared
live run and [DF6_SESSION_TRANSPORT_READINESS.md](DF6_SESSION_TRANSPORT_READINESS.md) for the
exact requirements a session transport must satisfy. Nothing in the table above moved to
verified, because nothing was acted on.

## 5. What this does NOT claim

* It does **not** claim containment for the interactive keyboard path. A focus
  change between the final check and its event still lands one event in the wrong
  window; the simulator reproduces exactly that
  (`test_race_between_check_and_dispatch_is_bounded_and_reported`) so the limit is
  measured, not asserted away.
* It does **not** claim a UIA PASS: the COM transport has not been exercised
  against a live control, and controls without a writable pattern are reported
  `unsupported_capability` rather than covered.
* It does **not** claim session isolation: no transport exists on this host, and a
  hidden desktop or `PostMessage` is explicitly not a substitute.
* It does **not** claim cancellation of a dispatched event — nothing here removes
  an event from the input stream.

## 6. Remaining authorisation requirement

DF-6 can only become PASS after a **separately authorised** live run **inside a
guaranteed profile** (UIA against a real control, or an isolated session/VM),
demonstrated with the dispatch ledger and real evidence. The UIA half is now
prepared and ready to request permission for — exactly one disposable window,
two pattern calls and six refusals
([DF6_LIVE_UIA_VERIFICATION_REPORT.md](DF6_LIVE_UIA_VERIFICATION_REPORT.md) §6).
Until then:

* interactive keyboard profile: **BEST_EFFORT**;
* DF-6: **PARTIAL**;
* Group 1: **NOT CLOSED**.
