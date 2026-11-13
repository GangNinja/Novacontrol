import { useMemo, useState } from "react";
import {
  AlertTriangle,
  ArrowRight,
  BellRing,
  BrainCircuit,
  BriefcaseBusiness,
  CalendarClock,
  ChartNoAxesCombined,
  CircleDollarSign,
  Clock3,
  Gauge,
  Landmark,
  Newspaper,
  RefreshCw,
  ShieldAlert,
  TrendingUp,
  WalletCards,
} from "lucide-react";
import { useApi, useDocumentTitle, navigate } from "../hooks";
import {
  arrayFrom,
  dateTime,
  first,
  horizonSlug,
  money,
  num,
  pct,
  pctUnsigned,
  quoteChange,
  quoteTime,
  quoteValue,
  type Row,
  shortDate,
  timeAgo,
  title,
  valuationPoints,
} from "../lib";
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  Delta,
  EmptyState,
  ErrorBlock,
  LoadingBlock,
  Metric,
  Money,
  PageHeader,
  StatusBadge,
} from "../components/ui";
import { AllocationBars, DonutChart, LineChart } from "../components/charts";

const HORIZONS = [
  { id: "short", label: "Day trading", color: "var(--accent)" },
  { id: "medium", label: "Swing trading", color: "var(--blue)" },
  { id: "long", label: "Long-term", color: "var(--gold)" },
];

function QuoteCell({ instrument }: { instrument: any }) {
  const id = instrument.instrument_id || instrument.id;
  const quote = useApi<any>(id ? `/market/data/quote/${encodeURIComponent(id)}` : null);
  // The screener already carries the reconciled row price. Use it when the
  // single-instrument quote endpoint cannot resolve one listing, otherwise a
  // quoted market-pulse row would show "Unavailable" beside a priced board row.
  const price = quoteValue(quote.data) ?? instrument.price ?? null;
  const change = quoteChange(quote.data) ?? instrument.change_pct ?? null;
  const when = quoteTime(quote.data) ?? instrument.observed_at ?? instrument.updated_at ?? null;
  return (
    <button type="button" className="ticker-row" onClick={() => navigate("market")}>
      <div className="ticker-identity">
        <span className="ticker-symbol">{instrument.symbol || id || "—"}</span>
        <span className="ticker-name">{instrument.name || instrument.asset_class || "Instrument"}</span>
      </div>
      <div className="ticker-price">
        {quote.loading ? <span className="skeleton skeleton-line" /> : price === null ? <span className="muted">{quote.error ? "No quote" : "Unavailable"}</span> : <Money value={price} />}
        {change !== null ? <span className={`delta ${change >= 0 ? "positive" : "negative"}`}>{pct(change)}</span> : when ? <span className="tiny-muted">{timeAgo(when)}</span> : null}
      </div>
      <ArrowRight size={14} className="ticker-arrow" />
    </button>
  );
}

function horizonStatus(statuses: unknown): string {
  const values = Array.isArray(statuses) ? statuses.map((value) => String(value || "").trim()).filter(Boolean) : [];
  if (!values.length) return "active";
  return values.every((value) => value === values[0]) ? values[0] : "mixed";
}

function RangeToggle({ value, onChange }: { value: string; onChange: (value: string) => void }) {
  return (
    <div className="range-toggle">
      {["1W", "1M", "3M", "ALL"].map((range) => (
        <button type="button" key={range} className={value === range ? "active" : undefined} onClick={() => onChange(range)}>
          {range}
        </button>
      ))}
    </div>
  );
}

export function DashboardPage() {
  useDocumentTitle("Dashboard");
  const overview = useApi<any>("/market/overview", { refreshMs: 60_000 });
  const events = useApi<any>("/market/events?limit=5");
  // Market pulse must show stored observations that actually have a quote.
  // The alphabetical instrument page can begin with hundreds of unquoted names.
  const pulse = useApi<any>("/market/screener?limit=12&available_only=true");
  const watchlists = useApi<any>("/market/watchlists");
  const riskAlerts = useApi<any>("/market/risk/alerts?status=open&limit=5");
  const learningAlerts = useApi<any>("/market/learning/alerts?status=open&limit=5");
  const [range, setRange] = useState("1M");

  const data = overview.data;
  const summary = data?.summary || {};
  const portfolio = data?.portfolios || {};
  const rows = Array.isArray(portfolio.rows) ? portfolio.rows : [];
  const firstPortfolio = rows[0];
  const valuations = useApi<any>(firstPortfolio ? `/market/portfolios/${encodeURIComponent(firstPortfolio.portfolio_id)}/valuations?limit=200` : null);
  const portfolioEvents = arrayFrom(events.data, "events");
  const instrumentRows = useMemo(() => {
    const seen = new Set<string>();
    return arrayFrom(pulse.data, "rows").filter((row: any) => {
      const key = String(row.symbol || row.instrument_id || "").trim();
      if (!key || seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }, [pulse.data]);
  const watchlistRows = arrayFrom(watchlists.data, "watchlists");
  const combinedAlerts: Row[] = [
    ...arrayFrom(riskAlerts.data, "alerts").map((row) => ({ ...row, _source: "Risk" })),
    ...arrayFrom(learningAlerts.data, "alerts").map((row) => ({ ...row, _source: "Learning" })),
  ].slice(0, 5);
  const activity = Array.isArray(data?.activity) ? data.activity : [];
  const horizonMap = new Map<string, any>((portfolio.by_horizon || []).map((item: any) => [horizonSlug(item.horizon), item]));
  const hasPortfolios = rows.length > 0;

  const series = useMemo(() => {
    const points = valuationPoints(valuations.data).map((point) => ({
      label: shortDate(point.label),
      value: point.value,
      raw: point.label,
    }));
    if (range === "ALL" || !points.length) return points;
    const days = range === "1W" ? 7 : range === "1M" ? 31 : 92;
    const cutoff = Date.now() - days * 86_400_000;
    const filtered = points.filter((point) => new Date(point.raw).getTime() >= cutoff);
    return filtered.length > 1 ? filtered : points;
  }, [valuations.data, range]);

  const allocation = HORIZONS.map((horizon) => ({
    label: horizon.label,
    value: num(horizonMap.get(horizon.id)?.total_value_inr),
    color: horizon.color,
  })).filter((segment) => segment.value > 0);

  if (overview.loading && !data) {
    return (
      <div className="page">
        <PageHeader eyebrow="Overview" title="Loading your financial desk" description="Reading the live NovaMarket service and its measured records." />
        <LoadingBlock rows={5} label="Opening the NovaMarket workspace…" />
      </div>
    );
  }

  if (overview.error && !data) {
    const status = overview.error.status;
    const rateLimited = status === 429;
    const authRequired = status === 401;
    return (
      <div className="page">
        <PageHeader
          eyebrow="Overview"
          title={rateLimited ? "Dashboard is rate limited" : authRequired ? "API token required" : "Dashboard unavailable"}
          description={
            rateLimited
              ? "The API is answering, but this tab has made too many requests. Wait a moment and retry; the service is not offline."
              : authRequired
                ? "Add the API bearer token in Settings before reading the financial desk."
                : "NovaMarket needs a live backend connection before it can show financial state."
          }
        />
        <ErrorBlock message={overview.error.detail} onRetry={overview.refresh} />
        <Banner tone="info" title="No values are being invented">
          {rateLimited
            ? "The dashboard deliberately shows nothing while the request limit is active rather than inventing a balance, return or holding. Retry after the short cooldown."
            : authRequired
              ? "The dashboard never substitutes an unauthenticated or placeholder balance for the authoritative record."
              : "The dashboard deliberately shows nothing rather than a placeholder balance, return or holding. Start the API, then retry."}
        </Banner>
      </div>
    );
  }

  const returnValue = summary.portfolio_return_pct;
  const walletBalance = summary.wallet_balance_inr;

  return (
    <div className="page dashboard-page">
      <PageHeader
        eyebrow="Intelligence overview"
        title={<>The whole book, <em>in one view.</em></>}
        description="Measured portfolio state, live market observations and the decisions that sit behind them. Paper and hypothetical books are always labelled."
        meta={
          <>
            <span className="meta-item"><Clock3 size={13} /> {dateTime(data?.generated_at)}</span>
            <span className="meta-item"><i className={`health-dot health-${String(data?.status?.health?.state || "unknown").toLowerCase()}`} /> {title(data?.status?.health?.state || "unknown")}</span>
          </>
        }
        actions={
          <>
            <Button variant="ghost" icon={<RefreshCw size={14} className={overview.refreshing ? "spin" : undefined} />} onClick={overview.refresh}>Refresh</Button>
            <Button variant="primary" icon={<BriefcaseBusiness size={15} />} onClick={() => navigate("portfolios/short")}>Open portfolios</Button>
          </>
        }
      />

      <div className="hero-grid">
        <section className="portfolio-hero">
          <div className="hero-orbit hero-orbit-one" />
          <div className="hero-orbit hero-orbit-two" />
          <div className="hero-topline">
            <span className="eyebrow">Total portfolio value</span>
            <Badge tone="info" dot>Authoritative book</Badge>
          </div>
          <div className="hero-value">
            {hasPortfolios ? <Money value={summary.portfolio_value_inr} /> : <span className="not-funded">Not funded</span>}
            <small>INR</small>
          </div>
          <div className="hero-return">
            <Delta value={returnValue} />
            <span>weighted return across {portfolio.count || 0} book{portfolio.count === 1 ? "" : "s"}</span>
          </div>
          <div className="hero-metrics">
            <div><span>Invested</span><strong>{hasPortfolios ? money(summary.portfolio_invested_inr) : "—"}</strong></div>
            <div><span>Available</span><strong>{hasPortfolios ? money(summary.portfolio_cash_inr) : "—"}</strong></div>
            <div><span>Exposure</span><strong>{hasPortfolios ? pctUnsigned(summary.portfolio_exposure_pct) : "—"}</strong></div>
          </div>
          <div className="hero-foot">
            <span><Landmark size={13} /> Paper capital, 1 NC = ₹1</span>
            <span><ShieldAlert size={13} /> Real-money execution disabled</span>
          </div>
        </section>

        <Card className="chart-card hero-chart-card">
          <CardHeader
            title="Portfolio trajectory"
            subtitle={firstPortfolio ? `${firstPortfolio.name || firstPortfolio.portfolio_id} · stored snapshots` : "No portfolio has been funded"}
            action={<RangeToggle value={range} onChange={setRange} />}
          />
          <LineChart data={series} height={246} empty="No valuation snapshots" />
          {!hasPortfolios && (
            <button type="button" className="inline-link" onClick={() => navigate("portfolios/short")}>Fund a portfolio to begin measuring performance <ArrowRight size={13} /></button>
          )}
        </Card>

        <Card className="wallet-glance">
          <CardHeader title="NovaCoin wallet" subtitle="Available balance" action={<WalletCards size={17} />} />
          <div className="wallet-value">
            {walletBalance !== undefined && walletBalance !== null ? <Money value={walletBalance} /> : <span className="not-funded">No wallet</span>}
            <small>NC</small>
          </div>
          <div className="wallet-meta">
            <div><span>Ledger entries</span><strong>{summary.ledger_entries ?? "—"}</strong></div>
            <div><span>Wallets</span><strong>{summary.wallets ?? "—"}</strong></div>
          </div>
          <div className="wallet-note">Wallet balances and portfolio values are separate books. They are never added together.</div>
          <Button variant="secondary" size="sm" onClick={() => navigate("wallet")}>Open wallet <ArrowRight size={13} /></Button>
        </Card>
      </div>

      <div className="metric-grid">
        <Metric label="Overall P&L" value={hasPortfolios ? money(summary.portfolio_pnl_inr) : "—"} help="Realized + unrealized" icon={<TrendingUp size={17} />} tone={num(summary.portfolio_pnl_inr) < 0 ? "bad" : "good"} />
        <Metric label="Paper orders" value={summary.paper_orders ?? 0} help={`${summary.paper_fills ?? 0} fills recorded`} icon={<Gauge size={17} />} tone="accent" />
        <Metric label="Risk decisions" value={summary.risk_decisions ?? 0} help="NM-08 authoritative" icon={<ShieldAlert size={17} />} />
        <Metric label="Shadow sessions" value={summary.shadow_sessions ?? 0} help={`${summary.backtests ?? 0} backtests`} icon={<ChartNoAxesCombined size={17} />} tone="warn" />
      </div>

      <div className="dashboard-grid dashboard-grid-main">
        <div className="stack">
          <section>
            <div className="section-title-row">
              <div>
                <div className="eyebrow">Allocations</div>
                <h2>Three horizons, three books</h2>
              </div>
              <Button variant="ghost" size="sm" onClick={() => navigate("portfolios/short")}>Manage books <ArrowRight size={13} /></Button>
            </div>
            <div className="horizon-grid">
              {HORIZONS.map((horizon) => {
                const item: any = horizonMap.get(horizon.id);
                return (
                  <button type="button" className={`horizon-card ${item ? "" : "horizon-card-empty"}`} key={horizon.id} onClick={() => navigate(`portfolios/${horizon.id}`)}>
                    <div className="horizon-top">
                      <span className="horizon-icon" style={{ color: horizon.color }}><span style={{ background: horizon.color }} /></span>
                      <span>{horizon.label}</span>
                      <ArrowRight size={14} />
                    </div>
                    {item ? (
                      <>
                        <div className="horizon-value">{money(item.total_value_inr)}</div>
                        <div className="horizon-row"><span>{item.count} book{item.count === 1 ? "" : "s"}</span><Delta value={item.return_pct} /></div>
                        <div className="horizon-foot"><span>Cash {money(item.cash_inr)}</span><StatusBadge value={horizonStatus(item.statuses)} /></div>
                      </>
                    ) : (
                      <>
                        <div className="horizon-value muted">Not funded</div>
                        <p>No {horizon.label.toLowerCase()} book has been opened.</p>
                        <span className="inline-link">Open portfolio <ArrowRight size={12} /></span>
                      </>
                    )}
                  </button>
                );
              })}
            </div>
          </section>

          <Card>
            <CardHeader title="Market pulse" subtitle="Latest reconciled observations" action={<button type="button" className="text-link" onClick={() => navigate("market")}>Open market <ArrowRight size={13} /></button>} />
            {pulse.loading ? <LoadingBlock rows={2} label="Reading quoted instruments…" /> : instrumentRows.length ? (
              <div className="ticker-list">
                {instrumentRows.slice(0, 5).map((instrument) => <QuoteCell key={instrument.instrument_id || instrument.symbol} instrument={instrument} />)}
              </div>
            ) : <EmptyState title="No instruments registered" description="Market data will appear after an instrument catalogue is populated." />}
          </Card>

          <Card>
            <CardHeader title="Recent intelligence" subtitle="Verified events from the radar" action={<button type="button" className="text-link" onClick={() => navigate("events")}>All events <ArrowRight size={13} /></button>} />
            {events.loading ? <LoadingBlock rows={2} label="Reading events…" /> : portfolioEvents.length ? (
              <div className="event-stack">
                {portfolioEvents.slice(0, 4).map((event, index) => (
                  <button type="button" className="event-row" key={event.event_id || index} onClick={() => navigate("events")}>
                    <span className="event-marker" />
                    <span className="event-copy">
                      <strong>{event.title || event.headline || event.summary || "Market event"}</strong>
                      <small>{event.category ? title(event.category) : "Event"} · {timeAgo(event.detected_at || event.created_at || event.published_at)}</small>
                    </span>
                    <Badge tone={String(event.verification_state || event.status || "").toLowerCase().includes("verif") ? "good" : "neutral"}>{title(event.verification_state || event.status || "recorded")}</Badge>
                  </button>
                ))}
              </div>
            ) : <EmptyState icon={<Newspaper size={20} />} title="No verified events yet" description="The radar writes events here when evidence-backed signals are detected." />}
          </Card>
        </div>

        <div className="stack">
          <Card>
            <CardHeader title="Allocation mix" subtitle="By measured portfolio value" action={<ChartNoAxesCombined size={17} />} />
            {hasPortfolios ? (
              <div className="allocation-panel">
                <DonutChart segments={allocation} totalLabel="Portfolio" totalValue={money(summary.portfolio_value_inr)} />
                <AllocationBars segments={allocation} />
              </div>
            ) : <EmptyState title="No allocation to show" description="Allocation appears only from funded portfolio valuations." />}
          </Card>

          <Card>
            <CardHeader title="Attention needed" subtitle="Open risk and learning alerts" action={<BellRing size={17} />} />
            {combinedAlerts.length ? (
              <div className="alert-stack">
                {combinedAlerts.map((alert, index) => (
                  <button type="button" className="alert-row" key={alert.alert_id || alert.decision_id || index} onClick={() => navigate(alert._source === "Learning" ? "learning" : "risk")}>
                    <span className={`alert-badge alert-${String(alert.severity || "warning").toLowerCase()}`}><AlertTriangle size={13} /></span>
                    <span className="alert-copy">
                      <strong>{alert.message || alert.explanation || alert.title || "Open alert"}</strong>
                      <small>{alert._source} · {timeAgo(alert.created_at || alert.updated_at)}</small>
                    </span>
                  </button>
                ))}
              </div>
            ) : <EmptyState icon={<ShieldAlert size={20} />} title="No open alerts" description="Risk and learning have no unresolved alerts." />}
          </Card>

          <Card>
            <CardHeader title="Watchlist highlights" subtitle="Tracked opportunities" action={<button type="button" className="text-link" onClick={() => navigate("watchlists")}>Open <ArrowRight size={13} /></button>} />
            {watchlistRows.length ? (
              <div className="watchlist-mini-list">
                {watchlistRows.slice(0, 4).map((list: any) => (
                  <button type="button" className="watchlist-mini" key={list.watchlist_id || list.name} onClick={() => navigate("watchlists")}>
                    <span className="watchlist-mini-icon"><ListChecksIcon /></span>
                    <span><strong>{list.name || "Untitled watchlist"}</strong><small>{title(list.kind || "custom")}</small></span>
                    <span className="watchlist-count">{list.active_count ?? list.member_count ?? "—"}</span>
                  </button>
                ))}
              </div>
            ) : <EmptyState icon={<CircleDollarSign size={20} />} title="No watchlists yet" description="Create one from Asset Discovery to keep candidates in view." />}
          </Card>

          <Card>
            <CardHeader title="Activity" subtitle="Latest committed operations" action={<button type="button" className="text-link" onClick={() => navigate("activity")}>Audit trail <ArrowRight size={13} /></button>} />
            {activity.length ? (
              <div className="activity-mini">
                {activity.slice(0, 5).map((item: any, index: number) => (
                  <div className="activity-mini-row" key={`${item.event_type || item.type || "activity"}-${index}`}>
                    <i />
                    <div><strong>{item.detail || item.message || title(item.event_type || item.type || "Activity")}</strong><small>{timeAgo(item.at || item.timestamp || item.created_at)}</small></div>
                  </div>
                ))}
              </div>
            ) : <EmptyState title="No activity recorded" description="Committed operations are listed here as the subsystem records them." />}
          </Card>

          <div className="mini-cta">
            <BrainCircuit size={18} />
            <div><strong>Learning is governed</strong><span>Every candidate requires evidence, risk review and explicit approval.</span></div>
            <button type="button" className="text-link" onClick={() => navigate("learning")}>Review <ArrowRight size={13} /></button>
          </div>
        </div>
      </div>

      <div className="dashboard-footnote">
        <CalendarClock size={14} /> Live values are read from NovaMarket's authoritative records. A dash means the backend has not measured it yet; it is never replaced with a sample figure.
      </div>
    </div>
  );
}

function ListChecksIcon() {
  return <span className="watchlist-dot" />;
}
