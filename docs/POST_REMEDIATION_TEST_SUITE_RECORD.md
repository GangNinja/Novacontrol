# Post-Remediation NovaMarket Test-Suite Record

**Recorded:** 23 November 2026  
**Scope:** `tests/test_novamarket_*.py` after the security and operational remediation.  
**Entry point:** `bash tmp/ops_audit/run_current_novamarket_suite.sh`  
**Python:** `../.venv/Scripts/python.exe`  
**Flags:** `-q -p no:cacheprovider`  
**Ordering:** Group A = first 21 files through `test_novamarket_lstm.py`; Group B = remaining 35 files; then all 56 files in one process.

The previous record is preserved at `tmp/ops_audit/pre_remediation/current_novamarket_suite_record.json` and has not been overwritten.

## Results

| Phase | Files | Result | Exit | Time |
|---|---:|---|---:|---:|
| Group A | 21 | 697 passed, 607 warnings, 390 subtests passed | 0 | 319.61 s |
| Group B | 35 | 1,273 passed, 2 skipped, 283 warnings, 57 subtests passed | 0 | 189.63 s |
| Single process | 56 | 1,970 passed, 2 skipped, 883 warnings, 447 subtests passed | 0 | 505.88 s |

All 1,972 collected tests are covered: 1,970 passed, 2 skipped, 0 failed. The two skips are pre-existing and reported, not hidden; neither is a security test and neither was added by this remediation.

## Evidence hashes

| Artifact | SHA-256 |
|---|---|
| `tmp/ops_audit/pytest_current_group_a.log` | `2429b0b5bca2857013575aca3368453c914287ab8861090ba3f81bcbcf52b387` |
| `tmp/ops_audit/pytest_current_group_b.log` | `1fc6ac055be85e909368436c87079adcfa1c652389b47cb2657fdd87b0b2e9bd` |
| `tmp/ops_audit/pytest_current_full_single_process.log` | `c1bcdfa37792580963163bce503253ee8f4d7186b15848edf00367264c95e295` |
| `tmp/ops_audit/post_remediation_security_db_final.log` | `ce1bf1f1ff927ff0ddf3508eeed0805192fcdf878426852bb67b2890c7e4df02` |
| `tmp/ops_audit/post_remediation_resource_samples.csv` | `64a40e7bfccece0c4d6c08552fdbce09f902406b3a2f5e16fc431832bffbb1f8` |
| `tmp/ops_audit/post_remediation_runner_console.log` | `0f64c15ec38b228265c7091c86d4363f61f165be4bcfc1ce9aa66ea45c0f65ab` |
| `tmp/ops_audit/soak_evidence.json` | `40d86468009464ec238e2221ae61b6dbfabb03e5fee723eb46608312821f429d` |

The authoritative machine record is `tmp/ops_audit/post_remediation_novamarket_suite_record.json`.

## Resource evidence

- 68 active pytest-child samples, 2026-11-23T02:57:33Z through 03:14:39Z.
- Working set: min 162.5 MB, max 819.3 MB, final 798.9 MB.
- Max sampled CPU time: 424.1 s.
- No out-of-memory event and no abrupt process exit; the historical crash was not reproduced.

## Focused security/database result

`tests/test_security_remediation.py` plus `tests/test_novamarket_database_resilience.py`:

**27 passed, 26 warnings in 6.80 s, exit 0.**

- 22 security tests: policy/auth, SSE session and cookie, middleware fail-closed, 5xx masking, audit failure visibility.
- 5 SQLite drills: lock contention, read-only, disk-full, uncommitted WAL, backup restore.

## Soak result

300.3 s, 58 cycles, 406 requests, **406 × HTTP 200**, 0 failures.

- Latency: median 11.4 ms, p95 57.8 ms, max 319.5 ms.
- RSS: 391.9 → 404.4 MB (+12.5 MB, max 404.4 MB).
- Handles: 299 → 306 (+7, max 316).
- SSE: session 200, connected frame, 20 keep-alives, no errors.
- Scheduler: running, 31 ticks, 0 failures.
- Audit: 6 records, 0 write failures, not degraded.
- API log: 932 lines, no CRITICAL/ERROR.

## Verdict

**PASS** for the current ordered and single-process runs. **PARTIAL** for sustained stability: the historical single-process failure was not explained and the soak is five minutes, not multi-hour. This remediation skipped, removed, weakened or deselected no test to make the suite pass; the two pre-existing optional-Node renderer skips are unchanged and remain reported above.
