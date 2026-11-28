# Supervised Market-Open Paper Runbook

**PAPER ONLY — NO LIVE TRADING — NO AUTONOMOUS PROMOTION**

Target session: **Monday, 2026-11-23**, NSE/BSE regular session **09:15–15:30 Asia/Kolkata**. The next exchange holiday is 2026-11-24 (Guru Nanak Jayanti), so 23 November is the next open session in the verified 2026 calendar.

This runbook uses the existing NovaMarket chain:

`real market observations → NM-02 → NM-09 strategy proposal → NM-08 risk → NM-10 paper execution → NM-11 reconciliation`

It does **not** enable live trading, a broker connection, an autonomy runtime, a scheduler, or learning/policy promotion.

---

## 1. What is prepared

The isolated preparation is under `tmp/market_open_paper/`:

| Item | Prepared value |
|---|---|
| Work directory | `tmp/market_open_paper/open_session_2026-11-23/` |
| Portfolio | `pf_41277535de675e34701d` (`short_term`, funded ₹1,00,000 paper capital) |
| Strategy | `strat_37dc278cecadc5ad` (`trend_sma`, fast 3 / slow 9, active) |
| Instruments | `XBOM:ADANIENT`, `XBOM:RELIANCE` |
| Provider | `yahoo` only; unofficial delayed feed, typically about 15 minutes |
| Background ingestion | **OFF** |
| Intraday gateway | **OFF** |
| Notifications | **OFF** for this isolated session |
| Live permission | denied (`market.trade.live`); `real_money_trading=false` |
| Autonomy release gate | `closed` |

The read-only preparation evidence is `tmp/market_open_paper/market_open_readiness.json`. The empty session capture is `tmp/market_open_paper/market_open_capture.json` with `"status": "prepared_not_run"`. **No decision, fill or reconciliation is claimed for 23 November until the session actually runs.**

A stored-data smoke test was run separately on 2026-11-21 and is in `tmp/market_open_paper/market_open_replay_capture.json`. It replayed the latest real stored bars, produced one NM-09 signal, and the multi-timeframe confirmation layer rejected it because the stored current-session 1m/5m series were incomplete. No order was forwarded. It is a **historical replay**, not the market-open session.

Yahoo is not a licensed real-time feed. Its expected delay is about 900 seconds. The capture records the actual provider, observation time, freshness and quality; it never relabels a delayed or closed-market observation as live.

---

## 2. Safety contract

The session harness (`tmp/continuous_paper_validation/run_market_open.py`) enforces:

1. `paper_only=True`, `live_trading=False`, `broker_connected=False`.
2. `permissions.real_money_trading=False` and `market.trade.live` denied.
3. The AT-01 autonomy release gate must be `closed`.
4. NM-10 must report `paper_only=True`.
5. Background ingestion and the gateway stay disabled; every collection is an explicit operator loop call.
6. The default run mode **captures decisions and creates no orders**.
7. `--execute-paper` is the only path that forwards a proposal, and it calls only the existing NM-09 forward endpoint into NM-10 paper execution. It never calls a broker or live route.
8. No learning, promotion, strategy-version approval or autonomy start is invoked.
9. The process exits at the session close; nothing remains scheduled.

If any safety flag fails, the harness writes `"status": "aborted_safety_gate"` and stops before collecting or forwarding.

---

## 3. Before the session

### Sunday 2026-11-22

- [ ] Confirm the prepared context exists:
  ```bash
  cd novamarket
  cat tmp/market_open_paper/session_context.json
  ```
- [ ] Confirm `market_open_readiness.json` has `"safe": true` and Yahoo's probe `ok: true`.
- [ ] Confirm `data/novamarket.json` was **not** modified by this preparation. The harness edits only `tmp/market_open_paper/open_session_2026-11-23/novamarket.json`.
- [ ] Confirm no broker credentials or live endpoint are configured in the prepared work.
- [ ] Decide the mode:
  - **Capture-only (default):** decisions and reconciliation, no orders. Use this for observation.
  - **Supervised paper execution:** `--execute-paper`; the operator must remain at the terminal. This can fill paper orders but never live ones.

### Monday 2026-11-23, 09:00–09:10 IST

- [ ] Start the session from the repository root.
- [ ] Use a foreground terminal; do not use Task Scheduler, `nohup`, a service, or an autonomy mode.
- [ ] Keep the preparation directory unchanged. Do **not** run `--prepare --reset` on the session day.

Capture-only command:

```bash
cd novamarket
PYTHONPATH=src ../.venv/Scripts/python.exe \
  tmp/continuous_paper_validation/run_market_open.py \
  --run --wait-for-open
```

Supervised paper-execution command, only with the operator present:

```bash
cd novamarket
PYTHONPATH=src ../.venv/Scripts/python.exe \
  tmp/continuous_paper_validation/run_market_open.py \
  --run --wait-for-open --execute-paper
```

Optional bounded first cycle (useful for a pre-flight check):

```bash
PYTHONPATH=src ../.venv/Scripts/python.exe \
  tmp/continuous_paper_validation/run_market_open.py \
  --run --wait-for-open --max-cycles 1
```

The loop collects `1m` and `5m` bars every 60 seconds. It refuses to run outside the prepared session date, before the open, or after the close.

---

## 4. During the session

The harness performs this sequence each minute:

1. `POST /market/data/ingest` for the two instruments with `intervals=["1m","5m"]`, quotes and `force_quotes=true`.
2. Reads the reconciled quote and records provider, observation time, quality, freshness and session date.
3. Runs NM-09 only for instruments whose quote is same-session, usable, and `fresh` or `delayed`. A stale or missing session observation is recorded and skipped; it is never converted into a trade.
4. Records the multi-timeframe decision, signals, proposals and errors.
5. With `--execute-paper`, forwards each new risk-approved proposal through NM-10 and records the HTTP response.
6. Reads orders, fills, holdings, valuation, wallet, ledger, portfolio reconciliation and wallet reconciliation.
7. Appends the complete cycle to `market_open_events.jsonl` and rewrites `market_open_capture.json` so a crash does not lose earlier cycles.

Expected timeline:

| Time (IST) | Action |
|---|---|
| 09:05 | Operator starts the harness with `--wait-for-open` |
| 09:15 | Session opens; loop begins |
| 09:15–09:30 | Yahoo delayed observations may still be the prior close; capture records them as delayed/closed and skips decisions until a same-session quote exists |
| 09:30–15:20 | Real delayed intraday decisions, optional paper fills, reconciliation |
| 15:20 | Operator may stop the process with `Ctrl+C`; no new entries are required |
| 15:25 | Final reconciliation snapshot should already be written |
| 15:30 | Session close; harness refuses new cycles |

There is no guaranteed fill. A session with no actionable signal is a valid result and must be reported as **no trade**, not backfilled with a synthetic order.

---

## 5. Paper-only controls during the run

- The strategy’s proposal is not an order. NM-08 must approve it; NM-10 must accept it.
- Do not edit a proposal, risk decision or fill directly.
- Do not lower risk limits, widen the notional cap, or bypass NM-08 to force a fill.
- Do not use `/market/learning/approve`, `/market/autonomy/start`, or any promotion route.
- Do not connect a broker, add live credentials, or call a live execution endpoint.
- If an order is rejected, record the rejection and continue. A rejection is evidence, not a failure to hide.
- If reconciliation returns `ok=false`, the harness sets an abort warning and stops forwarding. Preserve the files and investigate; do not “correct” the ledger by hand.

---

## 6. Reconciliation and stop conditions

The final capture contains:

- `final.orders`, `final.fills`, `final.holdings`, `final.valuation`
- `final.wallet` and `final.ledger`
- `final.reconcile` (portfolio funding, released capital, realised book/history, discrepancies)
- `final.wallet_reconcile` (stored balance vs ledger balance, entries/transactions checked, integrity status)
- `integrity` (`PRAGMA integrity_check` and foreign-key check across every copied SQLite store)

Stop immediately if any of these is true:

- safety flags are not paper-only;
- portfolio or wallet reconciliation is not `ok`;
- a fill exists without a corresponding approved proposal/order;
- an order exists for the wrong portfolio or instrument;
- the database integrity check fails;
- the provider returns a conflicting quote that cannot be reconciled.

Record the stop reason in the final message and preserve `market_open_capture.json`, `market_open_events.jsonl` and the terminal log. Do not delete an unfavorable fill.

---

## 7. Post-session review

1. Read `market_open_capture.json` and confirm `"status"` is `completed` or an explicit `aborted` reason.
2. Compare `before.counts` with `final.counts`; every new order/fill must trace to a proposal and risk decision.
3. Confirm `final.reconcile.ok` and `final.wallet_reconcile.ok`.
4. Confirm `integrity.all_ok`.
5. Summarise:
   - number of cycles with a same-session quote;
   - signals, proposals, forwards, fills and rejections;
   - realised/unrealised paper P&L, if any;
   - provider freshness and gaps;
   - whether the session was capture-only or supervised paper execution.
6. Label the result **PAPER ONLY**. Do not call a paper fill a live trade or a profitability result.
7. Keep the run directory and logs. Do not reuse the session work for a new date without a fresh `--prepare`.

---

## 8. Failure handling

| Failure | Action |
|---|---|
| Yahoo probe fails before open | Do not start. Record the failure; resolve the provider/network issue or run capture-only with no decisions |
| No same-session quote | The harness records a skip. Do not substitute a stored close as a live signal |
| Provider 429 / timeout | The cycle records the error and continues; check the next cycle. Do not hammer the provider |
| SQLite write lock or HTTP 500 during forward | No fill is expected. Preserve the response; retry only through the normal proposal path after the store is writable |
| Reconciliation `ok=false` | Abort forwarding, preserve evidence, investigate |
| Process crash | The last complete cycle is already in `market_open_capture.json` and `market_open_events.jsonl`. Do not fabricate missing cycles; restart requires an explicit operator decision and a fresh capture file |
| No signal all session | Report **no trade**; this is not a failure of the safety harness |

---

## 9. Evidence map

| Path | Meaning |
|---|---|
| `tmp/market_open_paper/market_open_evidence.json` | Prepared-session evidence bundle: safety, provider, readiness, capture status and replay boundary |
| `tmp/market_open_paper/market_open_readiness.json` | Prepared configuration, schedule, safety flags, provider probe, real quotes and stored bar coverage |
| `tmp/continuous_paper_validation/market_open_prepare.log` | Preparation command output |
| `tmp/market_open_paper/market_open_capture.json` | The future session capture; currently `prepared_not_run`. Session fills are scoped to this portfolio's orders. |
| `tmp/market_open_paper/market_open_events.jsonl` | Append-only per-cycle event stream (created when the session runs) |
| `tmp/market_open_paper/market_open_session.json` | Final run summary (created when the session runs) |
| `tmp/market_open_paper/market_open_replay_capture.json` | Historical stored-data smoke test; one real signal, rejected by MTF, no order |
| `tmp/continuous_paper_validation/market_open_replay.log` | Replay command output |
| `tmp/continuous_paper_validation/run_market_open.py` | The supervised harness itself |

The prepared snapshot is **not** a live-market capture. The only way to produce a real 23 November decision/fill/reconciliation capture is to run the harness during that session.

---

## 10. Explicit non-goals

- No live order, broker adapter, live credential or real-money route.
- No autonomous trading, scheduler, daemon or unattended promotion.
- No learning approval, strategy promotion, model promotion or release-gate change.
- No claim that Yahoo is a licensed real-time feed.
- No claim that one paper session demonstrates profitability or statistical skill.
- No commit, push, deployment or production configuration change as part of this runbook.
