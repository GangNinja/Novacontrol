"""Independent verification specialist (Phase 28).

The collaboration layer may not treat an agent's claim of success as verified
success. This agent is the independent side of that rule: it recomputes dataset
metrics from the CSV, checks that every evidence claim resolves to a real
source, checks file hashes, or runs the project's actual test suite through the
existing command runner.

Nothing here trusts the upstream agent's verdict. A verifier subtask can fail;
when it does, the collaboration is reported unverified.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from novacontrol.agents.developer import (
    SubprocessRunner,
    parse_test_output,
    recognise_test_command,
)
from novacontrol.agents.models import AgentRole, AgentTask
from novacontrol.agents.pipeline import (
    AgentRun,
    PipelineStep,
    SpecialistAgent,
    SpecialistDecision,
    SpecialistPipeline,
    StepOutcome,
    StepStatus,
)
from novacontrol.agentcore.verifier import VerificationResult, VerificationStatus
from novacontrol.core.security import PermissionScope, RiskLevel
from novacontrol.reliability.permissions import PermissionDeclaration

__all__ = ["VerificationAgent"]

_MAX_CLAIMS = 200
_MAX_BYTES = 20 * 1024 * 1024


class VerificationAgent(SpecialistAgent):
    """Recomputes/verifies the work another agent reported, from the evidence."""

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        pipeline: SpecialistPipeline | None = None,
        runner: SubprocessRunner | None = None,
        default_timeout: float = 120.0,
    ) -> None:
        super().__init__(
            "verification-agent",
            AgentRole.VERIFICATION,
            "Independently checks claims, recomputes reported dataset metrics, checks "
            "file hashes and runs the project's real test suite.",
            pipeline=pipeline,
        )
        self.root = Path(root or Path.cwd()).resolve()
        self.runner = runner or SubprocessRunner()
        self.default_timeout = max(1.0, float(default_timeout))

    def clone(self, pipeline: SpecialistPipeline) -> "VerificationAgent":
        return VerificationAgent(
            root=self.root,
            pipeline=pipeline,
            runner=self.runner,
            default_timeout=self.default_timeout,
        )

    def availability(self) -> Mapping[str, Any]:
        try:
            available = self.root.is_dir()
        except OSError:
            available = False
        return {
            "available": available,
            "reason": "" if available else f"verification root is not a directory: {self.root}",
        }

    def declarations(self) -> Mapping[str, PermissionDeclaration]:
        read = PermissionDeclaration(
            risk_level=RiskLevel.LOW, required_permission=PermissionScope.FILESYSTEM_READ
        )
        return {
            "verification.verify_claims": read,
            "verification.verify_metrics": read,
            "verification.verify_files": read,
            "verification.run_tests": PermissionDeclaration(
                risk_level=RiskLevel.HIGH,
                required_permission=PermissionScope.SHELL_EXECUTE,
                reversible=False,
                external_side_effect=False,
            ),
        }

    # -- decision and plan ---------------------------------------------------

    def decide(
        self, task: AgentTask, interpretation: Any, context: Mapping[str, Any]
    ) -> SpecialistDecision:
        kind = self._kind(task)
        return SpecialistDecision(
            action=kind,
            reason=f"independently {kind.replace('_', ' ')} from the supplied evidence",
            confidence=0.9,
            data={"kind": kind},
        )

    def plan(
        self,
        task: AgentTask,
        interpretation: Any,
        decision: SpecialistDecision,
        context: Mapping[str, Any],
    ) -> tuple[PipelineStep, ...]:
        kind = str(decision.action)
        if kind == "run_tests":
            command = recognise_test_command(self.root)
            if command is None:
                return (
                    PipelineStep(
                        action="verify_tests",
                        description=f"Report the unrecognised test runner for: {task.goal}",
                        tool="verification.verify_files",
                        target=str(self.root),
                        expected="The missing test runner is named rather than guessed",
                        permission=PermissionScope.FILESYSTEM_READ,
                    ),
                )
            return (
                PipelineStep(
                    action="run_tests",
                    description="Run the project's own test suite",
                    tool="verification.run_tests",
                    target=" ".join(command.argv),
                    expected="A test run with a recognised tally and no failures",
                    permission=PermissionScope.SHELL_EXECUTE,
                    risk=RiskLevel.HIGH,
                    requires_approval=True,
                    metadata={"command": list(command.argv)},
                ),
            )
        tool = {
            "verify_claims": "verification.verify_claims",
            "verify_metrics": "verification.verify_metrics",
            "verify_files": "verification.verify_files",
        }.get(kind, "verification.verify_claims")
        return (
            PipelineStep(
                action=kind,
                description=f"Independently {kind.replace('verify_', 'verify ').replace('_', ' ')}",
                tool=tool,
                target=str(self.root),
                expected=(
                    "A concrete pass/fail verdict produced by recomputation, with the "
                    "evidence that decided it"
                ),
                permission=PermissionScope.FILESYSTEM_READ,
            ),
        )

    # -- execution -----------------------------------------------------------

    async def execute(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        if step.action == "verify_tests":
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.FAILED,
                detail="no test runner was recognised in this project",
                tool=step.tool,
            )
        if step.action == "run_tests":
            return await self._do_run_tests(step, run)
        if step.action == "verify_metrics":
            return await asyncio.to_thread(self._do_verify_metrics, step, run)
        if step.action == "verify_files":
            return await asyncio.to_thread(self._do_verify_files, step, run)
        return await asyncio.to_thread(self._do_verify_claims, step, run)

    def verify(
        self, step: PipelineStep, outcome: StepOutcome, run: AgentRun
    ) -> VerificationResult:
        if not outcome.ok:
            return VerificationResult(
                status=VerificationStatus.FAIL,
                expectation=step.expected or "the verification ran",
                reason=outcome.detail or "the verification did not run",
                evidence={},
                confidence=0.9,
            )
        passed = bool(outcome.output.get("passed", False))
        checked = int(outcome.output.get("checked", 0) or 0)
        if checked <= 0:
            return VerificationResult(
                status=VerificationStatus.FAIL,
                expectation=step.expected or "at least one claim was checked",
                reason="the verifier had nothing to check, which is not a pass",
                evidence=dict(outcome.output),
                confidence=0.9,
            )
        return VerificationResult(
            status=VerificationStatus.PASS if passed else VerificationStatus.FAIL,
            expectation=step.expected or "the reported work checks out",
            reason=str(
                outcome.output.get("reason")
                or ("all checks passed" if passed else "one or more checks failed")
            ),
            evidence=dict(outcome.output),
            confidence=0.95 if passed else 0.9,
        )

    def report(self, run: AgentRun) -> dict[str, Any]:
        for outcome in reversed(run.outcomes):
            if outcome.ok and outcome.action in {
                "verify_claims",
                "verify_metrics",
                "verify_files",
                "run_tests",
            }:
                return {
                    **dict(outcome.output),
                    "verification_kind": outcome.action,
                    "verified": run.verified,
                }
        return {"verification_kind": "", "verified": run.verified, "passed": False}

    def summarize(self, run: AgentRun) -> str:
        report = self.report(run)
        if not report.get("verification_kind"):
            return super().summarize(run)
        verdict = "passed" if report.get("passed") else "failed"
        return (
            f"Independent verification {verdict}: {report.get('reason', 'no reason recorded')}"
        )

    # -- checks --------------------------------------------------------------

    def _kind(self, task: AgentTask) -> str:
        requested = str(task.metadata.get("verification", "")).strip().lower()
        if requested in {"claims", "metrics", "files", "tests"}:
            return f"verify_{requested}" if requested != "tests" else "run_tests"
        text = task.goal.lower()
        if re.search(r"\b(?:test|tests|pytest|suite)\b", text):
            return "run_tests"
        if re.search(r"\b(?:metric|statistic|mean|average|column)\b", text):
            return "verify_metrics"
        if re.search(r"\b(?:file|hash|sha256)\b", text):
            return "verify_files"
        return "verify_claims"

    def _inputs(self, metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
        context = metadata.get("context")
        context = context if isinstance(context, Mapping) else {}
        upstream = context.get("upstream") or context.get("verification_input")
        if isinstance(upstream, Mapping):
            views = [view for view in upstream.values() if isinstance(view, Mapping)]
            if views:
                return [dict(view) for view in views]
        claims = metadata.get("claims")
        if isinstance(claims, Sequence) and not isinstance(claims, str):
            return [{"claims": list(claims), "evidence": {}}]
        return []

    @staticmethod
    def _metadata(run: AgentRun) -> dict[str, Any]:
        metadata = run.context.get("task_metadata")
        return dict(metadata) if isinstance(metadata, Mapping) else {}

    def _claim_rows(self, views: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        claims: list[dict[str, Any]] = []
        for view in views:
            for claim in view.get("claims", ()):
                if isinstance(claim, Mapping):
                    claims.append(dict(claim))
            evidence = view.get("evidence")
            if isinstance(evidence, Mapping):
                for claim in evidence.get("claims", ()):
                    if isinstance(claim, Mapping) and dict(claim) not in claims:
                        claims.append(dict(claim))
        return claims[:_MAX_CLAIMS]

    def _do_verify_claims(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        views = self._inputs(self._metadata(run))
        claims = self._claim_rows(views)
        sources: dict[int, str] = {}
        unresolved: list[dict[str, Any]] = []
        for view in views:
            evidence = view.get("evidence")
            if isinstance(evidence, Mapping):
                for source in evidence.get("sources", ()):
                    if isinstance(source, Mapping):
                        try:
                            sources[int(source.get("index", 0))] = str(
                                source.get("url") or source.get("title") or ""
                            )
                        except (TypeError, ValueError):
                            continue
                for conflict in evidence.get("conflicts", ()):
                    if isinstance(conflict, Mapping):
                        unresolved.append(dict(conflict))
        problems: list[str] = []
        evidence_claims = 0
        for claim in claims:
            kind = str(claim.get("kind", "evidence")).lower()
            citations = claim.get("citations", claim.get("citation", ()))
            if isinstance(citations, int):
                citations = [citations]
            citations = [int(item) for item in citations or () if str(item).isdigit()]
            if kind == "evidence":
                evidence_claims += 1
                if not citations:
                    problems.append(
                        f"evidence claim has no citation: {str(claim.get('text', ''))[:120]}"
                    )
                elif any(index not in sources for index in citations):
                    problems.append(
                        f"citation does not resolve to a source: {citations}"
                    )
            else:
                if not claim.get("basis") and not claim.get("citations"):
                    problems.append(
                        f"inference claim has no stated basis: {str(claim.get('text', ''))[:120]}"
                    )
        if unresolved:
            problems.append(f"{len(unresolved)} conflict(s) were reported by the research agent")
        checked = len(claims)
        passed = bool(checked and not problems)
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED,
            detail=(
                f"{checked} claim(s) checked; {len(problems)} problem(s)"
                if checked
                else "no claims were supplied to verify"
            ),
            output={
                "passed": passed,
                "checked": checked,
                "evidence_claims": evidence_claims,
                "sources": len(sources),
                "problems": problems[:20],
                "conflicts": unresolved[:20],
                "reason": (
                    f"{checked} claim(s) resolved against {len(sources)} source(s)"
                    if passed
                    else "; ".join(problems[:3])
                    or "no claims were supplied to verify"
                ),
            },
            tool=step.tool,
        )

    def _do_verify_metrics(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        metadata = self._metadata(run)
        views = self._inputs(metadata)
        claims = self._claim_rows(views)
        context = metadata.get("context")
        context = context if isinstance(context, Mapping) else {}
        default_path = str(context.get("dataset_path") or context.get("path") or "")
        checks: dict[str, dict[str, Any]] = {}
        problems: list[str] = []
        for claim in claims:
            topic = str(claim.get("topic", ""))
            match = re.match(r"column:([^:]+):([a-z_]+)$", topic)
            if not match:
                continue
            column, metric = match.group(1), match.group(2)
            expected = _number(claim.get("value"))
            path = default_path
            for view in views:
                evidence = view.get("evidence")
                if isinstance(evidence, Mapping) and evidence.get("path"):
                    path = str(evidence["path"])
                    break
            if expected is None or not path:
                problems.append(f"{topic}: no numeric expectation or dataset path")
                continue
            try:
                actual = self._compute_metric(path, column, metric)
            except (OSError, ValueError, csv.Error) as exc:
                problems.append(f"{topic}: {type(exc).__name__}: {exc}")
                checks[topic] = {
                    "expected": expected,
                    "actual": None,
                    "ok": False,
                    "error": str(exc),
                }
                continue
            if actual is None:
                problems.append(f"{topic}: the column/metric could not be recomputed")
                checks[topic] = {"expected": expected, "actual": None, "ok": False}
                continue
            ok = abs(actual - expected) <= max(1e-9, abs(expected) * 1e-6)
            checks[topic] = {
                "expected": expected,
                "actual": actual,
                "ok": ok,
                "column": column,
                "metric": metric,
            }
            if not ok:
                problems.append(
                    f"{topic}: reported {expected}, recomputed {actual}"
                )
        checked = len(checks)
        passed = bool(checked and not problems)
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED,
            detail=f"{checked} metric(s) recomputed; {len(problems)} mismatch(es)",
            output={
                "passed": passed,
                "checked": checked,
                "checks": checks,
                "metrics": {
                    topic: check["actual"]
                    for topic, check in checks.items()
                    if check.get("actual") is not None
                },
                "problems": problems[:20],
                "reason": (
                    f"{checked} metric(s) matched independent recomputation"
                    if passed
                    else "; ".join(problems[:3])
                    or "no metrics were supplied to verify"
                ),
                "dataset_path": default_path,
            },
            tool=step.tool,
        )

    def _compute_metric(self, raw_path: str, column: str, metric: str) -> float | None:
        path = Path(raw_path).expanduser()
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError("the dataset is outside this verifier's root")
        if path.stat().st_size > _MAX_BYTES:
            raise ValueError("the dataset is larger than the read ceiling")
        numeric: list[float] = []
        raw_values: list[str] = []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if column not in (reader.fieldnames or ()):
                raise ValueError(f"column {column!r} is not present")
            for row in reader:
                raw = str(row.get(column, "")).strip()
                raw_values.append(raw)
                number = _number(raw)
                if number is not None:
                    numeric.append(number)
        if metric == "unique":
            return float(len({value for value in raw_values if value}))
        if not numeric:
            return None
        if metric == "count":
            return float(len(numeric))
        if metric == "sum":
            return float(sum(numeric))
        if metric == "min":
            return float(min(numeric))
        if metric == "max":
            return float(max(numeric))
        if metric == "mean":
            return float(sum(numeric) / len(numeric))
        raise ValueError(f"metric {metric!r} is not recomputable")

    def _do_verify_files(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        metadata = self._metadata(run)
        context = metadata.get("context")
        context = context if isinstance(context, Mapping) else {}
        expected = context.get("files") or metadata.get("files") or {}
        if not isinstance(expected, Mapping):
            expected = {}
        checks: dict[str, dict[str, Any]] = {}
        problems: list[str] = []
        for raw_path, expected_hash in expected.items():
            path = Path(str(raw_path)).expanduser()
            if not path.is_absolute():
                path = self.root / path
            try:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                problems.append(f"{path.name}: {exc}")
                checks[str(raw_path)] = {"ok": False, "error": str(exc)}
                continue
            ok = actual == str(expected_hash)
            checks[str(raw_path)] = {"ok": ok, "sha256": actual}
            if not ok:
                problems.append(f"{path.name}: hash mismatch")
        passed = bool(checks and not problems)
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED,
            detail=f"{len(checks)} file(s) checked; {len(problems)} problem(s)",
            output={
                "passed": passed,
                "checked": len(checks),
                "checks": checks,
                "problems": problems[:20],
                "reason": (
                    f"{len(checks)} file hash(es) matched"
                    if passed
                    else "; ".join(problems[:3]) or "no files were supplied to verify"
                ),
            },
            tool=step.tool,
        )

    async def _do_run_tests(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        command = step.metadata.get("command")
        if not isinstance(command, Sequence) or isinstance(command, str) or not command:
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.FAILED,
                detail="the test command was not captured as an argv list",
                tool=step.tool,
            )
        timeout = float(step.metadata.get("timeout", self.default_timeout) or self.default_timeout)
        result = await self.runner.run([str(part) for part in command], cwd=self.root, timeout=timeout)
        report = parse_test_output(result)
        passed = report.ok
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED if not result.timed_out else StepStatus.FAILED,
            detail=(
                f"{report.passed} passed, {report.failed} failed, {report.errors} error(s)"
                if report.recognised
                else "the test output carried no recognisable tally"
            ),
            output={
                "passed": passed,
                "checked": 1,
                "tests": report.to_dict(),
                "reason": (
                    f"{report.passed} test(s) passed"
                    if passed
                    else (
                        f"{report.failed} failed, {report.errors} error(s)"
                        if report.recognised
                        else "the test run produced no recognised tally"
                    )
                ),
            },
            tool=step.tool,
        )


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
