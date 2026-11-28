# DF-6 Dispatch-Ledger Test Report

**Suite:** `tests/test_df6_dispatch_ledger.py` — 43 tests.
**Executed:** 27 November 2026, offline. **No desktop input was sent and no live
harness was run.**
**Command:** `PYTHONPATH=src python -m pytest tests/test_df6_dispatch_ledger.py -q`
**Result:** `43 passed`.

---

## 1. Why a ledger, and why it is independent

The DF-6 failure was not "the controller reported success when it should have
reported failure". It was that **four events had already been dispatched** before
any check ran, and the controller's own message was the only witness. A test that
asserts on that message proves nothing about where the events went.

So the suite never reads the controller's `success`, `detail` or record to decide
what happened. A `SimulatedDesktop` owns the bound identity, the current foreground
identity, the window titles and the scripted hazards, and it writes a
`DispatchLedger` entry **at the moment the transport is called**:

```
LedgeredEvent(event, destination, kind, channel, gated, check_index)
```

`destination` is read from the simulator's foreground at that instant; `gated` is
`destination == bound`. The runner under test is the **real**
`LocalDesktopRunner` — the real `GatedChord` loop, the real clipboard lifecycle,
the real `_type_text` decision logic — with only its OS transports replaced
(foreground probe, key edge, clipboard read/write, integrity probe). That is the
same shape as the existing `ProbingRunner` pattern, extended with a destination
model.

## 2. The twelve scenarios

`B` = `[7123, 4242]` (bound), `O` = `[9921, 8]` (an unrelated application).

| # | Scenario | Scripted hazard | Ledger result | Verdict |
| --- | --- | --- | --- | --- |
| 1 | Normal operation | none | `ctrl_down, v_down, v_up, ctrl_up` all → B; every event `gated` | **PASS** |
| 2 | Process killed before dispatch | foreground = none | zero events; refusal carries bound B and observed `None` | **PASS** |
| 3 | Process killed on event 1 | kill after `ctrl_down` | `ctrl_down` → B; no `v_down`; zero input events | **PASS** |
| 4 | Focus stolen between modifier and key | steal after `ctrl_down` | `v_down` never dispatched; only `ctrl_up` reaches O (auxiliary, recorded) | **PASS** |
| 5 | Focus stolen between key-down and key-up | steal after `v_down` | `v_up` **withheld**; `stuck_keys == ["v"]` recorded | **PASS** |
| 6 | Unrelated foreground before send | foreground = O at start | zero events; `abort_reason = foreground_not_target` | **PASS** |
| 7 | Identity change (same title, new hwnd/pid) | replacement window mid-run | zero events; observed identity recorded as the replacement | **PASS** |
| 8 | Replacement window with identical title | O retitled to B's title | zero events — the title is not the identity | **PASS** |
| 9 | Unrelated destination | foreground = O | zero events; record carries `bound_identity` and `observed_identity = O` | **PASS** |
| 10 | Interrupted delivery | stop between events | no input event after the stop; held edges released or recorded | **PASS** |
| 11 | Recovery after uncertain delivery | steal after `v_down` | `input_delivery_state == uncertain`; orchestrator forces `RECHECK`; ledger shows **one** attempt | **PASS** |
| 12 | Emergency stop | stop before / during the run | no subsequent event; every held key released or recorded | **PASS** |

Supporting scenarios in the same class:

| Test | What it pins |
| --- | --- |
| `test_12b_emergency_stop_mid_run_leaves_no_held_key_unreported` | a stop mid-chord never leaves an unreported held key |
| `test_race_between_check_and_dispatch_is_bounded_and_reported` | **the documented residual**: a focus change between the check and its event lands exactly **one** input event, and the record says `input_events_after_last_identity_check == 1` |
| `test_an_unknown_profile_label_never_upgrades_the_claim` | an unrecognised `containment` label falls back to `best_effort` |

Scenarios 2–5 and 9 assert **zero foreign input events**. Scenario 4's `ctrl_up`
is the only event that reaches O, it is a modifier release (it cannot produce text),
and it is reported as `unbound_releases` rather than presented as gated.

## 3. Acceptance profile verdicts

**Interactive keyboard profile**

* At most one **input** event follows the last successful identity check in every
  scenario — measured, not asserted: the ledger's `check_index` and `gated` flags
  are produced by the simulator.
* All uncertainty is recorded (`aborted`, `abort_reason`, `stuck_keys`,
  `unbound_releases`, `input_events_after_last_identity_check`).
* The profile reports `guaranteed: false` and **never** carries the containment
  PASS. This is enforced by construction (`ContainmentProfile.guaranteed`) and
  asserted in scenario 1.

**UIA profile** (11 tests, simulated backend)

| Test | Result |
| --- | --- |
| `SetValue` on a text field | **PASS** — `keyboard_events == 0`, one `uia.SetValue` to B |
| canvas control with no writable pattern | `unsupported_capability`, zero calls, **no fallback** |
| element owned by another pid | `element_ownership_mismatch`, zero calls |
| two matching elements | `ambiguous_element`, zero calls |
| disabled element | `element_unavailable` |
| element rebuilt between resolution and interaction | `stale_element`, zero calls |
| element absent | `stale_element` (never a guess) |
| no backend at all | `backend_unavailable`, zero keyboard events |
| window with no control selector | `unsupported_capability` |
| controller routed with `containment: uia` and an unsupported control | refuses, zero keyboard events |
| controller routed with `containment: uia` on a supported control | records the element, the operation and `keyboard_events: 0` |

Zero foreign events holds **by construction** in this profile: no keyboard event
is generated at all.

**Isolated-session profile** (7 tests, fake transport)

| Test | Result |
| --- | --- |
| feasibility without a transport | `no_session_transport` blocker with its dependency; `interactive_session_is_not_isolation: true` |
| `start()` without a transport | `SessionUnavailable` with the same blocker |
| transport reports the interactive session | refused (`transport_not_isolated`), transport shut down |
| four events dispatched | all four reach the **isolated** transport; nothing in the interactive ledger |
| emergency stop | later events refused with `reason = emergency_stop` |
| teardown twice | one close, one audit entry |
| mechanism table | `CreateDesktop`, hidden desktop and `PostMessage` are marked unsuitable with reasons |

**Real isolated-session containment is not demonstrated**, because no transport
exists on this host. That is the natural blocker of §5 of the implementation
report, not a test failure.

## 4. Clipboard lifecycle (6 tests)

| Case | Result |
| --- | --- |
| success | previous text restored (`restored`) |
| refused/failed paste | previous text restored — no residue |
| interrupted paste | restored |
| another app takes the clipboard mid-run | `skipped_concurrent_change`, their value untouched, `residue: true` |
| Windows refuses the restore write | `denied`, `applied: false` — never claimed |
| previous clipboard held non-text / was unreadable / sequence unreadable | `skipped_non_text` / `unavailable` / `skipped_concurrent_change` |

## 5. Integrity preflight (4 tests)

* `target_higher` → **refused before injection**, zero events.
* `unknown` → proceeds in `best_effort` with `integrity_verified: false` and an
  explicit residual-risk note.
* `unknown` in a guaranteed profile → **refused**.

## 6. Regression

| Command | Result |
| --- | --- |
| `NOVACONTROL_DISABLE_OLLAMA=1 pytest tests/test_df6_dispatch_ledger.py tests/test_desktop.py tests/test_desktop_input.py tests/test_d12_targeting.py tests/test_lim2_recovery.py tests/test_phase29_computer.py tests/test_phase30_operations.py tests/test_phase31_skills.py tests/test_agentcore.py -q` | **399 passed**, 50 subtests |
| `mypy` on the six changed/new source modules | clean |
| `ruff check` on the four new modules and the new test module | clean |

**Not run to completion:** the whole `tests/` directory. `pytest tests -q` does not finish in
a workable time in this environment (3% in four minutes, dominated by network-bound LLM/API
tests) and `tests/test_remaining_modules.py` fails collection with
`ImportError: cannot import name 'SkillInvocation' from 'novacontrol.skills'` — a
pre-existing Phase 31 API drift in an unrelated module, not caused by this change. Neither is
claimed as a pass.

## 7. What these tests do not establish

* They are a **simulation** of the documented Windows semantics. They are not
  evidence about the OS; that is why the live gate stays separate and separately
  authorised.
* The UIA COM transport is unexercised (marked `verified_live = False`).
* No isolated session was started (none can be, without a transport).
* The preserved failed evidence from the 27 November run was **not** modified and
  was **not** re-interpreted by these tests.

## 8. Harness assertions

M2.8a/b/c are unchanged and were **not** re-run. The implementation now supplies
what M2.8b/c need on the failing path: a refused or aborted paste raises with its
`dispatch_record`, `AgenticOrchestrator._dispatch` copies it into
`ActionOutcome.output`, and the record contains
`verification.expected_identity` / `verification.observed_identity`. Before this
change a failed typing record carried only `{status, approval_id}` — the exact
evidence gap M2.8b exists to catch.

The corrected 21-check harness is **not comparable** to the retracted 19/19 run
and no attempt was made to make it pass by weakening anything.

Offline re-evaluation of the corrected audit (`tmp/group1/audit_m28_offline.py`, no input, no
harness run):

| Input to `_identity_audit` | M2.8b (evidence present) | M2.8c (destination is the bound target) |
| --- | --- | --- |
| preserved 2026-11-27 evidence (sha256 unchanged) | passes | **fails** — expected `[7670084, 33624]`, observed `[15729620, 4192]` |
| a new-style refusal record from the simulated runner (zero events dispatched) | passes | **fails** — expected `[7123, 4242]`, observed `[9921, 8]` |
| a bare record with no identity pair | **fails** | — |

The second row is the one that matters for this change: the failure path now supplies real
identity evidence, so M2.8b cannot pass by scanning an empty field, and M2.8c still refuses
a record whose destination is a different window. The audit did not become vacuous again.
