"""The Phase 31 skill service.

It owns the evidence decisions around the catalog — acquisition, validation,
adaptation, composition and execution — but owns no execution engine. Every
step is handed to the executor injected by the application, which routes it
through the existing request/planner/tool/desktop pipelines. Sensitive skill
permissions are checked with the application's ``PermissionManager`` before any
step starts; a skill never carries its own authority.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any
from uuid import uuid4

from novacontrol.reliability.permissions import PermissionManager
from novacontrol.skills.acquisition import SkillAcquisition
from novacontrol.skills.catalog import DuplicateSkillError, SkillCatalog
from novacontrol.skills.composition import SkillComposer
from novacontrol.skills.models import (
    AcquisitionResult,
    AdaptationResult,
    CompositionResult,
    Skill,
    SkillCategory,
    SkillEvidence,
    SkillExecution,
    SkillLifecycle,
    SkillMatch,
    SkillObservation,
    SkillStep,
    StepOutcome,
    unique_sorted,
    utcnow,
)
from novacontrol.skills.transfer import SkillTransfer

#: The scope a skill step runs under when it declares nothing narrower. The
#: application declares it beside the executor that performs the work.
DEFAULT_SKILL_SCOPE = "skills.execute"

StepExecutor = Callable[
    [SkillStep, Mapping[str, Any], Mapping[str, Any]],
    Awaitable[StepOutcome | None],
]
Publisher = Callable[..., None]
Persister = Callable[[], None]


class SkillService:
    def __init__(
        self,
        catalog: SkillCatalog | None = None,
        *,
        permissions: PermissionManager | None = None,
        execute_step: StepExecutor | None = None,
        persist: Persister | None = None,
        publish: Publisher | None = None,
        environment: str = "",
        available_tools: Sequence[str] | Callable[[], Sequence[str]] = (),
        learning: Any = None,
        acquisition: SkillAcquisition | None = None,
        transfer: SkillTransfer | None = None,
        composer: SkillComposer | None = None,
        max_evidence: int = 500,
        max_executions: int = 100,
    ) -> None:
        self.catalog = catalog or SkillCatalog()
        self.permissions = permissions
        self._execute_step = execute_step
        self._persist = persist
        self._publish = publish
        self.environment = str(environment or "")
        self._available_tools = available_tools
        # An empty sequence means "this caller did not declare a tool inventory",
        # not "no tools exist". Treating it as an empty inventory made every
        # skill with declared tools incompatible, including in retrieval.
        self._tools_declared = bool(available_tools)
        self.learning = learning
        self.acquisition = acquisition or SkillAcquisition()
        self.transfer = transfer or SkillTransfer()
        self.composer = composer or SkillComposer(self.transfer)
        self.evidence: list[SkillEvidence] = []
        self.executions: dict[str, SkillExecution] = {}
        self.adaptation_failures: list[dict[str, Any]] = []
        self.last_error = ""
        self.max_evidence = max(50, int(max_evidence))
        self.max_executions = max(10, int(max_executions))

    # -- configuration ---------------------------------------------------------

    def subscribe(
        self,
        *,
        execute_step: StepExecutor | None = None,
        persist: Persister | None = None,
        publish: Publisher | None = None,
    ) -> None:
        if execute_step is not None:
            self._execute_step = execute_step
        if persist is not None:
            self._persist = persist
        if publish is not None:
            self._publish = publish

    def tools(self) -> tuple[str, ...]:
        source = self._available_tools
        if callable(source):
            try:
                return tuple(str(item) for item in source())
            except Exception:
                return ()
        return tuple(str(item) for item in source)

    def _tools_for_check(self) -> tuple[str, ...] | None:
        """``None`` means unknown; an empty tuple means a declared empty inventory."""
        return self.tools() if self._tools_declared else None

    # -- acquisition -----------------------------------------------------------

    def acquire(
        self,
        goal: str,
        observations: Sequence[SkillObservation],
        *,
        name: str = "",
        description: str = "",
        category: SkillCategory | None = None,
        demonstrated: bool = False,
        requested_by: str = "operator",
        origin_agent: str = "",
    ) -> AcquisitionResult:
        result = self.acquisition.acquire(
            goal,
            observations,
            name=name,
            description=description,
            category=category,
            demonstrated=demonstrated,
            requested_by=requested_by,
        )
        if not result.created or result.skill is None:
            return result
        skill = result.skill
        if origin_agent:
            skill = skill.with_(
                provenance=unique_sorted(
                    list(skill.provenance) + [f"agent:{origin_agent}"]
                )
            )
        try:
            self.catalog.add(skill, activate=False)
        except DuplicateSkillError as exc:
            return AcquisitionResult(
                created=False,
                support=result.support,
                rejected=True,
                reasons=(str(exc),),
            )
        for row in self.acquisition.evidence_for(skill, observations):
            self._append_evidence(row)
        self._emit(
            "skills.skill_acquired",
            skill_id=skill.id,
            name=skill.name,
            support=result.support,
        )
        self._save()
        return AcquisitionResult(
            created=True,
            skill=skill,
            support=result.support,
            reasons=result.reasons,
        )

    def observe(
        self,
        goal: str,
        steps: Sequence[SkillStep | Mapping[str, Any]],
        *,
        verified: bool,
        inputs: Mapping[str, Any] | None = None,
        environment: str = "",
        tools: Sequence[str] = (),
        duration_ms: float = 0.0,
        source: str = "execution",
        agent: str = "",
    ) -> SkillObservation:
        parsed = tuple(
            step
            if isinstance(step, SkillStep)
            else SkillStep.from_dict(step)
            for step in steps
        )
        clean = tuple(step for step in parsed if step is not None)
        return SkillObservation(
            goal=goal,
            steps=clean,
            verified=bool(verified),
            environment=environment or self.environment,
            inputs=dict(inputs or {}),
            tools=tuple(str(tool) for tool in tools),
            duration_ms=max(0.0, float(duration_ms)),
            source=source,
            agent=agent,
        )

    # -- retrieval -------------------------------------------------------------

    def retrieve(
        self,
        goal: str,
        *,
        environment: str = "",
        inputs: Mapping[str, Any] | None = None,
        limit: int = 8,
        include_incompatible: bool = False,
    ) -> tuple[SkillMatch, ...]:
        hits = self.catalog.retrieve(
            goal,
            environment=environment or self.environment,
            inputs=inputs,
            available_tools=self._tools_for_check(),
            limit=limit,
            include_incompatible=include_incompatible,
        )
        recommendation = self._learning_recommendation(goal)
        if recommendation is None or not bool(
            getattr(recommendation, "compatible", True)
        ):
            return hits
        source = str(getattr(recommendation, "source", "") or "")
        strategy = str(getattr(recommendation, "strategy_id", "") or "")
        evidence = tuple(
            str(item) for item in (getattr(recommendation, "evidence_ids", ()) or ())
        )[:3]
        prefer = {
            str(tool)
            for tool in (getattr(recommendation, "prefer_tools", ()) or ())
        }
        avoid = {
            str(tool)
            for tool in (getattr(recommendation, "avoid_tools", ()) or ())
        }
        annotated: list[SkillMatch] = []
        for hit in hits:
            reasons = list(hit.reasons)
            score = float(hit.score)
            if source and source != "none":
                detail = f"continual learning: {source} recommendation"
                if strategy:
                    detail += f" ({strategy})"
                if evidence:
                    detail += f", evidence {', '.join(evidence)}"
                reasons.append(detail)
            try:
                skill = self.catalog.get(hit.skill_id, hit.version)
            except KeyError:
                skill = None
            if skill is not None and skill.tools:
                matched = prefer & set(skill.tools)
                if matched:
                    score += 0.08
                    reasons.append(
                        "continual evidence prefers: " + ", ".join(sorted(matched))
                    )
                discouraged = avoid & set(skill.tools)
                if discouraged:
                    score -= 0.3
                    reasons.append(
                        "continual evidence discourages: "
                        + ", ".join(sorted(discouraged))
                    )
            annotated.append(
                hit.with_(score=round(score, 4), reasons=tuple(reasons))
            )
        return tuple(sorted(annotated, key=lambda row: row.score, reverse=True))

    # -- lifecycle -------------------------------------------------------------

    def validate(self, skill_id: str, version: int | None = None) -> Skill:
        skill = self.catalog.get(skill_id, version)
        if skill.lifecycle in (SkillLifecycle.DEPRECATED, SkillLifecycle.FAILED):
            raise ValueError(
                f"{skill.name!r} v{skill.version} is {skill.lifecycle.value}; a "
                "deprecated or failed version cannot be silently restored by "
                "validation. Create and validate a new version instead."
            )
        rows = [
            row
            for row in self.evidence
            if row.skill_id == skill.id and row.version == skill.version
        ]
        successes = sum(1 for row in rows if row.verified)
        failures = sum(1 for row in rows if not row.verified)
        required = self.acquisition.min_support
        if successes < required:
            raise ValueError(
                f"{skill.name!r} v{skill.version} has {successes} verified "
                f"execution(s); {required} are required before validation."
            )
        if failures > successes:
            raise ValueError(
                "failure evidence outweighs success: the skill is not validated."
            )
        updated = skill.with_(
            lifecycle=SkillLifecycle.VALIDATED,
            validation=(
                *skill.validation,
                f"validated on {successes} verified execution(s), "
                f"{failures} failure(s)",
            ),
            updated_at=utcnow(),
            metadata={
                **dict(skill.metadata),
                "validated_at": utcnow().isoformat(),
                "validation_successes": successes,
                "validation_failures": failures,
            },
        )
        self.catalog.update(updated)
        self._emit(
            "skills.skill_validated",
            skill_id=skill.id,
            version=skill.version,
            successes=successes,
        )
        self._save()
        return updated

    def deprecate(
        self, skill_id: str, version: int | None = None, *, reason: str = ""
    ) -> Skill:
        skill = self.catalog.deprecate(skill_id, version, reason=reason)
        self._emit("skills.skill_deprecated", skill_id=skill.id, version=skill.version)
        self._save()
        return skill

    def rollback(self, skill_id: str, version: int) -> Skill:
        skill = self.catalog.rollback(skill_id, version)
        self._emit("skills.skill_rolled_back", skill_id=skill.id, version=version)
        self._save()
        return skill

    def promote(self, skill_id: str, version: int | None = None) -> Skill:
        """Make a trusted, independently validated version the active one."""
        skill = self.catalog.promote(skill_id, version)
        self._emit("skills.skill_promoted", skill_id=skill.id, version=skill.version)
        self._save()
        return skill

    # -- adaptation ------------------------------------------------------------

    def adapt(
        self,
        skill_id: str,
        *,
        goal: str,
        inputs: Mapping[str, Any] | None = None,
        environment: str = "",
        target_map: Mapping[str, str] | None = None,
        requested_by: str = "operator",
        version: int | None = None,
    ) -> AdaptationResult:
        base = self.catalog.get(skill_id, version)
        if base.lifecycle is not SkillLifecycle.VALIDATED:
            reason = (
                f"{base.name!r} v{base.version} is {base.lifecycle.value}; "
                "only a validated skill can be adapted"
            )
            self.adaptation_failures.append(
                {
                    "skill_id": base.id,
                    "source_version": base.version,
                    "goal": goal,
                    "environment": environment,
                    "reasons": [reason],
                    "missing": [],
                    "at": utcnow().isoformat(),
                }
            )
            self._trim()
            self._save()
            return AdaptationResult(
                source_skill_id=base.id,
                source_version=base.version,
                compatible=False,
                reasons=(reason,),
            )
        next_version = max(item.version for item in self.catalog.versions(skill_id)) + 1
        result = self.transfer.adapt(
            base,
            version=next_version,
            goal=goal,
            inputs=inputs,
            environment=environment or self.environment,
            available_tools=self._tools_for_check(),
            target_map=target_map,
            requested_by=requested_by,
        )
        if not result.compatible or result.skill is None:
            self.adaptation_failures.append(
                {
                    "skill_id": base.id,
                    "source_version": base.version,
                    "goal": goal,
                    "environment": environment,
                    "reasons": list(result.reasons),
                    "missing": list(result.missing_capabilities),
                    "at": utcnow().isoformat(),
                }
            )
            self._trim()
            self._save()
            return result
        self.catalog.add(result.skill, activate=False, allow_duplicate=True)
        self._emit(
            "skills.skill_adapted",
            skill_id=result.skill.id,
            source_version=result.source_version,
            version=result.skill.version,
        )
        self._save()
        return result

    # -- composition -----------------------------------------------------------

    def compose(
        self,
        objective: str,
        skill_ids: Sequence[str | Skill],
        *,
        inputs_by_skill: Mapping[str, Mapping[str, Any]] | None = None,
        environment: str = "",
        versions: Mapping[str, int] | None = None,
    ) -> CompositionResult:
        skills: list[Skill] = []
        for item in skill_ids:
            if isinstance(item, Skill):
                skills.append(item)
            else:
                skills.append(
                    self.catalog.get(str(item), (versions or {}).get(str(item)))
                )
        return self.composer.compose(
            objective,
            skills,
            inputs_by_skill=inputs_by_skill,
            environment=environment or self.environment,
            available_tools=self._tools_for_check(),
        )

    # -- execution -------------------------------------------------------------

    async def execute(
        self,
        skill_id: str,
        *,
        inputs: Mapping[str, Any] | None = None,
        goal: str = "",
        approved: bool = False,
        version: int | None = None,
        environment: str = "",
        execution_id: str = "",
        resume: bool = False,
        transfer: bool = False,
    ) -> dict[str, Any]:
        try:
            skill = self.catalog.get(skill_id, version)
        except KeyError as exc:
            return {"status": "failed", "error": str(exc), "skill_id": skill_id}
        if not skill.executable:
            return {
                "status": "refused",
                "reason": (
                    f"{skill.name!r} v{skill.version} is {skill.lifecycle.value}; "
                    "only validated skills or evaluated adaptations may execute"
                ),
                "skill_id": skill.id,
                "version": skill.version,
            }
        # ``example`` documents an input; it is not a default. Binding examples
        # let a required input appear supplied and executed a stale value from
        # the acquisition run.
        bound = dict(inputs or {})
        env = environment or self.environment
        compatibility = self.transfer.compatibility(
            skill,
            environment=env,
            inputs=bound,
            available_tools=self._tools_for_check(),
        )
        if not compatibility.compatible:
            self._record_transfer_failure(skill, goal, env, compatibility.reasons)
            return {
                "status": "blocked",
                "reason": "; ".join(compatibility.reasons),
                "missing_inputs": list(compatibility.missing_inputs),
                "missing_capabilities": list(compatibility.missing_capabilities),
                "skill_id": skill.id,
                "version": skill.version,
            }
        if env and env.strip().lower() != "any":
            for index, step in enumerate(skill.steps):
                declared = step.environment.strip().lower()
                if declared and declared not in ("any", env.strip().lower()):
                    reason = (
                        f"step {index + 1} is declared for {step.environment!r}, "
                        f"not {env!r}"
                    )
                    self._record_transfer_failure(skill, goal, env, (reason,))
                    return {
                        "status": "blocked",
                        "reason": reason,
                        "skill_id": skill.id,
                        "version": skill.version,
                    }
        if self.permissions is None:
            return {
                "status": "blocked",
                "reason": "no permission layer is wired, so no skill step is started",
                "skill_id": skill.id,
                "version": skill.version,
            }
        scopes = skill.permissions or (DEFAULT_SKILL_SCOPE,)
        for scope in scopes:
            decision = self.permissions.check(action=scope, approved=approved)
            if not decision.allow:
                if decision.requires_confirmation and not approved:
                    return {
                        "status": "waiting_approval",
                        "reason": decision.reason,
                        "scope": scope,
                        "skill_id": skill.id,
                        "version": skill.version,
                    }
                return {
                    "status": "blocked",
                    "reason": decision.reason,
                    "scope": scope,
                    "skill_id": skill.id,
                    "version": skill.version,
                }
        if self._execute_step is None:
            return {
                "status": "blocked",
                "reason": "no skill step executor is wired, so nothing was started",
                "skill_id": skill.id,
                "version": skill.version,
            }

        existing = self.executions.get(execution_id) if execution_id else None
        if existing is not None and existing.status == "completed":
            return existing.to_dict()
        completed = set(existing.completed_steps) if (resume and existing) else set()
        results: list[dict[str, Any]] = (
            [
                dict(row)
                for row in existing.results
                if int(row.get("index", -1)) in completed
            ]
            if (resume and existing)
            else []
        )
        outputs: dict[str, Any] = (
            dict(existing.outputs) if (resume and existing) else {}
        )
        declared_outputs = set(skill.outputs)
        started = time.perf_counter()
        execution = SkillExecution(
            id=existing.id if existing else (execution_id or uuid4().hex),
            skill_id=skill.id,
            version=skill.version,
            goal=goal or skill.objective,
            inputs=bound,
            environment=env,
            status="running",
            completed_steps=tuple(sorted(completed)),
            results=tuple(results),
            outputs=outputs,
            adaptation=bool(transfer or skill.lifecycle is SkillLifecycle.ADAPTED),
        )
        error = ""
        for index, step in enumerate(skill.steps):
            if index in completed:
                continue
            outcome = await self._run_step(
                step,
                bound,
                {
                    "goal": goal or skill.objective,
                    "environment": env,
                    "skill_id": skill.id,
                    "version": skill.version,
                    "step_index": index,
                    # The step router may need this to mint the SAME single-use
                    # device plan token a typed command uses; the service never
                    # treats its own approval as a substitute for that gate.
                    "approved": bool(approved),
                },
                approved=approved,
            )
            if outcome is None:
                error = (
                    f"step {index + 1} requires approval before it can run"
                )
                execution = execution.with_(
                    status="waiting_approval", error=error
                )
                break
            raw_data = getattr(outcome, "data", {})
            step_outputs = {
                key: value
                for key, value in (
                    dict(raw_data) if isinstance(raw_data, Mapping) else {}
                ).items()
                if key in declared_outputs
            }
            results.append(
                {
                    "index": index,
                    "instruction": step.instruction,
                    "kind": step.kind.value,
                    "verified": outcome.verified,
                    "detail": outcome.detail,
                    "error": outcome.error,
                    "evidence": list(outcome.evidence),
                    **(
                        {"outputs": step_outputs}
                        if step_outputs
                        else {}
                    ),
                }
            )
            if outcome.verified:
                completed.add(index)
                outputs.update(step_outputs)
            elif not step.optional:
                error = outcome.error or f"step {index + 1} did not verify"
                break
            execution = execution.with_(
                completed_steps=tuple(sorted(completed)),
                results=tuple(results),
                outputs=outputs,
            )
        required_steps = {
            index for index, step in enumerate(skill.steps) if not step.optional
        }
        verified = not error and required_steps <= completed
        duration_ms = (time.perf_counter() - started) * 1000.0
        execution = execution.with_(
            completed_steps=tuple(sorted(completed)),
            results=tuple(results),
            outputs=outputs,
            status="completed" if verified else ("waiting_approval" if execution.status == "waiting_approval" else "failed"),
            verified=verified,
            error=error,
            finished_at=utcnow() if execution.status != "waiting_approval" else None,
        )
        if execution.status != "waiting_approval":
            self._record_execution(execution, duration_ms, transfer=bool(transfer))
        self.executions[execution.id] = execution
        self._trim()
        self._save()
        self._emit(
            "skills.skill_executed",
            skill_id=skill.id,
            version=skill.version,
            execution_id=execution.id,
            verified=verified,
            status=execution.status,
        )
        return execution.to_dict()

    async def resume(
        self,
        execution_id: str,
        *,
        approved: bool = False,
    ) -> dict[str, Any]:
        execution = self.executions.get(str(execution_id))
        if execution is None:
            return {"status": "failed", "error": "execution not found"}
        if execution.status == "completed":
            return execution.to_dict()
        return await self.execute(
            execution.skill_id,
            inputs=execution.inputs,
            goal=execution.goal,
            approved=approved,
            version=execution.version,
            environment=execution.environment,
            execution_id=execution.id,
            resume=True,
            transfer=execution.adaptation,
        )

    async def compose_and_execute(
        self,
        objective: str,
        skill_ids: Sequence[str],
        *,
        inputs_by_skill: Mapping[str, Mapping[str, Any]] | None = None,
        environment: str = "",
        approved: bool = False,
    ) -> dict[str, Any]:
        composition = self.compose(
            objective,
            skill_ids,
            inputs_by_skill=inputs_by_skill,
            environment=environment,
        )
        if not composition.compatible:
            return {
                "status": "blocked",
                "composition": composition.to_dict(),
                "results": [],
            }
        results: list[dict[str, Any]] = []
        produced: dict[str, Any] = {}
        for row in composition.plan:
            supplied = dict(row.get("inputs") or {})
            # Bind a prior skill's DECLARED outputs only where the caller did
            # not supply a value. Composition already verified that these
            # inputs are covered by a producer; execution must carry them or
            # the plan would be considered compatible and then block.
            try:
                target = self.catalog.get(
                    str(row["skill_id"]), int(row.get("version", 1))
                )
                wanted = set(target.input_names)
            except KeyError:
                wanted = set()
            for name, value in produced.items():
                if name in wanted and name not in supplied:
                    supplied[name] = value
            result = await self.execute(
                str(row["skill_id"]),
                inputs=supplied,
                goal=objective,
                approved=approved,
                version=int(row.get("version", 1)),
                environment=environment,
            )
            results.append(result)
            if result.get("status") != "completed":
                return {
                    "status": "failed" if result.get("status") != "waiting_approval" else "waiting_approval",
                    "composition": composition.to_dict(),
                    "results": results,
                    "outputs": produced,
                    "error": result.get("reason") or result.get("error", ""),
                }
            produced.update(
                {
                    key: value
                    for key, value in dict(result.get("outputs") or {}).items()
                    if key in set(row.get("outputs") or ())
                }
            )
        return {
            "status": "completed",
            "verified": all(bool(item.get("verified")) for item in results),
            "composition": composition.to_dict(),
            "results": results,
            "outputs": produced,
        }

    # -- reading ---------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        stats = self.catalog.stats()
        return {
            **stats,
            "evidence": len(self.evidence),
            "executions": len(self.executions),
            "adaptation_failures": len(self.adaptation_failures),
            "environment": self.environment,
            "available_tools": list(self.tools()),
            "default_scope": DEFAULT_SKILL_SCOPE,
            "skill_count": int(stats.get("skills", 0)),
            "skills": [
                {
                    "id": skill.id,
                    "name": skill.name,
                    "version": skill.version,
                    "lifecycle": skill.lifecycle.value,
                    "category": skill.category.value,
                    "success_rate": skill.reliability.success_rate,
                    "trials": skill.reliability.trials,
                }
                for skill in self.catalog.all()
            ],
            "last_error": self.last_error,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "catalog": self.catalog.to_dict(),
            "evidence": [row.to_dict() for row in self.evidence],
            "executions": [row.to_dict() for row in self.executions.values()],
            "adaptation_failures": [dict(row) for row in self.adaptation_failures],
        }

    @classmethod
    def from_dict(
        cls, payload: Mapping[str, Any] | None, **kwargs: Any
    ) -> SkillService:
        data = payload if isinstance(payload, Mapping) else {}
        service = cls(
            SkillCatalog.from_dict(
                data.get("catalog") if isinstance(data.get("catalog"), Mapping) else None
            ),
            **kwargs,
        )
        for item in data.get("evidence", ()) or ():
            evidence_row = (
                SkillEvidence.from_dict(item) if isinstance(item, Mapping) else None
            )
            if evidence_row is not None:
                service.evidence.append(evidence_row)
        for item in data.get("executions", ()) or ():
            execution_row = (
                SkillExecution.from_dict(item) if isinstance(item, Mapping) else None
            )
            if execution_row is not None:
                service.executions[execution_row.id] = execution_row
        for item in data.get("adaptation_failures", ()) or ():
            if isinstance(item, Mapping):
                service.adaptation_failures.append(dict(item))
        return service

    # -- internals -------------------------------------------------------------

    async def _run_step(
        self,
        step: SkillStep,
        inputs: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        approved: bool,
    ) -> StepOutcome | None:
        if self.permissions is not None and step.permission:
            decision = self.permissions.check(
                action=step.permission, approved=approved
            )
            if not decision.allow:
                if decision.requires_confirmation and not approved:
                    return None
                return StepOutcome(
                    verified=False, error=decision.reason
                )
        assert self._execute_step is not None
        try:
            return await self._execute_step(step, inputs, context)
        except Exception as exc:  # a raising step is a failed, unverified step
            return StepOutcome(
                verified=False, error=f"{type(exc).__name__}: {exc}"
            )

    def _record_execution(
        self,
        execution: SkillExecution,
        duration_ms: float,
        *,
        transfer: bool,
    ) -> None:
        if not execution.verified and execution.status == "waiting_approval":
            return
        row = SkillEvidence(
            skill_id=execution.skill_id,
            version=execution.version,
            goal=execution.goal,
            verified=execution.verified,
            environment=execution.environment,
            inputs=dict(execution.inputs),
            steps=execution.results,
            duration_ms=duration_ms,
            error=execution.error,
            source="transfer" if transfer else "execution",
        )
        self._append_evidence(row)
        try:
            skill = self.catalog.get(execution.skill_id, execution.version)
        except KeyError:
            return
        updated = skill.with_(
            reliability=skill.reliability.record(
                verified=execution.verified,
                duration_ms=duration_ms,
                transfer=transfer,
            ),
            updated_at=utcnow(),
        )
        self.catalog.update(updated)

    def _append_evidence(self, row: SkillEvidence) -> None:
        self.evidence.append(row)
        if len(self.evidence) > self.max_evidence:
            del self.evidence[: len(self.evidence) - self.max_evidence]

    def _record_transfer_failure(
        self,
        skill: Skill,
        goal: str,
        environment: str,
        reasons: Sequence[str],
    ) -> None:
        self.adaptation_failures.append(
            {
                "skill_id": skill.id,
                "source_version": skill.version,
                "goal": goal,
                "environment": environment,
                "reasons": list(reasons),
                "at": utcnow().isoformat(),
            }
        )
        self._trim()
        self._save()

    def _learning_recommendation(self, goal: str) -> Any:
        if self.learning is None:
            return None
        try:
            return self.learning.recommend(goal)
        except Exception:
            return None

    def _learning_suggestion(self, goal: str) -> str:
        suggestion = self._learning_recommendation(goal)
        if suggestion is None:
            return ""
        source = str(getattr(suggestion, "source", "") or "")
        return f"{source} recommendation" if source and source != "none" else ""

    def _trim(self) -> None:
        if len(self.adaptation_failures) > self.max_evidence:
            del self.adaptation_failures[: len(self.adaptation_failures) - self.max_evidence]
        if len(self.executions) > self.max_executions:
            keep = sorted(
                self.executions.values(), key=lambda item: item.started_at
            )[-self.max_executions :]
            self.executions = {item.id: item for item in keep}

    def _save(self) -> None:
        if self._persist is None:
            return
        try:
            self._persist()
            self.last_error = ""
        except Exception as exc:
            # Execution already happened; raising here would turn a persistence
            # failure into a fabricated execution failure. Surface it instead.
            self.last_error = f"persistence failed: {type(exc).__name__}: {exc}"

    def _emit(self, type_: str, **payload: Any) -> None:
        if self._publish is None:
            return
        try:
            self._publish(type_, **payload)
        except Exception:
            return
