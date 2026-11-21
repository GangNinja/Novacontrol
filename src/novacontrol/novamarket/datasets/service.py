"""PI-02 dataset service: acquisition, versioning, retrieval and training sets.

The service composes existing authorities rather than replacing them:

* acquisition goes through ``MarketDataEngine.ingest_history``;
* observations come from the append-only ``market_bars`` table;
* calendars come from ``marketdata.calendar``;
* horizons and feature cutoffs come from PI-01's ``HORIZON_SPECS``;
* events use the existing ``MarketEventPublisher`` and event vocabulary.

It adds only dataset-owned persistence: immutable version metadata, membership
references, selection/conflict records, gap classification, corporate actions,
annotations and leakage-checked training samples.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from novacontrol.novamarket.datasets.acquisition import (
    DatasetAcquisition,
    acquisition_summary,
)
from novacontrol.novamarket.datasets.corporate import adjust_bars
from novacontrol.novamarket.datasets.models import (
    DATASET_CONTRACT_VERSION,
    DATASET_NORMALIZATION_VERSION,
    MAX_BARS_PER_INSTRUMENT,
    PI02_PHASE,
    AdjustmentBasis,
    CorporateAction,
    DatasetBuildRequest,
    DatasetError,
    DatasetMember,
    DatasetNotFoundError,
    DatasetQuality,
    DatasetSegment,
    DatasetStateError,
    DatasetValidationError,
    DatasetVersion,
    GapKind,
    MarketAnnotation,
)
from novacontrol.novamarket.datasets.store import DatasetStore
from novacontrol.novamarket.datasets.training import (
    build_training_dataset,
    verify_stored_training,
)
from novacontrol.novamarket.datasets.validation import (
    SegmentAssessment,
    aggregate_quality,
    apply_provider_failure_annotations,
    assess_segment,
    combine_coverage,
    combine_missing,
    combine_validation,
)
from novacontrol.novamarket.marketdata.calendar import calendar_contract, calendar_for
from novacontrol.novamarket.marketdata.models import (
    DataInterval,
    DataQuality,
    Instrument,
    OhlcvBar,
)
from novacontrol.novamarket.prediction.models import (
    ForecastHorizon,
    HORIZON_SPECS,
    HistoricalMarketDataset,
)


class HistoricalDatasetService:
    """Versioned historical datasets and leakage-safe training preparation."""

    def __init__(
        self,
        store_path: str | Path | None = None,
        *,
        store: DatasetStore | None = None,
        market_data: Any | None = None,
        publisher: Any | None = None,
        clock: Any | None = None,
        normalization_version: str = DATASET_NORMALIZATION_VERSION,
    ) -> None:
        self._store = store or DatasetStore(store_path or ":memory:")
        self._market_data = market_data
        self._acquisition = DatasetAcquisition(market_data) if market_data is not None else None
        self._publisher = publisher
        self._clock = clock or (lambda: datetime.now(UTC))
        self.normalization_version = normalization_version
        self._last_event_error = ""

    @property
    def store(self) -> DatasetStore:
        return self._store

    @property
    def market_data_attached(self) -> bool:
        return self._market_data is not None

    # -- events ---------------------------------------------------------------

    async def _publish(self, event_type: str, **payload: Any) -> None:
        if self._publisher is None:
            return
        try:
            publish = getattr(self._publisher, "publish", None)
            if publish is not None:
                outcome = publish(event_type, **payload)
            else:
                outcome = self._publisher(event_type, payload)
            if inspect.isawaitable(outcome):
                await outcome
        except Exception as exc:  # noqa: BLE001 - a committed dataset must not fail on an event
            self._last_event_error = f"{type(exc).__name__}: {exc}"

    # -- build ----------------------------------------------------------------

    async def build(
        self,
        request: DatasetBuildRequest | Mapping[str, Any],
        *,
        fetch: bool | None = None,
    ) -> dict[str, Any]:
        """Acquire, validate, version and persist one dataset build."""
        if not isinstance(request, DatasetBuildRequest):
            try:
                request = DatasetBuildRequest(**dict(request))
            except TypeError as exc:
                raise DatasetValidationError(f"invalid dataset build request: {exc}") from exc
        if self._market_data is None:
            raise DatasetError("dataset builds require the existing market-data engine")
        if fetch is not None:
            if not isinstance(fetch, bool):
                raise DatasetValidationError("fetch must be a boolean")
            request = DatasetBuildRequest(
                **{
                    **self._request_kwargs(request),
                    "fetch": fetch,
                }
            )
        resolution = DataInterval(request.resolution)
        build_id = self._store.start_build(request.dataset_id, request.version or "auto")
        await self._publish(
            "novamarket.dataset.build.started",
            build_id=build_id,
            dataset_id=request.dataset_id,
            instruments=list(request.instruments),
            resolution=resolution.value,
            start_at=request.start_at.isoformat(),
            end_at=request.end_at.isoformat(),
        )
        try:
            if request.adjustment_basis is AdjustmentBasis.PROVIDER_ADJUSTED:
                raise DatasetValidationError(
                    "no installed provider supplies a verified provider-adjusted "
                    "series; build raw membership and derive split/total-return "
                    "adjustments from recorded actions"
                )
            instruments = self._resolve_instruments(request)
            reports: dict[str, Any] = {}
            acquisition_errors: dict[str, tuple[str, ...]] = {}
            if request.fetch and self._acquisition is not None:
                reports = await self._acquisition.collect(request)
                acquisition_errors = {
                    instrument_id: report.errors
                    for instrument_id, report in reports.items()
                    if report.errors and not report.ok
                }
            assessments: list[SegmentAssessment] = []
            segments_list: list[DatasetSegment] = []
            # Member references are collected as lightweight tuples, not bar
            # objects: a build must not hold every selected candle in memory
            # while it validates the next instrument.
            member_rows: list[
                tuple[str, str, int, str, str, datetime, DataQuality]
            ] = []
            for instrument in instruments:
                bars = self._read_all_bars(
                    instrument.instrument_id, resolution, request
                )
                annotations = list(
                    self._store.annotations(
                        instrument_id=instrument.instrument_id,
                        start=request.start_at,
                        end=request.end_at,
                        limit=5_000,
                    )
                )
                annotations.extend(
                    apply_provider_failure_annotations(
                        instrument.instrument_id,
                        request.start_at,
                        request.end_at,
                        acquisition_errors.get(instrument.instrument_id, ()),
                    )
                )
                assessment = assess_segment(
                    instrument,
                    resolution,
                    request.start_at,
                    request.end_at,
                    bars,
                    provider_priority=request.providers,
                    annotations=annotations,
                    min_coverage_ratio=request.min_coverage_ratio,
                    allow_partial=request.allow_partial,
                )
                for ordinal, bar in enumerate(assessment.selected):
                    member_rows.append(
                        (
                            bar.instrument_id,
                            instrument.exchange,
                            ordinal,
                            bar.bar_id,
                            bar.provider,
                            bar.observed_at,
                            bar.quality,
                        )
                    )
                segments_list.append(
                    DatasetSegment(
                        instrument_id=instrument.instrument_id,
                        exchange=instrument.exchange,
                        mic=instrument.mic,
                        resolution=resolution,
                        bar_count=len(assessment.selected),
                        first_at=(
                            assessment.selected[0].observed_at
                            if assessment.selected
                            else None
                        ),
                        last_at=(
                            assessment.selected[-1].observed_at
                            if assessment.selected
                            else None
                        ),
                        providers=tuple(
                            sorted(
                                {bar.provider for bar in assessment.selected}
                            )
                        ),
                        quality=assessment.quality,
                        coverage=assessment.coverage,
                        validation=assessment.validation,
                        missing=assessment.missing,
                        membership_digest=assessment.membership_digest,
                        exchange_timezone=instrument.timezone,
                        currency=instrument.currency,
                        volume_unit="provider",
                    )
                )
                # Drop the bar sequence; the aggregate only needs the measured
                # summaries, and the references above are sufficient membership.
                assessments.append(replace(assessment, selected=()))
            segments = tuple(segments_list)
            trusted = all(
                segment.bar_count > 0
                and segment.quality
                not in (DatasetQuality.INCOMPLETE, DatasetQuality.INVALID)
                for segment in segments
            )
            quality = aggregate_quality(assessments, complete=trusted)
            actions = self._store.corporate_actions(
                start=None,
                end=request.end_at,
                limit=5_000,
            )["items"]
            relevant_actions = tuple(
                str(action["action_id"])
                for action in actions
                if str(action.get("instrument_id")) in request.instruments
            )
            limitations: list[str] = []
            for item in assessments:
                limitations.extend(item.limitations)
            if acquisition_errors:
                limitations.append(
                    "acquisition reported provider failure(s) for "
                    + ", ".join(sorted(acquisition_errors))
                    + "; previously stored observations were preserved"
                )
            if request.adjustment_basis is not AdjustmentBasis.RAW:
                limitations.append(
                    "dataset membership is raw exchange data; adjusted series are "
                    "derived on retrieval from recorded, sourced corporate actions"
                )
            if not relevant_actions:
                limitations.append(
                    "no corporate actions are recorded for these instruments; the "
                    "dataset does not claim that none occurred"
                )
            aggregate_coverage = combine_coverage(assessments)
            aggregate_validation = combine_validation(assessments)
            aggregate_missing = combine_missing(assessments)
            # `complete` is literal coverage, not a quality threshold: a build
            # with an absent expected observation is visible as incomplete even
            # when its remaining data is trustworthy enough for training.
            complete = bool(
                trusted
                and quality.usable_for_training
                and aggregate_coverage.calendar_verified
                and aggregate_validation.bars_selected_partial == 0
                and aggregate_missing.total_missing == 0
                and aggregate_coverage.expected_bars is not None
                and aggregate_coverage.expected_bars > 0
                and aggregate_coverage.bar_count
                >= aggregate_coverage.expected_bars
            )
            if quality is DatasetQuality.VALID and not complete:
                quality = DatasetQuality.INCOMPLETE
            provisional = DatasetVersion(
                dataset_id=request.dataset_id,
                version="pending",
                created_at=self._clock(),
                resolution=resolution,
                start_at=request.start_at,
                end_at=request.end_at,
                instruments=request.instruments,
                exchanges=tuple(item.instrument.exchange for item in assessments),
                providers=tuple(
                    sorted(
                        {
                            provider
                            for segment in segments
                            for provider in segment.providers
                        }
                    )
                ),
                normalization_version=self.normalization_version,
                price_basis=AdjustmentBasis.RAW,
                quality=quality,
                complete=complete,
                coverage=aggregate_coverage,
                validation=aggregate_validation,
                missing=aggregate_missing,
                segments=segments,
                config=request.config(),
                acquisition=tuple(
                    report.to_dict() for report in reports.values()
                ),
                corporate_actions=relevant_actions,
                limitations=tuple(dict.fromkeys(limitations)),
            )
            if request.version:
                # An explicit label is honoured before content reuse: asking for
                # v1 with different content must be refused, never silently
                # answered with a different existing version.
                pinned = self._store.get_version(request.dataset_id, request.version)
                if pinned is not None and (
                    str(pinned.get("content_digest")) != provisional.content_digest
                ):
                    raise DatasetStateError(
                        f"dataset version {request.dataset_id}:{request.version} is "
                        "immutable and already has different content; build a new version"
                    )
                existing = pinned
            else:
                existing = self._store.find_by_content_digest(
                    request.dataset_id, provisional.content_digest
                )
            if existing is not None:
                self._store.update_build(
                    build_id,
                    status="identical",
                    checkpoint={"content_digest": provisional.content_digest},
                )
                await self._publish(
                    "novamarket.dataset.build.completed",
                    build_id=build_id,
                    dataset_id=str(existing["dataset_id"]),
                    version=str(existing["version"]),
                    duplicate=True,
                )
                return {
                    **existing,
                    "duplicate": True,
                    "build_id": build_id,
                    "acquisition": acquisition_summary(
                        list(reports.values())
                    )
                    if reports
                    else {},
                    "historical_datasets": [
                        item.to_dict()
                        for item in self.historical_datasets(
                            request.dataset_id, str(existing["version"])
                        )
                    ],
                }
            version = request.version or self._next_version(request.dataset_id)
            final = DatasetVersion(
                **{
                    **self._version_kwargs(provisional),
                    "version": version,
                }
            )
            members: list[DatasetMember] = [
                DatasetMember(
                    dataset_id=request.dataset_id,
                    version=version,
                    instrument_id=instrument_id,
                    exchange=exchange,
                    ordinal=ordinal,
                    bar_id=bar_id,
                    provider=provider,
                    observed_at=observed_at,
                    quality=quality_value,
                )
                for (
                    instrument_id,
                    exchange,
                    ordinal,
                    bar_id,
                    provider,
                    observed_at,
                    quality_value,
                ) in member_rows
            ]
            gaps = tuple(
                gap for item in assessments for gap in item.missing.gaps
            )
            selections = tuple(
                selection for item in assessments for selection in item.selections
            )
            append = self._store.create_version(
                final, members=members, gaps=gaps, selections=selections
            )
            self._store.update_build(
                build_id,
                status="completed",
                checkpoint={
                    "dataset_id": final.dataset_id,
                    "version": final.version,
                    "bar_count": final.bar_count,
                    "content_digest": final.content_digest,
                    "quality": final.quality.value,
                },
            )
            payload = final.to_dict()
            await self._publish(
                "novamarket.dataset.version.created",
                build_id=build_id,
                dataset_id=final.dataset_id,
                version=final.version,
                quality=final.quality.value,
                bar_count=final.bar_count,
                content_digest=final.content_digest,
            )
            if not final.complete:
                await self._publish(
                    "novamarket.dataset.validation.incomplete",
                    build_id=build_id,
                    dataset_id=final.dataset_id,
                    version=final.version,
                    quality=final.quality.value,
                    missing=final.missing.total_missing,
                )
            await self._publish(
                "novamarket.dataset.build.completed",
                build_id=build_id,
                dataset_id=final.dataset_id,
                version=final.version,
                duplicate=append.duplicate,
            )
            payload["duplicate"] = append.duplicate
            payload["build_id"] = build_id
            payload["acquisition"] = (
                acquisition_summary(list(reports.values())) if reports else {"instruments": 0}
            )
            payload["historical_datasets"] = [
                item.to_dict() for item in final.as_prediction_datasets()
            ]
            return payload
        except Exception as exc:
            self._store.update_build(
                build_id,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )
            await self._publish(
                "novamarket.dataset.build.failed",
                build_id=build_id,
                dataset_id=request.dataset_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

    @staticmethod
    def _request_kwargs(request: DatasetBuildRequest) -> dict[str, Any]:
        return {
            "dataset_id": request.dataset_id,
            "instruments": request.instruments,
            "resolution": request.resolution,
            "start_at": request.start_at,
            "end_at": request.end_at,
            "exchange": request.exchange,
            "providers": request.providers,
            "version": request.version,
            "fetch": request.fetch,
            "allow_partial": request.allow_partial,
            "incremental": request.incremental,
            "min_coverage_ratio": request.min_coverage_ratio,
            "adjustment_basis": request.adjustment_basis,
            "description": request.description,
            "tags": request.tags,
        }

    @staticmethod
    def _version_kwargs(version: DatasetVersion) -> dict[str, Any]:
        return {
            "dataset_id": version.dataset_id,
            "version": version.version,
            "created_at": version.created_at,
            "resolution": version.resolution,
            "start_at": version.start_at,
            "end_at": version.end_at,
            "instruments": version.instruments,
            "exchanges": version.exchanges,
            "providers": version.providers,
            "normalization_version": version.normalization_version,
            "price_basis": version.price_basis,
            "quality": version.quality,
            "complete": version.complete,
            "coverage": version.coverage,
            "validation": version.validation,
            "missing": version.missing,
            "segments": version.segments,
            "config": version.config,
            "acquisition": version.acquisition,
            "corporate_actions": version.corporate_actions,
            "limitations": version.limitations,
        }

    def _resolve_instruments(self, request: DatasetBuildRequest) -> tuple[Instrument, ...]:
        market_data = self._market_data
        if market_data is None:
            raise DatasetError("dataset builds require the existing market-data engine")
        resolved: list[Instrument] = []
        for instrument_id in request.instruments:
            try:
                instrument = market_data.get_instrument(instrument_id)
            except Exception as exc:  # noqa: BLE001 - existing domain errors
                raise DatasetValidationError(
                    f"instrument {instrument_id} is not registered in the market-data "
                    f"authority: {exc}"
                ) from exc
            if request.exchange and request.exchange not in (
                instrument.exchange.upper(),
                instrument.mic.upper(),
            ):
                raise DatasetValidationError(
                    f"instrument {instrument_id} trades on {instrument.exchange}, not "
                    f"the requested exchange {request.exchange}"
                )
            resolved.append(instrument)
        return tuple(resolved)

    def _read_all_bars(
        self,
        instrument_id: str,
        resolution: DataInterval,
        request: DatasetBuildRequest,
    ) -> tuple[OhlcvBar, ...]:
        market_data = self._market_data
        if market_data is None:
            raise DatasetError("dataset reads require the existing market-data engine")
        bars: list[OhlcvBar] = []
        offset = 0
        while True:
            page = market_data.bars(
                instrument_id,
                interval=resolution,
                start=request.start_at,
                end=request.end_at,
                limit=1_000,
                offset=offset,
                ascending=True,
            )
            items = tuple(getattr(page, "items", ()))
            bars.extend(items)
            if len(bars) > MAX_BARS_PER_INSTRUMENT:
                raise DatasetValidationError(
                    f"{instrument_id} exceeds the {MAX_BARS_PER_INSTRUMENT}-bar "
                    "bound for one dataset build"
                )
            total = int(getattr(page, "total", len(bars)))
            offset += len(items)
            if not items or offset >= total:
                break
        return tuple(bars)

    def _next_version(self, dataset_id: str) -> str:
        rows = self._store.versions(dataset_id, limit=5_000)["items"]
        highest = 0
        for row in rows:
            label = str(row.get("version", ""))
            if label.startswith("v") and label[1:].isdigit():
                highest = max(highest, int(label[1:]))
        return f"v{highest + 1}"

    # -- reads ----------------------------------------------------------------

    def _require_version(self, dataset_id: str, version: str | None) -> dict[str, Any]:
        selected = version or None
        if selected is None:
            latest = self._store.latest_version(dataset_id)
        else:
            latest = self._store.get_version(dataset_id, selected)
        if latest is None:
            raise DatasetNotFoundError(
                f"dataset {dataset_id}:{selected or 'latest'} does not exist"
            )
        return latest

    def metadata(
        self, dataset_id: str, version: str | None = None
    ) -> dict[str, Any]:
        return self._require_version(dataset_id, version)

    def versions(
        self, dataset_id: str | None = None, *, limit: int = 50, offset: int = 0
    ) -> dict[str, Any]:
        return self._store.versions(dataset_id, limit=limit, offset=offset)

    def builds(
        self,
        dataset_id: str | None = None,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """The build journal, including failed and interrupted runs."""
        return self._store.builds(dataset_id, limit=limit, offset=offset)

    def datasets(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        ids = self._store.dataset_ids()
        limit = max(1, min(int(limit), 5_000))
        offset = max(0, int(offset))
        selected = ids[offset : offset + limit]
        return {
            "items": [
                {
                    "dataset_id": dataset_id,
                    "latest": self._store.latest_version(dataset_id),
                }
                for dataset_id in selected
            ],
            "total": len(ids),
            "limit": limit,
            "offset": offset,
            "has_more": offset + len(selected) < len(ids),
        }

    def latest_version(self, dataset_id: str) -> dict[str, Any]:
        return self._require_version(dataset_id, None)

    def coverage(self, dataset_id: str, version: str | None = None) -> dict[str, Any]:
        payload = self._require_version(dataset_id, version)
        return {
            "dataset_id": payload["dataset_id"],
            "version": payload["version"],
            "resolution": payload["resolution"],
            "start_at": payload["start_at"],
            "end_at": payload["end_at"],
            "coverage": payload["coverage"],
            "segments": [
                {
                    "instrument_id": segment["instrument_id"],
                    "coverage": segment["coverage"],
                }
                for segment in payload.get("segments", [])
            ],
        }

    def quality(self, dataset_id: str, version: str | None = None) -> dict[str, Any]:
        payload = self._require_version(dataset_id, version)
        return {
            "dataset_id": payload["dataset_id"],
            "version": payload["version"],
            "quality": payload["quality"],
            "usable_for_training": payload["usable_for_training"],
            "complete": payload["complete"],
            "validation": payload["validation"],
            "segments": [
                {
                    "instrument_id": segment["instrument_id"],
                    "quality": segment["quality"],
                    "validation": segment["validation"],
                    "limitations": segment["missing"],
                }
                for segment in payload.get("segments", [])
            ],
        }

    def missing(
        self,
        dataset_id: str,
        version: str | None = None,
        *,
        instrument_id: str | None = None,
        kind: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        payload = self._require_version(dataset_id, version)
        if kind:
            try:
                GapKind(kind)
            except ValueError as exc:
                raise DatasetValidationError(f"unknown gap kind: {kind!r}") from exc
        page = self._store.gaps(
            payload["dataset_id"],
            payload["version"],
            instrument_id=instrument_id,
            kind=kind,
            limit=limit,
            offset=offset,
        )
        return {
            "dataset_id": payload["dataset_id"],
            "version": payload["version"],
            "summary": payload["missing"],
            **page,
        }

    def selections(
        self,
        dataset_id: str,
        version: str | None = None,
        *,
        instrument_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        payload = self._require_version(dataset_id, version)
        return {
            "dataset_id": payload["dataset_id"],
            "version": payload["version"],
            **self._store.selections(
                payload["dataset_id"],
                payload["version"],
                instrument_id=instrument_id,
                limit=limit,
                offset=offset,
            ),
        }

    def bars(
        self,
        dataset_id: str,
        version: str | None = None,
        *,
        instrument_id: str | None = None,
        exchange: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        quality: DataQuality | str | None = None,
        provider: str | None = None,
        limit: int = 500,
        offset: int = 0,
        ascending: bool = True,
        adjusted: bool = False,
        adjustment_basis: AdjustmentBasis | str | None = None,
    ) -> dict[str, Any]:
        """Read exact version membership, never "whatever is stored now"."""
        payload = self._require_version(dataset_id, version)
        selected_quality = None
        if quality is not None:
            try:
                selected_quality = DataQuality(quality).value
            except ValueError as exc:
                raise DatasetValidationError(
                    f"unknown observation quality: {quality!r}"
                ) from exc
        page = self._store.members(
            payload["dataset_id"],
            payload["version"],
            instrument_id=instrument_id,
            exchange=exchange,
            start=start,
            end=end,
            quality=selected_quality,
            provider=provider,
            limit=limit,
            offset=offset,
            ascending=ascending,
        )
        bar_ids = [member.bar_id for member in page.items]
        bar_map = self._market_data.store.get_bars(bar_ids) if self._market_data is not None else {}
        missing_ids = [bar_id for bar_id in bar_ids if bar_id not in bar_map]
        adjusted_map: dict[tuple[str, str], dict[str, Any]] = {}
        adjustment_limitations: list[str] = []
        unapplied_action_ids: list[str] = []
        basis: AdjustmentBasis | None = None
        price_basis = AdjustmentBasis.RAW.value
        if adjusted:
            try:
                basis = AdjustmentBasis(
                    adjustment_basis or AdjustmentBasis.SPLIT_ADJUSTED
                )
            except ValueError as exc:
                raise DatasetValidationError(
                    f"unknown adjustment basis: {adjustment_basis!r}"
                ) from exc
            if basis is AdjustmentBasis.PROVIDER_ADJUSTED:
                raise DatasetValidationError(
                    "no installed provider supplies a verified provider-adjusted "
                    "series; use raw, split_adjusted or total_return_adjusted"
                )
            price_basis = basis.value
        if adjusted and bar_map:
            assert basis is not None
            grouped: dict[str, list[OhlcvBar]] = {}
            for bar in bar_map.values():
                grouped.setdefault(bar.instrument_id, []).append(bar)
            for grouped_instrument, grouped_bars in grouped.items():
                actions = self._actions_for(
                    grouped_instrument, datetime.fromisoformat(payload["end_at"])
                )
                series = adjust_bars(
                    grouped_instrument, sorted(grouped_bars, key=lambda item: item.observed_at),
                    actions, basis=basis,
                )
                for limitation in series.limitations:
                    if limitation not in adjustment_limitations:
                        adjustment_limitations.append(limitation)
                for action_id in series.unapplied_action_ids:
                    if action_id not in unapplied_action_ids:
                        unapplied_action_ids.append(action_id)
                for item in series.bars:
                    adjusted_map[(grouped_instrument, item.observed_at)] = item.to_dict()
        items: list[dict[str, Any]] = []
        for member in page.items:
            bar = bar_map.get(member.bar_id)
            if bar is None:
                items.append(
                    {
                        "bar_id": member.bar_id,
                        "dataset_id": payload["dataset_id"],
                        "version": payload["version"],
                        "instrument_id": member.instrument_id,
                        "ordinal": member.ordinal,
                        "missing": True,
                        "detail": "the referenced bar is no longer in market_bars",
                    }
                )
                continue
            item = bar.to_dict()
            item.update(
                {
                    "dataset_id": payload["dataset_id"],
                    "version": payload["version"],
                    "exchange": member.exchange,
                    "ordinal": member.ordinal,
                    "missing": False,
                }
            )
            adjusted_key = (member.instrument_id, member.observed_at.isoformat())
            if adjusted and adjusted_key in adjusted_map:
                adjusted_item = adjusted_map[adjusted_key]
                item["raw"] = {
                    "open": item["open"],
                    "high": item["high"],
                    "low": item["low"],
                    "close": item["close"],
                    "volume": item["volume"],
                }
                item.update(
                    {
                        "open": adjusted_item["open"],
                        "high": adjusted_item["high"],
                        "low": adjusted_item["low"],
                        "close": adjusted_item["close"],
                        "adjustment_factor": adjusted_item["factor"],
                        "applied_action_ids": adjusted_item["applied_action_ids"],
                        "adjusted": True,
                        "price_basis": price_basis,
                    }
                )
            items.append(item)
        return {
            "dataset_id": payload["dataset_id"],
            "version": payload["version"],
            "resolution": payload["resolution"],
            "adjusted": adjusted,
            "price_basis": price_basis,
            "adjustment_limitations": adjustment_limitations,
            "unapplied_action_ids": unapplied_action_ids,
            "items": items,
            "total": page.total,
            "limit": page.limit,
            "offset": page.offset,
            "has_more": page.has_more,
            "next_offset": page.offset + len(items) if page.has_more else None,
            "missing_bar_ids": missing_ids,
        }

    def batch(
        self,
        dataset_id: str,
        version: str | None = None,
        *,
        instruments: Sequence[str] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit_per_instrument: int = 500,
        adjusted: bool = False,
    ) -> dict[str, Any]:
        payload = self._require_version(dataset_id, version)
        if isinstance(instruments, str):
            raise DatasetValidationError("instruments must be a sequence, not a string")
        selected = tuple(instruments or payload["instruments"])
        result: dict[str, Any] = {}
        for instrument_id in selected:
            result[instrument_id] = self.bars(
                payload["dataset_id"],
                payload["version"],
                instrument_id=instrument_id,
                start=start,
                end=end,
                limit=limit_per_instrument,
                adjusted=adjusted,
            )
        return {
            "dataset_id": payload["dataset_id"],
            "version": payload["version"],
            "resolution": payload["resolution"],
            "instruments": result,
            "total": sum(int(item["total"]) for item in result.values()),
        }

    def historical_datasets(
        self, dataset_id: str, version: str | None = None
    ) -> tuple[HistoricalMarketDataset, ...]:
        payload = self._require_version(dataset_id, version)
        result: list[HistoricalMarketDataset] = []
        for segment in payload.get("segments", []):
            first = segment.get("first_at")
            last = segment.get("last_at")
            if not first or not last or int(segment["bar_count"]) == 0:
                continue
            quality = {
                "valid": DataQuality.VALID,
                "valid_with_warnings": DataQuality.WARNING,
                "provider_conflicted": DataQuality.SUSPECT,
                "invalid": DataQuality.INVALID,
            }.get(str(segment["quality"]), DataQuality.UNAVAILABLE)
            result.append(
                HistoricalMarketDataset(
                    dataset_id=payload["dataset_id"],
                    version=payload["version"],
                    instrument_id=segment["instrument_id"],
                    resolution=payload["resolution"],
                    start_at=datetime.fromisoformat(first),
                    end_at=datetime.fromisoformat(last),
                    bar_count=int(segment["bar_count"]),
                    source_snapshot_ids=(
                        f"dataset:{payload['dataset_id']}:{payload['version']}",
                    ),
                    quality=quality,
                    provenance={
                        "content_digest": payload["content_digest"],
                        "normalization_version": payload["normalization_version"],
                        "price_basis": payload["price_basis"],
                        "providers": segment["providers"],
                        "coverage_ratio": segment["coverage"]["coverage_ratio"],
                    },
                )
            )
        return tuple(result)

    # -- corporate actions and annotations ------------------------------------

    def register_corporate_action(
        self, action: CorporateAction | Mapping[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(action, CorporateAction):
            action = CorporateAction(**dict(action))
        result = self._store.record_corporate_action(action)
        return {
            "status": result.status.value,
            "action_id": action.action_id,
            "detail": result.detail,
            "action": action.to_dict(),
        }

    def corporate_actions(
        self,
        *,
        instrument_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        verified_only: bool = False,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self._store.corporate_actions(
            instrument_id=instrument_id,
            start=start,
            end=end,
            verified_only=verified_only,
            limit=limit,
            offset=offset,
        )

    def _actions_for(
        self, instrument_id: str, end: datetime | None = None
    ) -> tuple[CorporateAction, ...]:
        rows = self._store.corporate_actions(
            instrument_id=instrument_id, end=end, limit=5_000
        )["items"]
        actions: list[CorporateAction] = []
        for row in rows:
            actions.append(
                CorporateAction(
                    instrument_id=row["instrument_id"],
                    kind=row["kind"],
                    effective_at=datetime.fromisoformat(row["effective_at"]),
                    announced_at=(
                        datetime.fromisoformat(row["announced_at"])
                        if row.get("announced_at")
                        else None
                    ),
                    source=row["source"],
                    verified=bool(row["verified"]),
                    price_factor=row.get("price_factor"),
                    amount=row.get("amount"),
                    currency=row.get("currency", ""),
                    metadata=row.get("metadata", {}),
                    action_id=row["action_id"],
                )
            )
        return tuple(actions)

    def adjusted_bars(
        self,
        instrument_id: str,
        *,
        start: datetime,
        end: datetime,
        resolution: DataInterval | str = DataInterval.DAY_1,
        basis: AdjustmentBasis | str = AdjustmentBasis.SPLIT_ADJUSTED,
    ) -> dict[str, Any]:
        """Derived adjusted view over stored bars; raw rows are never rewritten."""
        if self._market_data is None:
            raise DatasetError("adjusted series require the market-data authority")
        try:
            interval = DataInterval(resolution)
        except ValueError as exc:
            raise DatasetValidationError(
                f"unknown resolution: {resolution!r}"
            ) from exc
        bars: list[OhlcvBar] = []
        offset = 0
        while True:
            page = self._market_data.bars(
                instrument_id,
                interval=interval,
                start=start,
                end=end,
                limit=1_000,
                offset=offset,
                ascending=True,
            )
            items = tuple(getattr(page, "items", ()))
            bars.extend(items)
            offset += len(items)
            if not items or offset >= int(getattr(page, "total", len(bars))):
                break
        series = adjust_bars(
            instrument_id,
            bars,
            self._actions_for(instrument_id, end),
            basis=basis,
        )
        return series.to_dict()

    def register_annotation(
        self, annotation: MarketAnnotation | Mapping[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(annotation, MarketAnnotation):
            annotation = MarketAnnotation(**dict(annotation))
        result = self._store.record_annotation(annotation)
        return {
            "status": result.status.value,
            "annotation_id": annotation.annotation_id,
            "detail": result.detail,
            "annotation": annotation.to_dict(),
        }

    def annotations(
        self,
        *,
        instrument_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        return [
            item.to_dict()
            for item in self._store.annotations(
                instrument_id=instrument_id,
                start=start,
                end=end,
                limit=limit,
                offset=offset,
            )
        ]

    # -- training -------------------------------------------------------------

    def build_training(
        self,
        dataset_id: str,
        version: str | None = None,
        *,
        horizon: ForecastHorizon | str = ForecastHorizon.DAY_1,
        window_bars: int = 60,
        train_ratio: float = 0.7,
        validation_ratio: float = 0.15,
        instruments: Sequence[str] | None = None,
        max_samples: int = 200_000,
    ) -> dict[str, Any]:
        payload = self._require_version(dataset_id, version)
        if not payload["usable_for_training"]:
            raise DatasetValidationError(
                f"dataset {payload['dataset_id']}:{payload['version']} has quality "
                f"{payload['quality']} and cannot enter a trusted training set"
            )
        market_data = self._market_data
        if market_data is None:
            raise DatasetError("training construction requires the market-data authority")
        if isinstance(instruments, str):
            raise DatasetValidationError("instruments must be a sequence, not a string")
        selected = tuple(instruments or payload["instruments"])
        unknown = [item for item in selected if item not in payload["instruments"]]
        if unknown:
            raise DatasetValidationError(
                "training instruments are not members of this dataset version: "
                + ", ".join(unknown)
            )
        bars_by_instrument: dict[str, tuple[OhlcvBar, ...]] = {}
        calendars: dict[str, Any] = {}
        for instrument_id in selected:
            rows = self._store.all_member_rows(
                payload["dataset_id"], payload["version"], instrument_id
            )
            bar_map = market_data.store.get_bars([row["bar_id"] for row in rows])
            missing = [row["bar_id"] for row in rows if row["bar_id"] not in bar_map]
            if missing:
                raise DatasetValidationError(
                    f"{len(missing)} referenced bar(s) for {instrument_id} are no "
                    "longer readable; the dataset version cannot be trusted for training"
                )
            bars = tuple(
                sorted(
                    (
                        bar_map[row["bar_id"]]
                        for row in rows
                        if row["bar_id"] in bar_map
                    ),
                    key=lambda item: item.observed_at,
                )
            )
            if not bars:
                continue
            bars_by_instrument[instrument_id] = bars
            try:
                calendars[instrument_id] = calendar_for(
                    market_data.get_instrument(instrument_id)
                )
            except Exception:  # noqa: BLE001 - calendar is optional evidence
                calendars[instrument_id] = None
        if not bars_by_instrument:
            raise DatasetValidationError(
                "the selected instruments have no readable membership bars"
            )
        training, samples = build_training_dataset(
            dataset_id=payload["dataset_id"],
            dataset_version=payload["version"],
            resolution=DataInterval(payload["resolution"]),
            horizon=horizon,
            created_at=self._clock(),
            bars_by_instrument=bars_by_instrument,
            calendars=calendars,
            window_bars=window_bars,
            train_ratio=train_ratio,
            validation_ratio=validation_ratio,
            max_samples=max_samples,
            limitations=payload.get("limitations", ()),
        )
        append = self._store.create_training(training, samples)
        stored = self._store.get_training(training.training_id) if append.duplicate else None
        result = stored or training.to_dict()
        result["duplicate"] = append.duplicate
        result["dataset_content_digest"] = payload["content_digest"]
        result["source_dataset"] = {
            "dataset_id": payload["dataset_id"],
            "version": payload["version"],
            "quality": payload["quality"],
            "content_digest": payload["content_digest"],
        }
        return result

    def training(self, training_id: str) -> dict[str, Any]:
        payload = self._store.get_training(training_id)
        if payload is None:
            raise DatasetNotFoundError(f"training dataset {training_id} does not exist")
        return payload

    def training_versions(
        self,
        *,
        dataset_id: str | None = None,
        dataset_version: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        return self._store.training_versions(
            dataset_id=dataset_id,
            dataset_version=dataset_version,
            limit=limit,
            offset=offset,
        )

    def training_samples(
        self,
        training_id: str,
        *,
        split: str | None = None,
        instrument_id: str | None = None,
        limit: int = 500,
        offset: int = 0,
    ) -> dict[str, Any]:
        self.training(training_id)
        return self._store.training_samples(
            training_id,
            split=split,
            instrument_id=instrument_id,
            limit=limit,
            offset=offset,
        )

    def verify_training(self, training_id: str) -> dict[str, Any]:
        payload = self.training(training_id)
        samples = self._store.all_training_samples(training_id)
        ok, problems = verify_stored_training(payload, samples)
        membership_ok = self._market_data is not None
        if self._market_data is None:
            problems = problems + (
                "market data is not attached; membership could not be verified",
            )
        else:
            dataset_payload = self._store.get_version(
                str(payload["dataset_id"]), str(payload["dataset_version"])
            )
            if dataset_payload is None:
                ok = False
                membership_ok = False
                problems = problems + ("the source dataset version is missing",)
            else:
                rows = self._store.all_member_rows(
                    str(payload["dataset_id"]), str(payload["dataset_version"])
                )
                member_bar_ids = {str(row["bar_id"]) for row in rows}
                for sample in samples:
                    if (
                        str(sample.get("decision_bar_id")) not in member_bar_ids
                        or str(sample.get("target_bar_id")) not in member_bar_ids
                    ):
                        membership_ok = False
                        problems = problems + (
                            "a sample references a bar outside its dataset version",
                        )
                        break
                    window_ids = sample.get("window_bar_ids", ())
                    if any(
                        str(bar_id) not in member_bar_ids
                        for bar_id in window_ids
                    ):
                        membership_ok = False
                        problems = problems + (
                            "a sample window references a bar outside its dataset version",
                        )
                        break
        return {
            "training_id": training_id,
            "ok": ok and membership_ok,
            "problems": list(problems),
            "sample_count": len(samples),
            "membership_verified": membership_ok,
            "chronological": True,
            "test_used_for_training": False,
        }

    # -- status and contracts -------------------------------------------------

    def status(self) -> dict[str, Any]:
        stats = self._store.stats()
        return {
            "phase": PI02_PHASE,
            "implemented": True,
            "initialised": True,
            "contract_version": DATASET_CONTRACT_VERSION,
            "normalization_version": self.normalization_version,
            "schema_version": self._store.schema_version,
            "market_data_attached": self.market_data_attached,
            "store": {
                "path": stats["path"],
                "schema_version": stats["schema_version"],
                "counts": stats["counts"],
                "datasets": stats["datasets"],
            },
            "last_event_error": self._last_event_error,
            "authorities": {
                "market_data": "MarketDataEngine/MarketDataStore (NM-02)",
                "risk": "NM-08",
                "execution": "NM-10",
                "dataset_authorizes_action": False,
            },
        }

    def contract(self) -> dict[str, Any]:
        capabilities: list[dict[str, Any]] = []
        if self._market_data is not None:
            try:
                capabilities = list(self._market_data.registry.capability_matrix())
            except Exception:  # noqa: BLE001 - contracts must render without providers
                capabilities = []
        return {
            "phase": PI02_PHASE,
            "contract_version": DATASET_CONTRACT_VERSION,
            "normalization_version": self.normalization_version,
            "resolutions": [
                interval.value
                for interval in DataInterval
                if interval is not DataInterval.SPOT
            ],
            "quality_states": [item.value for item in DatasetQuality],
            "gap_kinds": [item.value for item in GapKind],
            "training_horizons": {
                horizon.value: spec.to_dict()
                for horizon, spec in HORIZON_SPECS.items()
            },
            "storage": {
                "database": "existing NovaMarket SQLite file",
                "tables": [
                    "dataset_versions",
                    "dataset_segments",
                    "dataset_members",
                    "dataset_selections",
                    "dataset_gaps",
                    "dataset_corporate_actions",
                    "dataset_annotations",
                    "dataset_builds",
                    "training_versions",
                    "training_samples",
                    "training_splits",
                ],
                "members_reference_market_bars": True,
                "immutable_versions": True,
            },
            "normalization": {
                "instrument_identity": "MIC:SYMBOL from the canonical catalogue",
                "exchange_identity": "display exchange plus ISO 10383 MIC",
                "timestamps": "timezone-aware, normalized to UTC by the market-data models",
                "money": "Decimal only; binary floats refused",
                "volume": "provider units preserved; null when the provider publishes none",
                "price_basis": "raw membership; adjusted series derived on retrieval",
            },
            "providers": capabilities,
            "calendars": calendar_contract(),
            "training": {
                "chronological": True,
                "purged_target_overlaps": True,
                "features_at_or_before_decision": True,
                "targets_strictly_after_decision": True,
                "test_used_for_training": False,
                "imputation": "never in trusted datasets",
            },
            "limitations": [
                "Intraday depth follows each free provider; no paid history is used.",
                "Corporate-action ingestion is explicit; the existing adapters do not "
                "expose a verified events feed, so unrecorded actions are not invented.",
                "Calendar coverage is explicit per year; outside it, absence is unverified.",
                "Volume units are provider-specific and are not converted.",
            ],
            "authorities": {
                "risk": "NM-08",
                "execution": "NM-10",
                "dataset_authorizes_action": False,
            },
        }

    def check(self) -> dict[str, Any]:
        return self._store.check()

    def close(self) -> None:
        self._store.close()


__all__ = ["HistoricalDatasetService"]
