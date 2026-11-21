import { useEffect, useState } from "react";
import {
  Activity,
  BadgeCheck,
  CalendarClock,
  GitCompareArrows,
  Layers3,
  RefreshCw,
  ShieldQuestion,
  Sparkles,
} from "lucide-react";
import { apiPost, LOCAL_ADMIN_PROXY } from "../api";
import { useApi, useDocumentTitle } from "../hooks";
import { arrayFrom, dateTime, num, title } from "../lib";
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  DataTable,
  EmptyState,
  ErrorBlock,
  Field,
  KeyValue,
  LoadingBlock,
  Metric,
  PageHeader,
  SelectInput,
  TextInput,
  useToast,
  type Column,
} from "../components/ui";

const BASE = "/market/event-intelligence";

const VIEWS = [
  { id: "overview", label: "Overview", icon: Sparkles },
  { id: "timeline", label: "Timeline", icon: CalendarClock },
  { id: "relevance", label: "Relevance", icon: GitCompareArrows },
  { id: "impact", label: "Impact", icon: Activity },
  { id: "warnings", label: "Warnings", icon: ShieldQuestion },
  { id: "features", label: "Features", icon: Layers3 },
  { id: "outcomes", label: "Outcomes", icon: BadgeCheck },
  { id: "agents", label: "Agent evidence", icon: RefreshCw },
] as const;

type View = (typeof VIEWS)[number]["id"];

const HORIZONS = ["5m", "15m", "20m", "1h", "1d", "3d", "5d", "30d", "90d"] as const;

function tone(value: unknown): "good" | "warn" | "bad" | "neutral" {
  const level = String(value || "").toLowerCase();
  if (["verified", "direct", "normal", "suitable", "recorded"].includes(level)) return "good";
  if (
    [
      "unverified",
      "event_approaching",
      "conditionally_suitable",
      "market_wide",
      "sector_related",
      "single_source",
      "watch",
    ].includes(level)
  )
    return "warn";
  if (
    ["conflicted", "event_uncertainty", "event_volatility_risk", "event_volatility_risk_elevated", "expired"].includes(
      level,
    )
  )
    return "bad";
  if (["insufficient_evidence", "not_relevant", "insufficient_data", "unavailable"].includes(level)) return "warn";
  return "neutral";
}

function metricValue(value: unknown, digits = 4): string {
  // num()'s second argument is a FALLBACK, not a precision: passing 4 here once
  // rendered a missing feature value as a confident "4".
  if (value === null || value === undefined || value === "") return "—";
  const number = num(value, Number.NaN);
  if (!Number.isFinite(number)) return "—";
  return Number.isInteger(number) ? String(number) : number.toFixed(digits);
}

function Table({ rows, columns, empty }: { rows: any[]; columns: Column<any>[]; empty: string }) {
  return (
    <DataTable
      rows={rows}
      columns={columns}
      rowKey={(row, index) =>
        String(row.event_id || row.outcome_id || row.warning_id || row.name || index)
      }
      empty={<EmptyState title="No evidence" description={empty} />}
    />
  );
}

export function EventIntelligencePage() {
  useDocumentTitle("Event Intelligence");
  const toast = useToast();
  const [view, setView] = useState<View>("overview");
  const [instrument, setInstrument] = useState("");
  const [horizon, setHorizon] = useState("5d");
  const [eventId, setEventId] = useState("");
  const [operatorToken, setOperatorToken] = useState("");
  const [busy, setBusy] = useState("");
  const [revision, setRevision] = useState(0);
  const [result, setResult] = useState<any>(null);
  const [failure, setFailure] = useState<{ action: string; message: string } | null>(null);

  const status = useApi<any>(`${BASE}/status`, { refreshMs: 30_000, refreshKey: revision });
  const overview = useApi<any>(
    `${BASE}/overview?instrument_id=${encodeURIComponent(instrument.trim())}&horizon=${horizon}`,
    { refreshKey: revision },
  );
  const timeline = useApi<any>(
    view === "timeline"
      ? `${BASE}/timeline?instrument_id=${encodeURIComponent(instrument.trim())}&horizon=${horizon}`
      : null,
    { refreshKey: revision },
  );
  // A stored result belongs to the instrument and horizon that produced it.
  useEffect(() => {
    setResult(null);
    setFailure(null);
  }, [instrument, horizon]);

  const state = status.data || {};
  const latest = state.latest || {};
  const events = arrayFrom(overview.data, "events");
  const timelineRows = arrayFrom(timeline.data, "timeline");

  async function run(
    action: string,
    path: string,
    body: Record<string, unknown> = {},
    options: { requiresEvent?: boolean; readOnly?: boolean } = {},
  ) {
    if (!instrument.trim()) {
      const message = "Enter a canonical instrument id first, for example XNSE:RELIANCE.";
      toast.push(message, "error");
      setFailure({ action, message });
      return;
    }
    if (options.requiresEvent && !eventId.trim()) {
      const message = "Enter the event id this action reads before running it.";
      toast.push(message, "error");
      setFailure({ action, message });
      return;
    }
    setBusy(action);
    setFailure(null);
    try {
      const response = await apiPost(
        `${BASE}/${path}`,
        { instrument_id: instrument.trim(), horizon, ...body },
        undefined,
        { operatorToken: operatorToken.trim() },
      );
      setResult(response);
      toast.push(
        `${title(action)} ${options.readOnly ? "read" : "recorded"}. No forecast, model, risk decision or order was changed.`,
        "success",
      );
      setRevision((value) => value + 1);
    } catch (error: any) {
      const message = String(
        error?.detail?.message || error?.detail || error?.message || error,
      );
      toast.push(`${title(action)} refused: ${message}`, "error");
      setFailure({ action, message });
    } finally {
      setBusy("");
    }
  }

  function eventFields() {
    return (
      <div className="form-grid">
        <Field label="Event ID">
          <TextInput
            value={eventId}
            onChange={(event) => setEventId(event.target.value)}
            placeholder="evt_…"
          />
        </Field>
        <Field label="Operator token" hint={LOCAL_ADMIN_PROXY ? "The local admin proxy already authorizes this session." : undefined}>
          <TextInput
            value={operatorToken}
            onChange={(event) => setOperatorToken(event.target.value)}
            placeholder={LOCAL_ADMIN_PROXY ? "Local admin session active" : "Required for evidence-writing actions"}
            type="password"
          />
        </Field>
      </div>
    );
  }

  const failureBanner = failure ? (
    <Banner tone="bad" title={`${title(failure.action)} refused`}>
      {failure.message}
    </Banner>
  ) : null;

  function renderView() {
    if (view === "overview") {
      if (overview.loading) return <LoadingBlock label="Reading verified event evidence" />;
      if (overview.error) return <ErrorBlock message={overview.error.message} onRetry={overview.refresh} />;
      const warning = overview.data?.warning;
      return (
        <div className="stack">
          <div className="metric-grid">
            <Metric label="Events" value={overview.data?.total ?? 0} />
            <Metric label="Verified" value={overview.data?.verified ?? 0} />
            <Metric label="Unverified" value={overview.data?.unverified ?? 0} />
            <Metric label="Conflicted" value={overview.data?.conflicted ?? 0} />
          </div>
          <Banner tone="info" title="Advisory event evidence">
            PI-24 reads the existing NM-04 event radar and PI-19 research. It never converts sentiment into a
            direction, changes a forecast, promotes a model or creates a risk or execution decision.
          </Banner>
          {warning && (
            <Card>
              <CardHeader title="Current event warning" subtitle={warning.warning_id} />
              <dl className="key-value-list">
                <KeyValue label="Status">
                  <Badge tone={tone(warning.status)}>{title(warning.status)}</Badge>
                </KeyValue>
                <KeyValue label="Relevant events">{warning.relevant_events}</KeyValue>
                <KeyValue label="Direction">Not estimated</KeyValue>
                <KeyValue label="Risk authority">{warning.risk_authority}</KeyValue>
              </dl>
            </Card>
          )}
          <Card>
            <CardHeader
              title="Events"
              subtitle={
                (overview.data?.total ?? 0) > events.length
                  ? `Showing the ${events.length} most recent of ${overview.data?.total ?? 0} events — publication time and event date are separate fields.`
                  : `${events.length} event${events.length === 1 ? "" : "s"} — publication time and event date are separate fields.`
              }
            />
            <Table
              rows={events}
              columns={[
                { key: "title", label: "Event", render: (row) => <strong>{row.title || row.event_id}</strong> },
                { key: "group", label: "Group", render: (row) => title(row.event_group) },
                { key: "verification", label: "Verification", render: (row) => <Badge tone={tone(row.status)}>{title(row.status)}</Badge> },
                { key: "importance", label: "Importance", render: (row) => title(row.importance) },
                { key: "published", label: "Published", render: (row) => dateTime(row.published_at) },
                { key: "event_date", label: "Event date", render: (row) => dateTime(row.event_date) },
              ]}
              empty="No normalized events are available for this scope."
            />
          </Card>
          <Card>
            <CardHeader title="Stored analyses" subtitle="Append-only PI-24 evidence." />
            <dl className="key-value-list">
              <KeyValue label="Relevance records">{state.counts?.by_kind?.relevance ?? 0}</KeyValue>
              <KeyValue label="Feature snapshots">{state.counts?.by_kind?.feature_snapshot ?? 0}</KeyValue>
              <KeyValue label="Warnings">{state.counts?.by_kind?.warning ?? 0}</KeyValue>
              <KeyValue label="Outcomes">{state.counts?.by_kind?.outcome ?? 0}</KeyValue>
            </dl>
          </Card>
        </div>
      );
    }
    if (view === "timeline") {
      if (timeline.loading) return <LoadingBlock label="Reading event timeline" />;
      if (timeline.error) return <ErrorBlock message={timeline.error.message} onRetry={timeline.refresh} />;
      return (
        <Card>
          <CardHeader title="Event timeline" subtitle="Publication, event date, processing status and version." />
          <Table
            rows={timelineRows}
            columns={[
              { key: "event_date", label: "Event date", render: (row) => dateTime(row.event_date) },
              { key: "published", label: "Published", render: (row) => dateTime(row.published_at) },
              { key: "title", label: "Event", render: (row) => row.title || row.event_id },
              { key: "status", label: "Status", render: (row) => <Badge tone={tone(row.status)}>{title(row.status)}</Badge> },
              { key: "verification", label: "Verification", render: (row) => title(row.verification) },
            ]}
            empty={
              instrument.trim()
                ? `No stored events matched ${instrument.trim()} at horizon ${horizon}.`
                : "Enter an instrument to narrow the timeline, or read every stored event."
            }
          />
        </Card>
      );
    }
    if (view === "relevance") {
      return (
        <div className="stack">
          <Card>
            <CardHeader
              title="Event relevance"
              subtitle="Direct, sector-related, market-wide, indirect, not relevant or insufficient evidence."
            />
            {eventFields()}
            <Button
              variant="primary"
              icon={<GitCompareArrows size={15} />}
              loading={busy === "relevance"}
              onClick={() => run("relevance", "relevance", eventId.trim() ? { event_id: eventId.trim() } : {})}
            >
              Assess relevance
            </Button>
          </Card>
          {failureBanner}
          {result?.assessments && (
            <Card>
              <CardHeader
                title="Assessment"
                subtitle={`${result.policy_version} — ${result.events_considered ?? 0} events considered`}
              />
              <Table
                rows={result.assessments}
                columns={[
                  { key: "event_id", label: "Event" },
                  { key: "relevance", label: "Relevance", render: (row) => <Badge tone={tone(row.relevance)}>{title(row.relevance)}</Badge> },
                  { key: "confidence", label: "Confidence", render: (row) => title(row.confidence) },
                  { key: "importance", label: "Importance", render: (row) => title(row.importance) },
                  { key: "horizon", label: "Horizon impact", render: (row) => <small className="muted">{title(row.horizon_effect)}</small> },
                  { key: "direction", label: "Direction", render: (row) => title(row.direction) },
                ]}
                empty="No relevant events were found."
              />
            </Card>
          )}
        </div>
      );
    }
    if (view === "impact") {
      return (
        <div className="stack">
          <Card>
            <CardHeader
              title="Event impact"
              subtitle="Channels and observed historical co-movement. No direction or price adjustment."
            />
            {eventFields()}
            <Button
              variant="primary"
              icon={<Activity size={15} />}
              loading={busy === "impact"}
              onClick={() => run("impact", "impact", { event_id: eventId.trim() })}
            >
              Analyze impact
            </Button>
            {!eventId.trim() && <small className="muted">Impact reads one stored event; enter its event id above.</small>}
          </Card>
          {failureBanner}
          {result?.impact_id && (
            <Card>
              <CardHeader title="Impact assessment" subtitle={result.impact_id} />
              <dl className="key-value-list">
                <KeyValue label="Status"><Badge tone={tone(result.status)}>{title(result.status)}</Badge></KeyValue>
                <KeyValue label="Direction">{title(result.direction)}</KeyValue>
                <KeyValue label="Price adjustment">{title(result.price_adjustment)}</KeyValue>
                <KeyValue label="Historical samples">{result.historical?.samples ?? 0}</KeyValue>
                <KeyValue label="Causation established">No</KeyValue>
              </dl>
              <Banner tone="warn" title="Correlation, not causation">
                Historical co-movement is never presented as proof that the event caused a price move.
              </Banner>
            </Card>
          )}
        </div>
      );
    }
    if (view === "warnings") {
      return (
        <div className="stack">
          <Card>
            <CardHeader title="Event forecast warnings" subtitle="Horizon-aware uncertainty; no fabricated probability." />
            {eventFields()}
            <Button
              variant="primary"
              icon={<ShieldQuestion size={15} />}
              loading={busy === "warnings"}
              onClick={() => run("warnings", "warnings")}
            >
              Assess warnings
            </Button>
          </Card>
          {failureBanner}
          {result?.warnings && (
            <Card>
              <CardHeader title="Warnings" subtitle={result.warning_id} />
              <dl className="key-value-list">
                <KeyValue label="Status">
                  <Badge tone={tone(result.status)}>{title(result.status)}</Badge>
                </KeyValue>
                <KeyValue label="Relevant events">{result.relevant_events ?? 0}</KeyValue>
                <KeyValue label="Upcoming events">{(result.upcoming_event_ids || []).length}</KeyValue>
                <KeyValue label="Calibrated probability">
                  {result.probability_claimed ? "Claimed" : "Not claimed"}
                </KeyValue>
                <KeyValue label="Risk authority">{result.risk_authority || "NM-08"}</KeyValue>
              </dl>
              <Table
                rows={result.warnings}
                columns={[
                  { key: "code", label: "Code" },
                  { key: "severity", label: "Severity", render: (row) => <Badge tone={tone(row.severity)}>{title(row.severity)}</Badge> },
                  { key: "event_ids", label: "Events", render: (row) => (row.event_ids || []).join(", ") },
                  { key: "explanation", label: "Explanation", render: (row) => <small className="muted">{row.explanation}</small> },
                ]}
                empty="No event warnings were raised for this scope."
              />
            </Card>
          )}
        </div>
      );
    }
    if (view === "features") {
      return (
        <div className="stack">
          <Card>
            <CardHeader
              title="Event features"
              subtitle="Point-in-time PI-04 consumable evidence, with explicit zero and missing values."
            />
            {eventFields()}
            <Button
              variant="primary"
              icon={<Layers3 size={15} />}
              loading={busy === "features"}
              onClick={() => run("features", "features")}
            >
              Build feature snapshot
            </Button>
          </Card>
          {failureBanner}
          {result?.features && (
            <Card>
              <CardHeader
                title="Snapshot"
                subtitle={`${result.feature_version} — ${result.snapshot_id} — ${result.feature_count ?? result.features.length} features, ${result.events_used ?? 0} events used`}
              />
              <Table
                rows={result.features}
                columns={[
                  { key: "name", label: "Feature", render: (row) => <strong>{row.name}</strong> },
                  { key: "value", label: "Value", align: "right", render: (row) => metricValue(row.value) },
                  { key: "status", label: "Status", render: (row) => <Badge tone={tone(row.status)}>{title(row.status)}</Badge> },
                  { key: "source", label: "Source", render: (row) => row.source },
                ]}
                empty="No event features were produced."
              />
            </Card>
          )}
        </div>
      );
    }
    if (view === "outcomes") {
      return (
        <div className="stack">
          <Card>
            <CardHeader
              title="Event outcomes"
              subtitle="Observed market behaviour from stored bars, explicitly correlational."
            />
            {eventFields()}
            <div className="button-row">
              <Button
                variant="primary"
                icon={<BadgeCheck size={15} />}
                loading={busy === "outcome"}
                onClick={() => run("outcome", "outcomes", { event_id: eventId.trim() }, { requiresEvent: true })}
              >
                Record outcome
              </Button>
              <Button
                variant="secondary"
                loading={busy === "study"}
                onClick={() => run("study", "outcomes/study", {}, { readOnly: true })}
              >
                Read outcome study
              </Button>
            </div>
            {!eventId.trim() && (
              <small className="muted">Recording an outcome reads one stored event; the study above covers every recorded outcome.</small>
            )}
          </Card>
          {failureBanner}
          {result?.outcome_id && (
            <Card>
              <CardHeader title="Outcome" subtitle={result.outcome_id} />
              <dl className="key-value-list">
                <KeyValue label="Status"><Badge tone={tone(result.status)}>{title(result.status)}</Badge></KeyValue>
                <KeyValue label="Bars before / after">{`${result.bars_before} / ${result.bars_after}`}</KeyValue>
                <KeyValue label="Post-event return">{metricValue(result.post_return)}</KeyValue>
                <KeyValue label="Volatility change">{metricValue(result.volatility_change)}</KeyValue>
                <KeyValue label="Causation established">No</KeyValue>
              </dl>
            </Card>
          )}
          {result?.studies && (
            <Card>
              <CardHeader title="Outcome study" subtitle={`Minimum ${result.minimum_samples} samples per horizon.`} />
              <Table
                rows={result.studies}
                columns={[
                  { key: "horizon", label: "Horizon" },
                  { key: "samples", label: "Samples", align: "right" },
                  { key: "sufficient", label: "Sufficient", render: (row) => (row.sufficient ? "Yes" : "No") },
                  { key: "mean_post_return", label: "Mean return", align: "right", render: (row) => metricValue(row.mean_post_return) },
                  { key: "mean_abs", label: "Mean absolute", align: "right", render: (row) => metricValue(row.mean_absolute_post_return) },
                  { key: "causation", label: "Causation", render: () => "Not established" },
                ]}
                empty="No recorded event outcomes yet."
              />
            </Card>
          )}
        </div>
      );
    }
    return (
      <div className="stack">
        <Card>
          <CardHeader
            title="Agent evidence"
            subtitle="Verified and unverified events remain separate for the existing PI-17 agents."
          />
          {eventFields()}
          <Button
            variant="primary"
            icon={<RefreshCw size={15} />}
            loading={busy === "agents"}
            onClick={() => run("agents", "agents")}
          >
            Build agent evidence
          </Button>
        </Card>
        {failureBanner}
        {result?.roles && (
          <Card>
            <CardHeader
              title="Role evidence"
              subtitle={`${title(result.uncertainty)} uncertainty — ${(result.verified_events || []).length} verified / ${(result.unverified_events || []).length} unverified — horizon ${title(result.horizon_relevance)}`}
            />
            <Table
              rows={Object.entries(result.roles).map(([role, evidence]) => ({ role, ...(evidence as any) }))}
              columns={[
                { key: "role", label: "Agent role", render: (row) => <strong>{row.role}</strong> },
                { key: "events", label: "Events", render: (row) => (row.events || []).length },
                {
                  key: "warnings",
                  label: "Warnings",
                  render: (row) =>
                    Array.isArray(row.warnings)
                      ? row.warnings.length
                      : row.warnings
                        ? "1 warning record"
                        : "—",
                },
                { key: "status", label: "Warning status", render: (row) => row.warning_status || "—" },
              ]}
              empty="No role evidence was produced."
            />
            <Banner tone="warn" title="No execution authority">
              PI-24 adds evidence only. The Trading Decision Agent gains no order or autonomy authority.
              {" "}
              Conflicting reports: {result.conflicting_reports ? "present" : "none"}.
            </Banner>
          </Card>
        )}
      </div>
    );
  }

  return (
    <div className="page">
      <PageHeader
        title="Event Intelligence"
        description="Verified corporate, economic and market events for event-aware prediction."
        actions={
          <Button variant="secondary" icon={<RefreshCw size={15} />} onClick={status.refresh}>
            Refresh
          </Button>
        }
      />
      <div className="form-grid">
        <Field label="Instrument">
          <TextInput
            value={instrument}
            onChange={(event) => setInstrument(event.target.value)}
            placeholder="XNSE:RELIANCE"
          />
        </Field>
        <Field label="Horizon">
          <SelectInput value={horizon} onChange={(event) => setHorizon(event.target.value)}>
            {HORIZONS.map((item) => (
              <option key={item} value={item}>
                {item}
              </option>
            ))}
          </SelectInput>
        </Field>
      </div>
      <div className="tab-strip">
        {VIEWS.map((item) => {
          const Icon = item.icon;
          return (
            <button
              key={item.id}
              type="button"
              className={`tab${view === item.id ? " active" : ""}`}
              onClick={() => {
                setView(item.id);
                setResult(null);
                setFailure(null);
              }}
            >
              <Icon size={14} /> {item.label}
            </button>
          );
        })}
      </div>
      {status.error && <ErrorBlock message={status.error.message} onRetry={status.refresh} />}
      {renderView()}
      <Card>
        <CardHeader title="Latest stored event evidence" subtitle="Read-only audit surface." />
        <dl className="key-value-list">
          <KeyValue label="Latest relevance">
            {latest.relevance
              ? latest.relevance.assessment?.relevance
                ? title(latest.relevance.assessment.relevance)
                : `no relevant events (${latest.relevance.events_considered ?? 0} considered)`
              : "—"}
          </KeyValue>
          <KeyValue label="Latest warning">{latest.warning?.status ? title(latest.warning.status) : "—"}</KeyValue>
          <KeyValue label="Latest feature snapshot">{latest.feature_snapshot?.snapshot_id || "—"}</KeyValue>
          <KeyValue label="Latest outcome">{latest.outcome?.outcome_id || "—"}</KeyValue>
          <KeyValue label="Stored records">{state.counts?.total ?? 0}</KeyValue>
          <KeyValue label="Source errors">{(state.errors || []).length}</KeyValue>
        </dl>
      </Card>
    </div>
  );
}
