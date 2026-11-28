# DF-6 dedicated-session transport — readiness and exact requirements

Date: 2026-11-27
Scope: Group 1, DF-6-L1, the `isolated_session` containment profile.
Status: **BLOCKED — `no_session_transport`.** Nothing in this pass changes that,
and no substitute isolation was implemented.

`computer/session.py` already owns the *boundary*: lifecycle states, identity
refusal, per-event dispatch accounting, emergency stop, teardown and an audit
trail. What it does not own — and cannot invent — is a channel into a different
session. This document records exactly what such a channel must provide, so the
milestone can be specified instead of hand-waved.

## 1. What exists today (verified, not proposed)

| Capability | Where | Behaviour |
| --- | --- | --- |
| Feasibility report | `IsolatedSessionAdapter.feasibility()` | `{supported: false, blockers: [no_session_transport], interactive_session_is_not_isolation: true}` + the mechanism table |
| Lifecycle | `SessionState` = `blocked → ready → running → stopping → closed / failed` | `start()` refuses with `SessionUnavailable` carrying the blocker |
| Identity refusal | `start()` | a transport reporting `isolated: false` is shut down and refused (`transport_not_isolated`) |
| Dispatch accounting | `dispatch()` | refuses unless `ready`/`running`; returns `dispatched: false` with `emergency_stop` or `session_<state>` |
| Emergency stop | `emergency_stop()` | sets `stopped`, refuses every later event, records the count at the moment of the stop |
| Teardown | `teardown()` | idempotent; a transport that refuses to close is recorded (`session.teardown_failed`), never lost |
| Audit | `audit[]` | `session.blocked|ready|refused|failed|closed`, `input.dispatched|refused`, `session.emergency_stop`, `session.teardown_failed` |
| Provenance | `tests/test_df6_dispatch_ledger.py` (7 session tests) | a simulated transport proves the interactive ledger receives nothing |

The only input path out of the adapter is `SessionTransport.dispatch`. There is
no fallback branch to `SendInput`, which is what makes the refusal honest rather
than a labelled guess.

## 2. Requirements for a genuine transport

A transport is only acceptable if it satisfies **all nine** of the following.
Each maps to a test the milestone must add.

### 2.1 Session creation and lifecycle

* Creates or attaches to a session whose **id differs from the input session**
  (`WTSGetActiveConsoleSessionId` / the token's `TokenSessionId`), or to a VM.
* `start()` is idempotent enough to survive a retry after an ambiguous failure,
  and reports a typed failure instead of raising into the caller's work.
* State transitions are the existing five; `closed` is terminal.

### 2.2 Authentication and ownership

* Has an explicit, scoped authority to create the session: a stored credential
  for a second logon, a pre-provisioned logon token, or VM control credentials.
* The credential must be **installed by the operator** through the secret store
  (never in the repo, never in a chat log), and the adapter must report
  `no_session_credentials` distinctly from `no_session_transport`.
* NovaControl must own the process tree it starts inside the session: teardown
  kills what it created, by pid, and reports leftovers instead of assuming them.

### 2.3 Process isolation

* The workflow's processes run **inside** the session/VM: same session id as the
  transport's identity, no inheritance from the interactive session's window
  station, and no shared clipboard unless the transport explicitly bridges it.
* A verification capture read back for a step must come from the same session the
  input went to. Mixed capture/input sources are a defect, not a limitation.

### 2.4 Input transport

* One event in, one event out, addressed to the session: RDP input channel, VM
  virtual HID, or sandbox API. `SendInput` against the host is forbidden, and a
  transport that internally falls back to it is not usable.
* Events are individually acknowledged, so `events_dispatched` is a measurement
  rather than an assumption.

### 2.5 Session identity

* `SessionIdentity{isolation_verified: true}` only when the id/VM was checked, not
  merely requested. The existing `isolated: false` refusal stays the gate: a
  transport that reports the interactive session is refused at `start()`.

### 2.6 Recovery

* A session that dies mid-event is detected (`dispatch` raises/returns a typed
  failure), the adapter goes `failed`, and a retry creates a **new** session —
  never resumes into the old one.
* Recovery must not replay an event whose delivery is unknown; the existing
  ledger rule (an uncertain delivery is re-checked, never replayed) applies
  unchanged.

### 2.7 Emergency stop

* The operator's stop must interrupt input **already in flight**: the transport
  needs its own cancel path (close the input channel / release the virtual HID),
  because the adapter can only refuse future events.
* The count of events dispatched before the stop is reported, as today.

### 2.8 Teardown

* Graceful close, then force, then report: a session that will not close is
  `failed` with the reason and must not be left running silently.
* Teardown is safe to call twice and after a failed start.

### 2.9 Verification of isolation from the user's interactive session

This is the acceptance test, and it is what makes the profile a *guaranteed*
one:

1. The host session runs a **ledger** — an independent observer of its own input
   state, e.g. `GetLastInputInfo` deltas plus a low-level keyboard/mouse hook, and
   a count of focused-window changes.
2. The workflow runs a real keystroke/click sequence inside the isolated session.
3. The isolated session's application must show the effect (its own state, read
   back inside the session).
4. The host ledger must show **zero foreign input events** and no focus change in
   the user's applications for the whole run.
5. The result is recorded per event, not as a summary, and the two profiles are
   never aggregated into one PASS.

## 3. Candidate transports, with their real prerequisites

| Candidate | Isolation | Prerequisites this process lacks | Verdict |
| --- | --- | --- | --- |
| RDP/console session with its own logon token | genuine (different session id) | an account permitted to open an interactive session, its credential stored by the operator, and a host that accepts the connection | usable once credentials exist; the cheapest real option |
| Windows Sandbox | genuine (separate VM-ish container) | virtualization enabled, image, and a control channel (RDP or a mapped socket) — the sandbox API has no input injection of its own | usable, but needs a channel *and* the sandbox to be enabled on this host |
| Hyper-V VM with virtual HID | strongest | Hyper-V enabled, an image, VM control (`vmms`/WMI) and a HID channel | strongest, largest setup |
| `CreateDesktop` + `SwitchDesktop` | **not isolation** | — | refused: it replaces the user's desktop instead of protecting it |
| Hidden desktop without switching | **not isolation** | — | refused: `SendInput` fails with `ERROR_ACCESS_DENIED` off the input desktop |
| `PostMessage`/`WM_KEYDOWN` to a window | **not isolation** | — | refused: not genuine input, ignored by many applications, and a message to the wrong window is still a message to it |

None of these can be granted by writing code. Each is an operator/platform
decision, and the first two are the ones worth scheduling.

## 4. What the milestone would produce

1. `SessionTransport.start/dispatch/shutdown` for the chosen mechanism.
2. Window enumeration **inside** the session, so an objective binds `(handle, pid)`
   in the same space input is dispatched into (today's targeting binds host
   handles).
3. `containment: isolated_session` on the action record, selected explicitly, with
   `feasibility()` consulted first — a missing transport stays a refusal.
4. The nine tests of §2 plus the host-ledger acceptance run of §2.9.
5. Only then: DF-6 PASS for the arbitrary-application case, and this document's
   status changes from BLOCKED to verified with the ledger attached.

Until 1–5 exist, the profile stays **BLOCKED**, and no test in this repository may
report it as verified.

## 9. Update — the UIA half is no longer a preparation (2026-11-28)

The element-scoped profile described above was **executed** against a disposable
`NovaUiaProbe` window under explicit operator authorization: 23/23 checks, exit 0, with
`ValuePattern.SetValue` and `InvokePattern.Invoke` both confirmed by the application's own
state, six correct refusals, and zero keyboard/mouse events. Details:
[DF6_LIVE_UIA_VERIFICATION_REPORT.md](DF6_LIVE_UIA_VERIFICATION_REPORT.md).

What that changes here: the "UIA first, then session" ordering is still right, and the
reason for the session profile is now measured rather than assumed — the probe's own Panel
exposed **no automation pattern at all**, which is the same shape as a canvas, a game
surface or a custom-drawn control. Every such target is refused
(`unsupported_capability`), and this report's requirement list for a real session/VM
transport is unchanged:

* the session profile stays **BLOCKED — `no_session_transport`**;
* `CreateDesktop` plus a hidden desktop is still **not** a substitute (input into it is
  invisible to the user, and the DF-6 failure was about the user's interactive session);
* `PostMessage`/`WM_CLICK`-style substitutes are still refused rather than implemented.

The live run did not touch, and cannot support any claim about, the session profile.
