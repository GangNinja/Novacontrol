"""D4: one routing report asks the runtime once per fact, not once per question.

The integration audit (``docs/INTEGRATION_PERFORMANCE_AUDIT.md`` §7, defect D4)
measured ``GET /intelligence`` at **22.1 s** on a machine with no local runtime.
The cause was not the hardware probe (0.07 s) and not the report's work: the
routing report asks five selection questions, and EVERY question probed the
runtime twice — a model list and a residency check — each waiting out its own
socket timeout. Ten bounded HTTP probes to answer one status read.

The fix is a request-scoped memo, not a cache. ``ModelProbeSnapshot`` is created
by ``_model_routing_report``, shared by all five questions, and dropped when the
report returns. Nothing is remembered across requests, so a model pulled after
one report is still discovered by the very next one — the property the
deliberately-uncached miss exists to keep.

These tests are the contract: the probe count per report, the five distinct
selections, freshness across reports, and honest behaviour when the runtime is
unreachable or a probe fails (a failure is remembered for the call, so a down
runtime is not asked ten times to learn it once).

They use the shared ``FakeProvider`` from ``test_model_phase7`` — subclassed here
so the many tests that already use it keep exactly the fixture they had — and
never depend on a runtime being installed.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any
from unittest import mock

from novacontrol.models import ModelManager, ModelProbeSnapshot, ModelRegistry
from test_model_phase7 import CHAT, GIB, VISION, FakeMonitor, FakeProvider
from test_model_pipeline_phase7 import _PipelineCase
from test_web_api import _IsolatedApiTestCase

#: The five questions the routing report answers, in the order it asks them.
REPORT_LABELS = ("reasoning", "coding", "vision", "vision_tools", "strongest")

#: Models NOTHING declares, so a selection can only find them by discovering them
#: on the runtime. This is the "pulled five minutes ago" case.
NEW_VLM = "brand-new-vl:3b"
PLAIN = "plain-chat:1b"


class _CountingProvider(FakeProvider):
    """A runtime that counts its own probes, and can be told to fail them.

    Subclassed rather than edited into ``FakeProvider`` on purpose: that fixture
    is shared by the whole Phase 7 suite, and a probe counter added there would
    change a fixture many tests already depend on to prove something else.
    """

    name = "counting"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.model_list_calls = 0
        self.resident_calls = 0
        #: Set to make the next probes raise, the way an unreachable runtime does.
        self.model_list_error: Exception | None = None
        self.resident_error: Exception | None = None

    def list_models(self) -> tuple[str, ...]:
        self.model_list_calls += 1
        if self.model_list_error is not None:
            raise self.model_list_error
        return FakeProvider.list_models(self)

    def resident_models(self) -> tuple[tuple[str, int], ...] | None:
        self.resident_calls += 1
        if self.resident_error is not None:
            raise self.resident_error
        return FakeProvider.resident_models(self)

    @property
    def probes(self) -> int:
        """Every runtime probe this provider has served, of both kinds."""
        return self.model_list_calls + self.resident_calls


def _two_model_runtime() -> _CountingProvider:
    """A runtime holding exactly the two models the flows need."""
    return _CountingProvider(
        installed={CHAT: 6 * GIB, VISION: 3 * GIB},
        capabilities={
            CHAT: ("completion", "tools", "thinking"),
            VISION: ("completion", "vision"),
        },
    )


class SnapshotMemoTests(unittest.TestCase):
    """The memo itself: one probe per fact, and nothing shared between instances."""

    def test_a_snapshot_probes_each_fact_once_however_often_it_is_asked(self) -> None:
        models = mock.Mock(return_value=("a:1b",))
        resident = mock.Mock(return_value=(("a:1b", GIB),))
        snapshot = ModelProbeSnapshot()
        for _ in range(4):
            self.assertEqual(snapshot.models(models), ("a:1b",))
            self.assertEqual(snapshot.resident(resident), (("a:1b", GIB),))
        self.assertEqual(models.call_count, 1)
        self.assertEqual(resident.call_count, 1)
        self.assertEqual(snapshot.probes, 2)

    def test_an_untouched_snapshot_has_probed_nothing(self) -> None:
        self.assertEqual(ModelProbeSnapshot().probes, 0)

    def test_an_unanswerable_residency_is_remembered_as_such_not_as_empty(self) -> None:
        """``None`` means "could not ask" and must not decay into "nothing there".

        Only a real answer may drive an eviction, so a failed probe has to stay
        distinguishable from an empty runtime for every question in the call.
        """
        probe = mock.Mock(return_value=None)
        snapshot = ModelProbeSnapshot()
        self.assertIsNone(snapshot.resident(probe))
        self.assertIsNone(snapshot.resident(probe))
        self.assertEqual(probe.call_count, 1)

    def test_two_snapshots_never_share_what_they_learned(self) -> None:
        first = ModelProbeSnapshot()
        second = ModelProbeSnapshot()
        self.assertEqual(first.models(lambda: ("old:1b",)), ("old:1b",))
        # The runtime changes its answer between the two callers.
        self.assertEqual(second.models(lambda: ("new:1b",)), ("new:1b",))
        # The first caller's memo is its own; it did not adopt the second's answer.
        self.assertEqual(first.models(lambda: ("ignored:1b",)), ("old:1b",))
        self.assertEqual(first.probes, 1)
        self.assertEqual(second.probes, 1)


class RoutingReportProbeTests(_PipelineCase):
    """The report on the real application object, with a counting runtime."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.provider = _two_model_runtime()
        self.app.model_manager = ModelManager(self.provider, monitor=FakeMonitor())

    def test_a_report_answers_all_five_questions_with_distinct_work(self) -> None:
        report = self.app._model_routing_report()
        self.assertEqual(tuple(report), REPORT_LABELS)
        # The capability each question states is what selects the model: a
        # reasoning question gets the chat model, a vision question the VLM.
        self.assertEqual(report["reasoning"]["model"], CHAT)
        self.assertEqual(report["vision"]["model"], VISION)
        self.assertEqual(report["vision"]["route"], "local")
        # Distinct work, not five copies of one answer.
        self.assertNotEqual(report["reasoning"]["model"], report["vision"]["model"])

    def test_a_report_probes_the_runtime_once_for_each_fact(self) -> None:
        """Ten probes became two, and the two are the distinct facts."""
        self.app._model_routing_report()
        self.assertEqual(self.provider.model_list_calls, 1)
        self.assertEqual(self.provider.resident_calls, 1)
        self.assertEqual(self.provider.probes, 2)

    def test_the_probe_count_does_not_grow_with_the_question_count(self) -> None:
        """The regression this fix exists for: cost independent of questions."""
        self.app._model_routing_report()
        after_one_report = self.provider.probes
        self.app._model_routing_report()
        # A second report is a second REQUEST, so it probes again — twice more,
        # not ten more.
        self.assertEqual(self.provider.probes, after_one_report + 2)

    def test_without_a_snapshot_each_question_probes_the_runtime_again(self) -> None:
        """The old cost is preserved for callers that ask one question at a time.

        ``snapshot`` is opt-in: a single selection outside a report keeps the
        freshness (and the cost) it always had, which is why this fix changes no
        existing caller's behaviour.
        """
        for _ in range(len(REPORT_LABELS)):
            self.app.model_manager.select_for_request(needs_reasoning=True)
        self.assertEqual(self.provider.model_list_calls, len(REPORT_LABELS))
        self.assertEqual(self.provider.resident_calls, len(REPORT_LABELS))

    def test_two_reports_actively_exclude_each_other_from_each_others_snapshot(self) -> None:
        """Each report builds its OWN memo, so a stale answer cannot cross over."""
        seen: list[Any] = []
        original = self.app.model_manager.select_for_request

        def spy(*args: Any, **kwargs: Any) -> Any:
            seen.append(kwargs.get("snapshot"))
            return original(*args, **kwargs)

        with mock.patch.object(self.app.model_manager, "select_for_request", spy):
            first = self.app._model_routing_report()
            second = self.app._model_routing_report()

        self.assertEqual(len(seen), 2 * len(REPORT_LABELS))
        first_call, second_call = seen[: len(REPORT_LABELS)], seen[len(REPORT_LABELS) :]
        # One snapshot shared by every question in a report...
        self.assertEqual(len({id(item) for item in first_call}), 1)
        self.assertEqual(len({id(item) for item in second_call}), 1)
        # ...and never the same object across two reports. Holding the
        # references keeps the ids meaningful (a dropped object's id can be
        # reused by the next allocation).
        self.assertIsNot(first_call[0], second_call[0])
        self.assertEqual(len(first), len(REPORT_LABELS))
        self.assertEqual(len(second), len(REPORT_LABELS))

    async def test_two_reports_running_at_once_share_nothing(self) -> None:
        """Concurrency is the case a single-threaded check cannot see.

        Two reports really running in two worker threads at the same time must
        each probe the runtime for themselves. If a snapshot leaked between
        them, one report would answer from the other's findings and the probe
        total would collapse to two instead of four — which is exactly what this
        asserts against.
        """
        first, second = await asyncio.gather(
            asyncio.to_thread(self.app._model_routing_report),
            asyncio.to_thread(self.app._model_routing_report),
        )
        for report in (first, second):
            self.assertEqual(tuple(report), REPORT_LABELS)
            self.assertEqual(report["reasoning"]["model"], CHAT)
            self.assertEqual(report["vision"]["model"], VISION)
        # Each report paid for its own two probes.
        self.assertEqual(self.provider.probes, 4)

    def test_a_report_never_lets_one_failing_question_lose_the_others(self) -> None:
        """The readout must never throw — a broken question is an empty row."""
        report = self.app._model_routing_report()
        self.assertEqual(len(report), len(REPORT_LABELS))
        for row in report.values():
            self.assertIn("route", row)

    def test_an_unreachable_runtime_is_reported_honestly_and_never_raises(self) -> None:
        """A runtime nobody can reach is answered from nothing, and said so.

        A registry with no declarations on purpose: with declarations present
        the pool falls back to them (documented behaviour, and the reason a test
        can select against a declared capability), which would hide what this
        test is about — that an unreachable runtime is REPORTED rather than
        raising, and that its unreachability is not mistaken for an answer.
        """
        provider = _CountingProvider(installed={}, reachable=False)
        self.app.model_manager = ModelManager(
            provider,
            registry=ModelRegistry(with_defaults=False),
            monitor=FakeMonitor(),
        )
        report = self.app._model_routing_report()
        self.assertEqual(len(report), len(REPORT_LABELS))
        for label in REPORT_LABELS:
            self.assertEqual(report[label]["route"], "none", label)
            self.assertEqual(report[label]["model"], "", label)
        # Still bounded: the whole report asked the unreachable runtime twice,
        # not ten times. Counted here, before the extra read below adds one.
        self.assertEqual(provider.probes, 2)
        # The unreachability is stated as such, rather than left to be inferred
        # from an empty pool. This read is a residency check of its own, which is
        # why the figure moves by exactly one.
        self.assertIs(self.app.model_manager.runtime_status().reachable, False)
        self.assertEqual(provider.probes, 3)

    def test_a_probe_that_times_out_is_answered_once_per_fact(self) -> None:
        """A timeout is a bounded failure, and it is not retried per question."""
        self.provider.model_list_error = TimeoutError(
            "the runtime did not answer within its socket timeout"
        )
        report = self.app._model_routing_report()
        self.assertEqual(len(report), len(REPORT_LABELS))
        self.assertEqual(self.provider.model_list_calls, 1)
        self.assertEqual(self.provider.resident_calls, 1)

    def test_a_residency_probe_that_fails_is_handled_like_an_unanswerable_one(self) -> None:
        self.provider.resident_error = OSError("connection refused")
        report = self.app._model_routing_report()
        self.assertEqual(len(report), len(REPORT_LABELS))
        self.assertEqual(self.provider.resident_calls, 1)
        self.assertEqual(report["reasoning"]["route"], "local")


class FreshnessAcrossReportsTests(_PipelineCase):
    """No persistent negative cache: the next report sees what changed.

    An empty registry and an empty runtime, so nothing is declared and the only
    way a model can be selected is by actually finding it on the runtime.
    """

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.provider = _CountingProvider(installed={})
        self.app.model_manager = ModelManager(
            self.provider,
            registry=ModelRegistry(with_defaults=False),
            monitor=FakeMonitor(),
        )

    def test_a_model_pulled_later_is_found_without_a_restart(self) -> None:
        first = self.app._model_routing_report()
        for label in REPORT_LABELS:
            self.assertEqual(first[label]["route"], "none", label)

        # The runtime gains a model — "pulled five minutes ago", nothing declared.
        self.provider.installed[NEW_VLM] = 2 * GIB
        self.provider.reported[NEW_VLM] = ("completion", "vision")

        second = self.app._model_routing_report()
        self.assertEqual(second["vision"]["route"], "local")
        self.assertEqual(second["vision"]["model"], NEW_VLM)
        # A model that was ABSENT is not remembered as absent.
        self.assertNotEqual(second["vision"]["route"], first["vision"]["route"])

    def test_a_model_that_disappears_is_not_served_from_an_earlier_report(self) -> None:
        self.provider.installed[NEW_VLM] = 2 * GIB
        self.provider.reported[NEW_VLM] = ("completion", "vision")
        self.assertEqual(self.app._model_routing_report()["vision"]["model"], NEW_VLM)

        # It is gone. Another model is still installed, so the pool is the
        # runtime's CURRENT list rather than the registry's memory of every
        # model it has ever seen.
        self.provider.installed.pop(NEW_VLM)
        self.provider.installed[PLAIN] = 1 * GIB
        self.provider.reported[PLAIN] = ("completion",)
        self.assertEqual(self.app._model_routing_report()["vision"]["route"], "none")


class IntelligenceEndpointTests(_IsolatedApiTestCase):
    """The endpoint the 22.1 s was measured on, on the real route."""

    def setUp(self) -> None:
        super().setUp()
        self.provider = _two_model_runtime()
        self.assertIsNotNone(self._nova)
        self._nova.model_manager = ModelManager(self.provider, monitor=FakeMonitor())

    def test_the_endpoint_answers_all_five_scenarios_within_a_bounded_probe_budget(self) -> None:
        response = self._client.get("/intelligence")
        self.assertEqual(response.status_code, 200)
        selection = response.json()["models"]["selection"]
        self.assertEqual(tuple(selection), REPORT_LABELS)
        self.assertEqual(selection["vision"]["model"], VISION)
        # ONE probe of each fact for the ENTIRE request: the report's five
        # questions share a model list, and the report and the runtime block
        # beside it share a residency check. Eleven probes became two.
        self.assertEqual(self.provider.model_list_calls, 1)
        self.assertEqual(self.provider.resident_calls, 1)
        self.assertEqual(self.provider.probes, 2)

    def test_an_unreachable_runtime_still_answers_the_endpoint(self) -> None:
        provider = _CountingProvider(installed={}, reachable=False)
        self._nova.model_manager = ModelManager(
            provider,
            registry=ModelRegistry(with_defaults=False),
            monitor=FakeMonitor(),
        )
        response = self._client.get("/intelligence")
        self.assertEqual(response.status_code, 200)
        selection = response.json()["models"]["selection"]
        self.assertEqual(tuple(selection), REPORT_LABELS)
        for label in REPORT_LABELS:
            self.assertEqual(selection[label]["route"], "none", label)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
