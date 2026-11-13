import { useEffect, useMemo, useState } from "react";
import {
  ArrowRight,
  BarChart3,
  CalendarDays,
  CheckCircle2,
  ChevronRight,
  ExternalLink,
  Filter,
  Globe2,
  Layers3,
  LoaderCircle,
  Newspaper,
  RefreshCw,
  Search,
  ShieldCheck,
  TrendingDown,
  TrendingUp,
} from "lucide-react";
import { apiGet, apiPost } from "../api";
import { useApi, useDocumentTitle, navigate } from "../hooks";
import {
  arrayFrom,
  barPoints,
  dateTime,
  first,
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
  Field,
  LoadingBlock,
  Modal,
  PageHeader,
  SearchInput,
  SelectInput,
  StatusBadge,
  useToast,
} from "../components/ui";
import { LineChart } from "../components/charts";

type Instrument = Record<string, any>;

function instrumentId(instrument: Instrument): string {
  return String(instrument.instrument_id || instrument.id || instrument.symbol || "");
}

function instrumentName(instrument: Instrument): string {
  return String(instrument.name || instrument.symbol || instrumentId(instrument));
}

function quoteDirection(change: number | null) {
  if (change === null) return null;
  return change >= 0 ? <TrendingUp size={13} /> : <TrendingDown size={13} />;
}

function InstrumentDetail({ instrument }: { instrument: Instrument }) {
  const id = instrumentId(instrument);
  const quote = useApi<any>(`/market/data/quote/${encodeURIComponent(id)}`, { refreshMs: 60_000 });
  const bars = useApi<any>(`/market/data/bars/${encodeURIComponent(id)}?interval=1d&limit=120&ascending=false`);
  const price = quoteValue(quote.data);
  const change = quoteChange(quote.data);
  const points = useMemo(
    () => barPoints(bars.data).map((point) => ({ ...point, label: shortDate(point.label) })),
    [bars.data],
  );
  return (
    <Card className="instrument-detail">
      <div className="instrument-detail-head">
        <div>
          <div className="eyebrow">{instrument.exchange || "Market"} · {title(instrument.asset_class || "instrument")}</div>
          <h2>{instrumentName(instrument)}</h2>
          <div className="instrument-symbol">{instrument.symbol || id} <span>·</span> {instrument.currency || "INR"}</div>
        </div>
        <div className="instrument-price">
          {quote.loading ? <span className="skeleton skeleton-line" /> : price === null ? <span className="muted">No quote</span> : <strong>{money(price)}</strong>}
          {change !== null && <span className={`delta ${change >= 0 ? "positive" : "negative"}`}>{quoteDirection(change)}{pct(change)}</span>}
        </div>
      </div>
      <div className="instrument-meta">
        <span><Globe2 size={13} /> {quoteTime(quote.data) ? `Observed ${timeAgo(quoteTime(quote.data))}` : "No observation timestamp"}</span>
        <span><Layers3 size={13} /> {instrument.asset_class || "Unclassified"}</span>
        <span><ShieldCheck size={13} /> Provider observations are reconciled by NM-02</span>
      </div>
      <div className="chart-block">
        <div className="chart-block-head">
          <span>Price history</span>
          <Badge tone="neutral">{bars.data ? `${points.length} daily bars` : "Loading"}</Badge>
        </div>
        <LineChart data={points} height={265} color="var(--accent)" empty="No daily bars for this instrument" />
      </div>
      {quote.error && <div className="inline-warning">{quote.error.detail}</div>}
    </Card>
  );
}

export function MarketPage() {
  useDocumentTitle("Market Overview");
  const toast = useToast();
  const status = useApi<any>("/market/status", { refreshMs: 60_000 });
  const events = useApi<any>("/market/events?limit=6");
  const [rows, setRows] = useState<Instrument[]>([]);
  const [catalogueTotal, setCatalogueTotal] = useState(0);
  const [catalogueLoading, setCatalogueLoading] = useState(true);
  const [catalogueError, setCatalogueError] = useState("");
  const [catalogueReload, setCatalogueReload] = useState(0);
  const [search, setSearch] = useState("");
  const [assetClass, setAssetClass] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [ingesting, setIngesting] = useState(false);
  const [page, setPage] = useState(0);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      setCatalogueLoading(true);
      setCatalogueError("");
      const all: Instrument[] = [];
      let total = 0;
      try {
        // The backend caps one page at 1000 rows. Read every page so the
        // catalogue truly contains the whole synced NSE/BSE universe, while
        // the table below renders one page at a time.
        for (let offset = 0; offset < 10_000; offset += 1000) {
          const body = await apiGet<any>("/market/instruments", { limit: 1000, offset });
          all.push(...(Array.isArray(body?.items) ? body.items : []));
          total = Number(body?.total ?? all.length);
          if (!body?.has_more) break;
        }
        if (!cancelled) {
          setRows(all);
          setCatalogueTotal(total || all.length);
        }
      } catch (error: any) {
        if (!cancelled) setCatalogueError(error?.detail || "The instrument catalogue could not be read.");
      } finally {
        if (!cancelled) setCatalogueLoading(false);
      }
    };
    load();
    return () => { cancelled = true; };
  }, [catalogueReload]);
  const assetClasses = useMemo(
    () => Array.from(new Set(rows.map((row) => String(row.asset_class || "")).filter(Boolean))).sort(),
    [rows],
  );
  const filtered = useMemo(() => {
    const text = search.trim().toLowerCase();
    return rows.filter((row) => {
      const matchesText = !text || [row.symbol, row.name, row.instrument_id, row.exchange].some((value) => String(value || "").toLowerCase().includes(text));
      const matchesClass = !assetClass || String(row.asset_class || "") === assetClass;
      return matchesText && matchesClass;
    });
  }, [rows, search, assetClass]);
  const pageSize = 100;
  const pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
  const safePage = Math.min(page, pageCount - 1);
  const pageRows = filtered.slice(safePage * pageSize, (safePage + 1) * pageSize);
  const selected = rows.find((row) => instrumentId(row) === selectedId) || filtered[0] || rows[0];

  useEffect(() => { setPage(0); }, [search, assetClass]);

  useEffect(() => {
    if (!selectedId && rows.length) setSelectedId(instrumentId(rows[0]));
  }, [rows, selectedId]);

  const refresh = () => {
    status.refresh();
    events.refresh();
    setCatalogueReload((value) => value + 1);
  };

  const ingest = async () => {
    setIngesting(true);
    try {
      const result = await apiPost<any>("/market/data/ingest", {
        instrument_ids: selected ? [instrumentId(selected)] : undefined,
        intervals: ["1d"],
        trigger: "web",
      });
      toast.push(result?.detail || "Market-data cycle completed.", "success");
      refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The market-data cycle could not run.", "error");
    } finally {
      setIngesting(false);
    }
  };

  const statusData = status.data;
  const state = statusData?.health?.state || "unknown";
  const eventRows = arrayFrom(events.data, "events");

  return (
    <div className="page market-page">
      <PageHeader
        eyebrow="Market intelligence"
        title={<>Observe the market, <em>not a mock tape.</em></>}
        description="Instrument metadata, provider observations and evidence-backed events from the NovaMarket market-data layer."
        meta={
          <>
            <span className="meta-item"><i className={`health-dot health-${String(state).toLowerCase()}`} /> {title(state)}</span>
            <span className="meta-item"><Globe2 size={13} /> {catalogueTotal || rows.length} instruments</span>
            <span className="meta-item"><CalendarDays size={13} /> {dateTime(statusData?.generated_at || new Date().toISOString())}</span>
          </>
        }
        actions={
          <>
            <Button variant="ghost" icon={<RefreshCw size={14} className={status.refreshing ? "spin" : undefined} />} onClick={refresh}>Refresh</Button>
            <Button variant="primary" icon={<BarChart3 size={15} />} loading={ingesting} onClick={ingest}>Ingest latest cycle</Button>
          </>
        }
      />

      {status.error && <ErrorBlock message={status.error.detail} onRetry={status.refresh} />}
      {catalogueError && <ErrorBlock message={catalogueError} onRetry={() => setCatalogueReload((value) => value + 1)} />}
      {statusData?.config_error && <div className="inline-warning">{statusData.config_error}</div>}

      <div className="market-status-strip">
        <div><span className="strip-label">Lifecycle</span><StatusBadge value={statusData?.lifecycle || "unknown"} /></div>
        <div><span className="strip-label">Data layer</span><strong>{statusData?.enabled ? "Enabled" : "Not enabled"}</strong></div>
        <div><span className="strip-label">Provider readiness</span><strong>{statusData?.dependencies?.filter((item: any) => item.available).length ?? "—"} / {statusData?.dependencies?.length ?? "—"}</strong></div>
        <div><span className="strip-label">Quote freshness</span><strong>On-demand, reconciled</strong></div>
        <button type="button" className="text-link" onClick={() => navigate("health")}>System health <ArrowRight size={13} /></button>
      </div>

      <div className="market-layout">
        <div className="stack">
          {selected ? <InstrumentDetail instrument={selected} /> : <Card><EmptyState icon={<Globe2 size={22} />} title="No instrument selected" description="Register an instrument through the API catalogue to begin observing market data." /></Card>}

          <Card>
            <CardHeader
              title="Instrument catalogue"
              subtitle="Canonical identities registered with NM-02"
              action={<Badge tone="neutral">{filtered.length} matched · {catalogueTotal || rows.length} registered</Badge>}
            />
            <div className="filter-row">
              <SearchInput value={search} onChange={setSearch} placeholder="Search symbol, name or exchange…" />
              <SelectInput value={assetClass} onChange={(event) => setAssetClass(event.target.value)} aria-label="Asset class">
                <option value="">All asset classes</option>
                {assetClasses.map((item) => <option key={item} value={item}>{title(item)}</option>)}
              </SelectInput>
            </div>
            {catalogueLoading ? <LoadingBlock rows={4} label="Reading the NSE/BSE catalogue…" /> : (
              <>
                <DataTable
                  rows={pageRows}
                  rowKey={(row) => instrumentId(row)}
                  onRowClick={(row) => setSelectedId(instrumentId(row))}
                  empty={<EmptyState icon={<Search size={20} />} title="No instruments match" description="Adjust the search or asset-class filter, or sync the public NSE/BSE universe in Settings." />}
                  columns={[
                    { key: "symbol", label: "Instrument", render: (row) => <div className="identity-cell"><span className="symbol-mark">{(row.symbol || "?").slice(0, 2)}</span><div><strong>{row.symbol || "—"}</strong><small>{row.name || "Unnamed instrument"}</small></div></div>, sortable: true },
                    { key: "exchange", label: "Exchange", render: (row) => row.exchange || "—", sortable: true },
                    { key: "asset_class", label: "Class", render: (row) => <Badge tone="neutral">{title(row.asset_class || "—")}</Badge> },
                    { key: "currency", label: "Currency", render: (row) => row.currency || "—" },
                    { key: "action", label: "", render: () => <ChevronRight size={14} className="row-chevron" />, align: "right" },
                  ]}
                />
                {filtered.length > pageSize && (
                  <div className="pagination-row">
                    <span className="muted">Page {safePage + 1} of {pageCount}</span>
                    <div className="row-actions">
                      <Button variant="ghost" size="sm" disabled={safePage === 0} onClick={() => setPage(Math.max(0, safePage - 1))}>Previous</Button>
                      <Button variant="ghost" size="sm" disabled={safePage >= pageCount - 1} onClick={() => setPage(Math.min(pageCount - 1, safePage + 1))}>Next</Button>
                    </div>
                  </div>
                )}
              </>
            )}
          </Card>
        </div>

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
            <CardHeader title="Market-data contract" subtitle="What this page is allowed to claim" />
            <div className="contract-list">
              <div><CheckCircle2 size={15} /><span>Prices are provider observations reconciled by the backend.</span></div>
              <div><CheckCircle2 size={15} /><span>A missing quote is shown as unavailable, never interpolated.</span></div>
              <div><CheckCircle2 size={15} /><span>Market state is read from NovaMarket health and lifecycle.</span></div>
              <div><CheckCircle2 size={15} /><span>External ingestion is explicit and never runs on a timer in this UI.</span></div>
            </div>
            <Button variant="secondary" size="sm" onClick={() => navigate("events")} icon={<Newspaper size={14} />}>Review event evidence</Button>
          </Card>
        </div>
      </div>
    </div>
  );
}

export function EventsPage() {
  useDocumentTitle("Market Events");
  const toast = useToast();
  const events = useApi<any>("/market/events?limit=200");
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [verifiedOnly, setVerifiedOnly] = useState(false);
  const [stockLinkedOnly, setStockLinkedOnly] = useState(false);
  const [selected, setSelected] = useState<any | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [fetching, setFetching] = useState(false);
  const openEvent = async (row: any) => {
    setSelected(row);
    if (!row?.event_id) return;
    setDetailLoading(true);
    try {
      const detail = await apiGet<any>(`/market/events/${encodeURIComponent(row.event_id)}`);
      setSelected({ ...row, ...detail });
    } catch (error: any) {
      toast.push(error?.detail || "The event evidence could not be loaded.", "error");
    } finally {
      setDetailLoading(false);
    }
  };
  const fetchLiveFeeds = async () => {
    setFetching(true);
    try {
      const result = await apiPost<any>("/market/events/refresh", {});
      const results = Array.isArray(result?.results) ? result.results : [];
      const created = results.reduce((sum: number, row: any) => sum + Number(row?.created || 0), 0);
      const failed = results.filter((row: any) => row?.status === "error").length;
      toast.push(
        failed
          ? `Fetched live feeds; ${created} new event${created === 1 ? "" : "s"}, ${failed} feed${failed === 1 ? "" : "s"} unavailable.`
          : `Fetched live feeds; ${created} new event${created === 1 ? "" : "s"} recorded.`,
        failed ? "info" : "success",
      );
      events.refresh();
    } catch (error: any) {
      toast.push(error?.detail || "The live event feeds could not be fetched.", "error");
    } finally { setFetching(false); }
  };
  const rows = arrayFrom(events.data, "events");
  const categories = useMemo(() => Array.from(new Set(rows.map((row) => String(row.category || "")).filter(Boolean))).sort(), [rows]);
  const filtered = useMemo(() => rows.filter((row) => {
    const text = search.toLowerCase().trim();
    const matchesText = !text || [row.title, row.headline, row.summary, row.category, row.event_id].some((value) => String(value || "").toLowerCase().includes(text));
    const matchesCategory = !category || String(row.category || "") === category;
    const matchesVerified = !verifiedOnly || ["multi_source_corroborated", "officially_confirmed"].includes(String(row.verification || row.verification_state || "").toLowerCase());
    const matchesStock = !stockLinkedOnly || (Array.isArray(row.instruments) && row.instruments.length > 0);
    return matchesText && matchesCategory && matchesVerified && matchesStock;
  }), [rows, search, category, verifiedOnly, stockLinkedOnly]);

  return (
    <div className="page">
      <PageHeader
        eyebrow="Intelligence / Events"
        title={<>Market events, <em>with evidence attached.</em></>}
        description="Live NSE/BSE company and market news from the enabled Economic Times and Business Standard feeds. Every record keeps its source URL, retrieval time and verification state; the desk does not promote an unverified item into a forecast."
        meta={<><span className="meta-item"><Newspaper size={13} /> {events.data?.total ?? 0} recorded</span><span className="meta-item"><Filter size={13} /> {filtered.length} shown</span></>}
        actions={<><Button variant="ghost" icon={<RefreshCw size={14} />} onClick={events.refresh}>Refresh view</Button><Button variant="primary" icon={<RefreshCw size={14} className={fetching ? "spin" : undefined} />} loading={fetching} onClick={fetchLiveFeeds}>Fetch live feeds</Button></>}
      />
      {events.error && <ErrorBlock message={events.error.detail} onRetry={events.refresh} />}
      <Card>
        <div className="filter-row">
          <SearchInput value={search} onChange={setSearch} placeholder="Search events, categories or summaries…" />
          <SelectInput value={category} onChange={(event) => setCategory(event.target.value)} aria-label="Event category">
            <option value="">All categories</option>
            {categories.map((item) => <option key={item} value={item}>{title(item)}</option>)}
          </SelectInput>
          <label className="toggle-label"><input type="checkbox" checked={verifiedOnly} onChange={(event) => setVerifiedOnly(event.target.checked)} /> Corroborated only</label>
          <label className="toggle-label"><input type="checkbox" checked={stockLinkedOnly} onChange={(event) => setStockLinkedOnly(event.target.checked)} /> Stock-linked only</label>
        </div>
        {events.loading ? <LoadingBlock rows={5} label="Reading market events…" /> : (
          <DataTable
            rows={filtered}
            rowKey={(row) => row.event_id || row.id || `${row.title}-${row.detected_at}`}
            onRowClick={openEvent}
            empty={<EmptyState icon={<Newspaper size={22} />} title="No events match this view" description="Events appear after the radar records an evidence-backed signal." />}
            columns={[
              { key: "title", label: "Event", sortable: true, render: (row) => <div className="event-table-title"><span className={`event-marker event-${String(row.importance || "normal").toLowerCase()}`} /><div><strong>{row.title || row.headline || row.summary || "Untitled event"}</strong><small>{row.event_id ? String(row.event_id).slice(0, 18) : "Event record"}</small></div></div> },
              { key: "category", label: "Category", render: (row) => <Badge tone="neutral">{title(row.category || "—")}</Badge>, sortable: true },
              { key: "instruments", label: "Stocks", render: (row) => { const ids = Array.isArray(row.instruments) ? row.instruments : []; return ids.length ? <span className="mono-small">{ids.slice(0, 3).join(", ")}{ids.length > 3 ? ` +${ids.length - 3}` : ""}</span> : <span className="muted">Market-wide</span>; } },
              { key: "importance", label: "Importance", render: (row) => <Badge tone={String(row.importance || "").toLowerCase() === "high" ? "warn" : "neutral"}>{title(row.importance || "normal")}</Badge> },
              { key: "verification_state", label: "Verification", render: (row) => <StatusBadge value={row.verification || row.verification_state || row.status || "recorded"} /> },
              { key: "detected_at", label: "Detected", render: (row) => timeAgo(row.discovered_at || row.detected_at || row.published_at || row.created_at), sortable: true, sortValue: (row) => row.discovered_at || row.detected_at || row.published_at || row.created_at || "" },
              { key: "sources", label: "Sources", render: (row) => { const sources = Array.isArray(row.sources) ? row.sources : Array.isArray(row.evidence) ? row.evidence : []; const count = sources.length ? sources.length : row.evidence_count ?? row.independent_sources ?? "—"; return <span>{count}</span>; }, align: "right" },
            ]}
          />
        )}
      </Card>

      <EventDetail event={selected} loading={detailLoading} onClose={() => setSelected(null)} />
    </div>
  );
}

function EventDetail({ event, loading, onClose }: { event: any | null; loading: boolean; onClose: () => void }) {
  if (!event) return null;
  const evidence = Array.isArray(event.evidence) ? event.evidence : Array.isArray(event.sources) ? event.sources : [];
  const mappings = Array.isArray(event.mappings) ? event.mappings.filter((mapping: any) => mapping.relationship !== "uncertain") : [];
  const instruments = mappings.length ? mappings.map((mapping: any) => mapping.instrument_id) : event.instruments || event.instrument_ids || [];
  return (
    <Modal open onClose={onClose} title={event.title || event.headline || "Market event"} description={`${title(event.category || "event")} · ${dateTime(event.discovered_at || event.detected_at || event.published_at || event.created_at)}`} wide footer={<Button variant="secondary" onClick={onClose}>Close</Button>}>
      <div className="detail-grid">
        <div className="detail-main">
          <p className="detail-prose">{event.summary || event.description || "The backend recorded this event without a narrative summary."}</p>
          <div className="detail-section">
            <h3>Evidence & references</h3>
            {loading ? <LoadingBlock rows={3} label="Loading evidence…" /> : evidence.length ? (
              <div className="evidence-list">
                {evidence.map((item: any, index: number) => (
                  <div className="evidence-item" key={item.source_id || item.url || index}>
                    <span className="evidence-index">{String(index + 1).padStart(2, "0")}</span>
                    <div>
                      <strong>{item.title || item.source || item.label || "Source reference"}</strong>
                      {item.excerpt && <p>{item.excerpt}</p>}
                      {item.url && <a href={item.url} target="_blank" rel="noreferrer">Open source <ExternalLink size={12} /></a>}
                    </div>
                  </div>
                ))}
              </div>
            ) : <EmptyState title="No itemised sources" description="The event carries no evidence array in this response. It is not treated as verified by the desk." />}
          </div>
        </div>
        <aside className="detail-aside">
          <div className="detail-fact"><span>Verification</span><StatusBadge value={event.verification || event.verification_state || event.status || "recorded"} /></div>
          <div className="detail-fact"><span>Confidence</span><strong>{event.confidence !== undefined ? pct(Number(event.confidence) <= 1 ? Number(event.confidence) * 100 : event.confidence) : "—"}</strong></div>
          <div className="detail-fact"><span>Relevance</span><strong>{event.relevance !== undefined ? pct(Number(event.relevance) <= 1 ? Number(event.relevance) * 100 : event.relevance) : "—"}</strong></div>
          <div className="detail-fact"><span>Related instruments</span><strong>{Array.isArray(instruments) ? instruments.length : "—"}</strong></div>
          <div className="detail-fact"><span>Event ID</span><code>{event.event_id || event.id || "—"}</code></div>
        </aside>
      </div>
    </Modal>
  );
}
