# Stage C runtime, operator controls and recovery report

**Date:** 27 November 2026
**Status:** C5 PARTIAL; C6 PASS
**Stores:** Isolated NSE and BSE environments only

## Safety preflight and final state

Both exchange environments were verified disabled with auto-start disabled before accepted runtime work. The autonomy contract kept paper_only=true, live_trading=false, broker_connected=false, NM-08 approval mandatory before NM-10, duplicate protection active, and automatic policy promotion false.

After each user-level run, the runtime was stopped through its supported API. Read-only store checks showed mode=disabled and desired_mode=disabled. The UI and API final state had no active lease. A controlled app restart followed by recovery did not auto-resume and reported no unresolved records. Ports 8001, 5174 and 8002 had no listener after completion.

## C5 — pause and resume

| Requirement | Status | Observation |
|---|---|---|
| Start permitted paper-only runtime and verify health/lease | PASS | The UI showed autonomous runtime and a held lease during the bounded checkpoint. |
| Pause and persist desired mode | PASS | The pause control changed the mode to paused while retaining autonomous as the intended mode. |
| Resume without explicit mode | PASS | The API restored the saved intended mode when acknowledge_paper_only=true was supplied. |
| Autonomous acknowledgement and risk gates | PASS | Acknowledgement remained explicit; status retained NM-08/NM-10 restrictions and paper-only flags. |
| UI status and operator path | PASS | Pause, resume, and emergency-stop were performed in the actual Chromium page; the saved screenshot shows the active checkpoint. |
| No duplicate runtime or order | PASS | One active lease/cycle at a time remained enforced; no order, intent or fill was created. |
| Audit records for controls | PARTIAL | The isolated audit JSONL had zero occurrences for operator pause, operator resume and emergency stop. Runtime state and desired mode were persisted, but control-specific audit evidence was not found. |

C5 is PARTIAL because the specification explicitly requires audit records. This is an evidence/observability gap; no production change was made in this acceptance run.

## C6 — process restart and lease recovery

The accepted BSE run included a separate process-level probe using a fresh runtime-only store and a 60-second lease:

1. Process A entered autonomous mode and held the lease.
2. Process A was terminated while its lease was still valid.
3. Process B restarted, remained disabled, did not steal the live lease, and entered bounded auto-start retry.
4. After natural lease expiry, Process B recovered to autonomous with its own valid lease.
5. Order, fill and intent counts stayed zero; no duplicate execution was created.
6. Emergency stop disabled auto-start and cleared retry.
7. Process C restarted and remained disabled; emergency stop prevented auto-start.

Every recorded subcheck in the BSE lease probe was PASS. This verifies the specified owner-conflict, expiry-retry and emergency-stop behavior in the isolated process-level scenario. It does not imply that a paper order lifecycle was exercised.

## Evidence

- [NSE accepted run and UI evidence](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_acceptance_20261127T044728805194Z_xnse.json)
- [NSE screenshot](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_acceptance_20261127T044728805194Z_xnse_autonomy.png)
- [BSE accepted run and UI evidence](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_acceptance_20261127T045900735591Z_xbom.json)
- [BSE screenshot](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_acceptance_20261127T045900735591Z_xbom_autonomy.png)
- [BSE process-level lease evidence](../tmp/stage_c_v2_2026_11_27/xbom/lease_probe_runs/20261127T045900735591Z/)
- [NSE read-only final state](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_readonly_crosscheck.json)
- [BSE read-only final state](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_readonly_crosscheck.json)

## Conclusion

C6 passes for the isolated process-level scenario. C5 remains PARTIAL until control events are available as auditable records or the missing audit requirement is otherwise resolved with direct evidence.
