# NovaMarket Standalone Frontend — Final Report

Date: 4 November 2026
Scope: standalone NovaMarket web application in `novamarket/web`, consuming NM-01–NM-14 through the existing HTTP API.
Verdict: **PARTIAL** — superseded by `NM14_FINAL_ACCEPTANCE_REPORT.md`. The 25-route render, executed NM-01–NM-14 chain and production build remain valid; derivatives data and exhaustive control-level interaction are not complete.

---

## 1. Executive summary

NovaMarket now has its own frontend, entry point, routing, navigation, build and deployment boundary. It is not embedded in the NovaControl UI and it does not re-implement any financial engine. Every displayed financial value is read from the backend; absent measurements render as `—`, never as a sample number.

Delivered:

- 23 required routes across Overview, Intelligence, Portfolio Management, Trading Intelligence, Financial Records and System.
- A premium dark fintech shell with responsive navigation, SVG charts, tables, modals, toasts and honest empty/error states.
- Real operations for wallets, portfolios, watchlists, forecasts, strategies, risk, paper trading, backtesting, shadow mode, adaptive learning and lifecycle settings.
- Two additive backend changes only: an authoritative portfolio aggregate in `workspace_overview()` and opt-in exact-origin CORS.
- Bugs found during live verification were fixed, not hidden: a blank-page render crash, fabricated `₹0.00` quotes, an invalid horizon query (422), and a wallet total that mixed portfolio funding into the user balance.

Verified live: 25/25 routes render with zero error boundaries; wallet deposit and portfolio funding work end-to-end; the dashboard reports portfolio value and wallet balance as separate books.

---

## 2. Technology

| Area | Choice |
|---|---|
| UI | React 19.1 + TypeScript 5.8 |
| Build | Vite 6.4.5, `tsc -b` before bundling |
| Icons | `lucide-react` |
| Styling | Hand-written CSS design system (`styles.css`, `pages.css`) |
| Charts | Custom responsive SVG line, donut, allocation and sparkline components |
| HTTP | One typed client (`src/api.ts`) over `fetch` |
| Routing | Hash router (`#/route`) with no server rewrite requirement |
| Runtime used | Node 24.19.0, npm 11.17.0 |

No second chart library, CSS framework or state framework was added.

Production build (verified):

- 1,594 modules transformed.
- JS: 459.88 kB (125.28 kB gzip).
- CSS: 61.41 kB (12.23 kB gzip).
- Build time: ~5–6 s.

---

## 3. Architecture and structure

```
novamarket/web/
  index.html
  package.json          build/dev/preview scripts
  vite.config.ts        /market proxy to NOVAMARKET_API_ORIGIN (default :8011)
  tsconfig*.json
  src/
    main.tsx            React root + toast provider
    App.tsx             shell, sidebar, topbar, auth dialog, page router, error boundary
    api.ts              bearer token, timeout, error mapping, 401 event
    hooks.ts            hash route, API loading/refresh, local storage, debounce
    lib.ts              money/percent/date formatting, quote and horizon helpers
    nav.ts              25-route navigation registry
    components/ui.tsx   buttons, cards, tables, badges, modals, forms, toasts
    components/charts.tsx
    pages/              Dashboard, Market, Intelligence, Portfolios, Trading,
                        ShadowLearning, Records, System
    styles.css, pages.css
```

Backend boundary:

- `workspace_overview()` in `src/novacontrol/novamarket/service.py` gained `_portfolio_summary()`, which only sums valuations already produced by the portfolio engine.
- `src/novacontrol/api/app.py` gained opt-in `CORSMiddleware` controlled by `NOVAMARKET_CORS_ORIGINS`; no wildcard, credentials disabled, exact origins only.
- No new API routes were added; `python scripts/generate_api_reference.py --check` reports `docs/API.md: in sync`.

The frontend can be hosted separately (static `dist/`) against the existing API; for development Vite proxies `/market` to the backend so the browser stays same-origin.

---

## 4. Pages and navigation

All 25 routes were rendered in a live browser sweep.

| Group | Routes |
|---|---|
| Overview | Dashboard, Market Overview |
| Intelligence | Market Events, Asset Discovery, Watchlists, Forecasts |
| Portfolio Management | Short-Term, Medium-Term, Long-Term Portfolios, Portfolio Analytics |
| Trading Intelligence | Strategies, Risk Management, Paper Trading, Backtesting, Shadow Mode, Adaptive Learning |
| Financial Records | NovaCoin Wallet, Transaction History, Ledger |
| System | Notifications, Activity and Audit, System Health, Settings |

Each page has a distinct data surface and operation set; there are no placeholder metric tiles. Empty states explain what the backend has not measured, and "Not funded" is used instead of a zero book.

---

## 5. Design

- Deep navy/charcoal palette with teal accent, restrained gold and rose, subtle grid and glass panels.
- DM Sans for interface text; IBM Plex Mono for identifiers, balances and timestamps.
- Persistent grouped sidebar with active-route treatment, collapse control and an off-canvas mobile drawer.
- Consistent page header (eyebrow, headline, measured metadata, actions), cards, tables with sorting, badges, banners, modals, confirmation dialogs and toasts.
- Financial states are visually distinct: actual portfolio, paper order, hypothetical backtest, shadow session, stale quote.
- Tables are horizontally scrollable on narrow screens; charts resize with the container.
- Modal escape handling, focus placement, labelled controls, `aria-live` toast region, `aria-sort` on sortable tables, and visible focus styles are present.

---

## 6. API integration

- One API client, one base URL (`VITE_API_BASE_URL` or same-origin/proxy).
- Bearer token stored in `sessionStorage` under `novamarket.apiToken`; never bundled, never logged.
- 25-second request timeout, structured `ApiError` messages, 401 dispatches `novamarket:unauthorized` and opens the connection dialog.
- All reads are GET; mutations are POST and surface the backend's own refusal text.
- Cross-origin access is off by default. Production must declare exact origins through `NOVAMARKET_CORS_ORIGINS`; `allow_credentials=False`, methods limited to GET/POST/DELETE/OPTIONS, headers limited to Authorization/Content-Type.
- No financial arithmetic beyond presentation: sums shown in the UI are backend aggregates, P&L and returns come from portfolio valuations, and chart series come from stored observations.

---

## 7. Controls and real operations

Every control is wired to a real endpoint. Representative operations were executed live:

- **Wallet:** created a NovaCoin wallet, deposited ₹50,000; balance and ledger entries updated from the backend.
- **Portfolio:** created and funded a short-term book with ₹20,000 from the wallet; the ledger transfer and portfolio activation were recorded.
- **Dashboard:** reflected the new book and the reduced wallet balance without a restart.

Also wired: wallet withdraw/reconcile; portfolio pause/resume/rebalance/apply/close; watchlist create/delete; forecast create; strategy transitions; risk stress and alert acknowledgement/resolution; paper order submit/process/cancel; backtest create/start/cancel; shadow session create/lifecycle/cycle; learning candidate evaluate/approve/reject/promote and alert acknowledgement; market-data ingest; settings lifecycle enable/disable and token management.

No button is decorative: empty or unavailable actions are disabled with a reason, and every mutation reports success or the backend refusal in a toast.

---

## 8. Financial accuracy

Authoritative sources used:

| Value | Source |
|---|---|
| Portfolio value, invested, cash, P&L, return, exposure | `portfolio_detail` valuations via `workspace_overview().portfolios` |
| Wallet balance | `/market/wallet/status` summary |
| Ledger entries | wallet/ledger endpoints |
| Prices and changes | `/market/data/quote/...` selected observations only |
| Paper orders, backtests, shadow, learning | their own subsystem endpoints |

Accuracy fixes made during verification:

1. **Fabricated zero quote.** `num()` converted an absent value to `0` because `Number("") === 0`, so an unavailable quote rendered as `₹0.00`. Fixed; unavailable quotes now render "Unavailable".
2. **Wallet/portfolio mixing.** `summary.user_balance` summed portfolio funding wallets, reporting ₹50,000 after ₹20,000 had moved into a book. Fixed by excluding `novamarket:%`-owned wallets; the user balance is now ₹30,000.
3. **Horizon mismatch.** The UI sent `short`/`medium`/`long` where the API expects `short_term`/`medium_term`/`long_term`, causing 422s. Fixed with one mapping helper; all portfolio requests now return 200.
4. **Risk page blank screen.** `status.policies` is a list; it was rendered as a React child, crashing the tree. Fixed, and a page-level error boundary now prevents one surface from blanking the shell.

Live result after funding:

- Portfolio value: ₹20,000.00 (1 book, short-term, active).
- Wallet available: ₹30,000.00 (separate book).
- Invested: ₹0.00; exposure: 0.00%; return: 0.00%.
- 1 NC = ₹1 displayed consistently.

---

## 9. Responsive and browser results

Browser: packaged Chromium, live Vite server on `127.0.0.1:5173`, backend on `127.0.0.1:8011`.

| Viewport | Document width | Horizontal overflow | Layout notes |
|---|---|---|---|
| 1440 × 950 | 1431 px | none | full sidebar, four-column metric grid |
| 838 × 1200 | 829 px | none | 200 px sidebar, stacked page header below 760 px |
| 390 × 844 | 387 px | none | off-canvas sidebar (left −258 px), one-column metrics, mobile menu visible |

Results:

- 25/25 routes rendered; zero React error boundaries triggered; every title and `h1` matched its route.
- Console showed only Vite/React development messages and the expected 400 from an unavailable quote probe; there were no unhandled runtime errors.
- Network sweep: all stateful API calls returned 200; unavailable quote probes return 400 and render as “No quote” by design; no 5xx.
- Mobile drawer, modal close, forms and toasts were exercised during the wallet and portfolio flows.

Screenshots (`docs/screenshots/`):

- `dashboard-desktop.png` — dashboard with ₹20,000 portfolio and separate wallet card.
- `dashboard-mobile.png` — 390 px single-column layout with off-canvas navigation.
- `market-tablet.png` — market overview with real provider history and an unavailable live quote shown honestly.

Capture limitation: the preview screenshot tool records a panel-sized frame regardless of the emulated viewport, so some wider captures are cropped at the right edge. This affected the screenshots, not the measured layout (`scrollWidth ≤ innerWidth` at every tested width).

---

## 10. Performance

Measured:

- Production build: ~5–6 s, 1,594 modules, 459.88 kB JS / 125.28 kB gzip, 61.41 kB CSS / 12.23 kB gzip.
- Dashboard performs its reads in parallel and revalidates the overview every 60 s; status/health poll at 30 s.
- Charts are pure SVG and redraw from arrays; no chart framework or animation loop.
- Requests are aborted on unmount and guarded by a version counter, preventing stale responses from overwriting fresh data.

Performance evidence is bounded to the measured build and request behaviour above; no cold-host TTFB, Lighthouse, long-run memory or concurrent-load certification is claimed. These are not functional acceptance criteria for the standalone workspace.

---

## 11. Security

- Authentication: optional backend `NOVACONTROL_API_TOKEN`; the client sends it as a bearer token and stores it only in tab-scoped `sessionStorage`.
- No secret is embedded in the bundle; `.env.example` exposes only an API base URL.
- CORS: disabled by default, exact-origin allowlist only, no wildcard, `allow_credentials=False`.
- All financial decisions and mutations are backend-authoritative; the browser cannot price, approve, fill or settle anything.
- No live broker, bank, real-money route or execution gateway exists; paper/shadow/backtest books are labelled.
- React escaping is used for all free text; no `dangerouslySetInnerHTML`.
- Rate limiting, request logging and security headers remain the backend middleware's responsibility.

A formal penetration test and CSP deployment review were not performed and are not claimed; the boundaries above are enforced by the backend and the client's token handling.

---

## 12. Testing evidence

| Check | Result |
|---|---|
| Frontend TypeScript + production build | PASS (clean) |
| Full NovaMarket backend suite | 735 passed, 2 skipped, 352 subtests (three bounded groups) |
| End-to-end NM-01–NM-14 chain | PASS — exit 0, candidate promoted |
| `mypy src` | PASS — 552 files |
| `mypy src --platform win32` | PASS — 552 files |
| API reference sync | PASS — no new routes |
| Live route sweep | 25/25, zero errors |
| Wallet → deposit → portfolio → dashboard E2E | PASS |
| Aggregate fields + CORS tests | 9 passed |
| Responsive measurements | 390 / 838 / 1440, no overflow |

The suite is green after the strategy-window ordering fix; no failing test was skipped or weakened. The chain, full backend suite, both mypy views, API-reference check, and production build were all executed for this verdict.

---

## 13. Scope boundaries and verdict

Functional limitations within the declared scope: **control-level evidence is incomplete**. The executed chain and the 25-route sweep prove backend mutation paths and route rendering, but they do not prove that every filter, chart toggle, modal and pagination control was operated in the browser. Derivatives data is also unavailable. This report is retained as the frontend baseline and is superseded by `NM14_FINAL_ACCEPTANCE_REPORT.md`.

Declared boundaries:

- Live trading and real-money execution are intentionally absent.
- The workspace remains read-only for financial mutations; the API is the
  mutation surface.
- A production deployment must set the API origin (or serve same-origin) and,
  for authenticated backends, provide the bearer token to the operator.
- Formal WCAG certification, a penetration test and a production load
  certification were not performed and are not claimed; they are not functional
  acceptance criteria for this workspace.

Verdict: **PARTIAL** for the standalone frontend's declared scope.

All 25 routes, navigation, core financial operations and financial boundaries
are implemented, and the production build plus the paced live route sweep passed.
That is not the same as operating every advertised control: filters, chart
toggles, modal flows and pagination were not exhaustively driven in the browser.
Derivatives surfaces are blocked by absent data. The current verdict and full
matrices live in `NM14_FINAL_ACCEPTANCE_REPORT.md`.
