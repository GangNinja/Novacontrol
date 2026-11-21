"""Focused PI-24 event intelligence, integration and safety tests."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest import mock

from novacontrol.novamarket.event_intelligence import (
    EventIntelligenceNotFoundError,
    EventIntelligenceService,
    EventIntelligenceSources,
    EventIntelligenceValidationError,
)
from novacontrol.novamarket.event_intelligence.store import EventIntelligenceStore
from novacontrol.novamarket.evaluation import record_from_mapping
from novacontrol.novamarket.risk_intelligence.evidence import RiskIntelligenceSources
from novacontrol.novamarket.risk_intelligence.service import RiskIntelligenceService

NOW = datetime(2026, 3, 15, 12, tzinfo=UTC)
INSTRUMENT = "XNSE:AAA"
HORIZON = "5d"


def _event(
    event_id: str,
    *,
    subcategory: str = "earnings",
    category: str = "company",
    instrument: str = INSTRUMENT,
    verification: str = "multi_source_corroborated",
    importance: str = "high",
    published: datetime | None = None,
    occurred: datetime | None = None,
    sectors: tuple[str, ...] = ("Information Technology",),
    relationship: str = "direct",
) -> dict:
    published = published or datetime(2026, 3, 10, 9, tzinfo=UTC)
    occurred = occurred or datetime(2026, 3, 12, 9, tzinfo=UTC)
    return {
        "event_id": event_id,
        "event_key": event_id,
        "version": 1,
        "title": f"{event_id} headline",
        "description": "Point-in-time event evidence.",
        "category": category,
        "subcategory": subcategory,
        "status": "active",
        "verification": verification,
        "importance": importance,
        "confidence": 0.8,
        "occurred_at": occurred.isoformat(),
        "published_at": published.isoformat(),
        "discovered_at": published.isoformat(),
        "retrieved_at": published.isoformat(),
        "updated_at": published.isoformat(),
        "market": "IN",
        "instruments": [instrument] if instrument else [],
        "sectors": list(sectors),
        "evidence_count": 2,
        "independent_sources": 2,
        "mappings": [
            {
                "instrument_id": instrument,
                "relationship": relationship,
                "reason": "event text names the instrument",
                "sector": sectors[0] if sectors else "",
            }
        ]
        if instrument
        else [],
        "verification_detail": {"reasons": ["multiple independent sources"], "conflicts": []},
    }


EVENTS = [
    _event("evt_earnings"),
    _event(
        "evt_macro",
        subcategory="inflation",
        category="macroeconomic",
        instrument="",
        importance="high",
        published=datetime(2026, 3, 14, 9, tzinfo=UTC),
        occurred=datetime(2026, 3, 14, 9, tzinfo=UTC),
        sectors=(),
    ),
    _event(
        "evt_unverified",
        subcategory="regulatory_investigation",
        importance="moderate",
        verification="single_source",
        published=datetime(2026, 3, 14, 8, tzinfo=UTC),
        occurred=datetime(2026, 3, 14, 8, tzinfo=UTC),
    ),
    _event(
        "evt_conflict",
        subcategory="litigation",
        importance="moderate",
        verification="conflicting_evidence",
        published=datetime(2026, 3, 14, 7, tzinfo=UTC),
        occurred=datetime(2026, 3, 14, 7, tzinfo=UTC),
    ),
    _event(
        "evt_other",
        instrument="XNSE:BBB",
        sectors=("Financials",),
        published=datetime(2026, 3, 13, 9, tzinfo=UTC),
    ),
    _event(
        "evt_future",
        instrument=INSTRUMENT,
        published=datetime(2026, 3, 20, 9, tzinfo=UTC),
        occurred=datetime(2026, 3, 21, 9, tzinfo=UTC),
    ),
]


def _bars(**_: object) -> list[dict]:
    closes = [100 + index for index in range(10)]
    start = datetime(2026, 3, 5, 9, tzinfo=UTC)
    return [
        {
            "observed_at": (start + timedelta(days=index)).isoformat(),
            "close": str(value),
            "quality": "valid",
            "partial": False,
        }
        for index, value in enumerate(closes)
    ]


def _sources(*, bars=_bars) -> EventIntelligenceSources:
    def events(instrument_id=None, limit=200):
        rows = [
            row
            for row in EVENTS
            if not instrument_id or instrument_id in row["instruments"] or not row["instruments"]
        ]
        return rows[:limit]

    return EventIntelligenceSources(
        events=events,
        event=lambda event_id, version=None: next(
            (
                {
                    **row,
                    "evidence": [
                        {
                            "source_id": "nse",
                            "publisher": "Exchange",
                            "url": "https://example.test/disclosure",
                            "published_at": row["published_at"],
                            "retrieved_at": row["retrieved_at"],
                            "reliability": "official",
                            "status": "retrieved",
                        }
                    ],
                }
                for row in EVENTS
                if row["event_id"] == event_id
            ),
            {},
        ),
        event_history=lambda event_id: {
            "versions": [row for row in EVENTS if row["event_id"] == event_id]
        },
        instrument=lambda instrument_id: {
            "instrument_id": instrument_id,
            "name": "Alpha Industries" if instrument_id == INSTRUMENT else "Beta Finance",
            "sectors": ["Information Technology"]
            if instrument_id == INSTRUMENT
            else ["Financials"],
            "exchange": "XNSE",
        },
        regime=lambda instrument_id: {"primary": "weak_uptrend"},
        bars=bars,
        forecasts=lambda instrument_id, horizon, limit=50: [
            {
                "forecast_id": "fc_before",
                "model_id": "naive",
                "model_version": "v1",
                "predicted_price": "105",
                "as_of": "2026-03-11T09:00:00+00:00",
            },
            {
                "forecast_id": "fc_after",
                "model_id": "naive",
                "model_version": "v1",
                "predicted_price": "108",
                "as_of": "2026-03-13T09:00:00+00:00",
            },
        ],
        evaluations=lambda limit=8: [],
        portfolio=lambda portfolio_id: {
            "portfolio": {"portfolio_id": portfolio_id},
            "holdings": [{"instrument_id": INSTRUMENT}],
        },
        agents=lambda **_: {},
    )


def _service(**kwargs: object) -> EventIntelligenceService:
    return EventIntelligenceService(_sources(), store_path=":memory:", clock=lambda: NOW, **kwargs)


class NormalizationTests(unittest.TestCase):
    def test_normalization_preserves_times_status_and_source_provenance(self) -> None:
        service = _service()
        try:
            event = service.event("evt_earnings")["event"]
        finally:
            service.close()
        self.assertEqual(event["event_group"], "corporate")
        self.assertEqual(event["status"], "verified")
        self.assertEqual(event["instruments"], [INSTRUMENT])
        self.assertEqual(event["companies"], ["Alpha Industries"])
        self.assertEqual(event["exchange"], "XNSE")
        self.assertEqual(event["occurred_at"], "2026-03-12T09:00:00+00:00")
        self.assertNotEqual(event["occurred_at"], event["published_at"])
        self.assertEqual(event["source_references"][0]["source_id"], "nse")
        self.assertFalse(event["is_price_prediction"])
        self.assertFalse(event["instruction_risk"])

    def test_unknown_event_is_explicit_and_does_not_invent_a_record(self) -> None:
        service = _service()
        try:
            with self.assertRaises(EventIntelligenceNotFoundError):
                service.event("evt_missing")
        finally:
            service.close()

    def test_sync_is_idempotent_and_skips_malformed_rows(self) -> None:
        sources = _sources()
        bad = replace(
            sources,
            events=lambda **_: [
                *EVENTS[:2],
                {"event_id": "", "title": "broken"},
            ],
        )
        service = EventIntelligenceService(bad, store_path=":memory:", clock=lambda: NOW)
        try:
            first = service.sync()
            second = service.sync()
        finally:
            service.close()
        self.assertEqual(first["seen"], 3)
        self.assertEqual(first["synced"], 2)
        self.assertEqual(second["synced"], 0)
        self.assertEqual(len(first["skipped"]), 1)


class RelevanceTests(unittest.TestCase):
    def test_direct_market_and_unrelated_relevance_are_distinct(self) -> None:
        service = _service()
        try:
            direct = service.assess_relevance(
                {"instrument_id": INSTRUMENT, "horizon": HORIZON, "event_id": "evt_earnings"}
            )["assessment"]
            market = service.assess_relevance(
                {"instrument_id": INSTRUMENT, "horizon": HORIZON, "event_id": "evt_macro"}
            )["assessment"]
            unrelated = service.assess_relevance(
                {"instrument_id": INSTRUMENT, "horizon": HORIZON, "event_id": "evt_other"}
            )["assessment"]
        finally:
            service.close()
        self.assertEqual(direct["relevance"], "direct")
        self.assertEqual(direct["confidence"], "high")
        self.assertEqual(market["relevance"], "market_wide")
        self.assertEqual(unrelated["relevance"], "not_relevant")
        self.assertEqual(direct["direction"], "not_estimated")
        self.assertFalse(direct["is_price_prediction"])

    def test_published_after_as_of_is_insufficient_not_future_evidence(self) -> None:
        service = _service()
        try:
            body = service.assess_relevance(
                {
                    "instrument_id": INSTRUMENT,
                    "horizon": HORIZON,
                    "event_id": "evt_future",
                    "as_of": NOW.isoformat(),
                }
            )
            features = service.features(
                {"instrument_id": INSTRUMENT, "horizon": HORIZON, "as_of": NOW.isoformat()}
            )
        finally:
            service.close()
        self.assertEqual(body["assessment"]["relevance"], "insufficient_evidence")
        ids = {row["event_id"] for row in features["pi04_rows"]}
        self.assertNotIn("evt_future", ids)


class FeatureAndWarningTests(unittest.TestCase):
    def test_features_are_point_in_time_and_have_pi04_rows(self) -> None:
        service = _service()
        try:
            body = service.features(
                {"instrument_id": INSTRUMENT, "horizon": HORIZON, "as_of": NOW.isoformat()}
            )
        finally:
            service.close()
        names = {row["name"] for row in body["features"]}
        self.assertIn("event_count_20d", names)
        self.assertIn("event_earnings_flag", names)
        self.assertIn("event_upcoming_count", names)
        self.assertTrue(body["point_in_time"])
        self.assertFalse(body["future_outcomes_included"])
        self.assertTrue(body["pi04_rows"])
        self.assertFalse(any(row["event_id"] == "evt_future" for row in body["pi04_rows"]))

    def test_warning_statuses_cover_volatility_uncertainty_and_unverified(self) -> None:
        sources = _sources()
        service = EventIntelligenceService(
            replace(
                sources,
                events=lambda **_: [EVENTS[0]],
            ),
            store_path=":memory:",
            clock=lambda: NOW,
        )
        try:
            volatility = service.warnings({"instrument_id": INSTRUMENT, "horizon": HORIZON})
        finally:
            service.close()
        service = EventIntelligenceService(
            replace(
                sources,
                events=lambda **_: [EVENTS[2], EVENTS[3]],
            ),
            store_path=":memory:",
            clock=lambda: NOW,
        )
        try:
            uncertainty = service.warnings(
                {"instrument_id": INSTRUMENT, "horizon": HORIZON, "as_of": NOW.isoformat()}
            )
        finally:
            service.close()
        self.assertEqual(volatility["status"], "event_volatility_risk")
        self.assertIsNone(volatility["calibrated_event_probability"])
        self.assertFalse(volatility["probability_claimed"])
        self.assertIn(
            "event_unverified",
            {row["code"] for row in uncertainty["warnings"]},
        )
        self.assertEqual(uncertainty["risk_authority"], "NM-08")

    def test_a_malformed_event_does_not_poison_later_warnings(self) -> None:
        sources = _sources()
        service = EventIntelligenceService(
            replace(
                sources,
                events=lambda **_: [
                    EVENTS[0],
                    {"event_id": "broken", "published_at": "not-a-time"},
                ],
            ),
            store_path=":memory:",
            clock=lambda: NOW,
        )
        try:
            warning = service.warnings({"instrument_id": INSTRUMENT, "horizon": HORIZON})
        finally:
            service.close()
        self.assertEqual(warning["status"], "event_volatility_risk")
        self.assertTrue(warning["errors"])

    def test_forecast_context_never_mutates_a_forecast(self) -> None:
        service = _service()
        try:
            body = service.forecasts(
                {"instrument_id": INSTRUMENT, "horizon": HORIZON, "as_of": NOW.isoformat()}
            )
        finally:
            service.close()
        self.assertFalse(body["event_adjustment"]["applied"])
        self.assertEqual(body["event_adjustment"]["price_adjustment"], "none")
        self.assertFalse(body["forecast_mutated"])
        self.assertFalse(body["model_version_changed"])
        self.assertFalse(body["abstention_recommended"])


class ImpactOutcomeTests(unittest.TestCase):
    def test_impact_reports_channels_without_direction_or_probability(self) -> None:
        service = _service()
        try:
            body = service.impact(
                {"event_id": "evt_earnings", "instrument_id": INSTRUMENT, "horizon": HORIZON}
            )
        finally:
            service.close()
        self.assertEqual(body["direction"], "not_estimated")
        self.assertEqual(body["price_adjustment"], "none")
        self.assertIsNone(body["calibrated_event_probability"])
        self.assertFalse(body["causation_established"])
        self.assertIn("fundamental_expectations", body["channels"])

    def test_outcome_uses_stored_bars_and_preserves_correlation_caveat(self) -> None:
        service = _service()
        try:
            body = service.outcomes(
                {"event_id": "evt_earnings", "instrument_id": INSTRUMENT, "horizon": "1d"}
            )
            study = service.outcome_study({"instrument_id": INSTRUMENT})
        finally:
            service.close()
        self.assertEqual(body["status"], "recorded")
        self.assertIsNotNone(body["post_return"])
        self.assertEqual(body["resolution"], "1d")
        self.assertEqual(body["forecast_comparison"]["before_event"]["forecast_id"], "fc_before")
        self.assertEqual(body["forecast_comparison"]["after_event"]["forecast_id"], "fc_after")
        self.assertIsNotNone(body["prediction_error"])
        self.assertFalse(body["causation_established"])
        self.assertTrue(body["correlation_only"])
        self.assertEqual(study["total"], 1)
        self.assertFalse(study["studies"][0]["sufficient"])

    def test_outcome_marks_missing_bars_as_insufficient(self) -> None:
        service = EventIntelligenceService(
            _sources(bars=lambda **_: []),
            store_path=":memory:",
            clock=lambda: NOW,
        )
        try:
            body = service.outcomes(
                {"event_id": "evt_earnings", "instrument_id": INSTRUMENT, "horizon": "1d"}
            )
        finally:
            service.close()
        self.assertEqual(body["status"], "insufficient_data")
        self.assertIsNone(body["post_return"])


class HandoffTests(unittest.TestCase):
    def test_agent_evidence_keeps_unverified_events_separate(self) -> None:
        service = _service()
        try:
            body = service.agent_evidence(
                {"instrument_id": INSTRUMENT, "horizon": HORIZON, "as_of": NOW.isoformat()}
            )
        finally:
            service.close()
        self.assertFalse(body["unverified_presented_as_fact"])
        self.assertFalse(body["execution_authority"])
        self.assertTrue(body["trading_decision_authority_unchanged"])
        self.assertTrue(body["verified_events"])
        self.assertTrue(body["unverified_events"])
        self.assertIn("fin-risk", body["roles"])

    def test_portfolio_context_is_read_only(self) -> None:
        service = _service()
        try:
            body = service.portfolio_context("pf_1", horizon=HORIZON)
        finally:
            service.close()
        self.assertEqual(body["portfolio_id"], "pf_1")
        self.assertFalse(body["creates_risk_decision"])
        self.assertFalse(body["financial_mutation"])
        self.assertEqual(body["risk_authority"], "NM-08")
        self.assertTrue(body["positions"])
        with self.assertRaises(EventIntelligenceValidationError):
            service.portfolio_context("pf_1", horizon="not-a-horizon")

    def test_analyze_runs_the_read_only_pipeline(self) -> None:
        service = _service()
        try:
            body = service.analyze(
                {"instrument_id": INSTRUMENT, "horizon": HORIZON, "as_of": NOW.isoformat()}
            )
        finally:
            service.close()
        self.assertEqual(body["phase"], "PI-24")
        self.assertEqual(body["direction"], "not_estimated")
        self.assertFalse(body["financial_mutation"])
        self.assertFalse(body["automatic_promotion"])
        self.assertEqual(body["risk_authority"], "NM-08")
        self.assertIn("forecast_context", body["results"])
        self.assertFalse(body["results"]["forecast_context"]["forecast_mutated"])

    def test_risk_intelligence_accepts_event_loader_without_new_authority(self) -> None:
        sources = RiskIntelligenceSources(
            portfolio_list=lambda limit: [],
            portfolio_detail=lambda portfolio_id: {},
            valuation_history=lambda portfolio_id, limit: [],
            bars=lambda instrument_id, limit: [],
            forecast_views=lambda portfolio_id, horizon: {},
            instrument_evidence=lambda instrument_id: {},
            agent_decisions=lambda portfolio_id, limit: {},
            optimization_latest=lambda portfolio_id, horizon: {},
            risk_portfolio=lambda portfolio_id: {},
            risk_position=lambda portfolio_id, instrument_id: {},
            runtime_status=lambda: {"mode": "disabled"},
        )
        service = RiskIntelligenceService(
            sources,
            store_path=":memory:",
            clock=lambda: NOW,
            event_loader=lambda portfolio_id, horizon: {
                "status": "event_uncertainty",
                "warnings": [
                    {
                        "code": "event_unverified",
                        "severity": "watch",
                        "instrument_id": INSTRUMENT,
                        "event_ids": ["evt_unverified"],
                        "explanation": "unverified event",
                    }
                ],
            },
        )
        try:
            body = service._event_risk("pf_1", HORIZON)
        finally:
            service.close()
        self.assertTrue(body["available"])
        self.assertEqual(body["status"], "event_uncertainty")
        self.assertEqual(body["risk_authority"], "NM-08")
        self.assertFalse(body["creates_risk_decision"])


class StoreAndContractTests(unittest.TestCase):
    def test_immutable_store_rejects_update_and_counts_full_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "ei.sqlite3")
            store = EventIntelligenceStore(path, clock=lambda: NOW)
            try:
                store.save(
                    "outcome",
                    {"record_id": "one", "status": "recorded"},
                    record_id="one",
                    instrument_id=INSTRUMENT,
                )
                self.assertTrue(store.integrity()["ok"])
            finally:
                store.close()
            connection = sqlite3.connect(path)
            try:
                with self.assertRaises(sqlite3.DatabaseError):
                    connection.execute("UPDATE ei_records SET status='changed'")
                connection.rollback()
            finally:
                connection.close()

    def test_contract_and_navigation_expose_eight_views_and_authority(self) -> None:
        service = _service()
        try:
            contract = service.contract()
            navigation = service.navigation()
        finally:
            service.close()
        self.assertEqual(contract["authority"]["risk_authority"], "NM-08")
        self.assertEqual(contract["authority"]["execution_authority"], "NM-10")
        self.assertFalse(contract["authority"]["financial_mutation"])
        self.assertFalse(contract["authority"]["changes_forecast"])
        self.assertFalse(contract["new_feed_added"])
        self.assertEqual(len(navigation["views"]), 8)

    def test_config_is_bounded(self) -> None:
        service = _service()
        try:
            body = service.update_config({"min_outcome_samples": 12}, actor="tester")
            with self.assertRaises(EventIntelligenceValidationError):
                service.update_config({"max_events": 0}, actor="tester")
        finally:
            service.close()
        self.assertEqual(body["config"]["min_outcome_samples"], 12)
        self.assertEqual(body["actor"], "tester")


class ApiSurfaceTests(unittest.TestCase):
    EXPECTED = {
        ("GET", "/market/event-intelligence/status"),
        ("GET", "/market/event-intelligence/contracts"),
        ("GET", "/market/event-intelligence/navigation"),
        ("GET", "/market/event-intelligence/config"),
        ("PUT", "/market/event-intelligence/config"),
        ("GET", "/market/event-intelligence/events"),
        ("GET", "/market/event-intelligence/events/{event_id}"),
        ("GET", "/market/event-intelligence/timeline"),
        ("POST", "/market/event-intelligence/relevance"),
        ("POST", "/market/event-intelligence/impact"),
        ("POST", "/market/event-intelligence/warnings"),
        ("POST", "/market/event-intelligence/features"),
        ("POST", "/market/event-intelligence/forecast-context"),
        ("POST", "/market/event-intelligence/outcomes"),
        ("POST", "/market/event-intelligence/outcomes/study"),
        ("POST", "/market/event-intelligence/forecast-evaluation"),
        ("POST", "/market/event-intelligence/agents"),
        ("GET", "/market/event-intelligence/portfolio/{portfolio_id}/context"),
        ("POST", "/market/event-intelligence/sync"),
        ("POST", "/market/event-intelligence/ingest"),
        ("POST", "/market/event-intelligence/analyze"),
        ("GET", "/market/event-intelligence/overview"),
        ("GET", "/market/event-intelligence/history"),
        ("GET", "/market/event-intelligence/health"),
    }

    def test_every_pi24_route_is_registered_and_non_rendering(self) -> None:
        from novacontrol.api.models import ApiSurface
        from novacontrol.api.route_consumers import ROUTE_CONSUMERS

        routes = {(row.method, row.path) for row in ApiSurface.default().routes}
        self.assertEqual(self.EXPECTED - routes, set())
        for route in self.EXPECTED:
            self.assertEqual(ROUTE_CONSUMERS[route], "no-render")

    def test_pi24_routes_have_no_execution_or_broker_path(self) -> None:
        from novacontrol.api.models import ApiSurface

        rows = [
            row
            for row in ApiSurface.default().routes
            if row.path.startswith("/market/event-intelligence")
        ]
        self.assertEqual(len(rows), len(self.EXPECTED))
        for row in rows:
            for forbidden in ("order", "fill", "execute", "broker", "ledger"):
                self.assertNotIn(forbidden, row.path)
            self.assertTrue(row.authenticated)


class ApplicationIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        try:
            from fastapi.testclient import TestClient
        except Exception as exc:  # pragma: no cover - optional dev dependency
            raise unittest.SkipTest(f"fastapi.testclient requires httpx: {exc}") from exc
        cls._tmp = tempfile.TemporaryDirectory()
        cls._env = mock.patch.dict(
            os.environ,
            {
                "NOVACONTROL_API_TOKEN": "pi24-test-token",
                "NOVACONTROL_AUTONOMY_OPERATOR_TOKEN": "pi24-operator-token",
            },
            clear=False,
        )
        cls._env.start()
        from novacontrol.api.app import create_app

        cls._client = TestClient(create_app(data_dir=cls._tmp.name))
        cls._client.__enter__()
        cls._headers = {"Authorization": "Bearer pi24-test-token"}
        cls._operator = {
            **cls._headers,
            "X-Autonomy-Operator-Token": "pi24-operator-token",
        }

    @classmethod
    def tearDownClass(cls) -> None:
        cls._client.__exit__(None, None, None)
        cls._env.stop()
        cls._tmp.cleanup()

    def test_actual_application_opens_pi24_service(self) -> None:
        response = self._client.get("/market/event-intelligence/status", headers=self._headers)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["phase"], "PI-24")
        self.assertTrue(body["initialised"])
        self.assertFalse(body["counts"]["total"] < 0)

    def test_pi24_reads_require_authentication(self) -> None:
        for path in (
            "/market/event-intelligence/status",
            "/market/event-intelligence/contracts",
            "/market/event-intelligence/health",
            "/market/event-intelligence/overview",
        ):
            self.assertEqual(self._client.get(path).status_code, 401)

    def test_analysis_requires_operator_permission(self) -> None:
        for path in (
            "/market/event-intelligence/analyze",
            "/market/event-intelligence/features",
            "/market/event-intelligence/forecast-context",
        ):
            response = self._client.post(
                path,
                headers=self._headers,
                json={"instrument_id": INSTRUMENT, "horizon": HORIZON},
            )
            self.assertEqual(response.status_code, 403, response.text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


def _evaluation_record(
    index: int,
    *,
    event_aware: bool,
    error: float,
    generated: datetime | None = None,
) -> dict:
    """One stored forecast + realised outcome, in PI-11 record shape."""

    generated = generated or datetime(2026, 3, 11, 9, tzinfo=UTC)
    reference = 100.0
    actual_return = 0.01
    predicted_return = actual_return + error
    return {
        "forecast_id": f"fc_{index}_{'aware' if event_aware else 'base'}",
        "source": "records",
        "origin": "synthetic_fixture",
        "instrument_id": INSTRUMENT,
        "horizon": HORIZON,
        "generated_at": generated.isoformat(),
        "target_at": (generated + timedelta(days=5)).isoformat(),
        "reference_at": (generated - timedelta(minutes=5)).isoformat(),
        "actual_observed_at": (generated + timedelta(days=6)).isoformat(),
        "reference_price": reference,
        "predicted_price": reference * (1.0 + predicted_return),
        "actual_price": reference * (1.0 + actual_return),
        "predicted_return": predicted_return,
        "actual_return": actual_return,
        "predicted_direction": "up",
        "feature_set_version": (
            "pi24-event-features-1.0" if event_aware else "pi04-features-1.0"
        ),
        "metadata": {"event_aware": True} if event_aware else {},
    }


def _evaluation_service(
    records: list[dict], *, min_evaluation_samples: int | None = None
) -> EventIntelligenceService:
    from novacontrol.novamarket.event_intelligence import EventIntelligenceConfig

    sources = replace(
        _sources(),
        evaluation_records=lambda **_kwargs: {
            "ok": True,
            "records": [record_from_mapping(row) for row in records],
            "rejected_count": 0,
        },
    )
    config = (
        EventIntelligenceConfig(min_evaluation_samples=min_evaluation_samples)
        if min_evaluation_samples is not None
        else None
    )
    return EventIntelligenceService(
        sources, store_path=":memory:", clock=lambda: NOW, config=config
    )


class EventAwareEvaluationTests(unittest.TestCase):
    """PI-24 event-aware versus non-event-aware forecast error comparison."""

    def test_a_sufficient_sample_reports_a_measured_comparison(self) -> None:
        records = [
            _evaluation_record(index, event_aware=True, error=0.001)
            for index in range(20)
        ] + [
            _evaluation_record(
                index + 20,
                event_aware=False,
                error=0.02,
                generated=datetime(2025, 6, 2, 9, tzinfo=UTC),
            )
            for index in range(20)
        ]
        service = _evaluation_service(records)
        try:
            body = service.forecast_evaluation(
                {"instrument_id": INSTRUMENT, "horizon": HORIZON}
            )
        finally:
            service.close()
        self.assertEqual(body["status"], "completed")
        self.assertTrue(body["sample_sufficient"])
        self.assertEqual(body["samples"]["event_aware"], 20)
        self.assertEqual(body["samples"]["non_event_aware"], 20)
        self.assertEqual(body["samples"]["total"], 40)
        comparison = body["comparison"]
        self.assertIsNotNone(comparison)
        self.assertEqual(comparison["verdict"], "event_aware_better")
        self.assertTrue(comparison["measured"])
        self.assertLess(comparison["delta"]["return_mae"], 0)
        self.assertLess(comparison["delta"]["relative_return_mae_change"], -0.02)
        self.assertTrue(body["improvement_claimed"])
        self.assertFalse(body["causation_established"])
        self.assertTrue(body["correlation_only"])
        self.assertEqual(body["policy_version"], "pi24.event-aware-evaluation.1.0")

    def test_an_insufficient_sample_never_claims_an_improvement(self) -> None:
        records = [
            _evaluation_record(index, event_aware=True, error=0.001)
            for index in range(3)
        ] + [
            _evaluation_record(
                index + 3,
                event_aware=False,
                error=0.02,
                generated=datetime(2025, 6, 2, 9, tzinfo=UTC),
            )
            for index in range(3)
        ]
        service = _evaluation_service(records)
        try:
            body = service.forecast_evaluation(
                {"instrument_id": INSTRUMENT, "horizon": HORIZON}
            )
        finally:
            service.close()
        self.assertEqual(body["status"], "insufficient_data")
        self.assertFalse(body["sample_sufficient"])
        self.assertIsNone(body["comparison"])
        self.assertIsNone(body["verdict"])
        self.assertFalse(body["improvement_claimed"])
        self.assertEqual(body["minimum_samples"], 20)
        self.assertEqual(body["cohorts"]["event_aware"]["samples"], 3)
        self.assertEqual(len(body["reasons"]), 2)

    def test_no_material_difference_is_not_reported_as_an_improvement(self) -> None:
        records = [
            _evaluation_record(index, event_aware=True, error=0.010)
            for index in range(10)
        ] + [
            _evaluation_record(
                index + 10,
                event_aware=False,
                error=0.0102,
                generated=datetime(2025, 6, 2, 9, tzinfo=UTC),
            )
            for index in range(10)
        ]
        service = _evaluation_service(records, min_evaluation_samples=5)
        try:
            body = service.forecast_evaluation({})
        finally:
            service.close()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["comparison"]["verdict"], "no_material_difference")
        self.assertFalse(body["improvement_claimed"])

    def test_event_proximity_labels_an_undeclared_record_point_in_time(self) -> None:
        # The fixture event was published 2026-03-10; a forecast generated the
        # next day had that event in its lookback window.
        near = _evaluation_record(
            0, event_aware=False, error=0.01, generated=datetime(2026, 3, 11, 9, tzinfo=UTC)
        )
        near["feature_set_version"] = "pi04-features-1.0"
        far = _evaluation_record(
            1, event_aware=False, error=0.02, generated=datetime(2025, 1, 5, 9, tzinfo=UTC)
        )
        far["feature_set_version"] = "pi04-features-1.0"
        service = _evaluation_service([near, far])
        try:
            body = service.forecast_evaluation({})
        finally:
            service.close()
        self.assertEqual(body["samples"]["event_aware"], 1)
        self.assertEqual(body["samples"]["non_event_aware"], 1)
        self.assertEqual(body["samples"]["label_basis"]["event_proximity"], 1)
        self.assertEqual(body["samples"]["label_basis"]["declared"], 0)

    def test_declared_event_awareness_is_honoured(self) -> None:
        record = _evaluation_record(0, event_aware=True, error=0.01)
        record["feature_set_version"] = "pi04-features-1.0"
        record["metadata"] = {"event_ids": ["evt_earnings"]}
        service = _evaluation_service([record])
        try:
            body = service.forecast_evaluation({})
        finally:
            service.close()
        self.assertEqual(body["samples"]["event_aware"], 1)
        self.assertEqual(body["samples"]["label_basis"]["declared"], 1)

    def test_the_result_is_persisted_as_evidence(self) -> None:
        service = _evaluation_service([_evaluation_record(0, event_aware=True, error=0.01)])
        try:
            body = service.forecast_evaluation({})
            rows = service.store.list("forecast_evaluation", limit=5)
        finally:
            service.close()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evaluation_id"], body["evaluation_id"])
        self.assertEqual(rows[0]["status"], "insufficient_data")
