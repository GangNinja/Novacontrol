"""Focused PI-16 reassessment scope tests: only genuinely out-of-sample evidence.

The policy artifact records the split its weights were fitted on and the split
that selected its checkpoints.  A reassessment window inside either one is
in-sample: it is refused by default, can be recorded only with an explicit
``allow_in_sample`` flag, and never feeds suitability.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from novacontrol.novamarket.adaptive import (
    AdaptiveConfig,
    AdaptiveIntelligenceService,
    AdaptiveValidationError,
)
from novacontrol.novamarket.adaptive.scope import (
    describe_overlaps,
    reassessment_scope,
)
from test_novamarket_pi16 import FakeTraining, at, build_service, clock

POLICY_WITH_SPLITS: dict[str, Any] = {
    "policy_id": "policy_1",
    "name": "adaptive experiment",
    "status": "validated",
    "artifact_version": "7",
    "artifact_hash": "abc123",
    "config_hash": "cfg123",
    "environment_version": "pi13-v1",
    "reward_version": "pi14-v1",
    "dataset_provenance": {
        "dataset_version": "ds@1",
        "splits": {
            "train": {"start": at(0), "end": at(10)},
            "validation": {"start": at(10), "end": at(22)},
            "test": {"start": at(22), "end": at(31)},
        },
    },
}


def scoped_service() -> tuple[AdaptiveIntelligenceService, FakeTraining]:
    """The shared PI-16 fixture, with a policy that records its own splits."""
    service, training = build_service(config=AdaptiveConfig())
    baseline = training.policy

    def policy(policy_id: str) -> dict[str, Any]:
        if policy_id != "policy_1":
            return baseline(policy_id)
        return dict(POLICY_WITH_SPLITS)

    training.policy = policy  # type: ignore[method-assign]
    return service, training


def window(start: str, end: str) -> dict[str, str]:
    return {"start": start, "end": end}


class TestReassessmentScopeMath:
    def test_a_window_inside_the_training_split_is_in_sample(self) -> None:
        scope = reassessment_scope(POLICY_WITH_SPLITS, [window(at(2), at(6))])
        classified = scope["windows"][0]
        assert scope["verified"] is True
        assert scope["in_sample"] is True
        assert scope["out_of_sample"] is False
        assert classified["out_of_sample"] is False
        assert [hit["split"] for hit in classified["overlaps"]] == ["train"]
        assert classified["overlaps"][0]["overlap_fraction"] == 1.0

    def test_a_window_after_the_splits_is_out_of_sample(self) -> None:
        scope = reassessment_scope(POLICY_WITH_SPLITS, [window(at(24), at(29))])
        assert scope["in_sample"] is False
        assert scope["out_of_sample"] is True
        assert scope["windows"][0]["overlaps"] == []

    def test_a_window_over_the_validation_split_past_training_is_selection_overlap(
        self,
    ) -> None:
        scope = reassessment_scope(POLICY_WITH_SPLITS, [window(at(12), at(20))])
        classified = scope["windows"][0]
        assert scope["in_sample"] is True
        assert [hit["basis"] for hit in classified["overlaps"]] == ["selection"]

    def test_touching_a_split_boundary_is_not_an_overlap(self) -> None:
        policy = {
            "dataset_provenance": {"splits": {"train": {"start": at(0), "end": at(10)}}}
        }
        scope = reassessment_scope(policy, [window(at(10), at(15))])
        assert scope["windows"][0]["overlaps"] == []
        assert scope["out_of_sample"] is True

    def test_unknown_provenance_is_unverified_never_claimed_clean(self) -> None:
        scope = reassessment_scope({}, [window(at(24), at(29))])
        assert scope["verified"] is False
        assert scope["out_of_sample"] is None
        assert scope["windows"][0]["out_of_sample"] is None
        assert "could not be verified" in scope["note"]

    def test_overlap_descriptions_name_the_split_and_fraction(self) -> None:
        scope = reassessment_scope(POLICY_WITH_SPLITS, [window(at(5), at(15))])
        summary = describe_overlaps(scope)
        assert "train" in summary
        assert "selection" not in summary
        assert "50% of the window" in summary


class TestReassessmentScopeService:
    def test_an_in_sample_reassessment_is_refused_by_default(self) -> None:
        service, training = scoped_service()
        with pytest.raises(AdaptiveValidationError, match="overlap"):
            service.create_reassessment(
                {
                    "policy_id": "policy_1",
                    "regime": "strong_up",
                    "start": at(2),
                    "end": at(6),
                }
            )
        assert training.calls == []

    def test_allow_in_sample_records_flagged_evidence(self) -> None:
        service, _training = scoped_service()
        created = service.create_reassessment(
            {
                "policy_id": "policy_1",
                "regime": "strong_up",
                "start": at(2),
                "end": at(8),
                "allow_in_sample": True,
            }
        )
        record = created["reassessment"]
        assert record["in_sample"] is True
        assert record["out_of_sample"] is False
        assert record["evidence_scope"]["windows"][0]["overlaps"]

        completed = service.run_reassessment(record["reassessment_id"])
        assert completed["reassessment"]["status"] == "completed"
        stored = service.regime_performance("policy_1")["evaluations"]
        assert stored and stored[0]["in_sample"] is True
        assert stored[0]["out_of_sample"] is False
        assert stored[0]["training_overlap"]

        suitability = service.assess_suitability(
            {"policy_id": "policy_1", "regime": "strong_up"}
        )["suitability"]
        assert suitability["status"] == "not_evaluated"
        assert "in-sample" in suitability["reason"]
        assert suitability["evaluation_provenance"]["in_sample_excluded"] == 1

    def test_out_of_sample_evidence_feeds_suitability(self) -> None:
        service, _training = scoped_service()
        created = service.create_reassessment(
            {
                "policy_id": "policy_1",
                "regime": "strong_up",
                "start": at(24),
                "end": at(29),
            }
        )
        record = created["reassessment"]
        assert record["out_of_sample"] is True
        assert record["in_sample"] is False

        completed = service.run_reassessment(record["reassessment_id"])
        assert completed["reassessment"]["status"] == "completed"
        assert completed["reassessment"]["out_of_sample"] is True

        suitability = service.assess_suitability(
            {"policy_id": "policy_1", "regime": "strong_up"}
        )["suitability"]
        assert suitability["status"] != "not_evaluated"
        assert suitability["evaluation_provenance"]["out_of_sample"] is True
        assert suitability["evaluation_provenance"]["in_sample_excluded"] == 0

    def test_reassessment_evidence_is_keyed_to_the_policy_configuration(self) -> None:
        service, training = scoped_service()
        baseline = training.evaluate_regime_window

        def evaluate(policy_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
            response = baseline(policy_id, dict(payload))
            # PI-14 stamps its own evaluation configuration hash on the row.
            response["evaluation"]["config_hash"] = "pi14-evaluation-only"  # type: ignore[index]
            return response

        training.evaluate_regime_window = evaluate  # type: ignore[method-assign]
        created = service.create_reassessment(
            {
                "policy_id": "policy_1",
                "regime": "strong_up",
                "start": at(24),
                "end": at(29),
            }
        )
        service.run_reassessment(created["reassessment"]["reassessment_id"])
        stored = service.regime_performance("policy_1")["evaluations"]
        assert stored[0]["config_hash"] == "cfg123"
        suitability = service.assess_suitability(
            {"policy_id": "policy_1", "regime": "strong_up"}
        )["suitability"]
        assert suitability["status"] != "stale_evaluation"

    def test_an_explicit_in_sample_evaluation_id_cannot_feed_suitability(self) -> None:
        service, _training = scoped_service()
        service.record_regime_evaluation(
            {
                "policy_id": "policy_1",
                "regime": "strong_up",
                "evaluation_id": "pe_in_sample",
                "start": at(2),
                "end": at(8),
                "evaluated_at": at(9),
                "sample_size": 40,
                "metrics": {
                    "financial": {"total_return": 0.04, "observations": 40},
                    "risk": {"max_drawdown": 0.04, "sharpe": 1.1},
                    "trading": {"total_costs": "500"},
                },
            }
        )
        with pytest.raises(AdaptiveValidationError, match="in-sample"):
            service.assess_suitability(
                {
                    "policy_id": "policy_1",
                    "regime": "strong_up",
                    "evaluation_id": "pe_in_sample",
                }
            )

    def test_recorded_evidence_derives_its_scope_from_the_policy_windows(self) -> None:
        service, _training = scoped_service()
        stored = service.record_regime_evaluation(
            {
                "policy_id": "policy_1",
                "regime": "strong_up",
                "start": at(2),
                "end": at(8),
                "evaluated_at": at(9),
                "sample_size": 40,
                "metrics": {
                    "financial": {"total_return": 0.04, "observations": 40},
                    "risk": {"max_drawdown": 0.04, "sharpe": 1.1},
                    "trading": {"total_costs": "500"},
                },
            }
        )["evaluation"]
        assert stored["in_sample"] is True
        assert stored["out_of_sample"] is False
        assert stored["scope_verified"] is True

    def test_a_walk_forward_sequence_with_one_leaking_fold_is_refused(self) -> None:
        service, training = scoped_service()
        with pytest.raises(AdaptiveValidationError, match="train"):
            service.create_reassessment(
                {
                    "policy_id": "policy_1",
                    "regime": "strong_up",
                    "windows": [window(at(2), at(6)), window(at(24), at(29))],
                }
            )
        assert training.calls == []

    def test_a_flagged_walk_forward_sequence_reports_each_fold(self) -> None:
        service, _training = scoped_service()
        created = service.create_reassessment(
            {
                "policy_id": "policy_1",
                "regime": "strong_up",
                "windows": [window(at(2), at(6)), window(at(24), at(29))],
                "allow_in_sample": True,
            }
        )
        record = created["reassessment"]
        assert [item["in_sample"] for item in record["config"]["windows"]] == [True, False]
        completed = service.run_reassessment(record["reassessment_id"])["reassessment"]
        result = completed["result"]
        assert [fold["in_sample"] for fold in result["folds"]] == [True, False]
        assert [fold["out_of_sample"] for fold in result["folds"]] == [False, True]
        assert result["aggregate"]["in_sample"] is True
        assert result["aggregate"]["out_of_sample"] is False

    def test_a_fully_out_of_sample_sequence_is_marked_out_of_sample(self) -> None:
        service, _training = scoped_service()
        created = service.create_reassessment(
            {
                "policy_id": "policy_1",
                "regime": "strong_up",
                "windows": [window(at(23), at(26)), window(at(27), at(30))],
            }
        )
        record = created["reassessment"]
        assert record["out_of_sample"] is True
        completed = service.run_reassessment(record["reassessment_id"])["reassessment"]
        assert completed["result"]["aggregate"]["out_of_sample"] is True
        assert completed["result"]["aggregate"]["scope_verified"] is True
