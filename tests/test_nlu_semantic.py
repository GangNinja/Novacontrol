"""The optional semantic layer: embeddings as evidence, never as a guess.

The lexical layer needs shared words ("which programs are eating my memory"
reaches ``memory_status`` because both say "memory"). An embedding index reads
the paraphrases that share no words — but a similarity is only as good as its
threshold, and the measurement here is the whole point of the layer's design:

* the index is deterministic (same vectors, same answers, across processes);
* it is cached, because it sits on the path a user waits on;
* a match below the measured floor is declined, NOT acted on — an embedding
  that cannot separate a request from nonsense must not select a tool;
* the floor is on the CALIBRATED confidence, not the raw similarity, so a high
  cosine against the wrong intent (nonsense scoring 0.61 against ``open_folder``
  because "show me ..." looks like every "show me ..." exemplar) still declines;
* a supplied model backend replaces the local vector space without touching the
  engine — that is what makes the layer optional rather than mandatory.
"""

from __future__ import annotations

import time
import unittest
from typing import Any

from novacontrol.intelligence import (
    GlobalInputIntelligence,
    NluThresholds,
    default_catalog,
    default_exemplars,
)
from novacontrol.intelligence.intent import IntentName
from novacontrol.intelligence.normalize import normalize
from novacontrol.intelligence.scoring import (
    ConfidenceSignals,
    ambiguity_from,
    score_confidence,
)
from novacontrol.intelligence.semantic import (
    EmbeddingIndex,
    HashingEmbedder,
    SemanticMatch,
    content_words,
)


def _engine(**kwargs: Any) -> GlobalInputIntelligence:
    """A deterministic-only engine (no provider) unless a test passes one."""
    return GlobalInputIntelligence(**kwargs)


def _index() -> EmbeddingIndex:
    return EmbeddingIndex(default_exemplars(), catalog=default_catalog())


def _calibrated(ranked: tuple[SemanticMatch, ...]) -> float:
    """The same arithmetic the engine applies, so tests and engine agree."""
    ambiguity, margin = ambiguity_from(tuple(match.score for match in ranked))
    return score_confidence(
        ConfidenceSignals(
            semantic_score=ranked[0].score,
            ambiguity=ambiguity,
            candidate_margin=margin,
            context_available=False,
            strategy="embedding",
        )
    ).confidence


class IndexContractTests(unittest.TestCase):
    """The published shape of a candidate, and the caching around it."""

    def test_candidate_carries_the_agreed_keys(self) -> None:
        match = _index().best("which programs are eating my memory")
        self.assertIsNotNone(match)
        assert match is not None
        payload = match.to_dict()
        self.assertEqual(payload["candidate_intent"], "memory_status")
        self.assertIsInstance(payload["score"], float)
        self.assertEqual(payload["matched_examples"], ["which programs are eating my memory"])
        # The tool the candidate would reach travels with it, so a caller can
        # see what acting on this match would mean before acting on it.
        self.assertEqual(payload["tool"], "system_monitor")

    def test_rank_returns_intents_best_first_without_duplicates(self) -> None:
        ranked = _index().rank("open chrome", limit=4)
        intents = [match.intent for match in ranked]
        self.assertEqual(len(intents), len(set(intents)), "one row per intent")
        scores = [match.score for match in ranked]
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_results_are_cached_and_counted(self) -> None:
        index = _index()
        first = index.rank("show my ram", limit=2)
        second = index.rank("show my ram", limit=2)
        self.assertEqual(first, second)
        self.assertEqual(index.hits, 1)
        self.assertEqual(index.misses, 1)
        self.assertEqual(index.to_dict()["cached_queries"], 1)

    def test_same_input_scores_identically_in_a_fresh_index(self) -> None:
        """Determinism across processes: ``hash()`` is salted, blake2b is not."""
        one = _index().best("what is consuming my ram")
        two = _index().best("what is consuming my ram")
        assert one is not None and two is not None
        self.assertEqual(one.score, two.score)
        self.assertEqual(one.intent, two.intent)

    def test_unknown_text_scores_low_everywhere(self) -> None:
        match = _index().best("florb zzzz quux")
        if match is not None:
            self.assertLess(match.score, 0.5)

    def test_size_is_the_indexed_corpus(self) -> None:
        index = _index()
        expected = sum(len(phrases) for _, phrases in default_exemplars())
        self.assertEqual(index.size, expected)
        self.assertEqual(index.embedder_name, "hashing")

    def test_a_supplied_backend_replaces_the_local_vector_space(self) -> None:
        """The seam: a deployment with embeddings supplies them, nothing else."""

        class RecordingEmbedder:
            name = "recording"

            def __init__(self) -> None:
                self.calls: list[tuple[str, ...]] = []

            def embed(self, texts: Any) -> list[list[float]]:
                self.calls.append(tuple(texts))
                # 3 dimensions is enough to prove the seam; the index must not
                # care how many a backend returns.
                return [[float(len(t) % 7), 1.0, 0.5] for t in texts]

        embedder = RecordingEmbedder()
        index = EmbeddingIndex(default_exemplars(), embedder=embedder)
        self.assertEqual(index.embedder_name, "recording")
        self.assertTrue(embedder.calls, "the backend was used to build the index")
        self.assertIsNotNone(index.best("open chrome"))

    def test_empty_corpus_and_empty_query_decline(self) -> None:
        empty = EmbeddingIndex([])
        self.assertEqual(empty.size, 0)
        self.assertEqual(empty.rank("open chrome"), ())
        self.assertIsNone(empty.best("open chrome"))
        self.assertEqual(_index().rank(""), ())


class CalibrationTests(unittest.TestCase):
    """The floor is a decision about evidence, not a similarity knob."""

    def test_calibration_weights_an_embedding_as_evidence(self) -> None:
        """An embedding match is not 35% of a blend — it is the whole reading."""
        score = score_confidence(ConfidenceSignals(semantic_score=0.9, strategy="embedding"))
        self.assertGreater(score.confidence, 0.8)
        self.assertIn("matched by embedding similarity", score.notes)
        self.assertEqual(score.parts["evidence"], 0.9)

    def test_a_rule_is_never_overruled_by_a_feel_alike(self) -> None:
        """When both exist, the rule still carries the reading (65/35 blend)."""
        blended = score_confidence(ConfidenceSignals(rule_strength=0.9, semantic_score=0.2))
        # The feel-alike's 0.2 enters as 35% of a blend, not as the evidence.
        self.assertGreater(blended.parts["evidence"], 0.5)
        self.assertLess(blended.parts["evidence"], 0.7)
        self.assertEqual(blended.parts["semantic_score"], 0.2)

    def test_ambiguity_lowers_an_embedding_reading(self) -> None:
        clear = score_confidence(
            ConfidenceSignals(semantic_score=0.8, ambiguity=0.0, candidate_margin=1.0)
        )
        tied = score_confidence(
            ConfidenceSignals(semantic_score=0.8, ambiguity=0.9, candidate_margin=0.02)
        )
        self.assertLess(tied.confidence, clear.confidence)

    def test_nonsense_does_not_clear_the_default_floor(self) -> None:
        """The measured case: a high raw score that must still decline.

        "show me the florb" scores ~0.6 raw against "show me the documents
        folder" while being a near-tie with "show me the contents of notes.txt" —
        raw similarity alone would act on it.
        """
        ranked = _index().rank(normalize("show me the florb"), limit=2)
        self.assertTrue(ranked, "there is always a nearest exemplar")
        self.assertLess(_calibrated(ranked), NluThresholds().semantic_confidence)

    def test_a_question_about_resources_is_not_read_as_a_neighbour(self) -> None:
        """"how much ram do i have" must not match battery by sentence shape."""
        match = _index().best("how much ram do i have")
        assert match is not None
        self.assertEqual(match.intent, IntentName.MEMORY_STATUS)

    def test_content_words_drop_function_words_only(self) -> None:
        # Function words out — including the question words, which is what stops
        # "how much ram do i have" from matching "how much battery do i have".
        self.assertEqual(content_words("how much ram do i have"), ["ram"])
        # A VERB survives: it is what says which intent this is, not noise.
        self.assertEqual(content_words("open report-final.pdf"), ["open", "report-final.pdf"])


class EngineIntegrationTests(unittest.TestCase):
    """What the engine does with the layer: read it, or decline it."""

    def test_an_exact_exemplar_is_acted_on_when_lexical_is_off(self) -> None:
        """With the term-matching layer disabled the embedding still reads it."""
        engine = _engine(thresholds=NluThresholds(lexical_matching=False))
        understood = engine.understand("thanks that helped")
        self.assertEqual(understood.intent.intent, IntentName.CONVERSATION)
        self.assertEqual(understood.strategy, "embedding")
        self.assertIn("semantic_match", understood.intent.parameters)

    def test_switching_semantic_matching_off_removes_the_layer(self) -> None:
        engine = _engine(
            thresholds=NluThresholds(lexical_matching=False, semantic_matching=False)
        )
        understood = engine.understand("thanks that helped")
        self.assertEqual(understood.intent.intent, IntentName.CLARIFY)
        self.assertEqual(understood.strategy, "clarification")

    def test_the_layer_never_reports_itself_as_the_model(self) -> None:
        """A local vector match must not be described as a language-model call."""
        engine = _engine(thresholds=NluThresholds(lexical_matching=False))
        understood = engine.understand("thanks that helped")
        self.assertFalse(understood.intent.requires_llm)
        self.assertNotEqual(understood.strategy, "semantic")

    def test_a_declined_match_falls_through_instead_of_fabricating(self) -> None:
        """Nonsense keeps the "not understood" signal a multi-clause request needs."""
        engine = _engine(thresholds=NluThresholds(allow_llm=False))
        understood = engine.understand("open notepad and show me the florb")
        self.assertEqual(understood.intent.unresolved_steps, ("show me the florb",))
        self.assertEqual(understood.intents[0].intent, IntentName.OPEN_APPLICATION)

    def test_embedding_readings_are_not_taught_as_variations(self) -> None:
        """Similarity is not proof: it must not become a deterministic rule."""
        engine = _engine(thresholds=NluThresholds(lexical_matching=False))
        engine.understand("thanks that helped")
        self.assertIsNone(engine.registry.variant_intent("thanks that helped"))

    def test_an_embedding_reading_is_capped_at_the_fast_band(self) -> None:
        """Similarity never earns MORE than a deterministic match does."""
        engine = _engine(thresholds=NluThresholds(lexical_matching=False))
        understood = engine.understand("thanks that helped")
        self.assertLessEqual(understood.intent.confidence, NluThresholds().fast_confidence)
        # And the evidence travels with it, so the reading is auditable.
        self.assertIn("confidence_parts", understood.intent.parameters)

    def test_layer_latency_is_attributed_per_layer(self) -> None:
        engine = _engine(thresholds=NluThresholds(lexical_matching=False))
        understood = engine.understand("thanks that helped")
        layers = understood.intent.parameters.get("layer_ms", {})
        self.assertIn("rules", layers)
        self.assertIn("semantic", layers)
        self.assertNotIn("model", layers, "no model was consulted")

    def test_the_index_is_built_lazily(self) -> None:
        """A process that never needs the semantic layer never pays for it."""
        engine = _engine()
        self.assertIsNone(engine._semantic)
        engine.understand("open chrome")
        self.assertIsNone(engine._semantic, "a rule match needs no index")


class MeasuredCurveTests(unittest.TestCase):
    """The floor's justification, kept honest in the suite.

    A leave-one-out run over the whole corpus (rebuild the index without the
    query, then read it back) is the only way to know what a threshold MEANS.
    The full 200-phrase sweep is too slow for the suite, so this checks the
    properties the threshold depends on: the correct intent is usually the top
    candidate for its own phrasings, and a wrong top candidate is not scored
    high by accident.
    """

    def test_the_default_floor_is_worth_acting_on(self) -> None:
        """The threshold's justification, measured over the whole corpus.

        Leave-one-out: rebuild the index without the query, read it back, and
        score every decision the way the engine would. Per-phrase read-back is
        NOT reliable on this corpus — "open chrome" lands on ``browser_action``
        because that exemplar contains the phrase — which is exactly why the
        floor is on the calibrated confidence rather than on similarity. What
        must hold is the aggregate: most readings that clear the floor are
        right, and the floor declines most of the wrong ones.
        """
        pairs = [(intent, phrase) for intent, phrases in default_exemplars() for phrase in phrases]
        floor = NluThresholds().semantic_confidence
        decided = []
        for index, (intent, phrase) in enumerate(pairs):
            others = [(i, (p,)) for j, (i, p) in enumerate(pairs) if j != index]
            ranked = EmbeddingIndex(others).rank(normalize(phrase), limit=2)
            if not ranked:
                continue
            decided.append((ranked[0].intent is intent, _calibrated(ranked)))
        self.assertGreater(len(decided), 100)
        acted = [correct for correct, confidence in decided if confidence >= floor]
        self.assertTrue(acted, "the floor must not decline everything")
        precision = sum(1 for correct in acted if correct) / len(acted)
        coverage = len(acted) / len(decided)
        # Measured on the shipped corpus: 62% precision at 8% coverage.
        self.assertGreaterEqual(precision, 0.55, f"precision {precision:.1%} is too low")
        self.assertLessEqual(coverage, 0.30, f"coverage {coverage:.1%} is too high")
        declined_wrong = [c for c, conf in decided if not c and conf < floor]
        wrong = [c for c, _ in decided if not c]
        self.assertGreaterEqual(len(declined_wrong), 0.6 * len(wrong))

    def test_rank_work_is_bounded_on_the_shipped_corpus(self) -> None:
        """The layer sits on a user's critical path; a query is milliseconds."""
        index = _index()
        index.rank("warm the cache")
        started = time.perf_counter()
        for _ in range(20):
            index.rank("which programs are eating my memory")
        elapsed = (time.perf_counter() - started) / 20 * 1000
        self.assertLess(elapsed, 5.0, f"a cached query took {elapsed:.2f} ms")


class DefaultEmbedderTests(unittest.TestCase):
    """The always-available backend, without a model or a download."""

    def test_vectors_are_unit_length_and_stable(self) -> None:
        embedder = HashingEmbedder()
        vector = list(embedder.embed(["open chrome"])[0])
        norm = sum(value * value for value in vector) ** 0.5
        self.assertAlmostEqual(norm, 1.0, places=9)
        self.assertEqual(vector, list(embedder.embed(["open chrome"])[0]))

    def test_sparse_and_dense_agree(self) -> None:
        """The fast path and the published contract must not drift apart.

        ``sparse`` is the same vector without the empty slots, so the two must
        be proportional — same direction, whatever the length.
        """
        embedder = HashingEmbedder()
        text = "which programs are eating my memory"
        dense = list(embedder.embed([text])[0])
        sparse = embedder.sparse(text)
        # Same non-zero dimensions...
        self.assertEqual(set(sparse), {i for i, value in enumerate(dense) if value})
        # ...and the dense one is the sparse one scaled to unit length.
        scale = sum(value * value for value in sparse.values()) ** 0.5
        for dimension, value in sparse.items():
            self.assertAlmostEqual(dense[dimension], value / scale, places=9)
        self.assertAlmostEqual(sum(v * v for v in dense) ** 0.5, 1.0, places=9)


if __name__ == "__main__":
    unittest.main()
