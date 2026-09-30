"""Model benchmarking: what a model ACTUALLY did, measured and stored.

Phase 14 asks for benchmark results rather than opinions. This module therefore
has one rule it will not bend: a figure is either measured and stored with the
task that produced it, or it is ``None``. There is no "QWEN3 is best" branch
anywhere, and :meth:`ModelBenchmarking.best_for` answers only from stored
records — with the number of runs those records represent, so a single lucky
run cannot masquerade as a verdict.

The runner is injected, which is what keeps this layer honest and testable:

* it is an async callable ``(prompt, category) -> BenchmarkCompletion``, so the
  application wires the real brain/provider and a test wires a fake;
* the completion carries ``first_token_ms`` only when the runner could observe
  streaming — otherwise the record says ``None``, because a first-token figure
  nobody measured is exactly the kind of number this phase exists to replace;
* per-run machine readings (RAM, GPU, NPU) come from an injected telemetry
  callable and are sampled AROUND each task, so the figure describes that run
  rather than an average of the afternoon.

Storage mirrors the audit trail: an append-only JSONL file, rewritten through a
temporary file and an atomic replace, with unreadable lines skipped. A
benchmark file is evidence, so one truncated write must not make the rest of it
unreadable — and a row that does not carry its measurements is skipped rather
than zero-filled by :meth:`ModelBenchmarking.records`.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from novacontrol.audit.models import json_safe
from novacontrol.optimization.models import BenchmarkRecord, ModelScore

#: Where a JSONL benchmark history lives inside an application's data directory.
BENCHMARK_DIRECTORY = "benchmarks"
BENCHMARK_FILENAME = "benchmarks.jsonl"

#: The default cap on stored rows. A benchmark file grows one row per task per
#: run; bounded by default so an overnight sweep cannot fill the disk, and
#: configurable for an operator who wants a longer history.
DEFAULT_BENCHMARK_CAP = 2000


@runtime_checkable
class BenchmarkStore(Protocol):
    """Storage for benchmark rows. ``local`` is declared, like the audit sink."""

    name: str
    local: bool

    def append(self, record: Mapping[str, Any]) -> None: ...

    def read(self, limit: int = 0) -> list[Mapping[str, Any]]: ...

    def replace(self, records: Sequence[Mapping[str, Any]]) -> None: ...


class InMemoryBenchmarkStore:
    """In-process store, for tests and for a run with no data directory."""

    name = "memory"
    local = True

    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def append(self, record: Mapping[str, Any]) -> None:
        self._rows.append(dict(record))

    def read(self, limit: int = 0) -> list[Mapping[str, Any]]:
        rows: list[Mapping[str, Any]] = list(self._rows)
        if limit and limit > 0:
            return rows[-limit:]
        return rows

    def replace(self, records: Sequence[Mapping[str, Any]]) -> None:
        self._rows = [dict(row) for row in records]


class JsonlBenchmarkStore:
    """Append-only JSONL history, durable and local.

    Rewrites go through a temporary file and an atomic replace, exactly like the
    audit sink: a crash mid-prune leaves the previous history intact rather than
    a half-written one. Reading tolerates a damaged line by skipping it.
    """

    name = "jsonl"
    local = True

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, record: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(json_safe(record), ensure_ascii=False) + "\n")

    def read(self, limit: int = 0) -> list[Mapping[str, Any]]:
        if not self.path.exists():
            return []
        rows: list[Mapping[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if isinstance(parsed, dict):
                    rows.append(parsed)
        if limit and limit > 0:
            return rows[-limit:]
        return rows

    def replace(self, records: Sequence[Mapping[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            for row in records:
                handle.write(json.dumps(json_safe(row), ensure_ascii=False) + "\n")
        os.replace(temporary, self.path)


@dataclass(frozen=True, slots=True)
class BenchmarkCompletion:
    """What a runner returned, including what it could NOT measure.

    ``task_ok`` defaults to ``None``, meaning "the run completed and nothing
    checked the answer". A runner that verifies its own output sets it; the
    benchmark layer does not promote "it returned text" into "it was right".
    """

    text: str = ""
    first_token_ms: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    task_ok: bool | None = None
    structured_ok: bool | None = None
    tool_ok: bool | None = None
    metadata: dict[str, Any] | None = None


#: ``(prompt, category) -> completion``. The category travels with the call so a
#: runner can use a task-specific prompt shape without this layer knowing how.
BenchmarkRunner = Callable[[str, str], Awaitable[BenchmarkCompletion]]

#: Reads the machine NOW, for the per-run figures. A mapping (not a fixed
#: signature) so a deployment that measures less simply omits keys.
RuntimeReading = Callable[[], Mapping[str, Any]]


class ModelBenchmarking:
    """Run tasks against a runner, store the measurements, compare by category."""

    def __init__(
        self,
        store: BenchmarkStore | None = None,
        *,
        cap: int = DEFAULT_BENCHMARK_CAP,
        clock: Callable[[], float] = time.monotonic,
        stamp: Callable[[], datetime] | None = None,
        reading: RuntimeReading | None = None,
    ) -> None:
        self.cap = max(0, int(cap))
        self._store: BenchmarkStore = store if store is not None else InMemoryBenchmarkStore()
        self._clock = clock
        self._stamp = stamp or (lambda: datetime.now(UTC))
        self._reading = reading

    # -- storage ----------------------------------------------------------------
    @property
    def store(self) -> BenchmarkStore:
        return self._store

    def record(self, record: BenchmarkRecord) -> BenchmarkRecord:
        """Store one record, applying the cap by dropping the oldest rows."""
        self._store.append(record.to_dict())
        if self.cap > 0:
            rows = list(self._store.read())
            if len(rows) > self.cap:
                self._store.replace(rows[-self.cap :])
        return record

    def records(self, limit: int = 0) -> tuple[BenchmarkRecord, ...]:
        """Stored measurements, oldest first. Unreadable rows are skipped."""
        parsed: list[BenchmarkRecord] = []
        for row in self._store.read(limit):
            try:
                parsed.append(BenchmarkRecord.from_dict(dict(row)))
            except (TypeError, ValueError):
                continue
        return tuple(parsed)

    def clear(self) -> int:
        removed = len(self._store.read())
        self._store.replace([])
        return removed

    # -- running ----------------------------------------------------------------
    async def run(
        self,
        model: str,
        tasks: Sequence[str],
        *,
        category: str,
        runner: BenchmarkRunner,
        provider: str = "",
    ) -> tuple[BenchmarkRecord, ...]:
        """Measure ``model`` on every task, one record per task.

        A runner that raises is a FAILED record, not a lost one: the failure
        rate is one of the specification's figures, and a benchmark that drops
        its failures reports a better model than was actually measured. The
        per-task readings are sampled immediately before and after the call, so
        what is stored describes that run.
        """
        name = str(model or "").strip()
        if not name:
            raise ValueError("a benchmark needs a model name")
        label = str(category or "").strip()
        if not label:
            raise ValueError("a benchmark needs a task category")
        produced: list[BenchmarkRecord] = []
        for task in tasks:
            record = await self._run_one(
                name,
                str(task),
                category=label,
                runner=runner,
                provider=str(provider),
            )
            self.record(record)
            produced.append(record)
        return tuple(produced)

    async def _run_one(
        self,
        model: str,
        task: str,
        *,
        category: str,
        runner: BenchmarkRunner,
        provider: str,
    ) -> BenchmarkRecord:
        before = dict(self._read())
        started = self._clock()
        try:
            completion = await runner(task, category)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            elapsed_ms = max(0.0, (self._clock() - started) * 1000.0)
            after = dict(self._read())
            return BenchmarkRecord(
                model=model,
                category=category,
                task=task,
                provider=provider,
                total_ms=elapsed_ms,
                failure=f"{type(exc).__name__}: {exc}",
                timestamp=self._stamp().isoformat(),
                metadata={"readings_before": before, "readings_after": after},
            )
        elapsed_ms = max(0.0, (self._clock() - started) * 1000.0)
        after = dict(self._read())
        seconds = elapsed_ms / 1000.0
        tokens = int(completion.completion_tokens or 0)
        return BenchmarkRecord(
            model=model,
            category=category,
            task=task,
            provider=provider,
            total_ms=elapsed_ms,
            first_token_ms=completion.first_token_ms,
            tokens_per_second=(tokens / seconds) if tokens > 0 and seconds > 0 else None,
            ram_bytes=_first_int(after, before, "ram_bytes"),
            gpu_utilization=_first_float(after, before, "gpu_utilization"),
            gpu_memory_bytes=_first_int(after, before, "gpu_memory_bytes"),
            npu_used=_first_bool(after, before, "npu_available"),
            structured_ok=completion.structured_ok,
            task_ok=completion.task_ok,
            tool_ok=completion.tool_ok,
            prompt_tokens=int(completion.prompt_tokens or 0),
            completion_tokens=tokens,
            timestamp=self._stamp().isoformat(),
            metadata={"readings_before": before, "readings_after": after},
        )

    def _read(self) -> Mapping[str, Any]:
        if self._reading is None:
            return {}
        try:
            return dict(self._reading())
        except Exception:  # pragma: no cover - a reading must never break a run
            return {}

    # -- comparing --------------------------------------------------------------
    def compare(self, category: str | None = None) -> tuple[ModelScore, ...]:
        """Aggregate stored runs by model (and category), best first.

        Ordered by measured success rate, then by median latency, then by name —
        a total order so two status reads of the same data return the same
        table. Models with no comparable figure sort last rather than first.
        """
        wanted = str(category).strip() if category else ""
        rows = [
            record
            for record in self.records()
            if not wanted or record.category == wanted
        ]
        grouped: dict[tuple[str, str], list[BenchmarkRecord]] = {}
        for record in rows:
            grouped.setdefault((record.model, record.category), []).append(record)
        scores = [
            _score(model, label, group) for (model, label), group in grouped.items()
        ]
        scores.sort(
            key=lambda score: (
                -(score.success_rate if score.success_rate is not None else -1.0),
                score.median_total_ms if score.median_total_ms is not None else float("inf"),
                score.model,
            )
        )
        return tuple(scores)

    def best_for(self, category: str) -> ModelScore | None:
        """The best measured model in a category, or ``None`` with no data.

        "Best" is computed from stored records only: success rate first, median
        latency as the tie-break. Nothing in this build names a model — change
        the runner or the machine and the answer changes with the measurements.
        """
        scores = self.compare(category)
        return scores[0] if scores else None

    # -- reporting --------------------------------------------------------------
    def report(self) -> dict[str, Any]:
        rows = self._store.read()
        models = sorted({str(row.get("model", "")) for row in rows if row.get("model")})
        categories = sorted(
            {str(row.get("category", "")) for row in rows if row.get("category")}
        )
        return {
            "records": len(rows),
            "cap": self.cap,
            "sink": self._store.name,
            "storage": str(getattr(self._store, "path", "")) or "in-memory",
            "models": models,
            "categories": categories,
        }

    def to_dict(self) -> dict[str, Any]:
        return self.report()


def _score(model: str, category: str, group: Sequence[BenchmarkRecord]) -> ModelScore:
    successes = 0
    failures = 0
    structured_ok = 0
    structured_checked = 0
    tool_ok = 0
    tool_checked = 0
    totals: list[float] = []
    first_tokens: list[float] = []
    rates: list[float] = []
    for record in group:
        totals.append(float(record.total_ms))
        if record.failed or record.task_ok is False:
            failures += 1
        else:
            successes += 1
        if record.structured_ok is not None:
            structured_checked += 1
            if record.structured_ok:
                structured_ok += 1
        if record.tool_ok is not None:
            tool_checked += 1
            if record.tool_ok:
                tool_ok += 1
        if record.first_token_ms is not None:
            first_tokens.append(float(record.first_token_ms))
        if record.tokens_per_second is not None:
            rates.append(float(record.tokens_per_second))
    return ModelScore(
        model=model,
        category=category,
        runs=len(group),
        successes=successes,
        failures=failures,
        structured_ok=structured_ok,
        structured_checked=structured_checked,
        tool_ok=tool_ok,
        tool_checked=tool_checked,
        mean_total_ms=statistics.fmean(totals) if totals else None,
        median_total_ms=statistics.median(totals) if totals else None,
        mean_first_token_ms=statistics.fmean(first_tokens) if first_tokens else None,
        mean_tokens_per_second=statistics.fmean(rates) if rates else None,
    )


def _first_int(after: Mapping[str, Any], before: Mapping[str, Any], key: str) -> int | None:
    value = after.get(key, before.get(key))
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _first_float(after: Mapping[str, Any], before: Mapping[str, Any], key: str) -> float | None:
    value = after.get(key, before.get(key))
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _first_bool(after: Mapping[str, Any], before: Mapping[str, Any], key: str) -> bool | None:
    value = after.get(key, before.get(key))
    return value if isinstance(value, bool) else None


__all__ = [
    "BENCHMARK_DIRECTORY",
    "BENCHMARK_FILENAME",
    "DEFAULT_BENCHMARK_CAP",
    "BenchmarkCompletion",
    "BenchmarkRunner",
    "BenchmarkStore",
    "InMemoryBenchmarkStore",
    "JsonlBenchmarkStore",
    "ModelBenchmarking",
    "RuntimeReading",
]
