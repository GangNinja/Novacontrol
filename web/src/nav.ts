import {
  Activity,
  ArrowLeftRight,
  Bell,
  BookOpen,
  Brain,
  BrainCircuit,
  BriefcaseBusiness,
  CalendarClock,
  ChartNoAxesCombined,
  ChartPie,
  Coins,
  Eye,
  FileSearch,
  Gauge,
  HeartPulse,
  History,
  LayoutDashboard,
  Layers3,
  ListChecks,
  Newspaper,
  Network,
  Radar,
  ReceiptText,
  Settings2,
  SlidersHorizontal,
  ShieldCheck,
  Target,
  TrendingUp,
  WalletCards,
  Waypoints,
  FlaskConical,
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
      { id: "eventintelligence", label: "Event Intelligence", icon: CalendarClock, keywords: "event intelligence verified events earnings macro relevance impact warnings outcomes" },
      { id: "discovery", label: "Asset Discovery", icon: Radar },
      { id: "watchlists", label: "Watchlists", icon: ListChecks },
      { id: "forecasts", label: "Forecasts", icon: TrendingUp },
      { id: "predictionintel", label: "Prediction Intelligence", icon: Network, keywords: "dashboard explainability model comparison uncertainty lifecycle regimes agents evidence replay" },
      { id: "forecastoptimization", label: "Forecast Optimization", icon: TrendingUp, keywords: "forecast optimization selection calibration uncertainty disagreement drift degradation reliability fallback weights" },
      { id: "evaluation", label: "Prediction Evaluation", icon: FlaskConical, keywords: "backtest accuracy calibration regime walk-forward" },
    ],
  },
  {
    label: "Portfolio Management",
    items: [
      { id: "portfolios/short", label: "Day Trading Portfolio", icon: BriefcaseBusiness },
      { id: "portfolios/medium", label: "Swing Trading Portfolio", icon: Waypoints },
      { id: "portfolios/long", label: "Long-Term Portfolio", icon: ChartNoAxesCombined },
      { id: "portfoliomanagement", label: "Autonomous Management", icon: BriefcaseBusiness, keywords: "portfolio management positions opportunities capital cash exposure rebalancing decisions autonomy" },
      { id: "riskintelligence", label: "Risk Intelligence", icon: ShieldCheck, keywords: "risk intelligence concentration correlation volatility drawdown liquidity var cvar scenarios stress alerts recommendations" },
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
      { id: "rlresearch", label: "RL Research", icon: FlaskConical, keywords: "reinforcement learning environment simulation experience replay" },
      { id: "rewards", label: "Reward Evaluation", icon: FlaskConical, keywords: "reward policy evaluation baseline attribution simulation" },
      { id: "rltraining", label: "RL Training", icon: BrainCircuit, keywords: "reinforcement learning dqn training checkpoint hyperparameter policy artifact" },
      { id: "adaptive", label: "Adaptive Intelligence", icon: Brain, keywords: "market regime drift suitability degradation retraining adaptation" },
      { id: "multiagent", label: "Multi-Agent Intelligence", icon: Network, keywords: "agents coordinator evidence conflict research recommendation" },
      { id: "optimization", label: "Portfolio Optimization", icon: SlidersHorizontal, keywords: "capital allocation weights covariance diversification rebalancing scenarios backtest" },
      { id: "financialresearch", label: "Financial Research", icon: FileSearch, keywords: "company fundamentals statements valuation news events peers thesis evidence" },
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
  eventintelligence: "Event Intelligence",
  discovery: "Asset Discovery",
  watchlists: "Watchlists",
  forecasts: "Forecasts",
  predictionintel: "Prediction Intelligence",
  forecastoptimization: "Forecast Optimization",
  evaluation: "Prediction Evaluation",
  "portfolios/short": "Day Trading Portfolio",
  "portfolios/medium": "Swing Trading Portfolio",
  "portfolios/long": "Long-Term Portfolio",
  portfoliomanagement: "Portfolio Management",
  riskintelligence: "Risk Intelligence",
  analytics: "Portfolio Analytics",
  strategies: "Strategies",
  risk: "Risk Management",
  paper: "Paper Trading",
  backtesting: "Backtesting",
  rlresearch: "RL Research",
  rewards: "Reward & Policy Evaluation",
  rltraining: "RL Training & Optimisation",
  adaptive: "Adaptive Intelligence",
  multiagent: "Multi-Agent Intelligence",
  optimization: "Portfolio Optimization",
  financialresearch: "Financial Research",
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
