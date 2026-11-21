import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BrainCircuit,
  FlaskConical,
  GitBranch,
  Play,
  RefreshCw,
  ShieldCheck,
  Square,
  Users,
} from "lucide-react";
import { apiPost } from "../api";
import { useApi, useDocumentTitle } from "../hooks";
import { arrayFrom, dateTime, measured, num, shortId, statusTone, title } from "../lib";
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

const HORIZONS = ["5m", "15m", "20m", "60m", "1d", "3d", "5d", "30d", "90d"];
const OPTIONAL_SPECIALISTS = [
  ["fin-market-research", "Market Research"],
  ["fin-fundamental", "Fundamental"],
  ["fin-rl-policy", "RL Policy"],
] as const;

function tone(value: unknown): "good" | "warn" | "bad" | "neutral" | "info" {
  const status = String(value ?? "").toLowerCase();
  if (["healthy", "completed", "buy", "valid", "fresh", "verified"].includes(status)) return "good";
  if (["degraded", "delayed", "partial", "wait", "hold", "insufficient_evidence", "abstain"].includes(status)) return "warn";
  if (["failed", "timeout", "invalid", "stale", "unavailable", "no_action"].includes(status)) return "bad";
  return "info";
}

function actionTone(value: string): "good" | "warn" | "bad" | "neutral" | "info" {
  if (value === "buy") return "good";
  if (value === "sell") return "bad";
  if (value === "wait" || value === "hold") return "warn";
  return "neutral";
}

function errorRate(value: unknown): string {
  const number = num(value, NaN);
  return Number.isFinite(number) ? `${(number * 100).toFixed(1)}%` : "—";
}

export function MultiAgentIntelligencePage() {
  useDocumentTitle("Multi-Agent Intelligence");
  const toast = useToast();
  const status = useApi<any>("/market/multiagent/status", { refreshMs: 30_000 });
  const agents = useApi<any>("/market/multiagent/agents", { refreshMs: 30_000 });
  const health = useApi<any>("/market/multiagent/health", { refreshMs: 30_000 });
  const performance = useApi<any>("/market/multiagent/performance", { refreshMs: 30_000 });
  const cycles = useApi<any>("/market/multiagent/cycles?limit=50", { refreshMs: 15_000 });
  const history = useApi<any>("/market/multiagent/history?limit=25", { refreshMs: 30_000 });

  const [instrument, setInstrument] = useState("XNSE:ALPHA");
  const [exchange, setExchange] = useState("XNSE");
  const [horizon, setHorizon] = useState("5d");
  const [portfolioId, setPortfolioId] = useState("");
  const [asOf, setAsOf] = useState("");
  const [selectedAgents, setSelectedAgents] = useState<string[]>([]);
  const [selectedCycle, setSelectedCycle] = useState("");
  const [busy, setBusy] = useState("");
  const [selectedResponse, setSelectedResponse] = useState("");

  const cycle = useApi<any>(
    selectedCycle ? `/market/multiagent/cycles/${encodeURIComponent(selectedCycle)}` : null,
    { refreshMs: 10_000 },
  );
  const evidence = useApi<any>(
    selectedCycle ? `/market/multiagent/cycles/${encodeURIComponent(selectedCycle)}/evidence` : null,
    { refreshMs: 15_000 },
  );
  const conflicts = useApi<any>(
    selectedCycle ? `/market/multiagent/cycles/${encodeURIComponent(selectedCycle)}/conflicts` : null,
    { refreshMs: 15_000 },
  );

  const agentRows = arrayFrom(agents.data, "agents");
  const cycleRows = arrayFrom(cycles.data, "items");
  const performanceRows = arrayFrom(performance.data, "items");
  const historyRows = arrayFrom(history.data, "items");
  const evidenceRows = arrayFrom(evidence.data, "items");
  const conflictRows = arrayFrom(conflicts.data, "items");
  const activeCycle = cycle.data || cycleRows.find((row: any) => row.cycle_id === selectedCycle) || null;
  const responses = Array.isArray(activeCycle?.responses) ? activeCycle.responses : [];
  const decision = activeCycle?.decision || null;
  const response = responses.find((row: any) => row.agent_id === selectedResponse) || responses[0] || null;
  // A handoff only exists once THIS cycle produced a decision. Gating on the
  // loaded cycle (not on the previous selection) stops a request for a pending,
  // cancelled or failed cycle, and keeps a stale document from showing.
  const handoffReady = Boolean(
    selectedCycle && cycle.data?.cycle_id === selectedCycle && cycle.data?.decision,
  );
  const handoff = useApi<any>(
    handoffReady
      ? `/market/multiagent/cycles/${encodeURIComponent(selectedCycle)}/handoff`
      : null,
    { refreshMs: 30_000 },
  );
  const handoffDoc = handoff.data?.cycle_id === selectedCycle ? handoff.data : null;

  useEffect(() => {
    if (!selectedCycle && cycleRows.length) setSelectedCycle(String(cycleRows[0].cycle_id));
  }, [cycleRows, selectedCycle]);

  useEffect(() => {
    if (responses.length && !responses.some((row: any) => row.agent_id === selectedResponse)) {
      setSelectedResponse(String(responses[0].agent_id));
    }
  }, [responses, selectedResponse]);

  const refreshAll = () => {
    status.refresh();
    agents.refresh();
    health.refresh();
    performance.refresh();
    cycles.refresh();
    history.refresh();
    cycle.refresh();
    evidence.refresh();
    conflicts.refresh();
    handoff.refresh();
  };

  const run = async (key: string, action: () => Promise<void>) => {
    setBusy(key);
    try {
      await action();
    } catch (error: any) {
      toast.push(error?.detail || error?.message || "The research request was refused.", "error");
    } finally {
      setBusy("");
    }
  };

  const createCycle = () =>
    run("create", async () => {
      if (!instrument.trim()) throw new Error("Instrument is required.");
      const payload: Record<string, any> = {
        instrument_id: instrument.trim(),
        exchange: exchange.trim().toUpperCase() || "XNSE",
        horizon,
        portfolio_id: portfolioId.trim(),
      };
      if (asOf.trim()) payload.as_of = new Date(asOf).toISOString();
      if (selectedAgents.length) payload.agents = selectedAgents;
      const result = await apiPost<any>("/market/multiagent/cycles", payload);
      const id = result.cycle?.cycle_id;
      if (id) setSelectedCycle(String(id));
      toast.push(
        result.idempotent_reuse
          ? "Existing decision cycle reused; no duplicate research was started."
          : "Decision cycle created. Run it to collect independent agent evidence.",
        "success",
      );
      cycles.refresh();
      status.refresh();
    });

  const runCycle = (cycleId = selectedCycle) =>
    run("run", async () => {
      if (!cycleId) throw new Error("Select a decision cycle first.");
      const result = await apiPost<any>(
        `/market/multiagent/cycles/${encodeURIComponent(cycleId)}/run`,
        {},
        undefined,
        { timeoutMs: 120_000 },
      );
      const received = result.decision?.action || "no decision";
      toast.push(
        `Research completed: ${title(received)}. No order was submitted; NM-08 review is still required.`,
        ["buy", "sell"].includes(received) ? "success" : "info",
      );
      setSelectedCycle(String(cycleId));
      refreshAll();
    });

  const cancelCycle = (cycleId = selectedCycle) =>
    run("cancel", async () => {
      if (!cycleId) throw new Error("Select a decision cycle first.");
      await apiPost<any>(`/market/multiagent/cycles/${encodeURIComponent(cycleId)}/cancel`, {});
      toast.push("Research task cancelled. No financial state or order was touched.", "success");
      refreshAll();
    });

  const toggleAgent = (agentId: string) => {
    setSelectedAgents((current) =>
      current.includes(agentId) ? current.filter((item) => item !== agentId) : [...current, agentId],
    );
  };

  const progress = activeCycle?.assigned_agents?.length
    ? Math.round((responses.length / activeCycle.assigned_agents.length) * 100)
    : 0;
  // The coordinator only cancels a cycle that has not finished; offering the
  // control on a completed cycle produced a 409 error toast.
  const cancellable = ["pending", "running"].includes(
    String(activeCycle?.status || "").toLowerCase(),
  );

  const agentColumns: Column<any>[] = [
    {
      key: "name",
      label: "Agent",
      sortable: true,
      render: (row) => (
        <div>
          <strong>{row.name}</strong>
          <div className="muted mono">{row.agent_id}</div>
        </div>
      ),
    },
    { key: "role", label: "Role", render: (row) => title(row.role) },
    {
      key: "required",
      label: "Scope",
      render: (row) => (
        <Badge tone={row.required ? "info" : "neutral"}>
          {row.required ? "required" : "optional"}
        </Badge>
      ),
    },
    { key: "health", label: "Health", render: (row) => <StatusBadge value={row.health} /> },
    { key: "provider", label: "Provider", render: (row) => row.provider || "—" },
    { key: "model_version", label: "Model", render: (row) => row.model_version || "—" },
    { key: "last_execution", label: "Last execution", render: (row) => row.last_execution ? dateTime(row.last_execution) : "Never" },
    {
      key: "average_latency_ms",
      label: "Avg latency",
      align: "right",
      sortable: true,
      render: (row) => row.performance?.average_latency_ms != null ? `${num(row.performance.average_latency_ms).toFixed(0)} ms` : "—",
    },
    {
      key: "error_rate",
      label: "Error rate",
      align: "right",
      sortable: true,
      render: (row) => errorRate(row.performance?.error_rate),
    },
  ];

  const performanceColumns: Column<any>[] = [
    { key: "agent_id", label: "Agent", render: (row) => <span className="mono">{row.agent_id}</span> },
    { key: "executions", label: "Runs", align: "right", sortable: true },
    { key: "average_latency_ms", label: "Latency", align: "right", render: (row) => row.average_latency_ms != null ? `${num(row.average_latency_ms).toFixed(0)} ms` : "—" },
    { key: "timeout_rate", label: "Timeouts", align: "right", render: (row) => errorRate(row.timeout_rate) },
    { key: "error_rate", label: "Failures", align: "right", render: (row) => errorRate(row.error_rate) },
    { key: "validation_failure_rate", label: "Invalid", align: "right", render: (row) => errorRate(row.validation_failure_rate) },
    { key: "missing_evidence_rate", label: "Missing evidence", align: "right", render: (row) => errorRate(row.missing_evidence_rate) },
    {
      key: "historical_quality",
      label: "Historical quality",
      render: (row) => row.historical_quality == null ? <span className="muted">Not claimed</span> : num(row.historical_quality).toFixed(3),
    },
    { key: "health", label: "Health", render: (row) => <StatusBadge value={row.health} /> },
  ];

  const responseColumns: Column<any>[] = [
    { key: "agent_id", label: "Agent", render: (row) => <span className="mono">{row.agent_id}</span> },
    { key: "status", label: "Status", render: (row) => <StatusBadge value={row.status} /> },
    { key: "action", label: "Advisory", render: (row) => <Badge tone={actionTone(row.action)}>{title(row.action)}</Badge> },
    { key: "latency_ms", label: "Latency", align: "right", render: (row) => `${measured(row.latency_ms, 0)} ms` },
    { key: "supporting", label: "Support", render: (row) => `${arrayFrom({ items: row.supporting }).length} / ${arrayFrom({ items: row.contradicting }).length}` },
    { key: "missing", label: "Missing", render: (row) => arrayFrom({ items: row.missing }).length || "—" },
  ];

  const evidenceColumns: Column<any>[] = [
    { key: "evidence_type", label: "Type", render: (row) => <Badge tone={tone(row.evidence_type)}>{title(row.evidence_type)}</Badge> },
    { key: "source_identity", label: "Source", render: (row) => <span>{row.source_identity}</span> },
    { key: "agent_id", label: "Agent", render: (row) => <span className="mono">{row.agent_id}</span> },
    { key: "freshness", label: "Freshness", render: (row) => <StatusBadge value={row.freshness} /> },
    { key: "validation", label: "Validation", render: (row) => <StatusBadge value={row.validation} /> },
    { key: "original_at", label: "Source time", render: (row) => row.original_at ? dateTime(row.original_at) : "Unknown" },
    { key: "retrieved_at", label: "Retrieved", render: (row) => dateTime(row.retrieved_at) },
    { key: "source_reference", label: "Reference", render: (row) => <span className="mono">{shortId(row.source_reference, 20)}</span> },
  ];

  return (
    <div className="page">
      <PageHeader
        eyebrow="PI-17 · coordinated financial intelligence"
        title="Multi-Agent Intelligence"
        description="Nine explicit roles gather independent, point-in-time evidence and produce an advisory recommendation. NM-08 remains the only risk authority and NM-10 the only paper-execution authority."
        meta={
          <>
            <Badge tone="good">Advisory only</Badge>
            <Badge tone="neutral">NM-08 risk authority</Badge>
            <Badge tone="neutral">NM-10 execution authority</Badge>
            <Badge tone={status.data?.initialised ? "info" : "warn"}>{status.data?.initialised ? "Coordinator open" : "Coordinator lazy"}</Badge>
          </>
        }
        actions={
          <Button variant="secondary" icon={<RefreshCw size={15} />} onClick={refreshAll} loading={Boolean(status.refreshing || cycles.refreshing)}>
            Refresh
          </Button>
        }
      />

      <Banner tone="info" title="Research, not execution">
        Agents can read approved evidence and write advisory decisions. They cannot mutate portfolios or ledgers, call NM-08 for approval, submit NM-10 orders, change autonomy modes, activate experimental policies, connect brokers, or enable live trading.
      </Banner>

      {(status.error || agents.error || cycle.error) && (
        <ErrorBlock message={(status.error || agents.error || cycle.error)?.detail || "Could not read the multi-agent surface."} onRetry={refreshAll} />
      )}

      <div className="metric-grid">
        <Metric label="Registered roles" value={status.data?.agents ?? agentRows.length ?? "—"} icon={<Users size={16} />} help="8 specialists + advisory decision agent" />
        <Metric label="Decision cycles" value={status.data?.cycles ?? "—"} icon={<GitBranch size={16} />} help={`${status.data?.running_cycles ?? 0} running`} />
        <Metric label="Degraded cycles" value={status.data?.degraded_cycles ?? "—"} icon={<AlertTriangle size={16} />} tone="warn" help="failures stay visible, never hidden" />
        <Metric label="Agent health" value={health.data?.health ? title(health.data.health) : "—"} icon={<Activity size={16} />} tone={health.data?.ok ? "good" : "warn"} help="never disables NM-08 or NM-10" />
      </div>

      <div className="two-col-grid">
        <Card>
          <CardHeader
            title="Start a decision cycle"
            subtitle="One instrument, one horizon. Technical, prediction, regime, risk and portfolio always run; optional specialists are additive."
            eyebrow="Task"
          />
          <div className="stack-tight">
            <div className="form-grid">
              <Field label="Instrument">
                <TextInput value={instrument} onChange={(event) => setInstrument(event.target.value)} placeholder="XNSE:ALPHA" />
              </Field>
              <Field label="Exchange">
                <TextInput value={exchange} onChange={(event) => setExchange(event.target.value)} placeholder="XNSE" />
              </Field>
              <Field label="Horizon" hint="Horizons are never mixed in one cycle.">
                <SelectInput value={horizon} onChange={(event) => setHorizon(event.target.value)}>
                  {HORIZONS.map((item) => <option key={item} value={item}>{item}</option>)}
                </SelectInput>
              </Field>
              <Field label="Portfolio (required for risk)" hint="Read-only context; risk and portfolio specialists abstain without it.">
                <TextInput value={portfolioId} onChange={(event) => setPortfolioId(event.target.value)} placeholder="portfolio id" />
              </Field>
              <Field label="As of (optional)" hint="Later evidence is excluded; latest-only sources may abstain.">
                <TextInput type="datetime-local" value={asOf} onChange={(event) => setAsOf(event.target.value)} />
              </Field>
            </div>
            <div>
              <div className="field-label">Add optional specialists <span className="muted">(required roles cannot be removed)</span></div>
              <div className="chip-row">
                {OPTIONAL_SPECIALISTS.map(([id, label]) => (
                  <Chip key={id} active={selectedAgents.includes(id)} onClick={() => toggleAgent(id)}>{label}</Chip>
                ))}
              </div>
            </div>
            <div className="toolbar">
              <Button variant="primary" icon={<FlaskConical size={15} />} loading={busy === "create"} onClick={createCycle}>Create cycle</Button>
              <Button variant="secondary" icon={<Play size={15} />} loading={busy === "run"} disabled={!selectedCycle} onClick={() => runCycle()}>Run selected</Button>
              <Button variant="ghost" icon={<Square size={14} />} loading={busy === "cancel"} disabled={!selectedCycle || !cancellable} onClick={() => cancelCycle()}>Cancel</Button>
            </div>
          </div>
        </Card>

        <Card>
          <CardHeader
            title="Decision-cycle monitor"
            subtitle={activeCycle ? `${activeCycle.instrument_id} · ${activeCycle.exchange} · ${activeCycle.horizon}` : "Select a cycle to inspect it."}
            eyebrow="Orchestration"
            action={activeCycle && <StatusBadge value={activeCycle.status} />}
          />
          {activeCycle ? (
            <div className="stack-tight">
              <div className="detail-grid">
                <div>
                  <KeyValue label="Cycle ID" mono>{activeCycle.cycle_id}</KeyValue>
                  <KeyValue label="Correlation" mono>{activeCycle.correlation_id}</KeyValue>
                  <KeyValue label="Agents">{activeCycle.assigned_agents?.length || 0} assigned · {activeCycle.required_agents?.length || 0} required</KeyValue>
                  <KeyValue label="Started">{activeCycle.started_at ? dateTime(activeCycle.started_at) : "Not started"}</KeyValue>
                  <KeyValue label="Finished">{activeCycle.finished_at ? dateTime(activeCycle.finished_at) : "—"}</KeyValue>
                </div>
                <div className="detail-aside">
                  <KeyValue label="Completion">{responses.length} / {activeCycle.assigned_agents?.length || 0} responses</KeyValue>
                  <div className="progress-track"><div className="progress-fill" style={{ width: `${progress}%` }} /></div>
                  <KeyValue label="Conflicts">{conflictRows.length}</KeyValue>
                  <KeyValue label="Failures">{activeCycle.failed_agents || 0}</KeyValue>
                </div>
              </div>
              <div className="chip-row">
                {(activeCycle.assigned_agents || []).map((agentId: string) => {
                  const run = responses.find((item: any) => item.agent_id === agentId);
                  return <Badge key={agentId} tone={run ? tone(run.status) : "neutral"}>{agentId.replace("fin-", "")}: {run ? title(run.status) : "pending"}</Badge>;
                })}
              </div>
              {activeCycle.error && <Banner tone="warn" title="Orchestration note">{activeCycle.error}</Banner>}
              {activeCycle.degraded && <Banner tone="warn" title="Degraded evidence">At least one required or optional agent did not provide usable evidence. The decision is downgraded and the missing analysis remains visible.</Banner>}
            </div>
          ) : (
            <EmptyState icon={<GitBranch size={24} />} title="No cycle selected" description="Create a decision cycle or choose one from the monitor below." />
          )}
        </Card>
      </div>

      <Card>
        <CardHeader title="Agent dashboard" subtitle="Configuration, provider, execution history and health. Health is operational evidence, not predictive quality." eyebrow="Registry" />
        {agents.loading ? <LoadingBlock /> : <DataTable columns={agentColumns} rows={agentRows} rowKey={(row) => row.agent_id} empty={<EmptyState title="No agents registered" description="The coordinator could not open its registry." />} />}
      </Card>

      <Card>
        <CardHeader
          title="Cycle history"
          subtitle="Research history is kept separate from portfolios, ledgers, orders and fills."
          eyebrow="Monitor"
          action={<Badge tone="neutral">{cycleRows.length} recent</Badge>}
        />
        <DataTable
          columns={[
            { key: "cycle_id", label: "Cycle", render: (row) => <span className="mono">{shortId(row.cycle_id, 16)}</span> },
            { key: "instrument_id", label: "Instrument" },
            { key: "horizon", label: "Horizon" },
            { key: "status", label: "Status", render: (row) => <StatusBadge value={row.status} /> },
            { key: "agents", label: "Agents", align: "right", render: (row) => `${row.completed_agents || 0}/${row.agent_count || row.assigned_agents?.length || 0}` },
            { key: "conflict_count", label: "Conflicts", align: "right" },
            { key: "created_at", label: "Created", render: (row) => dateTime(row.created_at) },
          ]}
          rows={cycleRows}
          rowKey={(row) => row.cycle_id}
          onRowClick={(row) => setSelectedCycle(String(row.cycle_id))}
          empty={<EmptyState title="No decision cycles" description="Create the first bounded multi-agent research cycle above." />}
        />
      </Card>

      {activeCycle && (
        <>
          <div className="two-col-grid">
            <Card>
              <CardHeader title="Agent analysis workspace" subtitle="Supporting and contradictory findings, uncertainty and retained source references." eyebrow="Evidence" />
              <DataTable
                columns={responseColumns}
                rows={responses}
                rowKey={(row) => row.agent_id}
                onRowClick={(row) => setSelectedResponse(String(row.agent_id))}
                empty={<EmptyState title="No agent responses yet" description="Run the selected cycle to collect independent analysis." />}
              />
              {response && (
                <div className="detail-section">
                  <h3>{response.agent_id} · {title(response.status)}</h3>
                  <div className="detail-grid">
                    <div className="detail-main">
                      <div className="detail-prose">
                        Advisory action: <strong>{title(response.action)}</strong>. Confidence: <strong>{response.confidence == null ? "not calibrated / unavailable" : num(response.confidence).toFixed(3)}</strong> ({response.confidence_kind}).
                      </div>
                      <FindingList label="Supporting" items={response.supporting} />
                      <FindingList label="Contradictory" items={response.contradicting} />
                      <FindingList label="Uncertainty" items={response.uncertainties} />
                      <FindingList label="Missing evidence" items={response.missing} />
                      {response.error && <Banner tone="bad" title="Agent error">{response.error}</Banner>}
                      <details className="detail-section">
                        <summary>Structured findings</summary>
                        <pre className="code-block">{JSON.stringify(response.findings, null, 2)}</pre>
                      </details>
                    </div>
                    <aside className="detail-aside">
                      <KeyValue label="Started">{dateTime(response.started_at)}</KeyValue>
                      <KeyValue label="Finished">{dateTime(response.finished_at)}</KeyValue>
                      <KeyValue label="Latency">{measured(response.latency_ms, 0)} ms</KeyValue>
                      <KeyValue label="Config version">{response.config_version}</KeyValue>
                      <KeyValue label="Model version">{response.model_version}</KeyValue>
                      <KeyValue label="Source references">{arrayFrom({ items: response.source_references }).length}</KeyValue>
                      <KeyValue label="Evidence rows">{arrayFrom({ items: response.evidence }).length}</KeyValue>
                    </aside>
                  </div>
                </div>
              )}
            </Card>

            <Card>
              <CardHeader
                title="Decision aggregation"
                subtitle="Evidence-weighted, not a vote. Unresolved disagreement and authoritative restrictions are preserved."
                eyebrow="Advisory recommendation"
                action={decision && <Badge tone={actionTone(decision.action)}>{title(decision.action)}</Badge>}
              />
              {decision ? (
                <div className="stack-tight">
                  <div className="metric-grid">
                    <Metric label="Action" value={title(decision.action)} tone={actionTone(decision.action) === "good" ? "good" : actionTone(decision.action) === "bad" ? "bad" : "warn"} />
                    <Metric label="Evidence strength" value={title(decision.evidence_strength)} />
                    <Metric label="Confidence" value={decision.confidence == null ? "Not claimed" : num(decision.confidence).toFixed(3)} tone="neutral" />
                    <Metric label="Unresolved conflicts" value={arrayFrom({ items: decision.unresolved_conflicts }).length} tone={arrayFrom({ items: decision.unresolved_conflicts }).length ? "warn" : "good"} />
                  </div>
                  <div className="detail-grid">
                    <div className="detail-main">
                      <div className="detail-prose">{decision.explanation}</div>
                      <FindingList label="Supporting agents" items={decision.supporting_agents} />
                      <FindingList label="Contradictory agents" items={decision.contradicting_agents} />
                      <FindingList label="Risk restrictions" items={decision.risk_restrictions} />
                    </div>
                    <aside className="detail-aside">
                      <KeyValue label="Horizon">{decision.horizon} ({title(decision.horizon_group)})</KeyValue>
                      <KeyValue label="Risk authority">{decision.risk_authority}</KeyValue>
                      <KeyValue label="Execution authority">{decision.execution_authority}</KeyValue>
                      <KeyValue label="Order submitted">{decision.order_submitted ? "YES" : "No"}</KeyValue>
                      <KeyValue label="Production authority">{decision.production_authority ? "YES" : "No"}</KeyValue>
                      <KeyValue label="Decided">{dateTime(decision.decided_at)}</KeyValue>
                    </aside>
                  </div>
                  <Banner tone="good" title="Authority boundary preserved">
                    This recommendation is research evidence. Any paper action must be re-derived by PI-12, assessed by NM-08 and submitted by NM-10 under the existing operator permissions, modes and kill switch.
                  </Banner>
                  <details>
                    <summary>Confidence provenance</summary>
                    <pre className="code-block">{JSON.stringify(decision.confidence_provenance, null, 2)}</pre>
                  </details>
                </div>
              ) : (
                <EmptyState icon={<BrainCircuit size={24} />} title="No recommendation yet" description="A decision is created only after validated agent responses are collected." />
              )}
            </Card>
          </div>

          <Card>
            <CardHeader
              title="Handoff to PI-12"
              subtitle="The document PI-12 may consume. It is evidence: no authority, no order and no mode change."
              eyebrow="Handoff"
              action={handoffDoc && <Badge tone={actionTone(String(handoffDoc.recommendation || ""))}>{title(handoffDoc.recommendation)}</Badge>}
            />
            {handoffDoc ? (
              <div className="stack-tight">
                <div className="chip-row">
                  <Badge tone={handoffDoc.advisory_only ? "info" : "bad"}>Advisory only: {handoffDoc.advisory_only ? "Yes" : "No"}</Badge>
                  <Badge tone={handoffDoc.production_authority ? "bad" : "good"}>Production authority: {handoffDoc.production_authority ? "Yes" : "No"}</Badge>
                  <Badge tone={handoffDoc.order_submitted ? "bad" : "good"}>Order submitted: {handoffDoc.order_submitted ? "Yes" : "No"}</Badge>
                  <Badge tone={handoffDoc.eligible_research_signal ? "warn" : "neutral"}>Eligible research signal: {handoffDoc.eligible_research_signal ? "Yes" : "No"}</Badge>
                </div>
                <div className="detail-grid">
                  <div className="detail-main">
                    <div className="detail-prose">
                      {handoffDoc.note || "The handoff carries the advisory decision only; the trading decision stays with PI-12."}
                    </div>
                    <FindingList
                      label="Requirements"
                      items={[
                        handoffDoc.risk_review_required && "NM-08 risk review is required",
                        handoffDoc.requires_nm08_reassessment && "NM-08 must reassess risk before any paper action",
                        handoffDoc.requires_pi12_orchestration && "PI-12 must re-derive the decision",
                        handoffDoc.requires_operator_permission && "operator permissions and autonomy mode still apply",
                      ].filter(Boolean)}
                    />
                    <FindingList label="Execution path" items={handoffDoc.execution_path} />
                  </div>
                  <aside className="detail-aside">
                    <KeyValue label="Decision ID" mono>{handoffDoc.decision_id}</KeyValue>
                    <KeyValue label="Risk authority">{handoffDoc.authority?.risk || "NM-08"}</KeyValue>
                    <KeyValue label="Execution authority">{handoffDoc.authority?.paper_execution || "NM-10"}</KeyValue>
                    <KeyValue label="Autonomy">{handoffDoc.authority?.autonomy || "PI-12"}</KeyValue>
                    <KeyValue label="Agents submit orders">{handoffDoc.authority?.agents_can_submit_orders ? "Yes" : "No"}</KeyValue>
                    <KeyValue label="Agents override kill switch">{handoffDoc.authority?.agents_can_override_kill_switch ? "Yes" : "No"}</KeyValue>
                    <KeyValue label="Agents change modes">{handoffDoc.authority?.agents_can_change_modes ? "Yes" : "No"}</KeyValue>
                  </aside>
                </div>
              </div>
            ) : handoffReady ? (
              handoff.error ? <Banner tone="warn" title="Handoff unavailable">{handoff.error.message}</Banner> : <LoadingBlock />
            ) : (
              <EmptyState title="No handoff yet" description="A handoff exists only after a cycle produced a validated decision." />
            )}
          </Card>

          <Card>
            <CardHeader title="Shared evidence" subtitle="Source identity, timestamps, freshness, validation and correlation are retained for every item." eyebrow="Provenance" action={<Badge tone="neutral">{evidenceRows.length} rows</Badge>} />
            <DataTable
              columns={evidenceColumns}
              rows={evidenceRows}
              rowKey={(row) => row.evidence_id}
              empty={<EmptyState title="No evidence retained" description="Run the cycle to collect point-in-time evidence." />}
            />
          </Card>

          <div className="two-col-grid">
            <Card>
              <CardHeader title="Conflicts" subtitle="Directional disagreement, stale data, forecast uncertainty and authoritative restrictions remain visible." eyebrow="Resolution" />
              {conflictRows.length ? (
                <div className="stack-tight">
                  {conflictRows.map((item: any) => (
                    <div className="detail-fact" key={item.conflict_id}>
                      <span>{title(item.kind)} · {title(item.severity)} · {item.resolved ? "Resolved" : "Unresolved"}</span>
                      <strong>{item.description}</strong>
                      <code>Rule: {item.resolution_rule}</code>
                      <code>Outcome: {item.resolution}</code>
                      <div className="chip-row">
                        {arrayFrom({ items: item.agents }).map((agent) => <Badge key={String(agent)} tone="neutral">{String(agent)}</Badge>)}
                      </div>
                    </div>
                  ))}
                </div>
              ) : <EmptyState title="No conflicts" description="No disagreement or authoritative constraint was recorded for this cycle." />}
            </Card>

            <Card>
              <CardHeader title="Agent performance" subtitle="Latency, failures, invalid outputs and missing evidence are measured separately from predictive quality." eyebrow="Reliability" />
              <DataTable columns={performanceColumns} rows={performanceRows} rowKey={(row) => row.agent_id} empty={<EmptyState title="No execution history" description="Performance is recorded per agent as cycles run." />} />
              <p className="muted">{performance.data?.historical_quality || "Historical predictive quality is not claimed without validated out-of-sample evidence."}</p>
            </Card>
          </div>
        </>
      )}

      <Card>
        <CardHeader title="Decision history" subtitle="Advisory recommendations remain linked to their cycle, evidence and conflicts." eyebrow="History" action={<Badge tone="neutral">{historyRows.length} recent</Badge>} />
        <DataTable
          columns={[
            { key: "decision", label: "Instrument", render: (row) => row.decision?.instrument_id },
            { key: "horizon", label: "Horizon", render: (row) => row.decision?.horizon },
            { key: "action", label: "Action", render: (row) => <Badge tone={actionTone(row.decision?.action)}>{title(row.decision?.action)}</Badge> },
            { key: "strength", label: "Evidence", render: (row) => title(row.decision?.evidence_strength) },
            { key: "confidence", label: "Confidence", render: (row) => row.decision?.confidence == null ? "Not claimed" : num(row.decision.confidence).toFixed(3) },
            { key: "conflicts", label: "Conflicts", align: "right" },
            { key: "decided", label: "Decided", render: (row) => dateTime(row.decision?.decided_at) },
          ]}
          rows={historyRows}
          rowKey={(row) => row.decision?.decision_id}
          onRowClick={(row) => setSelectedCycle(String(row.decision?.cycle_id || ""))}
          empty={<EmptyState title="No advisory decisions" description="Completed cycles appear here with their evidence strength and limitations." />}
        />
      </Card>
    </div>
  );
}

function FindingList({ label, items }: { label: string; items: unknown }) {
  const rows = arrayFrom({ items });
  if (!rows.length) return <div className="detail-section"><h3>{label}</h3><p className="muted">None recorded.</p></div>;
  return (
    <div className="detail-section">
      <h3>{label}</h3>
      <ul className="detail-list">
        {rows.map((item, index) => <li key={`${label}-${index}`}>{String(item)}</li>)}
      </ul>
    </div>
  );
}
