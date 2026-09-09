"""Drift guard for the scratch intent registry (_INTENT_ROWS).

The registry is ONE table wiring detection, routing, answers, and examples
together. Two drifts to catch:

  - a row edit that keeps detection but breaks its answer builder (or the
    reverse), so a phrase is classified as an intent yet yields nothing
  - an example that no longer matches its own row — stale documentation
    silently rotting into a false contract

Every example must route through BOTH public surfaces (_classify for the
engine, scratchable_intent for the narrow decide() gate — the latter only for
routing_safe rows) to its OWN kind, and ScratchReasoningEngine.answer must
return a non-empty message carrying that intent.
"""
from __future__ import annotations

import unittest

from novacontrol.brain.scratch import (
    _INTENT_ROWS,
    ScratchReasoningEngine,
    _classify,
    scratchable_intent,
)

_ENGINE = ScratchReasoningEngine()
_CONTEXT: dict[str, object] = {"modules": ("explore", "memory"), "desktop_available": True}


class IntentRegistryDriftGuardTests(unittest.TestCase):
    def test_every_row_has_examples(self) -> None:
        for row in _INTENT_ROWS:
            with self.subTest(row=row.kind):
                self.assertTrue(row.examples, f"intent {row.kind!r} has no examples")

    def test_every_example_classifies_to_its_row(self) -> None:
        for row in _INTENT_ROWS:
            for example in row.examples:
                with self.subTest(row=row.kind, example=example):
                    self.assertEqual(
                        _classify(example),
                        row.kind,
                        f"example {example!r} no longer classifies as {row.kind!r}",
                    )

    def test_routing_safe_examples_clear_the_narrow_gate(self) -> None:
        for row in _INTENT_ROWS:
            if not row.routing_safe:
                continue
            for example in row.examples:
                with self.subTest(row=row.kind, example=example):
                    self.assertEqual(
                        scratchable_intent(example),
                        row.kind,
                        f"routing_safe row {row.kind!r} example {example!r} "
                        "does not clear the narrow routing gate",
                    )

    def test_every_example_answers_non_empty_with_its_intent(self) -> None:
        for row in _INTENT_ROWS:
            for example in row.examples:
                with self.subTest(row=row.kind, example=example):
                    built = _ENGINE.answer(example, _CONTEXT)
                    self.assertTrue(built["message"].strip(), "empty answer")
                    self.assertEqual(
                        built["intent"], row.kind, "engine intent diverged from row"
                    )


if __name__ == "__main__":
    unittest.main()
