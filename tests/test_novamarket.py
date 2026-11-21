"""NM-01 tests: boundaries, contracts, lifecycle, integration and isolation.

These are not documentation tests. Each one drives the real objects NovaControl
uses — the real ``LifecycleMachine``, the real event bus, the real permission and
capability registries, the real ``JsonStateStore`` — so a claim made in
``docs/NOVAMARKET.md`` has a failing test behind it if it stops being true.

The financial invariants are tested through the contracts themselves (Money
arithmetic, quote timestamps, append-only history, horizon profiles, the
permission matrix), never by asserting a constant that could drift from the code.
"""

from __future__ import annotations

import ast
import re
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from novacontrol.application import NovaControlApplication
from novacontrol.core.diagnostics import HealthState
from novacontrol.core.events import (
    EVENT_PAYLOAD_FIELDS,
    Event,
    EventBus,
    EventType,
    InvalidEventPayloadError,
)
from novacontrol.core.interfaces import RuntimeModule
from novacontrol.core.runtime import EventDrivenRuntime
from novacontrol.intelligence.intent import CapabilityRegistry
from novacontrol.novamarket import (
    ALLOWED_TRANSITIONS,
    CAPABILITIES,
    DOMAIN_BOUNDARIES,
    HORIZON_PROFILES,
    IMPLEMENTED_EVENTS,
    MARKET_CONTRACT_VERSION,
    ROADMAP,
    ROADMAP_PHASES,
    STORAGE_BOUNDARIES,
    ConfigurationError,
    Currency,
    InvestmentHorizon,
    LifecycleError,
    LifecycleMachine,
    LifecycleState,
    MarketDataProvider,
    MarketEventPublisher,
    MarketOperation,
    MarketPermissionModel,
    MarketQuote,
    Money,
    NOVA_COIN_INR_RATE,
    NovaMarketConfig,
    NovaMarketError,
    NovaMarketInitializationError,
    NovaMarketModule,
    NovaMarketService,
    NotOperationalError,
    PriceHistory,
    ResourceLimits,
    SecretReference,
    profile_for,
    validate_architecture,
)
from novacontrol.novamarket.contracts import (
    DuplicatePriceError,
    HistoryLimitError,
)
from novacontrol.novamarket.events import (
    FUTURE_EVENT_SPECS,
    FUTURE_EVENTS,
    validate_market_event,
)
from novacontrol.novamarket.horizons import validate_horizon_profiles
from novacontrol.persistence import JsonStateStore
from novacontrol.reliability.permissions import PermissionManager

_ROOT = Path(__file__).resolve().parents[1]
_PACKAGE = _ROOT / "src" / "novacontrol" / "novamarket"

_IMPLEMENTED_PAYLOADS: dict[EventType, dict[str, object]] = {
    EventType.NOVAMARKET_INITIALIZING: {"contract_version": MARKET_CONTRACT_VERSION},
    EventType.NOVAMARKET_INITIALIZED: {
        "contract_version": MARKET_CONTRACT_VERSION,
        "capabilities": ("market.status",),
    },
    EventType.NOVAMARKET_READY: {"lifecycle": "ready"},
    EventType.NOVAMARKET_ENABLED: {"lifecycle": "running"},
    EventType.NOVAMARKET_DISABLED: {"reason": "test"},
    EventType.NOVAMARKET_CONFIGURATION_REQUESTED: {"configuration": {}},
    EventType.NOVAMARKET_CONFIGURATION_CHANGED: {"changed": ("mode",)},
    EventType.NOVAMARKET_CAPABILITIES_REGISTERED: {
        "capabilities": ("market.status",),
        "count": 1,
    },
    EventType.NOVAMARKET_HEALTH_CHANGED: {"health": "ok", "previous": "skipped"},
    EventType.NOVAMARKET_WARNING: {"code": "test", "detail": "test"},
    EventType.NOVAMARKET_ERROR: {"error_type": "RuntimeError", "error": "boom"},
    EventType.NOVAMARKET_RL_EPISODE_STARTED: {"environment_id": "rl_env_1", "episode_id": "rl_ep_1"},
    EventType.NOVAMARKET_RL_STEP: {"environment_id": "rl_env_1", "episode_id": "rl_ep_1", "sequence": 1, "action": "hold"},
    EventType.NOVAMARKET_RL_EPISODE_COMPLETED: {"environment_id": "rl_env_1", "episode_id": "rl_ep_1", "status": "terminated", "steps": 1},
    EventType.NOVAMARKET_RL_EPISODE_RESET: {"environment_id": "rl_env_1", "episode_id": "rl_ep_1"},
    EventType.NOVAMARKET_RL_EPISODE_CANCELLED: {"environment_id": "rl_env_1", "episode_id": "rl_ep_1"},
    EventType.NOVAMARKET_SHUTDOWN: {"reason": "test"},
    # NM-03 wallet and ledger vocabulary: every payload names stored records.
    EventType.NOVAMARKET_WALLET_CREATED: {"wallet_id": "wal_1", "owner": "alice"},
    EventType.NOVAMARKET_WALLET_BALANCE_UPDATED: {
        "wallet_id": "wal_1",
        "balance": "100.00",
    },
    EventType.NOVAMARKET_WALLET_DEPOSIT_COMPLETED: {
        "wallet_id": "wal_1",
        "transaction_id": "txn_1",
        "amount": "100.00",
    },
    EventType.NOVAMARKET_WALLET_WITHDRAWAL_COMPLETED: {
        "wallet_id": "wal_1",
        "transaction_id": "txn_2",
        "amount": "25.00",
    },
    EventType.NOVAMARKET_WALLET_TRANSFER_COMPLETED: {
        "from_wallet_id": "wal_1",
        "to_wallet_id": "wal_2",
        "transaction_id": "txn_3",
        "amount": "40.00",
    },
    EventType.NOVAMARKET_WALLET_TRANSACTION_FAILED: {
        "operation": "transfer",
        "reason": "insufficient funds",
    },
    EventType.NOVAMARKET_LEDGER_ENTRY_CREATED: {
        "entry_id": "led_1",
        "transaction_id": "txn_1",
        "wallet_id": "wal_1",
    },
    EventType.NOVAMARKET_LEDGER_RECONCILIATION_COMPLETED: {
        "wallets_checked": 2,
        "ok": True,
    },
    EventType.NOVAMARKET_LEDGER_INTEGRITY_VIOLATION: {
        "wallet_id": "wal_1",
        "code": "balance_mismatch",
    },
    # NM-02 market-data vocabulary.
    EventType.NOVAMARKET_DATA_AVAILABLE: {
        "assets": ("XNSE:RELIANCE",),
        "sources": ("yahoo",),
    },
    EventType.NOVAMARKET_DATA_UNAVAILABLE: {"reason": "no provider supplied data"},
    EventType.NOVAMARKET_DATA_RECEIVED: {
        "instrument_id": "XNSE:RELIANCE",
        "provider": "yahoo",
        "quality": "valid",
    },
    EventType.NOVAMARKET_DATA_VALIDATED: {
        "instrument_id": "XNSE:RELIANCE",
        "quality": "valid",
    },
    EventType.NOVAMARKET_DATA_REJECTED: {
        "instrument_id": "XNSE:RELIANCE",
        "provider": "yahoo",
        "reason": "high below low",
    },
    EventType.NOVAMARKET_DATA_STALE: {
        "asset": "XNSE:RELIANCE",
        "provider": "yahoo",
        "age_seconds": 3600.0,
    },
    EventType.NOVAMARKET_DATA_FRESHNESS_CHANGED: {
        "asset": "XNSE:RELIANCE",
        "fresh": False,
    },
    EventType.NOVAMARKET_DATA_PROVIDER_UNAVAILABLE: {
        "provider": "yahoo",
        "reason": "timeout",
    },
    EventType.NOVAMARKET_DATA_PROVIDER_RECOVERED: {
        "provider": "yahoo",
        "circuit_state": "closed",
    },
    EventType.NOVAMARKET_DATA_QUALITY_CHANGED: {
        "instrument_id": "XNSE:RELIANCE",
        "previous": "valid",
        "quality": "suspect",
    },
    EventType.NOVAMARKET_DATA_INGESTION_FAILED: {
        "error_type": "ProviderUnavailableError",
        "error": "timeout",
    },
    # NM-04 market event vocabulary: each payload names the stored record.
    EventType.NOVAMARKET_MARKET_EVENT_DISCOVERED: {
        "event_id": "evt_1",
        "source_id": "rbi_press",
    },
    EventType.NOVAMARKET_MARKET_EVENT_EXTRACTED: {
        "event_id": "evt_1",
        "category": "macroeconomic",
    },
    EventType.NOVAMARKET_MARKET_EVENT_NORMALIZED: {
        "event_id": "evt_1",
        "instruments": ("XNSE:RELIANCE",),
    },
    EventType.NOVAMARKET_MARKET_EVENT_VERIFIED: {
        "event_id": "evt_1",
        "state": "single_source",
    },
    EventType.NOVAMARKET_MARKET_EVENT_CLASSIFIED: {
        "event_id": "evt_1",
        "importance": "moderate",
    },
    EventType.NOVAMARKET_MARKET_EVENT_UPDATED: {"event_id": "evt_1", "version": 2},
    EventType.NOVAMARKET_MARKET_EVENT_RETRACTED: {
        "event_id": "evt_1",
        "reason": "issuer withdrew the statement",
    },
    EventType.NOVAMARKET_MARKET_EVENT_EXPIRED: {
        "event_id": "evt_1",
        "reason": "review window closed",
    },
    EventType.NOVAMARKET_MARKET_EVENT_SOURCE_UNAVAILABLE: {
        "source_id": "rbi_press",
        "code": "timeout",
    },
    EventType.NOVAMARKET_MARKET_EVENT_EVIDENCE_CONFLICT: {
        "event_id": "evt_1",
        "detail": "two sources report different figures",
    },
}


class ArchitectureTests(unittest.TestCase):
    """The package boundary, the roadmap and the declared domains."""

    def test_the_canonical_roadmap_is_preserved_verbatim(self) -> None:
        self.assertEqual(len(ROADMAP), 14)
        self.assertEqual(
            [phase for phase, _ in ROADMAP],
            [f"NM-{index:02d}" for index in range(1, 15)],
        )
        self.assertEqual(ROADMAP[0], ("NM-01", "Architecture, Contracts & Integration"))
        self.assertEqual(
            ROADMAP[1], ("NM-02", "Market Data Reliability")
        )
        self.assertEqual(
            ROADMAP[-1], ("NM-14", "Integrated Workspace & Final Acceptance")
        )

    def test_twelve_distinct_domain_boundaries_are_declared(self) -> None:
        domains = [boundary.domain for boundary in DOMAIN_BOUNDARIES]
        self.assertEqual(len(domains), 12)
        self.assertEqual(len(set(domains)), 12)
        self.assertEqual(
            set(domains),
            {
                "market_data",
                "asset_discovery",
                "market_intelligence",
                "forecasting",
                "portfolio_management",
                "risk_management",
                "strategy_management",
                "paper_execution",
                "backtesting",
                "accounting_ledger",
                "adaptive_learning",
                "observability_audit",
            },
        )

    def test_declarations_are_structurally_consistent(self) -> None:
        self.assertEqual(validate_architecture(), ())
        self.assertEqual(validate_horizon_profiles(), ())

    def test_the_risk_boundary_never_executes_its_own_subject(self) -> None:
        risk = next(
            boundary
            for boundary in DOMAIN_BOUNDARIES
            if boundary.domain == "risk_management"
        )
        self.assertFalse(risk.executes_actions)

    def test_every_storage_family_has_an_owner_and_a_phase(self) -> None:
        domains = {boundary.domain for boundary in DOMAIN_BOUNDARIES}
        for storage in STORAGE_BOUNDARIES:
            self.assertIn(storage.owner, domains, storage.data)
            self.assertIn(storage.phase, ROADMAP_PHASES, storage.data)
        historical = next(
            storage for storage in STORAGE_BOUNDARIES if storage.data == "historical_prices"
        )
        self.assertTrue(historical.append_only)

    def test_the_package_imports_no_feature_subsystem(self) -> None:
        """NovaMarket must not depend on another feature's internals."""
        forbidden = (
            "novacontrol.desktop",
            "novacontrol.browser",
            "novacontrol.phone",
            "novacontrol.novaforge",
            "novacontrol.explore",
            "novacontrol.brain",
            "novacontrol.models",
            "novacontrol.tools",
            "novacontrol.vision",
            "novacontrol.perception",
            "novacontrol.world",
            "novacontrol.agentic",
            "novacontrol.evaluation",
            "novacontrol.training",
            "novacontrol.plugins",
            "novacontrol.automation",
            "novacontrol.application",
            "novacontrol.api",
            "novacontrol.web",
        )
        imported: set[str] = set()
        sources: list[str] = []
        for path in sorted(_PACKAGE.glob("*.py")):
            source = path.read_text(encoding="utf-8")
            sources.append(source)
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    imported.add(node.module)
                elif isinstance(node, ast.Import):
                    imported.update(alias.name for alias in node.names)
        for module in sorted(imported):
            for prefix in forbidden:
                self.assertFalse(
                    module == prefix or module.startswith(prefix + "."),
                    f"{prefix} must not be imported by NovaMarket ({module})",
                )
        self.assertFalse(any("class EventBus" in source for source in sources))

    def test_the_package_has_no_intra_package_import_cycle(self) -> None:
        """The modules may depend on each other, but only in one direction."""
        modules: dict[str, set[str]] = {}
        for path in sorted(_PACKAGE.glob("*.py")):
            name = f"novacontrol.novamarket.{path.stem}"
            dependencies: set[str] = set()
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                targets: list[str] = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    targets = [node.module]
                elif isinstance(node, ast.Import):
                    targets = [alias.name for alias in node.names]
                for target in targets:
                    if target.startswith("novacontrol.novamarket"):
                        dependencies.add(target)
            modules[name] = dependencies

        state: dict[str, int] = {}

        def visit(name: str, trail: tuple[str, ...]) -> None:
            if state.get(name) == 1:
                cycle = " -> ".join((*trail, name))
                self.fail(f"circular NovaMarket import: {cycle}")
            if state.get(name) == 2:
                return
            state[name] = 1
            for dependency in sorted(modules[name]):
                if dependency in modules:
                    visit(dependency, (*trail, name))
            state[name] = 2

        for module in sorted(modules):
            visit(module, ())

    def test_the_runtime_adapter_satisfies_the_shared_protocol(self) -> None:
        service = NovaMarketService()
        module = NovaMarketModule(service)
        self.assertIsInstance(module, RuntimeModule)
        self.assertEqual(module.name, "novamarket")
        self.assertTrue(module.capabilities)
        self.assertEqual(
            {capability.name for capability in module.capabilities},
            {item.capability_id for item in CAPABILITIES},
        )


class MoneyTests(unittest.TestCase):
    """NovaCoin = INR, decimal arithmetic, and refusal of binary floats."""

    def test_one_novacoin_is_one_rupee(self) -> None:
        self.assertEqual(NOVA_COIN_INR_RATE, Decimal("1"))
        self.assertEqual(Money.nc("12.50"), Money.inr("12.50"))
        self.assertEqual(Money.nc("12.50").nova_coin, Decimal("12.50"))
        self.assertEqual(Money.nc("12.50").format_inr(), "₹12.50")
        self.assertEqual(
            Money.nc("12.50").to_dict(),
            {"amount": "12.50", "currency": "INR", "nova_coin": "12.50"},
        )

    def test_decimal_arithmetic_is_exact(self) -> None:
        self.assertEqual(Money.inr("0.1") + Money.inr("0.2"), Money.inr("0.3"))
        self.assertEqual(
            (Money.inr("10.00") * "3").amount, Decimal("30.00")
        )
        self.assertEqual(
            (Money.inr("10.00") / "3").amount, Decimal("3.33")
        )
        # The configured rounding rule is banker's rounding, not truncation.
        self.assertEqual(Money.inr("10.005").amount, Decimal("10.00"))

    def test_binary_floats_are_refused(self) -> None:
        with self.assertRaises(TypeError):
            Money(0.1)
        with self.assertRaises(TypeError):
            Money.inr("1") * 0.5
        with self.assertRaises(TypeError):
            Money(True)

    def test_non_finite_and_unknown_currency_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            Money(Decimal("NaN"))
        # NM-02 quotes carry the provider's currency; USD is supported but
        # explicitly labelled, never silently treated as INR.
        self.assertEqual(Money(Decimal("1"), "USD").currency.value, "USD")
        with self.assertRaises(ValueError):
            Money(Decimal("1"), "XYZ")  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            Money(Decimal("1")) + "1"  # type: ignore[operator]

    def test_amounts_are_two_decimal_places(self) -> None:
        self.assertEqual(Currency(Money.inr("1").currency), Currency.INR)
        self.assertEqual(str(Money.inr("1").amount), "1.00")


class MarketQuoteTests(unittest.TestCase):
    """Prices are timestamped observations with provenance, never bare numbers."""

    def _quote(self, **overrides: object) -> MarketQuote:
        observed = datetime(2026, 10, 1, 3, 45, tzinfo=UTC)
        options = {
            "asset": "reliance",
            "price": Money.inr("2500.25"),
            "observed_at": observed,
            "source": "nse-bhavcopy",
            "retrieved_at": observed + timedelta(seconds=12),
        }
        options.update(overrides)
        return MarketQuote(**options)  # type: ignore[arg-type]

    def test_a_quote_carries_source_and_both_timestamps(self) -> None:
        quote = self._quote()
        payload = quote.to_dict()
        self.assertEqual(payload["asset"], "RELIANCE")
        self.assertEqual(payload["source"], "nse-bhavcopy")
        self.assertIn("observed_at", payload)
        self.assertIn("retrieved_at", payload)
        self.assertEqual(payload["price"]["amount"], "2500.25")

    def test_a_naive_timestamp_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self._quote(observed_at=datetime(2026, 10, 1, 3, 45))
        with self.assertRaises(ValueError):
            self._quote(
                retrieved_at=datetime(2026, 10, 1, 3, 45)
            )
        with self.assertRaises(ValueError):
            self._quote(source="")
        with self.assertRaises(ValueError):
            self._quote(asset=" ")
        with self.assertRaises(TypeError):
            self._quote(price=2500.25)

    def test_freshness_is_computed_from_the_observation_time(self) -> None:
        observed = datetime(2026, 10, 1, 3, 45, tzinfo=UTC)
        quote = self._quote(observed_at=observed, retrieved_at=observed)
        later = observed + timedelta(seconds=120)
        self.assertEqual(quote.age(now=later), timedelta(seconds=120))
        self.assertTrue(quote.is_stale(max_age=timedelta(minutes=1), now=later))
        self.assertFalse(quote.is_stale(max_age=timedelta(minutes=5), now=later))
        # A source clock ahead of ours reports zero age rather than negative time.
        self.assertEqual(quote.age(now=observed - timedelta(minutes=1)), timedelta(0))

    def test_the_market_data_provider_contract_is_structural(self) -> None:
        class _Provider:
            @property
            def name(self) -> str:
                return "fake"

            async def fetch_quotes(self, assets, *, observed_after=None):
                return ()

        self.assertIsInstance(_Provider(), MarketDataProvider)


class PriceHistoryTests(unittest.TestCase):
    """History is append-only: a newer price cannot overwrite an older one."""

    def _quote(self, observed: datetime, price: str = "100.00", source: str = "nse"):
        return MarketQuote("TCS", Money.inr(price), observed, source, observed)

    def test_a_new_price_does_not_overwrite_an_older_one(self) -> None:
        history = PriceHistory()
        first_at = datetime(2026, 10, 1, 3, 45, tzinfo=UTC)
        second_at = first_at + timedelta(days=1)
        first = self._quote(first_at, "100.00")
        second = self._quote(second_at, "110.00")
        history.append(second)
        history.append(first)

        self.assertEqual(len(history.quotes("TCS")), 2)
        self.assertEqual(history.latest("TCS"), second)
        self.assertIn(first, history.quotes("TCS"))
        self.assertIn(second, history.quotes("TCS"))

    def test_the_same_observation_cannot_be_appended_twice(self) -> None:
        history = PriceHistory()
        quote = self._quote(datetime(2026, 10, 1, 3, 45, tzinfo=UTC))
        history.append(quote)
        with self.assertRaises(DuplicatePriceError):
            history.append(quote)
        self.assertEqual(len(history), 1)

    def test_the_same_time_from_a_different_source_is_a_different_observation(self) -> None:
        history = PriceHistory()
        observed = datetime(2026, 10, 1, 3, 45, tzinfo=UTC)
        history.append(self._quote(observed, source="nse"))
        history.append(self._quote(observed, source="bse"))
        self.assertEqual(len(history), 2)

    def test_the_cap_refuses_rather_than_dropping_history(self) -> None:
        history = PriceHistory(max_quotes_per_asset=1)
        observed = datetime(2026, 10, 1, 3, 45, tzinfo=UTC)
        history.append(self._quote(observed))
        with self.assertRaises(HistoryLimitError):
            history.append(self._quote(observed + timedelta(hours=1)))
        self.assertEqual(len(history), 1)

    def test_history_exposes_no_mutation_door(self) -> None:
        history = PriceHistory()
        for name in ("update", "delete", "replace", "clear", "pop"):
            self.assertFalse(hasattr(history, name), name)


class HorizonTests(unittest.TestCase):
    """The three horizons must differ materially, not just by label."""

    def test_all_three_horizons_have_profiles(self) -> None:
        self.assertEqual(set(HORIZON_PROFILES), set(InvestmentHorizon))
        self.assertEqual(profile_for("swing_trading").horizon, InvestmentHorizon.SWING_TRADING)

    def test_holding_windows_increase_and_day_trading_is_intraday(self) -> None:
        day = HORIZON_PROFILES[InvestmentHorizon.DAY_TRADING]
        swing = HORIZON_PROFILES[InvestmentHorizon.SWING_TRADING]
        long_term = HORIZON_PROFILES[InvestmentHorizon.LONG_TERM]
        self.assertLess(day.max_holding_period, swing.max_holding_period)
        self.assertLess(swing.max_holding_period, long_term.max_holding_period)
        self.assertFalse(day.allows_overnight)
        self.assertTrue(swing.allows_overnight)
        self.assertTrue(long_term.allows_overnight)

    def test_each_horizon_has_its_own_ledger_namespace(self) -> None:
        namespaces = {
            profile.ledger_namespace for profile in HORIZON_PROFILES.values()
        }
        self.assertEqual(len(namespaces), 3)
        self.assertIn("portfolio.day", namespaces)

    def test_an_unknown_horizon_is_refused_by_name(self) -> None:
        with self.assertRaises(ValueError) as caught:
            profile_for("scalping")
        self.assertIn("scalping", str(caught.exception))


class ConfigTests(unittest.TestCase):
    """Validation, bounds, versioning and the no-secrets rule."""

    def test_the_defaults_validate_and_round_trip(self) -> None:
        config = NovaMarketConfig()
        self.assertEqual(config.mode, "observe")
        self.assertTrue(config.enabled)
        self.assertEqual(NovaMarketConfig.from_mapping(config.to_dict()), config)
        self.assertEqual(set(config.to_dict()), set(NovaMarketConfig.FIELDS))

    def test_unknown_fields_are_refused_not_ignored(self) -> None:
        with self.assertRaises(ConfigurationError) as caught:
            NovaMarketConfig.from_mapping({"stale_after_second": 30})
        self.assertIn("unknown configuration field", str(caught.exception))

    def test_credential_shaped_fields_are_refused(self) -> None:
        for field in ("api_key", "secret", "password", "broker_token"):
            with self.subTest(field=field):
                with self.assertRaises(ConfigurationError) as caught:
                    NovaMarketConfig.from_mapping({field: "value"})
                self.assertIn("credentials_ref", str(caught.exception))

    def test_only_observe_mode_is_operational(self) -> None:
        with self.assertRaises(ConfigurationError) as caught:
            NovaMarketConfig.from_mapping({"mode": "paper"})
        self.assertIn("NM-10", str(caught.exception))
        with self.assertRaises(ConfigurationError) as live:
            NovaMarketConfig.from_mapping({"mode": "live"})
        self.assertIn("out of scope", str(live.exception))

    def test_contract_and_schema_versions_are_enforced(self) -> None:
        with self.assertRaises(ConfigurationError):
            NovaMarketConfig.from_mapping({"schema_version": 2})
        with self.assertRaises(ConfigurationError):
            NovaMarketConfig.from_mapping({"contract_version": "0.9.0"})

    def test_numeric_bounds_are_enforced(self) -> None:
        for payload in (
            {"stale_after_seconds": 0},
            {"stale_after_seconds": 604_801},
            {"resources": {"max_assets": 0}},
            {"resources": {"max_quotes_per_asset": 100_001}},
            {"resources": {"max_concurrent_fetches": 17}},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ConfigurationError):
                    NovaMarketConfig.from_mapping(payload)

    def test_provider_references_are_names_not_urls(self) -> None:
        with self.assertRaises(ConfigurationError):
            NovaMarketConfig.from_mapping({"data_providers": "nse"})
        with self.assertRaises(ConfigurationError) as caught:
            NovaMarketConfig.from_mapping(
                {"data_providers": ["https://example.com/feed?key=abc"]}
            )
        # A rejected reference must not be echoed: the value a user pastes in
        # the wrong field is exactly the value that may contain a real key.
        self.assertNotIn("example.com", str(caught.exception))
        self.assertNotIn("abc", str(caught.exception))
        with self.assertRaises(ConfigurationError):
            NovaMarketConfig.from_mapping({"data_providers": ["nse", "NSE"]})
        config = NovaMarketConfig.from_mapping({"data_providers": ["NSE-Bhavcopy"]})
        self.assertEqual(config.data_providers, ("nse-bhavcopy",))

    def test_secret_references_never_carry_a_value(self) -> None:
        reference = SecretReference.parse("env:NOVAMARKET_DATA_KEY")
        self.assertEqual(str(reference), "env:NOVAMARKET_DATA_KEY")
        self.assertEqual(
            SecretReference.parse("store:market-data").to_dict(),
            {"scheme": "store", "name": "market-data"},
        )
        with self.assertRaises(ConfigurationError):
            SecretReference.parse("sk-live-abc123")
        with self.assertRaises(ConfigurationError):
            SecretReference.parse("env:bad-name-with-dashes")
        config = NovaMarketConfig.from_mapping(
            {"credentials_ref": "env:NOVAMARKET_DATA_KEY"}
        )
        self.assertEqual(config.to_dict()["credentials_ref"], "env:NOVAMARKET_DATA_KEY")
        self.assertNotIn("api_key", config.to_dict())

    def test_the_schema_lists_every_field_and_no_secret_field(self) -> None:
        schema = NovaMarketConfig.schema()
        self.assertEqual({row["field"] for row in schema}, set(NovaMarketConfig.FIELDS))
        self.assertNotIn("api_key", {row["field"] for row in schema})
        self.assertIn("reference", " ".join(str(row["notes"]) for row in schema))

    def test_resource_limits_require_integers(self) -> None:
        with self.assertRaises(ConfigurationError):
            ResourceLimits(max_assets=True)  # type: ignore[arg-type]


_IMPLEMENTED_PAYLOADS.update(
    {
        EventType.NOVAMARKET_STRATEGY_CREATED: {"strategy_id": "strategy_id", "catalog_id": "catalog_id", "category": "category"},
        EventType.NOVAMARKET_STRATEGY_ACTIVATED: {"strategy_id": "strategy_id", "reason": "reason", "actor": "actor"},
        EventType.NOVAMARKET_STRATEGY_PAUSED: {"strategy_id": "strategy_id", "reason": "reason", "actor": "actor"},
        EventType.NOVAMARKET_STRATEGY_DEGRADED: {"strategy_id": "strategy_id", "reason": "reason", "actor": "actor"},
        EventType.NOVAMARKET_STRATEGY_SUSPENDED: {"strategy_id": "strategy_id", "reason": "reason", "actor": "actor"},
        EventType.NOVAMARKET_STRATEGY_RETIRED: {"strategy_id": "strategy_id", "reason": "reason", "actor": "actor"},
        EventType.NOVAMARKET_STRATEGY_EVALUATED: {"strategy_id": "strategy_id", "portfolio_id": "portfolio_id", "signals": "signals"},
        EventType.NOVAMARKET_STRATEGY_SIGNAL_GENERATED: {"signal_id": "signal_id", "strategy_id": "strategy_id", "instrument_id": "instrument_id", "signal_type": "signal_type", "confidence": "confidence"},
        EventType.NOVAMARKET_STRATEGY_SIGNAL_EXPIRED: {"signal_id": "signal_id"},
        EventType.NOVAMARKET_STRATEGY_PROPOSAL_CREATED: {"proposal_id": "proposal_id", "strategy_id": "strategy_id", "signal_id": "signal_id", "instrument_id": "instrument_id"},
        EventType.NOVAMARKET_STRATEGY_PROPOSAL_RISK_REQUESTED: {"proposal_id": "proposal_id", "signal_id": "signal_id"},
        EventType.NOVAMARKET_STRATEGY_PROPOSAL_APPROVED: {"proposal_id": "proposal_id", "strategy_id": "strategy_id", "signal_id": "signal_id", "risk_decision_id": "risk_decision_id", "outcome": "outcome"},
        EventType.NOVAMARKET_STRATEGY_PROPOSAL_REJECTED: {"proposal_id": "proposal_id", "strategy_id": "strategy_id", "signal_id": "signal_id", "risk_decision_id": "risk_decision_id", "outcome": "outcome"},
        EventType.NOVAMARKET_STRATEGY_PROPOSAL_RESTRICTED: {"proposal_id": "proposal_id", "strategy_id": "strategy_id", "signal_id": "signal_id", "risk_decision_id": "risk_decision_id", "outcome": "outcome"},
        EventType.NOVAMARKET_STRATEGY_PROPOSAL_FORWARDED: {"proposal_id": "proposal_id", "strategy_id": "strategy_id", "signal_id": "signal_id", "order_id": "order_id", "order_status": "order_status"},
        EventType.NOVAMARKET_STRATEGY_CONFLICT_DETECTED: {"conflict_id": "conflict_id", "portfolio_id": "portfolio_id", "instrument_id": "instrument_id", "conflict_type": "conflict_type"},
        EventType.NOVAMARKET_STRATEGY_CONFLICT_RESOLVED: {"conflict_id": "conflict_id", "outcome": "outcome"},
        EventType.NOVAMARKET_STRATEGY_PERFORMANCE_UPDATED: {"strategy_id": "strategy_id", "observations": "observations", "degradation": "degradation"},
        # NM-08: independent risk management.
        EventType.NOVAMARKET_RISK_ASSESSMENT_REQUESTED: {"action": "action", "portfolio_id": "portfolio_id", "proposal_id": "proposal_id"},
        EventType.NOVAMARKET_RISK_ASSESSMENT_COMPLETED: {"decision_id": "decision_id", "outcome": "outcome", "alerts": "alerts", "latency_ms": "latency_ms"},
        EventType.NOVAMARKET_RISK_ACTION_APPROVED: {"decision_id": "decision_id", "action": "action", "portfolio_id": "portfolio_id", "proposal_id": "proposal_id", "outcome": "outcome", "policy_id": "policy_id", "policy_version": "policy_version", "violations": "violations"},
        EventType.NOVAMARKET_RISK_ACTION_REJECTED: {"decision_id": "decision_id", "action": "action", "portfolio_id": "portfolio_id", "proposal_id": "proposal_id", "outcome": "outcome", "policy_id": "policy_id", "policy_version": "policy_version", "violations": "violations"},
        EventType.NOVAMARKET_RISK_ACTION_RESTRICTED: {"decision_id": "decision_id", "action": "action", "portfolio_id": "portfolio_id", "proposal_id": "proposal_id", "outcome": "outcome", "policy_id": "policy_id", "policy_version": "policy_version", "violations": "violations"},
        EventType.NOVAMARKET_RISK_REVIEW_REQUIRED: {"decision_id": "decision_id", "action": "action", "portfolio_id": "portfolio_id", "proposal_id": "proposal_id", "outcome": "outcome", "policy_id": "policy_id", "policy_version": "policy_version", "violations": "violations"},
        EventType.NOVAMARKET_RISK_INSUFFICIENT_DATA: {"decision_id": "decision_id", "action": "action", "portfolio_id": "portfolio_id", "proposal_id": "proposal_id", "outcome": "outcome", "policy_id": "policy_id", "policy_version": "policy_version", "violations": "violations"},
        EventType.NOVAMARKET_RISK_LIMIT_BREACHED: {"decision_id": "decision_id", "portfolio_id": "portfolio_id", "codes": "codes"},
        EventType.NOVAMARKET_RISK_ALERT_CREATED: {"alert_id": "alert_id", "severity": "severity", "category": "category", "portfolio_id": "portfolio_id"},
        EventType.NOVAMARKET_RISK_ALERT_ACKNOWLEDGED: {"alert_id": "alert_id", "actor": "actor"},
        EventType.NOVAMARKET_RISK_ALERT_RESOLVED: {"alert_id": "alert_id", "actor": "actor"},
        EventType.NOVAMARKET_RISK_SCENARIO_COMPLETED: {"stress_id": "stress_id", "portfolio_id": "portfolio_id", "scenario": "scenario", "impact": "impact"},
        EventType.NOVAMARKET_RISK_POLICY_UPDATED: {"policy_id": "policy_id", "version": "version", "actor": "actor", "fields": "fields"},
        EventType.NOVAMARKET_RISK_OVERRIDE_RECORDED: {"override_id": "override_id", "decision_id": "decision_id", "actor": "actor", "expires_at": "expires_at"},
        EventType.NOVAMARKET_RISK_ACTION_APPLIED: {"decision_id": "decision_id", "proposal_id": "proposal_id", "portfolio_id": "portfolio_id", "actor": "actor"},
        # NM-07: three-horizon portfolios.
        EventType.NOVAMARKET_PORTFOLIO_CREATED: {
            "portfolio_id": "pf_1",
            "horizon": "short_term",
            "name": "Short-term opportunities",
        },
        EventType.NOVAMARKET_PORTFOLIO_ACTIVATED: {"portfolio_id": "pf_1"},
        EventType.NOVAMARKET_PORTFOLIO_ALLOCATION_UPDATED: {
            "portfolio_id": "pf_1",
            "positions": 2,
        },
        EventType.NOVAMARKET_PORTFOLIO_POSITION_ADDED: {
            "portfolio_id": "pf_1",
            "instrument_id": "XNSE:ALPHA",
            "quantity": "25.0000",
        },
        EventType.NOVAMARKET_PORTFOLIO_POSITION_UPDATED: {
            "portfolio_id": "pf_1",
            "instrument_id": "XNSE:ALPHA",
            "quantity": "10.0000",
            "price": "100.00",
        },
        EventType.NOVAMARKET_PORTFOLIO_POSITION_REMOVED: {
            "portfolio_id": "pf_1",
            "instrument_id": "XNSE:ALPHA",
            "reason": "sold to target weight",
        },
        EventType.NOVAMARKET_PORTFOLIO_VALUATION_UPDATED: {
            "portfolio_id": "pf_1",
            "total_value": "10000.00",
        },
        EventType.NOVAMARKET_PORTFOLIO_REBALANCE_PROPOSED: {
            "portfolio_id": "pf_1",
            "proposal_id": "preb_1",
            "turnover": "0.2000",
        },
        EventType.NOVAMARKET_PORTFOLIO_REBALANCE_COMPLETED: {
            "portfolio_id": "pf_1",
            "proposal_id": "preb_1",
            "turnover": "0.2000",
        },
        EventType.NOVAMARKET_PORTFOLIO_PERFORMANCE_UPDATED: {
            "portfolio_id": "pf_1",
            "observations": 5,
        },
        EventType.NOVAMARKET_PORTFOLIO_PAUSED: {
            "portfolio_id": "pf_1",
            "reason": "review",
        },
        EventType.NOVAMARKET_PORTFOLIO_SUSPENDED: {
            "portfolio_id": "pf_1",
            "reason": "risk",
        },
        EventType.NOVAMARKET_PORTFOLIO_CLOSED: {
            "portfolio_id": "pf_1",
            "reason": "done",
        },
        # NM-05: discovery and watchlists.
        EventType.NOVAMARKET_ASSET_DISCOVERED: {
            "instrument_id": "XNSE:ALPHA",
            "score": 61.0,
            "reason": "catalogue",
        },
        EventType.NOVAMARKET_ASSET_CANDIDATE_EVALUATED: {
            "instrument_id": "XNSE:ALPHA",
            "score": 61.0,
            "data_quality": "good",
        },
        EventType.NOVAMARKET_ASSET_CANDIDATE_RANKED: {
            "instrument_id": "XNSE:ALPHA",
            "score": 61.0,
            "tier": "review",
            "rank": 1,
        },
        EventType.NOVAMARKET_WATCHLIST_CREATED: {
            "watchlist_id": "wl_1",
            "kind": "custom",
            "name": "My list",
        },
        EventType.NOVAMARKET_WATCHLIST_RENAMED: {
            "watchlist_id": "wl_1",
            "name": "Renamed list",
            "previous_name": "My list",
        },
        EventType.NOVAMARKET_WATCHLIST_INSTRUMENT_ADDED: {
            "watchlist_id": "wl_1",
            "instrument_id": "XNSE:ALPHA",
            "score": 61.0,
            "reason": "added",
        },
        EventType.NOVAMARKET_WATCHLIST_INSTRUMENT_UPDATED: {
            "watchlist_id": "wl_1",
            "instrument_id": "XNSE:ALPHA",
            "status": "active",
        },
        EventType.NOVAMARKET_WATCHLIST_INSTRUMENT_DOWNGRADED: {
            "watchlist_id": "wl_1",
            "instrument_id": "XNSE:ALPHA",
            "from_status": "active",
            "to_status": "downgraded",
        },
        EventType.NOVAMARKET_WATCHLIST_INSTRUMENT_REMOVED: {
            "watchlist_id": "wl_1",
            "instrument_id": "XNSE:ALPHA",
            "reason": "score fell",
        },
        EventType.NOVAMARKET_WATCHLIST_EVALUATION_COMPLETED: {
            "evaluated": 2,
            "upgraded": 1,
            "downgraded": 0,
            "removed": 0,
        },
        EventType.NOVAMARKET_ASSET_DISCOVERY_FAILED: {
            "reason": "market data is not initialised",
            "source_id": "market_data",
        },
        # NM-06: probabilistic forecasting.
        EventType.NOVAMARKET_FORECAST_CREATED: {
            "forecast_id": "fc_1",
            "instrument_id": "XNSE:ALPHA",
            "horizon": "next_session",
            "model_id": "random_walk",
        },
        EventType.NOVAMARKET_FORECAST_COMPLETED: {
            "forecast_id": "fc_1",
            "instrument_id": "XNSE:ALPHA",
            "horizon": "next_session",
            "model_id": "random_walk",
            "predicted_return": 0.001,
            "p_up": 0.4,
        },
        EventType.NOVAMARKET_FORECAST_EVALUATION_PENDING: {
            "forecast_id": "fc_1",
            "instrument_id": "XNSE:ALPHA",
            "reason": "no bar",
        },
        EventType.NOVAMARKET_FORECAST_EVALUATED: {
            "forecast_id": "fc_1",
            "instrument_id": "XNSE:ALPHA",
            "horizon": "next_session",
            "model_id": "random_walk",
            "direction_correct": True,
            "brier": 0.6,
        },
        EventType.NOVAMARKET_FORECAST_MODEL_TRAINED: {
            "model_id": "random_walk",
            "instrument_id": "XNSE:ALPHA",
            "horizon": "next_session",
            "state": "active",
        },
        EventType.NOVAMARKET_FORECAST_MODEL_VALIDATED: {
            "instrument_id": "XNSE:ALPHA",
            "horizon": "next_session",
            "selected_model": "random_walk",
            "baseline_brier": 0.66,
        },
        EventType.NOVAMARKET_FORECAST_MODEL_ACTIVATED: {
            "model_id": "random_walk",
            "instrument_id": "XNSE:ALPHA",
            "horizon": "next_session",
        },
        EventType.NOVAMARKET_FORECAST_MODEL_DEGRADED: {
            "model_id": "random_walk",
            "horizon": "next_session",
            "brier": 0.7,
            "coverage": 0.5,
        },
        EventType.NOVAMARKET_FORECAST_MODEL_SUSPENDED: {
            "model_id": "random_walk",
            "horizon": "next_session",
            "unresolved": 12,
        },
        EventType.NOVAMARKET_FORECAST_CALIBRATION_COMPLETED: {
            "model_id": "random_walk",
            "horizon": "next_session",
            "status": "calibrated",
            "samples": 40,
        },
        EventType.NOVAMARKET_FORECAST_PERFORMANCE_UPDATED: {
            "model_id": "random_walk",
            "horizon": "next_session",
            "samples": 25,
        },
        # NM-10: realistic paper execution.
        EventType.NOVAMARKET_ORDER_CREATED: {
            "order_id": "ord_1", "proposal_id": "prop_1", "portfolio_id": "pf_1",
            "status": "created", "reason": "proposal received",
        },
        EventType.NOVAMARKET_ORDER_VALIDATED: {
            "order_id": "ord_1", "proposal_id": "prop_1", "portfolio_id": "pf_1",
            "status": "accepted", "reason": "validated",
        },
        EventType.NOVAMARKET_ORDER_OPENED: {
            "order_id": "ord_1", "proposal_id": "prop_1", "portfolio_id": "pf_1",
            "status": "open", "reason": "opened",
        },
        EventType.NOVAMARKET_ORDER_PARTIALLY_FILLED: {
            "order_id": "ord_1", "proposal_id": "prop_1", "portfolio_id": "pf_1",
            "status": "partially_filled", "reason": "fill recorded",
        },
        EventType.NOVAMARKET_ORDER_FILLED: {
            "order_id": "ord_1", "proposal_id": "prop_1", "portfolio_id": "pf_1",
            "status": "filled", "reason": "fill recorded",
        },
        EventType.NOVAMARKET_ORDER_CANCEL_REQUESTED: {
            "order_id": "ord_1", "proposal_id": "prop_1", "portfolio_id": "pf_1",
            "status": "cancel_pending", "reason": "cancelled by operator",
        },
        EventType.NOVAMARKET_ORDER_CANCELLED: {
            "order_id": "ord_1", "proposal_id": "prop_1", "portfolio_id": "pf_1",
            "status": "cancelled", "reason": "cancelled by operator",
        },
        EventType.NOVAMARKET_ORDER_REJECTED: {
            "order_id": "ord_1", "proposal_id": "prop_1", "portfolio_id": "pf_1",
            "status": "rejected", "reason": "risk decision expired",
        },
        EventType.NOVAMARKET_ORDER_EXPIRED: {
            "order_id": "ord_1", "proposal_id": "prop_1", "portfolio_id": "pf_1",
            "status": "expired", "reason": "expired",
        },
        EventType.NOVAMARKET_ORDER_FAILED: {
            "order_id": "ord_1", "proposal_id": "prop_1", "portfolio_id": "pf_1",
            "status": "failed", "reason": "ledger write failed",
        },
        EventType.NOVAMARKET_EXECUTION_FILL_RECORDED: {
            "fill_id": "fill_1", "order_id": "ord_1", "instrument_id": "XNSE:ALPHA",
            "quantity": "10.0000", "price": "100.00", "slippage_pct": "0.0100",
            "total_fees": "3.00", "ledger_transaction_ids": ("tx_1",),
            "execution_config_version": "nm10-exec-v1",
        },
        EventType.NOVAMARKET_EXECUTION_RECONCILED: {
            "reconciliation_id": "recon_1", "order_id": "ord_1", "ok": True, "issues": 0,
        },
        EventType.NOVAMARKET_EXECUTION_RECONCILIATION_FAILED: {
            "reconciliation_id": "recon_2", "order_id": "ord_1", "ok": False, "issues": 1,
        },
    }
)


class PermissionTests(unittest.TestCase):
    """Low-level reads are granted; execution is not, and live never is."""

    def test_every_operation_has_exactly_one_policy(self) -> None:
        model = MarketPermissionModel()
        self.assertEqual(
            [row["operation"] for row in model.matrix()],
            [operation.value for operation in MarketOperation],
        )

    def test_only_reads_configuration_and_simulated_money_are_granted(self) -> None:
        """NM-03 moves simulated NC; nothing trades, and nothing is invisible."""
        model = MarketPermissionModel()
        self.assertEqual(
            set(model.granted_operations()),
            {
                MarketOperation.READ_STATUS,
                MarketOperation.READ_MARKET,
                MarketOperation.MANAGE_CONFIG,
                MarketOperation.READ_WALLET,
                MarketOperation.MOVE_SIMULATED_FUNDS,
                MarketOperation.CORRECT_LEDGER,
                MarketOperation.READ_EVENTS,
                MarketOperation.REFRESH_EVENTS,
                MarketOperation.DISCOVER_ASSETS,
                MarketOperation.READ_WATCHLISTS,
                MarketOperation.MANAGE_WATCHLISTS,
                MarketOperation.READ_FORECASTS,
                MarketOperation.RUN_FORECASTS,
                MarketOperation.TRAIN_FORECAST_MODELS,
                MarketOperation.READ_PORTFOLIOS,
                MarketOperation.MANAGE_SIMULATED_PORTFOLIO,
                MarketOperation.READ_RISK,
                MarketOperation.ASSESS_RISK,
                MarketOperation.MANAGE_RISK_POLICY,
                MarketOperation.OVERRIDE_RISK,
                MarketOperation.ACKNOWLEDGE_RISK_ALERTS,
                MarketOperation.READ_STRATEGY,
                MarketOperation.MANAGE_STRATEGY,
                MarketOperation.RUN_STRATEGIES,
                MarketOperation.EXECUTE_PAPER_TRADE,
                MarketOperation.READ_EXECUTION,
                MarketOperation.RECONCILE_EXECUTION,
                MarketOperation.READ_BACKTESTS,
                MarketOperation.RUN_BACKTESTS,
                MarketOperation.COMPARE_BACKTESTS,
                MarketOperation.READ_SHADOW,
                MarketOperation.MANAGE_SHADOW,
                MarketOperation.RUN_SHADOW,
                MarketOperation.COMPARE_SHADOW,
                MarketOperation.READ_LEARNING,
                MarketOperation.RUN_LEARNING,
                MarketOperation.MANAGE_LEARNING,
                MarketOperation.APPROVE_LEARNING,
                MarketOperation.READ_PREDICTIONS,
                MarketOperation.RUN_PREDICTIONS,
                MarketOperation.MANAGE_PREDICTION_MODELS,
                MarketOperation.READ_DECISION_EVIDENCE,
                MarketOperation.READ_DATASETS,
                MarketOperation.BUILD_DATASETS,
                MarketOperation.MANAGE_DATASETS,
                MarketOperation.READ_FEATURES,
                MarketOperation.GENERATE_FEATURES,
                MarketOperation.MANAGE_FEATURES,
            },
        )
        for operation in (
            MarketOperation.READ_SENSITIVE_CONFIG,
            MarketOperation.EXECUTE_LIVE_TRADE,
        ):
            with self.subTest(operation=operation):
                decision = model.authorize(operation)
                self.assertFalse(decision.allowed)
                self.assertTrue(decision.reason)

    def test_live_trading_is_critical_destructive_and_irreversible(self) -> None:
        policy = MarketPermissionModel().policy(MarketOperation.EXECUTE_LIVE_TRADE)
        self.assertEqual(policy.declaration.risk_level.value, "critical")
        self.assertTrue(policy.declaration.destructive)
        self.assertFalse(policy.declaration.reversible)
        self.assertTrue(policy.declaration.external_side_effect)

    def test_declarations_go_into_and_come_out_of_the_shared_layer(self) -> None:
        manager = PermissionManager()
        model = MarketPermissionModel()
        declared = model.declare(manager)
        self.assertEqual(set(declared), {operation.value for operation in model.granted_operations()})
        self.assertIsNotNone(manager.declared_for("market.config.manage"))
        self.assertIsNone(manager.declared_for("market.trade.live"))
        removed = model.undeclare(manager, declared)
        self.assertEqual(set(removed), set(declared))
        self.assertIsNone(manager.declared_for("market.status.read"))

    def test_an_unknown_operation_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            MarketPermissionModel().authorize("market.trade.leveraged")

    def test_status_and_contracts_derive_the_live_trading_flag(self) -> None:
        """The flag is read from the model, not asserted as a constant."""
        from dataclasses import replace

        class _GrantingModel(MarketPermissionModel):
            def authorize(self, operation):
                decision = super().authorize(operation)
                if decision.operation is MarketOperation.EXECUTE_LIVE_TRADE:
                    return replace(decision, allowed=True)
                return decision

        service = NovaMarketService()
        service._permission_model = _GrantingModel()  # type: ignore[assignment]
        self.assertTrue(service.status()["permissions"]["real_money_trading"])
        self.assertTrue(service.contracts()["permissions"]["real_money_trading"])


_IMPLEMENTED_PAYLOADS.update(
    {
        # NM-11: reproducible historical backtesting.
        EventType.NOVAMARKET_BACKTEST_CREATED: {
            "backtest_id": "bt_1",
            "strategy_id": "strategy_1",
            "dataset_id": "nm02-point-in-time",
        },
        EventType.NOVAMARKET_BACKTEST_STARTED: {
            "backtest_id": "bt_1",
            "strategy_id": "strategy_1",
            "start": "2025-01-01T00:00:00+00:00",
            "end": "2025-12-31T00:00:00+00:00",
        },
        EventType.NOVAMARKET_BACKTEST_PROGRESS_UPDATED: {
            "backtest_id": "bt_1",
            "phase": "replay",
            "bars_loaded": 240,
        },
        EventType.NOVAMARKET_BACKTEST_COMPLETED: {
            "backtest_id": "bt_1",
            "status": "completed",
            "trades": 12,
            "repro_hash": "abc123",
        },
        EventType.NOVAMARKET_BACKTEST_FAILED: {
            "backtest_id": "bt_1",
            "error": "insufficient historical data",
        },
        EventType.NOVAMARKET_BACKTEST_CANCELLED: {"backtest_id": "bt_1"},
        EventType.NOVAMARKET_BACKTEST_CHECKPOINT_CREATED: {
            "backtest_id": "bt_1",
            "at": "2025-12-31T00:00:00+00:00",
            "observations": 240,
        },
        EventType.NOVAMARKET_BACKTEST_PERFORMANCE_CALCULATED: {
            "backtest_id": "bt_1",
            "observations": 240,
            "total_return": "0.0421",
        },
        EventType.NOVAMARKET_BACKTEST_COMPARISON_COMPLETED: {
            "base_backtest_id": "bt_1",
            "variants": 3,
        },
        EventType.NOVAMARKET_BACKTEST_DATA_QUALITY_WARNING: {
            "backtest_id": "bt_1",
            "warnings": 2,
        },
        EventType.NOVAMARKET_BACKTEST_REPRODUCIBILITY_VERIFIED: {
            "backtest_id": "bt_1",
            "ok": True,
        },
        # NM-12: non-executing Shadow Mode.
        EventType.NOVAMARKET_SHADOW_SESSION_CREATED: {
            "session_id": "shs_1",
            "instruments": 2,
            "strategies": 1,
        },
        EventType.NOVAMARKET_SHADOW_SESSION_STARTED: {
            "session_id": "shs_1",
            "baseline_snapshot_id": "psnap_1",
            "portfolio_id": "portfolio_1",
        },
        EventType.NOVAMARKET_SHADOW_SESSION_PAUSED: {"session_id": "shs_1"},
        EventType.NOVAMARKET_SHADOW_SESSION_RESUMED: {"session_id": "shs_1"},
        EventType.NOVAMARKET_SHADOW_SESSION_STOPPED: {"session_id": "shs_1"},
        EventType.NOVAMARKET_SHADOW_SESSION_COMPLETED: {
            "session_id": "shs_1",
            "decisions": 4,
        },
        EventType.NOVAMARKET_SHADOW_SESSION_CANCELLED: {"session_id": "shs_1"},
        EventType.NOVAMARKET_SHADOW_SESSION_FAILED: {
            "session_id": "shs_1",
            "error": "baseline capture failed",
        },
        EventType.NOVAMARKET_SHADOW_DECISION_RECORDED: {
            "session_id": "shs_1",
            "decision_id": "shd_1",
            "strategy_id": "strategy_1",
            "instrument_id": "XNSE:ALPHA",
            "signal_type": "buy",
            "status": "projected",
        },
        EventType.NOVAMARKET_SHADOW_RISK_EVALUATED: {
            "session_id": "shs_1",
            "decision_id": "shd_1",
            "outcome": "approved",
            "approved": True,
        },
        EventType.NOVAMARKET_SHADOW_PROJECTION_CREATED: {
            "session_id": "shs_1",
            "decision_id": "shd_1",
            "projection_id": "shp_1",
            "instrument_id": "XNSE:ALPHA",
            "side": "buy",
            "simulated": True,
        },
        EventType.NOVAMARKET_SHADOW_OUTCOME_UPDATED: {
            "session_id": "shs_1",
            "decision_id": "shd_1",
            "interval": 5,
            "status": "resolved",
            "hypothetical": True,
        },
        EventType.NOVAMARKET_SHADOW_CYCLE_COMPLETED: {
            "session_id": "shs_1",
            "decisions": 1,
            "duplicates": 0,
            "data_failures": 0,
            "latency_ms": 1.2,
        },
        EventType.NOVAMARKET_SHADOW_DATA_QUALITY_FAILED: {
            "session_id": "shs_1",
            "kind": "data_stale",
            "instrument_id": "XNSE:ALPHA",
        },
        EventType.NOVAMARKET_SHADOW_RECOVERED: {"sessions": 1, "paused": 1},
        EventType.NOVAMARKET_SHADOW_COMPARISON_COMPLETED: {
            "session_id": "shs_1",
            "comparison_id": "shc_1",
            "compatible": True,
            "categories": ["timing_difference"],
        },
        EventType.NOVAMARKET_SHADOW_ANALYTICS_UPDATED: {
            "session_id": "shs_1",
            "decisions": 1,
            "cycles": 1,
        },
        EventType.NOVAMARKET_SHADOW_DIVERGENCE_DETECTED: {
            "session_id": "shs_1",
            "comparison_id": "shc_1",
            "category": "outcome_divergence",
        },
        # NM-13: controlled adaptive learning.
        EventType.NOVAMARKET_LEARNING_EVIDENCE_RECORDED: {
            "evidence_id": "lev_1",
            "kind": "backtest",
            "strategy_id": "strat_1",
            "confidence": "0.90",
        },
        EventType.NOVAMARKET_LEARNING_EVIDENCE_REJECTED: {
            "evidence_id": "lev_2",
            "kind": "shadow",
            "strategy_id": "strat_1",
            "reasons": ["sample too small"],
        },
        EventType.NOVAMARKET_LEARNING_MONITOR_COMPLETED: {
            "strategy_id": "strat_1",
            "snapshots": 3,
            "alerts": 1,
        },
        EventType.NOVAMARKET_LEARNING_ALERT_RAISED: {
            "alert_id": "lea_1",
            "strategy_id": "strat_1",
            "kind": "performance_deterioration",
            "severity": "warning",
        },
        EventType.NOVAMARKET_LEARNING_ALERT_ACKNOWLEDGED: {
            "alert_id": "lea_1",
            "strategy_id": "strat_1",
        },
        EventType.NOVAMARKET_LEARNING_EXPERIMENT_CREATED: {
            "experiment_id": "lex_1",
            "strategy_id": "strat_1",
            "candidates": 3,
            "method": "grid",
        },
        EventType.NOVAMARKET_LEARNING_EXPERIMENT_STARTED: {
            "experiment_id": "lex_1",
            "strategy_id": "strat_1",
        },
        EventType.NOVAMARKET_LEARNING_EXPERIMENT_COMPLETED: {
            "experiment_id": "lex_1",
            "evaluated": 3,
            "eligible": 1,
            "rejected": 2,
        },
        EventType.NOVAMARKET_LEARNING_EXPERIMENT_FAILED: {
            "experiment_id": "lex_1",
            "error": "insufficient historical data",
        },
        EventType.NOVAMARKET_LEARNING_EXPERIMENT_CANCELLED: {
            "experiment_id": "lex_1",
        },
        EventType.NOVAMARKET_LEARNING_CANDIDATE_EVALUATED: {
            "candidate_id": "lcn_1",
            "experiment_id": "lex_1",
            "status": "pending_approval",
            "eligible": True,
        },
        EventType.NOVAMARKET_LEARNING_CANDIDATE_REJECTED: {
            "candidate_id": "lcn_2",
            "experiment_id": "lex_1",
            "reasons": ["does not improve on baseline"],
        },
        EventType.NOVAMARKET_LEARNING_CANDIDATE_APPROVED: {
            "candidate_id": "lcn_1",
            "experiment_id": "lex_1",
            "automatic": False,
        },
        EventType.NOVAMARKET_LEARNING_CANDIDATE_SHADOW_VALIDATED: {
            "candidate_id": "lcn_1",
            "status": "passed",
            "decisions": 4,
        },
        EventType.NOVAMARKET_LEARNING_CANDIDATE_PROMOTED: {
            "candidate_id": "lcn_1",
            "strategy_id": "strat_1",
            "version_id": "lrv_1",
            "previous_version": "nm09-trend_sma-1.0",
        },
        EventType.NOVAMARKET_LEARNING_STRATEGY_ROLLED_BACK: {
            "strategy_id": "strat_1",
            "from_version": "lrv_1",
            "to_version": "nm09-trend_sma-1.0",
            "reason": "performance deteriorated",
        },
        EventType.NOVAMARKET_PREDICTION_LIFECYCLE_CREATED: {
            "lifecycle_id": "plc_1",
            "request_id": "req_1",
            "instrument_id": "XNSE:RELIANCE",
            "horizon": "20m",
            "target_at": "2026-11-13T10:30:00+00:00",
        },
        EventType.NOVAMARKET_PREDICTION_MODEL_REGISTERED: {
            "model_id": "baseline",
            "version": "1.0.0",
            "state": "eligible",
            "calibration": "uncalibrated",
            "horizons": "20m",
        },
        EventType.NOVAMARKET_PREDICTION_ABSTAINED: {
            "lifecycle_id": "plc_1",
            "stage": "t_minus_5",
            "reasons": "no eligible model",
        },
        EventType.NOVAMARKET_PREDICTION_FORECAST_GENERATED: {
            "forecast_id": "pfc_1",
            "lifecycle_id": "plc_1",
            "instrument_id": "XNSE:RELIANCE",
            "horizon": "20m",
            "direction": "up",
            "stage": "t_minus_20",
            "target_at": "2026-11-13T10:30:00+00:00",
        },
        EventType.NOVAMARKET_PREDICTION_FORECAST_CONFIRMED: {
            "lifecycle_id": "plc_1",
            "stage": "t_minus_5",
            "forecast_id": "pfc_2",
            "direction": "up",
            "changed_fields": "expected_return",
        },
        EventType.NOVAMARKET_PREDICTION_CONFIRMATION_SKIPPED: {
            "lifecycle_id": "plc_1",
            "stage": "t_minus_5",
            "status": "skipped",
            "reason": "market data stale",
        },
        EventType.NOVAMARKET_PREDICTION_FORECAST_EVALUATED: {
            "lifecycle_id": "plc_1",
            "forecast_id": "pfc_1",
            "stage": "t_minus_20",
            "direction_correct": True,
            "status": "evaluated",
        },
        EventType.NOVAMARKET_PREDICTION_DECISION_EVIDENCE_READY: {
            "evidence_id": "pev_1",
            "lifecycle_id": "plc_1",
            "instrument_id": "XNSE:RELIANCE",
            "eligible": True,
        },
        # PI-02: historical dataset builds and immutable versions.
        EventType.NOVAMARKET_DATASET_BUILD_STARTED: {
            "build_id": "build_1",
            "dataset_id": "nse-daily-core",
            "instruments": ["XNSE:RELIANCE"],
            "resolution": "1d",
            "start_at": "2026-01-05T00:00:00+00:00",
            "end_at": "2026-01-09T23:59:00+00:00",
        },
        EventType.NOVAMARKET_DATASET_BUILD_COMPLETED: {
            "build_id": "build_1",
            "dataset_id": "nse-daily-core",
            "version": "v1",
            "duplicate": False,
        },
        EventType.NOVAMARKET_DATASET_BUILD_FAILED: {
            "build_id": "build_1",
            "dataset_id": "nse-daily-core",
            "error": "provider outage",
        },
        EventType.NOVAMARKET_DATASET_VERSION_CREATED: {
            "build_id": "build_1",
            "dataset_id": "nse-daily-core",
            "version": "v1",
            "quality": "valid",
            "bar_count": 5,
            "content_digest": "digest",
        },
        EventType.NOVAMARKET_DATASET_VALIDATION_INCOMPLETE: {
            "build_id": "build_1",
            "dataset_id": "nse-daily-core",
            "version": "v1",
            "quality": "incomplete",
            "missing": 2,
        },
        EventType.NOVAMARKET_FEATURES_GENERATED: {
            "feature_set_id": "fs_1",
            "instrument_id": "XNSE:RELIANCE",
            "as_of": "2026-01-09T10:00:00+00:00",
            "resolution": "1d",
            "profile": "swing",
            "quality": "valid",
            "completeness": 1.0,
            "persisted": True,
            "duplicate": False,
        },
        EventType.NOVAMARKET_FEATURES_FAILED: {
            "instrument_id": "XNSE:RELIANCE",
            "resolution": "1d",
            "profile": "swing",
            "error": "insufficient history",
        },
        EventType.NOVAMARKET_REGIME_DETECTED: {
            "regime_id": "rg_1",
            "feature_set_id": "fs_1",
            "instrument_id": "XNSE:RELIANCE",
            "as_of": "2026-01-09T10:00:00+00:00",
            "primary_regime": "sideways",
            "secondary": ["low_volatility"],
            "method_version": "pi04-regime-1.0",
        },
        EventType.NOVAMARKET_REGIME_TRANSITIONED: {
            "regime_id": "rg_1",
            "instrument_id": "XNSE:RELIANCE",
            "previous": "sideways",
            "current": "weak_up",
            "as_of": "2026-01-09T10:00:00+00:00",
        },
        EventType.NOVAMARKET_PREPROCESSING_FITTED: {
            "preprocessing_id": "pp_1",
            "method": "standard",
            "version": "pi04-preprocess-1.0",
            "training_digest": "digest",
        },
        EventType.NOVAMARKET_FEATURE_SELECTION_COMPLETED: {
            "report_id": "sel_1",
            "version": "pi04-selection-1.0",
            "observations": 20,
            "decision_count": 1,
        },
        EventType.NOVAMARKET_AGENT_CYCLE_COMPLETED: {
            "cycle_id": "mac_1",
            "instrument_id": "XNSE:RELIANCE",
            "horizon": "1d",
            "status": "completed",
            "action": "buy",
        },
        EventType.NOVAMARKET_OPTIMIZATION_RECOMMENDATION_CREATED: {
            "run_id": "opt_1",
            "portfolio_id": "pf_1",
            "status": "completed",
            "rebalance_action": "rebalance",
            "recommendation_count": 3,
        },
        EventType.NOVAMARKET_RESEARCH_RUN_COMPLETED: {
            "run_id": "prr_1",
            "instrument_id": "XNSE:RELIANCE",
            "status": "completed",
            "stance": "neutral",
        },
        EventType.NOVAMARKET_POLICY_DEGRADED: {
            "policy_id": "pol_1",
            "status": "persistent_degradation",
            "trigger_count": 2,
            "retraining_recommended": True,
        },
    }
)


class EventContractTests(unittest.IsolatedAsyncioTestCase):
    """Typed lifecycle events and the future-event extension path."""

    def test_every_implemented_event_has_a_payload_contract(self) -> None:
        self.assertEqual(set(IMPLEMENTED_EVENTS), set(_IMPLEMENTED_PAYLOADS))
        for event_type, payload in _IMPLEMENTED_PAYLOADS.items():
            self.assertEqual(
                set(EVENT_PAYLOAD_FIELDS[event_type]), set(payload), event_type
            )
            self.assertEqual(
                Event.of(event_type, **payload).type, str(event_type)
            )

    def test_future_events_are_namespaced_and_owned_by_a_later_phase(self) -> None:
        names = set()
        for spec in FUTURE_EVENT_SPECS:
            self.assertTrue(spec.name.startswith("novamarket."))
            self.assertIn(spec.phase, ROADMAP_PHASES)
            self.assertNotEqual(spec.phase, "NM-01")
            self.assertTrue(spec.payload_fields)
            self.assertNotIn(spec.name, names)
            names.add(spec.name)
        # NM-02's market-data events are IMPLEMENTED, so they must not remain
        # in the future extension table.
        self.assertNotIn("novamarket.data.available", names)
        self.assertNotIn("novamarket.data.freshness_changed", names)
        self.assertNotIn("novamarket.event.detected", names)

    def test_a_future_payload_contract_is_validated(self) -> None:
        validate_market_event(
            "novamarket.portfolio.updated",
            {"portfolio_id": "pf_1", "horizon": "medium"},
        )
        with self.assertRaises(ValueError) as caught:
            validate_market_event(
                "novamarket.portfolio.updated", {"portfolio_id": "pf_1"}
            )
        self.assertIn("horizon", str(caught.exception))

    def test_an_unregistered_event_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            validate_market_event("novamarket.something.new", {})

    async def test_a_payload_may_not_shadow_the_envelope_source(self) -> None:
        publisher = MarketEventPublisher(EventBus(continue_on_error=True))
        with self.assertRaises(ValueError) as caught:
            await publisher.publish(
                EventType.NOVAMARKET_DISABLED, reason="test", source="elsewhere"
            )
        self.assertIn("envelope", str(caught.exception))


class _WriteRefusingStore:
    """A store whose writes fail, to prove persistence cannot fail the work."""

    def read(self, name: str) -> dict[str, object]:
        return {}

    def write(self, name: str, payload: dict[str, object]) -> None:
        raise OSError("disk full")


class _ReadRefusingStore:
    """A store that cannot be read, to prove construction is not a failure door."""

    def read(self, name: str) -> dict[str, object]:
        raise OSError("state volume unavailable")

    def write(self, name: str, payload: dict[str, object]) -> None:
        raise OSError("state volume unavailable")


class _SubsystemCase(unittest.IsolatedAsyncioTestCase):
    """Real bus, real registries, real store, one service per test."""

    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.bus = EventBus(continue_on_error=True, history=400)
        self.registry = CapabilityRegistry()
        self.permissions = PermissionManager()
        self.store = JsonStateStore(Path(self._tmp.name))
        self.service = NovaMarketService(
            store=self.store,
            event_bus=self.bus,
            permissions=self.permissions,
            capabilities=self.registry,
        )

    async def asyncTearDown(self) -> None:
        await self.service.shutdown(reason="test teardown")
        self._tmp.cleanup()


class LifecycleTests(_SubsystemCase):
    """Initialization, readiness, enablement, reinit and full withdrawal."""

    async def test_initial_state_is_disabled_and_not_operational(self) -> None:
        self.assertEqual(self.service.state, LifecycleState.DISABLED)
        self.assertEqual(self.service.health().state, HealthState.SKIPPED)
        self.assertFalse(self.service.status()["operational"])

    async def test_enable_reaches_running_and_registers_its_contributions(self) -> None:
        status = await self.service.enable(reason="test")

        self.assertEqual(status["lifecycle"], "running")
        self.assertEqual(self.service.health().state, HealthState.OK)
        self.assertEqual(
            self.bus.subscriber_count("novamarket.configuration_requested"), 1
        )
        self.assertEqual(
            {record["capability_id"] for record in self.service.capability_records()},
            {item.capability_id for item in CAPABILITIES},
        )
        for capability_id in ("market.status", "market.configure", "market.contracts"):
            self.assertIsNotNone(self.registry.get(capability_id), capability_id)
        self.assertIsNotNone(self.permissions.declared_for("market.config.manage"))
        self.assertEqual(status["permissions"]["real_money_trading"], False)

    async def test_enable_is_idempotent(self) -> None:
        await self.service.enable()
        history_length = len(self.service.status()["transitions"])
        await self.service.enable()
        self.assertEqual(self.service.state, LifecycleState.RUNNING)
        self.assertEqual(
            self.bus.subscriber_count("novamarket.configuration_requested"), 1
        )
        self.assertEqual(len(self.service.status()["transitions"]), history_length)

    async def test_initialize_from_running_is_a_no_op(self) -> None:
        await self.service.enable()
        before = len(self.service.status()["transitions"])
        await self.service.initialize()
        self.assertEqual(len(self.service.status()["transitions"]), before)

    async def test_reinitialization_creates_no_duplicate_subscription_or_capability(self) -> None:
        await self.service.enable()
        await self.service.reinitialize(reason="test")

        self.assertEqual(self.service.state, LifecycleState.RUNNING)
        self.assertEqual(
            self.bus.subscriber_count("novamarket.configuration_requested"), 1
        )
        self.assertIsNotNone(self.registry.get("market.status"))
        self.assertEqual(self.service.status()["statistics"]["reinitializations"], 1)

    async def test_a_failed_reinitialization_keeps_the_enabled_choice(self) -> None:
        failure = {"on": False}

        def validator() -> None:
            if failure["on"]:
                raise RuntimeError("revalidation refused")

        service = NovaMarketService(
            store=self.store,
            event_bus=self.bus,
            permissions=self.permissions,
            capabilities=self.registry,
            validators=(validator,),
        )
        await service.enable()
        failure["on"] = True

        with self.assertRaises(NovaMarketInitializationError):
            await service.reinitialize(reason="test")

        self.assertEqual(service.state, LifecycleState.ERROR)
        self.assertTrue(service.config.enabled, "the operator's choice was enabled")
        self.assertTrue(service.status()["enabled"])
        stored = self.store.read("novamarket")
        self.assertTrue(stored["config"]["enabled"])
        await service.shutdown(reason="test teardown")

    async def test_disable_withdraws_capabilities_permissions_and_subscription(self) -> None:
        await self.service.enable()
        status = await self.service.disable(reason="operator")

        self.assertEqual(status["lifecycle"], "disabled")
        self.assertEqual(
            self.bus.subscriber_count("novamarket.configuration_requested"), 0
        )
        self.assertIsNone(self.registry.get("market.status"))
        self.assertIsNone(self.permissions.declared_for("market.config.manage"))
        self.assertFalse(status["enabled"])
        stored = self.store.read("novamarket")
        self.assertFalse(stored["config"]["enabled"])

    async def test_enable_after_disable_restores_the_subsystem(self) -> None:
        await self.service.enable()
        await self.service.disable()
        status = await self.service.enable()
        self.assertEqual(status["lifecycle"], "running")
        self.assertIsNotNone(self.registry.get("market.status"))

    async def test_shutdown_is_orderly_and_keeps_the_operator_choice(self) -> None:
        await self.service.enable()
        status = await self.service.shutdown(reason="application shutdown")

        self.assertEqual(status["lifecycle"], "disabled")
        self.assertTrue(status["enabled"], "shutdown must not flip the enabled setting")
        self.assertEqual(
            self.bus.subscriber_count("novamarket.configuration_requested"), 0
        )
        self.assertIsNone(self.registry.get("market.status"))

    async def test_failed_initialization_is_error_and_then_recoverable(self) -> None:
        failure = {"on": True}

        def validator() -> None:
            if failure["on"]:
                raise RuntimeError("market data provider refused")

        service = NovaMarketService(
            store=self.store,
            event_bus=self.bus,
            permissions=self.permissions,
            capabilities=self.registry,
            validators=(validator,),
        )
        with self.assertRaises(NovaMarketInitializationError):
            await service.enable()

        status = service.status()
        self.assertEqual(status["lifecycle"], "error")
        self.assertEqual(status["health"]["state"], "failing")
        self.assertIn("market data provider refused", status["last_error"])
        self.assertIsNone(self.registry.get("market.status"))
        self.assertIsNone(self.permissions.declared_for("market.config.manage"))
        self.assertIn(
            "novamarket.error", [event.type for event in self.bus.recent(limit=50)]
        )

        failure["on"] = False
        recovered = await service.enable(reason="recovered")
        self.assertEqual(recovered["lifecycle"], "running")
        self.assertIsNotNone(self.registry.get("market.status"))
        self.assertEqual(service.status()["last_error"], "")
        await service.shutdown(reason="test teardown")

    async def test_auto_start_never_raises_when_initialization_fails(self) -> None:
        def validator() -> None:
            raise RuntimeError("provider missing")

        service = NovaMarketService(
            store=self.store,
            event_bus=self.bus,
            permissions=self.permissions,
            capabilities=self.registry,
            validators=(validator,),
        )
        await service.auto_start()
        self.assertEqual(service.state, LifecycleState.ERROR)
        await service.shutdown(reason="test teardown")

    async def test_a_corrupt_stored_configuration_is_error_not_a_silent_reset(self) -> None:
        self.store.write("novamarket", {"config": {"mode": "paper"}})
        service = NovaMarketService(store=self.store, event_bus=self.bus)
        self.assertTrue(service.config_error)

        with self.assertRaises(NovaMarketInitializationError):
            await service.enable()
        self.assertEqual(service.state, LifecycleState.ERROR)

        result = await service.update_config({"mode": "observe"})
        self.assertEqual(result["status"]["lifecycle"], "running")
        self.assertEqual(service.config_error, "")
        await service.shutdown(reason="test teardown")

    async def test_no_bus_means_degraded_not_a_false_running(self) -> None:
        service = NovaMarketService(
            store=self.store,
            permissions=self.permissions,
            capabilities=self.registry,
        )
        status = await service.enable()
        self.assertEqual(status["lifecycle"], "degraded")
        self.assertEqual(service.health().state, HealthState.DEGRADED)
        with self.assertRaises(NovaMarketError):
            await service.recover()
        self.assertEqual(service.state, LifecycleState.DEGRADED)

        other_bus = EventBus(continue_on_error=True, history=20)
        service.attach_event_bus(other_bus)
        recovered = await service.recover()
        self.assertEqual(recovered["lifecycle"], "running")
        self.assertEqual(
            other_bus.subscriber_count("novamarket.configuration_requested"), 1
        )
        await service.shutdown(reason="test teardown")

    async def test_reinitializing_a_disabled_subsystem_leaves_it_disabled(self) -> None:
        status = await self.service.reinitialize(reason="test")
        self.assertEqual(status["lifecycle"], "disabled")
        self.assertFalse(status["enabled"])
        self.assertEqual(
            self.bus.subscriber_count("novamarket.configuration_requested"), 0
        )

    async def test_every_declared_transition_is_enforced(self) -> None:
        for state, targets in ALLOWED_TRANSITIONS.items():
            for target in targets:
                with self.subTest(state=state.value, target=target.value):
                    machine = LifecycleMachine(initial=state)
                    machine.transition(target)
                    self.assertEqual(machine.state, target)
            for target in set(LifecycleState) - targets - {state}:
                with self.subTest(
                    state=state.value, refused=target.value
                ):
                    machine = LifecycleMachine(initial=state)
                    with self.assertRaises(LifecycleError):
                        machine.transition(target)

    async def test_invalid_transitions_are_refused_by_the_machine(self) -> None:
        machine = LifecycleMachine()
        with self.assertRaises(LifecycleError):
            machine.transition(LifecycleState.READY)
        machine.transition(LifecycleState.INITIALIZING)
        machine.transition(LifecycleState.READY)
        with self.assertRaises(LifecycleError):
            machine.transition(LifecycleState.INITIALIZING)
        self.assertEqual(
            machine.transition(LifecycleState.READY).changed,
            False,
            "asking for the current state is an unchanged no-op",
        )
        self.assertIn(LifecycleState.RUNNING, ALLOWED_TRANSITIONS[LifecycleState.READY])

    async def test_events_use_the_real_bus_with_the_novamarket_source(self) -> None:
        seen: list[Event] = []

        async def capture(event: Event) -> None:
            seen.append(event)

        await self.bus.subscribe("novamarket.enabled", capture)
        await self.service.enable()
        self.assertTrue(seen)
        self.assertEqual(seen[0].source, "novamarket")
        self.assertEqual(seen[0].payload["lifecycle"], "running")

    async def test_the_bus_control_channel_applies_a_validated_change(self) -> None:
        await self.service.enable()
        await self.bus.publish(
            Event.of(
                EventType.NOVAMARKET_CONFIGURATION_REQUESTED,
                configuration={"stale_after_seconds": 1234},
            )
        )
        self.assertEqual(self.service.config.stale_after_seconds, 1234)
        self.assertIn(
            "novamarket.configuration_changed",
            [event.type for event in self.bus.recent(limit=50)],
        )

    async def test_an_invalid_bus_request_is_a_warning_not_a_crash(self) -> None:
        await self.service.enable()
        await self.bus.publish(
            Event.of(
                EventType.NOVAMARKET_CONFIGURATION_REQUESTED,
                configuration={"mode": "live"},
            )
        )
        self.assertEqual(self.service.state, LifecycleState.RUNNING)
        warnings = [
            event
            for event in self.bus.recent(limit=50)
            if event.type == "novamarket.warning"
        ]
        self.assertTrue(warnings)
        self.assertEqual(warnings[-1].payload["code"], "configuration_rejected")

    async def test_a_partial_configuration_update_preserves_other_fields(self) -> None:
        await self.service.update_config(
            {"data_providers": ["nse-bhavcopy"], "stale_after_seconds": 1200}
        )
        result = await self.service.update_config({"resources": {"max_assets": 10}})
        self.assertEqual(result["config"]["data_providers"], ["nse-bhavcopy"])
        self.assertEqual(result["config"]["stale_after_seconds"], 1200)
        self.assertEqual(result["config"]["resources"]["max_assets"], 10)
        self.assertEqual(result["changed"], ["resources"])

    async def test_naming_a_provider_does_not_advertise_unverified_market_read(self) -> None:
        await self.service.enable()
        await self.service.update_config({"data_providers": ["nse-bhavcopy"]})

        record = next(
            item
            for item in self.service.status()["capabilities"]
            if item["capability_id"] == "market.read"
        )
        # An uninstalled provider name is reported, not silently enabled, and a
        # named provider is never the same thing as a verified retrieval.
        self.assertFalse(record["available"])
        self.assertIn("nse-bhavcopy", record["reason"])
        self.assertEqual(
            self.service.market_data_status()["unknown_providers"],
            ["nse-bhavcopy"],
        )
        registered = self.registry.get("market.read")
        self.assertIsNotNone(registered)
        self.assertEqual(registered.availability.value, "unavailable")

    async def test_an_installed_provider_is_enabled_but_not_yet_verified(self) -> None:
        await self.service.enable()
        await self.service.update_config({"data_providers": ["yahoo", "coingecko"]})
        status = self.service.market_data_status()
        self.assertEqual(status["unknown_providers"], [])
        self.assertEqual(
            status["enabled_providers"], ["yahoo", "coingecko"]
        )
        self.assertEqual(status["verified_operations"], [])
        record = next(
            item
            for item in self.service.status()["capabilities"]
            if item["capability_id"] == "market.read"
        )
        self.assertFalse(record["available"])
        self.assertIn("verified", record["reason"])

    async def test_a_store_that_refuses_persistence_does_not_fail_the_work(self) -> None:
        service = NovaMarketService(
            store=_WriteRefusingStore(),
            event_bus=self.bus,
            permissions=self.permissions,
            capabilities=self.registry,
        )
        await service.enable()
        result = await service.update_config({"stale_after_seconds": 1234})

        self.assertEqual(result["config"]["stale_after_seconds"], 1234)
        self.assertEqual(service.state, LifecycleState.RUNNING)
        status = service.status()
        self.assertIn("disk full", status["last_persist_error"])
        self.assertEqual(status["statistics"]["persist_failures"], 1)
        self.assertEqual(status["health"]["state"], "degraded")
        await service.shutdown(reason="test teardown")

    async def test_an_unreadable_store_is_error_not_a_construction_crash(self) -> None:
        service = NovaMarketService(
            store=_ReadRefusingStore(),
            event_bus=self.bus,
            permissions=self.permissions,
            capabilities=self.registry,
        )
        self.assertIn("could not be read", service.config_error)

        with self.assertRaises(NovaMarketInitializationError):
            await service.enable()
        self.assertEqual(service.state, LifecycleState.ERROR)
        self.assertEqual(service.health().state, HealthState.FAILING)
        await service.shutdown(reason="test teardown")

    async def test_publication_can_be_switched_off_without_losing_the_contract(self) -> None:
        service = NovaMarketService(
            store=self.store,
            event_bus=self.bus,
            config=NovaMarketConfig(emit_events=False),
        )
        await service.enable()
        self.assertFalse(
            [event for event in self.bus.recent(limit=50) if event.type.startswith("novamarket.")]
        )
        self.assertEqual(service.state, LifecycleState.RUNNING)
        await service.shutdown(reason="test teardown")

    async def test_the_event_publisher_refuses_future_and_unknown_events(self) -> None:
        publisher = MarketEventPublisher(self.bus)
        with self.assertRaises(ValueError) as future:
            await publisher.publish(
                "novamarket.portfolio.updated", portfolio_id="pf_1"
            )
        self.assertIn("NM-07", str(future.exception))
        with self.assertRaises(ValueError):
            await publisher.publish("novamarket.unknown")
        with self.assertRaises(InvalidEventPayloadError):
            await publisher.publish(EventType.NOVAMARKET_DISABLED)
        # NM-02's events ARE publishable now, with their declared payload.
        published = await publisher.publish(
            "novamarket.data.available", assets=(), sources=()
        )
        self.assertIsNotNone(published)
        self.assertNotIn("novamarket.data.available", FUTURE_EVENTS)


class PersistenceTests(_SubsystemCase):
    """The configuration survives a restart; secrets never reach the snapshot."""

    async def test_the_configuration_survives_a_restart(self) -> None:
        await self.service.update_config(
            {
                "stale_after_seconds": 1234,
                "data_providers": ["nse-bhavcopy"],
                "credentials_ref": "env:NOVAMARKET_DATA_KEY",
            }
        )
        reopened = NovaMarketService(store=self.store)
        self.assertEqual(reopened.config.stale_after_seconds, 1234)
        self.assertEqual(reopened.config.data_providers, ("nse-bhavcopy",))
        self.assertEqual(reopened.config.credentials_ref, "env:NOVAMARKET_DATA_KEY")
        # The snapshot preserved the configuration; it did not start the subsystem.
        self.assertTrue(reopened.config.enabled)
        self.assertEqual(reopened.state, LifecycleState.DISABLED)

    async def test_a_secret_value_is_refused_before_it_can_be_persisted(self) -> None:
        snapshot = Path(self._tmp.name) / "novamarket.json"
        with self.assertRaises(ConfigurationError):
            await self.service.update_config(
                {"credentials_ref": "sk-live-abc123"}
            )
        self.assertFalse(
            snapshot.exists() and "sk-live-abc123" in snapshot.read_text(encoding="utf-8")
        )

    async def test_a_corrupt_json_snapshot_falls_back_to_defaults(self) -> None:
        snapshot = Path(self._tmp.name) / "novamarket.json"
        snapshot.write_text("{not json", encoding="utf-8")
        service = NovaMarketService(store=self.store)
        self.assertEqual(service.config.mode, "observe")
        self.assertEqual(service.config_error, "")

    async def test_enabled_false_is_honoured_on_the_next_start(self) -> None:
        await self.service.enable()
        await self.service.disable()
        reopened = NovaMarketService(store=self.store, event_bus=self.bus)
        await reopened.auto_start()
        self.assertEqual(reopened.state, LifecycleState.DISABLED)


class RuntimeIsolationTests(unittest.IsolatedAsyncioTestCase):
    """A failing module must not take the runtime down with it."""

    async def test_a_failing_novamarket_module_does_not_fail_runtime_start(self) -> None:
        def validator() -> None:
            raise RuntimeError("no provider")

        tmp = TemporaryDirectory()
        try:
            bus = EventBus(continue_on_error=True, history=50)
            service = NovaMarketService(
                store=JsonStateStore(Path(tmp.name)),
                event_bus=bus,
                validators=(validator,),
            )
            runtime = EventDrivenRuntime(bus)
            runtime.register_module(NovaMarketModule(service))
            await runtime.start()

            self.assertTrue(runtime.state.started)
            self.assertEqual(service.state, LifecycleState.ERROR)
            await runtime.stop()
            self.assertEqual(service.state, LifecycleState.DISABLED)
            self.assertFalse(runtime.state.started)
        finally:
            tmp.cleanup()


class ApplicationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """NovaControl detects, starts, monitors and stops the real subsystem."""

    async def asyncSetUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.app = NovaControlApplication(data_dir=Path(self._tmp.name))

    async def asyncTearDown(self) -> None:
        await self.app.stop()
        self._tmp.cleanup()

    async def test_the_application_detects_and_registers_novamarket(self) -> None:
        self.assertIn("novamarket", self.app.runtime.module_names())
        self.assertEqual(
            self.app.status()["novamarket"]["lifecycle"],
            LifecycleState.DISABLED.value,
        )

    async def test_starting_the_application_starts_novamarket(self) -> None:
        await self.app.start()

        status = self.app.status()["novamarket"]
        self.assertEqual(status["lifecycle"], "running")
        self.assertEqual(status["health"]["state"], "ok")
        self.assertEqual(status["contract_version"], MARKET_CONTRACT_VERSION)
        self.assertIsNotNone(self.app.capabilities.get("market.status"))
        self.assertIsNotNone(
            self.app.risk.declared_for("market.config.manage")
        )
        self.assertIsNone(self.app.risk.declared_for("market.trade.live"))

        report = await self.app.diagnostics_report(only=("NovaMarket",))
        row = report["components"][0]
        self.assertEqual(row["status"], "ok")
        self.assertEqual(row["metadata"]["lifecycle"], "running")

    async def test_the_application_bus_carries_novamarket_lifecycle_events(self) -> None:
        seen: list[Event] = []

        async def capture(event: Event) -> None:
            seen.append(event)

        await self.app.event_bus.subscribe("novamarket.enabled", capture)
        await self.app.start()

        self.assertTrue(seen)
        self.assertEqual(seen[0].source, "novamarket")
        self.assertEqual(seen[0].payload["lifecycle"], "running")

    async def test_disable_does_not_disturb_unrelated_functionality(self) -> None:
        await self.app.start()
        forge_before = self.app.novaforge.status().to_dict()

        status = await self.app.novamarket.disable(reason="integration test")
        self.assertEqual(status["lifecycle"], "disabled")
        self.assertIsNone(self.app.capabilities.get("market.status"))
        self.assertIsNone(self.app.risk.declared_for("market.config.manage"))

        answer = await self.app.handle_request("what is 2 + 2")
        self.assertTrue(answer.summary)
        self.assertEqual(self.app.runtime.state.started, True)
        self.assertEqual(self.app.novaforge.status().to_dict(), forge_before)

        restored = await self.app.novamarket.enable(reason="integration test")
        self.assertEqual(restored["lifecycle"], "running")
        self.assertIsNotNone(self.app.capabilities.get("market.status"))

    async def test_a_broken_novamarket_configuration_does_not_stop_the_app(self) -> None:
        await self.app.stop()
        store = JsonStateStore(Path(self._tmp.name))
        store.write("novamarket", {"config": {"mode": "paper"}})
        broken = NovaControlApplication(data_dir=Path(self._tmp.name))
        try:
            await broken.start()
            self.assertTrue(broken.runtime.state.started)
            self.assertEqual(broken.novamarket.state, LifecycleState.ERROR)
            self.assertEqual(
                broken.status()["novamarket"]["health"]["state"], "failing"
            )
            self.assertIsNone(broken.capabilities.get("market.status"))
        finally:
            await broken.stop()

    async def test_configuration_survives_an_application_restart(self) -> None:
        await self.app.start()
        await self.app.novamarket.update_config(
            {"stale_after_seconds": 1200, "data_providers": ["nse-bhavcopy"]}
        )
        await self.app.stop()

        second = NovaControlApplication(data_dir=Path(self._tmp.name))
        try:
            await second.start()
            self.assertEqual(second.novamarket.config.stale_after_seconds, 1200)
            self.assertEqual(
                second.novamarket.config.data_providers, ("nse-bhavcopy",)
            )
        finally:
            await second.stop()


class SubsystemIsolationTests(unittest.TestCase):
    """Static evidence that NM-01 did not reach into the NovaForge workstream."""

    def test_the_novamarket_package_is_its_own_tree(self) -> None:
        files = {path.name for path in _PACKAGE.glob("*.py")}
        self.assertIn("service.py", files)
        self.assertIn("module.py", files)
        self.assertNotIn("manager.py", files)

    def test_no_novamarket_module_mentions_novaforge(self) -> None:
        for path in _PACKAGE.glob("*.py"):
            text = path.read_text(encoding="utf-8").lower()
            self.assertNotIn("novaforge", text, path.name)
            self.assertNotIn("steam://", text, path.name)

    def test_the_route_namespace_is_market_not_home(self) -> None:
        api_source = (_ROOT / "src" / "novacontrol" / "api" / "app.py").read_text(
            encoding="utf-8"
        )
        # The rule is the namespace, not a total: the market surface grows with
        # every NM/PI phase (299 routes when this was written, 561 now), and a
        # hardcoded count turned that growth into a failing assertion that had
        # nothing to do with isolation.  Instead pin the shape: no /home/
        # namespace, a non-empty market namespace, and every market route
        # strictly under /market/ (including the standalone NM-14 workspace page,
        # HTML and deliberately outside ApiSurface, and the AT-01–AT-06 surface).
        paths = re.findall(
            r'@app\.(?:get|post|put|delete|patch)\(\s*"([^"]+)"', api_source
        )
        self.assertTrue(paths, "the handler parse must find routes, not nothing")
        self.assertEqual(
            [path for path in paths if path == "/home" or path.startswith("/home/")],
            [],
            "NovaMarket must not add a /home/ namespace",
        )
        market = [path for path in paths if path.startswith("/market")]
        self.assertGreater(len(market), 0, "the market namespace must be registered")
        self.assertEqual(
            [path for path in market if not path.startswith("/market/")],
            [],
            "every market route must live under /market/",
        )


if __name__ == "__main__":
    unittest.main()
