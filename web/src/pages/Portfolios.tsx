import { useEffect, useMemo, useState } from "react";
import {
  ArrowRight,
  ArrowUpRight,
  BarChart3,
  BriefcaseBusiness,
  CheckCircle2,
  ChevronRight,
  CircleDollarSign,
  Clock3,
  FilePlus2,
  Gauge,
  History,
  Info,
  Landmark,
  Pause,
  Play,
  Plus,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  WalletCards,
} from "lucide-react";
import { apiPost } from "../api";
import { useApi, useDocumentTitle, navigate } from "../hooks";
import {
  arrayFrom,
  dateTime,
  horizonApi,
  horizonSlug,
  money,
  num,
  pct,
  pctUnsigned,
  quantity,
  shortDate,
  shortId,
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
  ConfirmDialog,
  DataTable,
  Delta,
  EmptyState,
  ErrorBlock,
  Field,
  LoadingBlock,
  Metric,
  Modal,
  Money,
  PageHeader,
  SelectInput,
  StatusBadge,
  Tabs,
  TextInput,
  useToast,
} from "../components/ui";
import { AllocationBars, DonutChart, LineChart } from "../components/charts";

const HORIZON_META: Record<string, { label: string; description: string; color: string }> = {
  short: { label: "Day Trading", description: "Intraday signals with tighter turnover and higher cash discipline.", color: "var(--accent)" },
  medium: { label: "Swing Trading", description: "A risk-budgeted multi-day balance across validated forecasts and sectors.", color: "var(--blue)" },
  long: { label: "Long-Term", description: "Inverse-volatility allocation built for slower compounding.", color: "var(--gold)" },
};

const COLORS = ["var(--accent)", "var(--blue)", "var(--gold)", "var(--violet)", "var(--rose)", "var(--green)"];

function statusAction(status: string): string | null {
  if (["active", "under_review", "rebalancing"].includes(status)) return "pause";
  if (["paused", "suspended"].includes(status)) return "resume";
  return null;
}

function PortfolioTabs({ active, onChange }: { active: string; onChange: (value: string) => void }) {
  return <Tabs tabs={[{ id: "holdings", label: "Holdings" }, { id: "performance", label: "Performance" }, { id: "rebalance", label: "Rebalance" }, { id: "history", label: "History" }]} active={active} onChange={onChange} />;
}

export function PortfoliosPage({ horizon }: { horizon: string }) {
  const meta = HORIZON_META[horizon] || HORIZON_META.short;
  useDocumentTitle(`${meta.label} Portfolio`);
  const toast = useToast();
  const apiHorizon = horizonApi(horizon);
  const portfolios = useApi<any>(`/market/portfolios?horizon=${encodeURIComponent(apiHorizon)}&limit=30`);
  const wallets = useApi<any>("/market/wallets?limit=50");
  const [selectedId, setSelectedId] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState("");
  const [capital, setCapital] = useState("100000");
  const [fundingWalletId, setFundingWalletId] = useState("");
  const [saving, setSaving] = useState(false);
  const [tab, setTab] = useState("holdings");
  const [rebalanceOpen, setRebalanceOpen] = useState(false);
  const [statusTarget, setStatusTarget] = useState<string | null>(null);
  const [pendingProposal, setPendingProposal] = useState<any | null>(null);
  const rows = arrayFrom(portfolios.data, "portfolios");
  const current = rows.find((row) => row.portfolio_id === selectedId) || rows[0];
  const detail = useApi<any>(current ? `/market/portfolios/${encodeURIComponent(current.portfolio_id)}` : null);
  const valuations = useApi<any>(current ? `/market/portfolios/${encodeURIComponent(current.portfolio_id)}/valuations?limit=200` : null);
  const performance = useApi<any>(current ? `/market/portfolios/${encodeURIComponent(current.portfolio_id)}/performance` : null);
  const proposals = useApi<any>(current ? `/market/portfolios/${encodeURIComponent(current.portfolio_id)}/proposals?limit=50` : null);
  const walletRows = arrayFrom(wallets.data, "wallets");

  useEffect(() => {
    if (!selectedId && rows.length) setSelectedId(rows[0].portfolio_id);
  }, [rows, selectedId]);

  useEffect(() => {
    if (!fundingWalletId && walletRows.length) setFundingWalletId(walletRows[0].wallet_id);
  }, [walletRows, fundingWalletId]);

  const valuation = detail.data?.valuation || {};
  const portfolio = detail.data?.portfolio || current || {};
  const holdings = arrayFrom(detail.data, "holdings");
  const provenance = detail.data?.provenance;
  const snapshots = useMemo(() => valuationPoints(valuations.data).map((point) => ({ ...point, label: shortDate(point.label) })), [valuations.data]);
  const allocation = holdings.map((holding, index) => ({ label: holding.instrument_id || "Holding", value: num(holding.market_value), color: COLORS[index % COLORS.length] })).filter((item) => item.value > 0);
  const metrics = performance.data?.metrics || {};
  const nextStatusAction = statusAction(String(portfolio.status || ""));

  const create = async () => {
    setSaving(true);
    try {
      const result = await apiPost<any>("/market/portfolios", { horizon: apiHorizon, name, funding_wallet_id: fundingWalletId, capital });
      toast.push("Portfolio created and funded from the ledger.", "success");
      setCreateOpen(false);
      setName("");
      portfolios.refresh();
      if (result?.portfolio?.portfolio_id) setSelectedId(result.portfolio.portfolio_id);
    } catch (error: any) {
      toast.push(error?.detail || "The portfolio could not be created.", "error");
    } finally { setSaving(false); }
  };

  const rebalance = async () => {
    if (!current) return;
    setSaving(true);
    try {
      const result = await apiPost<any>(`/market/portfolios/${encodeURIComponent(current.portfolio_id)}/rebalance`, { force: true });
      if (result?.proposal) {
        setPendingProposal(result.proposal);
        toast.push("A validated rebalance proposal was prepared. Nothing was applied.", "success");
        setRebalanceOpen(true);
        proposals.refresh();
      } else {
        toast.push(result?.reason || "No rebalance was proposed.", "info");
      }
    } catch (error: any) {
      toast.push(error?.detail || "The rebalance could not be proposed.", "error");
    } finally { setSaving(false); }
  };

  const applyProposal = async (proposal: any) => {
    if (!current) return;
    setSaving(true);
    try {
      await apiPost(`/market/portfolios/${encodeURIComponent(current.portfolio_id)}/apply`, { proposal_id: proposal.proposal_id });
      toast.push("Rebalance applied to the simulated book.", "success");
      setPendingProposal(null);
      setRebalanceOpen(false);
      setTab("holdings");
      portfolios.refresh();
      detail.refresh();
      proposals.refresh();
      performance.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The proposal could not be applied.", "error");
    } finally { setSaving(false); }
  };

  const changeStatus = async (action: string) => {
    if (!current) return;
    setSaving(true);
    try {
      await apiPost(`/market/portfolios/${encodeURIComponent(current.portfolio_id)}/status`, { action, reason: `operator ${action} from the standalone desk` });
      toast.push(`Portfolio ${action === "pause" ? "paused" : "resumed"}.`, "success");
      setStatusTarget(null);
      portfolios.refresh();
      detail.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The portfolio status could not change.", "error");
    } finally { setSaving(false); }
  };

  return (
    <div className="page portfolio-page">
      <PageHeader
        eyebrow={`Portfolio management / ${meta.label}`}
        title={<>{meta.label} portfolio, <em>measured end to end.</em></>}
        description={meta.description}
        meta={<><span className="meta-item"><BriefcaseBusiness size={13} /> {rows.length} book{rows.length === 1 ? "" : "s"}</span><span className="meta-item"><Clock3 size={13} /> {valuation.at ? `Valued ${timeAgo(valuation.at)}` : "No valuation yet"}</span><span className="meta-item"><Landmark size={13} /> 1 NC = ₹1</span></>}
        actions={<><Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { portfolios.refresh(); detail.refresh(); }}>Refresh</Button>{current && nextStatusAction && <Button variant="ghost" icon={nextStatusAction === "pause" ? <Pause size={14} /> : <Play size={14} />} onClick={() => setStatusTarget(nextStatusAction)}>{nextStatusAction === "pause" ? "Pause" : "Resume"}</Button>}<Button variant="primary" icon={<FilePlus2 size={15} />} onClick={() => setCreateOpen(true)}>New book</Button></>}
      />
      <h2 className="sr-only">{meta.label} portfolio</h2>
      {portfolios.error && <ErrorBlock message={portfolios.error.detail} onRetry={portfolios.refresh} />}
      {!rows.length && !portfolios.loading ? (
        <Card className="portfolio-empty-card">
          <EmptyState icon={<BriefcaseBusiness size={24} />} title={`No ${meta.label.toLowerCase()} portfolio yet`} description="A book is created and funded with a real ledger transfer. No capital is implied until the backend records it." action={<Button variant="primary" onClick={() => setCreateOpen(true)} icon={<Plus size={14} />}>Create and fund a book</Button>} />
          <div className="create-prerequisites"><div><WalletCards size={15} /><span>A NovaCoin wallet</span><strong>{walletRows.length ? `${walletRows.length} available` : "None yet"}</strong></div><div><ShieldCheck size={15} /><span>Backend validation</span><strong>Required</strong></div><div><CircleDollarSign size={15} /><span>Capital</span><strong>From the wallet ledger</strong></div></div>
          {!walletRows.length && <Button variant="secondary" onClick={() => navigate("wallet")}>Create a wallet first <ArrowRight size={13} /></Button>}
        </Card>
      ) : current && (
        <>
          <div className="book-switcher">
            {rows.map((row) => (
              <button type="button" key={row.portfolio_id} className={`book-tab ${row.portfolio_id === current.portfolio_id ? "book-tab-active" : ""}`} onClick={() => setSelectedId(row.portfolio_id)}>
                <span><strong>{row.name || "Portfolio"}</strong><small>{shortId(row.portfolio_id, 12)}</small></span>
                <StatusBadge value={row.status} />
              </button>
            ))}
          </div>
          <div className="portfolio-summary-grid">
            <Card className="book-value-card">
              <div className="hero-topline"><span className="eyebrow">Book value</span><StatusBadge value={portfolio.status || "—"} /></div>
              <div className="book-value">{valuation.total_value !== undefined ? <Money value={valuation.total_value} /> : <span className="not-funded">Unavailable</span>}</div>
              <div className="book-return"><Delta value={valuation.return_pct} /><span>return measured by the backend</span></div>
              <div className="book-meta"><span>Cash <strong>{money(valuation.cash)}</strong></span><span>Invested <strong>{money(valuation.invested)}</strong></span></div>
              <div className="book-actions"><Button variant="primary" size="sm" icon={<Sparkles size={14} />} loading={saving} onClick={rebalance}>Propose rebalance</Button><Button variant="ghost" size="sm" onClick={() => navigate("risk")} icon={<ShieldCheck size={14} />}>Risk</Button></div>
            </Card>
            <Metric label="Unrealized P&L" value={money(valuation.unrealized_pnl)} help="Mark-to-market" tone={valuation.unrealized_pnl == null ? "neutral" : num(valuation.unrealized_pnl) < 0 ? "bad" : "good"} icon={<ArrowUpRight size={16} />} />
            <Metric label="Realized P&L" value={money(valuation.realized_pnl)} help="Closed positions" tone={valuation.realized_pnl == null ? "neutral" : num(valuation.realized_pnl) < 0 ? "bad" : "neutral"} icon={<History size={16} />} />
            <Metric label="Exposure" value={valuation.cash_weight == null ? "—" : pct(num(valuation.cash_weight) === 0 ? 0 : (1 - num(valuation.cash_weight)) * 100)} help={`${valuation.holdings ?? holdings.length} holdings`} icon={<Gauge size={16} />} />
          </div>

          <div className="dashboard-grid">
            <Card className="chart-card">
              <CardHeader title="Valuation history" subtitle={snapshots.length ? `${snapshots.length} stored snapshots` : "No snapshots stored"} action={<Badge tone="neutral">Measured</Badge>} />
              <LineChart data={snapshots} height={260} color={meta.color} empty="No stored valuation snapshots" />
            </Card>
            <Card>
              <CardHeader title="Allocation" subtitle="Current holdings by market value" />
              {allocation.length ? <div className="allocation-panel"><DonutChart segments={allocation} totalLabel="Book" totalValue={money(valuation.total_value)} /><AllocationBars segments={allocation} /></div> : <EmptyState title="No holdings" description="A funded book with no positions has cash only." />}
            </Card>
          </div>

          {Array.isArray(provenance?.fixture_priced) && provenance.fixture_priced.length > 0 ? (
            <div className="contract-list"><div><Info size={15} /><span>{provenance.note}</span></div></div>
          ) : null}
          <Card className="tabs-card">
            <div className="tabs-head"><PortfolioTabs active={tab} onChange={setTab} /><Button size="sm" variant="ghost" icon={<RefreshCw size={13} />} onClick={() => { detail.refresh(); valuations.refresh(); performance.refresh(); proposals.refresh(); }}>Reload book</Button></div>
            {tab === "holdings" && (detail.loading ? <LoadingBlock rows={4} label="Reading holdings…" /> : <DataTable rows={holdings} rowKey={(row) => row.instrument_id} empty={<EmptyState title="No open positions" description="The book is currently all cash, or the backend has not recorded a holding." />} columns={[
              { key: "instrument_id", label: "Instrument", sortable: true, render: (row) => <div className="identity-cell"><span className="symbol-mark">{String(row.instrument_id || "?").replace(/^.*:/, "").slice(0, 2)}</span><div><strong>{row.instrument_id}</strong><small>{title(row.asset_class || row.sector || "Position")}</small></div></div> },
              { key: "quantity", label: "Quantity", align: "right", render: (row) => <span className="mono">{quantity(row.quantity)}</span> },
              { key: "average_cost", label: "Avg. cost", align: "right", render: (row) => <Money value={row.average_cost} /> },
              { key: "last_price", label: "Last price", align: "right", render: (row) => <Money value={row.last_price} /> },
              { key: "market_value", label: "Market value", align: "right", sortable: true, sortValue: (row) => num(row.market_value), render: (row) => <Money value={row.market_value} /> },
              { key: "unrealized_pnl", label: "Unrealized", align: "right", render: (row) => <Delta value={num(row.unrealized_pnl) / Math.max(num(row.cost_basis), 1) * 100} /> },
              { key: "weight", label: "Weight", align: "right", render: (row) => <span>{row.weight == null ? "—" : pct(num(row.weight) * 100)}</span> },
              { key: "stale", label: "Price", render: (row) => row.fixture_priced ? <Badge tone="warn">Fixture</Badge> : <Badge tone={row.stale ? "warn" : "good"}>{row.stale ? "Stale" : "Current"}</Badge> },
            ]} />)}
            {tab === "performance" && (
              <div className="performance-panel">
                <div className="metric-grid metric-grid-four"><Metric label="Return" value={metrics.return_pct == null ? "—" : pct(num(metrics.return_pct))} help="Backend performance" tone={metrics.return_pct == null ? "neutral" : num(metrics.return_pct) < 0 ? "bad" : "good"} /><Metric label="Sharpe" value={metrics.sharpe_ratio ?? metrics.sharpe ?? "—"} help="Requires enough observations" /><Metric label="Max drawdown" value={metrics.maximum_drawdown_pct == null ? "—" : pctUnsigned(num(metrics.maximum_drawdown_pct))} help="Measured from snapshots" tone="bad" /><Metric label="Observations" value={metrics.observations ?? snapshots.length} help="Before ratios are reported" /></div>
                {performance.error ? <ErrorBlock message={performance.error.detail} onRetry={performance.refresh} /> : <div className="contract-list"><div><CheckCircle2 size={15} /><span>{performance.data?.flow_note || "External capital flows are reported separately from investment return."}</span></div><div><CheckCircle2 size={15} /><span>Performance ratios wait for the policy's minimum observation count.</span></div></div>}
                {Array.isArray(performance.data?.external_flows) && performance.data.external_flows.length > 0 && <DataTable rows={performance.data.external_flows} rowKey={(row, index) => row.transaction_id || String(index)} columns={[{ key: "at", label: "Flow date", render: (row) => dateTime(row.at || row.created_at) }, { key: "type", label: "Type", render: (row) => title(row.direction || row.transaction_type || "—") }, { key: "amount", label: "Amount", align: "right", render: (row) => <Money value={row.amount} /> }]} />}
              </div>
            )}
            {tab === "rebalance" && (
              <div className="stack">
                <div className="filter-row"><span className="inline-meta"><span className="meta-item">Proposals are validated before they can be applied.</span></span><Button size="sm" variant="primary" icon={<Sparkles size={14} />} loading={saving} onClick={rebalance}>Propose now</Button></div>
                {proposals.loading ? <LoadingBlock rows={4} label="Reading proposals…" /> : <DataTable rows={arrayFrom(proposals.data, "proposals")} rowKey={(row) => row.proposal_id} empty={<EmptyState title="No rebalance proposals" description="A proposal is only created when the policy allows a review." />} columns={[{ key: "created_at", label: "Created", render: (row) => timeAgo(row.created_at), sortable: true }, { key: "status", label: "Status", render: (row) => <StatusBadge value={row.status} /> }, { key: "turnover", label: "Turnover", render: (row) => row.turnover == null ? "—" : pct(num(row.turnover) * 100) }, { key: "drift", label: "Drift", render: (row) => row.drift == null ? "—" : pct(num(row.drift) * 100) }, { key: "intents", label: "Trades", render: (row) => Array.isArray(row.intents) ? row.intents.length : "—" }, { key: "action", label: "", align: "right", render: (row) => row.status === "validated" ? <Button size="sm" variant="secondary" onClick={() => { setPendingProposal(row); setRebalanceOpen(true); }}>Review</Button> : <span className="muted">—</span> }]} />}
              </div>
            )}
            {tab === "history" && (detail.loading ? <LoadingBlock rows={4} label="Reading book history…" /> : <DataTable rows={[...holdings].sort((a, b) => String(b.updated_at || "").localeCompare(String(a.updated_at || "")))} rowKey={(row) => `${row.instrument_id}-${row.updated_at}`} empty={<EmptyState title="No position history" description="Holding changes will appear here after a rebalance is applied." />} columns={[{ key: "instrument_id", label: "Instrument", render: (row) => row.instrument_id }, { key: "updated_at", label: "Last updated", render: (row) => dateTime(row.updated_at) }, { key: "opened_at", label: "Opened", render: (row) => dateTime(row.opened_at) }, { key: "realized_pnl", label: "Realized", align: "right", render: (row) => <Money value={row.realized_pnl} /> }, { key: "status", label: "State", render: (row) => <Badge tone={row.closed_at ? "neutral" : "good"}>{row.closed_at ? "Closed" : "Open"}</Badge> }]} />)}
          </Card>
        </>
      )}

      <Modal open={createOpen} onClose={() => setCreateOpen(false)} title={`Create a ${meta.label.toLowerCase()} book`} description="The backend validates the horizon, funds the book through the ledger and records the new portfolio." footer={<><Button variant="ghost" onClick={() => setCreateOpen(false)}>Cancel</Button><Button variant="primary" loading={saving} disabled={!name.trim() || !fundingWalletId || capital.trim() === "" || num(capital) <= 0} onClick={create}>Create and fund</Button></>}>
        <div className="form-grid"><Field label="Book name"><TextInput value={name} onChange={(event) => setName(event.target.value)} placeholder={`${meta.label} book`} /></Field><Field label="Initial capital (INR)"><TextInput inputMode="decimal" value={capital} onChange={(event) => setCapital(event.target.value)} /></Field></div>
        <Field label="Funding wallet" hint={walletRows.length ? "The ledger transfer is atomic; insufficient funds are refused." : "Create a wallet first."}><SelectInput value={fundingWalletId} onChange={(event) => setFundingWalletId(event.target.value)}><option value="">Choose a wallet…</option>{walletRows.map((wallet) => <option key={wallet.wallet_id} value={wallet.wallet_id}>{wallet.label || wallet.owner || wallet.wallet_id} · {money(wallet.balance?.available)}</option>)}</SelectInput></Field>
        {!walletRows.length && <Button variant="secondary" size="sm" onClick={() => { setCreateOpen(false); navigate("wallet"); }}>Create a wallet</Button>}
        <div className="inline-warning">This is simulated capital. The book is created with an explicit ledger funding transfer and no broker connection.</div>
      </Modal>

      <Modal open={rebalanceOpen} onClose={() => { setRebalanceOpen(false); setPendingProposal(null); }} title="Review rebalance proposal" description="This is the proposal the backend validated. It has not changed the book." footer={<><Button variant="ghost" onClick={() => { setRebalanceOpen(false); setPendingProposal(null); }}>Keep unapplied</Button><Button variant="primary" loading={saving} disabled={!pendingProposal || pendingProposal.status !== "validated"} onClick={() => pendingProposal && applyProposal(pendingProposal)}>Apply proposal</Button></>} wide>
        {pendingProposal ? <>
          <div className="proposal-summary"><div><span>Status</span><StatusBadge value={pendingProposal.status} /></div><div><span>Drift</span><strong>{pendingProposal.drift == null ? "—" : pct(num(pendingProposal.drift) * 100)}</strong></div><div><span>Turnover</span><strong>{pendingProposal.turnover == null ? "—" : pct(num(pendingProposal.turnover) * 100)}</strong></div><div><span>Cash after</span><strong>{money(pendingProposal.cash_after)}</strong></div></div>
          <p className="detail-prose">{pendingProposal.rationale || "The backend supplied no rationale."}</p>
          <DataTable rows={pendingProposal.intents || []} rowKey={(row) => `${row.instrument_id}-${row.side}`} columns={[{ key: "instrument_id", label: "Instrument" }, { key: "side", label: "Side", render: (row) => <Badge tone={row.side === "buy" ? "good" : "bad"}>{title(row.side)}</Badge> }, { key: "quantity_delta", label: "Qty", align: "right", render: (row) => quantity(row.quantity_delta) }, { key: "notional", label: "Notional", align: "right", render: (row) => <Money value={row.notional} /> }, { key: "reason", label: "Reason" }]} />
          <div className="inline-warning">Applying this proposal changes the simulated portfolio book. It does not connect to a broker.</div>
        </> : <LoadingBlock />}
      </Modal>

      <ConfirmDialog open={!!statusTarget} title={statusTarget === "pause" ? "Pause this portfolio?" : "Resume this portfolio?"} description={statusTarget === "pause" ? "Monitoring and rebalancing are paused; existing holdings remain." : "Monitoring and rebalancing return to the backend's active state."} confirmLabel={statusTarget === "pause" ? "Pause book" : "Resume book"} tone="primary" busy={saving} onConfirm={() => statusTarget && changeStatus(statusTarget)} onClose={() => setStatusTarget(null)} />
    </div>
  );
}

export function PortfolioAnalyticsPage() {
  useDocumentTitle("Portfolio Analytics");
  const overview = useApi<any>("/market/overview", { refreshMs: 60_000 });
  const portfolios = useApi<any>("/market/portfolios?limit=200");
  const rows = arrayFrom(portfolios.data, "portfolios");
  const summary = overview.data?.portfolios || {};
  const aggregateRows = summary.rows || [];
  return (
    <div className="page">
      <PageHeader
        eyebrow="Portfolio management / Analytics"
        title={<>The book of books, <em>without mixing the ledgers.</em></>}
        description="Compare the three portfolios using the backend's own valuations, returns, cash and holding counts. Shadow and backtest results are not included."
        meta={<><span className="meta-item"><BarChart3 size={13} /> {overview.data?.summary?.portfolios ?? aggregateRows.length} books</span><span className="meta-item"><Clock3 size={13} /> {overview.data?.generated_at ? timeAgo(overview.data.generated_at) : "Not synced"}</span></>}
        actions={<Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { overview.refresh(); portfolios.refresh(); }}>Refresh</Button>}
      />
      {overview.error && <ErrorBlock message={overview.error.detail} onRetry={overview.refresh} />}
      <div className="metric-grid metric-grid-four">
        <Metric label="Total portfolio value" value={money(summary.total_value_inr)} help="Authoritative valuation" tone="accent" />
        <Metric label="Invested capital" value={money(summary.invested_inr)} help={`${pctUnsigned(summary.exposure_pct)} exposure`} />
        <Metric label="Available cash" value={money(summary.cash_inr)} help="Inside the books" />
        <Metric label="Combined P&L" value={money(summary.pnl_inr)} help={`${pct(summary.return_pct)} weighted return`} tone={summary.pnl_inr == null ? "neutral" : num(summary.pnl_inr) < 0 ? "bad" : "good"} />
      </div>
      <div className="dashboard-grid">
        <Card className="chart-card">
          <CardHeader title="Book comparison" subtitle="Total value by horizon" />
          <LineChart data={aggregateRows.map((row: any) => ({ label: title(row.horizon), value: num(row.total_value_inr) }))} height={260} empty="At least two portfolio snapshots are needed for a trend" />
          {aggregateRows.length > 0 && <div className="allocation-bars">{aggregateRows.map((row: any) => <AllocationBars key={row.portfolio_id} segments={[{ label: `${title(row.horizon)} · ${row.name}`, value: num(row.total_value_inr), color: COLORS[aggregateRows.indexOf(row) % COLORS.length] }]} />)}</div>}
        </Card>
        <Card>
          <CardHeader title="Horizon totals" subtitle="From the same overview aggregate" />
          {(summary.by_horizon || []).length ? <div className="stack-tight">{(summary.by_horizon || []).map((item: any) => <div className="horizon-total-row" key={item.horizon}><span><i style={{ background: (HORIZON_META[horizonSlug(item.horizon)] || HORIZON_META.short).color }} />{title(item.horizon)}</span><strong>{money(item.total_value_inr)}</strong><small>{pct(item.return_pct)} · {item.count} book{item.count === 1 ? "" : "s"}</small></div>)}</div> : <EmptyState title="No funded books" description="Horizon totals appear after a portfolio is created and funded." />}
        </Card>
      </div>
      <Card>
        <CardHeader title="Portfolio register" subtitle="Every measured book" action={<Badge tone="neutral">{rows.length} portfolios</Badge>} />
        {portfolios.loading ? <LoadingBlock rows={4} label="Reading portfolios…" /> : (
          <DataTable rows={aggregateRows.length ? aggregateRows : rows} rowKey={(row) => row.portfolio_id} onRowClick={(row) => navigate(`portfolios/${horizonSlug(row.horizon) || "short"}`)} empty={<EmptyState icon={<BriefcaseBusiness size={22} />} title="No portfolios yet" description="Create a funded book from one of the three horizon pages." action={<Button variant="primary" size="sm" onClick={() => navigate("portfolios/short")}>Open portfolios</Button>} />} columns={[
            { key: "name", label: "Book", sortable: true, render: (row) => <div><strong>{row.name || row.portfolio_id}</strong><small className="table-sub">{shortId(row.portfolio_id, 14)}</small></div> },
            { key: "horizon", label: "Horizon", render: (row) => <Badge tone="neutral">{title(row.horizon)}</Badge> },
            { key: "total_value_inr", label: "Value", align: "right", sortable: true, sortValue: (row) => num(row.total_value_inr), render: (row) => <Money value={row.total_value_inr} /> },
            { key: "invested_inr", label: "Invested", align: "right", render: (row) => <Money value={row.invested_inr} /> },
            { key: "pnl_inr", label: "P&L", align: "right", render: (row) => <Money value={row.pnl_inr} /> },
            { key: "return_pct", label: "Return", align: "right", render: (row) => <Delta value={row.return_pct} /> },
            { key: "holdings", label: "Holdings", align: "right", render: (row) => row.holdings ?? "—" },
            { key: "status", label: "State", render: (row) => <StatusBadge value={row.status || row.statuses?.[0] || "active"} /> },
            { key: "action", label: "", align: "right", render: () => <ChevronRight size={14} className="row-chevron" /> },
          ]} />
        )}
      </Card>
      <Banner tone="info" title="What is not in this comparison">Backtest curves, shadow projections and wallet balances are separate ledgers. Adding them here would make simulated, historical and hypothetical results look like one real portfolio.</Banner>
    </div>
  );
}
