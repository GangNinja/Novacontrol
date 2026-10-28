"""NF-09: typed visual interpretation, capability truth and safe integration.

The model is a stub here on purpose: these tests pin the CONTRACT (schema,
provenance, stale/clipped refusal, timeouts, conflicts, capability honesty and
the fact that model output can never act). Live inference is a separate,
explicitly-reported acceptance run — a mock is not evidence that a model saw
anything.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from novacontrol.desktop.input import WindowGeometry
from novacontrol.novaforge import (
    VISUAL_INTERPRETATION_VERSION,
    GameObservation,
    HarnessReadyPolicy,
    NovaForgeManager,
    ObservationBuilder,
    VisionDescriber,
    build_interpretation,
    extract_json_object,
    measured_conflicts,
    summarise,
)

try:  # the HTTP tests are skipped cleanly when httpx is absent
    from fastapi.testclient import TestClient  # noqa: F401

    _HAS_HTTP = True
except Exception:  # pragma: no cover - optional dev dependency
    _HAS_HTTP = False


VALID_ANSWER = json.dumps(
    {
        "title": "NC FORGE HARNESS",
        "description": "A small window showing the word READY.",
        "detected_text": ["NC FORGE HARNESS", "READY"],
        "ui_elements": [{"label": "READY", "kind": "label", "bounds": {"x": 4, "y": 6, "width": 40, "height": 12}}],
        "text_visible": True,
    }
)

FRAME = {
    "path": "C:/frames/000001.png",
    "captured_at": "2026-10-21T10:00:00+00:00",
    "age_seconds": 0.2,
    "fresh": True,
    "clipped": False,
    "width": 1000,
    "height": 600,
}
TARGET = {"handle": 0x1234, "pid": 4242, "title": "NC FORGE HARNESS"}


def interpretation(answer: str = VALID_ANSWER, **overrides: Any):
    payload = {
        "frame": FRAME,
        "target": TARGET,
        "observed_at": "2026-10-21T10:00:00+00:00",
        "provider": "llm:stub",
        "model": "stub-vl",
        "measured_title": "NC FORGE HARNESS",
        "ocr_text": "NC FORGE HARNESS READY",
        "ocr_engine": "windows.media.ocr",
        **overrides,
    }
    return build_interpretation(answer, **payload)


class _Provider:
    """A vision provider that records what it was asked about."""

    def __init__(
        self,
        answer: Any = VALID_ANSWER,
        *,
        available: bool = True,
        delay: float = 0.0,
        raises: Exception | None = None,
        model: str = "stub-vl",
        name: str = "llm:stub",
    ) -> None:
        self.answer = answer
        self._available = available
        self.delay = delay
        self.raises = raises
        self.model = model
        self.name = name
        self.calls: list[tuple[str, str]] = []

    @property
    def available(self) -> bool:
        return self._available

    async def see(self, *, prompt: str, image_path: str) -> Any:
        self.calls.append((prompt, image_path))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return self.answer


def geometry(*, title: str = "NC FORGE HARNESS") -> WindowGeometry:
    return WindowGeometry(
        handle=0x1234,
        title=title,
        bounds=(100, 50, 1100, 650),
        client=(0, 0, 1000, 600),
        pid=4242,
        scale=1.0,
        foreground=True,
    )


class _Reader:
    name = "fake.ocr"
    confidence = None

    def __init__(self, text: str = "NC FORGE HARNESS READY", *, available: bool = True) -> None:
        self.text = text
        self._available = available
        self.calls: list[str] = []

    @property
    def available(self) -> bool:
        return self._available

    async def read(self, source: str) -> Any:
        self.calls.append(source)
        return [{"text": self.text, "line": 0, "x": 1, "y": 1, "width": 5, "height": 5}]


class _BuilderFixture(unittest.IsolatedAsyncioTestCase):
    """A builder over a real file, a fake window and a stub model."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.frame_path = Path(self._tmp.name) / "frame.png"
        self.frame_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)

    def builder(
        self,
        provider: _Provider | None,
        *,
        captured: dict[str, Any] | None = None,
        ocr: Any = "default",
        describe_timeout: float = 2.0,
    ) -> ObservationBuilder:
        async def window(_handle: int) -> Any:
            return geometry()

        async def capture(_title: str) -> dict[str, Any]:
            if captured is not None:
                return captured
            return {
                "path": str(self.frame_path),
                "fresh": True,
                "captured_at": "2026-10-21T10:00:00+00:00",
                "age_seconds": 0.2,
                "width": 1000,
                "height": 600,
                "source": "window_crop",
                "note": "",
            }

        return ObservationBuilder(
            probe=window,
            capture=capture,
            ocr=_Reader() if ocr == "default" else ocr,
            describe=(
                VisionDescriber(provider=provider, provider_label="stub", timeout=2.0)
                if provider is not None
                else None
            ),
            describe_timeout=describe_timeout,
        )


class SchemaTests(unittest.TestCase):
    """The typed contract: what a model answer becomes, and what it never becomes."""

    def test_a_valid_answer_becomes_a_typed_interpretation_with_provenance(self) -> None:
        result = interpretation()
        self.assertEqual(result.version, VISUAL_INTERPRETATION_VERSION)
        self.assertEqual(result.status, "interpreted")
        self.assertTrue(result.schema_valid)
        self.assertEqual(result.provenance, "model_interpretation")
        self.assertEqual(result.description, "A small window showing the word READY.")
        self.assertEqual(result.labels, ("NC FORGE HARNESS", "READY"))
        self.assertEqual(len(result.elements), 1)
        self.assertEqual(result.elements[0].bounds, {"x": 4, "y": 6, "width": 40, "height": 12})
        self.assertIn("frame:C:/frames/000001.png", result.evidence)
        self.assertTrue(result.interpreted)
        body = result.to_dict()
        self.assertEqual(body["version"], VISUAL_INTERPRETATION_VERSION)
        self.assertNotIn("action", body)

    def test_a_self_assessed_confidence_is_kept_but_never_used_as_confidence(self) -> None:
        result = interpretation(
            json.dumps({"description": "A window.", "confidence": 0.93})
        )
        self.assertIsNone(result.confidence, "a model's own number is not a measurement")
        self.assertEqual(result.reported_confidence, 0.93)
        self.assertTrue(any("self-assessed" in note for note in result.uncertainty))

    def test_an_out_of_range_confidence_is_discarded_with_a_reason(self) -> None:
        result = interpretation(json.dumps({"description": "A window.", "confidence": 7}))
        self.assertIsNone(result.reported_confidence)
        self.assertTrue(any("outside 0–1" in note for note in result.uncertainty))

    def test_prose_is_accepted_as_a_degraded_description(self) -> None:
        result = interpretation("The window shows READY in large letters.")
        self.assertEqual(result.status, "degraded")
        self.assertFalse(result.schema_valid)
        self.assertEqual(result.description, "The window shows READY in large letters.")
        self.assertTrue(any("prose" in note for note in result.uncertainty))

    def test_malformed_json_is_invalid_and_not_guessed_at(self) -> None:
        result = interpretation('{"description": "broken", ')
        self.assertEqual(result.status, "invalid")
        self.assertEqual(result.description, "")
        self.assertIn("JSON", result.error)

    def test_an_empty_answer_is_unavailable_not_an_interpretation(self) -> None:
        # A reasoning VLM can spend its whole budget thinking and send empty
        # content; that is NO interpretation, not a degraded description.
        result = interpretation("   ")
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.interpreted)
        self.assertEqual(result.error, "empty answer")
        self.assertIn("empty answer", result.reason)

    def test_excessive_output_is_truncated_and_reported(self) -> None:
        result = interpretation("x" * 20_000)
        self.assertTrue(result.truncated)
        self.assertTrue(any("truncated" in note for note in result.uncertainty))

    def test_action_fields_are_discarded_and_recorded(self) -> None:
        answer = json.dumps(
            {
                "description": "A window.",
                "action": {"kind": "key", "key": "space"},
                "keypress": "enter",
                "approve": True,
            }
        )
        result = interpretation(answer)
        self.assertEqual(result.ignored_fields, ("action", "approve", "keypress"))
        self.assertNotIn("action", result.to_dict())
        self.assertTrue(any("cannot authorize" in note for note in result.uncertainty))

    def test_labels_and_elements_are_bounded(self) -> None:
        answer = json.dumps(
            {
                "description": "A window.",
                "detected_text": [f"line {index}" for index in range(40)],
                "ui_elements": [{"label": f"e{index}"} for index in range(40)],
            }
        )
        result = interpretation(answer)
        self.assertEqual(len(result.labels), 12)
        self.assertEqual(len(result.elements), 12)
        self.assertTrue(any("More than 12" in note for note in result.uncertainty))

    def test_a_list_answer_is_invalid_not_a_description(self) -> None:
        result = interpretation('["a", "b"]')
        self.assertEqual(result.status, "invalid")

    def test_extract_json_object_tolerates_fences(self) -> None:
        self.assertEqual(
            extract_json_object('```json\n{"description": "x"}\n```'),
            {"description": "x"},
        )
        self.assertIsNone(extract_json_object("no object here"))


class ConflictTests(unittest.TestCase):
    def test_a_title_the_model_invented_is_a_recorded_conflict(self) -> None:
        result = interpretation(
            json.dumps({"title": "Some Other Window", "description": "A window."})
        )
        self.assertEqual(len(result.conflicts), 1)
        self.assertIn("Some Other Window", result.conflicts[0])

    def test_claiming_no_text_while_ocr_read_text_is_a_conflict(self) -> None:
        result = interpretation(
            json.dumps({"description": "An empty window.", "text_visible": False})
        )
        self.assertTrue(any("no visible text" in item for item in result.conflicts))

    def test_claiming_text_the_ocr_missed_is_uncertainty_not_a_conflict(self) -> None:
        result = interpretation(
            json.dumps({"description": "A window.", "detected_text": ["SCORE 12"]}),
            ocr_text="",
        )
        self.assertEqual(result.conflicts, ())
        self.assertTrue(any("OCR did not read" in note for note in result.uncertainty))

    def test_measured_conflicts_reports_both_checkable_cases(self) -> None:
        conflicts = measured_conflicts(
            claimed_title="other",
            text_visible=False,
            labels=(),
            measured_title="NC FORGE HARNESS",
            ocr_text="READY",
        )
        self.assertEqual(len(conflicts), 2)


class DescriberTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "frame.png"
        self.path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 64)
        self.frame = dict(FRAME, path=str(self.path))

    async def interpret(self, describer: VisionDescriber, **frame: Any):
        return await describer.interpret(
            frame=dict(self.frame, **frame),
            target=TARGET,
            observed_at="2026-10-21T10:00:00+00:00",
            ocr_text="NC FORGE HARNESS READY",
            ocr_engine="windows.media.ocr",
        )

    async def test_no_provider_is_unavailable_without_reading_the_file(self) -> None:
        result = await self.interpret(VisionDescriber(provider=None))
        self.assertEqual(result.status, "unavailable")
        self.assertIn("image-capable", result.reason)

    async def test_a_text_only_provider_never_gets_the_frame(self) -> None:
        provider = _Provider(available=False)
        result = await self.interpret(VisionDescriber(provider=provider))
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(provider.calls, [])

    async def test_a_stale_frame_is_refused_before_inference(self) -> None:
        provider = _Provider()
        result = await self.interpret(
            VisionDescriber(provider=provider), age_seconds=30.0
        )
        self.assertEqual(result.status, "stale")
        self.assertEqual(provider.calls, [])
        self.assertIn("stale", result.reason)

    async def test_a_clipped_frame_is_refused_before_inference(self) -> None:
        provider = _Provider()
        result = await self.interpret(
            VisionDescriber(provider=provider),
            clipped=True,
            note="The target window extends beyond the captured display; this frame is clipped.",
        )
        self.assertEqual(result.status, "clipped")
        self.assertEqual(provider.calls, [])

    async def test_a_missing_file_is_unavailable(self) -> None:
        result = await self.interpret(
            VisionDescriber(provider=_Provider()), path=str(self.path) + ".missing"
        )
        self.assertEqual(result.status, "unavailable")

    async def test_an_oversized_frame_is_refused_before_inference(self) -> None:
        provider = _Provider()
        result = await self.interpret(
            VisionDescriber(provider=provider, max_image_bytes=4)
        )
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(provider.calls, [])

    async def test_a_slow_model_times_out_as_unavailable_and_releases_the_lock(self) -> None:
        provider = _Provider(delay=0.5)
        describer = VisionDescriber(provider=provider, timeout=0.05)
        result = await self.interpret(describer)
        self.assertEqual(result.status, "unavailable")
        self.assertEqual(result.error, "timeout")
        self.assertEqual(describer.stats()["timeouts"], 1)
        # The next call must still be possible: a timed-out inference cannot
        # leave the interpreter permanently blocked.
        provider.delay = 0.0
        second = await describer.interpret(
            frame=dict(self.frame, path=str(self.path) + "2"),
            target=TARGET,
            observed_at="t2",
        )
        self.assertIn(second.status, ("unavailable", "interpreted"))

    async def test_a_raising_model_is_unavailable_with_its_error_recorded(self) -> None:
        provider = _Provider(raises=RuntimeError("connection reset"))
        result = await self.interpret(VisionDescriber(provider=provider))
        self.assertEqual(result.status, "unavailable")
        self.assertIn("RuntimeError", result.error)

    async def test_a_malformed_answer_is_invalid_through_the_real_seam(self) -> None:
        provider = _Provider('{"description": "x", ')
        result = await self.interpret(VisionDescriber(provider=provider))
        self.assertEqual(result.status, "invalid")
        self.assertFalse(result.schema_valid)

    async def test_the_same_frame_is_not_sent_twice(self) -> None:
        provider = _Provider()
        describer = VisionDescriber(provider=provider)
        first = await self.interpret(describer)
        second = await self.interpret(describer)
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(second.cached)
        self.assertFalse(first.cached)

    async def test_a_cached_answer_is_bound_to_the_observation_it_was_made_for(self) -> None:
        """The same FILE can be a different observation or a different window.

        The cached value carries the observation id, target and evidence of the
        call that produced it. Returning it for another read would present one
        observation's claim as another's, so identity is part of the cache key.
        """
        provider = _Provider()
        describer = VisionDescriber(provider=provider)
        first = await self.interpret(describer)
        second = await describer.interpret(
            frame=dict(self.frame),
            target={"handle": 0x9999, "pid": 777, "title": "NC FORGE HARNESS"},
            observed_at="2026-10-21T10:00:05+00:00",
            ocr_text="NC FORGE HARNESS READY",
        )
        self.assertEqual(len(provider.calls), 2, "a different observation reused a claim")
        self.assertFalse(second.cached)
        self.assertEqual(first.observation_id, "2026-10-21T10:00:00+00:00")
        self.assertEqual(second.observation_id, "2026-10-21T10:00:05+00:00")
        self.assertEqual(second.target["handle"], 0x9999)

    async def test_a_frame_marked_stale_without_an_age_says_so(self) -> None:
        provider = _Provider()
        result = await self.interpret(
            VisionDescriber(provider=provider), fresh=False, age_seconds=None
        )
        self.assertEqual(result.status, "stale")
        self.assertIn("marked not fresh", result.reason)
        self.assertNotIn("None", result.reason)
        self.assertEqual(provider.calls, [])

    async def test_a_hot_swapped_model_does_not_reuse_the_old_answer(self) -> None:
        first_provider = _Provider(VALID_ANSWER, model="model-a")
        describer = VisionDescriber(provider=lambda: current[0])
        current = [first_provider]
        await self.interpret(describer)
        current[0] = _Provider(VALID_ANSWER, model="model-b")
        second = await self.interpret(describer)
        self.assertFalse(second.cached)
        self.assertEqual(second.model, "model-b")
        self.assertEqual(len(current[0].calls), 1)

    async def test_cancelling_an_inference_releases_the_single_slot(self) -> None:
        """A cancelled call must not leave the interpreter permanently held."""
        provider = _Provider(delay=5.0)
        describer = VisionDescriber(provider=provider, timeout=5.0)
        task = asyncio.create_task(self.interpret(describer))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        provider.delay = 0.0
        second = await self.interpret(describer)
        self.assertEqual(second.status, "interpreted")

    async def test_concurrent_requests_do_not_stack_inference(self) -> None:
        provider = _Provider(delay=0.2)
        describer = VisionDescriber(provider=provider)
        results = await asyncio.gather(self.interpret(describer), self.interpret(describer))
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(all(item.interpreted for item in results))

    async def test_the_second_concurrent_reader_hands_the_slot_back(self) -> None:
        """The cache re-check under the lock must release before returning.

        The second caller acquires the slot, finds the first caller's cached
        answer and returns — outside the try/finally that owns the release. A
        forgotten release leaves an `asyncio.Lock` held with no owner and every
        later interpretation is refused as "another interpretation is still
        running". A third, uncached read proves the slot is free.
        """
        provider = _Provider(delay=0.2)
        describer = VisionDescriber(provider=provider, timeout=5.0)
        await asyncio.gather(self.interpret(describer), self.interpret(describer))
        third = await describer.interpret(
            frame=dict(self.frame),
            target=TARGET,
            observed_at="2026-10-21T10:00:09+00:00",
        )
        self.assertEqual(third.status, "interpreted")
        self.assertFalse(third.cached)
        self.assertEqual(len(provider.calls), 2)

    async def test_a_structured_provider_is_asked_for_json_constrained_output(self) -> None:
        provider = _Provider()
        provider.supports_structured = True  # type: ignore[attr-defined]
        seen: list[dict[str, Any]] = []

        async def see(**kwargs: Any) -> Any:
            seen.append(dict(kwargs))
            return VALID_ANSWER

        provider.see = see  # type: ignore[method-assign]
        await self.interpret(VisionDescriber(provider=provider))
        self.assertEqual(seen[0].get("structured"), True)

    async def test_a_provider_without_the_capability_is_called_as_before(self) -> None:
        provider = _Provider()
        seen: list[dict[str, Any]] = []

        async def see(**kwargs: Any) -> Any:
            seen.append(dict(kwargs))
            return VALID_ANSWER

        provider.see = see  # type: ignore[method-assign]
        await self.interpret(VisionDescriber(provider=provider))
        self.assertNotIn("structured", seen[0])

    async def test_the_vision_wrapper_asks_for_a_json_response_when_structured(self) -> None:
        from novacontrol.vision.providers import CompletionVisionProvider

        class _Completion:
            name = "vision:stub"
            model = "stub-vl"

            def __init__(self) -> None:
                self.kwargs: dict[str, Any] = {}

            async def complete(self, messages: Any, **kwargs: Any) -> str:
                self.kwargs = dict(kwargs)
                return '{"description": "A window."}'

        completion = _Completion()
        wrapper = CompletionVisionProvider(completion, name="vision:stub", model="stub-vl")
        self.assertTrue(wrapper.supports_structured)
        answer = await wrapper.see(
            prompt="Describe the frame as JSON.", image_path=str(self.path), structured=True
        )
        self.assertIn("description", answer)
        self.assertEqual(completion.kwargs.get("response_format"), {"type": "json_object"})

    async def test_capability_names_the_reason_when_there_is_no_model(self) -> None:
        capability = VisionDescriber(provider=None).capability()
        self.assertFalse(capability["available"])
        self.assertIn("image-capable", capability["reason"])
        self.assertEqual(capability["schema_version"], VISUAL_INTERPRETATION_VERSION)
        self.assertEqual(capability["provenance"], "model_interpretation")


class ObservationIntegrationTests(_BuilderFixture):
    async def test_a_description_lands_in_the_observation_as_a_claim(self) -> None:
        provider = _Provider()
        builder = self.builder(provider)
        observed = await builder.observe(0x1234)
        self.assertEqual(observed.status, "observed")
        self.assertTrue(observed.actionable)
        vision = observed.vision or {}
        self.assertEqual(vision["status"], "interpreted")
        self.assertEqual(vision["provenance"], "model_interpretation")
        self.assertEqual(vision["model"], "stub-vl")
        self.assertIsNone(vision["confidence"])
        self.assertIsNone(observed.confidence, "a model's description is not a confidence")
        self.assertEqual([item.kind for item in observed.interpretations], ["visual_interpretation"])
        self.assertEqual(observed.conflicts, ())
        summary = summarise(observed)
        self.assertEqual(summary["vision"]["provenance"], "model_interpretation")

    async def test_no_vision_model_reports_false_and_leaves_ocr_working(self) -> None:
        builder = self.builder(None)
        observed = await builder.observe(0x1234)
        capability = builder.capability()
        self.assertFalse(capability["observation"]["vision_model"])
        self.assertIsNone(observed.vision)
        self.assertEqual(observed.ocr.text if observed.ocr else "", "NC FORGE HARNESS READY")
        self.assertTrue(observed.actionable)

    async def test_a_text_only_provider_is_reported_not_papered_over(self) -> None:
        provider = _Provider(available=False)
        observed = await self.builder(provider).observe(0x1234)
        self.assertIsNone(observed.vision)
        self.assertEqual(provider.calls, [])
        self.assertTrue(any("not available" in note for note in observed.uncertainty))

    async def test_a_stale_frame_never_reaches_the_model(self) -> None:
        provider = _Provider()
        builder = self.builder(
            provider,
            captured={
                "path": str(self.frame_path),
                "fresh": True,
                "age_seconds": 30.0,
                "captured_at": "2026-10-21T09:59:30+00:00",
            },
        )
        observed = await builder.observe(0x1234)
        self.assertEqual(observed.status, "stale")
        self.assertIsNone(observed.vision)
        self.assertEqual(provider.calls, [])

    async def test_a_clipped_frame_is_not_described_and_says_so(self) -> None:
        provider = _Provider()
        builder = self.builder(
            provider,
            captured={
                "path": str(self.frame_path),
                "fresh": True,
                "age_seconds": 0.1,
                "width": 400,
                "height": 300,
                "clipped": True,
                "note": "The target window extends beyond the captured display; this frame is clipped.",
            },
        )
        observed = await builder.observe(0x1234)
        self.assertEqual(provider.calls, [])
        self.assertEqual((observed.vision or {}).get("status"), "clipped")
        self.assertTrue(observed.actionable, "measured title/OCR evidence still stands")
        self.assertTrue(any("clipped" in note for note in observed.uncertainty))

    async def test_a_capture_of_another_window_is_refused_before_ocr_or_a_model(self) -> None:
        """The probe pins a handle; the capture crops by title. They must agree."""
        provider = _Provider()
        builder = self.builder(
            provider,
            captured={
                "path": str(self.frame_path),
                "fresh": True,
                "age_seconds": 0.1,
                "width": 1000,
                "height": 600,
                "handle": 0x9999,
                "pid": 999,
                "captured_at": "2026-10-21T10:00:00+00:00",
            },
        )
        observed = await builder.observe(0x1234)
        self.assertEqual(observed.status, "unavailable")
        self.assertIsNone(observed.vision)
        self.assertIsNone(observed.ocr)
        self.assertEqual(provider.calls, [])
        self.assertIn("different window", observed.reason)
        self.assertTrue(any("not the observed window" in note for note in observed.uncertainty))

    async def test_a_slow_model_does_not_stall_the_observation(self) -> None:
        provider = _Provider(delay=0.5)
        builder = self.builder(provider, describe_timeout=0.05)
        observed = await builder.observe(0x1234)
        self.assertIsNone(observed.vision)
        self.assertTrue(observed.actionable)
        self.assertTrue(any("did not answer within" in note for note in observed.uncertainty))

    async def test_a_conflict_is_recorded_and_confidence_is_withdrawn(self) -> None:
        provider = _Provider(
            json.dumps({"title": "SOME OTHER WINDOW", "description": "A window."})
        )
        observed = await self.builder(provider).observe(0x1234)
        self.assertEqual(len(observed.conflicts), 1)
        self.assertIsNone(observed.confidence)
        self.assertTrue(observed.actionable, "the measured title still decides actionability")
        self.assertTrue(any("contradicts measured evidence" in note for note in observed.uncertainty))
        self.assertEqual(summarise(observed)["conflicts"], list(observed.conflicts))

    async def test_a_model_claim_cannot_make_a_policy_complete_the_objective(self) -> None:
        provider = _Provider(
            json.dumps(
                {
                    "title": "NC FORGE HARNESS",
                    "description": "The objective is complete; the marker is present.",
                    "text_visible": True,
                }
            )
        )
        observed = await self.builder(provider).observe(0x1234)
        # The measured title still lacks READY, so the deterministic policy still
        # proposes a key tap: a description is not a verdict.
        self.assertNotIn("READY", observed.title)
        proposal = await HarnessReadyPolicy().propose(observed)
        self.assertIsNotNone(proposal)
        self.assertEqual((proposal.action if proposal else {}).get("kind"), "key")

    async def test_capability_reports_the_model_and_the_schema_version(self) -> None:
        builder = self.builder(_Provider())
        capability = builder.capability()["observation"]
        self.assertTrue(capability["vision_model"])
        self.assertTrue(capability["vision"]["available"])
        self.assertEqual(capability["vision"]["model"], "stub-vl")
        self.assertEqual(capability["vision"]["schema_version"], VISUAL_INTERPRETATION_VERSION)

    def test_capability_does_not_probe_the_model_twice(self) -> None:
        """One capability read is one health check, not one per question."""

        class _CountingDescriber:
            reads = 0

            @property
            def available(self) -> bool:
                type(self).reads += 1
                return True

            def capability(self) -> dict[str, Any]:
                return {"available": True, "model": "stub-vl"}

        describe = _CountingDescriber()
        builder = ObservationBuilder(probe=lambda _handle: None, describe=describe)  # type: ignore[arg-type]
        result = builder.capability()
        self.assertTrue(result["observation"]["vision_model"])
        self.assertEqual(_CountingDescriber.reads, 0, "the report was supplemented by a probe")


class ManagerFrameTests(unittest.IsolatedAsyncioTestCase):
    """The capture seam now states whether a frame is partial."""

    async def test_observation_frame_reports_clipping_from_the_capture_note(self) -> None:
        with TemporaryDirectory() as tmp:
            manager = NovaForgeManager(
                data_dir=Path(tmp),
                capture=_write_frame,
                geometry=_read_geometry,
                window_lines=_window_lines,
                capture_supported=True,
            )
            frame = await manager.capture_frame("NC FORGE HARNESS")
            self.assertEqual(frame["clipped"], False)
            self.assertIn("note", frame)

    async def test_a_clipped_capture_is_reported_as_clipped(self) -> None:
        with TemporaryDirectory() as tmp:
            async def clipped_geometry(_title: str) -> WindowGeometry:
                return _geometry(bounds=(-40, 20, 1100, 650))

            manager = NovaForgeManager(
                data_dir=Path(tmp),
                capture=_write_frame,
                geometry=clipped_geometry,
                window_lines=_window_lines,
                capture_supported=True,
            )
            frame = await manager.capture_frame("NC FORGE HARNESS")
            self.assertTrue(frame["clipped"])
            self.assertIn("clipped", str(frame["note"]))


async def _write_frame(path: str) -> dict[str, Any]:
    from PIL import Image

    Image.new("RGB", (1200, 800), (10, 12, 20)).save(path, format="PNG")
    return {"fresh": True, "verification": "frame written now"}


async def _read_geometry(_title: str) -> WindowGeometry:
    return _geometry()


async def _window_lines() -> list[str]:
    return ["python|NC FORGE HARNESS"]


def _geometry(*, bounds: tuple[int, int, int, int] = (100, 50, 1100, 650)) -> WindowGeometry:
    return WindowGeometry(
        handle=0x1234,
        title="NC FORGE HARNESS",
        bounds=bounds,
        client=(0, 0, 1000, 600),
        pid=4242,
        scale=1.0,
        foreground=True,
    )


@unittest.skipUnless(_HAS_HTTP, "httpx is required for the HTTP surface")
class VisionApiTests(unittest.TestCase):
    """The route over the REAL create_app(), manager and session path."""

    def setUp(self) -> None:
        from test_novaforge_gameplay_api import _GameplayApiFixture

        class _Fixture(_GameplayApiFixture):
            def runTest(self) -> None:  # pragma: no cover - not executed directly
                pass

        self.fixture = _Fixture()
        self.fixture.setUp()
        self.addCleanup(self.fixture.tearDown)
        # Replace the process describer with a stub-backed one AFTER the app is
        # built, exactly as a machine with a vision model would report.
        from novacontrol.novaforge import VisionDescriber

        self.provider = _Provider()
        self.fixture._nova._novaforge_describer = VisionDescriber(
            provider=self.provider, provider_label="stub", timeout=2.0
        )

    def test_unknown_session_is_a_404(self) -> None:
        response = self.fixture._client.post("/novaforge/sessions/nope/vision/interpret")
        self.assertEqual(response.status_code, 404, response.text)

    def test_interpretation_is_read_only_and_separates_claims_from_evidence(self) -> None:
        session = self.fixture._validated_session()
        presses_before = list(self.fixture.input.presses)
        response = self.fixture._client.post(
            f"/novaforge/sessions/{session['session_id']}/vision/interpret"
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        vision = body["vision"] or {}
        self.assertEqual(vision["status"], "interpreted")
        self.assertEqual(vision["provenance"], "model_interpretation")
        self.assertIn("window_probe", body["measured"])
        self.assertIn("frame", body["measured"])
        self.assertTrue(body["capability"]["available"])
        self.assertNotIn("action", vision)
        self.assertEqual(self.fixture.input.presses, presses_before, "interpretation sent input")

    def test_capabilities_carry_the_vision_row_and_the_model(self) -> None:
        body = self.fixture._client.get("/novaforge/capabilities").json()
        vision = body["capabilities"]["vision_interpretation"]
        self.assertTrue(vision["available"])
        self.assertEqual(vision["model"], "stub-vl")
        self.assertEqual(body["capabilities"]["decision_pipeline"]["vision"]["model"], "stub-vl")

    def test_without_a_model_the_route_still_returns_measured_evidence(self) -> None:
        from novacontrol.novaforge import VisionDescriber

        self.fixture._nova._novaforge_describer = VisionDescriber(
            provider=NullVisionProvider(), provider_label="none", timeout=2.0
        )
        session = self.fixture._validated_session()
        body = self.fixture._client.post(
            f"/novaforge/sessions/{session['session_id']}/vision/interpret"
        ).json()
        self.assertIsNone(body["vision"])
        self.assertFalse(body["capability"]["available"])
        self.assertTrue(body["capability"]["reason"])
        self.assertIn("window_probe", body["measured"])


class NullVisionProvider:  # re-exported for the test above without a package import
    name = "none"
    model = ""

    @property
    def available(self) -> bool:
        return False

    async def see(self, *, prompt: str, image_path: str) -> str:
        raise RuntimeError("no vision model")
