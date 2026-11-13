import { useEffect, useMemo, useState } from "react";
import {
  ArrowDownLeft,
  ArrowLeftRight,
  ArrowRight,
  ArrowUpRight,
  BookOpen,
  CheckCircle2,
  Coins,
  CreditCard,
  History,
  Landmark,
  Plus,
  RefreshCw,
  Scale,
  ShieldCheck,
  WalletCards,
} from "lucide-react";
import { apiPost, newIdempotencyKey } from "../api";
import { useApi, useDocumentTitle, navigate } from "../hooks";
import {
  arrayFrom,
  dateTime,
  money,
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

function walletBalance(wallet: any): string {
  return wallet?.balance?.available ?? wallet?.available_balance ?? wallet?.balance ?? "—";
}

export function WalletPage() {
  useDocumentTitle("NovaCoin Wallet");
  const toast = useToast();
  const wallets = useApi<any>("/market/wallets?limit=100");
  const status = useApi<any>("/market/wallet/status");
  const [selectedId, setSelectedId] = useState("");
  const [createOpen, setCreateOpen] = useState(false);
  const [operation, setOperation] = useState<"deposit" | "withdraw" | null>(null);
  const [owner, setOwner] = useState("operator");
  const [label, setLabel] = useState("");
  const [amount, setAmount] = useState("");
  const [reference, setReference] = useState("");
  const [saving, setSaving] = useState(false);
  const [reconcileOpen, setReconcileOpen] = useState(false);
  const rows = arrayFrom(wallets.data, "wallets");
  const current = rows.find((wallet) => wallet.wallet_id === selectedId) || rows[0];
  const currentId = current?.wallet_id || "";
  const detail = useApi<any>(currentId ? `/market/wallets/${encodeURIComponent(currentId)}` : null);
  const transactions = useApi<any>(currentId ? `/market/wallets/${encodeURIComponent(currentId)}/transactions?limit=10` : null);

  useEffect(() => {
    if (!selectedId && rows.length) setSelectedId(rows[0].wallet_id);
  }, [rows, selectedId]);

  const createWallet = async () => {
    setSaving(true);
    try {
      const result = await apiPost<any>("/market/wallets", { owner, label });
      toast.push("NovaCoin wallet created.", "success");
      setCreateOpen(false);
      setLabel("");
      wallets.refresh();
      if (result?.wallet_id) setSelectedId(result.wallet_id);
    } catch (error: any) {
      toast.push(error?.detail || "The wallet could not be created.", "error");
    } finally { setSaving(false); }
  };

  const runOperation = async () => {
    if (!operation || !currentId) return;
    setSaving(true);
    try {
      await apiPost(`/market/wallets/${encodeURIComponent(currentId)}/${operation}`, {
        amount,
        reference,
        description: `${operation} from the standalone NovaMarket desk`,
        idempotency_key: newIdempotencyKey(`web-${operation}`),
      });
      toast.push(`Simulated ${operation} posted to the ledger.`, "success");
      setOperation(null);
      setAmount("");
      setReference("");
      wallets.refresh();
      detail.refresh();
      transactions.refresh();
      status.refresh();
    } catch (error: any) {
      toast.push(error?.detail || `The ${operation} was refused.`, "error");
    } finally { setSaving(false); }
  };

  const reconcile = async () => {
    setSaving(true);
    try {
      const result = await apiPost<any>("/market/wallet/reconcile", {});
      toast.push(result?.ok === false ? "Reconciliation reported an issue." : "All wallets reconciled against their ledger entries.", "success");
      setReconcileOpen(false);
      status.refresh();
      detail.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The reconciliation could not run.", "error");
    } finally { setSaving(false); }
  };

  return (
    <div className="page">
      <PageHeader eyebrow="Financial records / Wallet" title={<>NovaCoin, <em>one ledger at a time.</em></>} description="The wallet is a real double-entry ledger inside NovaMarket. 1 NC = ₹1, balances are authoritative and every movement is immutable."
        meta={<><span className="meta-item"><WalletCards size={13} /> {wallets.data?.total ?? rows.length} wallets</span><span className="meta-item"><Scale size={13} /> {status.data?.summary?.ledger_entries ?? "—"} entries</span><span className="meta-item"><ShieldCheck size={13} /> {status.data?.last_reconciliation?.ok === false ? "Reconciliation issue" : "Reconciliation available"}</span></>}
        actions={<><Button variant="ghost" icon={<RefreshCw size={14} />} onClick={() => { wallets.refresh(); status.refresh(); detail.refresh(); }}>Refresh</Button><Button variant="secondary" icon={<Scale size={14} />} onClick={() => setReconcileOpen(true)}>Reconcile</Button><Button variant="primary" icon={<Plus size={15} />} onClick={() => setCreateOpen(true)}>New wallet</Button></>}
      />
      <Banner tone="info" title="NovaCoin is not a second currency">1 NC equals ₹1. The wallet, portfolio cash and paper fees all share the same unit; the UI never performs its own conversion or balance arithmetic.</Banner>
      {wallets.error && <ErrorBlock message={wallets.error.detail} onRetry={wallets.refresh} />}
      <div className="wallet-layout">
        <Card className="list-rail"><CardHeader title="Wallets" subtitle="Owner and available balance" />{wallets.loading ? <LoadingBlock rows={3} label="Reading wallets…" /> : <div className="rail-list">{rows.map((wallet) => <button type="button" className={`rail-item ${wallet.wallet_id === currentId ? "rail-item-active" : ""}`} key={wallet.wallet_id} onClick={() => setSelectedId(wallet.wallet_id)}><span className="rail-icon"><WalletCards size={15} /></span><span><strong>{wallet.label || wallet.owner || "Wallet"}</strong><small>{wallet.owner || wallet.wallet_id} · {wallet.system ? "System" : "Operator"}</small></span><strong className="rail-money">{money(walletBalance(wallet))}</strong></button>)}</div>}{!rows.length && !wallets.loading && <EmptyState icon={<WalletCards size={22} />} title="No wallets yet" description="Create a wallet to hold simulated NovaCoin." action={<Button size="sm" variant="primary" onClick={() => setCreateOpen(true)}>Create wallet</Button>} />}</Card>
        <div className="stack">
          {current ? <>
            <Card className="wallet-hero-card"><div className="hero-topline"><span className="eyebrow">Available balance</span><StatusBadge value={current.status} /></div><div className="wallet-balance">{money(walletBalance(current))}<small>NC</small></div><div className="wallet-owner">{current.label || current.owner} <span>·</span> {current.wallet_id}</div><div className="wallet-actions"><Button variant="primary" icon={<ArrowDownLeft size={14} />} onClick={() => setOperation("deposit")}>Deposit</Button><Button variant="secondary" icon={<ArrowUpRight size={14} />} onClick={() => setOperation("withdraw")}>Withdraw</Button><Button variant="ghost" icon={<Scale size={14} />} onClick={() => navigate("ledger")}>Open ledger</Button></div><div className="wallet-facts"><div><span>Currency</span><strong>INR / NC</strong></div><div><span>Balance model</span><strong>Double-entry</strong></div><div><span>Last updated</span><strong>{timeAgo(current.updated_at)}</strong></div></div></Card>
            <Card><CardHeader title="Recent transactions" subtitle="Latest movements for this wallet" action={<button type="button" className="text-link" onClick={() => navigate("transactions")}>All transactions <ArrowRight size={13} /></button>} />{transactions.loading ? <LoadingBlock rows={3} label="Reading transactions…" /> : <DataTable rows={arrayFrom(transactions.data, "transactions")} rowKey={(row) => row.transaction_id} empty={<EmptyState title="No transactions" description="Deposits and transfers appear only after the ledger posts them." />} columns={[{ key: "created_at", label: "When", render: (row) => dateTime(row.created_at) }, { key: "transaction_type", label: "Type", render: (row) => <Badge tone={row.transaction_type === "deposit" ? "good" : row.transaction_type === "withdrawal" ? "bad" : "info"}>{title(row.transaction_type)}</Badge> }, { key: "amount", label: "Amount", align: "right", render: (row) => <Money value={row.amount} /> }, { key: "status", label: "Status", render: (row) => <StatusBadge value={row.status || "posted"} /> }, { key: "reference", label: "Reference", render: (row) => row.reference || row.description || "—" }]} />}</Card>
            <Card><CardHeader title="Wallet contract" subtitle="What is guaranteed" /><div className="contract-list"><div><CheckCircle2 size={15} /><span>Entries are append-only; a reversal creates mirrored entries.</span></div><div><CheckCircle2 size={15} /><span>Every wallet balance is reconciled against its ledger.</span></div><div><CheckCircle2 size={15} /><span>Transfers are atomic and conserve NC across wallets.</span></div></div></Card>
          </> : <Card><EmptyState icon={<WalletCards size={24} />} title="No wallet selected" description="Create a wallet to see its authoritative balance and immutable entries." action={<Button variant="primary" onClick={() => setCreateOpen(true)}>Create wallet</Button>} /></Card>}
        </div>
      </div>

      <Modal open={createOpen} onClose={() => setCreateOpen(false)} title="Create a NovaCoin wallet" description="A wallet opens with a zero balance. Funds enter only through an explicit simulated deposit or transfer." footer={<><Button variant="ghost" onClick={() => setCreateOpen(false)}>Cancel</Button><Button variant="primary" loading={saving} disabled={!owner.trim()} onClick={createWallet}>Create wallet</Button></>}><div className="form-grid"><Field label="Owner"><TextInput value={owner} onChange={(event) => setOwner(event.target.value)} /></Field><Field label="Label"><TextInput value={label} onChange={(event) => setLabel(event.target.value)} placeholder="Optional display label" /></Field></div><div className="inline-warning">No real money, bank account or broker is connected.</div></Modal>
      <Modal open={!!operation} onClose={() => setOperation(null)} title={operation === "deposit" ? "Deposit simulated NovaCoin" : "Withdraw simulated NovaCoin"} description="The backend validates precision, idempotency and available funds before posting the ledger transaction." footer={<><Button variant="ghost" onClick={() => setOperation(null)}>Cancel</Button><Button variant="primary" loading={saving} disabled={!amount.trim()} onClick={runOperation}>{operation === "deposit" ? "Post deposit" : "Post withdrawal"}</Button></>}><Field label="Amount (NC / INR)" hint="Up to 2 decimal places; floats are never accepted by the ledger."><TextInput value={amount} onChange={(event) => setAmount(event.target.value)} inputMode="decimal" placeholder="0.00" /></Field><Field label="Reference"><TextInput value={reference} onChange={(event) => setReference(event.target.value)} placeholder="Optional reference" /></Field><div className="inline-warning">This operation posts to the simulated ledger and is recorded in the audit trail.</div></Modal>
      <ConfirmDialog open={reconcileOpen} title="Reconcile every wallet?" description="The backend recomputes each wallet from its ledger entries and checks that NC is conserved. It does not move funds." confirmLabel="Run reconciliation" tone="primary" busy={saving} onConfirm={reconcile} onClose={() => setReconcileOpen(false)} />
    </div>
  );
}

export function TransactionsPage() {
  useDocumentTitle("Transaction History");
  const wallets = useApi<any>("/market/wallets?limit=100");
  const [walletId, setWalletId] = useState("");
  const [type, setType] = useState("");
  const rows = arrayFrom(wallets.data, "wallets");
  useEffect(() => { if (!walletId && rows.length) setWalletId(rows[0].wallet_id); }, [rows, walletId]);
  const transactions = useApi<any>(walletId ? `/market/wallets/${encodeURIComponent(walletId)}/transactions?limit=200` : null);
  const txRows = arrayFrom(transactions.data, "transactions").filter((row) => !type || row.transaction_type === type);
  return (
    <div className="page">
      <PageHeader eyebrow="Financial records / Transactions" title={<>Every posted movement, <em>with its reference.</em></>} description="Transaction history is read from the wallet ledger. Reversals are visible as mirrored records and never rewrite the original." actions={<Button variant="ghost" icon={<RefreshCw size={14} />} onClick={transactions.refresh}>Refresh</Button>} />
      <div className="filter-row"><SelectInput value={walletId} onChange={(event) => setWalletId(event.target.value)} aria-label="Wallet"><option value="">Choose a wallet…</option>{rows.map((wallet) => <option key={wallet.wallet_id} value={wallet.wallet_id}>{wallet.label || wallet.owner} · {money(walletBalance(wallet))}</option>)}</SelectInput><SelectInput value={type} onChange={(event) => setType(event.target.value)} aria-label="Transaction type"><option value="">All types</option>{["deposit", "withdrawal", "transfer", "reversal", "adjustment"].map((item) => <option key={item} value={item}>{title(item)}</option>)}</SelectInput><Badge tone="neutral">{txRows.length} shown</Badge></div>
      <Card><CardHeader title="Transactions" subtitle={walletId ? `Wallet ${walletId}` : "Select a wallet"} />{transactions.loading ? <LoadingBlock rows={5} label="Reading transaction history…" /> : transactions.error ? <ErrorBlock message={transactions.error.detail} onRetry={transactions.refresh} /> : <DataTable rows={txRows} rowKey={(row) => row.transaction_id} empty={<EmptyState icon={<History size={22} />} title="No transactions" description="The selected wallet has no posted movements." />} columns={[{ key: "created_at", label: "Posted", render: (row) => dateTime(row.created_at), sortable: true }, { key: "transaction_id", label: "Reference", render: (row) => <span className="mono-small">{shortId(row.transaction_id, 18)}</span> }, { key: "transaction_type", label: "Type", render: (row) => <Badge tone={row.transaction_type === "deposit" ? "good" : row.transaction_type === "withdrawal" ? "bad" : "info"}>{title(row.transaction_type)}</Badge> }, { key: "amount", label: "Amount", align: "right", render: (row) => <Money value={row.amount} /> }, { key: "status", label: "State", render: (row) => <StatusBadge value={row.status || "posted"} /> }, { key: "reference", label: "Reference / note", render: (row) => row.reference || row.description || "—" }, { key: "reversed_by", label: "Reversed by", render: (row) => (row.reversed_by_transaction_id || row.reversed_by) ? shortId(row.reversed_by_transaction_id || row.reversed_by, 14) : "—" }]} />}</Card>
    </div>
  );
}

export function LedgerPage() {
  useDocumentTitle("Ledger");
  const toast = useToast();
  const wallets = useApi<any>("/market/wallets?limit=100");
  const [walletId, setWalletId] = useState("");
  const [reconcileOpen, setReconcileOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const rows = arrayFrom(wallets.data, "wallets");
  useEffect(() => { if (!walletId && rows.length) setWalletId(rows[0].wallet_id); }, [rows, walletId]);
  const ledger = useApi<any>(walletId ? `/market/wallets/${encodeURIComponent(walletId)}/ledger?limit=200` : null);
  const wallet = rows.find((row) => row.wallet_id === walletId);
  const reconcile = async () => {
    setSaving(true);
    try {
      await apiPost(`/market/wallets/${encodeURIComponent(walletId)}/reconcile`, {});
      toast.push("Wallet reconciled against its immutable entries.", "success");
      setReconcileOpen(false);
      ledger.refresh();
    } catch (error: any) { toast.push(error?.detail || "Reconciliation failed.", "error"); } finally { setSaving(false); }
  };
  return (
    <div className="page">
      <PageHeader eyebrow="Financial records / Ledger" title={<>An append-only record, <em>not an editable balance.</em></>} description="Every entry has a direction, amount, running balance and sequence. Reconciliation is a read of the ledger, not a rewrite of the balance." actions={<><Button variant="ghost" icon={<RefreshCw size={14} />} onClick={ledger.refresh}>Refresh</Button><Button variant="secondary" icon={<Scale size={14} />} onClick={() => setReconcileOpen(true)} disabled={!walletId}>Reconcile wallet</Button></>} />
      <div className="filter-row"><SelectInput value={walletId} onChange={(event) => setWalletId(event.target.value)} aria-label="Wallet"><option value="">Choose a wallet…</option>{rows.map((item) => <option key={item.wallet_id} value={item.wallet_id}>{item.label || item.owner} · {money(walletBalance(item))}</option>)}</SelectInput><Badge tone="info">1 NC = ₹1</Badge></div>
      {wallet && <div className="metric-grid metric-grid-four"><Metric label="Available" value={money(walletBalance(wallet))} icon={<Coins size={16} />} tone="accent" /><Metric label="Reserved" value={money(wallet.balance?.reserved)} help="NM-03 holds no reserves" /><Metric label="Entries" value={ledger.data?.total ?? arrayFrom(ledger.data, "entries").length} help="Append-only" /><Metric label="Status" value={<StatusBadge value={wallet.status} />} help={wallet.owner} /></div>}
      <Card><CardHeader title="Ledger entries" subtitle="Newest first, with running balance" />{ledger.loading ? <LoadingBlock rows={5} label="Reading immutable entries…" /> : ledger.error ? <ErrorBlock message={ledger.error.detail} onRetry={ledger.refresh} /> : <DataTable rows={arrayFrom(ledger.data, "entries")} rowKey={(row) => row.entry_id} empty={<EmptyState icon={<BookOpen size={22} />} title="No ledger entries" description="A newly created wallet has no entries until funds are deposited." />} columns={[{ key: "created_at", label: "Posted", render: (row) => dateTime(row.created_at), sortable: true }, { key: "sequence", label: "Seq", align: "right", render: (row) => row.sequence ?? "—" }, { key: "entry_type", label: "Type", render: (row) => <Badge tone="neutral">{title(row.entry_type)}</Badge> }, { key: "direction", label: "Direction", render: (row) => <Badge tone={row.direction === "credit" ? "good" : "bad"}>{title(row.direction)}</Badge> }, { key: "amount", label: "Amount", align: "right", render: (row) => <Money value={row.amount} /> }, { key: "signed_amount", label: "Signed", align: "right", render: (row) => <Money value={row.signed_amount} /> }, { key: "balance_after", label: "Balance after", align: "right", render: (row) => <Money value={row.balance_after} /> }, { key: "reference", label: "Reference", render: (row) => row.reference || row.description || "—" }, { key: "entry_id", label: "Entry", render: (row) => <span className="mono-small">{shortId(row.entry_id, 14)}</span> }]} />}</Card>
      <ConfirmDialog open={reconcileOpen} title="Reconcile this wallet?" description="The backend recomputes the balance from every entry and reports any difference. It does not alter posted entries." confirmLabel="Reconcile" tone="primary" busy={saving} onConfirm={reconcile} onClose={() => setReconcileOpen(false)} />
    </div>
  );
}
