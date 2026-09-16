"""The Build coding agent: draft → run → diagnose → fix → re-run.

A one-shot LLM draft can look right and still be broken. The agent verifies:
it executes the artifact in a sandboxed subprocess, feeds the REAL error back
to the model, and iterates until the code runs clean or the fix budget is
spent. These tests pin the loop with scripted providers (no network):

  * code-block extraction (fences, prose wrappers, multi-block replies);
  * fail-then-pass: a bug in the draft, the fix round repairs it, trace shows
    every step and the final artifact is the FIXED one;
  * exhausted budget: honest failure with the last error in the trace;
  * sandbox: each run gets a fresh temp cwd, output is captured, and a
    hanging program is killed at the timeout;
  * application wiring: build_code_plan uses the agent when a model is
    configured and carries the trace; scaffold fallback when it isn't.
"""

from __future__ import annotations

import unittest
from unittest import mock

from novacontrol.core.code_agent import (
    CodeAgentResult,
    extract_code_block,
    run_coding_agent,
)


class ScriptedProvider:
    """LLM double returning queued replies; records every prompt."""

    name = "scripted"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.prompts: list[str] = []

    async def complete(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.prompts.append(messages[-1]["content"])
        if not self.replies:
            raise AssertionError("ScriptedProvider ran out of replies")
        return self.replies.pop(0)


GOOD_PY = "print('answer=', 6 * 7)"
BUGGY_PY = "print('answer=', 6 *"  # syntax error on first run
FIXED_PY = "print('answer=', 6 * 7)"


class ExtractCodeBlockTests(unittest.TestCase):
    def test_fenced_block_with_language_tag(self) -> None:
        reply = "Here you go:\n```python\nprint(1)\n```\nEnjoy!"
        self.assertEqual(extract_code_block(reply), "print(1)")

    def test_longest_fence_wins(self) -> None:
        reply = "```js\nshort```\nand\n```js\nlonger\nblock\n```"
        self.assertEqual(extract_code_block(reply), "longer\nblock")

    def test_unfenced_code_passes_through(self) -> None:
        self.assertEqual(extract_code_block("def f():\n    return 1"), "def f():\n    return 1")

    def test_leading_prose_stripped_when_unfenced(self) -> None:
        self.assertEqual(extract_code_block("Here is your function\ndef f():\n    return 1"), "def f():\n    return 1")


class AgentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_draft_runs_clean_on_first_try(self) -> None:
        provider = ScriptedProvider(f"```python\n{GOOD_PY}\n```")

        async def runner(language, code, filename):
            return True, "answer= 42"

        result = await run_coding_agent(
            "print the answer", "python", provider, filename="answer.py", runner=runner,
        )
        self.assertTrue(result.ran_ok)
        self.assertEqual(result.generated_by, "agent")
        self.assertEqual(result.content, GOOD_PY)
        self.assertEqual(result.fix_rounds, 0)
        self.assertEqual(result.final_output, "answer= 42")
        kinds = [s.kind for s in result.steps]
        self.assertEqual(kinds, ["draft", "run"])

    async def test_fail_then_fix_then_pass_uses_real_error(self) -> None:
        provider = ScriptedProvider(
            f"```python\n{BUGGY_PY}\n```",  # draft: broken
            f"```python\n{FIXED_PY}\n```",  # fix round 1
        )

        async def runner(language, code, filename):
            if code == BUGGY_PY:
                return False, "SyntaxError: '(' was never closed"
            return True, "answer= 42"

        result = await run_coding_agent(
            "print the answer", "python", provider, filename="answer.py", runner=runner,
        )
        self.assertTrue(result.ran_ok)
        self.assertEqual(result.fix_rounds, 1)
        self.assertEqual(result.content, FIXED_PY)  # the FIXED code ships
        # The fix prompt carried the REAL error text, not a summary.
        fix_prompt = provider.prompts[1]
        self.assertIn("SyntaxError", fix_prompt)
        self.assertIn(BUGGY_PY, fix_prompt)
        kinds = [s.kind for s in result.steps]
        self.assertEqual(kinds, ["draft", "run", "fixed", "run"])

    async def test_budget_exhausted_reports_honest_failure(self) -> None:
        provider = ScriptedProvider(
            f"```python\n{BUGGY_PY}\n```",
            *([f"```python\nstill_broken_{i}\n```" for i in range(3)]),
        )

        async def runner(language, code, filename):
            return False, "SyntaxError: '(' was never closed"

        result = await run_coding_agent(
            "print the answer", "python", provider, filename="answer.py", runner=runner,
        )
        self.assertFalse(result.ran_ok)
        self.assertEqual(result.fix_rounds, 3)
        self.assertEqual([s.kind for s in result.steps][-1], "run")
        self.assertIn("SyntaxError", result.final_output)

    async def test_provider_failure_raises_for_caller_fallback(self) -> None:
        class Dead:
            name = "dead"

            async def complete(self, messages, **kwargs):
                raise ConnectionError("no model")

        with self.assertRaises(RuntimeError):
            await run_coding_agent("goal", "python", Dead(), filename="g.py")

    async def test_no_runtime_marks_draft_only_not_verified(self) -> None:
        provider = ScriptedProvider(f"```python\n{GOOD_PY}\n```")

        async def runner(language, code, filename):
            from novacontrol.core.code_agent import RuntimeUnavailable

            raise RuntimeUnavailable("no runtime")

        result = await run_coding_agent(
            "goal", "python", provider, filename="g.py", runner=runner,
        )
        self.assertFalse(result.ran_ok)
        self.assertEqual(result.generated_by, "llm")
        self.assertEqual([s.kind for s in result.steps], ["draft", "skipped"])


class SandboxRunTests(unittest.IsolatedAsyncioTestCase):
    """Real subprocess execution (the production path, not the test seam)."""

    async def test_python_code_actually_runs_and_captures_output(self) -> None:
        result = await run_coding_agent(
            "print the answer", "python", ScriptedProvider(f"```python\n{GOOD_PY}\n```"),
            filename="answer.py",
        )
        self.assertTrue(result.ran_ok)
        self.assertIn("answer= 42", result.final_output)

    async def test_error_output_is_captured_for_the_fix_prompt(self) -> None:
        result = await run_coding_agent(
            "broken on purpose", "python",
            ScriptedProvider(f"```python\n{BUGGY_PY}\n```", f"```python\n{FIXED_PY}\n```"),
            filename="broken.py",
        )
        # Real subprocess ran the buggy code; the fix recovered.
        self.assertTrue(result.ran_ok)
        failed = [s for s in result.steps if s.kind == "run" and "SyntaxError" in s.output]
        self.assertTrue(failed, "the real interpreter error should be in the trace")

    async def test_timeout_kills_hanging_program(self) -> None:
        hanging = "import time; time.sleep(60)"
        result = await run_coding_agent(
            "hang", "python", ScriptedProvider(f"```python\n{hanging}\n```"),
            filename="hang.py",
        )
        self.assertFalse(result.ran_ok)
        self.assertIn("Timed out", result.final_output)

    async def test_draft_only_language_never_executes(self) -> None:
        result = await run_coding_agent(
            "schema", "sql", ScriptedProvider("```sql\nSELECT 1;\n```"),
            filename="q.sql",
        )
        self.assertFalse(result.ran_ok)
        self.assertEqual([s.kind for s in result.steps], ["draft", "skipped"])
        self.assertEqual(result.content, "SELECT 1;")


class BuildCodePlanAgentWiringTests(unittest.IsolatedAsyncioTestCase):
    """build_code_plan uses the agent when a model is configured, and the
    response carries the trace for the UI."""

    def _app_with_brain(self, provider) -> mock.MagicMock:
        app = mock.MagicMock()
        app.brain.completion_provider = provider
        app.brain.model_configured = True
        app._record_activity = mock.MagicMock()
        app._cloud_llm_store = None
        return app

    async def test_configured_model_runs_agent_and_carries_trace(self) -> None:
        from novacontrol.application import NovaControlApplication

        provider = ScriptedProvider(f"```python\n{GOOD_PY}\n```")
        app = self._app_with_brain(provider)
        report = await NovaControlApplication.build_code_plan(app, "print the answer", language="python")

        self.assertEqual(report["artifact"]["generated_by"], "agent")
        self.assertTrue(report["agent"]["ran_ok"])
        self.assertTrue(report["agent"]["steps"])
        self.assertIn("verified", report["summary"])

    async def test_unconfigured_model_keeps_scaffold_fallback(self) -> None:
        from novacontrol.application import NovaControlApplication

        app = self._app_with_brain(None)
        app.brain.model_configured = False
        report = await NovaControlApplication.build_code_plan(app, "print the answer", language="python")

        self.assertEqual(report["artifact"]["generated_by"], "scaffold")
        self.assertEqual(report["agent"]["steps"], [])
        self.assertIn("connect an LLM", report["summary"])


class AgentResultShapeTests(unittest.TestCase):
    def test_to_dict_shape(self) -> None:
        result = CodeAgentResult(
            content="x=1", language="python", generated_by="agent", ran_ok=True,
            steps=[], final_output="ok", fix_rounds=1, model_name="m",
        )
        d = result.to_dict()
        self.assertEqual(
            sorted(d.keys()),
            ["content", "final_output", "fix_rounds", "generated_by", "language", "model_name", "ran_ok", "steps"],
        )


if __name__ == "__main__":
    unittest.main()
