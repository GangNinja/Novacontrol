import {
  Activity,
  ArrowLeftRight,
  Bell,
  BookOpen,
  Brain,
  BriefcaseBusiness,
  ChartNoAxesCombined,
  ChartPie,
  Coins,
  Eye,
  Gauge,
  HeartPulse,
  History,
  LayoutDashboard,
  Layers3,
  ListChecks,
  Newspaper,
  Radar,
  ReceiptText,
  Settings2,
  ShieldCheck,
  Target,
  TrendingUp,
  WalletCards,
  Waypoints,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

export type NavItem = {
  id: string;
  label: string;
  icon: LucideIcon;
  keywords?: string;
};

export type NavGroup = {
  label: string;
  items: NavItem[];
};

export const NAV_GROUPS: NavGroup[] = [
  {
    label: "Overview",
    items: [
      { id: "dashboard", label: "Dashboard", icon: LayoutDashboard },
      { id: "market", label: "Market Overview", icon: ChartNoAxesCombined },
    ],
  },
  {
    label: "Trading desks",
    items: [
      { id: "orderblock", label: "OrderBlock", icon: Layers3, keywords: "order block structure liquidity" },
      { id: "tradingwatch", label: "TradingWatch", icon: Activity, keywords: "momentum heatmap replay watchlist" },
    ],
  },
  {
    label: "Intelligence",
    items: [
      { id: "events", label: "Market Events", icon: Newspaper },
      { id: "discovery", label: "Asset Discovery", icon: Radar },
      { id: "watchlists", label: "Watchlists", icon: ListChecks },
      { id: "forecasts", label: "Forecasts", icon: TrendingUp },
    ],
  },
  {
    label: "Portfolio Management",
    items: [
      { id: "portfolios/short", label: "Day Trading Portfolio", icon: BriefcaseBusiness },
      { id: "portfolios/medium", label: "Swing Trading Portfolio", icon: Waypoints },
      { id: "portfolios/long", label: "Long-Term Portfolio", icon: ChartNoAxesCombined },
      { id: "analytics", label: "Portfolio Analytics", icon: ChartPie },
    ],
  },
  {
    label: "Trading Intelligence",
    items: [
      { id: "strategies", label: "Strategies", icon: Target },
      { id: "risk", label: "Risk Management", icon: ShieldCheck },
      { id: "paper", label: "Paper Trading", icon: Gauge },
      { id: "backtesting", label: "Backtesting", icon: ChartNoAxesCombined },
      { id: "shadow", label: "Shadow Mode", icon: Eye },
      { id: "learning", label: "Adaptive Learning", icon: Brain },
    ],
  },
  {
    label: "Financial Records",
    items: [
      { id: "wallet", label: "NovaCoin Wallet", icon: WalletCards },
      { id: "transactions", label: "Transaction History", icon: ReceiptText },
      { id: "ledger", label: "Ledger", icon: BookOpen },
    ],
  },
  {
    label: "System",
    items: [
      { id: "autonomy", label: "Autonomy", icon: ShieldCheck },
      { id: "notifications", label: "Notifications", icon: Bell },
      { id: "activity", label: "Activity and Audit", icon: Activity },
      { id: "health", label: "System Health", icon: HeartPulse },
      { id: "settings", label: "Settings", icon: Settings2 },
    ],
  },
];

export const ALL_NAV_ITEMS = NAV_GROUPS.flatMap((group) => group.items);

export const ROUTE_TITLES: Record<string, string> = {
  dashboard: "Dashboard",
  market: "Market Overview",
  orderblock: "OrderBlock",
  tradingwatch: "TradingWatch",
  events: "Market Events",
  discovery: "Asset Discovery",
  watchlists: "Watchlists",
  forecasts: "Forecasts",
  "portfolios/short": "Day Trading Portfolio",
  "portfolios/medium": "Swing Trading Portfolio",
  "portfolios/long": "Long-Term Portfolio",
  analytics: "Portfolio Analytics",
  strategies: "Strategies",
  risk: "Risk Management",
  paper: "Paper Trading",
  backtesting: "Backtesting",
  shadow: "Shadow Mode",
  learning: "Adaptive Learning",
  wallet: "NovaCoin Wallet",
  transactions: "Transaction History",
  ledger: "Ledger",
  autonomy: "Autonomy",
  notifications: "Notifications",
  activity: "Activity and Audit",
  health: "System Health",
  settings: "Settings",
};

export function titleForRoute(page: string, section: string): string {
  if (page === "portfolios") return ROUTE_TITLES[`portfolios/${section}`] || "Portfolio";
  return ROUTE_TITLES[page] || "NovaMarket";
}

export const UTILITY_ICONS = { ArrowLeftRight, Coins, History, ReceiptText };
