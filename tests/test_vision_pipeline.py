"""Phase 6: the vision pipeline answers the question that was asked.

A screen request and a question about a screen are the same pixels and different
work. The pipeline used to hand the vision model a fixed checklist, so "why
isn't the button working?" came back as a general description of the desktop —
correct, unhelpful, and indistinguishable from an answer. These tests pin the
question's whole journey, and the honesty that goes with it:

  * the prompt puts the user's own words first and keeps the checklist;
  * the question reaches the provider inside the multimodal message;
  * the application decides what counts as a question and what is merely the
    instruction to capture;
  * a capture with no vision model declines the question and SAYS SO, instead
    of reporting a window probe as an answer;
  * the routed payload carries whether the question was answered.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from typing import Any

from novacontrol.application import NovaControlApplication
from novacontrol.brain.models import BrainRequest
from novacontrol.vision.multimodal import MultimodalVisionProcessor, _screen_prompt
from novacontrol.vision.processors import BasicScreenUnderstandingProcessor

QUESTION = "why isn't the button working?"


class _ScriptedVisionProvider:
    """Minimal multimodal provider double that records what it was asked."""

    def __init__(self, answer: str = "The button is disabled.") -> None:
        self._answer = answer
        self.calls: list[Any] = []

    @property
    def name(self) -> str:
        # The `vision:` prefix is the surface build_vision_provider marks as
        # already validated for images, so has_vision_model accepts this double
        # for the same reason it accepts a configured VLM.
        return "vision:scripted"

    async def complete(self, messages: Any, **_kwargs: Any) -> str:
        self.calls.append(messages)
        return self._answer


def _png_bytes() -> bytes:
    """A real one-pixel PNG, so the loader's format check passes."""
    import base64

    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8AAAwAB/AF+"
        "GuZgAAAAAElFTkSuQmCC"
    )


class ScreenPromptTests(unittest.TestCase):
    """The prompt: the user's question first, the checklist never dropped."""

    def test_no_question_keeps_the_original_checklist(self) -> None:
        prompt = _screen_prompt("")
        self.assertIn("Analyze this screenshot.", prompt)
        self.assertIn("interactive elements", prompt)

    def test_a_question_leads_the_prompt(self) -> None:
        prompt = _screen_prompt(QUESTION)
        self.assertIn(QUESTION, prompt)
        self.assertTrue(prompt.index(QUESTION) < prompt.index("Describe:"))

    def test_the_checklist_survives_a_question(self) -> None:
        """Answering the question needs the visible errors and elements too."""
        prompt = _screen_prompt(QUESTION)
        self.assertIn("errors, warnings", prompt)
        self.assertIn("interactive elements", prompt)

    def test_the_model_is_told_to_admit_what_the_screen_does_not_show(self) -> None:
        prompt = _screen_prompt(QUESTION)
        self.assertIn("does not", prompt)

    def test_whitespace_in_a_question_is_collapsed(self) -> None:
        self.assertIn(QUESTION, _screen_prompt(f"  {QUESTION}\n\n "))


class ProcessorQuestionTests(unittest.IsolatedAsyncioTestCase):
    """The question reaches the provider inside the multimodal message."""

    async def test_a_question_is_sent_with_the_image(self) -> None:
        provider = _ScriptedVisionProvider()
        processor = MultimodalVisionProcessor(llm_provider=provider)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "screen.png"
            path.write_bytes(_png_bytes())
            understanding = await processor.understand_screen(str(path), question=QUESTION)

        self.assertEqual(understanding.summary, "The button is disabled.")
        content = provider.calls[0][0]["content"]
        text = next(part["text"] for part in content if part["type"] == "text")
        self.assertIn(QUESTION, text)
        self.assertTrue(any(part["type"] == "image_url" for part in content))

    async def test_without_a_question_the_checklist_is_sent(self) -> None:
        provider = _ScriptedVisionProvider()
        processor = MultimodalVisionProcessor(llm_provider=provider)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "screen.png"
            path.write_bytes(_png_bytes())
            await processor.understand_screen(str(path))

        text = next(
            part["text"] for part in provider.calls[0][0]["content"] if part["type"] == "text"
        )
        self.assertIn("Analyze this screenshot.", text)
        self.assertNotIn(QUESTION, text)

    async def test_the_baseline_declines_a_question_and_says_so(self) -> None:
        """No vision model means no answer — and the metadata admits it."""
        processor = BasicScreenUnderstandingProcessor()
        understanding = await processor.understand_screen("screen.png", question=QUESTION)
        self.assertFalse(understanding.metadata.get("question_answered"))
        self.assertEqual(understanding.metadata.get("question"), QUESTION)
        self.assertIn("reason", understanding.metadata)

    async def test_the_baseline_without_a_question_carries_no_metadata(self) -> None:
        processor = BasicScreenUnderstandingProcessor()
        understanding = await processor.understand_screen("screen.png")
        self.assertEqual(dict(understanding.metadata), {})


class VisionQuestionSelectionTests(unittest.IsolatedAsyncioTestCase):
    """What counts as a question about the screen, and what is just the ask."""

    async def asyncSetUp(self) -> None:
        self.app = NovaControlApplication()

    async def asyncTearDown(self) -> None:
        await self.app.stop()

    def _request(self, text: str, *, goal: str = "") -> BrainRequest:
        nlu: dict[str, Any] = {"intent": "screenshot_analysis", "confidence": 0.9}
        if goal:
            nlu["goal"] = goal
        return BrainRequest(text=text, context={"nlu": nlu})

    def test_the_understood_goal_wins(self) -> None:
        request = self._request("look at this screenshot and tell me why", goal=QUESTION)
        self.assertEqual(self.app._vision_question(request), QUESTION)

    def test_a_bare_look_is_not_a_question(self) -> None:
        for text in ("look at the screen", "describe the screenshot", "please check my screen"):
            with self.subTest(text=text):
                self.assertEqual(self.app._vision_question(self._request(text)), "")

    def test_a_real_question_travels(self) -> None:
        request = self._request("why is the button greyed out on my screen?")
        self.assertEqual(
            self.app._vision_question(request),
            "why is the button greyed out on my screen?",
        )

    def test_without_an_nlu_block_the_raw_text_is_used(self) -> None:
        request = BrainRequest(text="what error is showing?", context={})
        self.assertEqual(self.app._vision_question(request), "what error is showing?")

    async def test_the_handler_sends_the_question_and_reports_the_outcome(
        self,
    ) -> None:
        """Stubbed at the controller, so no screen is captured in a test."""
        asked: list[str] = []

        class _StubVision:
            async def describe_screen(self, *, question: str = "") -> dict[str, Any]:
                asked.append(question)
                return {"captured": True, "message": "stub"} if not question else {
                    "captured": True,
                    "question": question,
                    "message": "stub",
                }

        original = self.app.vision
        self.app.vision = _StubVision()  # type: ignore[assignment]
        try:
            _route, payload = await self.app._handle_vision(
                self._request("what error is showing?"), "what error is showing?"
            )
            _route, bare = await self.app._handle_vision(
                self._request("describe the screen"), "describe the screen"
            )
        finally:
            self.app.vision = original

        self.assertEqual(asked, ["what error is showing?", ""])
        self.assertEqual(payload["question"], "what error is showing?")
        self.assertTrue(payload["question_answered"])
        self.assertNotIn("question", bare)
        self.assertFalse(bare["question_answered"])


class QuestionReachesTheControllerTests(unittest.IsolatedAsyncioTestCase):
    """The controller passes the question down, and reports the answer honestly."""

    async def asyncSetUp(self) -> None:
        self.app = NovaControlApplication()

    async def asyncTearDown(self) -> None:
        await self.app.stop()

    async def test_a_question_is_answered_when_a_vision_model_is_wired(self) -> None:
        provider = _ScriptedVisionProvider()
        controller = self.app.vision
        controller.set_llm_provider(provider)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "vision_screen.png"
            path.write_bytes(_png_bytes())
            understanding = await controller._understand(str(path), question=QUESTION)

        self.assertEqual(understanding["source"], "vision_model")
        self.assertTrue(understanding["question_answered"])
        self.assertEqual(understanding["question"], QUESTION)

    async def test_without_a_vision_model_the_question_is_reported_undelivered(self) -> None:
        controller = self.app.vision
        controller.set_llm_provider(None)
        understanding = await controller._understand("missing.png", question=QUESTION)
        self.assertEqual(understanding["source"], "window_probe")
        self.assertFalse(understanding["question_answered"])
        self.assertEqual(understanding["question"], QUESTION)


if __name__ == "__main__":
    unittest.main()
