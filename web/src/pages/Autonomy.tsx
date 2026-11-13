import { useState } from "react";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  CalendarClock,
  CheckCircle2,
  KeyRound,
  Pause,
  Play,
  Power,
  RefreshCw,
  ScanLine,
  ListChecks,
  ShieldAlert,
  Workflow,
  ShieldCheck,
  TimerReset,
} from "lucide-react";
import { apiDelete, apiPost, apiPut } from "../api";
import { useApi, useDocumentTitle } from "../hooks";
import { dateTime, timeAgo, title } from "../lib";
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  ConfirmDialog,
  ErrorBlock,
  Field,
  KeyValue,
  LoadingBlock,
  Metric,
  PageHeader,
  StatusBadge,
  TextInput,
  useToast,
} from "../components/ui";

const CONTROL_PATHS = {
  start: "/market/autonomy/start",
  pause: "/market/autonomy/pause",
  resume: "/market/autonomy/resume",
  stop: "/market/autonomy/stop",
} as const;

export function AutonomyPage() {
  useDocumentTitle("Autonomy");
  const toast = useToast();
  const [operatorToken, setOperatorToken] = useState("");
  const [busy, setBusy] = useState("");
  const [confirmStop, setConfirmStop] = useState(false);
  const [confirmRecovery, setConfirmRecovery] = useState(false);
  const [configPortfolio, setConfigPortfolio] = useState("");
  const [configInstrument, setConfigInstrument] = useState("");
  const [configTarget, setConfigTarget] = useState("");
  const [configTrailing, setConfigTrailing] = useState("");
  const status = useApi<any>("/market/autonomy/status", {
    refreshMs: 15_000,
    operatorToken,
  });
  const monitoring = useApi<any>("/market/autonomy/monitoring", {
    refreshMs: 15_000,
    operatorToken,
  });
  const decisions = useApi<any>("/market/autonomy/decisions/status", {
    refreshMs: 15_000,
    operatorToken,
  });
  const executions = useApi<any>("/market/autonomy/executions/status", {
    refreshMs: 10_000,
    operatorToken,
  });
  const exits = useApi<any>("/market/autonomy/exits/status", {
    refreshMs: 10_000,
    operatorToken,
  });
  const exitPositions = useApi<any>("/market/autonomy/exits/positions", {
    refreshMs: 15_000,
    operatorToken,
  });
  const exitLearning = useApi<any>("/market/autonomy/exits/learning", {
    refreshMs: 15_000,
    operatorToken,
  });
  const exitConfigurations = useApi<any>("/market/autonomy/exits/configurations", {
    refreshMs: 15_000,
    operatorToken,
  });
  const reliability = useApi<any>("/market/autonomy/reliability", {
    refreshMs: 15_000,
    operatorToken,
  });
  const data = status.data || {};
  const monitor = monitoring.data || {};
  const decision = decisions.data || {};
  const paper = executions.data || {};
  const exitData = exits.data || {};
  const exitCounts = exitData.counts || {};
  const exitCycle = exitData.last_cycle || {};
  const supervisedPositions = exitPositions.data?.positions || exitData.supervision?.positions || [];
  const learningRows = exitLearning.data?.evidence || [];
  const exitConfigurationRows = exitConfigurations.data?.configurations || exitData.supervision?.configurations || [];
  const reliabilityData = reliability.data || {};
  const reliabilityHealth = reliabilityData.health || {};
  const reliabilityRuntime = reliabilityData.runtime || {};
  const reliabilityPipeline = reliabilityData.pipeline || {};
  const reliabilityFinancial = reliabilityData.financial || {};
  const reliabilityRecovery = reliabilityData.recovery || {};
  const reliabilityFailureDomains = reliabilityHealth.failure_domains || {};
  const reliabilityReasons = reliabilityHealth.reasons || [];
  const reliabilityUnresolved = reliabilityRecovery.unresolved || [];
  const paperCounts = paper.counts || {};
  const paperHealth = paper.health || {};
  const recentExecutions = paper.recent || [];
  const lastExecution = paper.last_execution || {};
  const lastDecisionRun = decision.last_run || {};
  const session = monitor.session || {};
  const lease = data.lease || {};
  const health = data.health || {};
  const config = data.config || {};
  const mode = String(data.mode || "disabled");
  const runtimeState = String(data.runtime_state || "unknown");
  const authorized = Boolean(data.operator_authorized);
  const configured = Boolean(data.operator_authorization_configured);
  const controlsEnabled = configured && authorized;
  const tradingImplemented = data.trading_implemented === true;
  const decisionBlockedReason =
    mode === "paused"
      ? "New decision runs are blocked while paused. Resume monitoring to evaluate strategies; protective AT-05 exits and reconciliation remain active."
      : mode === "disabled"
        ? "Start monitoring before running the decision pipeline. Disabled mode performs no autonomous work."
        : decision.initialised && !decision.lease_valid
          ? "The runtime lease is not valid, so the backend would refuse this run. Refresh status and check lease ownership."
          : "";

  const control = async (action: keyof typeof CONTROL_PATHS, body: Record<string, unknown> = {}) => {
    setBusy(action);
    try {
      await apiPost(CONTROL_PATHS[action], body, undefined, { operatorToken });
      toast.push(`Autonomy ${action} request accepted.`, "success");
      // A mode change invalidates every gated card, not just the runtime
      // summary. Refresh the whole surface so the page cannot show a stale
      // "disabled" pipeline beside a running runtime.
      status.refresh();
      monitoring.refresh();
      decisions.refresh();
      executions.refresh();
      exits.refresh();
      exitPositions.refresh();
      reliability.refresh();
    } catch (error: any) {
      toast.push(error?.detail || `The autonomy ${action} request was refused.`, "error");
    } finally {
      setBusy("");
      setConfirmStop(false);
    }
  };

  const recoverRuntime = async () => {
    setBusy("recover");
    try {
      const result = await apiPost(
        "/market/autonomy/recover",
        { reason: "operator recovery from Autonomy page" },
        undefined,
        { operatorToken },
      );
      toast.push(
        `Recovery ${result?.status || "completed"}: ${result?.recovered ?? 0} recovered, ${result?.reconciled ?? 0} reconciled, ${result?.unresolved_count ?? 0} unresolved. Runtime ${result?.mode || "disabled"}; auto-resume is off.`,
        result?.ok ? "success" : "error",
      );
      status.refresh();
      monitoring.refresh();
      decisions.refresh();
      executions.refresh();
      exits.refresh();
      exitPositions.refresh();
      exitLearning.refresh();
      exitConfigurations.refresh();
      reliability.refresh();
    } catch (error: any) {
      toast.push(
        error?.detail?.message || error?.detail || "The recovery request was refused.",
        "error",
      );
    } finally {
      setBusy("");
      setConfirmRecovery(false);
    }
  };

  const runDecision = async () => {
    setBusy("decision");
    try {
      const result = await apiPost(
        "/market/autonomy/decisions/run",
        { trigger: "manual" },
        undefined,
        { operatorToken },
      );
      toast.push(
        `Decision run ${result?.status || "completed"}: ${result?.evaluations ?? 0} evaluations, ${result?.approved ?? 0} NM-08 approved, ${result?.rejected ?? 0} rejected.`,
        result?.status === "completed" ? "success" : "error",
      );
      decisions.refresh();
      status.refresh();
      monitoring.refresh();
    } catch (error: any) {
      toast.push(error?.detail?.message || error?.detail || "The decision run was refused.", "error");
    } finally {
      setBusy("");
    }
  };

  const runExecution = async () => {
    setBusy("execute");
    try {
      const result = await apiPost(
        "/market/autonomy/executions/run",
        { trigger: "manual" },
        undefined,
        { operatorToken },
      );
      toast.push(
        `Paper execution ${result?.status || "completed"}: ${result?.total ?? 0} execution intent(s), ${result?.counts?.filled ?? 0} filled, ${result?.counts?.partially_filled ?? 0} partially filled.`,
        result?.status === "failed" ? "error" : "success",
      );
      executions.refresh();
      decisions.refresh();
      monitoring.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(
        error?.detail?.message || error?.detail || "The paper-execution cycle was refused.",
        "error",
      );
    } finally {
      setBusy("");
    }
  };

  const runMonitoring = async () => {
    setBusy("monitor");
    try {
      const result = await apiPost(
        "/market/autonomy/monitoring/run",
        { trigger: "manual" },
        undefined,
        { operatorToken },
      );
      const label =
        result?.status === "completed"
          ? `Monitoring cycle completed: ${result.processed} processed, ${result.deferred} deferred.`
          : result?.status === "deferred"
            ? `Monitoring deferred: ${result.reason_code}.`
            : `Monitoring cycle ${result?.status || "refused"}.`;
      toast.push(label, result?.ok ? "success" : "error");
      monitoring.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The monitoring cycle was refused.", "error");
    } finally {
      setBusy("");
    }
  };

  const configureExit = async () => {
    if (!configPortfolio.trim() || !configInstrument.trim()) {
      toast.push("Portfolio and instrument ids are required.", "error");
      return;
    }
    if (!configTarget.trim() && !configTrailing.trim()) {
      toast.push("Enter a target price, a trailing stop percentage, or both.", "error");
      return;
    }
    setBusy("configure");
    try {
      const result = await apiPut(
        `/market/autonomy/exits/configurations/${encodeURIComponent(configPortfolio.trim())}/${encodeURIComponent(configInstrument.trim())}`,
        {
          ...(configTarget.trim() ? { target_price: configTarget.trim() } : {}),
          ...(configTrailing.trim() ? { trailing_stop_percent: configTrailing.trim() } : {}),
        },
        undefined,
        { operatorToken },
      );
      toast.push(
        `Exit configuration saved as v${result?.configuration?.version ?? "?"}. No order was created.`,
        "success",
      );
      exitConfigurations.refresh();
      exitPositions.refresh();
      exits.refresh();
    } catch (error: any) {
      toast.push(
        error?.detail?.message || error?.detail || "The exit configuration was refused.",
        "error",
      );
    } finally {
      setBusy("");
    }
  };

  const removeExitConfiguration = async (row: any) => {
    setBusy(`remove-${row.config_id}`);
    try {
      await apiDelete(
        `/market/autonomy/exits/configurations/${encodeURIComponent(row.portfolio_id)}/${encodeURIComponent(row.instrument_id)}`,
        undefined,
        { operatorToken },
      );
      toast.push("Target/trailing configuration removed. Stop-loss supervision remains active.", "success");
      exitConfigurations.refresh();
      exitPositions.refresh();
      exits.refresh();
    } catch (error: any) {
      toast.push(error?.detail?.message || error?.detail || "The configuration could not be removed.", "error");
    } finally {
      setBusy("");
    }
  };

  const runExitSupervision = async () => {
    setBusy("exits");
    try {
      const result = await apiPost(
        "/market/autonomy/exits/run",
        { trigger: "manual" },
        undefined,
        { operatorToken },
      );
      toast.push(
        `Exit supervision ${result?.status || "completed"}: ${result?.scanned ?? 0} scanned, ${result?.triggered ?? 0} exit thresholds reached, ${result?.submitted ?? 0} submitted.`,
        result?.status === "failed" ? "error" : "success",
      );
      exits.refresh();
      exitPositions.refresh();
      exitLearning.refresh();
      executions.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(
        error?.detail?.message || error?.detail || "The protective-exit cycle was refused.",
        "error",
      );
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="page">
      <PageHeader
        eyebrow="System / Autonomy"
        title={<>A shell with a <em>brake, not an engine.</em></>}
        description="AT-01 owns the lease and mode; AT-02 supplies verified-session monitoring; AT-03 evaluates existing NM-09 strategies and records NM-08 risk decisions; AT-04 forwards risk-approved entries through NM-10; AT-05 supervises stop-loss, target and trailing exits and records advisory entry-to-exit learning evidence. No live broker, real-money order or direct financial mutation exists."
        meta={
          <>
            <span className="meta-item"><ShieldCheck size={13} /> Paper only</span>
            <span className="meta-item"><Activity size={13} /> {title(mode)} · {title(runtimeState)}</span>
            <span className="meta-item"><TimerReset size={13} /> Lease {lease.held ? "held" : "not held"}</span>
          </>
        }
        actions={
          <Button variant="ghost" icon={<RefreshCw size={14} />} onClick={status.refresh}>
            Refresh
          </Button>
        }
      />

      {status.error && <ErrorBlock message={status.error.detail} onRetry={status.refresh} />}
      {!paper.initialised && (
        <Banner tone="warn" title="Paper execution is not initialised">
          AT-04 opens with the NovaMarket runtime. A <code>risk_approved</code> proposal is still only a decision: nothing is submitted until an operator runs an execution cycle in <code>autonomous</code> mode and NM-10 accepts the paper order.
        </Banner>
      )}
      {paper.initialised && mode !== "autonomous" && (
        <Banner tone="warn" title="Paper forwarding is blocked outside autonomous mode">
          The AT-04 journal and NM-10 execution state remain readable, but no new risk-approved proposal can be forwarded while the runtime is <code>{mode}</code>. Monitoring may assess risk; paused and disabled modes cannot open new entries.
        </Banner>
      )}
      {!configured && (
        <Banner tone="bad" title="Operator permission is not configured">
          Set <code>NOVACONTROL_AUTONOMY_OPERATOR_TOKEN</code> on the server to a value distinct from the ordinary API token. Until then, every autonomy control fails closed.
        </Banner>
      )}
      {configured && !authorized && (
        <Banner tone="warn" title="Operator permission not presented">
          This session can read status with its ordinary API token, but it cannot change autonomy state. Enter the dedicated operator token below; it is held in this page's memory only and is not stored.
        </Banner>
      )}

      <div className="metric-grid metric-grid-four">
        <Metric label="Mode" value={<StatusBadge value={mode} />} help="Persisted operating mode" tone={mode === "disabled" ? "neutral" : "warn"} icon={<Power size={16} />} />
        <Metric label="Runtime" value={<StatusBadge value={runtimeState} />} help={health.state ? `Health ${title(health.state)}` : "Awaiting status"} tone={health.state === "ok" ? "good" : "warn"} icon={<Activity size={16} />} />
        <Metric label="Lease" value={lease.held ? "Held" : "Not held"} help={lease.expires_at ? `Expires ${timeAgo(lease.expires_at)}` : "No live ownership"} tone={lease.conflict ? "bad" : lease.held ? "good" : "neutral"} icon={<KeyRound size={16} />} />
        <Metric label="Paper execution" value={paper.orders_possible ? "NM-10" : "Absent"} help="Paper orders only; live trading stays false" tone={paper.orders_possible ? "warn" : "bad"} icon={<ListChecks size={16} />} />
      </div>

      {lease.conflict && (
        <Banner tone="bad" title="Another runtime owns the lease">
          {lease.owner ? `Owner ${lease.owner}` : "A foreign owner"} holds the durable lease. This process reports degraded and cannot start a duplicate runtime.
        </Banner>
      )}

      <div className="dashboard-grid">
        <Card>
          <CardHeader
            title="Operator permission"
            subtitle="Separate from the ordinary API bearer token"
            action={<Badge tone={authorized ? "good" : configured ? "warn" : "bad"}>{authorized ? "Verified" : configured ? "Not presented" : "Not configured"}</Badge>}
          />
          <div className="settings-fields">
            <Field label="Autonomy operator token" hint="Sent as X-Autonomy-Operator-Token. Held in memory for this page; never written to local or session storage.">
              <TextInput
                type="password"
                value={operatorToken}
                onChange={(event) => setOperatorToken(event.target.value)}
                placeholder="Paste the dedicated operator token"
                autoComplete="off"
              />
            </Field>
            <div className="row-actions">
              <Badge tone={controlsEnabled ? "good" : "neutral"}>{controlsEnabled ? "Controls enabled" : "Controls locked"}</Badge>
              <Button size="sm" variant="ghost" onClick={() => setOperatorToken("")} disabled={!operatorToken}>Forget token</Button>
            </div>
          </div>
        </Card>

        <Card>
          <CardHeader
            title="Runtime controls"
            subtitle="Server-enforced; UI visibility is not authorization"
            action={<StatusBadge value={runtimeState} />}
          />
          {status.loading && !status.data ? (
            <LoadingBlock rows={3} label="Reading the autonomy runtime…" />
          ) : (
            <div className="settings-fields">
              <div className="row-actions">
                {mode === "disabled" && (
                  <Button variant="primary" icon={<Play size={14} />} loading={busy === "start"} disabled={!controlsEnabled} onClick={() => control("start", { mode: "monitoring", reason: "operator start from Autonomy page" })}>
                    Start monitoring
                  </Button>
                )}
                {mode === "paused" && (
                  <Button variant="primary" icon={<Play size={14} />} loading={busy === "resume"} disabled={!controlsEnabled} onClick={() => control("resume", { mode: "monitoring", reason: "operator resume from Autonomy page" })}>
                    Resume monitoring
                  </Button>
                )}
                {(mode === "monitoring" || mode === "autonomous") && (
                  <Button variant="secondary" icon={<Pause size={14} />} loading={busy === "pause"} disabled={!controlsEnabled} onClick={() => control("pause", { reason: "operator pause from Autonomy page" })}>
                    Pause
                  </Button>
                )}
                {mode !== "disabled" && (
                  <Button variant="danger" icon={<Power size={14} />} disabled={!controlsEnabled} onClick={() => setConfirmStop(true)}>
                    Stop and release lease
                  </Button>
                )}
              </div>
              <p className="muted">
                {mode === "autonomous"
                  ? "Autonomous mode can forward only AT-03 entries with a valid NM-08 approval. AT-04 sends them to NM-10 one cycle at a time. AT-05 separately supervises stop-loss, target and trailing exits through the same NM-08 and NM-10 authorities; no live broker exists."
                  : "Monitoring runs session-gated discovery (AT-02) and records NM-08 decisions (AT-03), but cannot forward orders. Autonomous mode enables AT-04 paper-order forwarding through NM-10; paused mode blocks new entries."}
              </p>
              <div className="contract-list">
                <div><CheckCircle2 size={15} /><span>One active runtime lease</span></div>
                <div><CheckCircle2 size={15} /><span>One active autonomy cycle at a time</span></div>
                <div><CheckCircle2 size={15} /><span>Restart comes up disabled; no automatic resume</span></div>
              </div>
            </div>
          )}
        </Card>
      </div>

      <div className="dashboard-grid">
        <Card>
          <CardHeader title="Lease and ownership" subtitle="Durable single-owner state" action={<Badge tone={lease.valid ? "good" : "warn"}>{lease.valid ? "Valid" : "Invalid"}</Badge>} />
          <dl className="key-value-list">
            <KeyValue label="Owner" mono>{lease.owner || "—"}</KeyValue>
            <KeyValue label="Expires">{lease.expires_at ? dateTime(lease.expires_at) : "—"}</KeyValue>
            <KeyValue label="Runtime owner" mono>{data.owner_id || "—"}</KeyValue>
            <KeyValue label="Recovery required"><StatusBadge value={data.recovery_required ? "required" : "no"} /></KeyValue>
            <KeyValue label="Active cycle" mono>{data.active_cycle?.cycle_id || "None"}</KeyValue>
            <KeyValue label="Last reason">{data.last_reason || data.last_error || "—"}</KeyValue>
          </dl>
        </Card>

        <Card>
          <CardHeader title="Safety boundaries" subtitle="Non-negotiable for this milestone" action={<Badge tone="good">Paper only</Badge>} />
          <div className="contract-list">
            <div><CheckCircle2 size={15} /><span>paper_only = {String(data.paper_only ?? true)}</span></div>
            <div><CheckCircle2 size={15} /><span>live_trading = {String(data.live_trading ?? false)}</span></div>
            <div><CheckCircle2 size={15} /><span>broker_connected = false</span></div>
            <div><CheckCircle2 size={15} /><span>paper orders possible through NM-10 = {String(paper.orders_possible ?? true)}</span></div>
            <div><CheckCircle2 size={15} /><span>live_orders_possible = false</span></div>
            <div><CheckCircle2 size={15} /><span>Paused mode keeps protective stop-loss, target and trailing exits and reconciliation active</span></div>
            <div><CheckCircle2 size={15} /><span>Learning promotion not implemented</span></div>
          </div>
          <div className="inline-note">
            <strong>Paper pipeline: {reliabilityData.health?.state === "ok" ? "healthy" : "needs attention"}</strong>
            <p>AT-02 discovers and monitors instruments; AT-03 records NM-08 decisions; AT-04 forwards only risk-approved entries to NM-10 paper execution; AT-05 supervises stop-loss, target and trailing exits through the same engines. No broker, live order, real-money path or direct ledger write exists.</p>
          </div>
        </Card>
      </div>

      <Card>
        <CardHeader
          title="Pipeline reliability and recovery"
          subtitle="AT-06 · authoritative runtime, scheduler, lease, reconciliation and failure domains"
          action={
            <Badge tone={reliabilityHealth.state === "ok" ? "good" : reliability.data ? "warn" : "neutral"}>
              {reliability.data ? title(reliabilityHealth.state || "unknown") : "Reading"}
            </Badge>
          }
        />
        {reliability.loading && !reliability.data ? (
          <LoadingBlock rows={4} label="Reading the pipeline reliability report…" />
        ) : reliability.error && !reliability.data ? (
          <ErrorBlock message={reliability.error.detail} onRetry={reliability.refresh} />
        ) : (
          <>
            {reliability.error && (
              <Banner tone="warn" title="Reliability refresh failed">
                {reliability.error.detail} The last authoritative report remains visible below.
              </Banner>
            )}
            <div className="metric-grid metric-grid-four">
              <Metric label="Pipeline health" value={<StatusBadge value={reliabilityHealth.state || "unknown"} />} help="AT-06 composed health" tone={reliabilityHealth.state === "ok" ? "good" : "warn"} icon={<Activity size={16} />} />
              <Metric label="Runtime" value={<StatusBadge value={reliabilityRuntime.runtime_state || "unknown"} />} help={title(reliabilityRuntime.mode || "disabled")} tone={reliabilityRuntime.runtime_state === "running" ? "good" : "warn"} icon={<Power size={16} />} />
              <Metric label="Scheduler" value={reliabilityRuntime.scheduler_running ? "Running" : "Idle"} help={title(reliabilityRuntime.scheduler_health?.state || "unknown")} tone={reliabilityRuntime.scheduler_health?.state === "ok" ? "good" : "warn"} icon={<CalendarClock size={16} />} />
              <Metric label="Lease" value={reliabilityRuntime.lease?.held ? "Held" : "Not held"} help={reliabilityRuntime.lease_expires_at ? `Expires ${timeAgo(reliabilityRuntime.lease_expires_at)}` : "No live ownership"} tone={reliabilityRuntime.lease?.conflict ? "bad" : reliabilityRuntime.lease?.held ? "good" : "neutral"} icon={<KeyRound size={16} />} />
            </div>
            <div className="dashboard-grid">
              <dl className="key-value-list">
                <KeyValue label="Mode"><StatusBadge value={reliabilityRuntime.mode || "disabled"} /></KeyValue>
                <KeyValue label="Desired mode">{title(reliabilityRuntime.desired_mode || "disabled")}</KeyValue>
                <KeyValue label="Started at">{reliabilityRuntime.started_at ? dateTime(reliabilityRuntime.started_at) : "—"}</KeyValue>
                <KeyValue label="Last heartbeat">{reliabilityRuntime.last_heartbeat ? dateTime(reliabilityRuntime.last_heartbeat) : "—"}</KeyValue>
                <KeyValue label="Lease expires">{reliabilityRuntime.lease_expires_at ? dateTime(reliabilityRuntime.lease_expires_at) : "—"}</KeyValue>
                <KeyValue label="Recovery required"><StatusBadge value={reliabilityRuntime.recovery_required ? "required" : "no"} /></KeyValue>
                <KeyValue label="Last error" mono>{reliabilityRuntime.last_error || "—"}</KeyValue>
              </dl>
              <dl className="key-value-list">
                <KeyValue label="Scheduler health"><StatusBadge value={reliabilityRuntime.scheduler_health?.state || "unknown"} /></KeyValue>
                <KeyValue label="Scheduler running">{reliabilityRuntime.scheduler_running ? "Yes" : "No"}</KeyValue>
                <KeyValue label="Last failure domain" mono>{reliabilityHealth.last_failure_domain || "—"}</KeyValue>
                <KeyValue label="Failure domains">
                  {Object.keys(reliabilityFailureDomains).length > 0
                    ? Object.entries(reliabilityFailureDomains).map(([domain, count]) => `${domain}: ${count}`).join(" · ")
                    : "None recorded"}
                </KeyValue>
                <KeyValue label="Unresolved records">{String(reliabilityRecovery.unresolved_count ?? reliabilityUnresolved.length)}</KeyValue>
                <KeyValue label="Pending reconciliation">{String(reliabilityFinancial.reconciliation_pending ?? 0)}</KeyValue>
                <KeyValue label="Pending orders">{String(reliabilityFinancial.pending_orders ?? 0)}</KeyValue>
              </dl>
            </div>
            <div className="dashboard-grid">
              <dl className="key-value-list">
                <div className="muted">Pipeline counts</div>
                <KeyValue label="Instruments discovered">{String(reliabilityPipeline.instruments_discovered ?? 0)}</KeyValue>
                <KeyValue label="Strategies evaluated">{String(reliabilityPipeline.strategies_evaluated ?? 0)}</KeyValue>
                <KeyValue label="Proposals">{String(reliabilityPipeline.proposals_created ?? 0)}</KeyValue>
                <KeyValue label="Risk assessments">{String(reliabilityPipeline.risk_assessments ?? 0)}</KeyValue>
                <KeyValue label="Paper orders">{String(reliabilityPipeline.paper_orders ?? 0)}</KeyValue>
                <KeyValue label="Fills">{String(reliabilityPipeline.fills ?? 0)}</KeyValue>
              </dl>
              <dl className="key-value-list">
                <div className="muted">Recovery and financial state</div>
                <KeyValue label="Last recovery">{reliabilityRecovery.last_action ? title(reliabilityRecovery.last_action) : "No recovery run yet"}</KeyValue>
                <KeyValue label="Recovered at">{reliabilityRecovery.last_at ? dateTime(reliabilityRecovery.last_at) : "—"}</KeyValue>
                <KeyValue label="Recovered / reconciled">{String(reliabilityRecovery.recovered ?? 0)} / {String(reliabilityRecovery.reconciled ?? 0)}</KeyValue>
                <KeyValue label="Auto-resumed"><StatusBadge value={reliabilityRecovery.auto_resumed ? "unexpected" : "off"} /></KeyValue>
                <KeyValue label="Pending orders preserved">{reliabilityRecovery.pending_orders_preserved ? "Yes" : "No"}</KeyValue>
                <KeyValue label="Supervised positions">{String(reliabilityFinancial.supervised_positions ?? 0)}</KeyValue>
                <KeyValue label="Learning incomplete">{String(reliabilityFinancial.learning_incomplete ?? 0)}</KeyValue>
              </dl>
            </div>
            {reliabilityReasons.length > 0 && (
              <div className="contract-list">
                {reliabilityReasons.map((reason: string) => (
                  <div key={reason}><AlertTriangle size={15} /><span>{reason}</span></div>
                ))}
              </div>
            )}
            <div className="key-value-list">
              <div className="muted">Unresolved reconciliation records</div>
              {reliabilityUnresolved.length > 0 ? (
                reliabilityUnresolved.map((record: any, index: number) => (
                  <KeyValue
                    key={record.order_id || record.execution_id || record.decision_id || `unresolved-${index}`}
                    label={record.order_id || record.execution_id || record.decision_id || `Unresolved ${index + 1}`}
                    mono
                  >
                    {record.reason || record.message || record.status || JSON.stringify(record)}
                  </KeyValue>
                ))
              ) : (
                <p className="muted">No unresolved reconciliation records.</p>
              )}
            </div>
            <div className="inline-note">
              <strong>Recovery reconciles; it never resumes.</strong>
              <p>Recovery closes the runtime lease, interrupts abandoned cycles, reconciles NM-10 paper orders and pending AT-05 exit decisions, and returns the runtime to disabled. Pending orders are preserved and unresolved work is reported rather than replayed.</p>
            </div>
            <div className="row-actions">
              <Button
                variant="primary"
                icon={<TimerReset size={14} />}
                loading={busy === "recover"}
                disabled={!controlsEnabled || reliabilityRecovery.available === false}
                onClick={() => setConfirmRecovery(true)}
              >
                Recover and disable
              </Button>
              <Button variant="ghost" icon={<RefreshCw size={14} />} onClick={reliability.refresh}>
                Refresh reliability
              </Button>
            </div>
            {!controlsEnabled && (
              <p className="muted">Recovery requires the dedicated operator token. Ordinary API authentication can read this report but cannot change runtime state.</p>
            )}
          </>
        )}
      </Card>

      <Card>
        <CardHeader title="Approved terminology and calendar coverage" subtitle="Owner decisions reflected in the runtime contract" />
        <div className="dashboard-grid">
          <div className="contract-list">
            <div><ShieldCheck size={15} /><span>short_term → day_trading</span></div>
            <div><ShieldCheck size={15} /><span>medium_term → swing_trading</span></div>
            <div><ShieldCheck size={15} /><span>long_term → long_term</span></div>
            <div><ShieldCheck size={15} /><span>Cycle concurrency: {String(data.one_cycle_at_a_time ?? true)}</span></div>
          </div>
          <div className="contract-list">
            <div><ShieldCheck size={15} /><span>Verified calendar years: {(data.calendar?.verified_years || []).join(", ") || "none"}</span></div>
            <div><ShieldCheck size={15} /><span>Verified through: {data.calendar?.verified_through ?? "—"}</span></div>
            <div><ShieldCheck size={15} /><span>Outside coverage: fail closed</span></div>
            <div><ShieldCheck size={15} /><span>No holiday extrapolation; weekdays are not assumed sessions</span></div>
          </div>
        </div>
        <p className="muted">Configuration version {config.config_version ?? "—"} · max cycle {config.max_cycle_seconds ?? "—"}s · lease {config.lease_seconds ?? "—"}s</p>
      </Card>

      <Card>
        <CardHeader
          title="Session-aware monitoring"
          subtitle="AT-02 gates discovery on the verified exchange calendar"
          action={
            <Badge tone={monitor.health?.state === "ok" ? "good" : monitor.initialised ? "warn" : "neutral"}>
              {monitor.health?.state ? title(monitor.health.state) : "Unknown"}
            </Badge>
          }
        />
        <div className="dashboard-grid">
          <dl className="key-value-list">
            <KeyValue label="Exchange" mono>{monitor.config?.exchange || session.exchange || "—"}</KeyValue>
            <KeyValue label="Exchange-local time">{session.local_time ? dateTime(session.local_time) : "—"}</KeyValue>
            <KeyValue label="Trading date">{session.trading_date || "—"}</KeyValue>
            <KeyValue label="Session">
              <StatusBadge value={session.eligible ? "open" : session.verdict || "unknown"} />
            </KeyValue>
            <KeyValue label="Calendar">
              {session.calendar_verified
                ? `Verified · ${session.calendar_source || "exchange calendar"}`
                : "Unverified — monitoring fails closed"}
            </KeyValue>
            <KeyValue label="Next open">{session.next_open ? dateTime(session.next_open) : "—"}</KeyValue>
          </dl>
          <dl className="key-value-list">
            <KeyValue label="Scheduler">{monitor.scheduler?.running ? "Running" : "Idle"}</KeyValue>
            <KeyValue label="Interval">{monitor.config ? `${monitor.config.interval_seconds}s` : "—"}</KeyValue>
            <KeyValue label="Rotation">
              {monitor.rotation
                ? `${monitor.rotation.cursor} · batch ${monitor.rotation.batch_size} of ${monitor.rotation.cap}`
                : "—"}
            </KeyValue>
            <KeyValue label="Last attempt">
              {monitor.scheduler?.last_attempt_at ? dateTime(monitor.scheduler.last_attempt_at) : "—"}
            </KeyValue>
            <KeyValue label="Next due">
              {monitor.scheduler?.next_due_at ? dateTime(monitor.scheduler.next_due_at) : "—"}
            </KeyValue>
            <KeyValue label="Consecutive failures">{String(monitor.health?.consecutive_failures ?? 0)}</KeyValue>
            <KeyValue label="Data freshness">
              <StatusBadge value={monitor.data_freshness?.state || "unavailable"} />
            </KeyValue>
          </dl>
        </div>
        {(monitor.health?.reasons || []).length > 0 && (
          <div className="contract-list">
            {(monitor.health.reasons || []).map((reason: string) => (
              <div key={reason}><AlertTriangle size={15} /><span>{reason}</span></div>
            ))}
          </div>
        )}
        <div className="row-actions">
          <Button
            variant="primary"
            icon={<ScanLine size={14} />}
            loading={busy === "monitor"}
            disabled={!controlsEnabled}
            onClick={runMonitoring}
          >
            Run monitoring cycle
          </Button>
          <Button variant="ghost" icon={<RefreshCw size={14} />} onClick={monitoring.refresh}>
            Refresh monitoring
          </Button>
        </div>
        <p className="muted">
          A manual run still passes the runtime-mode, lease and verified-session gates. It records a monitoring cycle only: no signal, proposal, order, fill, position or portfolio change.
        </p>
      </Card>

      <Card>
        <CardHeader
          title="Decision and risk pipeline"
          subtitle="NM-09 signal → AT-03 proposal → NM-08 risk decision; never an order"
          action={
            <Badge tone={decision.health?.state === "ok" ? "good" : decision.initialised ? "warn" : "neutral"}>
              {decision.health?.state ? title(decision.health.state) : "Not enabled"}
            </Badge>
          }
        />
        <div className="dashboard-grid">
          <dl className="key-value-list">
            <KeyValue label="Engine">NM-09 evaluate → NM-08 assess</KeyValue>
            <KeyValue label="Runtime mode">{decision.runtime_mode ? title(decision.runtime_mode) : "—"}</KeyValue>
            <KeyValue label="Monitoring cycle" mono>{decision.monitoring_cycle_id || "—"}</KeyValue>
            <KeyValue label="Last run">{lastDecisionRun.status ? title(lastDecisionRun.status) : "—"}</KeyValue>
            <KeyValue label="Last error" mono>{lastDecisionRun.error_code || decision.last_error_code || "—"}</KeyValue>
          </dl>
          <dl className="key-value-list">
            <KeyValue label="Evaluations">{String(decision.counts?.evaluations ?? 0)}</KeyValue>
            <KeyValue label="Signals">{String(decision.counts?.signals ?? 0)}</KeyValue>
            <KeyValue label="Proposals">{String(decision.counts?.proposals ?? 0)}</KeyValue>
            <KeyValue label="NM-08 decisions">{String(decision.counts?.risk_assessments ?? 0)}</KeyValue>
            <KeyValue label="Execution path"><StatusBadge value={paper.orders_possible ? "paper / NM-10" : "absent"} /></KeyValue>
          </dl>
        </div>
        {(decision.health?.reasons || []).length > 0 && (
          <div className="contract-list">
            {(decision.health.reasons || []).map((reason: string) => (
              <div key={reason}><ShieldAlert size={15} /><span>{reason}</span></div>
            ))}
          </div>
        )}
        {(decision.recent_proposals || []).length > 0 ? (
          <div className="key-value-list">
            {(decision.recent_proposals || []).slice(0, 8).map((proposal: any) => (
              <KeyValue
                key={proposal.proposal_id}
                label={`${proposal.instrument_id || "—"} · ${proposal.side || "—"} · ${proposal.status || "unknown"}`}
              >
                {proposal.strategy_id || "—"} v{proposal.strategy_version || "—"} · notional {proposal.notional || "—"} NC · risk {proposal.risk_outcome || "pending"}
                {proposal.risk_error ? ` · ${proposal.risk_error}` : ""}
                {proposal.risk_restrictions?.length ? ` · restricted: ${proposal.risk_restrictions.join(", ")}` : ""}
              </KeyValue>
            ))}
          </div>
        ) : (
          <p className="muted">No decision proposals recorded yet. A risk approval here is a decision record, not an executed trade.</p>
        )}
        {decisionBlockedReason && (
          <p className="muted" role="status">
            <strong>Decision run unavailable.</strong> {decisionBlockedReason}
          </p>
        )}
        <div className="row-actions">
          <Button
            variant="primary"
            icon={<Workflow size={14} />}
            loading={busy === "decision"}
            disabled={!controlsEnabled || Boolean(decisionBlockedReason)}
            onClick={runDecision}
          >
            Run decision pipeline
          </Button>
          <Button variant="ghost" icon={<RefreshCw size={14} />} onClick={decisions.refresh}>
            Refresh decisions
          </Button>
        </div>
        <p className="muted">
          This button evaluates existing active strategies against the latest completed, session-eligible AT-02 cycle and submits each eligible intent to NM-08. An NM-08 approval is shown as a decision, never as an order, fill or completed trade.
        </p>
      </Card>

      <Card>
        <CardHeader
          title="Autonomous paper execution"
          subtitle="AT-04 · risk-approved proposal → durable intent → NM-10 paper order → fill and reconciliation"
          action={
            <Badge tone={paperHealth.state === "ok" ? "good" : paper.initialised ? "warn" : "neutral"}>
              {paper.initialised ? title(paperHealth.state || "unknown") : "Not enabled"}
            </Badge>
          }
        />
        <div className="dashboard-grid">
          <dl className="key-value-list">
            <KeyValue label="Execution engine">NM-10 paper orders only</KeyValue>
            <KeyValue label="Risk authority">NM-08 approval required</KeyValue>
            <KeyValue label="Runtime gate">
              <StatusBadge value={paper.runtime_mode || mode} />
            </KeyValue>
            <KeyValue label="Lease">{paper.lease_valid ? "Valid" : "Invalid"}</KeyValue>
            <KeyValue label="Concurrency">{paper.one_cycle_at_a_time ? "One cycle at a time" : "Unverified"}</KeyValue>
            <KeyValue label="Live trading">{paper.live_trading ? "ENABLED (unexpected)" : "Disabled"}</KeyValue>
          </dl>
          <dl className="key-value-list">
            <KeyValue label="Execution intents">{String(paperCounts.intents ?? 0)}</KeyValue>
            <KeyValue label="Submitted">{String(paperCounts.submitted ?? 0)}</KeyValue>
            <KeyValue label="Partially filled">{String(paperCounts.partially_filled ?? 0)}</KeyValue>
            <KeyValue label="Filled">{String(paperCounts.filled ?? 0)}</KeyValue>
            <KeyValue label="Rejected">{String(paperCounts.rejected ?? 0)}</KeyValue>
            <KeyValue label="Reconciliation required">{String(paperCounts.reconciliation_required ?? 0)}</KeyValue>
            <KeyValue label="Journal events">{String(paper.journal?.events ?? 0)}</KeyValue>
          </dl>
        </div>
        {(paperHealth.reasons || []).length > 0 && (
          <div className="contract-list">
            {(paperHealth.reasons || []).map((reason: string) => (
              <div key={reason}><AlertTriangle size={15} /><span>{reason}</span></div>
            ))}
          </div>
        )}
        {lastExecution.execution_id ? (
          <div className="dashboard-grid">
            <dl className="key-value-list">
              <KeyValue label="Last execution" mono>{lastExecution.execution_id}</KeyValue>
              <KeyValue label="Proposal" mono>{lastExecution.proposal_id || "—"}</KeyValue>
              <KeyValue label="Status"><StatusBadge value={lastExecution.status || "unknown"} /></KeyValue>
              <KeyValue label="Order status"><StatusBadge value={lastExecution.order_status || "not submitted"} /></KeyValue>
              <KeyValue label="Authoritative order" mono>{lastExecution.order_id || "—"}</KeyValue>
              <KeyValue label="Reconciliation"><StatusBadge value={lastExecution.reconciliation_status || "pending"} /></KeyValue>
              <KeyValue label="Failure code" mono>{lastExecution.failure_code || "—"}</KeyValue>
            </dl>
            <dl className="key-value-list">
              <KeyValue label="Instrument" mono>{lastExecution.instrument_id || "—"}</KeyValue>
              <KeyValue label="Side">{lastExecution.side || "—"}</KeyValue>
              <KeyValue label="Filled / remaining">{lastExecution.filled_quantity || "0"} / {lastExecution.remaining_quantity || "—"}</KeyValue>
              <KeyValue label="Average paper price">{lastExecution.average_price || "—"}</KeyValue>
              <KeyValue label="Paper fees">{lastExecution.total_fees || "0"} INR</KeyValue>
              <KeyValue label="Fills">{String((paper.last_detail?.fills || []).length)}</KeyValue>
              <KeyValue label="Failure reason">{lastExecution.failure_reason || "—"}</KeyValue>
            </dl>
          </div>
        ) : (
          <p className="muted">No autonomous execution intent has been recorded. A risk-approved proposal is not an order until AT-04 persists an intent and NM-10 accepts it.</p>
        )}
        {(paper.last_detail?.fills || []).length > 0 && (
          <div className="key-value-list">
            <div className="muted">Authoritative NM-10 fill history for the latest execution</div>
            {(paper.last_detail.fills || []).map((fill: any) => (
              <KeyValue key={fill.fill_id} label={`${fill.fill_id} · ${fill.quantity} @ ${fill.price}`}>
                {fill.executed_at ? `${dateTime(fill.executed_at)} · ` : ""}fees {fill.total_fees || "0"} INR · order {fill.order_id || "—"}
              </KeyValue>
            ))}
          </div>
        )}
        {recentExecutions.length > 0 && (
          <div className="key-value-list">
            {recentExecutions.slice(0, 8).map((execution: any) => (
              <KeyValue
                key={execution.execution_id}
                label={`${execution.instrument_id || "—"} · ${execution.side || "—"} · ${execution.status || "unknown"}`}
              >
                <span className="mono">{execution.order_id || "no order"}</span> · filled {execution.filled_quantity || "0"} · remaining {execution.remaining_quantity || "—"} · reconciliation {execution.reconciliation_status || "pending"}
                {execution.failure_code ? ` · ${execution.failure_code}` : ""}
                {execution.updated_at ? ` · ${dateTime(execution.updated_at)}` : ""}
              </KeyValue>
            ))}
          </div>
        )}
        <div className="row-actions">
          <Button
            variant="primary"
            icon={<ArrowRight size={14} />}
            loading={busy === "execute"}
            disabled={!controlsEnabled || mode !== "autonomous" || !paper.lease_valid}
            onClick={runExecution}
          >
            Run paper execution
          </Button>
          <Button variant="ghost" icon={<RefreshCw size={14} />} onClick={executions.refresh}>
            Refresh executions
          </Button>
        </div>
        <p className="muted">
          Only AT-03 proposals with a verified NM-08 approval can be forwarded. AT-04 records the intent before calling NM-10, reuses the same idempotency key on recovery, and never treats an accepted or partially filled order as a completed trade. Order IDs, prices, fills and fees come from NM-10 records; they are never fabricated here.
        </p>
      </Card>

      <Card>
        <CardHeader
          title="Protective exit supervision"
          subtitle="AT-05 · existing position → stop-loss, target or trailing trigger → NM-08 → NM-10 paper exit → reconciliation → advisory evidence"
          action={
            <Badge tone={exitData.mode === "paused" ? "warn" : exitData.initialised ? "good" : "neutral"}>
              {exitData.initialised ? title(exitData.mode || mode) : "Not enabled"}
            </Badge>
          }
        />
        {mode === "paused" && (
          <Banner tone="warn" title="New entries are paused; protective exits remain active">
            AT-03 and AT-04 cannot open new entries while paused. AT-05 still evaluates open positions, submits eligible NM-08-approved stop-loss, target and trailing exits through NM-10, and reconciles existing paper orders.
          </Banner>
        )}
        <div className="metric-grid metric-grid-four">
          <Metric label="Supervised" value={String(exitData.supervised_positions ?? 0)} help="Positions with durable exit state" icon={<ShieldCheck size={16} />} />
          <Metric label="Open exits" value={String(exitData.open_decisions ?? 0)} help="Triggered, approved or awaiting fills" tone={exitData.open_decisions ? "warn" : "neutral"} icon={<Workflow size={16} />} />
          <Metric label="Filled exits" value={String(exitCounts.filled ?? 0)} help="NM-10 fills reconciled" tone="good" icon={<CheckCircle2 size={16} />} />
          <Metric label="Last scan" value={exitCycle.completed_at ? timeAgo(exitCycle.completed_at) : "Never"} help={`${exitCycle.scanned ?? 0} positions · ${exitCycle.triggered ?? 0} triggers`} icon={<ScanLine size={16} />} />
        </div>
        <div className="dashboard-grid">
          <dl className="key-value-list">
            <KeyValue label="Risk authority">NM-08 · remove_position</KeyValue>
            <KeyValue label="Execution authority">NM-10 paper orders only</KeyValue>
            <KeyValue label="Stop-loss source">Horizon `stop_loss_fraction`</KeyValue>
            <KeyValue label="Target price">Configured per long position</KeyValue>
            <KeyValue label="Trailing stop">Configured percent of high-water mark</KeyValue>
            <KeyValue label="Learning">Advisory only · no promotion</KeyValue>
          </dl>
          <dl className="key-value-list">
            <KeyValue label="Last mode">{exitCycle.mode ? title(exitCycle.mode) : title(mode)}</KeyValue>
            <KeyValue label="Scanned / supervised">{String(exitCycle.scanned ?? 0)} / {String(exitCycle.supervised ?? 0)}</KeyValue>
            <KeyValue label="Triggered / submitted">{String(exitCycle.triggered ?? 0)} / {String(exitCycle.submitted ?? 0)}</KeyValue>
            <KeyValue label="Pending exits">{String(exitCycle.pending_exits ?? 0)}</KeyValue>
            <KeyValue label="Last result"><StatusBadge value={exitCycle.status || "not run"} /></KeyValue>
            <KeyValue label="Last error" mono>{exitData.last_error || exitCycle.skipped_reason || "—"}</KeyValue>
          </dl>
        </div>
        <div className="settings-fields">
          <div className="form-grid">
            <Field label="Portfolio id" hint="The authoritative paper book containing the open position.">
              <TextInput value={configPortfolio} onChange={(event) => setConfigPortfolio(event.target.value)} placeholder="pfl_…" />
            </Field>
            <Field label="Instrument id" hint="Canonical instrument id, for example XNSE:ALPHA.">
              <TextInput value={configInstrument} onChange={(event) => setConfigInstrument(event.target.value)} placeholder="XNSE:ALPHA" />
            </Field>
            <Field label="Target price" hint="Optional fixed price above the position average cost.">
              <TextInput inputMode="decimal" value={configTarget} onChange={(event) => setConfigTarget(event.target.value)} placeholder="e.g. 120.00" />
            </Field>
            <Field label="Trailing stop %" hint="Optional distance below the highest valid observed price.">
              <TextInput inputMode="decimal" value={configTrailing} onChange={(event) => setConfigTrailing(event.target.value)} placeholder="e.g. 5" />
            </Field>
          </div>
          <div className="row-actions">
            <Button variant="primary" icon={<ShieldCheck size={14} />} loading={busy === "configure"} disabled={!controlsEnabled} onClick={configureExit}>
              Save exit configuration
            </Button>
            <span className="muted">A configuration creates no order; the next supervision cycle evaluates it.</span>
          </div>
        </div>
        {exitConfigurationRows.length > 0 && (
          <div className="key-value-list">
            <div className="muted">Target and trailing configuration</div>
            {exitConfigurationRows.slice(0, 10).map((row: any) => (
              <KeyValue
                key={row.config_id}
                label={`${row.instrument_id || "—"} · ${title(row.trigger_status || "armed")} · v${row.version || 1}`}
              >
                target {row.target_price || "—"} · trailing {row.trailing_stop_percent || "—"}% · high-water {row.high_water_mark || "—"} · trigger {row.current_trailing_stop_price || "—"} · last {row.last_evaluated_price || "—"}
                {row.last_market_data_timestamp ? ` · ${dateTime(row.last_market_data_timestamp)}` : ""}
                <Button size="sm" variant="ghost" loading={busy === `remove-${row.config_id}`} disabled={!controlsEnabled} onClick={() => removeExitConfiguration(row)}>Remove</Button>
              </KeyValue>
            ))}
          </div>
        )}
        {supervisedPositions.length > 0 ? (
          <div className="key-value-list">
            <div className="muted">Position supervision</div>
            {supervisedPositions.slice(0, 10).map((position: any) => (
              <KeyValue
                key={`${position.portfolio_id}-${position.instrument_id}`}
                label={`${position.instrument_id || "—"} · ${title(position.horizon || "horizon")} · ${title(position.supervision_status || position.trigger_state || "monitoring")}`}
              >
                qty {position.quantity || "—"} · available {position.available_quantity || "—"} · entry {position.average_cost || "—"} · price {position.current_price || "—"} · stop {position.stop_loss_price || "—"} · target {position.target_price || "—"} · trailing {position.trailing_stop_price || "—"} · high-water {position.trailing_high_water || "—"}
                {position.trigger_condition ? ` · trigger ${position.trigger_condition}` : ""}
                {position.pending_exit_quantity ? ` · pending ${position.pending_exit_quantity}` : ""}
                {position.last_supervision_at ? ` · ${dateTime(position.last_supervision_at)}` : ""}
              </KeyValue>
            ))}
          </div>
        ) : (
          <p className="muted">No position has been through an AT-05 supervision cycle yet. A supervised position is read from the authoritative portfolio, never from a duplicate position store.</p>
        )}
        {learningRows.length > 0 ? (
          <div className="key-value-list">
            <div className="muted">Entry-to-exit learning evidence</div>
            {learningRows.slice(0, 8).map((evidence: any) => (
              <KeyValue
                key={evidence.evidence_id}
                label={`${evidence.instrument_id || "—"} · ${evidence.strategy_id || "strategy"} v${evidence.strategy_version || "—"} · ${title(evidence.status || "incomplete")}`}
              >
                entry {evidence.entry?.order_id || "missing"} → exit {evidence.exit?.order_id || "missing"} · realized {evidence.realized?.portfolio_realized_pnl || "—"} · missing {(evidence.missing_evidence || []).join(", ") || "none"}
                {evidence.learning_service?.recorded === false ? " · learning service kept it advisory/incomplete" : ""}
              </KeyValue>
            ))}
          </div>
        ) : (
          <p className="muted">No exit evidence yet. Completed linkage is shown only when the exit fill, authoritative accounting and ledger reconciliation are all present; partial evidence is labelled partial.</p>
        )}
        <div className="row-actions">
          <Button
            variant="primary"
            icon={<ShieldCheck size={14} />}
            loading={busy === "exits"}
            disabled={!controlsEnabled || mode === "disabled" || !lease.valid}
            onClick={runExitSupervision}
          >
            Run exit supervision
          </Button>
          <Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { exits.refresh(); exitPositions.refresh(); exitLearning.refresh(); }}>
            Refresh exits
          </Button>
        </div>
        <p className="muted">
          Monitoring evaluates and records triggers without submitting. Autonomous and paused modes may submit a risk-approved stop-loss, target or trailing exit through NM-10. Duplicate triggers reuse the durable decision, pending orders are reconciled before retry, and no exit can exceed the available position quantity. A trailing high-water mark only rises for a long position; stale or missing data never triggers an exit.
        </p>
      </Card>

      <Card>
        <CardHeader
          title="Monitoring cycles"
          subtitle="Persistent history; a deferred cycle records exactly why discovery did not run"
        />
        {monitoring.loading && !monitoring.data ? (
          <LoadingBlock rows={3} label="Reading monitoring cycles…" />
        ) : (monitor.recent_cycles || []).length === 0 ? (
          <p className="muted">No monitoring cycles recorded yet.</p>
        ) : (
          <div className="key-value-list">
            {(monitor.recent_cycles || []).map((cycle: any) => (
              <KeyValue
                key={cycle.cycle_id || cycle.started_at}
                label={`${title(cycle.status || "unknown")} · ${cycle.trading_date || "—"}`}
              >
                {cycle.exchange} · {cycle.processed_count} processed · {cycle.deferred_count} deferred · {cycle.rejected_count} rejected
                {cycle.error_code ? ` · ${cycle.error_code}` : ""}
                {cycle.started_at ? ` · ${dateTime(cycle.started_at)}` : ""}
              </KeyValue>
            ))}
          </div>
        )}
      </Card>

      <ConfirmDialog
        open={confirmRecovery}
        title="Recover autonomy and return to disabled?"
        description="Recovery releases the runtime lease, reconciles existing NM-10 paper orders and AT-05 exit decisions, and leaves the runtime disabled. It never resumes autonomy, cancels a submitted order or replays unresolved work."
        confirmLabel="Run recovery"
        tone="primary"
        busy={busy === "recover"}
        onConfirm={recoverRuntime}
        onClose={() => setConfirmRecovery(false)}
      />

      <ConfirmDialog
        open={confirmStop}
        title="Stop the autonomy shell?"
        description="The runtime releases its lease and returns to disabled. An already accepted NM-10 paper order is not cancelled or reversed; new autonomous entries stop. No live broker exists."
        confirmLabel="Stop and release"
        tone="danger"
        busy={busy === "stop"}
        onConfirm={() => control("stop", { reason: "operator confirmed stop from Autonomy page" })}
        onClose={() => setConfirmStop(false)}
      />
    </div>
  );
}
