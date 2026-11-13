# NovaMarket OrderBlock and TradingWatch

The standalone NovaMarket web app now has two trading-desk routes beside the
existing Overview dashboard:

| Route | Surface | Purpose |
| --- | --- | --- |
| `#/dashboard` | Overview | Existing dashboard, unchanged. |
| `#/market` | Market Overview | Existing market browser, unchanged. |
| `#/orderblock` | OrderBlock | Directional structure, order blocks, liquidity, X-Ray and sector flow. |
| `#/tradingwatch` | TradingWatch | Momentum, zones, heatmap, market flow, replay and watchlists. |

Both routes are hash routes, so direct URL entry and browser refresh do not need
a server-side rewrite. The active instrument is stored under
`novamarket.deskInstrument` as the canonical `MIC:SYMBOL` id, which lets a user
move from OrderBlock to TradingWatch without losing the selection.

## Backend contracts reused

No second market-data service was introduced. The pages consume the existing
NM-14 workbench facade:

- `GET /market/orderblock?interval=1d&limit=50`
- `GET /market/orderblock/{instrument_id}?interval=1d`
- `GET /market/tradingwatch?interval=1d&limit=50`
- `GET /market/tradingwatch/watchlists`
- `GET /market/tradingwatch/replay/{instrument_id}?interval=1d|1wk&limit=180&end_date=YYYY-MM-DD`

The workbench reads the NM-02 instrument registry and validated OHLCV store.
Charts, levels, blocks, momentum and replay points are calculated by
`novacontrol.novamarket.workbench`; the browser only renders backend values.
Watchlist reads and writes go through the existing NM-05 endpoints through the
shared `WatchlistButton` and `WatchlistPanel` components.

## OrderBlock

- Bullish and bearish ranked boards with filters and client-side sorting.
- Directional strength, relative volume, VWAP relation, block state, entry zone
  and freshness for each row.
- Instrument detail with an interactive SVG candlestick chart, detected zones,
  VWAP, volume, support/resistance, invalidation, targets, weekly context,
  signal evidence and an order-block ledger.
- Liquidity board with the level kind, distance, side, status and calculation
  method.
- Market X-Ray with advance/decline breadth and index context when available.
- Sector flow with average change, momentum, relative volume and directional
  participation.
- An explicit F&O scanner state. No contract master or derivatives provider is
  registered, so the surface reports `BLOCKED` and lists the missing inputs.

## TradingWatch

- Momentum radar with closed-bar momentum, direction, relative volume, RSI,
  block state and freshness.
- Zone board containing support, resistance and block areas.
- Interactive sector/instrument heatmap; selecting a tile or sector carries the
  canonical instrument/sector into the rest of the desk.
- Market flow with breadth, momentum distribution, average relative volume,
  index context and sector participation.
- Index-options status. There is no licensed option-chain source, so strikes,
  OI, change in OI, volume, IV, PCR and expiry are reported as unavailable
  rather than generated.
- Historical replay with instrument, cutoff date and daily or derived weekly
  timeframe selection. The cursor chart is sliced to `bars[0..cursor]`, so a
  later bar cannot influence the displayed indicators. The response carries
  `coverage.look_ahead_guard: true` and the surface labels the data historical.
- Persisted watchlists with create, rename, add, remove and latest-stored-price
  columns, reusable from both desks.

## Methodology and data states

The backend exposes its methodology in each workbench response. In summary:

- A valid block requires a displacement move and structural confirmation; every
  opposite-coloured candle is not treated as an order block.
- Confirmed historical signals use closed bars and are not repainted. Developing
  setups are labelled separately and are not actionable.
- Order-block analysis is derived from price and volume structure. It is not a
  claim of access to institutional order flow.
- Weekly replay is an explicit aggregation of the stored daily series (open
  first, high max, low min, close last, volume sum); it is not a separate feed.
- The snapshot reports `data_state: "stored_ohlcv"`, `live: false` and a
  `price_basis` of the last closed bar. Freshness is shown per instrument.
- Coverage is the complete set of instruments with validated bars in the stored
  universe (126 at the 7 November 2026 acceptance), not the full NSE/BSE
  catalogue (4,785). `sample_only=false` means the 250-instrument scan cap did
  not truncate that set; it does not mean every catalogue row has bars.
- Derivative and option-chain values are never synthesized.

## Verification

```bash
cd novamarket/web && npm run build
cd .. && PYTHONPATH=src ../.venv/Scripts/python.exe -m pytest tests/test_novamarket_workbench_ui.py tests/test_novamarket_api.py tests/test_api_reference_sync.py -q
PYTHONPATH=src ../.venv/Scripts/python.exe -m mypy src/novacontrol/novamarket/workbench.py
```

The browser checks cover: opening `#/orderblock` and `#/tradingwatch` directly,
reloading each, moving the selected instrument between desks, board filters,
chart rendering, replay cursor behavior and the watchlist panel. No analysis
button dispatches an order, wallet transfer or paper-trade execution.

## Known limitations

- The F&O scanner remains `BLOCKED` until a derivatives contract master, stored
  option-chain fields and a licensed derivatives feed exist.
- Index options remain `BLOCKED` until a licensed index-option chain source is
  configured.
- 4,635 catalogue instruments have no stored daily bars, and 24 XBOM
  cross-listings have only one or two bars. They are excluded with a reason and
  are not assigned synthetic values.
- Sector metadata is absent from the current instrument registry, so heatmap
  and sector-flow groups can be `Unclassified`.
- Stored bars can be stale; the surface reports freshness and never substitutes
  a live price.
- The Python frontend contract tests are source-level checks. Browser behavior
  is verified separately; there is no JavaScript unit-test framework in this
  project.
