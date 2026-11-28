import { useEffect, useMemo, useRef, useState } from "react";
import { History, RefreshCw, Save, Search } from "lucide-react";
import { API_BASE, apiGet, apiPost, apiPut } from "../api";
import { useApi, useDocumentTitle } from "../hooks";
import { arrayFrom, dateTime, num, title } from "../lib";
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  Chip,
  DataTable,
  EmptyState,
  ErrorBlock,
  Field,
  KeyValue,
  LoadingBlock,
  Metric,
  PageHeader,
  SelectInput,
  StatusBadge,
  TextInput,
  useToast,
  type Column,
} from "../components/ui";
import {
  CandlestickChart,
  HBar,
  LineChart,
  type CandleBar,
} from "../components/charts";

const VIEWS = [
  { id: "overview", label: "Overview" },
  { id: "instrument", label: "Instrument" },
  { id: "forecasts", label: "Forecasts" },
  { id: "compare", label: "Comparison" },
  { id: "horizons", label: "Horizons" },
  { id: "lifecycle", label: "Lifecycle" },
  { id: "performance", label: "Performance" },
  { id: "regimes", label: "Regimes" },
  { id: "decisions", label: "Agent decisions" },
  { id: "research", label: "Research" },
  { id: "portfolio", label: "Portfolio" },
  { id: "policies", label: "RL & adaptive" },
  { id: "replay", label: "Replay" },
] as const;

const HORIZONS = ["", "5m", "15m", "20m", "1h", "1d", "3d", "5d", "30d", "90d"];
const DIRECTIONS = ["", "up", "down", "neutral", "uncertain"];
const VALIDITY = ["", "valid", "unconfirmed", "expired", "stale", "invalid", "unavailable"];
const DECISION_STATUS = ["", "completed", "abstained", "degraded", "failed"];
/** The API rejects any instrument filter that is not MIC-qualified (422). */
const MIC_SYMBOL = /^[A-Z0-9]{2,8}:[A-Z0-9][A-Z0-9._-]*$/;

type Filters = {
  instrument_id: string;
  exchange: string;
  sector: string;
  regime: string;
  horizon: string;
  model_id: string;
  direction: string;
  validity: string;
  evaluation_start: string;
  evaluation_end: string;
  decision_status: string;
  agent_id: string;
  portfolio_id: string;
  research_status: string;
};

const DASHBOARD_EVENT_TYPES = [
  "novamarket.forecast.created",
  "novamarket.forecast.completed",
  "novamarket.forecast.evaluated",
  "novamarket.forecast.calibration.completed",
  "novamarket.forecast.performance.updated",
  "novamarket.forecast.model.trained",
  "novamarket.forecast.model.validated",
  "novamarket.forecast.model.activated",
  "novamarket.forecast.model.degraded",
  "novamarket.prediction.lifecycle.created",
  "novamarket.prediction.forecast.generated",
  "novamarket.prediction.forecast.confirmed",
  "novamarket.prediction.forecast.evaluated",
  "novamarket.prediction.decision.evidence_ready",
  "novamarket.prediction.abstained",
  "novamarket.regime.detected",
  "novamarket.regime.transitioned",
  "novamarket.portfolio.valuation.updated",
  "novamarket.portfolio.rebalance.proposed",
  "novamarket.portfolio.rebalance.completed",
  "novamarket.data.freshness_changed",
  "novamarket.market_event.discovered",
  "novamarket.market_event.updated",
  "novamarket.market_event.retracted",
  "novamarket.data.stale",
  "novamarket.data.available",
  "novamarket.data.unavailable",
  // One event per committed PI-17/18/19 operation and policy-health alert, so
  // the decisions, research, portfolio and policies views refresh on change.
  "novamarket.agent.cycle.completed",
  "novamarket.research.run.completed",
  "novamarket.optimization.recommendation.created",
  "novamarket.policy.degraded",
] as const;

const EMPTY_FILTERS: Filters = {
  instrument_id: "",
  exchange: "",
  sector: "",
  regime: "",
  horizon: "",
  model_id: "",
  direction: "",
  validity: "",
  evaluation_start: "",
  evaluation_end: "",
  decision_status: "",
  agent_id: "",
  portfolio_id: "",
  research_status: "",
};

function useDashboardEvents(): number {
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    const refresh = () => setRevision((current) => current + 1);
    let cancelled = false;
    let stream: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    const scheduleRetry = () => {
      if (cancelled || retry) return;
      retry = setTimeout(() => {
        retry = undefined;
        void open();
      }, 5_000);
    };
    const open = async () => {
      try {
        await apiPost("/events/session", {}, undefined, { timeoutMs: 5_000 });
        if (cancelled) return;
        stream = new EventSource(`${API_BASE}/events/stream`);
        stream.onerror = () => {
          stream?.close();
          stream = null;
          scheduleRetry();
        };
        DASHBOARD_EVENT_TYPES.forEach((eventType) =>
          stream?.addEventListener(eventType, refresh),
        );
      } catch {
        // A broken stream must not break the dashboard or become a polling loop.
      }
    };
    void open();
    // EventSource reconnects on its own while the server-side session lives.
    return () => {
      cancelled = true;
      if (retry) clearTimeout(retry);
      stream?.close();
    };
  }, []);
  return revision;
}

function queryFor(filters: Filters, extra: Record<string, string | number> = {}): string {
  const params = new URLSearchParams();
  Object.entries({ ...filters, limit: 50, ...extra }).forEach(([key, value]) => {
    if (value !== "" && value !== null && value !== undefined) params.set(key, String(value));
  });
  return params.toString();
}

function pct(value: unknown, digits = 2): string {
  const number = Number(value);
  return Number.isFinite(number) ? `${(number * 100).toFixed(digits)}%` : "—";
}

function intervalText(low: unknown, high: unknown): string {
  const lower = Number(low);
  const upper = Number(high);
  return Number.isFinite(lower) && Number.isFinite(upper)
    ? `${price(lower)} – ${price(upper)}`
    : "—";
}

function ForecastRanges({ rows }: { rows: any[] }) {
  const usable = rows.filter((row) =>
    [row.interval_low, row.interval_high, row.reference_price].every((value) =>
      Number.isFinite(Number(value)),
    ),
  );
  if (!usable.length) {
    return <p className="muted">No forecast has a usable prediction interval.</p>;
  }
  const values = usable.flatMap((row) => [
    Number(row.interval_low),
    Number(row.interval_high),
    Number(row.reference_price),
  ]);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;
  return (
    <div className="stack-tight">
      {usable.map((row, index) => {
        const low = Number(row.interval_low);
        const high = Number(row.interval_high);
        const reference = Number(row.reference_price);
        return (
          <div key={`${row.forecast_id || row.model_id}-${index}`}>
            <div className="hbar-label">
              <span>{row.model_id || row.engine || "model"} · {row.horizon}</span>
              <strong>{intervalText(low, high)}</strong>
            </div>
            <div className="forecast-range-track" aria-label={`Prediction interval for ${row.model_id || row.engine}`}>
              <span
                className="forecast-range-band"
                style={{
                  left: `${((low - min) / span) * 100}%`,
                  width: `${Math.max(0.5, ((high - low) / span) * 100)}%`,
                }}
              />
              <span
                className="forecast-range-reference"
                style={{ left: `${((reference - min) / span) * 100}%` }}
                title={`Reference ${price(reference)}`}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

function price(value: unknown): string {
  const number = Number(value);
  return Number.isFinite(number)
    ? number.toLocaleString("en-IN", { maximumFractionDigits: 2 })
    : "—";
}

/** A table cell value: scalar text, or compact JSON for a structured payload.
 * React cannot render an object child, and PI-14 validation/reward payloads
 * arrive as objects rather than strings. */
function cell(value: unknown, fallback = "—"): string {
  if (value === null || value === undefined || value === "") return fallback;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** PI-19 thesis factors arrive as objects; render the recorded rationale, never
 * a raw JSON blob (an unbroken JSON string widened the page past the viewport). */
function FactorList({ rows }: { rows: any[] }) {
  if (!rows.length) return <p className="muted">No factor recorded.</p>;
  return (
    <ul className="plain-list">
      {rows.map((item: any, index: number) => {
        const text =
          typeof item === "string"
            ? item
            : item?.rationale || item?.claim || item?.summary || item?.factor || JSON.stringify(item);
        const references = typeof item === "object" && item ? arrayFrom(item.evidence_ids).length : 0;
        return (
          <li key={index}>
            {text}
            {references > 0 && <span className="muted"> · {references} evidence reference(s)</span>}
          </li>
        );
      })}
    </ul>
  );
}

function shortDate(value: unknown): string {
  if (!value) return "—";
  const parsed = new Date(String(value));
  return Number.isNaN(parsed.getTime()) ? String(value) : dateTime(parsed.toISOString());
}

function stateTone(value: unknown): "good" | "warn" | "bad" | "neutral" | "info" {
  const status = String(value || "").toLowerCase();
  if (["available", "valid", "up", "buy", "calibrated", "completed"].includes(status)) return "good";
  if (["partial_data", "stale", "uncertain", "hold", "wait", "unconfirmed", "degraded"].includes(status)) return "warn";
  if (["failed", "unavailable", "invalid", "down", "sell", "insufficient_evidence"].includes(status)) return "bad";
  return "info";
}

function metricTone(value: unknown): "good" | "warn" | "bad" | "neutral" | "accent" {
  const tone = stateTone(value);
  return tone === "info" ? "neutral" : tone;
}

function directionBadge(value: unknown) {
  return <Badge tone={stateTone(value)} dot>{title(value || "uncertain")}</Badge>;
}

function Gate({
  state,
  empty,
  children,
}: {
  state: any;
  empty?: string;
  children: React.ReactNode;
}) {
  if (state.loading && !state.data) return <LoadingBlock />;
  if (state.error && !state.data) {
    return <ErrorBlock message={state.error.detail || state.error.message} onRetry={state.refresh} />;
  }
  if (!state.data) return <EmptyState level={2} title="No data yet" description={empty || "The backend has no recorded evidence for this view."} />;
  return (
    <>
      {state.error && (
        <Banner tone="warn" title="Refresh failed">
          Showing the last successful response. {state.error.detail || state.error.message}
        </Banner>
      )}
      {children}
    </>
  );
}

function StatusLine({ body }: { body: any }) {
  if (!body) return null;
  const status = String(body.data_status || "unknown");
  return (
    <div className="status-line">
      <StatusBadge value={status} />
      <span className="muted">As of {shortDate(body.as_of)}</span>
      {body.source_errors && Object.keys(body.source_errors).length > 0 && (
        <Badge tone="warn">{Object.keys(body.source_errors).length} unavailable source(s)</Badge>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ overview */

function OverviewView({ filters, revision }: { filters: Filters; revision: number }) {
  const state = useApi<any>(`/market/intelligence/overview?${queryFor(filters)}`, {
    refreshKey: revision,
  });
  return (
    <Gate state={state}>
      {state.data && (
        <div className="stack">
          <StatusLine body={state.data} />
          <div className="metric-grid">
            <Metric label="Active forecasts" value={state.data.predictions?.active_forecasts ?? "—"} help={state.data.predictions?.eligible_instruments == null ? undefined : `${state.data.predictions.eligible_instruments} instruments`} />
            <Metric label="Upward" value={state.data.predictions?.upward ?? "—"} tone="good" />
            <Metric label="Downward" value={state.data.predictions?.downward ?? "—"} tone="bad" />
            <Metric label="Neutral / uncertain" value={`${state.data.predictions?.neutral ?? "—"} / ${state.data.predictions?.uncertain ?? "—"}`} tone="warn" />
            <Metric label="Models" value={state.data.models?.available_count ?? "—"} help={state.data.models?.active_versions == null ? undefined : `${state.data.models.active_versions} active versions`} />
            <Metric label="Decision cycles" value={state.data.decisions?.cycles ?? "—"} help={state.data.decisions?.requires_more_evidence == null ? undefined : `${state.data.decisions.requires_more_evidence} need more evidence`} />
          </div>

          <div className="split-grid">
            <Card>
              <CardHeader title="Market context" subtitle={state.data.market?.price_basis || "Stored provider observations"} action={<Badge tone={state.data.market?.live ? "good" : "warn"}>{state.data.market?.live ? "Live" : "Delayed / stored"}</Badge>} />
              <div className="metric-grid compact-grid">
                <Metric label="Gainers" value={state.data.market?.summary?.gainers ?? "—"} tone="good" />
                <Metric label="Losers" value={state.data.market?.summary?.losers ?? "—"} tone="bad" />
                <Metric label="Advance / decline" value={state.data.market?.summary?.advance_decline_ratio ?? "—"} />
              </div>
              <dl className="key-value-grid">
                <KeyValue label="Session">{title(state.data.market?.session?.state || "not provided")}</KeyValue>
                <KeyValue label="Regime">{state.data.regime?.primary || "uncertain"}</KeyValue>
                <KeyValue label="Regime confidence">{state.data.regime?.confidence_kind ? `${state.data.regime.confidence ?? "—"} (${state.data.regime.confidence_kind})` : "not calibrated"}</KeyValue>
                <KeyValue label="Detected">{shortDate(state.data.regime?.detected_at)}</KeyValue>
              </dl>
              {state.data.regime?.status === "insufficient_evidence" && (
                <Banner tone="warn" title="No regime call">The detector returned uncertain; the dashboard does not invent a regime.</Banner>
              )}
            </Card>

            <Card>
              <CardHeader title="Decision snapshot" subtitle="Recommendations are not executed trades" />
              <div className="metric-grid compact-grid">
                <Metric label="Buy" value={state.data.decisions?.buy ?? "—"} tone="good" />
                <Metric label="Sell" value={state.data.decisions?.sell ?? "—"} tone="bad" />
                <Metric label="Hold" value={state.data.decisions?.hold ?? "—"} />
                <Metric label="Wait" value={state.data.decisions?.wait ?? "—"} tone="warn" />
                <Metric label="No action" value={state.data.decisions?.no_action ?? "—"} />
              </div>
              <p className="muted">{state.data.decisions?.distinction}</p>
              <DataTable
                rows={arrayFrom(state.data.decisions?.latest)}
                rowKey={(row: any, index) => row.cycle_id || String(index)}
                empty={<EmptyState title="No decision cycles" description="PI-17 cycles appear here once recorded." />}
                columns={[
                  { key: "instrument_id", label: "Instrument" },
                  { key: "action", label: "Action", render: (row: any) => directionBadge(row.action) },
                  { key: "confidence", label: "Decision confidence (not probability)", render: (row: any) => `${row.confidence ?? "—"} ${row.confidence_kind ? `(${row.confidence_kind})` : ""}` },
                  { key: "decided_at", label: "Decided", render: (row: any) => shortDate(row.decided_at) },
                ]}
              />
            </Card>
          </div>

          <Card>
            <CardHeader title="Available models" subtitle="Only engines with recorded model metadata are listed" />
            <DataTable
              rows={arrayFrom(state.data.models?.available)}
              rowKey={(row: any, index) => `${row.engine}-${row.model_id}-${index}`}
              empty={<EmptyState title="No model registry evidence" />}
              columns={[
                { key: "model_id", label: "Model" },
                { key: "model_version", label: "Version" },
                { key: "engine", label: "Engine" },
                { key: "state", label: "State", render: (row: any) => <StatusBadge value={row.state || "unknown"} /> },
                { key: "trained_at", label: "Trained", render: (row: any) => shortDate(row.trained_at) },
              ]}
            />
          </Card>

          <div className="split-grid">
            <Card>
              <CardHeader title="Research and portfolio" subtitle="Advisory records, not accounting changes" />
              <dl className="key-value-grid">
                <KeyValue label="Research runs">{state.data.research?.runs ?? "—"}</KeyValue>
                <KeyValue label="Portfolios">{arrayFrom(state.data.portfolio?.portfolios).length}</KeyValue>
                <KeyValue label="Latest optimization">{state.data.portfolio?.latest_optimization || "—"}</KeyValue>
                <KeyValue label="Experimental policies">{state.data.policies?.count ?? "—"}</KeyValue>
              </dl>
            </Card>
            <Card>
              <CardHeader title="Limitations" subtitle="The dashboard does not promote missing values to zero" />
              <ul className="plain-list">
                {arrayFrom(state.data.limitations).map((item: any) => <li key={String(item)}>{String(item)}</li>)}
              </ul>
            </Card>
          </div>
        </div>
      )}
    </Gate>
  );
}

/* ---------------------------------------------------------------- instrument */

function InstrumentView({
  filters,
  onInstrument,
  revision,
}: {
  filters: Filters;
  onInstrument: (value: string) => void;
  revision: number;
}) {
  const [input, setInput] = useState(filters.instrument_id);
  const [searchError, setSearchError] = useState("");
  useEffect(() => setInput(filters.instrument_id), [filters.instrument_id]);
  const id = filters.instrument_id;
  const openInstrument = () => {
    const value = input.trim().toUpperCase();
    if (!value) return;
    if (!MIC_SYMBOL.test(value)) {
      setSearchError("Enter a MIC-qualified symbol such as XNSE:ALPHA. Nothing was queried.");
      return;
    }
    setSearchError("");
    onInstrument(value);
  };
  const detail = useApi<any>(
    id ? `/market/intelligence/instruments/${encodeURIComponent(id)}` : null,
    { refreshKey: revision },
  );
  const horizons = useApi<any>(
    id ? `/market/intelligence/horizons/${encodeURIComponent(id)}` : null,
    { refreshKey: revision },
  );
  const research = useApi<any>(
    id ? `/market/intelligence/research?instrument_id=${encodeURIComponent(id)}&limit=5` : null,
    { refreshKey: revision },
  );
  const bars: CandleBar[] = useMemo(
    () =>
      arrayFrom(detail.data?.chart?.rows)
        .filter((bar: any) => [bar.open, bar.high, bar.low, bar.close].every((value) => Number.isFinite(Number(value))))
        .map((bar: any) => ({
          time: String(bar.time || ""),
          open: Number(bar.open),
          high: Number(bar.high),
          low: Number(bar.low),
          close: Number(bar.close),
          volume: Number.isFinite(Number(bar.volume)) ? Number(bar.volume) : null,
          vwap20: Number.isFinite(Number(bar.vwap20)) ? Number(bar.vwap20) : null,
          partial: Boolean(bar.partial),
        })),
    [detail.data],
  );
  const indicatorRows = useMemo(
    () =>
      Object.entries(detail.data?.chart?.indicators || {}).filter(
        ([, value]) => typeof value === "number" || typeof value === "string" || typeof value === "boolean",
      ),
    [detail.data],
  );

  return (
    <div className="stack">
      <Card>
        <div className="toolbar">
          <Field label="Instrument" hint="MIC-qualified symbol, e.g. XNSE:ALPHA">
            <TextInput value={input} onChange={(event) => { setInput(event.target.value.toUpperCase()); setSearchError(""); }} placeholder="XNSE:ALPHA" aria-label="Instrument symbol" />
          </Field>
          <Button variant="primary" icon={<Search size={15} />} onClick={openInstrument}>Open instrument</Button>
        </div>
      </Card>

      {searchError && <Banner tone="warn" title="Instrument not searched">{searchError}</Banner>}

      {!id ? (
        <EmptyState level={2} title="Select an instrument" description="Instrument analysis uses only stored prices, bars, regimes, forecasts and research already recorded by NovaMarket." />
      ) : (
        <Gate state={detail}>
          {detail.data && (
            <>
              <StatusLine body={detail.data} />
              <div className="split-grid">
                <Card>
                  <CardHeader
                    title={detail.data.instrument?.name || detail.data.instrument?.symbol || id}
                    subtitle={`${detail.data.instrument?.exchange || "—"} · ${detail.data.instrument?.sector || "sector unavailable"}`}
                    action={<Badge tone={detail.data.instrument?.delayed_or_historical ? "warn" : "good"}>{detail.data.instrument?.delayed_or_historical ? "Delayed / historical" : "Current stored quote"}</Badge>}
                  />
                  <div className="metric-grid compact-grid">
                    <Metric label="Last price" value={price(detail.data.instrument?.price)} help={detail.data.instrument?.currency || "INR"} />
                    <Metric label="Price timestamp" value={shortDate(detail.data.instrument?.price_at)} />
                    <Metric label="Freshness" value={title(detail.data.instrument?.data_freshness?.state || "unknown")} tone={metricTone(detail.data.instrument?.data_freshness?.state)} />
                    <Metric label="Regime" value={detail.data.regime?.primary || "uncertain"} />
                  </div>
                  <p className="muted">{detail.data.instrument?.price_basis || "Price provenance is not supplied; treat the quote as stale."}</p>
                </Card>
                <Card>
                  <CardHeader title="Research status" subtitle="Facts, interpretations and uncertainty remain separate" />
                  <dl className="key-value-grid">
                    <KeyValue label="Run">{detail.data.research?.run_id || "—"}</KeyValue>
                    <KeyValue label="Status">{title(detail.data.research?.status || "unavailable")}</KeyValue>
                    <KeyValue label="As of">{shortDate(detail.data.research?.as_of)}</KeyValue>
                  </dl>
                  <Button size="sm" variant="secondary" onClick={() => onInstrument(id)}>Refresh research</Button>
                </Card>
              </div>

              <Card>
                <CardHeader title="Price and volume" subtitle={detail.data.chart?.supports_ohlc ? "Stored OHLC bars with recorded volume" : "Bars are incomplete; no synthetic candles are drawn"} />
                <CandlestickChart bars={bars} zones={[]} levels={arrayFrom(detail.data.chart?.levels).map((level: any) => ({ ...level, value: Number(level.value) || 0 })).filter((level: any) => level.value)} />
                <div className="chip-row">
                  <Badge tone="neutral">{arrayFrom(detail.data.chart?.rows).length} stored bars</Badge>
                  <Badge tone="neutral">{detail.data.chart?.interval || "1d"} interval</Badge>
                  {arrayFrom(detail.data.chart?.news_events).length > 0 && <Badge tone="info">{arrayFrom(detail.data.chart?.news_events).length} event markers</Badge>}
                </div>
              </Card>

              <div className="split-grid">
                <Card>
                  <CardHeader title="Technical indicators" subtitle="Recorded indicator values; no indicator is recomputed in the browser" />
                  <dl className="key-value-grid">
                    {indicatorRows.map(([name, value]) => (
                      <KeyValue key={name} label={title(name)}>{String(value)}</KeyValue>
                    ))}
                  </dl>
                  {indicatorRows.length === 0 && <EmptyState title="No recorded indicators" />}
                </Card>
                <Card>
                  <CardHeader title="Regime and corporate annotations" subtitle="Only recorded annotations are shown" />
                  <pre className="code-block">{JSON.stringify(detail.data.chart?.regime_annotations || [], null, 2)}</pre>
                  <pre className="code-block">{JSON.stringify(detail.data.chart?.corporate_events || [], null, 2)}</pre>
                </Card>
              </div>

              <Card>
                <CardHeader title="Forecasts for this instrument" subtitle="Each row keeps its own model version, horizon and validity" />
                <ForecastTable rows={arrayFrom(detail.data.forecasts)} />
              </Card>

              <Gate state={horizons}>
                {horizons.data && (
                  <Card>
                    <CardHeader title="Separate horizon families" subtitle="Targets are not merged into one price" action={horizons.data.cross_horizon?.disagreement ? <Badge tone="warn">Horizons disagree</Badge> : null} />
                    <div className="split-grid">
                      {Object.entries(horizons.data.families || {}).map(([family, group]: [string, any]) => (
                        <div key={family} className="stack-tight">
                          <h3>{title(family)}</h3>
                          <div className="chip-row">
                            {arrayFrom(group.directions).map((direction: any) => <Chip key={String(direction)} active>{title(direction)}</Chip>)}
                          </div>
                          <ForecastTable rows={arrayFrom(group.forecasts)} compact />
                        </div>
                      ))}
                    </div>
                  </Card>
                )}
              </Gate>

              <Card>
                <CardHeader title="Regime annotations and events" subtitle="Only recorded markers are displayed" />
                <DataTable
                  rows={arrayFrom(detail.data.chart?.news_events)}
                  rowKey={(row: any, index) => row.event_id || String(index)}
                  empty={<EmptyState title="No event markers" description="The event radar recorded no event for this instrument." />}
                  columns={[
                    { key: "title", label: "Event", render: (row: any) => row.title || row.headline || row.summary || "—" },
                    { key: "published_at", label: "Published", render: (row: any) => shortDate(row.published_at || row.observed_at) },
                    { key: "source", label: "Source" },
                  ]}
                />
              </Card>

              <Gate state={research}>
                {research.data && (
                  <Card>
                    <CardHeader title="Latest research thesis" subtitle="Source provenance stays with every claim" />
                    <dl className="key-value-grid">
                      <KeyValue label="Bullish factors">{arrayFrom(research.data.thesis?.bullish).length}</KeyValue>
                      <KeyValue label="Bearish factors">{arrayFrom(research.data.thesis?.bearish).length}</KeyValue>
                      <KeyValue label="Evidence records">{arrayFrom(research.data.evidence).length}</KeyValue>
                    </dl>
                  </Card>
                )}
              </Gate>
            </>
          )}
        </Gate>
      )}
    </div>
  );
}

/* ----------------------------------------------------------------- forecasts */

function ForecastTable({ rows, compact = false }: { rows: any[]; compact?: boolean }) {
  const columns: Column<any>[] = [
    { key: "model_id", label: "Model", render: (row) => row.model_id || row.engine || "—" },
    { key: "model_version", label: "Version" },
    { key: "horizon", label: "Horizon" },
    { key: "reference_price", label: "Reference", align: "right", render: (row) => price(row.reference_price) },
    { key: "predicted_price", label: "Target", align: "right", render: (row) => price(row.predicted_price) },
    { key: "expected_return", label: "Expected", align: "right", render: (row) => pct(row.expected_return) },
    { key: "direction", label: "Direction", render: (row) => directionBadge(row.direction) },
    { key: "interval", label: "Prediction interval", align: "right", render: (row) => intervalText(row.interval_low, row.interval_high) },
    { key: "probability", label: "Calibrated probability", align: "right", render: (row) => row.probability === null || row.probability === undefined ? <span className="muted">Not calibrated</span> : pct(row.probability) },
    { key: "probabilities", label: "P(up / down / neutral)", align: "right", render: (row) => row.probabilities?.up === null || row.probabilities?.up === undefined ? <span className="muted">—</span> : `${pct(row.probabilities.up)} / ${pct(row.probabilities.down)} / ${pct(row.probabilities.neutral)}` },
    { key: "uncertainty", label: "Uncertainty", render: (row) => row.uncertainty?.class || row.uncertainty?.width || "—" },
    { key: "validity", label: "Validity", render: (row) => <StatusBadge value={row.validity} /> },
    { key: "target_at", label: "Target", render: (row) => shortDate(row.target_at) },
    { key: "data_freshness", label: "Freshness", render: (row) => row.data_freshness?.state || "—" },
    { key: "generated_at", label: "Generated", render: (row) => shortDate(row.generated_at) },
  ];
  return (
    <DataTable
      rows={rows}
      compact={compact}
      rowKey={(row, index) => row.forecast_id || `${row.model_id}-${row.horizon}-${index}`}
      empty={<EmptyState title="No stored forecasts" description="No model output matches this scope; the dashboard does not invent one." />}
      columns={compact ? [columns[0], columns[1], columns[2], columns[3], columns[4], columns[5], columns[6], columns[8]] : columns}
    />
  );
}

function ExplainPanel({ id, kind }: { id: string; kind: string }) {
  const state = useApi<any>(id ? `/market/intelligence/explain/${encodeURIComponent(id)}?kind=${kind}` : null);
  const attribution = useApi<any>(id && kind === "forecast" ? `/market/intelligence/attribution/${encodeURIComponent(id)}` : null);
  return (
    <Card>
      <CardHeader title="Decision explainability" subtitle="What, why, evidence, contradictions, limitations and aftermath" action={state.data ? <StatusBadge value={state.data.data_status} /> : null} />
      <Gate state={state} empty="No explanation is available for this record.">
        {state.data && (
          <div className="stack">
            <dl className="key-value-grid">
              <KeyValue label="Action">{title(state.data.what?.action || "—")}</KeyValue>
              <KeyValue label="Instrument">{state.data.what?.instrument_id || "—"}</KeyValue>
              <KeyValue label="Horizon">{state.data.what?.horizon || "—"}</KeyValue>
              <KeyValue label="Timestamp">{shortDate(state.data.what?.recommendation_timestamp)}</KeyValue>
            </dl>
            <div className="split-grid">
              <div className="stack-tight">
                <h3>Why</h3>
                <p>{state.data.why?.explanation || state.data.why?.recorded_explanation || "No recorded explanation was supplied."}</p>
                <p className="muted">{state.data.why?.explanation_method ? `Method: ${state.data.why.explanation_method}` : "No feature-attribution method is claimed."}</p>
              </div>
              <div className="stack-tight">
                <h3>Contradicted by</h3>
                <pre className="code-block">{JSON.stringify(state.data.contradicted_by || {}, null, 2)}</pre>
              </div>
            </div>
            <div className="split-grid">
              <div>
                <h3>Supporting evidence</h3>
                <EvidenceList rows={arrayFrom(state.data.supporting)} />
              </div>
              <div>
                <h3>Limitations</h3>
                <ul className="plain-list">{arrayFrom(state.data.limitations).map((item: any, index: number) => <li key={`${String(item)}-${index}`}>{String(item)}</li>)}</ul>
                <h3>Aftermath</h3>
                <pre className="code-block">{JSON.stringify(state.data.aftermath || {}, null, 2)}</pre>
              </div>
            </div>
            {kind === "forecast" && (
              <div>
                <h3>Feature attribution</h3>
                <Gate state={attribution} empty="No computed attribution is available.">
                  {attribution.data && (
                    <>
                      <Banner
                        tone={arrayFrom(attribution.data.contributions).length || arrayFrom(attribution.data.model_contributions).length ? "info" : "warn"}
                        title={arrayFrom(attribution.data.contributions).length || arrayFrom(attribution.data.model_contributions).length ? "Model-specific, not causal" : "Attribution unavailable"}
                      >
                        {arrayFrom(attribution.data.contributions).length
                          ? attribution.data.attribution_kind === "global_importance"
                            ? "Global validation importance, not a per-forecast local contribution. It is not causal proof."
                            : "Contributions were computed by the model that produced this forecast. They are not causal proof."
                          : arrayFrom(attribution.data.model_contributions).length
                            ? "These are ensemble constituent contributions, not feature-level attribution or causal proof."
                            : attribution.data.unavailable_reason || "This model did not return contributions."}
                      </Banner>
                      <dl className="key-value-grid">
                        <KeyValue label="Method">{attribution.data.method || "—"}</KeyValue>
                        <KeyValue label="Model version">{attribution.data.model_version || "—"}</KeyValue>
                        <KeyValue label="Feature set">{attribution.data.feature_set_version || "—"}</KeyValue>
                        <KeyValue label="Explained at">{shortDate(attribution.data.explanation_timestamp)}</KeyValue>
                      </dl>
                      <DataTable
                        rows={arrayFrom(attribution.data.contributions)}
                        rowKey={(row: any, index) => `${row.feature}-${index}`}
                        empty={<EmptyState title="No feature contributions" description={arrayFrom(attribution.data.model_contributions).length ? "This model reports constituent contributions instead." : undefined} />}
                        columns={[
                          { key: "feature", label: "Feature" },
                          { key: "contribution", label: "Contribution", align: "right", render: (row: any) => row.contribution == null ? "—" : num(row.contribution, 0).toFixed(4) },
                          { key: "direction", label: "Direction" },
                          { key: "method", label: "Method" },
                          { key: "kind", label: "Kind" },
                        ]}
                      />
                      {arrayFrom(attribution.data.model_contributions).length > 0 && (
                        <DataTable
                          rows={arrayFrom(attribution.data.model_contributions)}
                          rowKey={(row: any, index) => `${row.model_id || row.model || index}-${index}`}
                          columns={[
                            { key: "model_id", label: "Constituent", render: (row: any) => row.model_id || row.model || row.engine || "—" },
                            { key: "weight", label: "Weight", align: "right", render: (row: any) => row.weight === undefined ? "—" : pct(row.weight) },
                            { key: "expected_return", label: "Expected return", align: "right", render: (row: any) => row.expected_return === undefined ? "—" : pct(row.expected_return) },
                            { key: "direction", label: "Direction" },
                          ]}
                        />
                      )}
                    </>
                  )}
                </Gate>
              </div>
            )}
          </div>
        )}
      </Gate>
    </Card>
  );
}

function ForecastsView({ filters, revision }: { filters: Filters; revision: number }) {
  const state = useApi<any>(`/market/intelligence/forecasts?${queryFor(filters)}`, {
    refreshKey: revision,
  });
  const [selected, setSelected] = useState("");
  return (
    <div className="stack">
      <Gate state={state}>
        {state.data && (
          <>
            <StatusLine body={state.data} />
            <Card>
              <CardHeader title="Forecast rows" subtitle={state.data.probability_rule} action={<Badge tone="neutral">{state.data.total ?? "—"} in scope</Badge>} />
              <ForecastTable rows={arrayFrom(state.data.forecasts)} />
            </Card>
            <div className="toolbar">
              <Field label="Explain a forecast">
                <TextInput value={selected} onChange={(event) => setSelected(event.target.value.trim())} placeholder="Forecast ID" />
              </Field>
              <Button variant="secondary" onClick={() => setSelected(selected.trim())}>Load explanation</Button>
            </div>
            {selected && <ExplainPanel id={selected} kind="forecast" />}
          </>
        )}
      </Gate>
    </div>
  );
}

function CompareView({ filters, revision }: { filters: Filters; revision: number }) {
  const state = useApi<any>(
    `/market/intelligence/compare?${queryFor(filters, { limit: 100 })}`,
    { refreshKey: revision },
  );
  return (
    <Gate state={state}>
      {state.data && (
        <div className="stack">
          <StatusLine body={state.data} />
          <Banner tone="info" title="Scoped comparison">{state.data.comparison_rule}</Banner>
          {arrayFrom(state.data.comparisons).length === 0 ? (
            <EmptyState title="No comparable forecasts" description="Comparison needs the same instrument, horizon and recorded reference timestamp." />
          ) : (
            arrayFrom(state.data.comparisons).map((group: any) => (
              <Card key={group.key}>
                <CardHeader
                  title={`${group.instrument_id} · ${group.horizon}`}
                  subtitle={`Reference ${shortDate(group.reference_at)} → target ${shortDate(group.target_at)}`}
                  action={<Badge tone={group.comparable ? "info" : "warn"}>{group.comparable ? `${group.model_count} models` : "Not comparable"}</Badge>}
                />
                {!group.comparable && <Banner tone="warn" title="Reference mismatch">{group.reason}</Banner>}
                {group.disagreement && <Banner tone="warn" title="Models disagree">Directions differ across models at this same horizon.</Banner>}
                <ForecastTable rows={arrayFrom(group.models)} compact />
                <div className="split-grid">
                  <div className="stack-tight">
                    <h3>Prediction intervals</h3>
                    <ForecastRanges rows={arrayFrom(group.models)} />
                  </div>
                  <div className="stack-tight">
                    <h3>Expected return by model</h3>
                    {arrayFrom(group.models).map((row: any, index: number) => {
                      const value = Number(row.expected_return);
                      const scale = Math.max(
                        ...arrayFrom(group.models).map((item: any) => Math.abs(Number(item.expected_return) || 0)),
                        0.0001,
                      );
                      return Number.isFinite(value) ? (
                        <HBar
                          key={`${row.model_id || row.engine}-${index}`}
                          label={`${row.model_id || row.engine || "model"} · ${row.horizon}`}
                          value={value}
                          max={scale}
                          display={pct(value)}
                          color={value >= 0 ? "var(--green)" : "var(--danger)"}
                        />
                      ) : null;
                    })}
                  </div>
                </div>
                <p className="muted">{group.best_model_scope}</p>
              </Card>
            ))
          )}
        </div>
      )}
    </Gate>
  );
}

function HorizonsView({ filters, revision }: { filters: Filters; revision: number }) {
  const id = filters.instrument_id;
  const state = useApi<any>(
    id ? `/market/intelligence/horizons/${encodeURIComponent(id)}` : null,
    { refreshKey: revision },
  );
  if (!id) return <EmptyState level={2} title="Choose an instrument" description="Set an instrument filter to inspect intraday, swing and long-term forecasts separately." />;
  return (
    <Gate state={state}>
      {state.data && (
        <div className="stack">
          <StatusLine body={state.data} />
          {state.data.cross_horizon?.disagreement && <Banner tone="warn" title="Cross-horizon disagreement">Different horizons point in different directions. They are shown separately, never averaged into one target.</Banner>}
          {Object.entries(state.data.families || {}).map(([family, group]: [string, any]) => (
            <Card key={family}>
              <CardHeader title={title(family)} subtitle={`Horizons: ${arrayFrom(group.horizons).join(", ") || "none"}`} />
              <ForecastTable rows={arrayFrom(group.forecasts)} />
              <ForecastRanges rows={arrayFrom(group.forecasts)} />
            </Card>
          ))}
          {Object.keys(state.data.families || {}).length === 0 && <EmptyState title="No horizon forecasts" description="No stored intraday, swing or long-term forecast exists for this instrument." />}
        </div>
      )}
    </Gate>
  );
}

/* ---------------------------------------------------------------- lifecycle */

function LifecycleView({ filters, revision }: { filters: Filters; revision: number }) {
  const state = useApi<any>(
    `/market/intelligence/lifecycle?${queryFor(filters, { limit: 20 })}`,
    { refreshKey: revision },
  );
  return (
    <Gate state={state}>
      {state.data && (
        <div className="stack">
          <StatusLine body={state.data} />
          <Banner tone="info" title="Recorded checkpoints">{state.data.note}</Banner>
          {arrayFrom(state.data.cycles).length === 0 && <EmptyState title="No lifecycle cycles" description="No PI-09 cycle matches this scope." />}
          {arrayFrom(state.data.cycles).map((cycle: any) => (
            <Card key={cycle.cycle_id}>
              <CardHeader
                title={`${cycle.instrument_id || "Instrument"} · ${cycle.horizon || "horizon"}`}
                subtitle={`${cycle.checkpoint_schedule} · target ${shortDate(cycle.target_at)}`}
                action={<Badge tone="neutral">{cycle.cycle_id}</Badge>}
              />
              <DataTable
                rows={arrayFrom(cycle.checkpoints)}
                rowKey={(row: any, index) => `${row.checkpoint}-${index}`}
                empty={<EmptyState title="No checkpoints recorded" description="Missing checkpoints are not synthesized." />}
                columns={[
                  { key: "checkpoint", label: "Checkpoint" },
                  { key: "status", label: "Status", render: (row: any) => <StatusBadge value={row.status} /> },
                  { key: "planned_at", label: "Planned", render: (row: any) => shortDate(row.planned_at) },
                  { key: "generated_at", label: "Generated", render: (row: any) => shortDate(row.generated_at) },
                  { key: "late", label: "Late", render: (row: any) => row.late ? <Badge tone="warn">Late</Badge> : "No" },
                  { key: "predicted_direction", label: "Direction", render: (row: any) => directionBadge(row.predicted_direction) },
                  { key: "confirmation_result", label: "Confirmation" },
                ]}
              />
              {arrayFrom(cycle.missing_checkpoints).length > 0 && (
                <Banner tone="warn" title="Missing checkpoints">{arrayFrom(cycle.missing_checkpoints).length} checkpoint(s) were not recorded and are shown as missing.</Banner>
              )}
              <p className="muted">Immutable version: {cycle.immutable_version ? "recorded" : "not asserted"}</p>
            </Card>
          ))}
        </div>
      )}
    </Gate>
  );
}

/* --------------------------------------------------------------- performance */

function numericMetrics(metrics: any): Array<{ key: string; value: number }> {
  const result: Array<{ key: string; value: number }> = [];
  const walk = (value: any, prefix = "") => {
    if (!value || typeof value !== "object") return;
    Object.entries(value).forEach(([key, item]) => {
      const label = prefix ? `${prefix}.${key}` : key;
      if (typeof item === "number" && Number.isFinite(item)) result.push({ key: label, value: item });
      else if (item && typeof item === "object" && !Array.isArray(item) && prefix.split(".").length < 2) walk(item, label);
    });
  };
  walk(metrics);
  return result;
}

function PerformanceView({ filters, revision }: { filters: Filters; revision: number }) {
  const state = useApi<any>(
    `/market/intelligence/performance?limit=30${filters.instrument_id ? `&instrument_id=${encodeURIComponent(filters.instrument_id)}` : ""}${filters.horizon ? `&horizon=${filters.horizon}` : ""}${filters.evaluation_start ? `&evaluation_start=${encodeURIComponent(filters.evaluation_start)}` : ""}${filters.evaluation_end ? `&evaluation_end=${encodeURIComponent(filters.evaluation_end)}` : ""}`,
    { refreshKey: revision },
  );
  const evaluation = state.data?.evaluation;
  const metricRows = useMemo(() => numericMetrics(evaluation?.metrics || {}), [evaluation]);
  return (
    <Gate state={state}>
      {state.data && (
        <div className="stack">
          <StatusLine body={state.data} />
          {!evaluation?.statistically_valid && (
            <Banner tone="warn" title="Insufficient evidence">{evaluation?.invalid_reason || "No evaluation report has a usable sample."}</Banner>
          )}
          <div className="metric-grid">
            <Metric label="Sample size" value={evaluation?.sample_size ?? "—"} />
            <Metric label="Horizon" value={evaluation?.horizon || "—"} />
            <Metric label="Model" value={evaluation?.model_id || "—"} />
            <Metric label="Dataset" value={evaluation?.dataset_version || "—"} />
          </div>
          <Card>
            <CardHeader title="Evaluation metrics" subtitle={`${shortDate(evaluation?.period_start)} → ${shortDate(evaluation?.period_end)} · evaluated ${shortDate(evaluation?.evaluated_at)}`} />
            <DataTable
              rows={metricRows}
              rowKey={(row) => row.key}
              empty={<EmptyState title="No metrics" description="The evaluation report contains no computed numeric metrics." />}
              columns={[
                { key: "key", label: "Metric", render: (row) => row.key.replaceAll("_", " ") },
                { key: "value", label: "Value", align: "right", render: (row) => row.value.toFixed(4) },
              ]}
            />
          </Card>
          <div className="split-grid">
            <Card>
              <CardHeader title="Directional confusion matrix" subtitle="Recorded classifications only" />
              <pre className="code-block">{JSON.stringify(evaluation?.confusion_matrix || {}, null, 2)}</pre>
            </Card>
            <Card>
              <CardHeader title="Leakage audit" subtitle="Point-in-time controls reported by PI-11" />
              <pre className="code-block">{JSON.stringify(evaluation?.leakage_audit || {}, null, 2)}</pre>
            </Card>
          </div>
          {evaluation?.by_regime && arrayFrom(evaluation.by_regime).length > 0 && (
            <Card>
              <CardHeader title="Performance by regime" subtitle="A model is not universally best across regimes" />
              <pre className="code-block">{JSON.stringify(evaluation.by_regime, null, 2)}</pre>
            </Card>
          )}
          <div className="split-grid">
            <Card>
              <CardHeader title="By horizon and instrument" subtitle="Each slice keeps its own sample size" />
              <pre className="code-block">{JSON.stringify({ by_horizon: evaluation?.by_horizon || [], by_instrument: evaluation?.by_instrument || [] }, null, 2)}</pre>
            </Card>
            <Card>
              <CardHeader title="Over time and ensemble comparison" subtitle="Stored evaluation evidence; no universal ranking" />
              <pre className="code-block">{JSON.stringify({ over_time: evaluation?.over_time || [], ensemble_vs_individual: evaluation?.ensemble_vs_individual || {} }, null, 2)}</pre>
            </Card>
          </div>
          <Banner tone="info" title="Accuracy is not profitability">{state.data.separation?.trading_profitability}</Banner>
        </div>
      )}
    </Gate>
  );
}

/* ------------------------------------------------------------------- regimes */

function RegimesView({ filters, revision }: { filters: Filters; revision: number }) {
  const state = useApi<any>(
    `/market/intelligence/regimes?limit=100${filters.instrument_id ? `&instrument_id=${encodeURIComponent(filters.instrument_id)}` : ""}`,
    { refreshKey: revision },
  );
  return (
    <Gate state={state}>
      {state.data && (
        <div className="stack">
          <StatusLine body={state.data} />
          {state.data.insufficient_evidence && <Banner tone="warn" title="Regime unknown">The detector returned uncertain; no narrative regime is invented.</Banner>}
          <div className="metric-grid">
            <Metric label="Primary regime" value={title(state.data.current?.primary || "uncertain")} />
            <Metric label="Secondary" value={state.data.current?.secondary || "—"} />
            <Metric label="Detector score (not a probability)" value={state.data.current?.confidence_kind ? `${state.data.current.confidence ?? "—"} (${state.data.current.confidence_kind})` : "not reported"} />
            <Metric label="Detected" value={shortDate(state.data.current?.detected_at)} />
            <Metric label="Trend strength" value={state.data.trend_strength ?? "—"} />
          </div>
          <div className="split-grid">
            <Card>
              <CardHeader title="Transitions" subtitle="Derived from consecutive recorded regime observations" />
              <DataTable
                rows={arrayFrom(state.data.transitions)}
                rowKey={(row: any, index) => `${row.at}-${index}`}
                empty={<EmptyState title="No transitions recorded" />}
                columns={[
                  { key: "from", label: "From" },
                  { key: "to", label: "To" },
                  { key: "at", label: "At", render: (row: any) => shortDate(row.at) },
                  { key: "duration_hours", label: "Observed duration (h)", align: "right", render: (row: any) => row.duration_hours ?? "—" },
                  { key: "duration_source", label: "Duration basis" },
                ]}
              />
            </Card>
            <Card>
              <CardHeader title="Drift and suitability" subtitle="Advisory PI-16 outputs" />
              <pre className="code-block">{JSON.stringify({ drift: state.data.drift, degradation: state.data.degradation, suitability: state.data.suitability }, null, 2)}</pre>
            </Card>
          </div>
          <Card>
            <CardHeader title="Regime history" subtitle="Newest first" />
            <DataTable
              rows={arrayFrom(state.data.history)}
              rowKey={(row: any, index) => row.regime_id || String(index)}
              empty={<EmptyState title="No regime history" />}
              columns={[
                { key: "as_of", label: "As of", render: (row: any) => shortDate(row.as_of) },
                { key: "instrument_id", label: "Instrument" },
                { key: "primary_regime", label: "Primary" },
                { key: "resolution", label: "Resolution" },
                { key: "method_version", label: "Method" },
              ]}
            />
          </Card>
        </div>
      )}
    </Gate>
  );
}

/* ---------------------------------------------------------------- decisions */

function EvidenceList({ rows }: { rows: any[] }) {
  if (!rows.length) return <p className="muted">No supporting evidence records were supplied.</p>;
  return (
    <DataTable
      rows={rows}
      rowKey={(row: any, index) => row.evidence_id || `${row.source}-${index}`}
      columns={[
        { key: "claim", label: "Claim", render: (row: any) => row.claim || row.statement || row.summary || row.title || row.headline || "—" },
        { key: "source", label: "Source" },
        { key: "kind", label: "Class" },
        { key: "timestamp", label: "Recorded", render: (row: any) => shortDate(row.timestamp) },
      ]}
    />
  );
}

function DecisionsView({
  filters,
  onCycle,
  revision,
}: {
  filters: Filters;
  onCycle: (value: string) => void;
  revision: number;
}) {
  const state = useApi<any>(
    `/market/intelligence/decisions?limit=50${filters.instrument_id ? `&instrument_id=${encodeURIComponent(filters.instrument_id)}` : ""}${filters.decision_status ? `&status=${filters.decision_status}` : ""}${filters.agent_id ? `&agent_id=${encodeURIComponent(filters.agent_id)}` : ""}`,
    { refreshKey: revision },
  );
  const [selected, setSelected] = useState("");
  const detail = useApi<any>(
    selected ? `/market/intelligence/decisions/${encodeURIComponent(selected)}` : null,
    { refreshKey: revision },
  );
  const evidence = useApi<any>(
    selected ? `/market/intelligence/evidence/${encodeURIComponent(selected)}` : null,
    { refreshKey: revision },
  );
  return (
    <div className="stack">
      <Gate state={state}>
        {state.data && (
          <>
            <StatusLine body={state.data} />
            <Card>
              <CardHeader title="Decision cycles" subtitle="Select a cycle to inspect every agent contribution and evidence record" />
              <DataTable
                rows={arrayFrom(state.data.decisions)}
                rowKey={(row: any, index) => row.cycle_id || String(index)}
                onRowClick={(row: any) => { setSelected(row.cycle_id); onCycle(row.cycle_id); }}
                empty={<EmptyState title="No agent decisions" description="PI-17 cycles appear here once recorded." />}
                columns={[
                  { key: "instrument_id", label: "Instrument" },
                  { key: "horizon", label: "Horizon" },
                  { key: "action", label: "Action", render: (row: any) => directionBadge(row.action) },
                  { key: "confidence", label: "Decision confidence (not probability)", render: (row: any) => `${row.confidence ?? "—"} ${row.confidence_kind ? `(${row.confidence_kind})` : ""}` },
                  { key: "evidence_strength", label: "Evidence" },
                  { key: "status", label: "Status", render: (row: any) => <StatusBadge value={row.status || "unknown"} /> },
                  { key: "decided_at", label: "Decided", render: (row: any) => shortDate(row.decided_at) },
                ]}
              />
            </Card>
          </>
        )}
      </Gate>
      {selected && (
        <Gate state={detail}>
          {detail.data && (
            <div className="stack">
              <Card>
                <CardHeader title={`${detail.data.cycle?.instrument_id || "Decision"} · ${detail.data.cycle?.horizon || ""}`} subtitle={`Cycle ${detail.data.cycle?.cycle_id} · ${shortDate(detail.data.cycle?.decided_at)}`} action={directionBadge(detail.data.decision?.action)} />
                <div className="metric-grid">
                  <Metric label="Evidence strength" value={detail.data.decision?.evidence_strength || "—"} />
                  <Metric label="Decision confidence (not probability)" value={`${detail.data.decision?.confidence ?? "—"} ${detail.data.decision?.confidence_kind ? `(${detail.data.decision.confidence_kind})` : ""}`} />
                  <Metric label="Supporting agents" value={arrayFrom(detail.data.decision?.supporting_agents).length} />
                  <Metric label="Contradicting agents" value={arrayFrom(detail.data.decision?.contradicting_agents).length} />
                  <Metric label="Order submitted" value="No" help="NM-10 is the only execution authority" />
                </div>
                <p>{detail.data.decision?.explanation || "No recorded explanation was supplied."}</p>
                <Banner tone="info" title="Evidence independence">{detail.data.evidence_note}</Banner>
              </Card>
              <Card>
                <CardHeader title="Agent contributions" subtitle="Independent evidence, interpretations and missing data stay distinct" />
                <DataTable
                  rows={arrayFrom(detail.data.agents)}
                  rowKey={(row: any, index) => row.agent_id || String(index)}
                  columns={[
                    { key: "agent_id", label: "Agent" },
                    { key: "role", label: "Role" },
                    { key: "status", label: "Status", render: (row: any) => <StatusBadge value={row.status} /> },
                    { key: "action", label: "Action", render: (row: any) => directionBadge(row.action) },
                    { key: "findings", label: "Findings", render: (row: any) => `${arrayFrom(row.findings).length} finding(s)` },
                    { key: "uncertainties", label: "Uncertainty", render: (row: any) => arrayFrom(row.uncertainties).length || "—" },
                    { key: "missing", label: "Missing", render: (row: any) => arrayFrom(row.missing).length || "—" },
                  ]}
                />
              </Card>
              <Card>
                <CardHeader title="Agent findings and uncertainty" subtitle="Each specialist's detailed findings, contradictions and gaps" />
                <div className="stack-tight">
                  {arrayFrom(detail.data.agents).map((agent: any) => (
                    <details key={agent.agent_id} open={arrayFrom(detail.data.agents).length === 1}>
                      <summary>
                        <strong>{agent.agent_id}</strong> · {agent.role || "specialist"} · {agent.status || "status unavailable"}
                      </summary>
                      <div className="split-grid">
                        <div>
                          <h3>Findings</h3>
                          <ul className="plain-list">
                            {arrayFrom(agent.findings).map((item: any, index: number) => (
                              <li key={index}>{typeof item === "string" ? item : item.summary || item.claim || JSON.stringify(item)}</li>
                            ))}
                            {arrayFrom(agent.findings).length === 0 && <li>No finding recorded.</li>}
                          </ul>
                          <h3>Supporting</h3>
                          <EvidenceList rows={arrayFrom(agent.supporting)} />
                        </div>
                        <div>
                          <h3>Contradicting</h3>
                          <EvidenceList rows={arrayFrom(agent.contradicting)} />
                          <h3>Uncertainty and missing information</h3>
                          <pre className="code-block">{JSON.stringify({ uncertainties: agent.uncertainties || [], missing: agent.missing || [], source_references: agent.source_references || [] }, null, 2)}</pre>
                        </div>
                      </div>
                    </details>
                  ))}
                  {arrayFrom(detail.data.agents).length === 0 && <EmptyState title="No agent responses recorded" />}
                </div>
              </Card>
              <div className="split-grid">
                <Card><CardHeader title="Decision evidence" /><EvidenceList rows={arrayFrom(detail.data.evidence)} /></Card>
                <Card><CardHeader title="Conflicts" /><pre className="code-block">{JSON.stringify(detail.data.conflicts || [], null, 2)}</pre></Card>
              </div>
              <Gate state={evidence}>
                {evidence.data && (
                  <Card>
                    <CardHeader title="Evidence timeline" subtitle={evidence.data.note} />
                    <EvidenceList rows={arrayFrom(evidence.data.timeline)} />
                  </Card>
                )}
              </Gate>
              <ExplainPanel id={selected} kind="decision" />
            </div>
          )}
        </Gate>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ research */

function ResearchView({ filters, revision }: { filters: Filters; revision: number }) {
  const [runId, setRunId] = useState("");
  const path = `/market/intelligence/research?limit=30${filters.instrument_id ? `&instrument_id=${encodeURIComponent(filters.instrument_id)}` : ""}${runId ? `&run_id=${encodeURIComponent(runId)}` : ""}${filters.research_status ? `&research_status=${encodeURIComponent(filters.research_status)}` : ""}`;
  const state = useApi<any>(path, { refreshKey: revision });
  const data = state.data;
  return (
    <Gate state={state}>
      {data && (
        <div className="stack">
          <StatusLine body={data} />
          <div className="split-grid">
            <Card>
              <CardHeader title="Research runs" subtitle="Select a run for its thesis and provenance" />
              <DataTable
                rows={arrayFrom(data.runs)}
                rowKey={(row: any, index) => row.run_id || String(index)}
                onRowClick={(row: any) => setRunId(row.run_id)}
                empty={<EmptyState title="No research runs" description="PI-19 research appears here once recorded." />}
                columns={[
                  { key: "instrument_id", label: "Instrument" },
                  { key: "status", label: "Status", render: (row: any) => <StatusBadge value={row.status} /> },
                  { key: "as_of", label: "As of", render: (row: any) => shortDate(row.as_of) },
                  { key: "run_id", label: "Run" },
                ]}
              />
            </Card>
            <Card>
              <CardHeader title="Reported facts vs interpretation" subtitle="No AI estimate is presented as a reported financial fact" />
              <ul className="plain-list">
                {Object.entries(data.separation || {}).map(([key, value]) => <li key={key}><strong>{title(key)}:</strong> {String(value)}</li>)}
              </ul>
              <dl className="key-value-grid">
                <KeyValue label="Profile">{data.profile?.name || data.detail?.instrument_id || "—"}</KeyValue>
                <KeyValue label="Sector">{data.profile?.sector || "—"}</KeyValue>
                <KeyValue label="Evidence">{arrayFrom(data.evidence).length}</KeyValue>
              </dl>
            </Card>
          </div>
          {data.detail && Object.keys(data.detail).length > 0 ? (
            <>
              <Card>
                <CardHeader title="Company, financials and valuation" subtitle="Reported facts, derived estimates and AI interpretation remain separate" />
                <div className="split-grid">
                  <div>
                    <h3>Company profile</h3>
                    <dl className="key-value-grid">
                      <KeyValue label="Name">{data.company?.name || data.profile?.name || "—"}</KeyValue>
                      <KeyValue label="Exchange">{data.company?.exchange || "—"}</KeyValue>
                      <KeyValue label="Sector">{data.company?.sector || data.profile?.sector || "—"}</KeyValue>
                      <KeyValue label="As of">{shortDate(data.detail?.as_of)}</KeyValue>
                    </dl>
                    <pre className="code-block">{JSON.stringify(data.financials || {}, null, 2)}</pre>
                  </div>
                  <div>
                    <h3>Valuation</h3>
                    <pre className="code-block">{JSON.stringify(data.valuation || {}, null, 2)}</pre>
                    <h3>Sector and peers</h3>
                    <pre className="code-block">{JSON.stringify({ sector: data.sector || {}, peers: data.peers || [], macro: data.macro || [] }, null, 2)}</pre>
                  </div>
                </div>
              </Card>
              <div className="split-grid">
                <Card><CardHeader title="Bullish factors" /><FactorList rows={arrayFrom(data.thesis?.bullish)} /></Card>
                <Card><CardHeader title="Bearish factors" /><FactorList rows={arrayFrom(data.thesis?.bearish)} /></Card>
              </div>
              <div className="split-grid">
                <Card>
                  <CardHeader title="Corporate events" subtitle="Announcement and occurrence times stay distinct" />
                  <DataTable
                    rows={arrayFrom(data.events)}
                    rowKey={(row: any, index) => row.event_id || String(index)}
                    empty={<EmptyState title="No corporate events" />}
                    columns={[
                      { key: "title", label: "Event", render: (row: any) => row.title || row.summary || row.kind || "—" },
                      { key: "announced_at", label: "Announced", render: (row: any) => shortDate(row.announced_at || row.published_at) },
                      { key: "occurred_at", label: "Occurred", render: (row: any) => shortDate(row.occurred_at || row.effective_at) },
                      { key: "verification", label: "Verification" },
                    ]}
                  />
                </Card>
                <Card>
                  <CardHeader title="Relevant news" subtitle="Publication time and source are preserved" />
                  <DataTable
                    rows={arrayFrom(data.news)}
                    rowKey={(row: any, index) => row.article_id || String(index)}
                    empty={<EmptyState title="No stored news" />}
                    columns={[
                      { key: "headline", label: "Headline", render: (row: any) => row.headline || row.title || "—" },
                      { key: "published_at", label: "Published", render: (row: any) => shortDate(row.published_at) },
                      { key: "source", label: "Source" },
                      { key: "sentiment", label: "Sentiment" },
                    ]}
                  />
                </Card>
              </div>
              <Card><CardHeader title="Research uncertainty" /><pre className="code-block">{JSON.stringify(data.uncertainty || {}, null, 2)}</pre></Card>
              <Card><CardHeader title="Evidence provenance" /><EvidenceList rows={arrayFrom(data.evidence)} /></Card>
            </>
          ) : (
            <EmptyState title="Choose a research run" description="Financial facts, thesis factors, uncertainty and source evidence are displayed only for a recorded run." />
          )}
        </div>
      )}
    </Gate>
  );
}

/* ----------------------------------------------------------------- portfolio */

function PortfolioView({ filters, revision }: { filters: Filters; revision: number }) {
  const state = useApi<any>(
    `/market/intelligence/portfolio?limit=30${filters.portfolio_id ? `&portfolio_id=${encodeURIComponent(filters.portfolio_id)}` : ""}`,
    { refreshKey: revision },
  );
  const data = state.data;
  return (
    <Gate state={state}>
      {data && (
        <div className="stack">
          <StatusLine body={data} />
          <div className="metric-grid">
            <Metric label="Portfolio value" value={price(data.current_authoritative?.total_value_nc)} help={data.current_authoritative?.currency || "NC"} />
            <Metric label="Available cash" value={price(data.current_authoritative?.cash_nc)} help="Authoritative snapshot" />
            <Metric label="INR equivalent" value={price(data.current_authoritative?.inr_equivalent)} help={data.current_authoritative?.inr_conversion?.note} />
            <Metric label="Stale positions" value={data.current_authoritative?.stale_prices ?? "—"} tone={num(data.current_authoritative?.stale_prices, 0) > 0 ? "warn" : "neutral"} />
          </div>
          <Banner tone="warn" title="Authoritative vs advisory">
            The portfolio snapshot is read-only. Optimization allocation and rebalancing below are experimental recommendations and never change positions or the ledger.
          </Banner>
          <div className="split-grid">
            <Card>
              <CardHeader title="Current holdings" subtitle="Authoritative NM-07 state" />
              <DataTable
                rows={arrayFrom(data.current_authoritative?.holdings)}
                rowKey={(row: any, index) => row.position_id || row.instrument_id || String(index)}
                empty={<EmptyState title="No holdings" />}
                columns={[
                  { key: "instrument_id", label: "Instrument" },
                  { key: "quantity", label: "Qty", align: "right" },
                  { key: "average_price", label: "Avg", align: "right", render: (row: any) => price(row.average_price) },
                  { key: "market_price", label: "Market", align: "right", render: (row: any) => price(row.market_price) },
                  { key: "weight", label: "Weight" },
                  { key: "stale", label: "Price", render: (row: any) => row.stale ? <Badge tone="warn">Stale</Badge> : "Current" },
                ]}
              />
              <h3>Current allocation</h3>
              <DataTable
                rows={arrayFrom(data.current_authoritative?.current_allocation)}
                rowKey={(row: any, index) => row.instrument_id || String(index)}
                empty={<EmptyState title="Position weights unavailable" description="No recorded position weight was returned; the dashboard does not derive one." />}
                columns={[
                  { key: "instrument_id", label: "Instrument" },
                  { key: "weight", label: "Weight", align: "right", render: (row: any) => pct(row.weight) },
                ]}
              />
            </Card>
            <Card>
              <CardHeader title="Optimization recommendation" subtitle={data.recommendation?.run_id || "No completed run"} action={<Badge tone="warn">Experimental</Badge>} />
              <dl className="key-value-grid">
                <KeyValue label="Status">{title(data.recommendation?.status || "unavailable")}</KeyValue>
                <KeyValue label="Advisory only">Yes</KeyValue>
                <KeyValue label="Changes portfolio">No</KeyValue>
              </dl>
              <h3>Recommended allocation</h3>
              <pre className="code-block">{JSON.stringify(data.recommendation?.allocation || [], null, 2)}</pre>
              <h3>Sector exposure, diversification and risk</h3>
              <pre className="code-block">{JSON.stringify({ sector_exposure: data.recommendation?.sector_exposure || [], diversification: data.recommendation?.diversification || {}, risk: data.recommendation?.risk || {} }, null, 2)}</pre>
            </Card>
          </div>
          <Card>
            <CardHeader title="Rebalancing proposals" subtitle="Proposals require the existing authorized workflow; this view cannot apply them" />
            <pre className="code-block">{JSON.stringify(data.recommendation?.rebalancing || [], null, 2)}</pre>
          </Card>
        </div>
      )}
    </Gate>
  );
}

/* ------------------------------------------------------------------ policies */

function PoliciesView({ revision }: { revision: number }) {
  const state = useApi<any>("/market/intelligence/policies?limit=50", {
    refreshKey: revision,
  });
  return (
    <Gate state={state}>
      {state.data && (
        <div className="stack">
          <StatusLine body={state.data} />
          <Banner tone="warn" title="Experimental, advisory-only">{state.data.labels?.training_reward} · activation is not available from this dashboard.</Banner>
          <Card>
            <CardHeader title="RL policies" subtitle="Policy versions, training state and evaluation evidence" />
            <DataTable
              rows={arrayFrom(state.data.policies)}
              rowKey={(row: any, index) => row.policy_id || String(index)}
              empty={<EmptyState title="No experimental policies" />}
              columns={[
                { key: "policy_id", label: "Policy" },
                { key: "algorithm", label: "Algorithm" },
                { key: "policy_version", label: "Version" },
                { key: "status", label: "Status", render: (row: any) => <StatusBadge value={row.status} /> },
                { key: "training_reward", label: "Training reward", align: "right", render: (row: any) => cell(row.training_reward) },
                { key: "validation", label: "Validation", render: (row: any) => cell(row.validation ?? row.validation_status) },
                { key: "out_of_sample", label: "Out of sample", render: (row: any) => cell(row.out_of_sample_metrics && Object.keys(row.out_of_sample_metrics).length ? row.out_of_sample_metrics : row.out_of_sample) },
                { key: "suitability", label: "Regime suitability", render: (row: any) => cell(row.regime_suitability) },
                { key: "drift", label: "Drift", render: (row: any) => cell(row.drift ?? row.drift_status) },
                { key: "experimental", label: "Scope", render: () => <Badge tone="warn">Experimental</Badge> },
              ]}
            />
          </Card>
          <div className="split-grid">
            <Card><CardHeader title="Degradation" subtitle="Drift and policy-degradation evidence" /><pre className="code-block">{JSON.stringify(state.data.degradation || [], null, 2)}</pre></Card>
            <Card><CardHeader title="Retraining recommendations" subtitle="Advisory only" /><pre className="code-block">{JSON.stringify(state.data.retraining || [], null, 2)}</pre></Card>
          </div>
          <div className="split-grid">
            <Card><CardHeader title="Training runs" subtitle="Recorded training status and validation evidence" /><pre className="code-block">{JSON.stringify(state.data.runs || [], null, 2)}</pre></Card>
            <Card><CardHeader title="Reward evaluations" subtitle="Optimization signal, never portfolio profit" /><pre className="code-block">{JSON.stringify(state.data.reward_evaluations || [], null, 2)}</pre></Card>
          </div>
        </div>
      )}
    </Gate>
  );
}

/* -------------------------------------------------------------------- replay */

function ReplayView({ filters, cycle, revision }: { filters: Filters; cycle: string; revision: number }) {
  const [cycleId, setCycleId] = useState(cycle);
  useEffect(() => setCycleId(cycle), [cycle]);
  const state = useApi<any>(
    cycleId ? `/market/intelligence/replay/${encodeURIComponent(cycleId)}` : null,
    { refreshKey: revision },
  );
  const data = state.data;
  return (
    <div className="stack">
      <Card>
        <div className="toolbar">
          <Field label="Decision cycle">
            <TextInput value={cycleId} onChange={(event) => setCycleId(event.target.value.trim())} placeholder="Decision cycle ID" />
          </Field>
          <Button variant="secondary" icon={<History size={15} />} onClick={() => setCycleId(cycleId.trim())}>Reconstruct</Button>
          <Badge tone="neutral">{filters.instrument_id || "all instruments"}</Badge>
        </div>
      </Card>
      {!cycleId ? (
        <EmptyState level={2} title="Choose a past decision" description="Replay reconstructs what NovaMarket knew at that decision timestamp and excludes later forecasts, evidence, news, research and regime observations." />
      ) : (
        <Gate state={state}>
          {data && (
            <>
              <StatusLine body={data} />
              <div className="metric-grid">
                <Metric label="Known at" value={shortDate(data.known_at)} />
                <Metric label="Future forecasts excluded" value={data.point_in_time?.future_forecasts_excluded ?? "—"} tone="warn" />
                <Metric label="Future evidence excluded" value={data.point_in_time?.future_evidence_excluded ?? "—"} tone="warn" />
                <Metric label="Future news excluded" value={data.point_in_time?.future_news_excluded ?? "—"} tone="warn" />
                <Metric label="Future research / regime excluded" value={`${data.point_in_time?.future_research_excluded ?? "—"} / ${data.point_in_time?.future_regime_excluded ?? "—"}`} tone="warn" />
                <Metric label="Look-ahead enforced" value={data.point_in_time?.look_ahead_enforced ? "Yes" : "No"} />
              </div>
              <Banner tone="info" title="Recorded history vs reconstruction">{data.separation}</Banner>
              <div className="split-grid">
                <Card>
                  <CardHeader title="Recorded decision" subtitle="Original operation history, unchanged" action={directionBadge(data.recorded_history?.decision?.action)} />
                  <p>{data.recorded_history?.decision?.explanation || "No explanation recorded."}</p>
                  <dl className="key-value-grid">
                    <KeyValue label="Risk restrictions">{arrayFrom(data.recorded_history?.risk_restrictions).length}</KeyValue>
                    <KeyValue label="Portfolio context">{Object.keys(data.recorded_history?.portfolio_context || {}).length ? "Recorded" : "—"}</KeyValue>
                    <KeyValue label="Execution status">{data.recorded_history?.execution_status || data.recorded_history?.decision?.execution_status || data.recorded_history?.execution_outcome?.status || "Not asserted"}</KeyValue>
                    <KeyValue label="Execution outcome">{Object.keys(data.recorded_history?.execution_outcome || {}).length ? "Recorded" : "Not asserted"}</KeyValue>
                  </dl>
                </Card>
                <Card>
                  <CardHeader title="Known research" subtitle="Nothing published after the cutoff is shown" />
                  {data.reconstructed_at_cutoff?.research ? <pre className="code-block">{JSON.stringify(data.reconstructed_at_cutoff.research, null, 2)}</pre> : <p className="muted">No research was available at the cutoff.</p>}
                </Card>
              </div>
              <div className="split-grid">
                <Card>
                  <CardHeader title="Market regime known then" subtitle="Latest recorded regime at or before the cutoff" />
                  <pre className="code-block">{JSON.stringify(data.reconstructed_at_cutoff?.regime || {}, null, 2)}</pre>
                </Card>
                <Card>
                  <CardHeader title="Recorded execution outcome" subtitle="Only a recorded outcome is shown; NM-10 owns execution" />
                  <pre className="code-block">{JSON.stringify(data.recorded_history?.execution_outcome || {}, null, 2)}</pre>
                </Card>
              </div>
              <Card>
                <CardHeader title="Market observation at cutoff" subtitle="Stored bars only" />
                <LineChart data={arrayFrom(data.reconstructed_at_cutoff?.market_observations).map((bar: any) => ({ label: String(bar.time || "").slice(0, 10), value: Number(bar.close) })).filter((point: any) => Number.isFinite(point.value))} empty="No stored market bars before this cutoff" />
              </Card>
              <Card>
                <CardHeader title="Forecasts known then" subtitle="Only forecasts generated at or before the decision time" />
                <ForecastTable rows={arrayFrom(data.reconstructed_at_cutoff?.forecasts_available)} compact />
              </Card>
              <div className="split-grid">
                <Card><CardHeader title="Evidence known then" /><EvidenceList rows={arrayFrom(data.recorded_history?.evidence)} /></Card>
                <Card><CardHeader title="News known then" /><EvidenceList rows={arrayFrom(data.reconstructed_at_cutoff?.news_events)} /></Card>
              </div>
            </>
          )}
        </Gate>
      )}
    </div>
  );
}

/* --------------------------------------------------------------------- page */

export function PredictionIntelligencePage() {
  useDocumentTitle("Prediction Intelligence");
  const toast = useToast();
  const [view, setView] = useState<string>(() => {
    try {
      const saved = window.localStorage.getItem("novamarket.predictionIntelView");
      return VIEWS.some((item) => item.id === saved) ? String(saved) : "overview";
    } catch {
      return "overview";
    }
  });
  const [filters, setFilters] = useState<Filters>(EMPTY_FILTERS);
  const [instrumentDraft, setInstrumentDraft] = useState("");
  const [cycle, setCycle] = useState("");
  const [saving, setSaving] = useState(false);
  const revision = useDashboardEvents();
  const filtersTouched = useRef(false);

  useEffect(() => {
    let active = true;
    void apiGet<any>("/market/intelligence/filters", { name: "default" })
      .then((body) => {
        if (!active || filtersTouched.current || !body?.saved) return;
        setFilters({ ...EMPTY_FILTERS, ...(body.filters || {}) });
      })
      .catch(() => {
        // A missing preference store must not prevent the dashboard from loading.
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    try {
      window.localStorage.setItem("novamarket.predictionIntelView", view);
    } catch {
      // Storage can be unavailable in hardened browsers.
    }
  }, [view]);

  useEffect(() => setInstrumentDraft(filters.instrument_id), [filters.instrument_id]);

  const update = (key: keyof Filters, value: string) => {
    filtersTouched.current = true;
    setFilters((current) => ({ ...current, [key]: value }));
  };

  const updateInstrument = (value: string) => {
    // Keep a half-typed or malformed symbol out of the shared filter: the API
    // answers 422 and every view would fail on a filter the user did not mean.
    setInstrumentDraft(value);
    if (!value || MIC_SYMBOL.test(value)) update("instrument_id", value);
  };

  const saveFilters = async () => {
    setSaving(true);
    try {
      await apiPut("/market/intelligence/filters", { name: "default", filters });
      toast.push("Dashboard filters saved for this workspace.", "success");
    } catch (error: any) {
      toast.push(error?.detail || "Could not save dashboard filters.", "error");
    } finally {
      setSaving(false);
    }
  };

  const activeView = VIEWS.find((item) => item.id === view) || VIEWS[0];
  const moveTab = (event: React.KeyboardEvent<HTMLButtonElement>, index: number) => {
    const keys = ["ArrowLeft", "ArrowRight", "Home", "End"];
    if (!keys.includes(event.key)) return;
    event.preventDefault();
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? VIEWS.length - 1
          : (index + (event.key === "ArrowRight" ? 1 : -1) + VIEWS.length) % VIEWS.length;
    setView(VIEWS[next].id);
    requestAnimationFrame(() => document.getElementById(`prediction-intel-tab-${VIEWS[next].id}`)?.focus());
  };
  return (
    <div className="page">
      <PageHeader
        eyebrow="PI-20 · read-only intelligence desk"
        title="Prediction Intelligence"
        description="Forecasts, model evidence, regimes, agent decisions, research and portfolio intelligence in one inspectable workspace."
        actions={
          <Button variant="secondary" icon={<RefreshCw size={15} />} onClick={() => window.location.reload()}>
            Reload
          </Button>
        }
        meta={
          <>
            <Badge tone="info" dot>Read-only</Badge>
            <Badge tone="neutral">NM-08 risk authority</Badge>
            <Badge tone="neutral">NM-10 paper execution</Badge>
          </>
        }
      />

      <Card className="filter-card">
        <div className="toolbar filter-toolbar">
          <Field
            label="Instrument"
            hint={instrumentDraft && !MIC_SYMBOL.test(instrumentDraft) ? "MIC-qualified symbol required, e.g. XNSE:ALPHA" : "MIC-qualified symbol, e.g. XNSE:ALPHA"}
          >
            <TextInput value={instrumentDraft} onChange={(event) => updateInstrument(event.target.value.toUpperCase())} placeholder="XNSE:ALPHA" />
          </Field>
          <Field label="Horizon">
            <SelectInput value={filters.horizon} onChange={(event) => update("horizon", event.target.value)}>
              {HORIZONS.map((value) => <option key={value || "all"} value={value}>{value || "All horizons"}</option>)}
            </SelectInput>
          </Field>
          <Field label="Model"><TextInput value={filters.model_id} onChange={(event) => update("model_id", event.target.value)} placeholder="Any model" /></Field>
          <Field label="Direction">
            <SelectInput value={filters.direction} onChange={(event) => update("direction", event.target.value)}>
              {DIRECTIONS.map((value) => <option key={value || "all"} value={value}>{value || "Any direction"}</option>)}
            </SelectInput>
          </Field>
          <Field label="Validity">
            <SelectInput value={filters.validity} onChange={(event) => update("validity", event.target.value)}>
              {VALIDITY.map((value) => <option key={value || "all"} value={value}>{value || "Any validity"}</option>)}
            </SelectInput>
          </Field>
          <Field label="Decision status">
            <SelectInput value={filters.decision_status} onChange={(event) => update("decision_status", event.target.value)}>
              {DECISION_STATUS.map((value) => <option key={value || "all"} value={value}>{value || "Any status"}</option>)}
            </SelectInput>
          </Field>
          <div className="toolbar-end">
            <Button variant="secondary" icon={<Save size={15} />} loading={saving} onClick={() => void saveFilters()}>Save filters</Button>
          </div>
        </div>
        <details className="filter-drawer">
          <summary>More filters</summary>
          <div className="toolbar filter-toolbar">
            <Field label="Exchange"><TextInput value={filters.exchange} onChange={(event) => update("exchange", event.target.value.toUpperCase())} placeholder="XNSE" /></Field>
            <Field label="Sector"><TextInput value={filters.sector} onChange={(event) => update("sector", event.target.value)} placeholder="Any sector" /></Field>
            <Field label="Regime"><TextInput value={filters.regime} onChange={(event) => update("regime", event.target.value)} placeholder="Any regime" /></Field>
            <Field label="Evaluation start"><TextInput type="date" value={filters.evaluation_start} onChange={(event) => update("evaluation_start", event.target.value)} /></Field>
            <Field label="Evaluation end"><TextInput type="date" value={filters.evaluation_end} onChange={(event) => update("evaluation_end", event.target.value)} /></Field>
            <Field label="Agent"><TextInput value={filters.agent_id} onChange={(event) => update("agent_id", event.target.value)} placeholder="Any agent" /></Field>
            <Field label="Portfolio"><TextInput value={filters.portfolio_id} onChange={(event) => update("portfolio_id", event.target.value)} placeholder="Any portfolio" /></Field>
            <Field label="Research status"><TextInput value={filters.research_status} onChange={(event) => update("research_status", event.target.value)} placeholder="Any status" /></Field>
          </div>
        </details>
      </Card>

      <div className="tabs" role="tablist" aria-label="Prediction intelligence views">
        {VIEWS.map((item, index) => (
          <button
            key={item.id}
            type="button"
            role="tab"
            id={`prediction-intel-tab-${item.id}`}
            aria-controls="prediction-intel-panel"
            aria-selected={view === item.id}
            tabIndex={view === item.id ? 0 : -1}
            className={`tab ${view === item.id ? "tab-active" : ""}`}
            onClick={() => setView(item.id)}
            onKeyDown={(event) => moveTab(event, index)}
          >
            {item.label}
          </button>
        ))}
      </div>

      <div className="stack" role="tabpanel" id="prediction-intel-panel" aria-labelledby={`prediction-intel-tab-${activeView.id}`}>
        {view === "overview" && <OverviewView filters={filters} revision={revision} />}
        {view === "instrument" && <InstrumentView filters={filters} onInstrument={(value) => update("instrument_id", value)} revision={revision} />}
        {view === "forecasts" && <ForecastsView filters={filters} revision={revision} />}
        {view === "compare" && <CompareView filters={filters} revision={revision} />}
        {view === "horizons" && <HorizonsView filters={filters} revision={revision} />}
        {view === "lifecycle" && <LifecycleView filters={filters} revision={revision} />}
        {view === "performance" && <PerformanceView filters={filters} revision={revision} />}
        {view === "regimes" && <RegimesView filters={filters} revision={revision} />}
        {view === "decisions" && <DecisionsView filters={filters} onCycle={setCycle} revision={revision} />}
        {view === "research" && <ResearchView filters={filters} revision={revision} />}
        {view === "portfolio" && <PortfolioView filters={filters} revision={revision} />}
        {view === "policies" && <PoliciesView revision={revision} />}
        {view === "replay" && <ReplayView filters={filters} cycle={cycle} revision={revision} />}
      </div>
    </div>
  );
}
