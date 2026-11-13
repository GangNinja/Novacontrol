import { Component, useEffect, useMemo, useState, type ReactNode } from "react";
import {
  Bell,
  ChevronLeft,
  ChevronRight,
  Command,
  KeyRound,
  Menu,
  RefreshCw,
  ShieldCheck,
  X,
  Zap,
} from "lucide-react";
import { setToken, getToken } from "./api";
import { useApi, useLocalState, useRoute, navigate } from "./hooks";
import { ALL_NAV_ITEMS, NAV_GROUPS, titleForRoute } from "./nav";
import { arrayFrom, dateTime, num, timeAgo } from "./lib";
import {
  Button,
  IconButton,
  Modal,
  TextInput,
  Field,
  useToast,
} from "./components/ui";
import { DashboardPage } from "./pages/Dashboard";
import { MarketPage } from "./pages/MarketOverview";
import { OrderBlockPage } from "./pages/OrderBlock";
import { TradingWatchPage } from "./pages/TradingWatch";
import { EventsPage } from "./pages/Market";
import { DiscoveryPage, WatchlistsPage, ForecastsPage } from "./pages/Intelligence";
import { PortfoliosPage, PortfolioAnalyticsPage } from "./pages/Portfolios";
import { StrategiesPage, RiskPage, PaperPage, BacktestingPage } from "./pages/Trading";
import { ShadowPage, LearningPage } from "./pages/ShadowLearning";
import { WalletPage, TransactionsPage, LedgerPage } from "./pages/Records";
import { NotificationsPage, ActivityPage, HealthPage, SettingsPage } from "./pages/System";
import { AutonomyPage } from "./pages/Autonomy";

function Brand() {
  return (
    <div className="brand">
      <div className="brand-mark" aria-hidden="true">
        <span>N</span>
        <i />
      </div>
      <div className="brand-copy">
        <strong>NovaMarket</strong>
        <span>Intelligence desk</span>
      </div>
    </div>
  );
}

function Sidebar({
  route,
  collapsed,
  mobileOpen,
  onCloseMobile,
  onToggleCollapse,
}: {
  route: ReturnType<typeof useRoute>;
  collapsed: boolean;
  mobileOpen: boolean;
  onCloseMobile: () => void;
  onToggleCollapse: () => void;
}) {
  const activeId = route.page === "portfolios" ? `portfolios/${route.section || "short"}` : route.page;
  return (
    <>
      <aside className={`sidebar ${collapsed ? "sidebar-collapsed" : ""} ${mobileOpen ? "sidebar-mobile-open" : ""}`}>
        <div className="sidebar-head">
          <Brand />
          <IconButton label="Close navigation" className="sidebar-mobile-close" onClick={onCloseMobile}>
            <X size={18} />
          </IconButton>
        </div>
        <nav className="sidebar-nav" aria-label="NovaMarket navigation">
          {NAV_GROUPS.map((group) => (
            <div className="nav-group" key={group.label}>
              <div className="nav-group-label">{group.label}</div>
              {group.items.map((item) => {
                const Icon = item.icon;
                return (
                  <button
                    type="button"
                    className={`nav-item ${activeId === item.id ? "nav-item-active" : ""}`}
                    key={item.id}
                    onClick={() => {
                      navigate(item.id);
                      onCloseMobile();
                    }}
                    title={collapsed ? item.label : undefined}
                  >
                    <Icon size={17} strokeWidth={1.8} />
                    <span>{item.label}</span>
                    {item.id === "notifications" && <i className="nav-ping" />}
                  </button>
                );
              })}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div className="paper-chip"><ShieldCheck size={13} /> Paper capital only</div>
          <div className="sidebar-foot-row">
            <span>1 NC = ₹1</span>
            <span>NM-14</span>
          </div>
        </div>
        <button type="button" className="collapse-button" onClick={onToggleCollapse} aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}>
          {collapsed ? <ChevronRight size={15} /> : <ChevronLeft size={15} />}
        </button>
      </aside>
      {mobileOpen && <button type="button" className="sidebar-scrim" aria-label="Close navigation" onClick={onCloseMobile} />}
    </>
  );
}

function AuthDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const [value, setValue] = useState(getToken());
  const [show, setShow] = useState(false);
  const toast = useToast();
  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Connect to the API"
      description="The NovaMarket backend is protected by a bearer token. It is kept in this browser tab's session storage and is never bundled or logged."
      footer={
        <>
          <Button variant="ghost" onClick={() => { setToken(""); setValue(""); onClose(); window.location.reload(); }}>Clear token</Button>
          <Button variant="primary" icon={<KeyRound size={15} />} onClick={() => { setToken(value); toast.push("Session token saved. Reloading the surface.", "success"); onClose(); window.location.reload(); }}>Save and connect</Button>
        </>
      }
    >
      <Field label="API bearer token" hint="Set NOVACONTROL_API_TOKEN on the backend. Leave empty only when the backend runs without authentication.">
        <div className="input-with-action">
          <TextInput
            type={show ? "text" : "password"}
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder="Paste the token from your local environment"
            autoComplete="off"
          />
          <Button size="sm" variant="quiet" onClick={() => setShow((current) => !current)}>{show ? "Hide" : "Show"}</Button>
        </div>
      </Field>
    </Modal>
  );
}

class PageBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  render() {
    if (this.state.error) {
      return (
        <div className="page">
          <div className="error-block" role="alert">
            <div>
              <strong>This surface could not render</strong>
              <p>{this.state.error.message}</p>
              <p className="muted">The rest of the desk is unaffected. Retry the surface, or switch to another section.</p>
            </div>
            <Button size="sm" variant="secondary" onClick={() => this.setState({ error: null })}>Retry surface</Button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

function PageForRoute({ route }: { route: ReturnType<typeof useRoute> }) {
  switch (route.page) {
    case "dashboard": return <DashboardPage />;
    case "market": return <MarketPage />;
    case "orderblock": return <OrderBlockPage />;
    case "tradingwatch": return <TradingWatchPage />;
    case "events": return <EventsPage />;
    case "discovery": return <DiscoveryPage />;
    case "watchlists": return <WatchlistsPage />;
    case "forecasts": return <ForecastsPage />;
    case "portfolios": return <PortfoliosPage horizon={route.section || "short"} />;
    case "analytics": return <PortfolioAnalyticsPage />;
    case "strategies": return <StrategiesPage />;
    case "risk": return <RiskPage />;
    case "paper": return <PaperPage />;
    case "backtesting": return <BacktestingPage />;
    case "shadow": return <ShadowPage />;
    case "learning": return <LearningPage />;
    case "wallet": return <WalletPage />;
    case "transactions": return <TransactionsPage />;
    case "ledger": return <LedgerPage />;
    case "autonomy": return <AutonomyPage />;
    case "notifications": return <NotificationsPage />;
    case "activity": return <ActivityPage />;
    case "health": return <HealthPage />;
    case "settings": return <SettingsPage />;
    default:
      return (
        <div className="page">
          <div className="empty-state standalone-empty">
            <h2>Surface not found</h2>
            <p>The route <code>{route.path}</code> is not part of the NovaMarket desk.</p>
            <Button variant="primary" onClick={() => navigate("dashboard")}>Return to dashboard</Button>
          </div>
        </div>
      );
  }
}

export default function App() {
  const route = useRoute();
  const [collapsed, setCollapsed] = useLocalState("novamarket.sidebarCollapsed", false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [authOpen, setAuthOpen] = useState(false);
  const [lastSynced, setLastSynced] = useState<string>("");
  const overview = useApi<any>("/market/overview", { refreshMs: 60_000 });
  const alerts = useApi<any>("/market/risk/alerts?status=open&limit=50", { refreshMs: 60_000 });
  const learningAlerts = useApi<any>("/market/learning/alerts?status=open&limit=50", { refreshMs: 60_000 });

  const pageTitle = titleForRoute(route.page, route.section);
  const apiError = overview.error;
  const apiStatus = apiError?.status;
  const summary = overview.data?.summary || {};
  const health = overview.data?.status?.health?.state || (overview.error ? "offline" : "checking");
  const lifecycle = overview.data?.status?.lifecycle || "unknown";
  const paused = Boolean(overview.data?.status?.paused);
  const session = overview.data?.status?.market_session || {};
  const openAlerts = arrayFrom(alerts.data, "alerts").length + arrayFrom(learningAlerts.data, "alerts").length;

  useEffect(() => {
    if (!window.location.hash) window.location.hash = "#/dashboard";
    document.title = `${pageTitle} · NovaMarket`;
  }, [pageTitle]);

  useEffect(() => {
    const unauthorized = () => setAuthOpen(true);
    window.addEventListener("novamarket:unauthorized", unauthorized);
    return () => window.removeEventListener("novamarket:unauthorized", unauthorized);
  }, []);

  useEffect(() => {
    if (overview.data?.generated_at) setLastSynced(overview.data.generated_at);
  }, [overview.data?.generated_at]);

  const syncLabel = overview.loading
    ? "Connecting…"
    : apiError
      ? apiStatus === 429
        ? "API busy — retry shortly"
        : apiStatus === 401
          ? "API token required"
          : "Backend offline"
      : `Synced ${timeAgo(lastSynced || overview.data?.generated_at)}`;

  const statusLabel = useMemo(() => {
    if (overview.loading) return "NovaMarket service: connecting";
    if (apiError) {
      if (apiStatus === 429) return "NovaMarket service: rate limited";
      if (apiStatus === 401) return "NovaMarket service: authentication required";
      return "NovaMarket service: offline";
    }
    if (paused) return "NovaMarket service: paused";
    if (lifecycle === "running") return "NovaMarket service: running";
    if (lifecycle === "ready") return "NovaMarket service: ready";
    if (lifecycle === "disabled" || lifecycle === "shutting_down") return "NovaMarket service: stopped";
    return `NovaMarket service: ${String(lifecycle).replace(/_/g, " ")}`;
  }, [overview.loading, apiError, apiStatus, paused, lifecycle]);

  const sessionLabel = String(session.label || "NSE/BSE session unknown");
  const sessionState = String(session.state || "unknown");
  const sessionClass = sessionState === "open"
    ? "live"
    : sessionState === "pre_open"
      ? "waiting"
      : "closed";

  return (
    <div className={`app-shell ${collapsed ? "app-shell-collapsed" : ""}`}>
      <a className="skip-link" href="#main-content">Skip to main content</a>
      <Sidebar
        route={route}
        collapsed={collapsed}
        mobileOpen={mobileOpen}
        onCloseMobile={() => setMobileOpen(false)}
        onToggleCollapse={() => setCollapsed(!collapsed)}
      />
      <div className="app-main">
        <header className="topbar">
          <div className="topbar-left">
            <IconButton label="Open navigation" className="mobile-menu" onClick={() => setMobileOpen(true)}>
              <Menu size={19} />
            </IconButton>
            <div className="breadcrumb">
              <span>NovaMarket</span>
              <i>/</i>
              <strong>{pageTitle}</strong>
            </div>
          </div>
          <div className="topbar-right">
            <span
              className={`live-state live-${apiError ? (apiStatus === 429 ? "waiting" : "offline") : overview.loading ? "waiting" : paused ? "waiting" : "live"}`}
              title="NovaMarket subsystem state, not the stock exchange session"
            >
              <i />
              {statusLabel}
            </span>
            <span
              className={`live-state live-${sessionClass} session-state`}
              title={`${sessionLabel} · ${String(session.timezone || "Asia/Kolkata")}${session.coverage === false ? " · holiday coverage not verified" : ""}`}
            >
              <i />
              {sessionLabel}
            </span>
            <span className="sync-state">{syncLabel}</span>
            <IconButton label="Refresh live data" onClick={() => { overview.refresh(); alerts.refresh(); learningAlerts.refresh(); setLastSynced(new Date().toISOString()); }}>
              <RefreshCw size={16} className={overview.refreshing ? "spin" : undefined} />
            </IconButton>
            <button type="button" className="notification-button" aria-label={`${openAlerts} open alerts`} onClick={() => navigate("notifications")}>
              <Bell size={17} />
              {openAlerts > 0 && <span>{openAlerts > 9 ? "9+" : openAlerts}</span>}
            </button>
            <button type="button" className="api-key-button" onClick={() => setAuthOpen(true)} title="API session">
              <KeyRound size={15} />
              <span>{getToken() ? "API connected" : "API key"}</span>
            </button>
            <button type="button" className="profile-button" onClick={() => navigate("settings")} aria-label="Open settings">
              <span>OP</span>
              <div><strong>Operator</strong><small>Local session</small></div>
            </button>
          </div>
        </header>

        <main className="content" id="main-content" tabIndex={-1}>
          <PageBoundary key={route.path}>
            <PageForRoute route={route} />
          </PageBoundary>
        </main>

        <footer className="app-footer">
          <span><Zap size={12} /> NovaMarket NM-14 · authoritative backend queries only</span>
          <span><Command size={12} /> Paper, backtests and shadow are separate books</span>
          <span>{dateTime(new Date().toISOString())}</span>
        </footer>
      </div>
      <AuthDialog open={authOpen} onClose={() => setAuthOpen(false)} />
    </div>
  );
}
