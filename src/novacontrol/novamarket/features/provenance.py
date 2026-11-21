"""Declared event-feature usage for a PI-04 feature set.

PI-24's event-aware comparison needs a label only the forecast itself can give:
did the model actually consume news/event features?  Event proximity is not that
label -- in a busy period almost every forecast sits near some event.

The evidence already exists on the feature set: ``lineage["news_provenance"]``
lists the eligible events that were used, and the ``news_*`` features carry a
computed status.  This module reads that lineage so a forecast can declare

* ``event_aware`` -- true only when computed event features reached the model,
* ``event_ids`` -- the radar/event ids behind those features,
* ``event_feature_set_version`` -- the feature-set version those columns belong
  to.

A model whose persisted schema has no ``news_*`` column (PI-05 statistical
models) consumes no event feature and must declare ``event_aware: False`` even
when events were supplied.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from novacontrol.novamarket.features.definitions import FEATURE_SPECS
from novacontrol.novamarket.features.models import (
    FeatureFamily,
    FeatureSet,
    FeatureStatus,
)

#: Every feature the PI-04 engine computes from event evidence.
EVENT_FEATURE_NAMES = frozenset(
    spec.name
    for spec in FEATURE_SPECS.values()
    if spec.family is FeatureFamily.NEWS_EVENT
)

_COMPUTED = {FeatureStatus.VALUE.value, FeatureStatus.ZERO.value}


def _event_id(row: Mapping[str, Any]) -> str:
    for key in ("radar_event_id", "event_id", "evidence_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return ""


def event_feature_usage(
    feature_set: FeatureSet | None,
    feature_names: Sequence[str] = (),
) -> dict[str, Any]:
    """What ``feature_set`` can honestly declare, in forecast metadata form.

    ``feature_names`` is the consuming model's persisted schema.  When it is
    given and contains no ``news_*`` column, the model cannot have used event
    features, so the declaration is an explicit ``False`` with no ids.  An empty
    ``feature_names`` means the caller is the feature contract itself.
    """

    if feature_set is None:
        return {
            "event_aware": False,
            "event_ids": [],
            "event_feature_set_version": "",
        }
    lineage = feature_set.lineage if isinstance(feature_set.lineage, Mapping) else {}
    raw = lineage.get("news_provenance")
    rows = (
        [row for row in raw or () if isinstance(row, Mapping)]
        if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes))
        else []
    )
    consumed = [name for name in feature_names if name in EVENT_FEATURE_NAMES]
    if feature_names and not consumed:
        return {
            "event_aware": False,
            "event_ids": [],
            "event_feature_set_version": feature_set.version,
        }
    candidates = consumed or sorted(EVENT_FEATURE_NAMES)
    computed = [
        name
        for name in candidates
        if str(feature_set.statuses.get(name, "")) in _COMPUTED
    ]
    ids = sorted({event_id for row in rows if (event_id := _event_id(row))})
    aware = bool(rows) and bool(computed)
    return {
        "event_aware": aware,
        "event_ids": ids if aware else [],
        "event_feature_set_version": feature_set.version,
        "event_feature_names": sorted(computed) if aware else [],
    }


__all__ = ["EVENT_FEATURE_NAMES", "event_feature_usage"]
