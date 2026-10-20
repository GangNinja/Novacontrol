# NovaForge NF-02 — Forge Telemetry & Guardian

Status date: 2026-10-17. Scope: NF-02 only. **NF-03 was not started.**

Verdict: **PASS WITH LIMITATIONS** — every safety rule NF-02 claims is enforced by the existing
session manager and pinned by a test; the one guarantee the phase cannot honestly make (an action
budget over an action path that does not exist) is reported as unenforced on the wire, in the panel
and here, and accelerator/per-session resource telemetry is reported `unavailable` with a reason
rather than estimated.

## 1. What was implemented

NF-02 extends the NF-01 session foundation; it does not add a second session manager, event bus,
permission layer or input controller.

### Telemetry (`src/novacontrol/novaforge/telemetry.py`)

One bounded `TelemetrySnapshot` per session record, measured at the place each fact occurs (the
manager's tick, pause, resume, finalize) so a second observer cannot double-count:

* frames captured, stale captures, capture failures, frames with no usable duration;
* measured capture latency around the capture await: count, min, median, p95 (only from
  `P95_MIN_SAMPLES = 20`; otherwise `null` plus the reason), max, mean, and the exact window size
  (`LATENCY_WINDOW = 120`) used for the percentiles;
* last frame age (wall-clock label) and frame gaps, with the gap threshold derived from the
  configured loop cadence;
* active seconds, paused seconds, pause count, state changes, end reason, and an `interrupted` flag
  when the process stopped mid-session (persisted values are then labelled as the last values, not a
  reconstruction);
* target checks, losses, identity changes, unreadable probes;
* configured observation interval and an observed frame rate only when at least one second of active
  time and one frame exist;
* actions: `available: false` with a reason and `null` counts — NF-02 attaches no action path, so
  proposed/attempted/executed/failed/verified are **not measured and not zero**;
* resources: `cpu`, `memory`, `gpu`, `npu` each `available: false`, `value: null` and a reason. No
  per-session provider exists; machine-wide readings are not attributed to a session;
* measurement notes naming the monotonic clock, the percentile window and any anomaly.

Elapsed durations use `time.monotonic()`; ISO-8601 UTC timestamps are labels. Two memory bounds
exist: the latency window is 120 samples and the snapshot persists at most every
`persist_interval` (2 s) during ticks, while lifecycle moves persist immediately.

### Guardian (`src/novacontrol/novaforge/guardian.py`)

One payload, built from the authoritative `SessionRecord`, that the API, the panel and this report
share:

* `safety_state` (unarmed/starting/observing/holding/held/stopping/closed) and target-validation
  state (valid/lost/changed/unreadable/held/not_running/closed) with the reason;
* `input_safety_decision()` — the boundary a future input-producing phase must call. It refuses
  unless the session is RUNNING against its pinned target **and that identity was confirmed for this
  run** (`SessionRecord.target_validated`, set by a tick or by resume's fresh geometry check, cleared
  on a target event), and today it also refuses because no gameplay input path exists.
  `INPUT_PATH_AVAILABLE = False`; the flag is in the Guardian payload (`target.validated`) and is
  persisted, so a resumed session cannot ride on a check from before the pause;
* limits: time limit (`enforced` when configured), action budget (`configured_not_enforceable` when
  configured — no action path exists — never presented as a working bound), observation loop
  (`bounded: true`, one pending operation, no retries, frame/session caps);
* controls: emergency stop and cancel, both idempotent and bounded by `stop_timeout`;
* cleanup view (state, ok, held-after, presses/releases, note);
* interventions (bounded, newest last) and a guarantee table with `enforced` flags.

### Manager (`src/novacontrol/novaforge/manager.py`)

* Cancel (`_abort`) and emergency stop share one bounded path: record the request, set the stop
  event, cancel the task, wait at most `stop_timeout`, then finalize — one input release through the
  existing `release_all_inputs` seam. A repeated request on a terminal record returns
  `already_stopped: true`, `stop_latency_ms: null`, and does nothing;
* stop, cancel, emergency stop, loop exceptions and restart reconciliation all pass through the one
  `_finalize`, which releases input, closes telemetry, records the end reason and persists;
* the time limit is checked while running **and** while paused, measured with `time.monotonic()` so a
  wall-clock change cannot extend it;
* the observation loop records telemetry in place (no extra observer), and tick persistence is
  throttled while lifecycle moves persist immediately;
* the session cap is a bound on DISK as well as memory: an evicted session's frame directory is
  removed (contained to the frame root), and a restart that trims over-cap sessions writes the
  trimmed set back instead of leaving records the backend has already dropped;
* `history()` is a compact summary — it no longer ships the raw persisted snapshot (the latency
  window) that `SessionRecord.to_dict()` carries.

### API

| Route | Purpose |
|---|---|
| `GET /novaforge/sessions/{id}/telemetry` | telemetry + Guardian payload for one session (404 unknown, authenticated) |
| `POST /novaforge/sessions/{id}/cancel` | bounded abort + cleanup; idempotent; authenticated |
| `POST /novaforge/sessions/{id}/emergency-stop` | Guardian emergency stop; idempotent; authenticated |
| `GET /novaforge/sessions/{id}` | existing route, now also carries `telemetry`, `guardian` and `can.emergency_stop`/`can.cancel` |

All three new routes are registered in the handler, `ApiSurface` (`api/models.py`), the consumer
registry (`api/route_consumers.py`) and the regenerated `docs/API.md`, and are pinned as
`authenticated=True` by a test.

Events: `novaforge.guardian.intervention` is published after a confirmed abort on the existing
app-wide bus (payload: `session_id`, `kind`, `reason`, `outcome`, `latency_ms`); existing
`novaforge.session.changed` and `novaforge.frame.captured` are unchanged.

### UI (Forge Mission Control)

* Two new tabs: **Telemetry** (Capture, Session Continuity, Target & Loop, Resources & Actions,
  Measurement Notes) and **Guardian** (safety state, enforced boundaries, target & input, input
  cleanup, interventions, guarantees, Emergency Stop);
* a **Cancel** button joins the lifecycle controls; all control enablement follows the backend's
  `can` flags and the authoritative session state;
* `metricText` renders `0` as a measurement and `null`/`NaN`/empty as `unavailable`;
* the emergency stop disables itself until the backend answers, then reports the confirmed result
  (state, cleanup, bounded wait) — a refusal reports `NOT confirmed`;
* stale-response protection: every authoritative render bumps a token and an in-flight poll whose
  token is older is discarded (`responseStillCurrent` is a pure, Node-tested rule);
* NF-02 remains the roadmap row that is implemented; the sidebar keeps only NF-03…NF-06 disabled.
  Evidence/Benchmarks/Evaluation/Forge Coach stay unavailable.

## 2. Files changed

New: `src/novacontrol/novaforge/telemetry.py`, `src/novacontrol/novaforge/guardian.py`,
`scripts/benchmark_novaforge_telemetry.py`, this report.

Modified: `novaforge/models.py`, `novaforge/manager.py`, `novaforge/__init__.py`,
`api/app.py`, `api/models.py`, `api/route_consumers.py`, `web/static/index.html`,
`web/static/nc-os.css`, `web/static/js/novaforge.js`, `tests/test_novaforge.py`,
`tests/test_novaforge_api.py`, `tests/test_novaforge_ui.py`, `docs/API.md`, `docs/STATUS.md`,
`docs/DEVELOPMENT_LOG.md`, `README.md`.

Pre-existing NF-01 and earlier-phase modifications in the working tree were preserved; nothing was
reset, stashed, committed or pushed.

## 3. Measurement methodology

| Figure | Source | Rule |
|---|---|---|
| Capture latency | `time.perf_counter()` around the capture await in `_tick` | measured; a capture that reports no usable duration is counted as a frame with `invalid_samples` and excluded from the distribution |
| Percentiles | nearest-rank over the sorted latency window | median for any non-empty window; p95 only from ≥ 20 samples, with the reason otherwise |
| Active/paused time | `time.monotonic()` segments closed at pause/resume/close | wall-clock timestamps are labels only |
| Frame gaps | monotonic delta between two successful frames | pauses reset the previous frame, so an intentional pause is not a stall |
| Frame rate | frames ÷ active seconds, only with ≥ 1 s active and ≥ 1 frame | otherwise `null` + reason |
| Target checks/events | counted in the loop where they occur | losses/changes/unreadable are distinct |
| Actions/resources | not measured here | `available: false`, `null`, explicit reason |
| UI | renders only the payload above | no client-side estimates |

## 4. Tests and exact results

All commands run with `PYTHONPATH=src` on this Windows host (Python 3.13.0).

| Command | Result |
|---|---|
| `python -m pytest tests/test_novaforge.py -q` | **74 passed** in 10.64 s, exit 0 |
| `python -m pytest tests/test_novaforge_ui.py -q` | **25 passed** in 0.36 s, exit 0 |
| `python -m pytest tests/test_novaforge_api.py -q` | **23 passed** in 294.33 s, exit 0 (each test builds the real app; the runtime is the pre-existing fixture cost) |
| `python -m pytest tests/test_novaforge.py tests/test_novaforge_ui.py tests/test_route_shape_contract.py -q` | **117 passed, 90 subtests passed** in 273.09 s, exit 0 |
| `python -m pytest tests/test_web_platform.py::BackendFrontendContractTests -q` | **10 passed, 289 subtests passed** in 0.11 s, exit 0 |
| `python -m pytest tests/test_web_api.py -k "static or asset_versions..."` | **3 passed, 12 subtests passed** in 51.00 s, exit 0 (cache-header and asset-version pins for the edited static tree) |
| `python -m mypy src` | Success: no issues found in **419 source files**, exit 0 |
| `python -m mypy src --platform win32` | Success: no issues found in **419 source files**, exit 0 |
| `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync`, exit 0 |

The NovaForge scope now runs **122 tests** (74 unit + 25 panel + 23 HTTP), up from 72 in NF-01
(+49). The panel suite is two layers: Node-executed pure decisions, and a NEW DOM-shim suite that
evaluates the REAL `renderTelemetry`/`renderGuardian` and inspects the rows they produce (zero,
unavailable, unsupported, enforced and NOT-enforced, plus the empty state). The full repository
suite was not run in this pass; the focused suites, route contracts and both mypy views above were.

### Live verification (real server, real disposable window)

A uvicorn instance was started on a free port (127.0.0.1:8017) against this working tree and the
Forge panel was driven in the browser against a disposable Tk window (`NC NF02 UI PROBE`), created
for the check and killed afterwards:

* Telemetry tab: **22 frames**, 0 stale, 0 capture failures, 0 gaps, largest gap 1359.4 ms (the
  first frame after the persistent worker spawn), last frame age 664 ms, percentile basis "all 22
  sample(s)"; CPU/Memory/GPU/NPU rendered `unavailable` and Actions `unsupported` with their reasons.
* Guardian tab: safety `observing`, reason "The newest geometry check matched the pinned handle and
  pid.", time limit `enforced`, observation loop `bounded`, action budget `unsupported` (unconfigured)
  and the guarantee list showing `action budget` and `gameplay input` as **NOT enforced**.
* Emergency Stop: disabled while nothing was stoppable, enabled for the live session, and only after
  the backend answered it reported **"Emergency stop confirmed by the backend: session STOPPED,
  input cleanup ok (no held input), bounded wait 8s."** The persisted intervention reads
  `emergency_stop / stopped / 5.8 ms`, and cleanup `ok`, `held_after 0`, releases/presses `0/0`.
* A 45 s time-limit session ended as `STOPPED/time_limit` with the explicit "objective was NOT
  verified" error, as designed.

This live pass found one real defect the unit tests had not asserted: a Guardian abort persisted
`end_reason: "stopped"` (the finalize default) instead of naming the abort. `_abort` now sets
`end_reason` to the abort reason, and both cancel and emergency stop pin it in the unit and HTTP
tests.

### Required coverage map

1. Aggregation of known synthetic measurements — `TelemetryAggregationTests` (latency, gaps, stale,
   failures, pause segments, round-trip).
2. Percentiles with sufficient and insufficient samples — p95 at 40 samples = 38 ms, p95 `null` with
   its reason at 5 samples, bounded window basis test.
3. Missing/unavailable/unsupported/zero — zero frames is `0`; actions and GPU are `available: false`
   with `null` and reasons; malformed persisted values fall back.
4. Stale frames, failures, gaps, target loss — counters in telemetry, target-loss pause + Guardian
   intervention, no-frames-after-stop.
5. Pause/resume accounting and duration — monotonic segments and pause count.
6. Time-limit enforcement while running and paused — NF-01 tests retained, now monotonic.
7. Action budget — configured but `configured_not_enforceable`, on the wire and in `guarantees`.
8. Repeated stop/cancel/emergency-stop — idempotent, one release.
9. Input release on stop, cancel, exception, timeout — cancellation test, injected loop exception
   (FAILED) test, slow-loop bounded stop test, raising-release reported as failed.
10. Target-validation failure — `input_safety_decision` refuses, Guardian says `lost`.
11. Bounded shutdown / no actions after emergency stop — stop-latency bound and frame-count test.
12. Permission refusal and API authentication — a refusing permission layer creates no session; the
    new routes are pinned `authenticated=True`.
13. Persistence and restart — telemetry, interventions and cleanup survive a restart; the session
    cap evicts records, their frame directories and their snapshot entries (never the active one).
14. API schemas and route registration — telemetry/emergency/cancel over the real `TestClient`, plus
    `ApiSurface` registration; a history row carries no raw latency window.
15. UI unavailable values, stale responses, stop outcomes — Node-executed `metricText`,
    `guardianLimitState`, `responseStillCurrent`, `controlAvailability`, the DOM-shim renderer suite
    (`NovaForgeRendererDomTests`) over real payload shapes, plus the static contract that success is
    only claimed after the request.
16. NF-01 regression — the original 72 NF-01 tests are retained; the pre-existing suites above pass.

### Verification pass (2026-10-17)

The phase was re-audited against its objectives by probing each seam and reading the persisted and
served state back, not by re-reading this report. **Six defects were found and fixed**, each with a
regression test:

1. **The input boundary trusted session state alone.** `input_safety_decision()` checked RUNNING +
   a pinned target, but the identity check that confirmed that target belonged to the session, not
   to the current run: after a pause whose resume re-validated the window, or before the first tick,
   the old confirmation still read as valid. `SessionRecord.target_validated` now records whether a
   geometry check has confirmed THIS run's identity (set by a matching tick and by resume's fresh
   check, cleared when the target is lost/changed/unreadable), is persisted, is shown as
   `guardian.target.validated`, and the boundary refuses on it. Pinned by
   `test_target_validated_flag_gates_the_input_boundary` (unconfirmed → `pending`, after a tick →
   valid, after a loss → cleared, after resume → re-confirmed).
2. **The session cap was not a disk bound.** Evicting a session from memory left its frame directory
   behind forever, so `max_frames_on_disk` bounded one session while the installation grew without
   limit. `_trim_sessions` now removes the evicted session's directory (resolved and confined to the
   frame root) and never evicts the active session; `test_evicted_sessions_take_their_frames_with_them`
   pins the removal and the surviving sessions (mutation-checked: with `_drop_frames_dir` made a
   no-op, the test fails).
3. **A restart resurrected evicted sessions on disk.** Loading an over-cap snapshot trimmed memory
   but only wrote the trimmed set back if it had also reconciled an active record, so the file kept
   listing sessions the backend had dropped. `_load` now persists when it trimmed;
   `test_a_lowered_session_cap_is_written_back_on_load` writes three sessions under a larger cap,
   reloads with a cap of two, and asserts the stored snapshot holds exactly the survivors
   (mutation-checked: hiding the trim count from `_load` makes it fail).
4. **`history()` contradicted its own "compact rows" contract.** Every row carried the RAW persisted
   telemetry snapshot, including the 120-float latency window, because it comes from
   `SessionRecord.to_dict()`. The raw snapshot is dropped from history rows (unit assertion) and
   `latency_window_ms` must not appear anywhere in `/novaforge/sessions` (HTTP assertion).
5. **The cleanup note could claim counters that did not match.** When the release path reported more
   releases than presses, no input was held (so `ok: true` was right) but the note still said "presses
   matched releases". It now says the counters disagree; `test_cleanup_note_does_not_claim_matched_counters_when_they_differ`
   pins it.
6. **The telemetry benchmark timed half of what it claimed.** `measure_snapshot` timed only
   `json.dumps(payload)`, while its note and the report said `to_dict()` + `json.dumps`. Both are now
   timed (the manager serializes with `to_dict()` before the store writes), and §5 carries the
   re-measured figures.

The panel gained a DOM-level pin in this pass as well: `NovaForgeRendererDomTests` evaluates the real
`renderTelemetry`/`renderGuardian` against a minimal DOM shim with live payload shapes, so "zero is a
measurement", "unavailable", "unsupported", "configured — NOT enforced" and the empty state are
asserted on the rows the panel builds, not on source substrings. The suite went from 104 to **122
tests**; both mypy views, the API-reference check, `BackendFrontendContractTests` (10 tests / 289
subtests) and the route-shape contract (18 tests / 90 subtests) are green on this host.

## 5. Performance

Baselines below are **historical measurements, not targets**.

* NF-01 report: median frame-capture latency ≈ **135.6 ms** in its disposable-window run; earlier
  remediation: ≈ **8.2 fps** steady capture, ≈ **1.35 s** warm rich observation.
* `python scripts/benchmark_screen_capture.py --frames 6 --baseline-frames 2` (NF-02 pass, this
  host): persistent worker steady state median **119.7 ms**, mean **120.5 ms** → **8.3 fps**;
  all-frames median 121.2 ms with the first worker spawn as the p95 outlier (812.7 ms); one-shot arm
  median 619.4 ms → 1.61 fps. 6/6 frames distinct and fresh; worker closed, `pid_gone: true`.
* `python scripts/benchmark_novaforge_telemetry.py --frames 60 --iterations 20000 --legacy-persist`,
  re-run three times in this verification pass: telemetry aggregation **3.73 / 4.22 / 4.22 µs per
  iteration** (one `record_frame`, a full `describe` every 10th); snapshot serialization **0.0725 /
  0.0827 / 0.1121 ms** — `SessionRecord.to_dict()` **and** `json.dumps` over 30 retained frames, which
  is what the loop actually pays. The earlier 0.0407 ms figure timed only `json.dumps` and is
  superseded.
* The two 60-frame arms now separate consistently: throttled persistence **2.113 / 2.124 / 2.139 s**
  versus write-every-frame **2.323 / 2.385 / 2.389 s** — **0.21–0.26 s per 60 frames**, i.e. ~9–11 %
  of loop wall time at this cadence (the loop is cadence-bound at ~28 vs ~25 fps here). The
  throttled arm always runs FIRST, so cold caches work against it and arm order does not explain the
  difference. This supersedes the earlier "difference 0.008 s ≈ noise / no speedup claimed" note:
  the per-frame `JsonStateStore` write is a measured loop cost, not a theoretical one. It remains a
  policy measurement on a synthetic capture, not a claim about real gameplay.

No blocking I/O was added to the loop: telemetry is in-memory aggregation; persistence is throttled;
the UI polls the existing endpoint.

## 6. Safety guarantees: enforced vs not

**Enforced (each with a test):**

* Time limit: running and paused, monotonic, ends `STOPPED/time_limit`; the objective is not claimed.
* Emergency stop and cancel: bounded by `stop_timeout`, cancel the loop, finalize exactly once,
  release input through `release_all_inputs`, report the confirmed result, idempotent.
* Input cleanup: runs in the single `_finalize` path for stop, cancel, emergency stop, loop failure,
  start timeout and restart reconciliation; a raising release is reported `ok: false` with the
  reason, never swallowed.
* Target identity: handle/pid pinned at start, checked every tick and on resume; loss/change/
  unreadable pauses the session and `input_safety_decision` refuses.
* Bounded loop/queue/retries: one pending operation, no retries, bounded frame and session caps.
* Guardian interventions recorded and persisted, including restart reconciliation.

**Not enforced / unavailable (reported as such, never simulated):**

* Action budget — no action-execution path exists. `max_actions_enforced: false`,
  `action_budget.enforced: false`, `state: configured_not_enforceable`.
* Gameplay input safety — NF-02 sends none, so the guarantee is vacuous today; the boundary
  (`input_safety_decision`, `INPUT_PATH_AVAILABLE = False`) is the integration point for the phase
  that adds input.
* GPU/NPU and per-session CPU/RAM telemetry — `available: false` with a reason.
* Objective completion/verification — no verifier; `COMPLETED` remains unreachable.

## 7. Failures, limitations and unverified items

* The action budget cannot fire in NF-02 by design; a later phase with a real action path must both
  count actions and wire the budget to it.
* The persistence-policy measurement runs its two arms sequentially (throttled first) rather than
  interleaved, so a residual warm-up effect cannot be fully excluded; the direction of that bias
  (the throttled arm colder) argues against the measured difference being an artefact.
* The session-cap trim deletes an evicted session's frames. That is the intended bound, but it means
  a person cannot inspect a run older than the newest `max_sessions_kept`; raising the cap is the
  supported way to keep more.
* No input-producing operation exists, so Guardian's target revalidation cannot be demonstrated
  against live input; it is tested at the policy boundary it will be called through.
* `windows_available` remains a capability claim rather than a live probe (NF-01 residual).
* Accelerator telemetry is unavailable on this host and no provider is shipped for it.
* The observed frame rate is intentionally `null` until 1 s of active time; a very short session
  reports the reason instead of a rate.
* The full repository suite was not run; CI (Ubuntu) may surface host differences. The focused
  suites, both mypy views and the API-reference check are green on this host.
* The live UI pass above was driven through the served page and the real API; a screenshot could not
  be captured because the embedded preview webview was not compositing frames, so the evidence is
  the page's own DOM/API output rather than an image.
* Performance figures are single-machine measurements with a small number of frames; they are
  reported as measured, not as targets.
* The UI was verified through Node-executed pure logic, the HTTP contract and the static asset
  tests, not by replaying a race in a live browser.

## 8. Platform notes

* Windows: PowerShell persistent capture worker; emergency stop measured on this host.
* Non-Windows: capture is unsupported, so the panel reports `capture_supported: false` and Start is
  refused with a reason; telemetry/Guardian logic itself is platform-independent.
* No game was launched, no online service was touched and no anti-cheat surface was addressed.

## 9. Verdict

**PASS WITH LIMITATIONS.** Telemetry is measured and honest about the missing figures; the Guardian
enforces every boundary that exists in this build, records its interventions, and bounds its shutdown
and input cleanup; the single guarantee it cannot make (an action budget over no action path) is
labelled unenforced in the payload, the UI and this report. **NF-03 (Evidence Timeline & Replay) was
not started.**
