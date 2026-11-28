# Stage C performance and responsiveness report

**Date:** 27 November 2026
**Status:** PARTIAL — bounded responsiveness observed; stress and soak remain unverified

## Measurements

| Measure | NSE | BSE |
|---|---:|---:|
| Provider availability probe | 285.55 ms | 858.98 ms |
| Quote refresh | 8.220 s | 7.206 s |
| Supported ingestion | 5.449 s | 10.547 s |
| TradingWatch radar request | 103.92 ms | 66.50 ms |
| Monitoring cycle | 30.37 ms | 25.25 ms |
| Forecast-horizon lookup | 21.34 ms | 2.69 ms |
| Decision pipeline | 40.07 ms | 26.62 ms |
| Seven API responsiveness samples | median 7.78 ms; p95 29.50 ms | median 19.58 ms; p95 29.25 ms |
| Event-loop lag sampling | Not captured | 410 samples at 50 ms; median 12.11 ms, p95 13.694 ms, max 485.799 ms |
| Process CPU and working set | Not captured | Average CPU 21.7%; working set 407,261,184 bytes after ingestion and 433,295,360 bytes at final sample |

The forecast figure is a horizon lookup timing, not proof that a forecast was generated. BSE's decision detail reported insufficient history. Quote and ingestion times include external Yahoo request latency.

## Radar starvation recheck

The radar route completed in 66.50–103.92 ms during the two bounded runs. Seven follow-up API samples after ingestion had p95 below 30 ms, and the page loaded without browser page errors. No multi-second outage was observed in these samples.

The BSE event-loop sampler recorded a maximum lag of 485.799 ms. The older integration report measured a 270 ms maximum after its radar fix under a different workload. These measurements are not directly comparable; this run did not correlate individual lag samples with the radar request. Therefore this is not a regression finding, but it is also not enough to claim the starvation risk is fully cleared under load. A repeated, request-correlated measurement remains open.

The sampled app/API process also hosted the acceptance harness; the browser ran separately. CPU and memory values are process-level observations, not an isolated backend-only benchmark.

## Targets and limits

The checked performance documentation records the earlier event-loop defect and a different integration workload, but no matching hard Stage C latency budget was found for these provider, radar, monitoring or decision calls. No threshold was invented for this report. The measured sample does not include concurrent load or a long-duration soak.

## Result

Ingestion did not make the sampled API or UI unresponsive: both bounded user workflows completed, API samples remained responsive, and no page errors were recorded. Event-loop maximum lag, absent concurrency and absent soak keep the performance conclusion PARTIAL rather than PASS.

## Evidence

- [NSE performance measurements](../tmp/stage_c_v2_2026_11_27/xnse/evidence/stage_c_acceptance_20261127T044728805194Z_xnse.json)
- [BSE performance measurements](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_acceptance_20261127T045900735591Z_xbom.json)
- [BSE read-only runtime snapshot](../tmp/stage_c_v2_2026_11_27/xbom/evidence/stage_c_readonly_crosscheck.json)
- Historical context: [Operational reliability report](OPERATIONAL_RELIABILITY_SECURITY_AND_RESILIENCE_REPORT.md), [decision-path remediation report](AUTONOMOUS_DECISION_PATH_REMEDIATION_REPORT.md), and [integration performance audit](INTEGRATION_PERFORMANCE_AUDIT.md).
