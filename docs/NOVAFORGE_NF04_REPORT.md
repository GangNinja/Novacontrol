# NovaForge NF-04 — Benchmark Lab

**Verdict: PASS WITH LIMITATIONS.**

NF-04 turns "how did that run behave?" into a repeatable measurement. A benchmark run is **one real
observation session** started through the existing `NovaForgeManager`, measured from that session's own
telemetry and evidence timeline, and persisted as a run record with a scenario version, a configuration,
typed metrics, evidence links and its limitations. There is no second session manager, no second store,
no second event bus, and no invented score.

> **UI correction, 2026-10-20.** The sidebar `Benchmarks NF-04` destination this phase added has been
> **removed**: it opened the same `novaforgePanel` as the `NovaForge` entry, so the lab lives only in its
> **Benchmark Lab** tab. The tab strip was also made to wrap, because with the sidebar entry gone an
> off-screen tab would have been the only route to the lab. See
> [NOVAFORGE_VERIFICATION.md](NOVAFORGE_VERIFICATION.md) §8.3.

The lab measures **observation and capture behaviour**. It invokes no model and no policy, sends no
input, launches no game and evaluates no objective. A run that ends with its required data is `completed`
— *data complete*, never "objective achieved"; every surface says so.

NF-05 (Controlled Game Evaluation) and NF-06 (Forge Coach) were **not started**.

---

## 1. What was built

| Area | Delivered |
| --- | --- |
| Scenario catalog | `novaforge/benchmark.py`: versioned `BenchmarkScenario` records with objective, kind, evaluation mode, target requirement, preconditions, reset procedure, defaults, minimum samples, observation source, capability flags, success criteria, verifier, required evidence, failure conditions, reproducibility rules, metric keys and limitations. One entry is explicitly **unsupported** (`gameplay-objective`) with the reason it cannot run. |
| Metric catalog | 25 typed `MetricSpec` entries (key, label, unit, kind `measured`/`derived`/`unsupported`, numerator/denominator, aggregation, eligibility, method and rounding) declared once. A run's metric rows carry their own label/unit/reason, so a later catalog edit cannot rewrite what an older run measured. |
| Configuration | `BenchmarkConfig` validates scenario id/version, target title, 1–300 s budget, 0.05–5 s interval, notes (≤500 chars) and model/policy ids; it also enforces the scenario's minimum frames for the latency percentiles (`at least 20 frames`). A boolean, a fraction of a second or a non-numeric value is refused rather than coerced (`true` is not one second, `2.9` is not two). Invalid input is a 422, never a silently clamped or truncated run. |
| Preflight | `preflight()` validates the scenario, the config and the **live** target window through the manager's own `validate()`, reports active-run/session conflicts, capture support, expected frame opportunities and the two applicable warnings. It starts nothing and creates no record. |
| Runner | `novaforge/benchmark_lab.py`: one active run at a time, started in a background task, bounded by the configured budget plus the manager's stop timeout and a hard grace deadline. Cancellation goes through the session's normal stop path (input cleanup and the intervention record still happen). Every transition is persisted. |
| Result | `completed / failed / aborted / invalid` decided from where the session actually ended (state, end reason, frame count, latency samples, runner cancellation/deadline), never from "the loop returned". `valid` is true only for `completed`. |
| Metrics collection | `collect_metrics()` reads the session's telemetry and timeline; a missing measurement is `available: false` with a non-empty reason (enforced in the row builder itself), never a zero. Values come from `telemetry.capture.*`, the session record and the timeline retention block, and each row names its source. |
| Aggregation | `aggregate_runs()` over **eligible** runs only (completed + valid) **and only those recorded on the same basis** — configuration, target geometry and operating system — with the newest eligible run defining the cohort. It reports attempts, eligible, sample size, status counts, every excluded attempt (by status or by configuration) with its reason, the cohort itself, and per-metric median/mean/n/min/max/stdev. Standard deviation is the sample formula and is `None` below two samples. |
| Fair comparison | `compare_runs()` refuses incompatible comparisons with a stated reason (scenario, version, budget, interval, window title/geometry, operating system, mixed configurations, non-eligible runs) and reports deltas only for metrics present on both sides. It never names a winner; the verdict is `not_established` with the reason. |
| Persistence | All runs under one `JsonStateStore` namespace (`novaforge_benchmarks`), bounded at 60 retained attempts, newest first. On every terminal transition the written record is **read back** and the run reports `persistence.state: verified`, or `failed` when the store dropped it; a run stored as `running` from a dead process is recovered as `aborted` with a reason — never upgraded. |
| Events | `novaforge.benchmark.started`, `.session_started` and `.finished` on the app-wide bus through the manager's existing publish seam. |
| API | 9 authenticated routes (catalog, scenario detail, preflight, history, start, run detail, cancel, compare, aggregate) registered in the handler, `ApiSurface`, `route_consumers`, `docs/API.md` and tests. |
| UI | Seventh Forge tab **Benchmark Lab** plus an enabled sidebar **Benchmarks (NF-04)** destination: catalog + scenario detail, target picker, budget/interval/notes, Validate and Start, live run card, history, aggregate, run detail with Export Result (JSON), compatible comparison, and the honest limitation notes. |

---

## 2. Scenario catalog

| Scenario | Version | Evaluation | Runnable | Minimum data |
| --- | --- | --- | --- | --- |
| `observation-baseline` | 1 | `observation_only` | yes | 4 fresh frames / 1 latency sample |
| `capture-latency` | 1 | `observation_only` | yes | 20 frames **and** 20 latency samples (p95 defined) |
| `observation-continuity` | 1 | `observation_only` | yes | 10 fresh frames |
| `gameplay-objective` | 1 | `objective_verified` | **no** | Not evaluable: no decision pipeline, no input path, no objective verifier |

The unsupported entry is a **catalog record**, not a hidden failure: the catalog endpoint, the scenario
detail, the preflight and the UI all report `supported: false` with the reason ("Running this scenario
would fabricate both the actions and the score; gameplay evaluation belongs to NF-05"). `POST
/novaforge/benchmarks/runs` with it returns 422.

Every runnable scenario declares `act: false` and `verify_objective: false` in its capability map, and
its verifier is `{available: false, kind: "none", reason: …}`. Those are facts about this build, not
placeholders for a future phase.

---

## 3. Metrics: measured, derived, unavailable

`collect_metrics()` fills the scenario's metric list from the session's own telemetry:

| Kind | Examples | Rule |
| --- | --- | --- |
| `measured` | `frames`, `stale_frames`, `capture_failures`, `frame_gaps`, `max_gap_ms`, `latency_median_ms`, `latency_p95_ms`, `latency_max_ms`, `duration_seconds`, `active_seconds`, `target_checks`, `target_losses`, `target_changes`, `timeline_events`, `interventions`, `errors` | Printed from telemetry/record/timeline as recorded. |
| `derived` | `frame_yield` (frames ÷ observation opportunities), `observed_rate_hz` (frames ÷ active seconds) | Computed from two measured values; the source string names the formula and both inputs. |
| `unsupported` | `objective_success`, `completion_time_seconds`, `actions_executed` | `available: false` with the reason (`no verifier`, `no action path`); the value stays `None`. **Never** zero. |

A p95 with fewer than 20 latency samples is reported `unavailable` and says how many samples exist. The
telemetry's bounded newest-latency window is named in the row's reason, so a percentile that covers only
that window is not presented as covering the whole run. Every unavailable row carries a reason — the row
builder substitutes one if a collector ever forgets, so a bare `null` cannot reach the UI. A missing
median specifically says no latency sample was recorded; it never quotes the p95 rule.

---

## 4. Run lifecycle and statuses

```
preflight (validates scenario + config + live window, starts nothing)
  → start (conflict check, run record "running", background task)
    → manager.start(...)  ── validate again through the manager's own path
      → poll until the session is terminal / cancelled / deadline
        → finalize: telemetry + timeline → metric rows → terminal status
          → persist → publish novaforge.benchmark.finished
```

| Status | Meaning | Enters aggregates |
| --- | --- | --- |
| `running` | Active; one per process. | no |
| `completed` | `STOPPED`/`COMPLETED` with end reason `time_limit` or `completed`, and the scenario's minimum frames/latency samples were recorded. Reason says "No objective is claimed: NF-04 has no verifier." | **yes** |
| `failed` | The session failed; the reason quotes the recorded error. | no |
| `aborted` | Cancelled, deadline hit, stopped outside the runner, application shutdown, or a stored `running` record recovered after a restart. | no |
| `invalid` | Start refused, target/record unavailable, evidence unreadable, or the run recorded fewer frames/samples than the scenario requires. | no |

Guarantees:

* **One slot.** A benchmark run uses the manager's one session slot, so preflight/start refuse while
  another run or a Mission Control session is active (409), and no second session can overlap it.
* **Cancellation is the session's stop path.** `cancel` sets a request flag, waits bounded for the task
  (stop timeout + 5 s grace), then reports the run's real state; a repeat is `already_stopped`.
* **A deadline is not a completion.** If the runner's own deadline passes, the session is cancelled and
  the run is `aborted`, not `completed`.
* **Start failures are records.** A `MissionValidationError`, session conflict/transition error or any
  other exception during start becomes an `invalid` run with the reason — an attempt that cannot run is
  still visible instead of vanishing into a 500.
* **Recording cannot raise.** `_persist()` swallows a store failure (the run continues; the next
  transition writes again), matching the persistent-store rule the rest of the build follows.

---

## 5. Fair comparison

`compare_runs()` answers "are these two sets of runs on the same basis?" **before** it computes a single
delta. It refuses with a typed reason when:

* any run on either side is not `completed` and valid;
* a side mixes configurations, target geometry or operating systems (a side is one measurement basis,
  so this is checked across every run on it, not only its first);
* scenario id or scenario version differs;
* execution budget or observation interval differs;
* the observed window title differs, or width/height/scale differ or are unrecorded on one side;
* the recorded operating systems differ.

It warns (without refusing) when operator notes differ or a run is marked interrupted. Compatible
comparisons return `samples` per side, per-metric left/right/delta (delta only when both sides are
numeric), the evidence endpoints for each side, and an explicit verdict:

> `not_established` — NF-04 invokes no model or policy, so no model can be declared superior. At least
> one side has a single run, which shows no variability; a higher or lower value is a measurement, not
> an established difference.

A UI hint repeats the same rule, so a user cannot read a delta as a win.

---

## 6. Aggregation

`GET /novaforge/benchmarks/aggregate?scenario=…&version=…` returns:

* `attempts` — every retained run of that scenario/version;
* `eligible` — completed and valid only;
* `cohort` — the configuration, target geometry and operating system the figures below describe (the
  newest eligible run defines it), with its `run_count` and `newest_run_id`;
* `sample_size` — eligible runs **inside that cohort**;
* `status_counts` — running, completed, failed, aborted, invalid;
* `mixed_configurations` / `configuration_excluded_count` — whether completed runs on another basis were
  left out, and how many;
* `excluded` — one entry per attempt that did not qualify, with `excluded_by` (`status` or
  `configuration`) and its reason;
* per-metric `value` (**median** of the cohort), `mean`, `n`, `min`, `max`, `stdev` (sample formula,
  `None` when `n < 2`), and the metric's unit/kind/reason.

A ten-second observation and a five-minute observation do not share a frame count, so they are never
pooled: a different budget, interval, target title or geometry, or operating system splits the cohort.
Operator notes are context, not a basis, and do not split it. Failed, aborted, invalid and
differently-configured attempts are **counted and listed**, never folded into a metric as zero. The
payload carries the same "data completeness, not a won game" note as the run record.

---

## 7. Persistence and restart

* All runs live in one namespace (`novaforge_benchmarks`), written on every terminal transition and
  every 20 polls of an active run.
* On a terminal write the lab reads the snapshot back and records `persistence.state: verified` with an
  empty reason, or `failed` with the reason the record could not be found (the store drops a snapshot it
  cannot replace by design, so "write returned" is not proof the run exists). A lab with no store reports
  `not_persisted`.
* Retention is 60 attempts; the oldest are dropped first and the history payload names the cap.
* `_load()` validates each stored run; an unrecognised status becomes `invalid` (preserved, not
  deleted), and a `running` record left by a dead process becomes `aborted` with the reason "the
  process that owned it is gone". A benchmark run is never resurrected as an active one.
* The application constructs the lab with the manager's own store and the shared publish seam, and calls
  `await novaforge_benchmarks.shutdown()` before the manager stops at application shutdown, so an active
  run is closed through the stop path and persisted rather than lost.

Measured snapshot size on this machine: **42,141 B for 3 runs** and **84,158 B for 6 runs** (~14 KB per
run), i.e. ~0.84 MB at the 60-run cap. The run record deliberately stores the scenario **snapshot** (what
the run was asked to measure), not a second copy of the metric definitions; those live in the catalog.

---

## 8. API

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/novaforge/benchmarks/scenarios` | Catalog with `supported`/`unsupported` status, verifiers, metric definitions, `metric_catalog`, `unsupported_metrics`, counts. |
| GET | `/novaforge/benchmarks/scenarios/{scenario_id}` | One scenario; optional `version`; unknown id/version is 404. |
| POST | `/novaforge/benchmarks/preflight` | Validates scenario + config + live window; returns `valid`, `errors`, `warnings`, `scenario`, `config`, `target`, `conflicts`, `runnable`, `expected_frames`, `limitations`. Starts nothing. |
| GET | `/novaforge/benchmarks/runs` | History, newest first; `limit` (default 20), `scenario` filter; retention block. |
| POST | `/novaforge/benchmarks/runs` | Starts one run; 422 for invalid config/scenario, 409 for an active run/session. |
| GET | `/novaforge/benchmarks/runs/{run_id}` | One run whole: config, scenario snapshot, metrics, evidence, classification, environment, model/policy reasons, limitations; 404 when unknown. |
| POST | `/novaforge/benchmarks/runs/{run_id}/cancel` | Stop through the session's stop path; idempotent once terminal; 404 when unknown. |
| GET | `/novaforge/benchmarks/compare` | `left`/`right` (comma-separated, bounded at 10 ids each); 422 for a bad selection, 404 for an unknown id. |
| GET | `/novaforge/benchmarks/aggregate` | `scenario`, optional `version`; 404 for an unknown scenario/version. |

All are authenticated, declared in `ApiSurface` and `route_consumers.py`, generated into `docs/API.md`,
and pinned by the route-contract tests. The compare/aggregate handlers use the same exception types the
lab raises (`BenchmarkValidationError` → 422, `BenchmarkNotFoundError` → 404), so the wire behaviour is
the module's behaviour rather than a second set of rules in the route.

---

## 9. UI

`index.html` gains a seventh Forge tab (`#forgeBenchmarkView`) and the sidebar **Benchmarks (NF-04)**
entry is now a working destination (Evaluation and Forge Coach remain disabled). `novaforge.js` gains a
benchmark module with these behaviours:

* **Catalog** — scenario select from `/benchmarks/scenarios`, detail rendered from the selected
  scenario (objective, support, evaluation mode, observation source, target requirement, default
  budget/interval, minimum frames, verifier and preconditions), and the unsupported entry shown with its
  reason instead of a runnable form.
* **Configuration** — live window picker from `/novaforge/windows` (the same titles validation uses,
  never a launch), budget/interval inputs seeded from the scenario defaults, notes, and a note that
  model/policy are unavailable because NF-04 invokes neither.
* **Validate** — preflight card renders the measured window geometry, budget/interval, expected
  opportunities and the two warnings. **Start stays disabled until a preflight passes**, and a failed
  preflight shows its errors without enabling it.
* **Active run** — state badge, run id, target, session and reason; polled only while a run is active,
  with a request token so a stale response cannot overwrite a newer render.
* **History** — every retained attempt with status, time, scenario/target, a compact metric line, the
  reason and an **Open** action; failed/aborted/invalid rows stay visible with their exclusion.
* **Aggregate** — the scenario's attempts/eligible/sample size/status counts, the cohort the figures
  describe (scenario version, budget/interval, window geometry, operating system), a note when completed
  runs on another configuration were excluded, and the per-metric table (median/n/min/max/stdev) with
  the "no verifier" note. An unavailable metric shows its reason in the value cell.
* **Run detail** — typed metric rows (value, kind, source/reason), configuration, evidence endpoints,
  environment, model/policy reasons, limitations and the `Export Result (JSON)` download of the run
  exactly as the backend recorded it.
* **Compare** — two run pickers that default to two distinct runs, the compatibility verdict, the
  metric deltas and the evidence links; an incompatible pair renders the refusal reasons instead of a
  table.
* **Honest figures in the shared formatter** — a missing duration (`null`, `undefined`, `""`) renders as
  `—`, not `0.0 s`; a measured `0` still renders `0.0 s`.
* Re-opening the tab refreshes the live window list but does not reset the budget/interval or discard a
  passing preflight when the target did not change; a failed window fetch does invalidate it.
* The run record's frame list is labelled **Frame references** because it holds the newest three
  references, not a retained-frame count.
* Loading, empty, unavailable, auth-error and conflict states are distinct; `node --check` passes.

The panel never claims a score: the banner, the model note, the comparison hint and the run reason each
state that NF-04 measures observation/capture behaviour.

---

## 10. Files changed

New:

* `src/novacontrol/novaforge/benchmark.py` — scenario catalog, metric catalog, config validation,
  metric collection, cohort aggregation, fair comparison, environment snapshot, limitations.
  (1,692 lines after the verification fixes.)
* `src/novacontrol/novaforge/benchmark_lab.py` — the run lifecycle, evidence collection, verified
  persistence, restart recovery, cancellation/shutdown and the read surfaces. (1,005 lines.)
* `tests/test_novaforge_benchmark.py` — 50 unit tests / 97 subtests (catalog, config, metrics,
  aggregate cohort, comparison, runner, persistence, events).
* `tests/test_novaforge_benchmark_api.py` — 15 HTTP tests through the real `create_app()` with only the
  desktop seam replaced.
* `scripts/benchmark_novaforge_benchmark.py` — measures runner wall time, finalize cost, read paths,
  snapshot size and restart recovery on this machine.
* `docs/NOVAFORGE_NF04_REPORT.md` — this report.

Modified:

* `src/novacontrol/novaforge/__init__.py` — exports (`NovaForgeBenchmarkLab`, `RUNS_NAMESPACE`,
  `BENCHMARK_VERSION`, the benchmark error types, the catalog helpers).
* `src/novacontrol/application.py` — constructs the lab over the real manager/store/publish seam and
  shuts it down before the manager.
* `src/novacontrol/api/app.py`, `api/models.py`, `api/route_consumers.py` — the nine routes in all
  registration places.
* `src/novacontrol/web/static/index.html`, `nc-os.css`, `js/novaforge.js` — the Benchmark Lab tab,
  sidebar destination, styles and renderers.
* `tests/test_novaforge_ui.py` — 7 new benchmark logic/renderer tests (catalog, preflight gating, honest
  unavailable/verdict text, no POST outside the two explicit lifecycle calls), plus the roadmap-card and
  planned-destination pins updated for NF-04.
* `docs/API.md` — regenerated.
* `README.md`, `docs/STATUS.md`, `docs/DEVELOPMENT_LOG.md` — NF-04 status, measurements and limitations.

---

## 11. Verification

Measured on this machine (Windows, Python 3.13) on 2026-10-18. That is **260 NovaForge tests** in all,
113 of them covering NF-04 (50 benchmark unit + 48 panel/logic + 15 benchmark HTTP), plus the route/API
contract files and both mypy views CI checks. The full repository suite was **not** run.

| Check | Result |
| --- | --- |
| `python -m pytest tests/test_novaforge_benchmark.py -q` | **50 passed, 97 subtests** (exit 0) |
| `python -m pytest tests/test_novaforge_ui.py -q` | **48 passed** (exit 0) |
| `python -m pytest tests/test_novaforge_benchmark_api.py -q` | **15 passed in 212.64 s** (exit 0) |
| `python -m pytest tests/test_novaforge.py tests/test_novaforge_timeline.py -q` | **116 passed** (exit 0) — NF-01/NF-03 regression |
| `python -m pytest tests/test_novaforge_api.py -q` | **31 passed in 397.49 s** (exit 0) — NF-01/NF-02/NF-03 HTTP regression |
| `python -m pytest tests/test_route_shape_contract.py tests/test_web_platform.py -q` | **63 passed, 425 subtests in 276.65 s** (exit 0) |
| `python -m mypy src --platform win32` | **clean over 422 files** (exit 0) |
| `python -m mypy src` | **clean over 422 files** (exit 0) |
| `python scripts/generate_api_reference.py --check` | `docs/API.md: in sync` (exit 0) |
| `node --check` on `static/js/*.js` + `static/app.js` | all pass |

### Defects found and fixed during the verification pass

A second, adversarial read of the module against its own stated contract found nine defects. Each was
reproduced first, fixed, and pinned by a test that fails on the old behaviour.

1. **Six unavailable metric rows shipped an empty reason.** `frame_gaps`, `max_gap_ms`,
   `latency_median_ms`, `duration_seconds`, `active_seconds` and `target_checks` returned
   `available: false, reason: ""` when telemetry was absent — exactly the bare null the phase forbids.
   Every collector now states its reason and the row builder substitutes one if a future collector
   forgets. Pinned across all three supported scenarios by `test_every_unavailable_row_carries_a_reason`.
2. **A missing median quoted the p95 rule.** The median row borrowed `p95_reason`, so a run with no
   latency sample reported "a p95 needs at least 20" beside a missing median. It now says no usable
   capture-latency sample was recorded (`test_a_missing_median_does_not_borrow_the_p95_reason`).
3. **Config coercion recorded a run nobody asked for.** JSON `true` became a 1-second budget/interval
   (`float(True)`/`int(True)`), and `2.9` seconds silently truncated to `2`. Booleans, non-finite values
   and non-integral seconds are now refused; `version: true` no longer resolves to version 1.
   (`test_booleans_and_fractional_seconds_are_refused_not_coerced`,
   `test_a_boolean_version_is_not_a_version_number`.)
4. **Aggregation pooled incomparable runs.** A 10 s and a 300 s run of the same scenario produced a
   "median 255 frames" figure even though comparison would refuse that pair. Aggregation is now
   cohort-scoped to the newest eligible run's configuration/geometry/OS, reports the cohort, and lists
   differently-configured completed runs as `excluded_by: configuration`
   (`test_aggregate_pools_only_the_newest_configuration`,
   `test_a_notes_difference_does_not_split_the_cohort`).
5. **Comparison checked only the first run on each side.** A side mixing 1000 px and 1920 px windows (or
   two operating systems) was accepted and produced deltas as if it were one basis. Each side is now
   checked across every run, with a targeted refusal
   (`test_a_side_mixing_window_geometry_is_refused`).
6. **A persisted run was assumed, not verified.** `JsonStateStore` drops a snapshot it cannot replace,
   so a successful `write()` said nothing about whether the run existed. A terminal run is now read back
   and reports `persistence.state: verified` or `failed` with the reason; a lab with no store reports
   `not_persisted`. Pinned by `test_a_store_that_drops_the_snapshot_is_reported_not_hidden` and the
   normal run's `verified` assertion.
7. **The UI printed a missing duration as `0.0 s`.** `Number(null)` is `0`; the shared formatter now
   returns `—` for `null`/`undefined`/`""` while still printing a measured `0.0 s`
   (`test_duration_formatting_is_honest_about_negative_and_missing_values`).
8. **The run card called three frame references "retained frames"**, claiming a count the record never
   held. It is labelled **Frame references** now.
9. **Re-opening the Benchmark tab reset the validated form**, and the compare pickers defaulted both
   sides to the newest run (so Compare refused its own default). Re-entry now refreshes only the window
   list and preserves the budget/interval/preflight when the target is unchanged; the pickers default to
   two distinct runs.

### Live panel verification

A preview server served the real application with the shipped static files and a **real**
`NovaForgeManager` over the real desktop capture path; the target was an already-open Notepad window.
Actions taken and observed in the browser:

1. Opened the sidebar **Benchmarks (NF-04)** destination: `#forgeBenchmarkView` became the selected
   Forge tab, the catalog read **3 runnable · 1 unsupported**, and the scenario detail rendered the
   observation source, target requirement, budget/interval, minimum frames and
   `verification: unavailable — No objective verifier is attached`.
2. Selected `start.ps1 - Notepad`, set a 4 s budget / 0.25 s interval and clicked **Validate**: the
   preflight card reported the live window at **1388 × 1294 px**, 17 expected opportunities, and the two
   warnings; **Start Benchmark** became enabled only after that pass.
3. Clicked **Start Benchmark**: the run completed (`completed · data complete`) with **6 fresh frames**,
   **201.242 ms** median capture latency, a **0.353** frame yield and 4.2 s observed duration. A second
   identical run completed with 7 frames / 206.828 ms.
4. The **Run History** listed both attempts with their metric lines; **Aggregate** reported
   `2 ELIGIBLE / 2 ATTEMPTS`, sample size 2, per-metric mean/min/max/stdev and the exclusions note;
   **Compare** reported `COMPATIBLE`, `samples 1 / 1`, the deltas, both evidence endpoints and the
   **`Not established`** verdict with its reason.
5. The **Run Detail** rendered the metric table (value/kind/source) and the
   **Export Result (JSON)** button enabled; the run state badge read `COMPLETED · DATA COMPLETE`.
   The browser console held **no errors** after the whole sequence.

Second live pass, after the fixes (`127.0.0.1:8015`, same real capture path):

6. Validated a 3 s / 0.1 s Notepad run (1388 × 1294 px, 31 expected opportunities), then switched to
   the Evidence tab and back: the budget, interval and the passing preflight were still there and
   **Start** was still enabled — the re-entry regression is gone.
7. Ran it twice. **Aggregate** read `2 SAMPLE / 4 ATTEMPTS`, named the cohort
   (`observation-baseline v1 · 3 s / 0.1 s · 1388 × 1294 px · Windows`) and explained that the two
   completed 4 s runs from the earlier session were excluded rather than averaged in.
8. The **Run Detail** showed `Run persistence: verified` beside the timeline's own persistence state;
   the browser's `NovaForgeLogic.formatDuration(null)` returned `—` while `formatDuration(0)` returned
   `0.0 s`.
9. The **Compare** pickers held two distinct runs, comparison stayed `COMPATIBLE` with the
   `Not established` verdict, and the console again held no errors. The verification server was stopped
   afterwards.

No game was launched and no gameplay input was sent. The desktop capture path was the real one, so the
figures above are this machine's real capture cost with Notepad as the target.

### Performance (measured, `scripts/benchmark_novaforge_benchmark.py`)

Deterministic 320×200 PNG capture seam, because the script must not record the user's screen. These are
runner/read-path costs, not a capture benchmark.

| Measurement | 3 runs × 2 s @ 0.1 s | 6 runs × 1 s @ 0.1 s |
| --- | --- | --- |
| Runner wall time (min / median / max) | 2114.47 / 2124.42 / 2140.20 ms | 1049.74 / 1146.18 / 1190.48 ms |
| Session overrun above budget | 20 / 80 / 60 ms | — (all completed) |
| Finalize (telemetry + timeline + metrics + verified write) | 17.72 / 19.02 / 19.84 ms | — |
| Collection step alone | 0.24 / 0.36 / 0.58 ms | 0.25 / 0.38 / 0.46 ms |
| `history` / `aggregate` / `compare` | 0.078 / 0.829 / 0.351 ms | 0.079 / 0.663 / 0.219 ms |
| Restart history read | 0.062 ms | 0.126 ms |
| Persisted snapshot for N runs | 42,534 B (3) | 84,931 B (6) |

The runner's own metric collection is **sub-millisecond**; finalize is **18–20 ms** because it now also
verifies the terminal write by reading the whole snapshot back (a 42 KB read measured at 13–17 ms on this
host, dominated by the file read rather than parsing), and the wall time is dominated by the observation
budget the scenario asks for plus a 20–80 ms session overrun on a 2 s budget (the loop's last tick and
shutdown). Persistence is ~14 KB per run because each record carries its configuration,
target geometry, environment, metric rows and evidence links; at the 60-run cap that is ~0.84 MB,
written on terminal transitions rather than per frame. Read paths (history, aggregate, compare, restart)
are all **under 1 ms** at this size.

---

## 12. Scope confirmation

NF-05 and NF-06 were **not started**: no evaluation workflow, no coach, no autonomous gameplay, no
gameplay input path, no objective verifier, no historical action re-execution, no model or policy
invocation, no game launching, no anti-cheat bypass, no second session manager/store/event bus. NF-04
adds two modules to the existing `novaforge` subsystem, nine routes, one panel tab and one sidebar
destination. It reuses NF-01's session lifecycle, NF-02's telemetry and Guardian record, and NF-03's
timeline and classification — that reuse is the point: a benchmark result is a **reading of evidence that
already exists**, not a parallel measurement stack.

---

## 13. Known limitations

* **No model, policy, action or objective evaluation.** The lab measures observation and capture only;
  `model`/`policy` are `available: false` with reasons, gameplay is a listed unsupported scenario, and a
  comparison's verdict is `not_established` rather than a winner.
* **One benchmark run at a time, sharing the one session slot.** Starting while a run or a Mission
  Control session is active is a conflict (409), by design.
* **Retention is 60 attempts.** Older runs are dropped; aggregate/comparison only read what is retained,
  and the history payload names the cap.
* **Snapshot growth is linear in retained runs** (~14 KB each, ~0.84 MB at the cap). No compaction was
  added; the run record stores the scenario snapshot rather than a second copy of definitions, which is
  the only size reduction made. Verifying a terminal write reads the whole snapshot back once
  (~14 KB per retained run), so the read-back cost grows with history size; it is bounded by the
  60-run cap and measured at under 1 ms at six runs.
* **The aggregate cohort is implicit.** The newest eligible run defines it; there is no API/UI control
  to select an older configuration's cohort, so a user who wants a 300 s summary after running a 10 s
  benchmark must re-run or read the history directly. The cohort is always named in the payload, so the
  figures cannot be mistaken for a pooled average.
* **A p95 needs 20 latency samples**; below that it is `unavailable`, and the percentile covers the
  telemetry's bounded newest window (named in the row), not necessarily the whole run.
* **Capture is primary-display, window-cropped**, so a window partly off-screen or on another display is
  measured as the capture backend sees it.
* **The live UI check used a real Notepad window** as the target; no game was launched and no gameplay
  input was sent, so "gameplay measurement" remains unverified by construction.
* **The full repository suite was not run** (the suite exceeds one tool window); only the files listed in
  §11 ran. `test_novaforge_benchmark_api.py` is slow on this host because `create_app()` performs
  Ollama/vision probes (~12 s per test with the environment's disable flag set for the brain but not the
  vision path); that cost is application boot, not the benchmark runner.
* **No statistical claim** is made anywhere: aggregates report n/min/max/stdev and comparisons report
  deltas, with the explicit statement that a single run per side shows no variability.
