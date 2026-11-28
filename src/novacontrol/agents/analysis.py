"""Deterministic dataset analysis specialist (Phase 28).

This is a real agent, not a simulated one: it opens a CSV, computes statistics
from the actual bytes, and reports the row count and SHA-256 it read. It exists
because a collaboration needs at least one workstream whose output can be
recomputed independently; model-backed reasoning cannot provide that guarantee
on a machine with no model.

The agent runs through the same :class:`~novacontrol.agents.pipeline.SpecialistAgent`
stages as every other specialist, so its permission declaration, verification
stage and failure containment are the build's, not this file's.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import re
import statistics
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from novacontrol.agents.models import AgentRole, AgentTask
from novacontrol.agents.pipeline import (
    AgentRun,
    PipelineStep,
    SpecialistAgent,
    SpecialistDecision,
    StepOutcome,
    StepStatus,
)
from novacontrol.agents.pipeline import SpecialistPipeline
from novacontrol.agentcore.verifier import VerificationResult, VerificationStatus
from novacontrol.core.security import PermissionScope, RiskLevel
from novacontrol.reliability.permissions import PermissionDeclaration

__all__ = ["DataAnalysisAgent"]

_CSV_PATH = re.compile(r"""["']?([^\s"']+\.csv)["']?""", re.IGNORECASE)
_COLUMN = re.compile(
    # "column in data.csv" names a file, not a column called "in data".
    # A quoted name may still legitimately start with any word; the stop-word
    # guard applies only to the bare form.
    r"""\bcolumn\s+(?!(?:in|from|of|for|with|and|the|each|every|all|is|are|to|by)\b)"
    r"(?:"([^"]+)"|'([^']+)'|([A-Za-z_][\w-]*(?:\s+[A-Za-z_][\w-]*)?))"
    r"(?=\s*(?:$|[,.;]|\b(?:in|from|of|with)\b))""",
    re.IGNORECASE,
)
_MAX_BYTES = 20 * 1024 * 1024
_MAX_ROWS = 200_000
_MAX_COLUMNS = 60


def _declaration(scope: PermissionScope, risk: RiskLevel = RiskLevel.LOW) -> PermissionDeclaration:
    return PermissionDeclaration(risk_level=risk, required_permission=scope)


class DataAnalysisAgent(SpecialistAgent):
    """Reads a CSV inside its root and reports recomputable column statistics."""

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        pipeline: SpecialistPipeline | None = None,
        max_rows: int = _MAX_ROWS,
    ) -> None:
        super().__init__(
            "data-analysis-agent",
            AgentRole.DATA_ANALYSIS,
            "Computes per-column statistics from a real CSV and returns the evidence "
            "a verifier can independently recompute.",
            pipeline=pipeline,
        )
        self.root = Path(root or Path.cwd()).resolve()
        self.max_rows = max(1, min(int(max_rows), _MAX_ROWS))

    def clone(self, pipeline: SpecialistPipeline) -> "DataAnalysisAgent":
        return DataAnalysisAgent(
            root=self.root, pipeline=pipeline, max_rows=self.max_rows
        )

    def declarations(self) -> Mapping[str, PermissionDeclaration]:
        return {
            "analysis.analyze_dataset": _declaration(PermissionScope.FILESYSTEM_READ),
            "analysis.inspect_dataset": _declaration(PermissionScope.FILESYSTEM_READ),
        }

    def availability(self) -> Mapping[str, Any]:
        try:
            available = self.root.is_dir()
        except OSError:
            available = False
        return {
            "available": available,
            "reason": "" if available else f"data root is not a directory: {self.root}",
        }

    # -- decision and plan ---------------------------------------------------

    def decide(
        self, task: AgentTask, interpretation: Any, context: Mapping[str, Any]
    ) -> SpecialistDecision:
        data = self._request(task)
        if not data["path"]:
            return SpecialistDecision(
                action="inspect_dataset",
                reason="no dataset path was supplied in the task or its context",
                confidence=0.0,
                data=data,
            )
        return SpecialistDecision(
            action="analyze_dataset",
            reason=(
                f"compute {'column ' + data['column'] if data['column'] else 'per-column'} "
                f"statistics from {data['path']}"
            ),
            confidence=0.9,
            data=data,
        )

    def plan(
        self,
        task: AgentTask,
        interpretation: Any,
        decision: SpecialistDecision,
        context: Mapping[str, Any],
    ) -> tuple[PipelineStep, ...]:
        path = str(decision.data.get("path", ""))
        column = str(decision.data.get("column", ""))
        if not path:
            return (
                PipelineStep(
                    action="inspect_dataset",
                    description=f"Report the missing dataset input for: {task.goal}",
                    tool="analysis.inspect_dataset",
                    target=task.goal,
                    expected="The missing dataset is named with a reason",
                ),
            )
        return (
            PipelineStep(
                action="analyze_dataset",
                description=(
                    f"Analyse {'column ' + column if column else 'every column'} in {path}"
                ),
                tool="analysis.analyze_dataset",
                target=path,
                expected=(
                    "Rows, columns, SHA-256 and numeric statistics computed from the file; "
                    "every metric is independently recomputable"
                ),
                permission=PermissionScope.FILESYSTEM_READ,
                risk=RiskLevel.LOW,
                metadata={"path": path, "column": column},
            ),
        )

    # -- execution -----------------------------------------------------------

    async def execute(self, step: PipelineStep, run: AgentRun) -> StepOutcome:
        if step.action == "inspect_dataset":
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.FAILED,
                detail="no dataset path was supplied",
                tool=step.tool,
            )
        path = str(step.metadata.get("path", ""))
        column = str(step.metadata.get("column", ""))
        try:
            analysis = await asyncio.to_thread(self._analyze, path, column)
        except ValueError as exc:
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.FAILED,
                detail=str(exc),
                tool=step.tool,
            )
        except (OSError, UnicodeError, csv.Error) as exc:
            return StepOutcome(
                step_id=step.id,
                action=step.action,
                status=StepStatus.FAILED,
                detail=f"the dataset could not be read: {type(exc).__name__}: {exc}",
                tool=step.tool,
            )
        return StepOutcome(
            step_id=step.id,
            action=step.action,
            status=StepStatus.COMPLETED,
            detail=(
                f"{analysis['rows']} row(s), {analysis['columns']} column(s); "
                f"{len(analysis['metrics'])} metric(s)"
            ),
            output=analysis,
            tool=step.tool,
        )

    def verify(
        self, step: PipelineStep, outcome: StepOutcome, run: AgentRun
    ) -> VerificationResult:
        if not outcome.ok:
            return VerificationResult(
                status=VerificationStatus.FAIL,
                expectation=step.expected or "the dataset was analysed",
                reason=outcome.detail or "the analysis did not complete",
                evidence={},
                confidence=0.8,
            )
        metrics = outcome.output.get("metrics")
        digest = str(outcome.output.get("sha256", ""))
        path = str(outcome.output.get("dataset_path", ""))
        rows = int(outcome.output.get("rows", 0) or 0)
        if not isinstance(metrics, dict) or not metrics or rows <= 0 or not digest:
            return VerificationResult(
                status=VerificationStatus.INCONCLUSIVE,
                expectation=step.expected or "recomputable statistics",
                reason="the analysis produced no usable metrics or identity",
                evidence={"rows": rows},
                confidence=0.0,
            )
        try:
            actual = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except (OSError, ValueError):
            return VerificationResult(
                status=VerificationStatus.FAIL,
                expectation=step.expected or "the analysed file still exists",
                reason=f"the analysed file is no longer readable: {path}",
                evidence={"path": path},
                confidence=0.9,
            )
        if actual != digest:
            return VerificationResult(
                status=VerificationStatus.FAIL,
                expectation=step.expected or "the analysed bytes are the reported bytes",
                reason="the file changed after it was analysed; the metrics no longer describe it",
                evidence={"expected_sha256": digest, "actual_sha256": actual},
                confidence=0.95,
            )
        return VerificationResult(
            status=VerificationStatus.PASS,
            expectation=step.expected or "recomputable statistics",
            reason=f"{rows} row(s) and {len(metrics)} metric(s) read from unchanged bytes",
            evidence={
                "sha256": digest,
                "rows": rows,
                "path": path,
            },
            confidence=0.9,
        )

    def report(self, run: AgentRun) -> dict[str, Any]:
        output: dict[str, Any] = {
            "dataset": "",
            "path": "",
            "rows": 0,
            "columns": 0,
            "metrics": {},
            "claims": [],
            "verified": run.verified,
        }
        for outcome in run.outcomes:
            if outcome.action == "analyze_dataset" and outcome.ok:
                output.update(
                    {
                        "dataset": outcome.output.get("dataset_name", ""),
                        "path": outcome.output.get("dataset_path", ""),
                        "rows": outcome.output.get("rows", 0),
                        "columns": outcome.output.get("columns", 0),
                        "metrics": outcome.output.get("metrics", {}),
                        "claims": outcome.output.get("claims", []),
                        "sha256": outcome.output.get("sha256", ""),
                    }
                )
                output["verified"] = run.verified
                break
        return output

    def summarize(self, run: AgentRun) -> str:
        if not run.verified:
            return super().summarize(run)
        for outcome in run.outcomes:
            if outcome.action == "analyze_dataset" and outcome.ok:
                return (
                    f"Analysed {outcome.output.get('dataset_name', 'the dataset')}: "
                    f"{outcome.output.get('rows', 0)} row(s), "
                    f"{outcome.output.get('columns', 0)} column(s), "
                    f"{len(outcome.output.get('metrics', {}))} recomputable metric(s)."
                )
        return super().summarize(run)

    # -- the actual computation ---------------------------------------------

    def _request(self, task: AgentTask) -> dict[str, str]:
        context = task.metadata.get("context")
        context = context if isinstance(context, Mapping) else {}
        raw_path = str(
            task.metadata.get("dataset")
            or context.get("dataset_path")
            or context.get("path")
            or ""
        ).strip()
        if not raw_path:
            match = _CSV_PATH.search(task.goal)
            raw_path = match.group(1) if match else ""
        column = str(
            task.metadata.get("column")
            or context.get("column")
            or ""
        ).strip()
        if not column:
            match = _COLUMN.search(task.goal)
            if match:
                column = next(
                    (group for group in match.groups() if group), ""
                ).strip()
        return {"path": raw_path, "column": column}

    def _resolve(self, raw: str) -> Path:
        path = Path(raw).expanduser()
        if not path.is_absolute():
            path = self.root / path
        resolved = path.resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise ValueError(
                f"the dataset is outside this agent's root ({self.root}): {raw}"
            )
        if not resolved.is_file():
            raise ValueError(f"the dataset does not exist: {raw}")
        if resolved.stat().st_size > _MAX_BYTES:
            raise ValueError(
                f"the dataset is larger than the {_MAX_BYTES // (1024 * 1024)} MiB read ceiling"
            )
        return resolved

    def _analyze(self, raw_path: str, column: str) -> dict[str, Any]:
        path = self._resolve(raw_path)
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        rows: list[dict[str, str]] = []
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fieldnames = [str(name) for name in (reader.fieldnames or ())]
            if not fieldnames:
                raise ValueError("the CSV has no header row")
            if column and column not in fieldnames:
                raise ValueError(
                    f"column {column!r} is not in the dataset (columns: {', '.join(fieldnames[:20])})"
                )
            for index, row in enumerate(reader):
                if index >= self.max_rows:
                    break
                rows.append({key: str(value or "") for key, value in row.items() if key})
        selected = [column] if column else fieldnames[:_MAX_COLUMNS]
        metrics: dict[str, float] = {}
        claims: list[dict[str, Any]] = []
        details: dict[str, Any] = {}
        for name in selected:
            values = [row.get(name, "").strip() for row in rows]
            numbers = [number for value in values if (number := _number(value)) is not None]
            missing = sum(1 for value in values if not value)
            column_data: dict[str, Any] = {
                "count": len(values),
                "missing": missing,
                "numeric": len(numbers),
            }
            if numbers and len(numbers) >= 3:
                column_data.update(
                    {
                        "mean": statistics.fmean(numbers),
                        "sum": sum(numbers),
                        "min": min(numbers),
                        "max": max(numbers),
                        "stdev": statistics.pstdev(numbers) if len(numbers) > 1 else 0.0,
                    }
                )
                for metric in ("mean", "sum", "min", "max", "count"):
                    key = f"column:{name}:{metric}"
                    value = column_data[metric]
                    metrics[key] = float(value)
                    claims.append(
                        {
                            "topic": key,
                            "value": float(value),
                            "metric": metric,
                            "column": name,
                            "kind": "evidence",
                            "text": f"Column {name!r} {metric} is {value} over {len(values)} row(s).",
                            "confidence": 0.9,
                        }
                    )
            else:
                counts = Counter(value for value in values if value)
                top, top_count = counts.most_common(1)[0] if counts else ("", 0)
                unique = len(counts)
                column_data.update({"unique": unique, "top": top, "top_count": top_count})
                key = f"column:{name}:unique"
                metrics[key] = float(unique)
                claims.append(
                    {
                        "topic": key,
                        "value": float(unique),
                        "metric": "unique",
                        "column": name,
                        "kind": "evidence",
                        "text": f"Column {name!r} has {unique} distinct value(s).",
                        "confidence": 0.85,
                    }
                )
            details[name] = column_data
        if not metrics:
            raise ValueError("the dataset produced no analysable values")
        return {
            "dataset_path": str(path),
            "dataset_name": path.name,
            "sha256": digest,
            "rows": len(rows),
            "columns": len(fieldnames),
            "analyzed_columns": selected,
            "metrics": metrics,
            "claims": claims,
            "column_details": details,
            "truncated": len(rows) >= self.max_rows,
        }


def _number(value: str) -> float | None:
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None
