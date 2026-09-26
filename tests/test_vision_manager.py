"""Phase 6: vision as its own capability, with a replaceable provider.

Three rules are worth testing more than the code around them:

  * **A picture is read before it is interpreted.** The OCR route must answer
    text questions without ever calling a model — the whole reason the cheap
    path exists — and must escalate only when the text genuinely cannot answer.
  * **The provider is a plug.** A fake provider must be able to stand in for any
    VLM, which is what makes "do not hard-code a single vision model" a fact
    about the code rather than a promise in a specification.
  * **A refusal is a result.** With no model wired, the pipeline says the
    question was not answered and why, instead of dressing an OCR dump up as an
    answer.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from typing import Any

from novacontrol.core.config import NovaControlConfig, VisionSettings
from novacontrol.intelligence.engine import GlobalInputIntelligence
from novacontrol.vision import (
    NullOcrEngine,
    NullVisionProvider,
    OcrWord,
    TextFileOcrEngine,
    VisionImageType,
    VisionManager,
    VisionProviderError,
    VisionRequest,
    VisionTaskKind,
    build_vision_provider,
    classify_image_type,
    extract_errors,
    group_lines,
    read_json_object,
    task_for_question,
)
from novacontrol.vision.ocr import ChainOcrEngine, default_ocr_engine

STRUCTURED_ANSWER = """
Here is what I can see:
```json
{"summary": "A dialog reports the upload failed.",
 "image_type": "error_screenshot",
 "detected_text": ["Upload failed", "Retry"],
 "ui_elements": [
   {"label": "Retry", "kind": "button",
    "bounds": {"x": 10, "y": 20, "width": 30, "height": 12}}
 ],
 "errors": ["Upload failed"],
 "relevant_regions": [{"label": "error banner", "x": 0, "y": 0}],
 "confidence": 0.91}
```
"""


class FakeVisionProvider:
    """A stand-in VLM: records what it was asked, returns what it was given."""

    def __init__(
        self,
        answer: str = "",
        *,
        available: bool = True,
        error: str = "",
        name: str = "fake",
    ) -> None:
        self.name = name
        self.model = "fake-vision"
        self._available = available
        self.answer = answer
        self.error = error
        self.calls: list[dict[str, str]] = []

    @property
    def available(self) -> bool:
        return self._available

    async def see(self, *, prompt: str, image_path: str) -> str:
        self.calls.append({"prompt": prompt, "image_path": image_path})
        if self.error:
            raise VisionProviderError(self.error)
        return self.answer


class FakeOcrEngine:
    """A stand-in OCR engine: whatever words the test wants, with geometry."""

    def __init__(
        self, lines: list[str], *, available: bool = True, geometry: bool = False
    ) -> None:
        self.name = "fake-ocr"
        self._available = available
        self._words = tuple(
            OcrWord(
                text=word,
                line=index,
                x=10 * position,
                y=20 * index,
                width=40 if geometry else 0,
                height=12 if geometry else 0,
                kind=self.name,
            )
            for index, line in enumerate(lines)
            for position, word in enumerate(line.split())
        )

    @property
    def available(self) -> bool:
        return self._available

    async def read(self, source: str) -> tuple[OcrWord, ...]:
        del source
        return self._words


class VisionProviderBoundaryTests(unittest.TestCase):
    """The seam: replaceable, gated, and honest about what it cannot do."""

    def test_a_text_only_provider_is_refused_rather_than_trusted(self) -> None:
        class TextOnly:
            name = "text"

            async def complete(self, messages: object) -> str:  # pragma: no cover
                return "I cannot see"

        provider = build_vision_provider(TextOnly())
        self.assertFalse(provider.available)
        self.assertEqual(provider.name, "none")

    def test_no_provider_resolves_to_the_null_one(self) -> None:
        self.assertIsInstance(build_vision_provider(None), NullVisionProvider)

    def test_the_null_provider_says_why_instead_of_answering(self) -> None:
        import asyncio

        with self.assertRaises(VisionProviderError):
            asyncio.run(NullVisionProvider().see(prompt="what is this?", image_path="x.png"))

    def test_a_non_null_provider_is_wrapped_with_its_model(self) -> None:
        from novacontrol.integrations.llm import OpenAICompatibleLLMProvider

        provider = OpenAICompatibleLLMProvider(
            name="vision:ollama",
            base_url="http://localhost:11434",
            api_key="x",
            model="qwen3-vl:4b",
        )
        wrapped = build_vision_provider(provider)
        self.assertTrue(wrapped.available)
        self.assertIn("ollama", wrapped.name)
        self.assertEqual(wrapped.model, "qwen3-vl:4b")


class OcrEngineTests(unittest.TestCase):
    """OCR is a named engine, and the shipped one reuses what already existed."""

    def test_the_shipped_engine_tries_text_files_first(self) -> None:
        engine = default_ocr_engine()
        self.assertIn("text-file", engine.name)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "note.txt"
            path.write_text("first line\n\nsecond line\n", encoding="utf-8")
            import asyncio

            words = asyncio.run(engine.read(str(path)))
        self.assertEqual(group_lines(words), ("first line", "second line"))

    def test_a_missing_file_reads_nothing(self) -> None:
        import asyncio

        self.assertEqual(asyncio.run(TextFileOcrEngine().read("nope.txt")), ())

    def test_a_chain_falls_through_to_the_next_engine(self) -> None:
        engine = ChainOcrEngine((NullOcrEngine(), TextFileOcrEngine()))
        self.assertTrue(engine.available)
        self.assertEqual(engine.name, "none+text-file")

    def test_an_empty_chain_is_reported_as_unavailable(self) -> None:
        engine = ChainOcrEngine((NullOcrEngine(),))
        self.assertFalse(engine.available)

    def test_lines_are_grouped_by_the_engine_s_own_order(self) -> None:
        words = (OcrWord("b", line=1), OcrWord("a", line=0), OcrWord("c", line=1))
        self.assertEqual(group_lines(words), ("a", "b c"))


class VisionStructuringTests(unittest.TestCase):
    """Pure helpers behind the structured result."""

    def test_errors_are_the_lines_that_read_as_failures(self) -> None:
        lines = ("Welcome", "Error: disk full", "Saved", "Traceback: ValueError")
        found = extract_errors(lines)
        self.assertIn("Error: disk full", found)
        self.assertIn("Traceback: ValueError", found)
        self.assertNotIn("Welcome", found)

    def test_an_exception_class_name_is_a_failure_line(self) -> None:
        # "Error" glued to a class name has no word boundary, so the plain word
        # rule misses the canonical screenshot a person asks about.
        lines = (
            "Traceback (most recent call last):",
            "TypeError: expected str, got int",
            "ZeroDivisionError: division by zero",
            "Build succeeded",
        )
        self.assertEqual(
            extract_errors(lines),
            (
                "Traceback (most recent call last):",
                "TypeError: expected str, got int",
                "ZeroDivisionError: division by zero",
            ),
        )

    def test_a_json_object_is_found_through_prose_and_fences(self) -> None:
        self.assertIsNotNone(read_json_object(STRUCTURED_ANSWER))
        self.assertEqual(read_json_object("no json here"), None)
        self.assertEqual(read_json_object("") , None)

    def test_the_first_object_wins_when_several_parse(self) -> None:
        parsed = read_json_object('{"a": 1} and {"b": 2}')
        self.assertEqual(parsed, {"a": 1})

    def test_a_broken_object_does_not_break_the_read(self) -> None:
        self.assertIsNone(read_json_object('{"a": }'))
        self.assertIsNotNone(read_json_object('{"a": } {"b": 2}'))

    def test_image_type_comes_from_the_name_the_question_and_the_text(self) -> None:
        self.assertEqual(classify_image_type("shot.png"), VisionImageType.PHOTO)
        self.assertEqual(
            classify_image_type("capture.png"), VisionImageType.APPLICATION_SCREENSHOT
        )
        self.assertEqual(
            classify_image_type("x.png", errors=("Error: disk full",)),
            VisionImageType.ERROR_SCREENSHOT,
        )
        self.assertEqual(classify_image_type("report.pdf"), VisionImageType.DOCUMENT)
        self.assertEqual(classify_image_type("mystery"), VisionImageType.UNKNOWN)


class OcrFirstOrModelTests(unittest.IsolatedAsyncioTestCase):
    """The decision Phase 6 exists for: read first, think only if needed."""

    async def test_a_text_question_is_answered_without_consulting_the_model(self) -> None:
        provider = FakeVisionProvider("should never be called")
        ocr = FakeOcrEngine(["Error: disk full", "Retry"])
        manager = VisionManager(provider=provider, ocr=ocr)

        result = await manager.analyze(VisionRequest("x.png", question="what is this error?"))

        self.assertTrue(result.answered)
        self.assertFalse(result.escalated)
        self.assertEqual(provider.calls, [])
        self.assertEqual(result.metadata["answer_source"], "ocr")
        self.assertEqual(result.errors, ("Error: disk full",))
        self.assertEqual(result.image_type, VisionImageType.ERROR_SCREENSHOT)

    async def test_a_text_extraction_is_confident_about_the_extraction(self) -> None:
        # "Read the text in this screenshot" is a REQUEST, and its own words are
        # not claims about the image, so scoring the answer by how many of them
        # appear in the text returned 0.0 beside answered=True — a result that
        # contradicts itself.
        manager = VisionManager(
            provider=NullVisionProvider(), ocr=FakeOcrEngine(["Welcome", "Password"])
        )
        result = await manager.analyze(
            VisionRequest("x.png", question="read the text in this screenshot")
        )
        self.assertTrue(result.answered)
        self.assertEqual(result.metadata["confidence_basis"], "ocr-text-extraction")
        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(result.metadata["question_coverage"], 0.0)

    async def test_an_unanswered_question_is_never_confident(self) -> None:
        manager = VisionManager(
            provider=NullVisionProvider(), ocr=FakeOcrEngine(["Welcome"])
        )
        for question in ("where is the login button?", ""):
            with self.subTest(question=question):
                result = await manager.analyze(
                    VisionRequest("x.png", question=question)
                )
                self.assertFalse(result.answered)
                self.assertEqual(result.confidence, 0.0)

    async def test_the_confidence_is_the_measured_question_coverage(self) -> None:
        # Every content word of the question is in the text, so coverage is 1.
        manager = VisionManager(
            provider=FakeVisionProvider(), ocr=FakeOcrEngine(["Error: disk full"])
        )
        result = await manager.analyze(VisionRequest("x.png", question="what is this error?"))
        self.assertEqual(result.confidence, 1.0)
        self.assertEqual(result.metadata["confidence_basis"], "ocr-question-coverage")

    async def test_a_question_the_text_cannot_answer_reaches_the_model(self) -> None:
        provider = FakeVisionProvider("The button is at the top right.")
        ocr = FakeOcrEngine(["Welcome", "Username", "Password"])
        manager = VisionManager(provider=provider, ocr=ocr)

        result = await manager.analyze(
            VisionRequest("x.png", question="where is the login button?")
        )

        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(result.escalated)
        self.assertEqual(result.metadata["answer_source"], "fake")
        self.assertTrue(result.answered)

    async def test_a_locate_task_never_settles_for_text_alone(self) -> None:
        # The engine read the words but reported NO positions (a plain text
        # file does exactly this), so the text cannot say where anything is.
        provider = FakeVisionProvider('{"found": true, "x": 500, "y": 250, "label": "login"}')
        ocr = FakeOcrEngine(["login button"])
        manager = VisionManager(provider=provider, ocr=ocr)

        result = await manager.analyze(
            VisionRequest(
                "x.png", task=VisionTaskKind.LOCATE, target="login button"
            )
        )

        self.assertEqual(len(provider.calls), 1)
        self.assertIn("login button", provider.calls[0]["prompt"])
        self.assertTrue(result.relevant_regions)

    async def test_a_located_label_is_answered_without_a_model(self) -> None:
        # The read words carry positions, so the label's own coordinates ARE
        # the answer: paying a VLM for a point the read already produced is the
        # inference the OCR-first rule exists to avoid.
        provider = FakeVisionProvider('{"found": true, "x": 1, "y": 2}')
        ocr = FakeOcrEngine(["Welcome", "Login with SSO"], geometry=True)
        manager = VisionManager(provider=provider, ocr=ocr)

        result = await manager.analyze(
            VisionRequest("x.png", task=VisionTaskKind.LOCATE, target="login button")
        )

        self.assertEqual(provider.calls, [])
        self.assertTrue(result.answered)
        self.assertFalse(result.escalated)
        self.assertEqual(result.metadata["answer_source"], "ocr")
        self.assertEqual(result.metadata["confidence_basis"], "ocr-locate")
        region = result.relevant_regions[0]
        self.assertEqual(region["label"], "Login")
        self.assertIn("center_x", region)
        self.assertIn("center_y", region)
        # The control noun ("button") is not looked for as screen text.
        self.assertTrue(
            any(element.label == "Login" for element in result.ui_elements)
        )

    async def test_a_label_the_text_does_not_place_is_escalated(self) -> None:
        for words in (
            FakeOcrEngine(["Welcome", "Login with SSO"], geometry=True),  # word absent
            FakeOcrEngine(["Login with SSO"]),  # present but unplaced
        ):
            with self.subTest(placed=words._words[1].has_geometry):
                provider = FakeVisionProvider('{"found": false}')
                manager = VisionManager(provider=provider, ocr=words)
                result = await manager.analyze(
                    VisionRequest(
                        "x.png", task=VisionTaskKind.LOCATE, target="logout button"
                    )
                )
                self.assertEqual(len(provider.calls), 1)
                self.assertFalse(result.relevant_regions)

    async def test_a_located_answer_is_not_more_confident_than_a_read_word(self) -> None:
        # Finding the word is not knowing it is the control that was asked
        # about, so the located answer quotes the same figure as the element
        # list rather than inventing a higher one.
        ocr = FakeOcrEngine(["Login"], geometry=True)
        manager = VisionManager(provider=NullVisionProvider(), ocr=ocr)
        result = await manager.analyze(
            VisionRequest("x.png", task=VisionTaskKind.LOCATE, target="Login")
        )
        self.assertEqual(result.confidence, 0.5)
        self.assertEqual(result.ui_elements[0].confidence, 0.5)

    async def test_an_ocr_configured_request_never_reaches_the_model(self) -> None:
        provider = FakeVisionProvider("unused")
        manager = VisionManager(provider=provider, ocr=FakeOcrEngine(["some text"]))
        result = await manager.analyze(
            VisionRequest("x.png", task=VisionTaskKind.OCR, prefer_ocr=False)
        )
        self.assertEqual(provider.calls, [])
        self.assertTrue(result.answered)

    async def test_turning_the_cheap_path_off_goes_straight_to_the_model(self) -> None:
        provider = FakeVisionProvider("A blue sky over a lake.")
        manager = VisionManager(
            provider=provider, ocr=FakeOcrEngine(["some text"]), prefer_ocr=False
        )
        result = await manager.analyze(VisionRequest("photo.png", question="what is this?"))
        self.assertEqual(len(provider.calls), 1)
        self.assertTrue(result.escalated)

    async def test_no_model_and_an_unanswered_question_is_an_honest_refusal(self) -> None:
        manager = VisionManager(provider=NullVisionProvider(), ocr=FakeOcrEngine(["Welcome"]))
        result = await manager.analyze(
            VisionRequest("x.png", question="where is the login button?")
        )
        self.assertFalse(result.answered)
        self.assertEqual(result.metadata["confidence_basis"], "no-vision-provider")
        self.assertIn("no vision model", result.metadata["reason"])
        # It still returns what it could read, rather than nothing at all.
        self.assertEqual(result.detected_text, ("Welcome",))

    async def test_no_ocr_and_no_model_reports_which_failure_it_was(self) -> None:
        manager = VisionManager(
            provider=NullVisionProvider(), ocr=NullOcrEngine()
        )
        result = await manager.analyze(VisionRequest("x.png", question="what is this?"))
        self.assertFalse(result.answered)
        self.assertIn("no OCR engine", result.metadata["reason"])

    async def test_a_text_only_request_is_not_escalated(self) -> None:
        provider = FakeVisionProvider("unused")
        manager = VisionManager(provider=provider, ocr=FakeOcrEngine(["Welcome"]))
        result = await manager.analyze(
            VisionRequest("x.png", question="what is this?", allow_vlm=False)
        )
        self.assertEqual(provider.calls, [])
        self.assertFalse(result.answered)
        self.assertEqual(result.metadata["confidence_basis"], "vision-not-allowed")

    async def test_a_provider_failure_is_classified_not_swallowed(self) -> None:
        manager = VisionManager(
            provider=FakeVisionProvider(error="connection refused"),
            ocr=FakeOcrEngine(["Welcome"]),
        )
        result = await manager.analyze(
            VisionRequest("x.png", question="where is the login button?")
        )
        self.assertFalse(result.answered)
        self.assertEqual(result.metadata["confidence_basis"], "vision-provider-error")
        self.assertIn("connection refused", result.metadata["reason"])

    async def test_a_provider_that_failed_is_still_recorded_as_consulted(self) -> None:
        # The model WAS tried and could not answer, so provenance must say so:
        # "escalated" is about who was asked, and "answer_source" is about who
        # answered. Reporting escalated=False here credited the OCR reader with
        # an answer nobody produced.
        manager = VisionManager(
            provider=FakeVisionProvider(error="connection refused"),
            ocr=FakeOcrEngine(["Welcome"]),
        )
        result = await manager.analyze(
            VisionRequest("x.png", question="where is the login button?")
        )
        self.assertTrue(result.escalated)
        self.assertTrue(result.metadata["escalated"])
        self.assertEqual(result.metadata["answer_source"], "none")
        self.assertFalse(result.answered)
        # The text that WAS read still travels, as context rather than an answer.
        self.assertEqual(result.detected_text, ("Welcome",))


class StructuredResultTests(unittest.IsolatedAsyncioTestCase):
    """The result the planner consumes — structured, bounded, and no reasoning."""

    async def test_the_documented_shape_is_what_leaves(self) -> None:
        manager = VisionManager(provider=NullVisionProvider(), ocr=FakeOcrEngine(["hi"]))
        predicted = await manager.analyze(VisionRequest("x.png"))
        self.assertEqual(
            set(predicted.to_dict()),
            {
                "image_type",
                "detected_text",
                "ui_elements",
                "errors",
                "relevant_regions",
                "summary",
                "confidence",
                "metadata",
            },
        )

    async def test_a_structured_answer_becomes_data_not_prose(self) -> None:
        manager = VisionManager(
            provider=FakeVisionProvider(STRUCTURED_ANSWER), ocr=FakeOcrEngine([])
        )
        result = await manager.analyze(VisionRequest("x.png", question="what is this error?"))

        self.assertTrue(result.escalated)
        self.assertEqual(result.image_type, VisionImageType.ERROR_SCREENSHOT)
        self.assertEqual(result.summary, "A dialog reports the upload failed.")
        self.assertEqual([element.label for element in result.ui_elements], ["Retry"])
        self.assertEqual(result.ui_elements[0].kind, "button")
        self.assertEqual(result.ui_elements[0].bounds["width"], 30)
        self.assertEqual(result.errors, ("Upload failed",))
        self.assertTrue(result.relevant_regions)
        self.assertEqual(result.confidence, 0.91)
        self.assertTrue(result.metadata["structured"])

    async def test_the_locate_contract_is_rendered_as_a_sentence(self) -> None:
        # The contract's JSON is a WIRE format. A person reading the summary
        # used to be shown the object itself.
        cases = (
            (
                '{"found": true, "x": 500, "y": 250, "label": "Login"}',
                "where is the login button?",
                "Login is visible at (500, 250) on a 0-1000 grid.",
                True,
            ),
            (
                '{"found": false}',
                "where is the logout button?",
                "No logout button is visible in the image.",
                True,
            ),
            (
                '{"found": true}',
                "where is the save button?",
                "The element was reported as visible, but no position was given.",
                True,
            ),
            (
                '{"found": true, "x": 12, "y": 34}',
                "where is it?",
                "The element is visible at (12, 34) on a 0-1000 grid.",
                True,
            ),
        )
        for answer, question, expected, answered in cases:
            with self.subTest(answer=answer):
                manager = VisionManager(
                    provider=FakeVisionProvider(answer), ocr=FakeOcrEngine([])
                )
                result = await manager.analyze(
                    VisionRequest(
                        "x.png", question=question, task=VisionTaskKind.LOCATE
                    )
                )
                self.assertEqual(result.summary, expected)
                self.assertEqual(result.answered, answered)
                self.assertNotIn("{", result.summary)

    async def test_a_not_found_answer_is_not_confident_about_a_location(self) -> None:
        # There is no located element, so quoting a middle figure beside an
        # empty region list read as though one had been found.
        manager = VisionManager(
            provider=FakeVisionProvider('{"found": false}'), ocr=FakeOcrEngine([])
        )
        result = await manager.analyze(
            VisionRequest(
                "x.png", question="where is the logout button?", task=VisionTaskKind.LOCATE
            )
        )
        self.assertEqual(result.confidence, 0.0)
        self.assertEqual(result.relevant_regions, ())
        self.assertEqual(result.metadata["found"], False)

    async def test_an_unquantified_answer_is_labelled_as_unquantified(self) -> None:
        # A model that gives no figure has not measured one, so the number the
        # pipeline reports is a named placeholder and the basis says so.
        manager = VisionManager(
            provider=FakeVisionProvider("A login form with a disabled submit button."),
            ocr=FakeOcrEngine([]),
        )
        result = await manager.analyze(VisionRequest("x.png", question="what is this?"))
        self.assertEqual(result.metadata["confidence_basis"], "none")
        self.assertEqual(result.confidence, 0.5)
        manager = VisionManager(
            provider=FakeVisionProvider("{\"summary\": \"A dialog.\", \"confidence\": 0.7}"),
            ocr=FakeOcrEngine([]),
        )
        reported = await manager.analyze(VisionRequest("x.png"))
        self.assertEqual(reported.metadata["confidence_basis"], "model-reported")
        self.assertEqual(reported.confidence, 0.7)

    async def test_a_prose_answer_is_still_an_answer(self) -> None:
        manager = VisionManager(
            provider=FakeVisionProvider("A login form with a disabled submit button."),
            ocr=FakeOcrEngine([]),
        )
        result = await manager.analyze(VisionRequest("x.png", question="what is this?"))
        self.assertTrue(result.answered)
        self.assertEqual(result.summary, "A login form with a disabled submit button.")
        self.assertFalse(result.metadata["structured"])
        self.assertEqual(result.ui_elements, ())

    async def test_no_hidden_reasoning_is_exposed(self) -> None:
        manager = VisionManager(
            provider=FakeVisionProvider(STRUCTURED_ANSWER), ocr=FakeOcrEngine([])
        )
        payload = (await manager.analyze(VisionRequest("x.png"))).to_dict()
        flat = repr(payload).lower()
        for banned in ("reasoning", "thinking", "chain of thought", "chain_of_thought"):
            self.assertNotIn(banned, flat)
        # Provenance is present, and it is about the pipeline, not the model.
        self.assertEqual(
            set(payload["metadata"]) & {"answer_source", "escalated", "provider"},
            {"answer_source", "escalated", "provider"},
        )

    async def test_the_older_screen_shape_is_still_produced(self) -> None:
        manager = VisionManager(
            provider=FakeVisionProvider(STRUCTURED_ANSWER), ocr=FakeOcrEngine([])
        )
        result = await manager.analyze(VisionRequest("x.png", question="what is this?"))
        screen = result.to_screen_understanding()
        self.assertEqual(screen.summary, result.summary)
        self.assertTrue(screen.metadata["question_answered"])

    async def test_ocr_geometry_becomes_elements_and_none_invents_any(self) -> None:
        with_geometry = VisionManager(
            provider=NullVisionProvider(),
            ocr=FakeOcrEngine(["Retry"], geometry=True),
        )
        result = await with_geometry.analyze(VisionRequest("x.png", task=VisionTaskKind.OCR))
        self.assertEqual([element.label for element in result.ui_elements], ["Retry"])
        self.assertEqual(result.ui_elements[0].kind, "text")

        without_geometry = VisionManager(
            provider=NullVisionProvider(), ocr=FakeOcrEngine(["Retry"])
        )
        plain = await without_geometry.analyze(
            VisionRequest("x.png", task=VisionTaskKind.OCR)
        )
        self.assertEqual(plain.ui_elements, ())

    async def test_an_empty_model_answer_is_not_an_answer(self) -> None:
        manager = VisionManager(provider=FakeVisionProvider(""), ocr=FakeOcrEngine([]))
        result = await manager.analyze(VisionRequest("x.png", question="what is this?"))
        self.assertFalse(result.answered)
        self.assertEqual(result.metadata["reason"], "the vision model returned nothing usable")

    async def test_a_result_is_json_safe(self) -> None:
        import json

        manager = VisionManager(
            provider=FakeVisionProvider(STRUCTURED_ANSWER), ocr=FakeOcrEngine([])
        )
        payload = (await manager.analyze(VisionRequest("x.png"))).to_dict()
        self.assertIn("image_type", json.loads(json.dumps(payload)))


class ProviderSwapTests(unittest.IsolatedAsyncioTestCase):
    """Swapping models is a method call, not a rebuild."""

    async def test_setting_a_provider_changes_who_answers(self) -> None:
        manager = VisionManager(provider=NullVisionProvider(), ocr=FakeOcrEngine([]))
        self.assertFalse(manager.status()["provider"]["available"])

        replacement = FakeVisionProvider("now I can see")
        manager.set_provider(replacement)

        self.assertEqual(manager.status()["provider"]["name"], "fake")
        result = await manager.analyze(VisionRequest("x.png", question="what is this?"))
        self.assertTrue(result.answered)
        self.assertEqual(result.summary, "now I can see")

    async def test_setting_none_goes_back_to_the_honest_refusal(self) -> None:
        manager = VisionManager(provider=FakeVisionProvider("saw it"), ocr=FakeOcrEngine([]))
        manager.set_provider(None)
        result = await manager.analyze(VisionRequest("x.png", question="what is this?"))
        self.assertFalse(result.answered)
        self.assertEqual(manager.provider.name, "none")


class TaskKindTests(unittest.TestCase):
    """Which cheap path a question picks — read from the words, not guessed."""

    def test_a_where_question_about_a_control_is_a_locator(self) -> None:
        self.assertEqual(
            task_for_question("Where is the login button?"), VisionTaskKind.LOCATE
        )
        self.assertEqual(
            task_for_question("find the settings menu"), VisionTaskKind.LOCATE
        )

    def test_a_read_question_is_ocr_and_never_needs_the_model(self) -> None:
        for question in (
            "Read the text in this screenshot.",
            "extract the text from this image",
            "what does this screenshot say?",
        ):
            self.assertEqual(task_for_question(question), VisionTaskKind.OCR, question)

    def test_an_explicit_target_outranks_the_phrasing(self) -> None:
        self.assertEqual(
            task_for_question("what is this?", target="Retry"), VisionTaskKind.LOCATE
        )

    def test_anything_else_is_an_understand_request(self) -> None:
        self.assertEqual(
            task_for_question("what is this error?"), VisionTaskKind.UNDERSTAND
        )
        # No question at all is a description request, not a text extraction.
        self.assertEqual(task_for_question(""), VisionTaskKind.UNDERSTAND)
        self.assertEqual(task_for_question("   "), VisionTaskKind.UNDERSTAND)


class VisionRoutingTests(unittest.TestCase):
    """Which requests reach the pipeline at all."""

    def setUp(self) -> None:
        os.environ["NOVACONTROL_NLU_ALLOW_LLM"] = "false"
        self.gil = GlobalInputIntelligence()

    def _vision(self, text: str, *, has_image: bool = False) -> bool:
        return self.gil.understand(text, has_image=has_image).intent.requires_vision

    def test_the_specification_s_examples_route_to_vision(self) -> None:
        for text in (
            "What is this error?",
            "Where is the login button?",
            "Read the text in this screenshot.",
        ):
            with self.subTest(text=text):
                self.assertTrue(self._vision(text))

    def test_a_request_about_no_picture_does_not(self) -> None:
        for text in ("Open Chrome.", "delete the file report.pdf", "what is 15% of 240?"):
            with self.subTest(text=text):
                self.assertFalse(self._vision(text))

    def test_a_coding_request_about_an_error_is_not_a_vision_request(self) -> None:
        # The precision case: "this error" points at a picture in one sentence
        # and at a traceback in the next, and the question form is what tells
        # them apart.
        self.assertFalse(self._vision("fix this error"))
        self.assertFalse(self._vision("debug this error in my code"))

    def test_taking_a_screenshot_is_an_action_not_an_analysis(self) -> None:
        self.assertFalse(self._vision("take a screenshot"))

    def test_an_attached_image_forces_the_requirement(self) -> None:
        # "What is this?" is unanswerable without knowing whether a picture came
        # with it, so the caller says so — and the flag is decisive.
        self.assertFalse(self._vision("What is this?"))
        self.assertTrue(self._vision("What is this?", has_image=True))

    def test_the_route_follows_the_widened_requirement(self) -> None:
        intent = self.gil.understand("What is this?", has_image=True).intent
        self.assertEqual(intent.decision.get("route"), "vision")


class VisionConfigTests(unittest.TestCase):
    """Configuration decides the provider — and `none` is a real answer."""

    def test_the_default_is_auto_with_ocr_first(self) -> None:
        settings = NovaControlConfig().vision
        self.assertEqual(settings.provider, "auto")
        self.assertTrue(settings.prefer_ocr)
        self.assertEqual(settings.model, "")

    def test_a_mapping_sets_the_provider_and_model(self) -> None:
        settings = VisionSettings.from_mapping(
            {"provider": "NONE", "model": "qwen2.5vl:3b", "prefer_ocr": False}
        )
        self.assertEqual(settings.provider, "none")
        self.assertEqual(settings.model, "qwen2.5vl:3b")
        self.assertFalse(settings.prefer_ocr)

    def test_an_unknown_provider_keeps_the_default(self) -> None:
        # A typo must not be the reason the vision layer is unreachable.
        self.assertEqual(VisionSettings.from_mapping({"provider": "gemini"}).provider, "auto")

    def test_the_environment_overrides_the_file(self) -> None:
        os.environ["NOVACONTROL_VISION_PROVIDER"] = "none"
        os.environ["NOVACONTROL_VISION_MODEL"] = "llava"
        os.environ["NOVACONTROL_VISION_PREFER_OCR"] = "0"
        try:
            settings = NovaControlConfig.from_environment().vision
        finally:
            for name in (
                "NOVACONTROL_VISION_PROVIDER",
                "NOVACONTROL_VISION_MODEL",
                "NOVACONTROL_VISION_PREFER_OCR",
            ):
                os.environ.pop(name, None)
        self.assertEqual(settings.provider, "none")
        self.assertEqual(settings.model, "llava")
        self.assertFalse(settings.prefer_ocr)

    def test_the_shipped_config_file_carries_the_section(self) -> None:
        config = NovaControlConfig.from_file(Path("configs/default.yaml"))
        self.assertEqual(config.vision.provider, "auto")
        self.assertTrue(config.vision.prefer_ocr)


class ApplicationVisionPipelineTests(unittest.IsolatedAsyncioTestCase):
    """The pipeline as the application wires it."""

    async def asyncSetUp(self) -> None:
        os.environ["NOVACONTROL_NLU_ALLOW_LLM"] = "false"
        from novacontrol.application import NovaControlApplication

        self.app = NovaControlApplication()
        await self.app.start()

    async def asyncTearDown(self) -> None:
        await self.app.stop()

    async def test_configuration_of_none_refuses_a_vision_model(self) -> None:
        """`vision.provider: none` must reach the pipeline, not just the file.

        It is the setting for a deployment that must not run a vision model at
        all, so it has to survive the trip from configuration to the provider
        the manager would actually consult — and the pipeline must then decline
        rather than reach for whatever the panel has installed.
        """
        from novacontrol.application import NovaControlApplication

        os.environ["NOVACONTROL_VISION_PROVIDER"] = "none"
        try:
            app = NovaControlApplication()
            await app.start()
            try:
                self.assertFalse(app.vision_manager.provider.available)
                self.assertEqual(app.vision_manager.provider.name, "none")
                source = app.data_dir / "refused_probe.txt"
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("Login with SSO\n", encoding="utf-8")
                response = await app.handle_request(
                    "where is the login button?", image=str(source)
                )
                self.assertFalse(response.payload["answered"])
                self.assertEqual(
                    response.payload["metadata"]["confidence_basis"],
                    "no-vision-provider",
                )
            finally:
                await app.stop()
        finally:
            os.environ.pop("NOVACONTROL_VISION_PROVIDER", None)

    async def test_the_application_owns_one_manager_and_reports_it(self) -> None:
        status = self.app.status()["vision_pipeline"]
        self.assertIn("provider", status)
        self.assertIn("ocr", status)
        self.assertTrue(status["prefer_ocr"])

    async def test_a_text_source_is_answered_without_a_model_call(self) -> None:
        path = self.app.data_dir / "vision_pipeline_probe.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "Traceback: ValueError: could not convert string to float\n", encoding="utf-8"
        )
        result = await self.app.vision_manager.analyze(
            VisionRequest(str(path), question="what is this error?")
        )
        self.assertTrue(result.answered)
        self.assertFalse(result.escalated)
        self.assertEqual(result.image_type, VisionImageType.ERROR_SCREENSHOT)

    async def test_the_request_path_goes_through_the_manager(self) -> None:
        """The spec's architecture, checked on the live request path.

        A captured screen must reach the MANAGER — capture, read, structure —
        and not the older describe path, because that is the pipeline the plan
        and the UI both read from. ``capture_screen`` is stubbed so the test
        asserts the wiring rather than this machine's ability to screenshot.
        """
        from novacontrol.brain.models import BrainRequest

        source = self.app.data_dir / "request_path_probe.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("ValueError: upload failed\nRetry\n", encoding="utf-8")

        captured: dict[str, Any] = {}

        async def fake_capture(*, save_path: str = "vision_screen.png") -> dict[str, Any]:
            del save_path
            captured["called"] = True
            return {"screenshot": str(source), "captured": True, "vision_model": True}

        self.app.vision.capture_screen = fake_capture  # type: ignore[method-assign]
        route, payload = await self.app._handle_vision(
            BrainRequest(text="what is this error?"), "what is this error?"
        )

        self.assertEqual(route, "vision")
        self.assertTrue(captured.get("called"))
        # The documented structured shape, not a description.
        self.assertEqual(payload["image_type"], VisionImageType.ERROR_SCREENSHOT.value)
        self.assertIn("ValueError: upload failed", payload["errors"])
        self.assertTrue(payload["metadata"]["question_answered"])
        self.assertIsInstance(payload["confidence"], float)
        # And the manager's own result is what was recorded as evidence.
        self.assertIsNotNone(self.app.last_vision_result)
        self.assertEqual(
            self.app.vision_evidence()["image_type"],
            VisionImageType.ERROR_SCREENSHOT.value,
        )

    async def test_a_question_needing_eyes_is_refused_not_answered(self) -> None:
        """A locate question with no model must say so, through the request path."""
        from novacontrol.vision.providers import NullVisionProvider

        source = self.app.data_dir / "locate_probe.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("Welcome\nLogin with SSO\n", encoding="utf-8")
        self.app.vision_manager.set_provider(NullVisionProvider())
        try:
            result = await self.app.analyze_image(
                str(source), question="where is the login button?"
            )
        finally:
            self.app.vision_manager.set_provider(self.app._vision_pipeline_provider())
        payload = result.to_dict()
        self.assertFalse(result.answered)
        self.assertFalse(payload["metadata"]["question_answered"])
        self.assertEqual(payload["metadata"]["confidence_basis"], "no-vision-provider")
        self.assertIn("no vision model", payload["metadata"]["reason"])
        self.assertEqual(payload["detected_text"], ["Welcome", "Login with SSO"])

    async def test_the_plan_carries_the_structured_result_beside_it(self) -> None:
        """The "structured vision result -> planner" hand-off."""
        from novacontrol.brain.models import BrainRequest

        source = self.app.data_dir / "plan_evidence_probe.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("TypeError: expected str, got int\n", encoding="utf-8")
        await self.app.analyze_image(str(source), question="what is this error?")

        _kind, payload = await self.app._handle_plan(
            BrainRequest(text="explain what failed"), "explain what failed"
        )

        self.assertIn("vision", payload)
        self.assertEqual(payload["vision"]["errors"], ["TypeError: expected str, got int"])
        self.assertIn("confidence", payload["vision"])

    async def test_no_plan_evidence_is_reported_when_no_image_was_read(self) -> None:
        self.assertEqual(self.app.vision_evidence(), {})
        self.assertIsNone(self.app.last_vision_result)

    async def test_an_attached_image_needs_vision_and_is_what_gets_read(self) -> None:
        """"An image is attached" — the first routing trigger, end to end.

        The flag has to be SUPPLIED, not inferred, and the pipeline has to read
        the picture that was attached rather than capture the desktop: taking a
        screenshot would answer about the wrong pixels.
        """
        source = self.app.data_dir / "attached_probe.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("Traceback: ValueError: upload failed\nRetry\n", encoding="utf-8")

        captured: list[str] = []

        async def fake_capture(*, save_path: str = "vision_screen.png") -> dict[str, Any]:
            del save_path
            captured.append("capture")
            return {"screenshot": "vision_screen.png", "captured": True}

        self.app.vision.capture_screen = fake_capture  # type: ignore[method-assign]
        response = await self.app.handle_request(
            "what is this error?", image=str(source)
        )

        self.assertEqual(response.route, "vision")
        self.assertTrue(response.payload["nlu"]["requires_vision"])
        self.assertEqual(captured, [])
        self.assertEqual(response.payload["screenshot"], str(source))
        self.assertEqual(response.payload["errors"], ["Traceback: ValueError: upload failed"])
        self.assertTrue(response.payload["question_answered"])
        self.assertTrue(response.payload["answered"])

    async def test_an_attached_image_makes_a_plain_command_need_eyes(self) -> None:
        """The requirement is a FACT about the request, not a reading of it."""
        source = self.app.data_dir / "attached_command_probe.txt"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("Some window text\n", encoding="utf-8")

        with_image = await self.app.handle_request("open chrome", image=str(source))
        without = await self.app.handle_request("open chrome")

        self.assertTrue(with_image.payload["nlu"]["requires_vision"])
        self.assertFalse(without.payload["nlu"]["requires_vision"])

    async def test_an_unreadable_attachment_refuses_instead_of_describing(self) -> None:
        response = await self.app.handle_request(
            "what is this error?", image=str(self.app.data_dir / "nope.png")
        )
        self.assertEqual(response.route, "vision")
        self.assertFalse(response.payload["answered"])
        self.assertIn("could not read an image", str(response.payload["metadata"]["reason"]))

    async def test_re_resolving_the_provider_keeps_the_wiring_honest(self) -> None:
        self.app.vision_manager.set_provider(self.app._vision_pipeline_provider())
        # Whatever this machine has, the resolved provider must be honest about
        # whether it can actually see, and the manager must report the same
        # answer it would act on.
        self.assertIsInstance(self.app.vision_manager.provider.available, bool)
        self.assertEqual(
            self.app.vision_manager.status()["provider"]["available"],
            self.app.vision_manager.provider.available,
        )


if __name__ == "__main__":
    unittest.main()
