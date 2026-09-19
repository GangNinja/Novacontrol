"""Explore answers the QUESTION, not the topic's keywords.

Three contracts, all in service of the same failure (a research answer that
re-listed keyword-matched source blurbs instead of answering the question):

1. **Planning** — when a synthesis model is connected, IT chooses the search
   queries, from the user's own words. No keyword rule table decides.
2. **Answering** — the synthesis prompt carries the user's question (and the
   planner's reading of it), and a well-formed answer is used verbatim.
3. **Honesty** — when no model can write the answer, the report says so, and
   the digest it falls back to contains no video descriptions, no pasted URLs,
   and no truncated-sentence artifacts.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import unittest

from novacontrol.explore import ExploreRequest, ExploreService, ResearchSource
from novacontrol.explore.explainer import ResearchExplainer
from novacontrol.explore.planner import ResearchPlan, parse_plan, plan_research
from novacontrol.explore.synthesizer import clean_snippet, extract_facts

from conftest import FakeVideoProvider


# ────────────────────────────────────────────────────────────
# Test doubles
# ────────────────────────────────────────────────────────────

class RecordingSearchProvider:
    """Search provider that records every query it is asked."""

    def __init__(self, results: Sequence[ResearchSource] = ()) -> None:
        self.queries: list[str] = []
        self._results = tuple(results)

    async def search(self, query: str, *, limit: int = 6) -> tuple[ResearchSource, ...]:
        self.queries.append(query)
        return self._results[:limit]


class PlanningModel:
    """Model that plans searches and then answers from the research."""

    name = "test-planner"

    def __init__(self, plan_json: str, answer: str) -> None:
        self._plan_json = plan_json
        self._answer = answer
        self.prompts: list[str] = []
        self.system_prompts: list[str] = []
        self.last_error = ""

    async def complete(self, messages: Sequence[Mapping[str, str]], **kwargs: object) -> str:
        system = next((str(m.get("content", "")) for m in messages if m.get("role") == "system"), "")
        user = next((str(m.get("content", "")) for m in messages if m.get("role") == "user"), "")
        self.system_prompts.append(system)
        self.prompts.append(user)
        return self._plan_json if "plan web searches" in system else self._answer


class BrokenModel:
    """Model that is connected but cannot be reached (dead key, dead Ollama)."""

    name = "cloud:broken"
    last_error = "HTTPError: HTTP Error 404: Not Found"

    async def complete(self, messages: Sequence[Mapping[str, str]], **kwargs: object) -> str:
        raise RuntimeError("HTTP Error 404: Not Found")


class EchoModel:
    name = "echo"

    async def complete(self, messages: Sequence[Mapping[str, str]], **kwargs: object) -> str:
        return str(messages[-1].get("content", ""))


_PLAN = '{"focus": "why cats purr and what each purr means", "queries": ["do cats purr when in pain", "cat purr science vocal folds"]}'

_PURR_SOURCES = (
    ResearchSource(
        "Why do cats purr? The science of cat purring",
        "https://vet.example.com/purring",
        "Cats purr through rapid vocal fold vibration, and purring can signal pain as well as comfort.",
    ),
    ResearchSource(
        "Cat purring meaning explained",
        "https://cat.example.org/purr",
        "Cats purr when content, hungry, or hurt; the vibration itself may promote healing.",
    ),
)


# ────────────────────────────────────────────────────────────
# 1. Planning
# ────────────────────────────────────────────────────────────

class PlanParsingTests(unittest.TestCase):
    """The plan parser accepts real model output and rejects everything else."""

    def test_parses_plain_json(self) -> None:
        plan = parse_plan(_PLAN)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.queries, ("do cats purr when in pain", "cat purr science vocal folds"))
        self.assertIn("why cats purr", plan.focus)

    def test_parses_json_inside_a_markdown_fence(self) -> None:
        plan = parse_plan(f"```json\n{_PLAN}\n```")
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(len(plan.queries), 2)

    def test_parses_json_after_preamble_prose(self) -> None:
        plan = parse_plan(f"Here is the plan you asked for:\n{_PLAN}")
        self.assertIsNotNone(plan)

    def test_strips_bullets_and_duplicates(self) -> None:
        plan = parse_plan('{"queries": ["- 1. cat purr signals", "cat purr signals", "2) purr healing"]}')
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(plan.queries, ("cat purr signals", "purr healing"))

    def test_rejects_prose_without_json(self) -> None:
        """Model prose is not a plan: searching it would search nonsense."""
        self.assertIsNone(parse_plan("Cats purr for several reasons, including comfort. Sources: petmd, catster."))

    def test_rejects_empty_and_oversized_queries(self) -> None:
        self.assertIsNone(parse_plan('{"queries": []}'))
        self.assertIsNone(parse_plan('{"queries": ["   "]}'))
        self.assertIsNone(parse_plan('{"queries": ["' + "x" * 400 + '"]}'))

    def test_caps_query_count(self) -> None:
        plan = parse_plan('{"queries": ["a one", "b two", "c three", "d four", "e five"]}', max_queries=3)
        self.assertIsNotNone(plan)
        assert plan is not None
        self.assertEqual(len(plan.queries), 3)

    def test_plan_truthiness_follows_queries(self) -> None:
        self.assertFalse(bool(ResearchPlan(focus="f", queries=())))
        self.assertTrue(bool(ResearchPlan(focus="f", queries=("q",))))


class PlanRequestTests(unittest.IsolatedAsyncioTestCase):
    """plan_research returns None instead of raising, in every dead-end case."""

    async def test_no_provider_means_no_plan(self) -> None:
        self.assertIsNone(await plan_research(None, "why do cats purr"))

    async def test_echo_provider_is_skipped(self) -> None:
        self.assertIsNone(await plan_research(EchoModel(), "why do cats purr"))

    async def test_provider_failure_is_swallowed(self) -> None:
        self.assertIsNone(await plan_research(BrokenModel(), "why do cats purr"))

    async def test_blank_question_means_no_plan(self) -> None:
        model = PlanningModel(_PLAN, "unused")
        self.assertIsNone(await plan_research(model, "   "))

    async def test_plan_reaches_the_model_with_the_question_and_context(self) -> None:
        model = PlanningModel(_PLAN, "unused")
        plan = await plan_research(model, "why do cats purr", prior_topics=("cats",))
        self.assertIsNotNone(plan)
        self.assertIn("why do cats purr", model.prompts[-1])
        self.assertIn("cats", model.prompts[-1])


# ────────────────────────────────────────────────────────────
# 2. Answering
# ────────────────────────────────────────────────────────────

class PlannedResearchTests(unittest.IsolatedAsyncioTestCase):
    """A connected model drives both the searches and the answer."""

    def _service(self, model: object, search: object) -> ExploreService:
        return ExploreService(
            search_provider=search,  # type: ignore[arg-type]
            video_provider=FakeVideoProvider(),
            completion_provider=model,
        )

    async def test_planned_queries_are_the_ones_searched(self) -> None:
        search = RecordingSearchProvider(_PURR_SOURCES)
        model = PlanningModel(_PLAN, "Cats purr for comfort, but also for hunger and pain.")
        report = await self._service(model, search).research(ExploreRequest("why do cats purr"))

        self.assertEqual(search.queries, ["do cats purr when in pain", "cat purr science vocal folds"])
        self.assertIsNotNone(report)

    async def test_stranger_angle_from_the_plan_still_counts_as_relevant(self) -> None:
        """"cat purr science vocal folds" is evidence the literal topic words miss."""
        search = RecordingSearchProvider(
            (
                ResearchSource(
                    "Feline vocal fold vibration",
                    "https://vet.example.com/vocal-folds",
                    "Vocal fold vibration in cats produces the purr; pain and healing both change it.",
                ),
            )
        )
        model = PlanningModel(_PLAN, "Cats purr by vibrating their vocal folds.")
        report = await self._service(model, search).research(ExploreRequest("why do cats purr"))

        self.assertTrue(report.sources, "the model-chosen angle must survive the relevance gate")

    async def test_model_answer_is_used_verbatim(self) -> None:
        search = RecordingSearchProvider(_PURR_SOURCES)
        model = PlanningModel(_PLAN, "Cats purr for comfort, and also when hungry or in pain.")
        report = await self._service(model, search).research(ExploreRequest("why do cats purr"))

        self.assertEqual(report.answer, "Cats purr for comfort, and also when hungry or in pain.")
        self.assertEqual(report.warnings, (), "an answered question needs no provenance apology")

    async def test_synthesis_prompt_carries_the_question_and_the_focus(self) -> None:
        search = RecordingSearchProvider(_PURR_SOURCES)
        model = PlanningModel(_PLAN, "Cats purr for comfort, and also when hungry or in pain.")
        await self._service(model, search).research(ExploreRequest("why do cats purr"))

        synthesis_prompt = model.prompts[-1]
        self.assertIn("Question: why do cats purr", synthesis_prompt)
        self.assertIn("why cats purr and what each purr means", synthesis_prompt)
        self.assertIn("do not paste snippet text", synthesis_prompt.lower())

    async def test_question_survives_followup_resolution(self) -> None:
        """A vague follow-up is asked about its referent, not about the phrase."""
        search = RecordingSearchProvider(_PURR_SOURCES)
        model = PlanningModel(_PLAN, "Cats purr for comfort, and also when hungry or in pain.")
        service = self._service(model, search)
        await service.research(ExploreRequest("why do cats purr"))
        await service.research(ExploreRequest("tell me more about that"))

        self.assertIn("Question: cats purr", model.prompts[-1])


# ────────────────────────────────────────────────────────────
# 3. Honesty when no model can answer
# ────────────────────────────────────────────────────────────

class HonestFallbackTests(unittest.IsolatedAsyncioTestCase):

    def _service(self, provider: object | None) -> ExploreService:
        return ExploreService(
            search_provider=RecordingSearchProvider(_PURR_SOURCES),
            video_provider=FakeVideoProvider(),
            completion_provider=provider,
        )

    async def test_no_model_says_so(self) -> None:
        report = await self._service(None).research(ExploreRequest("why do cats purr"))
        self.assertTrue(any("No model is connected" in w for w in report.warnings))

    async def test_broken_model_names_the_failure(self) -> None:
        """A dead cloud key must be visible, not silently downgraded to blurbs."""
        report = await self._service(BrokenModel()).research(ExploreRequest("why do cats purr"))
        note = next((w for w in report.warnings if "cloud:broken" in w), "")
        self.assertIn("404", note)

    async def test_model_answer_reports_no_synthesis_note(self) -> None:
        report = await self._service(PlanningModel(_PLAN, "Cats purr for comfort and for pain.")).research(
            ExploreRequest("why do cats purr")
        )
        self.assertFalse([w for w in report.warnings if "synthesis model" in w.lower()])

    async def test_fallback_answer_is_attributed_to_its_sources(self) -> None:
        """No model: the answer is assembled from source sentences and names them.

        It must NOT be framed as "here is what the sources say about X" — that
        line answers nothing, and it was the shape of the failure this suite
        exists to prevent.
        """
        report = await self._service(None).research(ExploreRequest("why do cats purr"))
        self.assertIn("Cats purr", report.answer)
        self.assertIn("Sources:", report.answer)
        self.assertIn("vet.example.com", report.answer)
        self.assertNotIn("what the sources say", report.answer.lower())


class DigestHygieneTests(unittest.TestCase):
    """The no-model digest carries evidence, not video blurbs or URL fragments."""

    def test_video_pages_never_become_facts(self) -> None:
        sources = (
            ResearchSource(
                "Cat Purring Has To Be The Most Relaxing Sound Ever",
                "https://www.youtube.com/watch?v=joilTJ7f8KY",
                "Stop and listen to these cats purring for 3 minutes to feel deeply calm. Introducing Dodo swag!",
            ),
            ResearchSource(
                "Why do cats purr?",
                "https://www.petmd.com/cat/behavior/why-do-cats-purr",
                "Cats purr to self-soothe and to communicate, and purring continues when they are in pain.",
            ),
        )
        facts = extract_facts(sources)
        self.assertTrue(facts)
        self.assertFalse([f for f in facts if "dodo" in f.lower()])

    def test_url_fragments_are_stripped(self) -> None:
        cleaned = clean_snippet("Read the full guide https:/./broken-link to understand why cats purr.")
        self.assertNotIn("http", cleaned.lower())

    def test_leading_teaser_is_dropped(self) -> None:
        cleaned = clean_snippet(
            'Ever wonder, "why do cats purr?" Cats purr when they are content, and also when they are hurt.'
        )
        self.assertFalse(cleaned.lower().startswith("ever wonder"))
        self.assertIn("Cats purr when they are content", cleaned)

    def test_truncated_snippet_keeps_its_ellipsis(self) -> None:
        """A dangling 'or.' reads as a finished sentence; '…' reads as cut off."""
        cleaned = clean_snippet("But sometimes cat purring is due to stress or\u2026")
        self.assertTrue(cleaned.endswith("\u2026"), cleaned)
        self.assertFalse(cleaned.endswith("or."))

    def test_explainer_property_exposes_the_live_provider(self) -> None:
        explainer = ResearchExplainer(completion_provider=None)
        self.assertIsNone(explainer.completion_provider)
        model = BrokenModel()
        explainer.set_completion_provider(model)
        self.assertIs(explainer.completion_provider, model)


if __name__ == "__main__":
    unittest.main()
