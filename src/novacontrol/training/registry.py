"""The SFT model registry: trained models, their status, and their provenance.

A completed training run does NOT become a production model. It registers as
``EXPERIMENTAL``, the post-training comparison moves it to ``EVALUATING``, and
only an explicit approval — backed by a ``pass`` verdict — may move it to
``APPROVED``; only a second, explicit promotion may move it to ``PRODUCTION``.
Every transition this module accepts is written down in
:data:`~novacontrol.training.models.ALLOWED_STATUS_TRANSITIONS`; anything else
is refused with a reason rather than quietly applied.

The registry stores what a future model selection needs: the base model, the
adapter (path/rank/alpha — separate from the base, which is never duplicated),
the dataset version that trained it, the run that produced it, the evaluation it
was approved on, and the resource requirements an operator should know before
loading it. Promotion also writes ROLLBACK METADATA: which model was in
production before, and what it was, so an operator can reverse a promotion
without reconstructing history from the audit trail.

The existing ``ModelManager`` is consulted, never replaced: when it can answer
whether a base model is known, the registry records that answer; the model
selection layer is left exactly as it was.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from novacontrol.evaluation.models import now_iso
from novacontrol.training.models import (
    ALLOWED_STATUS_TRANSITIONS,
    MAX_MODEL_HISTORY,
    ModelStatus,
    TrainingEvaluation,
    TrainingModelRecord,
    TrainingRun,
)
from novacontrol.training.storage import TrainingModelRepository

_STATUSES = frozenset(member.value for member in ModelStatus)


def _text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    text = str(value).strip()
    return text or default


class SFTModelRegistry:
    """Registration, evaluation state and explicit promotion for trained models."""

    def __init__(
        self,
        models: TrainingModelRepository,
        *,
        model_manager: Any = None,
    ) -> None:
        self.models = models
        self._model_manager = model_manager

    # -- registration ----------------------------------------------------------

    def register(
        self,
        run: TrainingRun,
        *,
        base_model: str = "",
        name: str = "",
        adapter: Mapping[str, Any] | None = None,
        training_method: str = "",
        algorithm: str = "",
        resource_requirements: Mapping[str, Any] | None = None,
        notes: str = "",
    ) -> TrainingModelRecord:
        """Register a finished run as an EXPERIMENTAL model.

        Nothing here approves anything: the record starts EXPERIMENTAL, and the
        evaluation and approval methods below are the only ways forward.

        ``algorithm`` says which objective adapted the model (``sft``, ``dpo``,
        ``orpo``) and is read from the run when the caller does not name it, so a
        phase that adds an objective does not have to add a registry.
        """
        config = dict(run.training_config)
        resolved_base = base_model or _text(config.get("base_model")) or run.model
        method = training_method or _text(config.get("training_method")) or "lora"
        objective = algorithm or _text(getattr(run, "algorithm", "")) or "sft"
        adapter_info = dict(adapter or {})
        if not adapter_info:
            adapter_info = {
                "kind": method,
                "rank": config.get("lora_rank"),
                "alpha": config.get("lora_alpha"),
                "dropout": config.get("lora_dropout"),
                "target_modules": list(config.get("target_modules") or []),
                "base_model": resolved_base,
                "path": "",
            }
        record = TrainingModelRecord(
            name=name or f"{resolved_base}+{run.dataset_type or 'sft'}",
            base_model=resolved_base,
            training_method=method,
            algorithm=objective,
            dataset_version=run.dataset_version,
            training_run_id=run.run_id,
            adapter=adapter_info,
            resource_requirements=dict(resource_requirements or run.estimate),
            notes=str(notes or ""),
        )
        known = self._base_model_known(resolved_base)
        if known is not None:
            record = replace(
                record,
                adapter={**dict(record.adapter), "base_model_known": known},
            )
        return self.models.save(record)

    def _base_model_known(self, base_model: str) -> bool | None:
        """Whether the existing model registry knows this base model (or None)."""
        if self._model_manager is None or not base_model:
            return None
        try:
            registry = getattr(self._model_manager, "registry", None)
            if registry is not None and hasattr(registry, "get"):
                return registry.get(base_model) is not None
            size = self._model_manager.model_size_bytes(base_model)
            if size is not None:
                return True
        except Exception:  # noqa: BLE001 - a registry lookup must not break a registration
            return None
        return None

    # -- reading ---------------------------------------------------------------

    def get(self, model_id: str) -> TrainingModelRecord | None:
        return self.models.get(model_id)

    def list(
        self, *, status: str = "", base_model: str = "", limit: int = 0
    ) -> tuple[TrainingModelRecord, ...]:
        return self.models.list(status=status, base_model=base_model, limit=limit)

    def counts(self) -> dict[str, int]:
        counts = {member.value: 0 for member in ModelStatus}
        for record in self.models.list():
            if record.status in counts:
                counts[record.status] += 1
        return counts

    # -- transitions -----------------------------------------------------------

    def _transition(
        self,
        record: TrainingModelRecord,
        target: ModelStatus,
        *,
        reason: str = "",
        **updates: Any,
    ) -> dict[str, Any]:
        allowed = ALLOWED_STATUS_TRANSITIONS.get(record.status, frozenset())
        if target.value not in allowed:
            return {
                "ok": False,
                "model_id": record.model_id,
                "from": record.status,
                "to": target.value,
                "reason": (
                    f"a model cannot move from {record.status!r} to {target.value!r}"
                    + (f": {reason}" if reason else "")
                ),
            }
        # The reason a status changed is the answer to "why is this model in
        # production?" and it used to be spoken in the response and dropped.
        # It is kept on the record, bounded, newest last.
        trail = tuple(record.history)
        entry = {
            "from": record.status,
            "to": target.value,
            "reason": reason,
            "at": now_iso(),
        }
        trail = (*trail, entry)[-MAX_MODEL_HISTORY:]
        updated = record.with_status(target, history=trail, **updates)
        self.models.save(updated)
        return {
            "ok": True,
            "model_id": record.model_id,
            "from": record.status,
            "to": target.value,
            "model": updated.to_dict(),
            "reason": reason,
        }

    def evaluate_now(self, model_id: str) -> tuple[TrainingModelRecord | None, str]:
        """Move a model into EVALUATING before a comparison is recorded."""
        record = self.get(model_id)
        if record is None:
            return (None, "no such model")
        if record.status == ModelStatus.REJECTED.value:
            moved = self._transition(record, ModelStatus.EXPERIMENTAL, reason="re-evaluating")
            if not moved.get("ok"):
                return (record, str(moved.get("reason", "refused")))
            record = self.get(model_id)
            if record is None:  # pragma: no cover - it was just saved
                return (None, "the model disappeared")
        if record.status == ModelStatus.EXPERIMENTAL.value:
            result = self._transition(record, ModelStatus.EVALUATING, reason="evaluation started")
            if not result.get("ok"):
                return (record, str(result.get("reason", "refused")))
            return (self.get(model_id), "")
        if record.status == ModelStatus.EVALUATING.value:
            return (record, "")
        return (record, f"a {record.status} model is not being evaluated")

    def record_evaluation(
        self, model_id: str, evaluation: TrainingEvaluation
    ) -> dict[str, Any]:
        """Attach a comparison. A regressed candidate is REJECTED automatically."""
        record = self.get(model_id)
        if record is None:
            return {"ok": False, "reason": "no such model"}
        payload = evaluation.to_dict()
        if evaluation.verdict == "regress":
            return self._transition(
                record,
                ModelStatus.REJECTED,
                reason="the candidate regressed against the base model",
                evaluation=payload,
            )
        updated = replace(
            record,
            evaluation=payload,
            updated_at=now_iso(),
        )
        self.models.save(updated)
        return {
            "ok": True,
            "model_id": model_id,
            "from": record.status,
            "to": record.status,
            "verdict": evaluation.verdict,
            "model": updated.to_dict(),
            "reason": (
                "evaluation recorded; approval is still a separate, explicit step"
                if evaluation.passed
                else "evaluation was inconclusive; approval is not available"
            ),
        }

    def approve(
        self,
        model_id: str,
        *,
        approved_by: str = "",
        note: str = "",
    ) -> dict[str, Any]:
        """Approve a model for promotion — only with a PASSING evaluation."""
        record = self.get(model_id)
        if record is None:
            return {"ok": False, "reason": "no such model"}
        verdict = _text(record.evaluation.get("verdict"))
        if verdict != "pass":
            return {
                "ok": False,
                "model_id": model_id,
                "reason": (
                    "a model cannot be approved without a passing evaluation "
                    f"(the recorded verdict is {verdict or 'none'})"
                ),
            }
        if record.status not in {ModelStatus.EVALUATING.value, ModelStatus.EXPERIMENTAL.value}:
            return {
                "ok": False,
                "model_id": model_id,
                "reason": f"a {record.status} model cannot be approved",
            }
        return self._transition(
            record,
            ModelStatus.APPROVED,
            reason=note or "approved explicitly",
            approved_by=_text(approved_by),
            approved_at=now_iso(),
        )

    def promote(self, model_id: str, *, note: str = "") -> dict[str, Any]:
        """Move an APPROVED model to PRODUCTION, recording how to roll back."""
        record = self.get(model_id)
        if record is None:
            return {"ok": False, "reason": "no such model"}
        if record.status != ModelStatus.APPROVED.value:
            return {
                "ok": False,
                "model_id": model_id,
                "reason": (
                    f"only an approved model can be promoted to production "
                    f"(this model is {record.status})"
                ),
            }
        previous = next(
            (item for item in self.models.list() if item.status == ModelStatus.PRODUCTION.value),
            None,
        )
        result = self._transition(
            record,
            ModelStatus.PRODUCTION,
            reason=note or "promoted explicitly",
            promotion={
                "promoted_at": now_iso(),
                "explicit": True,
                "previous_production": previous.model_id if previous is not None else "",
            },
            rollback={
                "restore_model_id": previous.model_id if previous is not None else "",
                "previous_status": ModelStatus.PRODUCTION.value if previous is not None else "",
                "recorded_at": now_iso(),
            },
        )
        if result.get("ok") and previous is not None:
            demoted = previous.with_status(
                ModelStatus.DEPRECATED,
                notes=(
                    previous.notes
                    or f"superseded by {record.model_id} on promotion"
                ),
            )
            self.models.save(demoted)
            result["demoted_model_id"] = previous.model_id
        return result

    def reject(self, model_id: str, *, reason: str = "") -> dict[str, Any]:
        record = self.get(model_id)
        if record is None:
            return {"ok": False, "reason": "no such model"}
        return self._transition(record, ModelStatus.REJECTED, reason=reason or "rejected")

    def deprecate(self, model_id: str, *, reason: str = "") -> dict[str, Any]:
        record = self.get(model_id)
        if record is None:
            return {"ok": False, "reason": "no such model"}
        return self._transition(record, ModelStatus.DEPRECATED, reason=reason or "deprecated")

    def rollback(self, model_id: str, *, reason: str = "") -> dict[str, Any]:
        """Undo a promotion: deprecate the current production model and restore the previous one."""
        record = self.get(model_id)
        if record is None:
            return {"ok": False, "reason": "no such model"}
        if record.status != ModelStatus.PRODUCTION.value:
            return {
                "ok": False,
                "model_id": model_id,
                "reason": (
                    "only a production model can be rolled back "
                    f"(this one is {record.status})"
                ),
            }
        restore_id = _text(record.rollback.get("restore_model_id"))
        if not restore_id:
            return {
                "ok": False,
                "model_id": model_id,
                "reason": "no previous production model is recorded for this promotion",
            }
        previous = self.get(restore_id)
        if previous is None:
            return {
                "ok": False,
                "model_id": model_id,
                "reason": f"the recorded previous model {restore_id!r} no longer exists",
            }
        demoted = self._transition(
            record,
            ModelStatus.DEPRECATED,
            reason=reason or "rolled back",
            rollback={**dict(record.rollback), "rolled_back_at": now_iso()},
        )
        restored = previous.with_status(
            ModelStatus.PRODUCTION,
            promotion={
                **dict(previous.promotion),
                "restored_at": now_iso(),
                "restored_from": record.model_id,
            },
        )
        self.models.save(restored)
        return {
            "ok": True,
            "model_id": model_id,
            "deprecated": demoted.get("model") if demoted.get("ok") else None,
            "restored_model_id": restore_id,
            "restored": restored.to_dict(),
            "reason": reason or "rolled back",
        }

    # -- reporting -------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        records = self.models.list()
        return {
            "count": len(records),
            "by_status": self.counts(),
            "production": next(
                (
                    record.to_dict()
                    for record in records
                    if record.status == ModelStatus.PRODUCTION.value
                ),
                None,
            ),
            "models": [
                {
                    "model_id": record.model_id,
                    "name": record.name,
                    "base_model": record.base_model,
                    "training_method": record.training_method,
                    "dataset_version": record.dataset_version,
                    "training_run_id": record.training_run_id,
                    "status": record.status,
                    "evaluation_verdict": _text(record.evaluation.get("verdict")),
                    "adapter": dict(record.adapter),
                    "created_at": record.created_at,
                }
                for record in records
            ],
        }


__all__ = ["SFTModelRegistry"]
