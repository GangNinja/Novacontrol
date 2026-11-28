# DF-6 live UI Automation verification — PREPARED, NOT EXECUTED

Date: 2026-11-27
Scope: Group 1, DF-6-L1, the `uia` containment profile only.
Status: **harness built and pre-flighted; no live UIA operation was performed,
because no operator authorization for desktop interaction was given in this
pass.**

This report distinguishes four things that are easy to blur together:

| Level | Question | Answer for this pass |
| --- | --- | --- |
| Implementation | does the adapter exist and refuse correctly on a simulator? | **yes** — 11 adapter tests + 7 session/integrity neighbours, offline |
| COM availability | can this host create the UI Automation client at all? | **yes**, measured (§2) |
| Real COM transport | do `ValuePattern.SetValue` / `InvokePattern.Invoke` work on a live control? | **not verified** — no live call was made |
| Live disposable application | does the result reach the application, verified independently, with zero keyboard events? | **not executed** — awaiting authorization (§6) |

A UIA PASS still would not mean games, canvas UIs or custom-drawn surfaces are
contained: those expose no pattern, so the adapter refuses them
(`unsupported_capability`) and only an isolated session/VM can cover them.

## 1. What the harness is

Two files, both under `tmp/group1/`:

| File | Role |
| --- | --- |
| `uia_probe_app.ps1` | the disposable WinForms probe: owns its window, records every meaningful event to a JSONL state file |
| `verify_uia_live.py` | the harness: launches the probe, drives the **real** `ComtypesUiaBackend`, checks the application's own state, writes evidence |

The probe exposes exactly the five control cases the brief asks for:

| Control | AutomationId | Purpose |
| --- | --- | --- |
| TextBox | `txtMessage` | supports `ValuePattern.SetValue` |
| Button | `btnApply` | supports `InvokePattern.Invoke`; its handler records the click and the text it read |
| Panel | `pnlCanvas` | exposes **no** automation pattern (unsupported case) |
| disabled Button | `btnDisabled` | `IsEnabled = false` (unavailable case) |
| two Buttons | `btnDuplicate` | one shared AutomationId (ambiguous case) |

Nothing else on the desktop is addressed: the harness resolves the window by the
probe's own pid (`EnumWindows` + `GetWindowThreadProcessId`) and re-checks every
element's pid/handle against that window before using it.

## 2. What *was* verified — and one wrong conclusion it corrected

**COM class registration and client construction.** A read-only probe
(`tmp/group1/probe_uia_com.py`) calls `CoInitialize` + `CoCreateInstance` for
`CLSID_CUIAutomation {ff48dba4-60ef-4201-aa87-54103eef594e}` and releases it. It
touches no window, element or pattern.

```
CoCreateInstance(CUIAutomation) -> S_OK
registered: True
```

The real backend was then constructed through the production path — no element
resolution, no pattern call:

```
availability: [true, "UI Automation client created (transport not verified live)."]
adapter: {"available": true, "backend": "ComtypesUiaBackend", "verified_live": false}
```

That is `ComtypesUiaBackend` doing `comtypes.client.GetModule("UIAutomationCore.dll")`
+ `CreateObject`, i.e. the transport object this project actually ships, not a
stand-in. `verified_live` stays `false` because constructing a client is not a
verified interaction, and this report does not change it.

**A mistaken conclusion, corrected by measurement.** The first version of that
probe hand-packed the CLSID from its hex string in big-endian order and reported
`REGDB_E_CLASSNOTREG`. That would have been recorded as "the UIA class is not
registered on this host" — a false blocker. The registry does contain
`HKEY_CLASSES_ROOT\CLSID\{FF48DBA4-…}` (`UIAutomation Client Central Class`,
`InprocServer32` → `UIAutomationCore.dll`), and packing the GUID as
`uuid.UUID(...).bytes_le` returns `S_OK` for both `CUIAutomation` and a control
CLSID. The probe now uses `bytes_le` and this report records the correction
rather than the original, wrong result.

## 3. The fourteen checks, as the harness implements them

Each row becomes one entry in the evidence JSON (`checks[]`, with the detail that
decided it) and one `operations[]` record for the pattern calls.

| # | Check | Assertion |
| --- | --- | --- |
| 1 | resolve the application window | `find_window_for_pid(pid)` equals the probe's reported `Handle` |
| 2 | resolve the objective-bound element | `resolve(txtMessage)` returns exactly one element |
| 3 | verify process ownership | element `pid` == probe pid |
| 4 | verify element identity | element `window_handle` == probe hwnd, and patterns include `ValuePattern` |
| 5 | enabled / available | `element.enabled` and not `element.offscreen` |
| 6 | supported `SetValue` | `adapter.set_text(txtMessage, TEXT)` → `ok` |
| 7 | supported `Invoke` | `adapter.invoke(btnApply)` → `ok` |
| 8 | independent state check | split: **8a** the probe's own state file or native `GetWindowText` shows `TEXT`; **8b** `clicks` increased by exactly 1; **8c** the application recorded the typed text as `applied` |
| 9 | refuse a disabled control | `invoke(btnDisabled)` → `ELEMENT_UNAVAILABLE` |
| 10 | refuse an unavailable element | `invoke(nope_missing)` → `STALE_ELEMENT` |
| 11 | refuse an ambiguous element | `invoke(btnDuplicate)` → `AMBIGUOUS_ELEMENT` |
| 12 | refuse a foreign-owned element | target reports `window_pid = os.getpid()` → `OWNERSHIP_MISMATCH` |
| 13 | refuse an unsupported pattern | `set_text(pnlCanvas)` → `UNSUPPORTED_CAPABILITY` |
| 14 | no keyboard fallback | `LocalDesktopRunner._type_text(profile=UIA, uia_target=pnlCanvas)` raises `PasteContainmentError` instead of injecting |

Check 8 is deliberately **not** UI Automation: the state file is written by the
probe's own `Add_Click` / timer handlers, and `GetWindowText` on the native `EDIT`
child is Win32. A UIA read-back could agree with a UIA write while the
application state never changed; these two readers cannot.

## 4. Keyboard evidence

Two independent measurements per run:

* **System-wide:** `GetLastInputInfo` is sampled before the first operation and
  after the last. Any synthetic `SendInput` would advance the tick, whoever sent
  it.
* **Probe-local:** the form has `KeyPreview = $true` and counts `KeyDown` messages,
  so a keyboard-based fallback addressed at the probe would be recorded even if
  the global tick were already advancing for an unrelated reason.

Both are asserted unchanged (`K1`, `K2`). This is stronger than counting events
the harness itself dispatched, because it cannot be satisfied by the harness
merely declining to send.

## 5. Refusal and failure handling

* `UiaRefusal` values are asserted **by identity**, so a different refusal cannot
  silently satisfy the wrong check.
* A COM exception inside a pattern call is caught by the backend and becomes a
  recorded refusal/error, not a traceback; the harness additionally wraps the
  whole run so a crash is written as evidence (`X0 harness error`) instead of
  being lost.
* The probe process is terminated in a `finally` block; a failed run leaves the
  evidence file behind and nothing running.
* Timeouts are bounded (25 s for readiness, 5 s per exchange) so a probe that
  never shows its window fails as a named check rather than hanging.

## 6. Authorization gate

`verify_uia_live.py` has two modes. The **default prints the plan and exits** —
no window is created, no UIA call is made:

```
$ python tmp/group1/verify_uia_live.py
...
Not authorised: no window was created and no UIA call was made.
Re-run with --authorize-live after the operator authorises this exact plan.
```

`--authorize-live` performs the run. The authorization requested is exactly:

* **target:** one disposable window created by `tmp/group1/uia_probe_app.ps1`
  (title `NovaUiaProbe`), for the lifetime of the check (a few seconds);
* **operations:** `ValuePattern.SetValue` on its text box, one
  `InvokePattern.Invoke` on its Apply button, and six refusals against its own
  controls;
* **expected effect:** that window's text box contains
  `hello from the UIA transport`, its Apply button is clicked once, and the probe
  writes both facts to its state file; no keyboard or mouse event is generated
  and no other window is addressed;
* **side effects visible to the operator:** a small window appears briefly and
  takes focus when it is shown (that is the WinForms `ShowDialog` activation, not
  UIA); the window disappears when the check ends.

This prompt is not that authorization, and the pass that wrote this report did
not act on it.

## 7. What a future PASS would and would not establish

A successful authorized run would move the rows to:

| Row | Today | After a green run |
| --- | --- | --- |
| UIA: supported control, live | not verified | **live-verified**, zero keyboard events, application state independently confirmed |
| UIA: unsupported control | refuses on a simulator | **fail-closed live** (check 13 + 14) |
| Interactive keyboard | BEST_EFFORT | unchanged — a UIA pass says nothing about the keyboard profile |
| Isolated session | BLOCKED | unchanged — still `no_session_transport` |
| DF-6 | PARTIAL | **PARTIAL**, with one guaranteed profile live-verified; the arbitrary-application case still needs the session/VM milestone |
| Group 1 | NOT CLOSED | NOT CLOSED until every mandatory criterion is resolved |

It would not establish containment for applications whose controls expose no
pattern (games, canvas, custom-drawn UIs). Those are refused by this profile and
require the transport described in
[DF6_SESSION_TRANSPORT_READINESS.md](DF6_SESSION_TRANSPORT_READINESS.md).

## 8. Verification performed in this pass

| Check | Result |
| --- | --- |
| harness + probe compile (`py_compile`) | clean |
| plan mode prints the target/operations/effect and exits without creating a window | confirmed, output quoted in §6 |
| COM class registration probe | `S_OK` (§2) |
| production `ComtypesUiaBackend` availability | available, `verified_live = false` (§2) |
| live operations | **NOT EXECUTED** — no authorization |
| evidence file | none, because nothing ran (a run would write `tmp/group1/uia_live_evidence_<ts>.json`) |
