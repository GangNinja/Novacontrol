# Full-Suite Failure Remediation Report

**Repository:** NovaControl / NovaMarket
**Date:** 26 November 2026
**Status:** **COMPLETE — the broader suite is green.**
**Scope:** Investigate and fix every failure in the prior 8,137-passed / 17-failed
broader-suite baseline without weakening authentication, trading controls, test
assertions, or skipping any test.

## Verdict

The 17 baseline failures were resolved by fixing the genuine defects found in
production and test infrastructure and correcting the stale or environment-
sensitive test contracts.
The final 44-group broad run completed with **8,154 passed, 0 failed, 3 skipped
and 6,426 subtests passed**. All 8,157 runtime outcomes were accounted for; no
group timed out and no collected test file was omitted.

No Stage C work, live trading, RL promotion, commit, or push was performed.

## Baseline and final result

| Measure | Prior baseline | Final run |
|---|---:|---:|
| Collected tests | 8,156 | 8,156 |
| Runtime outcomes | 8,157 | 8,157 |
| Passed | 8,137 | **8,154** |
| Failed | 17 | **0** |
| Skipped | 3 | 3 |
| Subtests passed | 6,426 | 6,426 |
| Bounded groups | 44 | **44, all exit 0** |
| Timed-out groups | 3 (300 s guard) | **0** |

The final run used `tmp/full_suite_groups/run_all.py 1 44` with a 1,800-second
per-group guard, then retained the same fixed 44-file grouping. The guard change
prevents a slow-but-healthy group (for example the 310-second NovaMarket block)
from being recorded as a timeout. No test was deselected.

The three final skips are intentional and pre-existing:

1. `tests/test_novamarket_marketdata.py::LiveProviderTests::test_yahoo_reports_a_real_delayed_quote`
   — requires `NOVAMARKET_LIVE_TESTS=1`.
2. `tests/test_novamarket_marketdata.py::LiveProviderTests::test_coingecko_reports_a_real_crypto_quote`
   — same live-provider opt-in.
3. `tests/test_database.py` module-level skip — the optional `sqlalchemy`
   `database` extra is not installed in this environment.

None is a hidden failure. The two live-provider tests are deliberately excluded
from offline CI and no live provider call is claimed.

## Root-Cause Matrix — the original 17 failures

| ID | Test(s) | Classification | Root cause | Resolution |
|---|---|---|---|---|
| A | `test_automation.py::AutomationApplicationTests::test_a_spoken_schedule_mention_plans_but_does_not_execute` | Outdated test contract | The request is audited as `source="request"`, but the application trail also contains legitimate NovaMarket lifecycle rows. The old assertion required **every** row to be a request row. No automation ran; the task stayed `pending_approval`, disabled, zero runs. | Assert no `source="automation"` row, exactly one matching request row, and no automation-run evidence. Lifecycle rows remain. |
| B | `test_browser_token_reuse_e2e.py::BrowserTokenReuseE2ETests::test_approve_and_run_reuses_token_without_replanning` | Test isolation + production responsiveness | The uvicorn subprocess loaded the developer `.env` (`NOVACONTROL_ADMIN_TOKEN`), disabling anonymous loopback mode, so `/browser/plan` returned 401. After isolation, the API still stalled because radar ingestion ran regex work synchronously on the event loop. | `NOVACONTROL_SKIP_DOTENV=1` in shared conftest; cache compiled term patterns; move extraction to a worker thread and precompute per-refresh entity hints. Token validation itself was never weakened. |
| C | `test_build_save.py::BuildSaveApiTests::test_save_over_http` | Incomplete async test double | Shutdown awaits `nova.novamarket.stop_runtime()`, but the mocked application left it as a synchronous `MagicMock`. | Configure `stop_runtime` as `AsyncMock`. Production still awaits the real async lifecycle. |
| D | `test_evaluation.py::ServiceTests::test_retention_removes_old_rows_from_every_store` | Time-dependent test | A hard-coded “fresh” row at 2026-09-30 became 57 days old against the real clock and was pruned with the deliberately old row. | Use `datetime.now(UTC)` for the reference instant. The 90-day-old row remains the only expired row; all three stores are still checked. |
| E | `test_focus_rings.py::FocusRingCssContractTests::test_every_focus_visible_rule_declares_a_ring` and `::test_no_base_outline_none_without_focus_restoration` | Genuine accessibility defect | `market-workspace.css` combined hover and `:focus-visible` selectors and set `outline: none`, suppressing the keyboard ring. | Split hover from focus; both `.nav-item:focus-visible` and `.button:focus-visible` now declare an explicit fallback ring and offset. |
| F | `test_focus_rings.py::FocusRingBrowserWalkTests::test_tab_walk_shows_2px_solid_focus_ring_everywhere` | Test isolation + harness encoding | The subprocess inherited the developer `.env`, so the page rendered an authentication error instead of real controls. The Node walker’s output was also decoded with the Windows locale, crashing the reader thread on non-ASCII output. | Shared `.env` isolation; decode the walker with UTF-8 and `errors="replace"`. The 2 px solid ring assertion is unchanged. |
| G | `test_novaforge_state.py::StateApiTests::test_a_pinned_profile_id_is_honoured` | Test-fixture timestamp defect | `FakeObserver` stamped its synthetic frame at construction. Slow grouped startup made the frame older than the 5-second freshness limit, and production correctly rejected it as stale. | Default the fake timestamp to read time; explicitly stale/unknown fixtures still pass their own timestamps. Production stale rejection is unchanged. |
| H | `test_novaforge_timeline.py::ReplaySafetyTests::test_timeline_routes_are_get_only_and_authenticated` | Outdated route count | Four GET, authenticated timeline routes legitimately exist: three NovaForge routes plus `/market/event-intelligence/timeline`. | Assert the exact four-route inventory instead of a stale count of three. |
| I | `test_reduced_motion_e2e.py::ReducedMotionPlaywrightTests::test_live_os_toggle_stops_and_restarts_canvas_and_tilt` | Test isolation + probe selection | The `.env`-induced auth state removed the real cards. After isolation, the probe still picked the first on-screen card — the interactive command console — which `effects.js` deliberately excludes from tilt. | Shared `.env` isolation; the probe now picks the first on-screen card without interactive descendants, matching the handler’s eligibility rule. Motion assertions are unchanged. |
| J | `test_tab_focus_walk_e2e.py::TabFocusWalkPlaywrightTests::test_focus_predicate_has_teeth` | Test isolation | The wait was satisfied by the static shell even when API calls returned 401, leaving no stable real control for the negative control. | Shared `.env` isolation; the injected negative control is unchanged. |
| K | `test_tab_focus_walk_e2e.py::TabFocusWalkPlaywrightTests::test_tab_walk_shows_2px_solid_outline_on_every_interactive_element` | Test isolation | The page displayed “Authentication required.”; the only focused element was an error-history div with the browser’s 1 px auto outline. | Shared `.env` isolation; the 2 px solid ring requirement is unchanged. |
| L | `test_telemetry_e2e.py::TelemetryPlaywrightTests` (3 tests) | Test isolation + live-value race | `/system/telemetry` returned 401 in subprocesses. After isolation, the memory-bar check compared a card painted by the last 2-second poll with a later API reading; live memory moved by three points. | Shared `.env` isolation. The bar is now checked against the card’s own displayed used/total values (deterministic), while availability/value assertions still use the API payload. |
| M | `test_guided_click_e2e.py::GuidedClickVerifiedTests::test_verified_click_renders_card_and_resolves_open_bug` and `::GuidedClickUnverifiedTests::test_unverified_click_reports_honestly_and_records_bug` | Test isolation + cleanup timing | The subprocess inherited the admin credential and rendered “Authentication required.” After isolation, Windows still held the temporary SQLite file briefly after `terminate()`, so `TemporaryDirectory.cleanup()` raised `WinError 32` even though the test passed. | Shared `.env` isolation; retry disposable-directory cleanup for up to 10 seconds and still fail if the handle never clears. |

## Additional defects exposed after the original 17

| ID | Finding | Classification | Resolution |
|---|---|---|---|
| N | Radar refresh starved the uvicorn event loop | Genuine production defect | `radar/extraction.py` compiled a regex per instrument alias per feed entry; `radar/engine.py` also rebuilt instrument/entity tables per entry. The API stopped answering during a refresh, which is why browser token reuse still timed out after authentication was fixed. Compiled term patterns are now `lru_cache`d, entity hints are built once per refresh, and extraction runs via `asyncio.to_thread`. Matching semantics are unchanged; radar tests pass. |
| O | Guided-click temp cleanup race | Test-infrastructure defect | Bounded retry for the Windows file-handle release window; a persistent lock still raises. |
| P | Telemetry memory bar compared two different instants | Test race | Compare the bar to the percentage implied by the card’s own displayed used/total bytes. This still detects a decorative or mis-mapped bar without requiring live memory to stand still. |
| Q | Reduced-motion tilt probe chose an ineligible card | Outdated probe contract | The command console contains controls and is intentionally not tilted by `effects.js`; the probe now applies the same eligibility rule. |
| R | Focus walker crashed its output reader | Test-harness encoding defect | Decode Node’s stdout as UTF-8 instead of the Windows locale code page. |
| S | Preference API tests depended on live free memory | Test-environment defect | After a long suite the live-memory estimate could become `UNSAFE`, turning `start` into a 409 before the tested behavior. `PreferenceApiTests` now supplies a deterministic 64 GiB estimate. The estimator’s own safety tests still exercise refusal and override behavior. |

## Shared test-infrastructure finding

`create_app()` intentionally calls `load_dotenv()` before reading security
configuration. In-process pytest applications were already protected because
the loader returns early when `pytest` is imported. Browser E2E specs launch
uvicorn in a **subprocess**, where pytest is absent from `sys.modules`; that
subprocess loaded the developer `.env` and switched the server out of tokenless
loopback mode.

`tests/conftest.py` now sets, for the test process and every spawned server:

- `NOVACONTROL_ALLOW_ANONYMOUS_LOCAL=1` (the explicit development opt-in, never
  an application default);
- `NOVACONTROL_SKIP_DOTENV=1` (the subprocess-safe guard that prevents a local
  credential file from changing test security policy).

Production still loads `.env` normally. No production authentication or
authorization branch was disabled or bypassed.

## Production change detail

`MarketEventRadar.ingest` now:

1. reads the instrument table once per refresh;
2. builds entity hints once per refresh;
3. passes those hints into each entry’s ingestion;
4. awaits `extraction_module.extract` in `asyncio.to_thread`.

`extraction._term_pattern` compiles each alias once with an LRU cache instead of
compiling it inside the per-entry/per-alias loop. Both changes preserve the
existing word-boundary matching behavior and the radar’s classification results.

## Verification evidence

### Final broad run

```text
runner started 19:10:50
...
runner finished 20:28:29 chunks=44 failures=0
state DONE, 44 chunks, 215 files
8,154 passed, 0 failed, 3 skipped, 6,426 subtests passed
8,157 runtime outcomes (8,156 collected + one module-level runtime skip)
```

Every chunk exited 0. The chunk inventory contains exactly 215 unique files,
which equals the 215 `tests/test_*.py` files on disk: no file is missing or
listed twice. The slowest group (`chunk_43`, `test_web_api` +
`test_web_platform`) completed in 306.9 s; `chunk_23` completed in 310.2 s.
Neither hit the 1,800-second guard.

### Targeted reruns

| Test file | Result |
|---|---|
| `test_automation.py` | 50 passed, 10 subtests |
| `test_build_save.py` | 9 passed |
| `test_evaluation.py` | 110 passed, 6 subtests |
| `test_novaforge_state.py` | 60 passed |
| `test_novaforge_timeline.py` | 42 passed |
| `test_novamarket_radar.py` | 71 passed, 9 subtests |
| `test_novamarket_pi24.py` | 32 passed |
| `test_browser_token_reuse_e2e.py` | 1 passed (67.18 s) |
| `test_guided_click_e2e.py` | 2 passed (63.61 s) |
| `test_telemetry_e2e.py` | 3 passed (184.68 s) |
| `test_reduced_motion_e2e.py` | 2 passed (95.54 s) |
| `test_focus_rings.py` | 7 passed (76.21 s) |
| `test_tab_focus_walk_e2e.py` | 2 passed (143.28 s) |
| Six-file block including `test_preference.py` | 212 passed, 8 subtests (126.38 s) |

### Stage, type, frontend and API checks

| Check | Command | Result |
|---|---|---|
| Stage A focused regression | 13 NovaMarket AT/marketdata/autonomy files | **377 passed, 2 skipped, 9 subtests**, exit 0 (166.79 s) |
| Stage B isolated lifecycle | `tests/test_novamarket_at06_end_to_end.py` | **1 passed**, exit 0 (0.77 s) |
| Mypy (Linux view) | `python -m mypy src` | **728 files, no issues**, exit 0 |
| Mypy (Windows view) | `python -m mypy src --platform win32` | **728 files, no issues**, exit 0 |
| Frontend build | `cd web && npm run build` | exit 0 (`tsc -b && vite build`, 6.32 s; only the pre-existing dynamic-import and chunk-size warnings) |
| API reference | `python scripts/generate_api_reference.py --check` | **in sync, 936 routes**, exit 0 |
| Security + database | `test_security_remediation.py` + `test_novamarket_database_resilience.py` | **27 passed**, exit 0 (10.00 s) |
| Whitespace | `git diff --check` | exit 0 |

The two Stage A skips are the live-provider opt-ins in
`test_novamarket_marketdata.py`; they are not failures.

## Boundaries

- No real-market acceptance, live trading, Stage C or multi-hour soak was run.
- Browser evidence is from the installed Edge/Chromium on this Windows host,
  not a cross-browser matrix.
- The 3 skips above are intentional environment/optional-extra skips.
- No commit or push was made; the existing dirty worktree was preserved.
