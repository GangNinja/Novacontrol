# DF-6 Windows Dedicated-Session / VM Architecture

**Purpose:** define the only execution profile that can give **guaranteed
containment** for arbitrary Windows applications — including games, canvas UIs and
custom-drawn controls that expose no UI Automation pattern.
**Status:** boundary **implemented** (`src/novacontrol/computer/session.py`),
transport **not available on this host**. No session was created, no input was
sent, and nothing here relabels the interactive session as isolated.

---

## 1. Why a session, and not a desktop

| Mechanism | Separate input queue? | Suitable | Why |
| --- | --- | --- | --- |
| Session with its own logon token (RDP/console) | **yes** | **yes** | A different session id cannot receive the interactive session's input. Needs credentials or a pre-provisioned token this process does not hold. |
| Windows Sandbox / Hyper-V VM with a device channel | **yes** | **yes** | Strongest boundary. Needs virtualization plus a control channel (RDP/HID injection). |
| `CreateDesktop` + `SwitchDesktop` | no | **no** | `SendInput` only reaches the *input desktop*; switching it in **replaces the user's desktop** rather than protecting it. |
| Hidden desktop, never switched | no | **no** | `SendInput` fails with `ERROR_ACCESS_DENIED` — it is not the input desktop. Measured in the DF-6 investigation. |
| `PostMessage`/`WM_KEYDOWN` to a window | no | **no** | Not genuine keyboard input; frequently ignored, and a message to the wrong window is still a message to it. |

The consequence is blunt and is the reason DF-6 cannot be closed on the current
host: **there is no "isolate without a session" on Windows.** Everything short of a
different session or a VM remains best-effort keyboard injection into the user's
own input queue.

## 2. The device boundary

`IsolatedSessionAdapter` is the whole boundary. Its contract:

| Element | Implementation |
| --- | --- |
| **Session lifecycle** | `BLOCKED → READY → RUNNING → STOPPING → CLOSED`; `FAILED` on a transport error. `start()` transitions to `READY` only after the transport returns an identity. |
| **Session identity** | `SessionIdentity(session_id, name, transport, isolated, started_at)`. A transport that reports `isolated=False` (i.e. the interactive session) is **refused** and shut down (`transport_not_isolated`). |
| **Input transport** | `SessionTransport.dispatch(event)`. The adapter cannot reach the host's `SendInput`; its only dispatch path is this method. |
| **Target resolution** | Owned by the transport (a window inside the isolated session). The adapter never addresses a host window. |
| **Input dispatch** | Counted (`events_dispatched`) and audited per event; refused unless the session is `READY`/`RUNNING` and not stopped. |
| **Process isolation** | The transport owns the process boundary (session or VM). The adapter deliberately has no `subprocess`/`ctypes` path of its own. |
| **Recovery** | A refused event returns `{dispatched: false, reason}` instead of raising mid-sequence; teardown is idempotent and records a failure rather than losing it. |
| **Emergency stop** | `emergency_stop()` sets `stopped`, refuses every later event with `reason: emergency_stop`, and records the dispatch count at the moment of the stop. |
| **Audit events** | `session.blocked`, `session.ready`, `session.refused`, `session.failed`, `input.dispatched`, `input.refused`, `session.emergency_stop`, `session.teardown_failed`, `session.closed` — each with a UTC timestamp. |
| **Session teardown** | `teardown()` closes the transport once, is safe to call twice, and reports the count of events dispatched before the close. |

## 3. Feasibility on this host (the blocker, precisely)

`IsolatedSessionAdapter(transport=None).feasibility()` returns:

```json
{
  "supported": false,
  "profile": "isolated_session",
  "blockers": [
    {
      "code": "no_session_transport",
      "detail": "No dedicated-session transport is configured. The interactive session cannot stand in for one: its input queue is shared with the user's applications.",
      "dependency": "A session host (RDP/console session with its own logon token) or a VM with a device channel, plus the credentials or sandbox API it needs."
    }
  ],
  "interactive_session_is_not_isolation": true
}
```

What is missing is **not code** — it is a platform capability this process does not
own:

* `LogonUser` + `CreateProcessAsUser` needs credentials for an account that may
  open an interactive session; NovaControl holds none, and inventing one is not an
  option.
* `CreateProcessWithTokenW` with a duplicated token requires an existing session to
  duplicate from, i.e. a session that is already isolated.
* Windows Sandbox / Hyper-V need virtualization enabled, an image, and a control
  channel (RDP or virtual HID) — plus the authority to start and drive a VM.
* An RDP session needs a host, a certificate/logon flow and the same credential
  problem.

None of these can be granted by writing a class; each is an operator/platform
decision.

## 4. Implementation plan (the milestone that would close DF-6 for arbitrary apps)

1. **Choose the transport.** Recommended first: a dedicated VM with a virtual HID
   channel, because it needs no interactive logon token and its input device cannot
   address the host at all. Second: an RDP session with a stored, scoped credential.
2. **Implement `SessionTransport`** for it:
   `start()` → connect to / boot the session, return an identity whose `isolated`
   is true only if the session id differs from the host's input session;
   `dispatch(event)` → send one key/click into that session;
   `shutdown()` → stop it.
3. **Provide target resolution inside the session** (window enumeration *of that
   session*), so an objective binds `(handle, pid)` in the same space the transport
   dispatches into.
4. **Select the profile explicitly.** `containment: isolated_session` on a desktop
   action; `feasibility()` is checked first, and a missing transport is a refusal
   (`SessionUnavailable`), never a fallback to keyboard injection.
5. **Verify with the ledger, then live.** The twelve `tests/test_df6_dispatch_ledger.py`
   scenarios run against a simulated transport today; the milestone adds the real
   transport, then a separately authorised live run **inside** the session, with a
   ledger produced by the host (the interactive session must show zero input
   events).
6. **Only then** flip `DF-6` to PASS for the arbitrary-application case.

## 5. Interaction with the other profiles

* **UIA** stays the cheaper guaranteed profile for *supported controls* — no VM, no
  session, no keyboard events. It cannot cover canvas/games; those need §4.
* **`best_effort` keyboard** stays for compatibility, is labelled, and can never be
  the basis of a DF-6 PASS.
* Ordering for a caller: try UIA when the control is known to expose a pattern →
  otherwise require the isolated session → otherwise report the limitation and do
  **not** type.

## 6. Residual risks specific to this boundary

* A transport bug that falls back to host input would defeat the whole design; the
  adapter's only dispatch path is `SessionTransport.dispatch`, and
  `tests/test_df6_dispatch_ledger.py::IsolatedSessionTests` pins that nothing is
  ever written to the interactive ledger.
* Screen capture for verification must also come from the isolated session;
  capturing the host and typing into a VM (or the reverse) would silently verify
  the wrong surface. That must be part of the transport implementation.
* Clipboard is **not** shared by default between sessions; the paste path's
  clipboard restore is a host-session concern and does not apply inside an isolated
  session (text is set by the transport, not by the host clipboard).
* A VM's performance characteristics change timeouts that the QA harness assumes;
  the milestone must re-baseline them inside the isolated session.
