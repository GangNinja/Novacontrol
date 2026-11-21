import { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Bell,
  Check,
  CheckCircle2,
  CircleAlert,
  Clock3,
  Database,
  Globe2,
  KeyRound,
  RefreshCw,
  Server,
  Settings2,
  ShieldCheck,
  SlidersHorizontal,
  Terminal,
  XCircle,
  Zap,
} from "lucide-react";
import { apiGet, apiPost, getToken, hasApiAccess, setToken } from "../api";
import { useApi, useDocumentTitle, useLocalState, navigate } from "../hooks";
import {
  arrayFrom,
  dateTime,
  money,
  num,
  shortId,
  timeAgo,
  title,
} from "../lib";
import {
  Badge,
  Banner,
  Button,
  Card,
  CardHeader,
  ConfirmDialog,
  DataTable,
  EmptyState,
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

function alertTone(severity: string): "bad" | "warn" | "info" {
  if (severity === "critical" || severity === "high") return "bad";
  if (severity === "warning" || severity === "medium") return "warn";
  return "info";
}

export function NotificationsPage() {
  useDocumentTitle("Notifications");
  const overview = useApi<any>("/market/overview", { refreshMs: 60_000 });
  const riskAlerts = useApi<any>("/market/risk/alerts?limit=100");
  const learningAlerts = useApi<any>("/market/learning/alerts?limit=100");
  const [filter, setFilter] = useState("all");
  const [dismissed, setDismissed] = useLocalState<string[]>("novamarket.dismissedNotifications", []);
  const items = useMemo(() => {
    const risk = arrayFrom(riskAlerts.data, "alerts").map((row) => ({ ...row, source: "Risk", severity: row.severity || "warning", href: "risk" }));
    const learning = arrayFrom(learningAlerts.data, "alerts").map((row) => ({ ...row, source: "Learning", severity: row.severity || "info", href: "learning" }));
    const activity = Array.isArray(overview.data?.activity) ? overview.data.activity.slice(0, 8).map((row: any, index: number) => ({ ...row, alert_id: `activity-${index}`, source: "Activity", severity: "info", message: row.detail || row.message || title(row.event_type || "activity"), href: "activity" })) : [];
    return [...risk, ...learning, ...activity].filter((item) => !dismissed.includes(item.alert_id)).sort((a, b) => String(b.created_at || b.at || "").localeCompare(String(a.created_at || a.at || "")));
  }, [riskAlerts.data, learningAlerts.data, overview.data, dismissed]);
  const visible = filter === "all" ? items : items.filter((item) => item.source.toLowerCase() === filter);
  const openCount = items.filter((item) => item.source !== "Activity" && String(item.status || "open").toLowerCase() === "open").length;
  return (
    <div className="page">
      <PageHeader eyebrow="System / Notifications" title={<>What needs attention, <em>nothing else.</em></>} description="Risk and learning alerts, plus meaningful activity. Notifications are derived from the backend; no unread count is invented."
        meta={<><span className="meta-item"><Bell size={13} /> {openCount} open</span><span className="meta-item"><Activity size={13} /> {items.length} current signals</span></>}
        actions={<><Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { riskAlerts.refresh(); learningAlerts.refresh(); overview.refresh(); }}>Refresh</Button>{dismissed.length > 0 && <Button variant="secondary" onClick={() => setDismissed([])}>Restore dismissed</Button>}</>}
      />
      <div className="filter-row"><div className="chip-row"><button type="button" className={filter === "all" ? "chip chip-active" : "chip"} onClick={() => setFilter("all")}>All</button><button type="button" className={filter === "risk" ? "chip chip-active" : "chip"} onClick={() => setFilter("risk")}>Risk</button><button type="button" className={filter === "learning" ? "chip chip-active" : "chip"} onClick={() => setFilter("learning")}>Learning</button><button type="button" className={filter === "activity" ? "chip chip-active" : "chip"} onClick={() => setFilter("activity")}>Activity</button></div><Badge tone="neutral">{visible.length} shown</Badge></div>
      {(riskAlerts.error || learningAlerts.error || overview.error) && <ErrorBlock message={(riskAlerts.error || learningAlerts.error || overview.error)?.detail || "Some notification sources are unavailable."} />}
      <Card><CardHeader title="Signal inbox" subtitle="Live alerts and recent committed activity" />{visible.length ? <div className="notification-list">{visible.map((item: any) => <div className="notification-item" key={item.alert_id || `${item.source}-${item.created_at || item.at}`}><span className={`alert-badge alert-${String(item.severity || "info").toLowerCase()}`}>{item.severity === "info" ? <Activity size={14} /> : <AlertTriangle size={14} />}</span><div className="notification-copy"><div><strong>{item.message || item.explanation || item.kind || "Notification"}</strong><Badge tone={alertTone(String(item.severity || "info"))}>{title(item.severity || "info")}</Badge></div><p>{item.source} · {timeAgo(item.created_at || item.at || item.timestamp)}</p></div><div className="row-actions"><Button size="sm" variant="ghost" onClick={() => navigate(item.href || "activity")}>Open</Button><Button size="sm" variant="quiet" onClick={() => setDismissed([...dismissed, item.alert_id || `${item.source}-${item.created_at}`])} aria-label="Dismiss"><XCircle size={13} /></Button></div></div>)}</div> : <EmptyState icon={<CheckCircle2 size={22} />} title="Nothing needs attention" description="No open risk alerts, learning alerts or recent activity match this filter." />}</Card>
    </div>
  );
}

export function ActivityPage() {
  useDocumentTitle("Activity and Audit");
  const overview = useApi<any>("/market/overview", { refreshMs: 60_000 });
  const [tab, setTab] = useState("activity");
  const [search, setSearch] = useState("");
  const activities = Array.isArray(overview.data?.activity) ? overview.data.activity : [];
  const audit = overview.data?.audit || {};
  const auditRows = arrayFrom(audit, "entries");
  const source = tab === "activity" ? activities : auditRows;
  const filtered = source.filter((row: any) => {
    const text = search.toLowerCase().trim();
    return !text || JSON.stringify(row).toLowerCase().includes(text);
  });
  return (
    <div className="page">
      <PageHeader eyebrow="System / Activity and Audit" title={<>A timeline you can <em>check.</em></>} description="Activity is the subsystem's published trail; audit rows come from the shared NovaControl audit logger. Both are read-only here."
        meta={<><span className="meta-item"><Activity size={13} /> {activities.length} activity rows</span><span className="meta-item"><ShieldCheck size={13} /> {audit.records ?? auditRows.length} audit records</span><span className="meta-item"><Clock3 size={13} /> {dateTime(overview.data?.generated_at)}</span></>}
        actions={<Button variant="ghost" icon={<RefreshCw size={14} />} onClick={overview.refresh}>Refresh</Button>}
      />
      {overview.error && <ErrorBlock message={overview.error.detail} onRetry={overview.refresh} />}
      <Card><div className="tabs-head"><div className="tabs"><button type="button" className={tab === "activity" ? "tab tab-active" : "tab"} onClick={() => setTab("activity")}>Activity</button><button type="button" className={tab === "audit" ? "tab tab-active" : "tab"} onClick={() => setTab("audit")}>Audit trail <span>{auditRows.length}</span></button></div><TextInput value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search timeline…" aria-label="Search timeline" /></div>{overview.loading ? <LoadingBlock rows={5} label="Reading the operational timeline…" /> : tab === "activity" ? (
        <div className="timeline">{filtered.map((row: any, index) => <div className="timeline-row" key={row.event_id || `${row.at || row.created_at}-${index}`}><span className="timeline-dot" /><div><strong>{row.detail || row.message || title(row.event_type || row.type || "Activity")}</strong><small>{title(row.event_type || row.type || "event")} · {timeAgo(row.at || row.created_at || row.timestamp)}</small></div>{row.correlation_id && <code>{shortId(row.correlation_id, 12)}</code>}</div>)}{!filtered.length && <EmptyState title="No activity rows" description="Activity appears when NovaMarket publishes a lifecycle or operation event." />}</div>
      ) : <DataTable rows={filtered} rowKey={(row, index) => row.audit_id || row.id || String(index)} empty={<EmptyState icon={<ShieldCheck size={22} />} title="No audit rows" description={audit.available === false ? audit.detail || "The audit logger is not attached." : "The audit query returned no NovaMarket rows."} />} columns={[{ key: "created_at", label: "When", render: (row) => dateTime(row.created_at || row.at || row.timestamp), sortable: true }, { key: "action", label: "Action", render: (row) => title(row.action || row.event || row.operation || "audit") }, { key: "source", label: "Source", render: (row) => row.source || "novamarket" }, { key: "detail", label: "Detail", render: (row) => <span className="clamp-cell">{row.detail || row.message || row.summary || "—"}</span> }, { key: "outcome", label: "Outcome", render: (row) => <StatusBadge value={row.outcome || row.status || "recorded"} /> }]} />}</Card>
      {audit.available === false && <Banner tone="warn" title="Audit sink unavailable">{audit.detail || "The workspace has no attached audit logger."}</Banner>}
    </div>
  );
}

export function HealthPage() {
  useDocumentTitle("System Health");
  const toast = useToast();
  const status = useApi<any>("/market/status", { refreshMs: 30_000 });
  const contracts = useApi<any>("/market/contracts");
  const [action, setAction] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const data = status.data || {};
  const health = data.health || {};
  const deps = Array.isArray(data.dependencies) ? data.dependencies : [];
  const caps = Array.isArray(data.capabilities) ? data.capabilities : [];
  const lifecycle = data.lifecycle || "unknown";
  const healthy = health.healthy ?? String(health.state).toLowerCase() === "ok";
  const changeLifecycle = async () => {
    if (!action) return;
    setSaving(true);
    try {
      await apiPost(`/market/${action}`, { reason: `operator ${action} from standalone settings` });
      toast.push(`NovaMarket ${action} request accepted.`, "success");
      setAction(null);
      status.refresh();
      contracts.refresh();
    } catch (error: any) {
      toast.push(error?.detail || `The ${action} request was refused.`, "error");
    } finally { setSaving(false); }
  };
  return (
    <div className="page">
      <PageHeader eyebrow="System / Health" title={<>Trust is a <em>measured state.</em></>} description="Lifecycle, health reasons, dependencies and capability verification come directly from the NovaMarket service. A missing probe is unknown, never promoted to green."
        meta={<><span className="meta-item"><i className={`health-dot health-${String(health.state || "unknown").toLowerCase()}`} /> {title(health.state || "unknown")}</span><span className="meta-item"><Server size={13} /> {title(lifecycle)}</span><span className="meta-item"><Database size={13} /> schema {data.schema_version || "—"}</span></>}
        actions={<><Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { status.refresh(); contracts.refresh(); }}>Refresh</Button>{lifecycle !== "running" ? <Button variant="primary" icon={<Zap size={14} />} onClick={() => setAction("enable")} disabled={lifecycle === "enabling"}>Enable NovaMarket</Button> : <Button variant="danger" icon={<XCircle size={14} />} onClick={() => setAction("disable")}>Disable</Button>}</>}
      />
      {status.error && <ErrorBlock message={status.error.detail} onRetry={status.refresh} />}
      <div className="metric-grid metric-grid-four">
        <Metric label="Health" value={<StatusBadge value={health.state || "unknown"} />} help={health.checked_at ? `Checked ${timeAgo(health.checked_at)}` : "No check timestamp"} tone={healthy ? "good" : "warn"} />
        <Metric label="Lifecycle" value={title(lifecycle)} help={data.enabled ? "Enabled" : "Not enabled"} icon={<Server size={16} />} />
        <Metric label="Dependencies" value={`${deps.filter((item) => item.available).length} / ${deps.length}`} help="Available declared" icon={<Database size={16} />} />
        <Metric label="Capabilities" value={caps.length} help="Registered records" icon={<SlidersHorizontal size={16} />} />
      </div>
      {health.reasons?.length > 0 && <Banner tone={healthy ? "info" : "warn"} title={healthy ? "Health notes" : "Health attention required"}>{health.reasons.join(" · ")}</Banner>}
      {data.config_error && <Banner tone="bad" title="Configuration error">{data.config_error}</Banner>}
      <div className="dashboard-grid">
        <Card><CardHeader title="Dependencies" subtitle="What the subsystem needs" />{deps.length ? <div className="dependency-list">{deps.map((item: any) => <div className="dependency-row" key={item.name}><span className={`health-dot health-${item.available ? "ok" : "degraded"}`} /><div><strong>{item.name}</strong><small>{item.detail || (item.available ? "Available" : "Unavailable")}</small></div><Badge tone={item.available ? "good" : item.required ? "bad" : "warn"}>{item.available ? "Available" : item.required ? "Required" : "Optional"}</Badge></div>)}</div> : <EmptyState title="No dependencies reported" />}<div className="health-actions"><Button size="sm" variant="secondary" onClick={() => navigate("settings")} icon={<Settings2 size={14} />}>Open settings</Button><Button size="sm" variant="ghost" onClick={() => navigate("activity")} icon={<Activity size={14} />}>Audit trail</Button></div></Card>
        <Card><CardHeader title="Lifecycle contract" subtitle="Operational state and safety" /><div className="contract-list"><div><CheckCircle2 size={15} /><span>Real-money execution: {data.permissions?.real_money_trading ? "not verified" : "disabled"}</span></div><div><CheckCircle2 size={15} /><span>Paper execution: {contracts.data?.execution?.implemented ? "implemented" : "not exposed"}</span></div><div><CheckCircle2 size={15} /><span>Shadow execution: {contracts.data?.shadow?.implemented ? "isolated" : "not exposed"}</span></div><div><CheckCircle2 size={15} /><span>Learning execution: {contracts.data?.learning?.implemented ? "no order gateway" : "not exposed"}</span></div></div><pre className="json-preview">{JSON.stringify({ lifecycle, enabled: data.enabled, operational: data.operational, schema: data.schema_version, contract: data.contract_version }, null, 2)}</pre></Card>
      </div>
      <Card><CardHeader title="Capability registry" subtitle="Availability is a property of this machine" action={<Badge tone="neutral">{caps.length}</Badge>} /><DataTable rows={caps} rowKey={(row) => row.capability_id || row.name || row.id} empty={<EmptyState title="No capability records" description="The service has not registered capabilities yet." />} columns={[{ key: "name", label: "Capability", render: (row) => row.name || row.capability_id || "—" }, { key: "operation", label: "Operation", render: (row) => row.operation || row.action || "—" }, { key: "availability", label: "Availability", render: (row) => <StatusBadge value={row.availability || row.status || (row.available ? "available" : "unknown")} /> }, { key: "verified_operation", label: "Verified by", render: (row) => row.verified_operation || row.evidence || "—" }, { key: "risk", label: "Risk", render: (row) => <Badge tone={String(row.risk).toLowerCase() === "high" ? "warn" : "neutral"}>{title(row.risk || "low")}</Badge> }]} /></Card>
      <ConfirmDialog open={!!action} title={`${action === "disable" ? "Disable" : "Enable"} NovaMarket?`} description={action === "disable" ? "The subsystem will stand down and withdraw its registered capabilities. Its persisted financial records remain intact." : "The subsystem will initialise, register capabilities and start its market-data lifecycle."} confirmLabel={action === "disable" ? "Disable subsystem" : "Enable subsystem"} tone={action === "disable" ? "danger" : "primary"} busy={saving} onConfirm={changeLifecycle} onClose={() => setAction(null)} />
    </div>
  );
}

function OperatorControls({ status, toast, onStop }: { status: any; toast: ReturnType<typeof useToast>; onStop: () => void }) {
  const config = status.data?.config || {};
  const notifications = status.data?.notifications || {};
  const paused = Boolean(config.paused);
  const [email, setEmail] = useState("");
  const [enabled, setEnabled] = useState(true);
  const [notifyOpen, setNotifyOpen] = useState(true);
  const [notifyClose, setNotifyClose] = useState(true);
  const [profit, setProfit] = useState("5");
  const [loss, setLoss] = useState("3");
  const [lead, setLead] = useState("10");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [pausing, setPausing] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [coverage, setCoverage] = useState("");

  useEffect(() => {
    setEmail(String(config.notification_email || ""));
    setEnabled(config.notifications_enabled !== false);
    setNotifyOpen(config.notify_market_open !== false);
    setNotifyClose(config.notify_market_close !== false);
    setProfit(String(config.profit_alert_pct ?? 5));
    setLoss(String(config.loss_alert_pct ?? 3));
    setLead(String(config.alert_lead_minutes ?? 10));
  }, [
    config.notification_email,
    config.notifications_enabled,
    config.notify_market_open,
    config.notify_market_close,
    config.profit_alert_pct,
    config.loss_alert_pct,
    config.alert_lead_minutes,
  ]);

  const save = async () => {
    setSaving(true);
    try {
      await apiPost("/market/config", {
        notification_email: email.trim(),
        notifications_enabled: enabled,
        notify_market_open: notifyOpen,
        notify_market_close: notifyClose,
        profit_alert_pct: Number(profit),
        loss_alert_pct: Number(loss),
        alert_lead_minutes: Number(lead),
      });
      toast.push("Notification settings saved.", "success");
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The notification settings were refused.", "error");
    } finally { setSaving(false); }
  };

  const sendTest = async () => {
    setTesting(true);
    try {
      const result = await apiPost<any>("/market/notifications/test", {});
      toast.push(result?.detail || "Test email delivered.", "success");
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The test email could not be delivered.", "error");
    } finally { setTesting(false); }
  };

  const togglePause = async () => {
    setPausing(true);
    try {
      await apiPost("/market/config", { paused: !paused });
      toast.push(paused ? "NovaMarket monitoring continued." : "NovaMarket monitoring paused. Records and the API remain available.", "success");
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The pause request failed.", "error");
    } finally { setPausing(false); }
  };

  const syncUniverse = async () => {
    setSyncing(true);
    try {
      const result = await apiPost<any>("/market/universe/sync", { include_bse: true });
      setCoverage(String(result?.coverage || ""));
      toast.push(`Universe synced: ${result?.registered ?? 0} listings registered.`, "success");
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The NSE/BSE universe could not be synced.", "error");
    } finally { setSyncing(false); }
  };

  return (
    <Card>
      <CardHeader
        title="Live market alerts"
        subtitle="Email on NSE/BSE open/close and per-stock thresholds"
        action={<Badge tone={notifications.smtp_configured ? "good" : "warn"}>{notifications.smtp_configured ? "SMTP ready" : "SMTP missing"}</Badge>}
      />
      {!notifications.smtp_configured && (
        <Banner tone="warn" title="Email transport needs server configuration">
          Set <code>NOVAMARKET_SMTP_HOST</code> and <code>NOVAMARKET_SMTP_FROM</code> (and user/password if required) in the backend environment. The recipient and thresholds below are saved in NovaMarket; no SMTP secret is sent to this browser.
        </Banner>
      )}
      <div className="settings-fields">
        <Field label="Notification email" hint="The only recipient for market-open, market-close and threshold alerts.">
          <TextInput type="email" value={email} onChange={(event) => setEmail(event.target.value)} placeholder="operator@example.com" autoComplete="email" />
        </Field>
        <label className="toggle-label"><input type="checkbox" checked={enabled} onChange={(event) => setEnabled(event.target.checked)} /> Email notifications enabled</label>
        <div className="form-grid">
          <Field label="Profit threshold %" hint="Per stock, against average cost.">
            <TextInput type="number" min="0.1" max="100" step="0.1" value={profit} onChange={(event) => setProfit(event.target.value)} />
          </Field>
          <Field label="Loss threshold %" hint="Positive number; a 3 means -3%.">
            <TextInput type="number" min="0.1" max="100" step="0.1" value={loss} onChange={(event) => setLoss(event.target.value)} />
          </Field>
          <Field label="Alert lead (minutes)" hint="5–30 minutes before the verified NSE/BSE open or close.">
            <TextInput type="number" min="5" max="30" step="1" value={lead} onChange={(event) => setLead(event.target.value)} />
          </Field>
        </div>
        <div className="row-actions">
          <label className="toggle-label"><input type="checkbox" checked={notifyOpen} onChange={(event) => setNotifyOpen(event.target.checked)} /> Market open</label>
          <label className="toggle-label"><input type="checkbox" checked={notifyClose} onChange={(event) => setNotifyClose(event.target.checked)} /> Market close</label>
        </div>
        <div className="row-actions">
          <Button variant="primary" onClick={save} loading={saving}>Save alerts</Button>
          <Button variant="secondary" onClick={sendTest} loading={testing}>Send test email</Button>
        </div>
      </div>
      <div className="settings-lifecycle">
        <div><span className="eyebrow">Continuous monitor</span><StatusBadge value={paused ? "paused" : status.data?.lifecycle || "unknown"} /></div>
        <div className="row-actions">
          <Button variant={paused ? "primary" : "secondary"} onClick={togglePause} loading={pausing}>{paused ? "Continue monitoring" : "Pause monitoring"}</Button>
          <Button variant="danger" onClick={onStop}>Stop NovaMarket</Button>
          <span className="muted">Pause and stop affect NovaMarket only; the API and this dashboard stay up.</span>
        </div>
      </div>
      <div className="settings-lifecycle">
        <div><span className="eyebrow">NSE/BSE catalogue</span><span className="muted">{coverage || `${status.data?.market_data?.instruments ?? "—"} instruments reported by the backend`}</span></div>
        <Button variant="secondary" onClick={syncUniverse} loading={syncing}>Sync public universe</Button>
      </div>
    </Card>
  );
}

export function SettingsPage() {
  useDocumentTitle("Settings");
  const toast = useToast();
  const status = useApi<any>("/market/status");
  const [tokenDraft, setTokenDraft] = useState("");
  const [savedToken, setSavedToken] = useState(Boolean(getToken()));
  const proxyAdmin = !savedToken && hasApiAccess();
  const [lifecycle, setLifecycle] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const apiOrigin = import.meta.env.VITE_API_BASE_URL || `${window.location.origin} (same origin / Vite proxy)`;
  const applyToken = () => {
    setToken(tokenDraft);
    setSavedToken(Boolean(tokenDraft.trim()));
    setTokenDraft("");
    toast.push(tokenDraft.trim() ? "Session token saved for this tab." : "Session token cleared.", "success");
    window.setTimeout(() => window.location.reload(), 350);
  };
  const changeLifecycle = async () => {
    if (!lifecycle) return;
    setSaving(true);
    try {
      await apiPost(`/market/${lifecycle}`, { reason: `operator ${lifecycle} from settings` });
      toast.push(`NovaMarket ${lifecycle} request accepted.`, "success");
      setLifecycle(null);
      status.refresh();
    } catch (error: any) { toast.push(error?.detail || "The lifecycle request failed.", "error"); } finally { setSaving(false); }
  };
  return (
    <div className="page">
      <PageHeader eyebrow="System / Settings" title={<>Connection and <em>operating boundaries.</em></>} description="The recipient, thresholds and pause state are persisted by the NovaMarket backend. SMTP credentials stay in the server environment; this browser never receives them." />
      <div className="settings-grid">
        <Card><CardHeader title="API connection" subtitle="The browser talks only to the NovaMarket API" action={<Badge tone={savedToken || proxyAdmin ? "good" : "neutral"}>{proxyAdmin ? "Local admin" : savedToken ? "Token present" : "No token"}</Badge>} /><div className="settings-fields"><KeyValue label="API origin" mono>{apiOrigin}</KeyValue><KeyValue label="Transport">{proxyAdmin ? "Local Vite admin proxy" : "Bearer token, session storage"}</KeyValue><KeyValue label="Live status"><StatusBadge value={status.data?.health?.state || (status.error ? "offline" : "unknown")} /></KeyValue><KeyValue label="Lifecycle">{title(status.data?.lifecycle || "unknown")}</KeyValue></div><div className="inline-note"><strong>Secrets stay out of the bundle.</strong><p>{proxyAdmin ? "The local Vite proxy supplies the loopback-only admin session. The credential stays in the dev server configuration, not in this browser or the bundle." : "The token is entered at runtime and held in this tab's session storage. It is never written to a source file, local storage or a chart."}</p></div><Field label="Session API token" hint={proxyAdmin ? "The local proxy already holds the loopback admin session; no token is needed here." : "Set NOVACONTROL_API_TOKEN on the backend; leave empty only for an unauthenticated local backend."}><div className="input-with-action"><TextInput type="password" value={tokenDraft} onChange={(event) => setTokenDraft(event.target.value)} placeholder={savedToken ? "Replace current token…" : "Paste token"} autoComplete="off" /><Button variant="primary" size="sm" onClick={applyToken}>Save</Button></div></Field>{savedToken && <Button variant="ghost" size="sm" onClick={() => { setToken(""); setSavedToken(false); toast.push("Session token cleared.", "success"); window.setTimeout(() => window.location.reload(), 300); }}>Clear session token</Button>}<Button variant="secondary" size="sm" icon={<KeyRound size={14} />} onClick={() => window.location.reload()}>Reconnect</Button></Card>
        <Card><CardHeader title="Operating boundaries" subtitle="Non-negotiable" /><div className="contract-list"><div><ShieldCheck size={15} /><span>Real-money trading is absent from the permission model.</span></div><div><ShieldCheck size={15} /><span>Paper, backtest and shadow books are deliberately separate.</span></div><div><ShieldCheck size={15} /><span>Risk approval cannot be bypassed from this interface.</span></div><div><ShieldCheck size={15} /><span>Learning promotion requires explicit governance.</span></div></div><div className="settings-lifecycle"><div><span className="eyebrow">Subsystem lifecycle</span><StatusBadge value={status.data?.lifecycle || "unknown"} /></div>          <div className="row-actions">{status.data?.lifecycle !== "running" ? <Button variant="primary" onClick={() => setLifecycle("enable")}>Start NovaMarket</Button> : <Button variant="danger" onClick={() => setLifecycle("disable")}>Stop NovaMarket</Button>}<Button variant="ghost" onClick={() => status.refresh()}>Refresh status</Button></div></div></Card>
        <OperatorControls status={status} toast={toast} onStop={() => setLifecycle("disable")} />
      </div>
      <Card><CardHeader title="Frontend deployment" subtitle="For operators" /><div className="deployment-grid"><div><Globe2 size={16} /><strong>Build</strong><code>npm run build</code><span>Static Vite output in <code>dist/</code>.</span></div><div><Terminal size={16} /><strong>Development</strong><code>npm run dev</code><span>Vite proxies <code>/market</code> to the backend origin.</span></div><div><ShieldCheck size={16} /><strong>Cross-origin</strong><code>NOVAMARKET_CORS_ORIGINS</code><span>Set an exact allowlist on the backend; never use a wildcard with authorization.</span></div></div></Card>
      <ConfirmDialog open={!!lifecycle} title={`${lifecycle === "disable" ? "Stop" : "Start"} NovaMarket?`} description={lifecycle === "disable" ? "NovaMarket will stand down and withdraw its capabilities; the API and dashboard stay running. Existing records remain persisted." : "NovaMarket will initialise and resume its continuous monitoring lifecycle."} confirmLabel={lifecycle === "disable" ? "Stop NovaMarket" : "Start NovaMarket"} tone={lifecycle === "disable" ? "danger" : "primary"} busy={saving} onConfirm={changeLifecycle} onClose={() => setLifecycle(null)} />
    </div>
  );
}
