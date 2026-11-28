# DF-6 live UI Automation verification — EXECUTED

Date: 2026-11-28
Scope: Group 1, DF-6-L1, the `uia` containment profile only.
Authorization: explicit operator authorization for **one disposable, harness-owned
`NovaUiaProbe` window** — one `ValuePattern.SetValue`, one `InvokePattern.Invoke`,
six refusal checks, zero-input verification and cleanup. No other window was
authorized, and none was touched.

Status: **PASS on all 23 checks** against the disposable probe. `ComtypesUiaBackend`
(`verified_live` now **true**) resolved an element by AutomationId, wrote through
`ValuePattern`, invoked through `InvokePattern`, refused all six unsafe controls,
and generated **zero keyboard and zero mouse events** — proven by the probe's own
counters and by a session input tick that did not move at all during the run.

This is a statement about **the transport**, not about desktop coverage. Games,
canvas/custom-drawn surfaces, Electron views without patterns and any control that
needs a real keystroke remain outside this profile, and the isolated session/VM
transport is still **BLOCKED — `no_session_transport`**. DF-6 stays **PARTIAL** and
Group 1 stays **NOT CLOSED**.

Four things are easy to blur together; this report keeps them apart:

| Level | Question | Result |
| --- | --- | --- |
| Transport constructed | can the COM client be created here? | **PASS** — `CoCreateInstance(CLSID_CUIAutomation)` → `S_OK`, backend `available: true` |
| Live element resolved | does UIA return the real control with its identity? | **PASS** — 1 element by `txtMessage`, pid + top-level window bound, `ValuePattern` only |
| Pattern acquired and executed | does the pattern call actually write/invoke? | **PASS** — `SetValue` → `hello from the UIA transport`; `Invoke` → exactly 1 click |
| Result independently verified | does the *application* show it, without UIA? | **PASS** — probe JSONL (`text`, `apply`, `clicks: 0→1`) **and** a Win32 `WM_GETTEXT` read of the EDIT control |

## 1. Authorization and command

Authorized: launch one disposable probe, one `SetValue`, one `Invoke`, six
refusals, zero-input verification, cleanup. Prohibited and not done: any other
window, any user application, keyboard or mouse injection, clipboard, `SendKeys`,
`SendInput`, `PostMessage` as an input substitute, a broad desktop scan.

```
PYTHONPATH=src NOVACONTROL_DISABLE_OLLAMA=1 python tmp/group1/verify_uia_live.py --authorize-live
```

Process exit code `0` on the final run. Default mode (no flag) creates no window;
`--authorize-live` is required, and the harness only ever addresses the window whose
pid it launched itself.

## 2. Preflight

| Check | Result |
| --- | --- |
| repository / branch / commit | `NovaControl`, `main`, `3ca2a49` (clean tree) |
| probe files | `tmp/group1/uia_probe_app.ps1` (5,178 B), `tmp/group1/verify_uia_live.py` |
| preserved evidence sha256 | `2dfb0a4e18ecf0c50916e107e75fd91c8d3c47bb2d46413719f2e58d0e31cf34` — unchanged before **and** after every run |
| host | Windows 11 `10.0.26300`, Python `3.13.0` |
| COM client | `CoCreateInstance(CLSID_CUIAutomation)` → `S_OK` |
| production backend before the run | `available: true`, `verified_live: false` |
| production backend after the run | `available: true`, `verified_live: true` |
| stop conditions | none triggered: files present, backend importable, target unique (pid → exactly one visible window, title `NovaUiaProbe`) |

## 3. Results — every check

Final run, evidence `uia_live_evidence_20261128T220302.json` (archived in
`docs/evidence/df6_uia_live_20261128/`), probe state
`uia_probe_state_20261128T220302.jsonl`.

| # | Check | Result | Evidence |
| --- | --- | --- | --- |
| K0 | ambient session input baseline (no probe, no UIA) | INFO | `tick_delta_ms 1984 / 2.0 s` — the operator was using the machine, which is exactly why the rate comparison below exists |
| R0 | probe window ready | INFO | pid 10584, hwnd 917814, title `NovaUiaProbe` |
| R1 | window found by pid (unique) | **PASS** | `hwnd 917814` |
| R2 | title is the disposable window | **PASS** | `NovaUiaProbe` |
| R3 | adapter availability | INFO | `available: true`, `backend: ComtypesUiaBackend`, `verified_live: true` |
| R3a | COM transport constructs | **PASS** | detail: "client created and verified live (disposable probe)" |
| 1 | element resolved by AutomationId | **PASS** | exactly 1: `txtMessage`, runtime id `[42, 656540]` |
| 2 | element owned by the probe process | **PASS** | `element pid 10584 == expected 10584` |
| 3 | element belongs to the probe window | **PASS** | `top_level_handle 917814 == hwnd 917814` (own handle `656540`) |
| 4 | `ValuePattern` is exposed | **PASS** | `['ValuePattern']` — and *only* that |
| 5 | element enabled and on screen | **PASS** | `enabled: true`, `offscreen: false` |
| 6 | `SetValue` accepted by the pattern | **PASS** | `ok: true`, read-back `'hello from the UIA transport'` |
| 7 | `Invoke` accepted by the pattern | **PASS** | `ok: true`, `InvokePattern succeeded on 'Apply'` |
| 8a | independent state shows the typed text | **PASS** | probe JSONL `text`, **and** `WM_GETTEXT` on the EDIT → both `hello from the UIA transport` |
| 8b | independent state shows exactly one click | **PASS** | `clicks 0 → 1` |
| 8c | the application observed the typed text | **PASS** | `applied = 'hello from the UIA transport'` |
| 9 | disabled control refused | **PASS (correct refusal)** | `element_unavailable` — "The element is disabled" |
| 10 | absent element refused | **PASS (correct refusal)** | `stale_element` — no element matching `nope_missing` |
| 11 | ambiguous control refused | **PASS (correct refusal)** | `ambiguous_element` — 2 elements match `btnDuplicate` |
| 12 | foreign-owned element refused | **PASS (correct refusal)** | `ownership_mismatch` — bound pid 15248, element pid 10584 |
| 13 | pattern-less control refused | **PASS (correct refusal)** | `unsupported_capability` — `pnlCanvas` exposes `[]`, not `ValuePattern` |
| 14 | controller does not fall back to the keyboard | **PASS** | `PasteContainmentError` raised with `containment: uia`; no injection attempted |
| K1 | no input attributable to this run | **PASS** | tick `29105109 → 29105109` (**drift 0 ms** in 2.30 s) against an ambient 991.8 ms/s |
| K2 | probe received no key message | **PASS** | `keys: 0` |
| K3 | probe received no mouse message | **PASS** | `mouse: 0` |
| C1 | probe window closed, process exited | **PASS** | `process_exited: true`, `window_still_open: false`, probe logged its own `closed` event |

## 4. Live operations, in detail

**`ValuePattern.SetValue`.** Target: `txtMessage` inside window 917814 (pid 10584).
Resolved to one element (runtime id `[42, 656540]`), element pid matched the bound
pid, top-level window matched the bound window, `enabled` and not offscreen,
`ValuePattern` present, and the element was **re-resolved immediately before the
call** with an identical runtime id. `SetValue("hello from the UIA transport")`
returned; the pattern was then read back through the same transport
(`CurrentValue`) and matched exactly.

**Independent verification.** Two channels that are not UI Automation:
1. the probe's own 250 ms publisher writes `$textBox.Text` to JSONL →
   `{"event":"text","text":"hello from the UIA transport"}`;
2. `SendMessageTimeout(WM_GETTEXT)` against the EDIT control's own HWND →
   `hello from the UIA transport`.

`GetWindowText` was tried first and returned an empty string: it cannot read a
control in another process. That is a limitation of the *check*, not of the
operation, and it is why the read is now a read-only message query. No key or
button message is sent by it — proven by K2/K3 remaining 0.

**`InvokePattern.Invoke`.** Target: `btnApply`. Same resolution path; the element
reported `['InvokePattern']` only. One `Invoke()` call. The probe's `Add_Click`
handler recorded `clicks: 1` and `applied: 'hello from the UIA transport'`, i.e. the
button ran the handler that reads the text box the previous operation had written.
The button was **not** invoked again: one call, one click, and 8b asserts
`clicks_before + 1`.

## 5. Zero-input verification

| Channel | Result | Meaning |
| --- | --- | --- |
| Probe `KeyDown` counter | **0** | no key message reached the window, so no keyboard fallback occurred |
| Probe `MouseDown` counter (added for this run) | **0** | no button message reached the window or any of its controls |
| `GetLastInputInfo` (session-wide) | before `29105109`, after `29105109` | the session tick did **not** move during the 2.30 s run |
| Ambient baseline (measured first) | 1,984 ms of drift in 2.0 s *with nothing of ours running* | the session does see background input; a bare comparison would have blamed this run for it |
| Controller path | `PasteContainmentError`, containment `uia` | the refusal at check 14 never reaches an injection path |

The K1 assertion compares rates (run 0.0 ms/s vs ambient 991.8 ms/s) instead of
asserting a static tick, and the raw numbers are kept in the evidence file. In this
run the stronger statement also holds: **the tick did not move at all**. Residual
uncertainty is stated rather than hidden: `GetLastInputInfo` is session-wide, so it
can only *fail* to show our input, it cannot attribute input to an application; the
probe counters are the attribution.

## 6. Four defects the live run found (all fixed at the cause)

None of these was visible to the simulated backend; each was found by running the
real transport, and each now has a regression test in
`tests/test_df6_uia_live_defects.py` (7 tests).

| # | Defect | Evidence from the run | Fix |
| --- | --- | --- | --- |
| 1 | `_ownership_problem` compared the element's **own** `CurrentNativeWindowHandle` with the bound **window** handle, so every child control was "foreign" | run 1: `element_hwnd 393522, expected 917746` → `element_ownership_mismatch` on the text box, the Apply button and the canvas | `UiaElement.top_level_handle` added; the COM backend walks the raw view to the top-level window; ownership compares that |
| 2 | `_patterns` used `is not None` on `GetCurrentPattern`, but an unsupported pattern returns **S_OK with a NULL pointer** (not `None`), so **every element reported every pattern** | run 1: a WinForms TextBox and a plain Panel both "exposed" `ValuePattern, InvokePattern, SelectionItemPattern, TogglePattern` | truthiness test; a Panel now reports `[]` (check 13 depends on it) |
| 3 | `_pattern` made the same `is None` mistake when re-acquiring | same mechanism, would have handed a null interface to a pattern call | truthiness test with an explicit error |
| 4 | the top-level walk stopped only at a **0** handle, but the desktop root reports its own handle (`#32769` → 65548 here) | run 2: `top_level_handle 65548` (the desktop) while the form was 2359818 | the walk stops at `GetDesktopWindow()` |
| 5 | `_element` re-found elements with `ElementFromHandle(element.window_handle)` — the element's own handle — so the search root was the control itself | runs 1–3: "The element is no longer present in its window" on both operations | re-find from `top_level_handle or window_handle` |

Two further corrections were to the **harness and evidence**, not to behaviour:
check 3 compared the child handle with the form (an assertion that could never be
true for a child control) and now compares the top-level handle while still
recording the raw hwnd; and the zero-input evidence gained the ambient baseline,
the probe-side mouse counter, the cross-process text read and a graceful
`WM_CLOSE` cleanup with its own check.

Run history, kept because it shows what the pass actually depended on:

| Run | Evidence | Result |
| --- | --- | --- |
| 1 | `uia_live_evidence_20261128T215416.json` | 12 PASS / 7 FAIL — defects 1, 2, 4 exposed |
| 2 | `…T215850.json` | 15 PASS / 8 FAIL — patterns fixed, desktop-handle defect exposed |
| 3 | `…T220015.json` | 20 PASS / 2 FAIL — only checks 6/7 failing (defect 5) |
| 4 | `…T220103.json` | **23 PASS / 0 FAIL**, exit 0 |
| 5 (final) | `…T220302.json` | **23 PASS / 0 FAIL**, exit 0, with the cross-process text read, probe mouse counter and C1 cleanup |

## 7. Cleanup

| Item | Result |
| --- | --- |
| probe window | closed with `WM_CLOSE` sent to **that** window; the probe recorded `{"event":"closed"}` |
| probe process | exited (`C1`), verified against the pid the harness launched |
| COM objects | released with the process; no global COM registration changed |
| temporary resources | the run's own state file and evidence JSON are preserved; nothing else created |
| other processes / windows | none enumerated for interaction, none closed, none terminated |

## 8. Evidence integrity

| Evidence | sha256 |
| --- | --- |
| `tmp/group1/midaction_crash_evidence_20261127T010222.json` (before the run) | `2dfb0a4e18ecf0c50916e107e75fd91c8d3c47bb2d46413719f2e58d0e31cf34` |
| same file (after all five runs) | `2dfb0a4e18ecf0c50916e107e75fd91c8d3c47bb2d46413719f2e58d0e31cf34` |

**BYTE_IDENTICAL = TRUE.** The failed mid-action evidence was never overwritten.

## 9. What this does and does not establish

Established, live, on this host: the production COM/UIA transport resolves an
element with its identity, executes `ValuePattern.SetValue` and
`InvokePattern.Invoke` on a real control, refuses disabled, absent, ambiguous,
foreign-owned and pattern-less targets with the correct reason, never falls back to
keyboard injection at the controller level, and generates no input events.

Not established, and unchanged by this pass:

* games, protected applications and anything behind UIPI;
* canvas, custom-drawn and some Electron views — measured here as a real refusal
  (`pnlCanvas` exposes no pattern at all, check 13);
* any application that genuinely requires a keystroke or a click at a coordinate;
* the dedicated session/VM boundary — still **BLOCKED, `no_session_transport`**;
  `CreateDesktop`, hidden-desktop interaction and `PostMessage` substitutes remain
  prohibited rather than merely unimplemented.

| Item | Status |
| --- | --- |
| COM transport construction | **PASS** |
| Production UIA backend | **PASS — `verified_live: true`** |
| `ValuePattern` live operation | **PASS** |
| `InvokePattern` live operation | **PASS** |
| Six refusal checks | **PASS (6/6, correct reasons)** |
| Zero keyboard events | **PASS** (probe counter 0, tick drift 0) |
| Zero mouse events | **PASS** (probe counter 0) |
| Evidence integrity | **BYTE_IDENTICAL** |
| Dedicated-session transport | **BLOCKED — `no_session_transport`** |
| DF-6 | **PARTIAL** — UIA live-verification subcriterion met; the arbitrary-application requirement still needs the session milestone |
| Group 1 | **NOT CLOSED** |
