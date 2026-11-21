"""PI-02 historical acquisition through the existing market-data engine.

This module owns no provider, no transport, no retry policy and no store.  It
calls ``MarketDataEngine.ingest_history`` for each requested instrument, in
configuration order, and returns measured reports.  A provider failure is a
report with errors — never a fabricated bar and never a lost previously stored
observation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any, cast

from novacontrol.novamarket.datasets.models import DatasetBuildRequest
from novacontrol.novamarket.marketdata.models import DataInterval, HistoryIngestReport


class DatasetAcquisition:
    """Bounded, sequential range acquisition over the approved providers."""

    def __init__(self, market_data: Any) -> None:
        if market_data is None:
            raise ValueError("a MarketDataEngine (or compatible reader) is required")
        self._market_data = market_data

    async def collect(
        self, request: DatasetBuildRequest
    ) -> dict[str, HistoryIngestReport]:
        reports: dict[str, HistoryIngestReport] = {}
        # Sequential on purpose: the free providers share a rate limit and the
        # 20-instrument cap; parallel range downloads would multiply the burst
        # without adding throughput a rate-limited provider can serve.
        for instrument_id in request.instruments:
            reports[instrument_id] = await self._collect_one(request, instrument_id)
        return reports

    async def _collect_one(
        self, request: DatasetBuildRequest, instrument_id: str
    ) -> HistoryIngestReport:
        # Providers are tried in the requested priority order.  The engine still
        # owns rotation, rate limiting, retries and normalization; this loop only
        # chooses which approved provider to ask.
        providers: tuple[str | None, ...] = tuple(request.providers) or (None,)
        failures: list[str] = []
        report: HistoryIngestReport | None = None
        start = request.start_at
        if request.incremental:
            # Overlap the latest stored candle: the provider may have revised it,
            # and the store's unique key deduplicates the re-read.
            try:
                latest = self._market_data.bars(
                    instrument_id,
                    interval=request.resolution,
                    start=request.start_at,
                    end=request.end_at,
                    limit=1,
                    ascending=False,
                )
            except Exception:  # noqa: BLE001 - an unreadable store means a full fetch
                latest = None
            if latest is not None and latest.items:
                start = latest.items[0].observed_at
        for provider in providers:
            try:
                attempt = cast(
                    HistoryIngestReport,
                    await self._market_data.ingest_history(
                        instrument_id,
                        request.resolution,
                        start=start,
                        end=request.end_at,
                        provider=provider,
                        allow_partial=request.allow_partial,
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - one provider must not stop a build
                attempt = _failed_report(request, instrument_id, exc)
            report = attempt
            if attempt.ok:
                if failures:
                    # Keep fallback failures visible in the report without
                    # turning a successful second provider into an outage.
                    return replace(
                        attempt,
                        errors=tuple(dict.fromkeys(failures + list(attempt.errors))),
                    )
                return attempt
            failures.extend(attempt.errors)
        if report is None:  # pragma: no cover - providers is never empty
            return _failed_report(
                request, instrument_id, RuntimeError("no provider configured")
            )
        return replace(
            report, errors=tuple(dict.fromkeys(failures)) or report.errors
        )


def _failed_report(
    request: DatasetBuildRequest, instrument_id: str, exc: Exception
) -> HistoryIngestReport:
    now = request.start_at
    return HistoryIngestReport(
        instrument_id=instrument_id,
        interval=DataInterval(request.resolution),
        requested_start=request.start_at,
        requested_end=request.end_at,
        started_at=now,
        finished_at=now,
        errors=(f"{type(exc).__name__}: {exc}",),
    )


def acquisition_summary(
    reports: Sequence[HistoryIngestReport],
) -> dict[str, Any]:
    return {
        "instruments": len(reports),
        "succeeded": sum(1 for report in reports if report.ok),
        "failed": sum(1 for report in reports if not report.ok),
        "bars_received": sum(report.bars_received for report in reports),
        "bars_inserted": sum(report.bars_inserted for report in reports),
        "bars_duplicate": sum(report.bars_duplicate for report in reports),
        "bars_rejected": sum(report.bars_rejected for report in reports),
        "providers": sorted({report.provider for report in reports if report.provider}),
        "reports": [report.to_dict() for report in reports],
    }


__all__ = ["DatasetAcquisition", "acquisition_summary"]
