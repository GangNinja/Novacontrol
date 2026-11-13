import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  ArrowRight,
  BarChart3,
  CalendarDays,
  CheckCircle2,
  ChevronRight,
  CircleAlert,
  Filter,
  Gauge,
  Globe2,
  Layers3,
  Newspaper,
  Radio,
  RefreshCw,
  Search,
  ShieldCheck,
  TrendingDown,
  TrendingUp,
  X,
} from "lucide-react";
import { apiGet, apiPost } from "../api";
import { useApi, useDocumentTitle, navigate } from "../hooks";
import {
  arrayFrom,
  barPoints,
  compact,
  dateTime,
  money,
  num,
  pct,
  quoteChange,
  quoteTime,
  quoteValue,
  shortDate,
  timeAgo,
  title,
} from "../lib";
import {
  Badge,
  Button,
  Card,
  CardHeader,
  DataTable,
  EmptyState,
  ErrorBlock,
  LoadingBlock,
  PageHeader,
  SearchInput,
  SelectInput,
  useToast,
} from "../components/ui";
import { LineChart } from "../components/charts";

type Row = Record<string, any>;
type FilterState = {
  exchange: string;
  cap: string;
  index: string;
  sector: string;
  sort: string;
  movers: string;
  availableOnly: boolean;
};

const DEFAULT_FILTERS: FilterState = {
  exchange: "all",
  cap: "all",
  index: "all",
  sector: "all",
  sort: "default",
  movers: "all",
  availableOnly: false,
};

const FALLBACK_EXCHANGES = [
  ["all", "NSE + BSE"],
  ["NSE", "NSE"],
  ["BSE", "BSE"],
];
const FALLBACK_CAPS = [
  ["all", "All market caps"],
  ["large", "Large cap (Nifty 100)"],
  ["mid", "Mid cap (Nifty Midcap 100)"],
  ["small", "Small cap (Nifty Smallcap 100)"],
  ["unclassified", "Unclassified"],
];
const FALLBACK_INDICES = [
  ["all", "All index universes"],
  ["nifty50", "Nifty 50"],
  ["nifty100", "Nifty 100"],
  ["niftytotalmarket", "Nifty Total Market"],
  ["nifty500", "Nifty 500"],
  ["niftymidcap100", "Nifty Midcap 100"],
  ["niftysmallcap100", "Nifty Smallcap 100"],
];
const FALLBACK_SORTS = [
  ["default", "Default"],
  ["gainers", "Top gainers (%)"],
  ["losers", "Top losers (%)"],
  ["profit", "Profit (1D %)"],
  ["loss", "Loss (1D %)"],
  ["high", "1D high"],
  ["low", "1D low"],
  ["volume", "High volume"],
  ["price", "Market price"],
];

const PAGE_SIZE = 50;

function rowId(row: Row | null | undefined): string {
  return String(row?.instrument_id || "");
}

function rowSymbol(row: Row | null | undefined): string {
  return String(row?.symbol || "—");
}

function queryPath(filters: FilterState, search: string, page: number): string {
  const params = new URLSearchParams();
  params.set("exchange", filters.exchange);
  params.set("cap", filters.cap);
  params.set("index", filters.index);
  params.set("sector", filters.sector);
  params.set("sort", filters.sort);
  params.set("movers", filters.movers);
  params.set("available_only", String(filters.availableOnly));
  params.set("limit", String(PAGE_SIZE));
  params.set("offset", String(page * PAGE_SIZE));
  if (search.trim()) params.set("query", search.trim());
  return `/market/screener?${params.toString()}`;
}

function changeClass(value: unknown): string {
  const parsed = num(value, NaN);
  if (!Number.isFinite(parsed)) return "muted";
  return parsed > 0 ? "positive" : parsed < 0 ? "negative" : "muted";
}

function capLabel(value: unknown): string {
  const key = String(value || "unclassified");
  return key === "large" ? "Large" : key === "mid" ? "Mid" : key === "small" ? "Small" : "Unclassified";
}

const INDEX_LABELS = new Map(FALLBACK_INDICES.map(([value, label]) => [value, label]));

function indexLabel(value: unknown): string {
  const raw = String(value || "");
  return INDEX_LABELS.get(raw.toLowerCase())
    || raw.replace("nifty", "Nifty ").replace("midcap", "Midcap ").replace("smallcap", "Smallcap ");
}

function freshnessOf(body: any): string {
  const source = body?.freshness;
  if (!source) return "unknown";
  return String(source.state || source.status || source.freshness || source.label || "unknown");
}

function quoteStatus(body: any): string {
  const quote = body?.quote;
  return String(quote?.status || quote?.usability || (quote?.usable ? "usable" : "unavailable"));
}

function providerOf(body: any): string {
  return String(body?.quote?.selected?.provider || body?.provenance?.provider || "");
}

function LiveInstrument({
  row,
  onBoardRefresh,
  session,
}: {
  row: Row | null;
  onBoardRefresh: () => void;
  session: any;
}) {
  const toast = useToast();
  const id = rowId(row);
  const plan = session?.refresh || {};
  const live = plan.phase === "live";
  // The exchange calendar supplies one cadence: fifteen minutes while the
  // session is active, 15-minute open probes, then hourly. The provider is
  // delayed, so this is a refresh plan, never a claim that the quote is an
  // exchange tick.
  const refreshMs = Math.max(60_000, Number(plan.interval_ms) || 15 * 60_000);
  const quoteRefreshMs = refreshMs;
  const barRefreshMs = refreshMs;
  const quotePath = id
    ? `/market/data/quote/${encodeURIComponent(id)}${live ? "?refresh=true" : ""}`
    : null;
  const intradayPath = id
    ? `/market/data/bars/${encodeURIComponent(id)}?interval=1m&limit=240&ascending=false`
    : null;
  const dailyPath = id
    ? `/market/data/bars/${encodeURIComponent(id)}?interval=1d&limit=120&ascending=false`
    : null;
  const quote = useApi<any>(quotePath, { refreshMs: quoteRefreshMs });
  const bars = useApi<any>(intradayPath, { refreshMs: barRefreshMs });
  // Yahoo's daily bar can lag a completed session while the one-minute series
  // already contains that session's last trades. Read both and show the newer
  // series, so a closed market never looks two days behind.
  const daily = useApi<any>(dailyPath, { refreshMs: 60 * 60 * 1000 });
  // Prefer the daily series for its longer history when both reach the same
  // trading date. Only switch to intraday when the daily bar is a session (or
  // more) behind, which is the stale-chart case this fixes.
  const chartBody =
    newestBarDate(bars.data) > newestBarDate(daily.data) ? bars.data : daily.data;
  const chartIsIntraday =
    chartBody === bars.data &&
    Array.isArray(bars.data?.items) &&
    bars.data.items.length > 0;
  const [fresh, setFresh] = useState<any>(null);
  const [fetching, setFetching] = useState(false);

  useEffect(() => {
    setFresh(null);
  }, [id]);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    const refreshStored = async () => {
      try {
        await apiPost<any>("/market/data/ingest", {
          instrument_ids: [id],
          // Closed: collect both, because the daily bar may not yet include
          // the completed session while the one-minute bars do.
          intervals: live ? ["1m"] : ["1d", "1m"],
          trigger: live ? "live-ui" : "session-ui",
        });
        if (!cancelled) {
          bars.refresh();
          daily.refresh();
          quote.refresh();
          onBoardRefresh();
        }
      } catch {
        // quote/bars show their own errors; an automatic probe must not toast.
      }
    };
    void refreshStored();
    const timer = window.setInterval(refreshStored, refreshMs);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [id, live, refreshMs]); // refresh functions intentionally excluded

  const body = fresh || quote.data;
  const price = quoteValue(body) ?? (row?.quote_available ? row.price : null);
  const change = quoteChange(body) ?? row?.change_pct ?? null;
  const observedAt = quoteTime(body) || row?.as_of;
  const points = useMemo(
    () => barPoints(chartBody).map((point) => ({ ...point, label: shortDate(point.label) })),
    [chartBody],
  );

  const refreshQuote = async () => {
    if (!id) return;
    setFetching(true);
    try {
      const result = await apiGet<any>(`/market/data/quote/${encodeURIComponent(id)}?refresh=true`);
      setFresh(result);
      bars.refresh();
      quote.refresh();
      onBoardRefresh();
      toast.push(
        quoteValue(result) === null
          ? "The provider returned no usable quote for this instrument."
          : `Latest stored quote received for ${rowSymbol(row)}.`,
        quoteValue(result) === null ? "info" : "success",
      );
    } catch (error: any) {
      toast.push(error?.detail || "The quote refresh failed.", "error");
    } finally {
      setFetching(false);
    }
  };

  if (!row) {
    return (
      <Card className="market-live-card">
        <EmptyState
          icon={<Radio size={22} />}
          title="Select an instrument"
          description="Choose a row to inspect its latest stored quote, 1-day range and price history."
        />
      </Card>
    );
  }

  return (
    <Card className="market-live-card">
      <div className="market-live-head">
        <div>
          <div className="eyebrow">Latest market quote</div>
          <h2>{rowSymbol(row)}</h2>
          <p>{row.name || "Instrument"}</p>
        </div>
        <div className="market-live-price">
          {price === null ? <strong className="muted">Unavailable</strong> : <strong>{money(price)}</strong>}
          {change !== null && (
            <span className={`delta ${changeClass(change)}`}>
              {num(change) >= 0 ? <TrendingUp size={13} /> : <TrendingDown size={13} />}
              {pct(change)}
            </span>
          )}
        </div>
      </div>
      <div className="market-live-meta">
        <span><Globe2 size={12} /> {row.exchange || "Market"} · {row.currency || "INR"}</span>
        <Badge tone={row.market_cap === "large" ? "good" : row.market_cap === "mid" ? "info" : row.market_cap === "small" ? "warn" : "neutral"}>
          {capLabel(row.market_cap)}
        </Badge>
        {row.industry && <Badge tone="neutral">{row.industry}</Badge>}
        <span className="market-live-meta-right">
          <Badge tone={freshnessOf(body) === "fresh" ? "good" : freshnessOf(body) === "stale" ? "warn" : "neutral"}>
            {title(freshnessOf(body))}
          </Badge>
        </span>
      </div>
      <div className="market-live-source">
        <span>{providerOf(body) ? `Provider: ${providerOf(body)}` : "Provider: not reported"}</span>
        <span>{observedAt ? `Observed ${timeAgo(observedAt)}` : "No observation timestamp"}</span>
        <span>{session?.label || "Session unavailable"}</span>
        <span title={plan.provider_note || undefined}>
          {plan.phase
            ? `Refresh: ${
                live
                  ? "15m"
                  : plan.phase === "probing"
                    ? `15m open probe ${plan.attempts}/${plan.max_attempts}`
                    : "hourly"
              }`
            : "Refresh: stored"}
        </span>
      </div>
      {quote.error && <div className="inline-warning">{quote.error.detail}</div>}
      {quoteStatus(body) === "conflict" && (
        <div className="inline-warning">Providers disagree beyond tolerance; no reconciled price is selected.</div>
      )}
      <div className="market-live-actions">
        <Button
          variant="primary"
          size="sm"
          icon={<RefreshCw size={13} className={fetching ? "spin" : undefined} />}
          disabled={fetching}
          onClick={refreshQuote}
        >
          {fetching ? "Fetching provider quote…" : "Refresh latest quote"}
        </Button>
        <Button variant="ghost" size="sm" icon={<ListChecksIcon />} onClick={() => navigate("watchlists")}>
          Open watchlists
        </Button>
      </div>

      <div className="market-live-facts">
        <div><span>Previous close</span><strong>{row.previous_close == null ? "Unavailable" : money(row.previous_close)}</strong></div>
        <div><span>1D open</span><strong>{row.day_open == null ? "Unavailable" : money(row.day_open)}</strong></div>
        <div><span>1D high</span><strong>{row.day_high == null ? "Unavailable" : money(row.day_high)}</strong></div>
        <div><span>1D low</span><strong>{row.day_low == null ? "Unavailable" : money(row.day_low)}</strong></div>
        <div><span>Volume</span><strong>{row.volume == null ? "Unavailable" : compact(row.volume)}</strong></div>
        <div><span>Relative vol.</span><strong>{row.relative_volume == null ? "Unavailable" : `${num(row.relative_volume).toFixed(2)}×`}</strong></div>
      </div>
      <div className="market-live-basis">
        <CircleAlert size={12} />
        <span>
          {row.range_available
            ? row.range_basis === "provider quote day range"
              ? "1-day range and volume come from the provider quote's own session."
              : "1-day range and volume are from the same stored session as the quote."
            : "No session range is stored, so the 1-day range and volume are unavailable. The quote is not substituted for them."}
        </span>
      </div>

      <div className="chart-block market-live-chart">
        <div className="chart-block-head">
          <span>
            {chartIsIntraday
              ? live
                ? "Live 1-minute price history"
                : "Latest 1-minute price history (last session)"
              : "Stored daily price history"}
          </span>
          <Badge tone="neutral">{points.length ? `${points.length} bars` : "No bars"}</Badge>
        </div>
        {bars.loading && daily.loading ? (
          <LoadingBlock rows={3} label="Reading stored bars…" />
        ) : !chartBody && (bars.error || daily.error) ? (
          <ErrorBlock
            message={(bars.error || daily.error).detail}
            onRetry={() => {
              bars.refresh();
              daily.refresh();
            }}
          />
        ) : (
          <LineChart
            data={points}
            height={235}
            color="var(--accent)"
            empty={chartIsIntraday ? "No intraday bars are stored for the last session" : "No daily bars are stored for this instrument"}
          />
        )}
      </div>
      <div className="market-live-foot">
        <ShieldCheck size={12} />
        <span>Observation only. No order, signal or trade is created here.</span>
      </div>
    </Card>
  );
}

function newestBarDate(body: any): string {
  const items = Array.isArray(body?.items) ? body.items : [];
  const newest = items.reduce((latest: number, item: any) => {
    const value = Date.parse(String(item?.observed_at || ""));
    return Number.isFinite(value) && value > latest ? value : latest;
  }, 0);
  return newest ? new Date(newest).toISOString().slice(0, 10) : "";
}

function ListChecksIcon() {
  return <Layers3 size={13} />;
}

function CapSignal({
  label,
  tier,
  data,
  active,
  onClick,
}: {
  label: string;
  tier: string;
  data: any;
  active: boolean;
  onClick: () => void;
}) {
  const row = data || {};
  return (
    <button type="button" className={`market-cap-signal ${active ? "active" : ""}`} data-tier={tier} onClick={onClick}>
      <span className="market-cap-signal-title">{label}</span>
      <div className="market-cap-signal-count">
        <strong>{row.quotes_available ?? 0}</strong>
        <span>quoted / {row.count ?? 0}</span>
      </div>
      <div className="market-cap-signal-moves">
        <span className="positive"><TrendingUp size={11} /> {row.gainers ?? 0}</span>
        <span className="negative"><TrendingDown size={11} /> {row.losers ?? 0}</span>
        <span className="muted">{row.average_change_pct == null ? "—" : pct(row.average_change_pct)}</span>
      </div>
    </button>
  );
}

export function MarketPage() {
  useDocumentTitle("Market Overview");
  const toast = useToast();
  // The server owns the 09:00/09:15/15:30/17:00 transitions; a short poll only
  // discovers the transition promptly in an already-open tab.
  const status = useApi<any>("/market/status", { refreshMs: 15_000 });
  const events = useApi<any>("/market/events?limit=5");
  const [filters, setFilters] = useState<FilterState>(DEFAULT_FILTERS);
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [page, setPage] = useState(0);
  const [fetching, setFetching] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => setSearch(searchInput), 300);
    return () => window.clearTimeout(timer);
  }, [searchInput]);

  const screener = useApi<any>(queryPath(filters, search, page), { refreshMs: 60_000 });
  const data = screener.data;
  const rows = useMemo(() => arrayFrom(data, "rows"), [data]);
  const summary = data?.summary || {};
  const coverage = data?.coverage || {};
  const session = status.data?.market_session || data?.session || {};
  const options = data?.options || {};
  const statusState = status.data?.health?.state || "unknown";

  useEffect(() => {
    setPage(0);
  }, [filters, search]);

  useEffect(() => {
    if (!rows.length) {
      if (selectedId) setSelectedId("");
      return;
    }
    if (!rows.some((row) => rowId(row) === selectedId)) setSelectedId(rowId(rows[0]));
  }, [rows, selectedId]);

  const selected = rows.find((row) => rowId(row) === selectedId) || rows[0] || null;
  const totalRows = num(coverage.total ?? coverage.listed_after_availability ?? coverage.matched ?? rows.length);
  const pageCount = Math.max(1, Math.ceil(totalRows / PAGE_SIZE));
  const safePage = Math.min(page, pageCount - 1);
  const pageRows = rows.slice(0, PAGE_SIZE);

  const setFilter = <K extends keyof FilterState>(key: K, value: FilterState[K]) => {
    setFilters((current) => ({ ...current, [key]: value }));
  };

  const resetFilters = () => {
    setFilters(DEFAULT_FILTERS);
    setSearchInput("");
    setSearch("");
  };

  const refreshAll = () => {
    screener.refresh();
    status.refresh();
    events.refresh();
  };

  const fetchLatest = async () => {
    setFetching(true);
    try {
      // "Quoted only" is a view filter, not a fetch filter. Otherwise an empty
      // board can never discover a first quote for any of its rows.
      const candidateBody = filters.availableOnly
        ? await apiGet<any>(queryPath({ ...filters, availableOnly: false }, search, 0))
        : data;
      const candidates = filters.availableOnly ? arrayFrom(candidateBody, "rows") : rows;
      const missing = candidates.filter((row) => rowId(row) && row.price == null);
      // A refresh that keeps asking for the same already-quoted rows cannot
      // grow the board. Fill the gaps first, then refresh existing quotes.
      const ids = (missing.length ? missing : candidates)
        .slice(0, 25)
        .map(rowId)
        .filter(Boolean);
      if (!ids.length) {
        toast.push("No instruments match the current filters.", "info");
        return;
      }
      const report = await apiPost<any>(
        "/market/data/ingest",
        {
          instrument_ids: ids,
          intervals: ["1d"],
          trigger: "market-overview",
          force_quotes: true,
        },
        undefined,
        { timeoutMs: 180_000 },
      );
      const inserted = Number(report?.quotes_inserted ?? 0);
      const duplicates = Number(report?.quotes_duplicate ?? 0);
      const unavailable = Number(report?.instruments_unavailable ?? 0);
      toast.push(
        `Bounded cycle: ${inserted} new quote${inserted === 1 ? "" : "s"}, ${unavailable} unavailable${duplicates ? `, ${duplicates} already stored` : ""}. Quotes stay marked with their provider freshness.`,
        inserted || duplicates ? "success" : "info",
      );
      refreshAll();
    } catch (error: any) {
      toast.push(error?.detail || "The market-data cycle could not run.", "error");
    } finally {
      setFetching(false);
    }
  };

  const eventRows = arrayFrom(events.data, "events");
  const breadth = summary.gainers + summary.losers || 0;
  const breadthPct = breadth ? (summary.gainers / breadth) * 100 : 50;
  const exchangeOptions = options.exchanges?.length ? options.exchanges : FALLBACK_EXCHANGES;
  const capOptions = options.caps?.length ? options.caps : FALLBACK_CAPS;
  const indexOptions = options.indices?.length ? options.indices : FALLBACK_INDICES;
  const sortOptions = options.sorts?.length ? options.sorts : FALLBACK_SORTS;
  const sectorOptions = options.sectors || [];

  return (
    <div className="page market-page market-overview-page">
      <PageHeader
        eyebrow="Market intelligence / Live overview"
        title={<>The tape, <em>with its limits visible.</em></>}
        description="A filterable NSE and BSE market board built from stored provider observations and the published Nifty constituent lists. Every value carries its source, timestamp and freshness. Delayed data is never painted as real-time."
        meta={
          <>
            <span className="meta-item"><i className={`health-dot health-${String(statusState).toLowerCase()}`} /> {title(statusState)}</span>
            <span className="meta-item"><Radio size={13} /> {session.label || "Session unavailable"}</span>
            <span className="meta-item"><CalendarDays size={13} /> {dateTime(data?.generated_at || new Date().toISOString())}</span>
            <span className="meta-item"><ShieldCheck size={13} /> {data?.live ? "Live" : "Stored observations"}</span>
          </>
        }
        actions={
          <>
            <Button variant="ghost" icon={<RefreshCw size={14} className={screener.refreshing ? "spin" : undefined} />} onClick={refreshAll}>
              Refresh view
            </Button>
            <Button variant="primary" icon={<Radio size={14} />} loading={fetching} onClick={fetchLatest}>
              {Number(coverage.quotes_missing_listings ?? 0) > 0
                ? `Fetch missing · ${Math.min(25, Number(coverage.quotes_missing_listings))}`
                : "Refresh latest · 25"}
            </Button>
          </>
        }
      />

      {status.error && <ErrorBlock message={status.error.detail} onRetry={status.refresh} />}
      {screener.error && <ErrorBlock message={screener.error.detail} onRetry={screener.refresh} />}
      {status.data?.config_error && <div className="inline-warning">{status.data.config_error}</div>}

      <div className="market-tape-strip">
        <div className="market-tape-session">
          <span className={`market-live-dot ${String(session.state || "").toLowerCase() === "open" ? "is-open" : ""}`} />
          <div>
            <span>Market session</span>
            <strong>{session.label || "Calendar unavailable"}</strong>
          </div>
        </div>
        <div><span>Quoted stocks</span><strong>{summary.quotes_available ?? 0} / {summary.stocks ?? coverage.catalogue_total ?? 0}</strong></div>
        <div><span>Board rows</span><strong>{rows.length} shown of {coverage.matched ?? 0} matched</strong></div>
        <div><span>Advance / decline</span><strong><span className="positive">{summary.gainers ?? 0}</span> / <span className="negative">{summary.losers ?? 0}</span></strong></div>
        <div><span>Index snapshot</span><strong>{data?.reference?.as_of || "—"}</strong></div>
        <div className="market-tape-breadth">
          <span style={{ width: `${breadthPct}%` }} />
        </div>
        <div className="market-tape-actions">
          <Badge tone={data?.live ? "good" : "warn"}>{data?.live ? "Live feed" : "Delayed / stored"}</Badge>
          <button type="button" className="text-link" onClick={() => navigate("health")}>Data health <ArrowRight size={12} /></button>
        </div>
      </div>

      <div className="market-signal-strip">
        <button
          type="button"
          className={`market-move-indicator ${filters.movers === "all" ? "active" : ""}`}
          onClick={() => setFilter("movers", "all")}
        >
          All stocks <strong>{summary.stocks ?? 0}</strong>
        </button>
        <button
          type="button"
          className={`market-move-indicator market-move-up ${filters.movers === "gainers" ? "active" : ""}`}
          onClick={() => setFilter("movers", filters.movers === "gainers" ? "all" : "gainers")}
        >
          <TrendingUp size={14} /> Gainers <strong>{summary.gainers ?? 0}</strong>
        </button>
        <button
          type="button"
          className={`market-move-indicator market-move-down ${filters.movers === "losers" ? "active" : ""}`}
          onClick={() => setFilter("movers", filters.movers === "losers" ? "all" : "losers")}
        >
          <TrendingDown size={14} /> Losers <strong>{summary.losers ?? 0}</strong>
        </button>
        <span className={`market-breadth-label ${summary.gainers > summary.losers ? "positive" : summary.losers > summary.gainers ? "negative" : "muted"}`}>
          {summary.average_change_pct == null ? "Average move unavailable" : `Average move ${pct(summary.average_change_pct)}`}
        </span>
        <span className="market-signal-coverage">{summary.quotes_missing ? `${summary.quotes_missing} without a stored quote` : "Every matched row has a quote"}</span>
      </div>

      <div className="market-cap-grid">
        <CapSignal label="Large cap" tier="large" data={summary.by_market_cap?.large} active={filters.cap === "large"} onClick={() => setFilter("cap", filters.cap === "large" ? "all" : "large")} />
        <CapSignal label="Mid cap" tier="mid" data={summary.by_market_cap?.mid} active={filters.cap === "mid"} onClick={() => setFilter("cap", filters.cap === "mid" ? "all" : "mid")} />
        <CapSignal label="Small cap" tier="small" data={summary.by_market_cap?.small} active={filters.cap === "small"} onClick={() => setFilter("cap", filters.cap === "small" ? "all" : "small")} />
        <div className="market-cap-note">
          <Gauge size={15} />
          <span>Index-defined tiers: Nifty 100 / Midcap 100 / Smallcap 100. Other listings stay unclassified.</span>
        </div>
      </div>

      <Card className="market-board-card">
        <CardHeader
          title="Market board"
          subtitle="Canonical instrument identities, shared by every NovaMarket desk"
          action={<Badge tone="neutral">{coverage.returned ?? rows.length} of {coverage.matched ?? 0}</Badge>}
        />
        <div className="market-filter-bar">
          <div className="market-filter market-filter-search">
            <span>Search</span>
            <SearchInput value={searchInput} onChange={setSearchInput} placeholder="Symbol, company or industry…" />
          </div>
          <label className="market-filter">
            <span>Exchange</span>
            <SelectInput value={filters.exchange} onChange={(event) => setFilter("exchange", event.target.value)}>
              {exchangeOptions.map(([value, label]: [string, string]) => <option key={value} value={value}>{label}</option>)}
            </SelectInput>
          </label>
          <label className="market-filter">
            <span>Market cap</span>
            <SelectInput value={filters.cap} onChange={(event) => setFilter("cap", event.target.value)}>
              {capOptions.map(([value, label]: [string, string]) => <option key={value} value={value}>{label}</option>)}
            </SelectInput>
          </label>
          <label className="market-filter">
            <span>Index category</span>
            <SelectInput value={filters.index} onChange={(event) => setFilter("index", event.target.value)}>
              {indexOptions.map(([value, label]: [string, string]) => <option key={value} value={value}>{label}</option>)}
            </SelectInput>
          </label>
          <label className="market-filter">
            <span>Industry</span>
            <SelectInput value={filters.sector} onChange={(event) => setFilter("sector", event.target.value)}>
              <option value="all">All industries</option>
              {sectorOptions.map((value: string) => <option key={value} value={value}>{value}</option>)}
            </SelectInput>
          </label>
          <label className="market-filter">
            <span>Arrangement</span>
            <SelectInput value={filters.sort} onChange={(event) => setFilter("sort", event.target.value)}>
              {sortOptions.map(([value, label]: [string, string]) => <option key={value} value={value}>{label}</option>)}
            </SelectInput>
          </label>
          <label className="market-filter market-filter-toggle">
            <input type="checkbox" checked={filters.availableOnly} onChange={(event) => setFilter("availableOnly", event.target.checked)} />
            <span>Quoted only</span>
          </label>
          <Button variant="ghost" size="sm" icon={<X size={13} />} onClick={resetFilters}>Reset</Button>
        </div>

        <div className="market-board-note">
          <Filter size={12} />
          <span>
            {filters.availableOnly
              ? "Rows without a stored provider quote are hidden. Turn off “Quoted only” to browse the full matched universe."
              : "Rows without a stored provider quote remain visible with an unavailable price, never a placeholder."}
          </span>
          <span className="market-board-source">Source: {data?.data_state === "stored_provider_observations" ? "stored provider observations" : "no board response"}</span>
        </div>

        {screener.loading ? (
          <LoadingBlock rows={8} label="Reading the market board and stored quotes…" />
        ) : (
          <>
            <DataTable
              compact
              rows={pageRows}
              rowKey={(row: Row) => rowId(row)}
              onRowClick={(row: Row) => setSelectedId(rowId(row))}
              empty={
                <EmptyState
                  icon={<Search size={21} />}
                  title="No instruments match"
                  description="Reset the filters, broaden the index or turn off “Quoted only” to see names without stored prices."
                  action={<Button size="sm" variant="primary" onClick={resetFilters}>Reset filters</Button>}
                />
              }
              columns={[
                {
                  key: "rank",
                  label: "#",
                  width: "38px",
                  render: (_row: Row) => <span className="muted mono">{String(safePage * PAGE_SIZE + pageRows.indexOf(_row) + 1).padStart(2, "0")}</span>,
                },
                {
                  key: "instrument",
                  label: "Instrument",
                  render: (row: Row) => (
                    <div className="identity-cell">
                      <span className="symbol-mark">{rowSymbol(row).slice(0, 2)}</span>
                      <div>
                        <strong>{rowSymbol(row)}</strong>
                        <small>{row.name || "Unnamed instrument"}{row.industry ? ` · ${row.industry}` : ""}</small>
                      </div>
                    </div>
                  ),
                },
                {
                  key: "exchange",
                  label: "Exch.",
                  width: "62px",
                  render: (row: Row) => <Badge tone={row.exchange === "NSE" ? "info" : "neutral"}>{row.exchange || "—"}</Badge>,
                },
                {
                  key: "cap",
                  label: "Tier",
                  width: "104px",
                  render: (row: Row) => (
                    <div className="market-tier-cell">
                      <Badge tone={row.market_cap === "large" ? "good" : row.market_cap === "mid" ? "info" : row.market_cap === "small" ? "warn" : "neutral"}>{capLabel(row.market_cap)}</Badge>
                      {row.index_membership?.length > 0 && <small>{indexLabel(row.index_membership[0])}</small>}
                    </div>
                  ),
                },
                {
                  key: "price",
                  label: "Last",
                  align: "right",
                  render: (row: Row) => row.price == null ? <span className="muted">Unavailable</span> : <span className="mono market-last-price">{money(row.price)}</span>,
                },
                {
                  key: "change",
                  label: "Change",
                  align: "right",
                  render: (row: Row) => row.change_pct == null ? <span className="muted">—</span> : (
                    <span className={`delta ${changeClass(row.change_pct)}`}>
                      {num(row.change_pct) >= 0 ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
                      {pct(row.change_pct)}
                    </span>
                  ),
                },
                {
                  key: "range",
                  label: "1D range",
                  align: "right",
                  render: (row: Row) => row.day_high == null || row.day_low == null
                    ? <span className="muted">Unavailable</span>
                    : <span className="mono market-range"><span>{money(row.day_high)}</span><small>{money(row.day_low)}</small></span>,
                },
                {
                  key: "volume",
                  label: "Volume",
                  align: "right",
                  render: (row: Row) => row.volume == null ? <span className="muted">—</span> : <span className="mono">{compact(row.volume)}</span>,
                },
                {
                  key: "freshness",
                  label: "As of",
                  render: (row: Row) => (
                    <div className="market-freshness-cell">
                      <span>{row.as_of ? timeAgo(row.as_of) : "No quote"}</span>
                      <small>{row.provider || "—"} · {row.freshness || "unknown"}</small>
                    </div>
                  ),
                },
                { key: "chevron", label: "", align: "right", width: "28px", render: () => <ChevronRight size={14} className="row-chevron" /> },
              ]}
            />
            {pageCount > 1 && (
              <div className="pagination-row">
                <span className="muted">Page {safePage + 1} of {pageCount} · {totalRows.toLocaleString()} listings match</span>
                <div className="row-actions">
                  <Button variant="ghost" size="sm" disabled={safePage === 0} onClick={() => setPage(Math.max(0, safePage - 1))}>Previous</Button>
                  <Button variant="ghost" size="sm" disabled={safePage >= pageCount - 1} onClick={() => setPage(Math.min(pageCount - 1, safePage + 1))}>Next</Button>
                </div>
              </div>
            )}
          </>
        )}
      </Card>

      <div className="market-layout market-detail-layout">
        <LiveInstrument row={selected} onBoardRefresh={screener.refresh} session={session} />
        <div className="stack">
          <Card>
            <CardHeader title="Latest events" subtitle="Evidence-backed market signals" action={<button type="button" className="text-link" onClick={() => navigate("events")}>All events <ArrowRight size={13} /></button>} />
            {events.loading ? <LoadingBlock rows={3} label="Reading the radar…" /> : eventRows.length ? (
              <div className="event-stack">
                {eventRows.map((event, index) => (
                  <button type="button" className="event-row" key={event.event_id || index} onClick={() => navigate("events")}>
                    <span className={`event-marker event-${String(event.importance || "normal").toLowerCase()}`} />
                    <span className="event-copy"><strong>{event.title || event.headline || event.summary || "Market event"}</strong><small>{title(event.category || "event")} · {timeAgo(event.detected_at || event.created_at)}</small></span>
                    <Badge tone="neutral">{title(event.verification_state || event.status || "recorded")}</Badge>
                  </button>
                ))}
              </div>
            ) : <EmptyState icon={<Newspaper size={20} />} title="No events recorded" description="The radar will publish verified signals here." />}
          </Card>

          <Card>
            <CardHeader title="Data contract" subtitle="What this board is allowed to claim" />
            <div className="contract-list">
              <div><CheckCircle2 size={15} /><span>Prices are stored provider observations, not synthesized ticks.</span></div>
              <div><CheckCircle2 size={15} /><span>A missing quote is shown as unavailable, never interpolated.</span></div>
              <div><CheckCircle2 size={15} /><span>Index membership comes from a dated NSE constituent snapshot.</span></div>
              <div><CheckCircle2 size={15} /><span>Reference: {data?.reference?.as_of || "unavailable"} · {data?.reference?.source || "source unavailable"}</span></div>
            </div>
            <div className="market-contract-actions">
              <Button variant="secondary" size="sm" onClick={() => navigate("health")} icon={<Activity size={13} />}>System health</Button>
              <Button variant="ghost" size="sm" onClick={() => navigate("settings")} icon={<Gauge size={13} />}>Data settings</Button>
            </div>
          </Card>
        </div>
      </div>

      <details className="market-methodology">
        <summary><BarChart3 size={14} /> Calculation methodology and coverage</summary>
        <div className="market-methodology-grid">
          <div><strong>Quote</strong><span>Latest valid observation from the configured provider, with its own observed timestamp. Yahoo is delayed/unofficial; the board labels it as stored rather than real-time.</span></div>
          <div><strong>Change</strong><span>Provider observation versus provider previous close. If the provider does not expose one, only a same-session or last-four-day stored close is used; otherwise change is unavailable.</span></div>
          <div><strong>1D range / volume</strong><span>Only a daily bar in the same session as the quote is used. Stale bars are not relabelled as today.</span></div>
          <div><strong>Index / cap</strong><span>Exact NSE symbol match. Cap tier is index-defined, not a calculated market capitalisation.</span></div>
          <div><strong>Sample</strong><span>{coverage.scanned ?? 0} catalogue rows scanned; {coverage.quotes_available ?? 0} have a usable stored quote; {coverage.quotes_missing ?? 0} are explicitly unavailable.</span></div>
          <div><strong>Boundary</strong><span>No real-money execution. Risk, paper trading and Shadow Mode remain separate books and are not bypassed by this screen.</span></div>
        </div>
        <p className="market-disclaimer">{data?.disclaimer || "Provider observations only; not investment advice."}</p>
      </details>
    </div>
  );
}
