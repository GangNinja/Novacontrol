"""Phase 5: the execution pipeline — validation, normalization, caching.

The specification's order is ToolCall -> schema validation -> permission
validation -> execution, and its two additions are normalization (never hand a
model ten thousand raw lines) and caching (reuse what is safe to reuse). Each
class here pins one of those rules at the place it can actually be violated.
"""

from __future__ import annotations

import unittest

from conftest import AllowGateway
from novacontrol.core.security import PermissionScope
from novacontrol.tools import (
    FunctionTool,
    NormalizationLimits,
    OutputNormalizer,
    ToolCatalog,
    ToolExecutor,
    ToolMetadata,
    ToolParameter,
    ToolRegistry,
    ToolRequest,
    ToolResultCache,
    ToolSchema,
    ToolStatus,
)


def clock_at(times: list[float]) -> object:
    """A clock reading the times it is given, then the last one forever."""

    def read() -> float:
        return times.pop(0) if len(times) > 1 else times[0]

    return read


def tool(
    name: str = "clock",
    *,
    parameters: tuple[ToolParameter, ...] = (),
    permissions: tuple[PermissionScope, ...] = (),
    handler: object = None,
) -> FunctionTool:
    return FunctionTool(
        name,
        ToolSchema(name, f"The {name} tool", parameters=parameters),
        handler if callable(handler) else (lambda args: {"value": 1}),
        required_permissions=permissions,
    )


class SchemaValidationTests(unittest.TestCase):
    """A schema is the gate, so it has to say no to the right things."""

    def setUp(self) -> None:
        self.schema = ToolSchema(
            "run_command",
            "Run a command",
            parameters=(
                ToolParameter("command", "string", required=True),
                ToolParameter("timeout", "integer", required=False),
            ),
        )

    def test_a_valid_call_passes(self) -> None:
        self.assertEqual(self.schema.validate({"command": "pytest", "timeout": 30}), ())

    def test_an_undeclared_argument_is_refused(self) -> None:
        # The failure this prevents: a model invents an argument, the tool's
        # implementation half-honours it, and nobody is told.
        errors = self.schema.validate({"command": "pytest", "recursive": True})
        self.assertEqual(len(errors), 1)
        self.assertIn("Unexpected argument: recursive", errors[0])

    def test_a_required_argument_given_nothing_is_refused(self) -> None:
        self.assertIn("no value", self.schema.validate({"command": "   "})[0])
        self.assertIn("no value", self.schema.validate({"command": None})[0])

    def test_every_problem_is_reported_at_once(self) -> None:
        errors = self.schema.validate({"timeout": "soon", "extra": 1})
        self.assertEqual(len(errors), 3)

    def test_a_type_mismatch_names_both_types(self) -> None:
        errors = self.schema.validate({"command": "pytest", "timeout": "thirty"})
        self.assertIn("must be integer, got str", errors[0])

    def test_a_tool_that_takes_anything_can_say_so(self) -> None:
        free_form = ToolSchema("raw", "Anything", allow_extra_arguments=True)
        self.assertEqual(free_form.validate({"whatever": 1}), ())

    def test_a_schema_nobody_could_satisfy_fails_at_construction(self) -> None:
        with self.assertRaises(ValueError):
            ToolSchema("bad", "Bad", parameters=(ToolParameter("x", "telepathy"),))
        with self.assertRaises(ValueError):
            ToolSchema(
                "bad",
                "Bad",
                parameters=(ToolParameter("x", "string"), ToolParameter("x", "integer")),
            )


class NormalizationTests(unittest.TestCase):
    """What a result may cost the context it travels into."""

    def setUp(self) -> None:
        self.normalizer = OutputNormalizer()

    def test_a_small_result_is_untouched(self) -> None:
        # The layer bounds context; it does not rewrite every value passing by.
        self.assertEqual(self.normalizer.normalize("echo", {"echo": "hello"}), {"echo": "hello"})

    def test_terminal_output_becomes_an_exit_code_a_summary_and_the_lines_that_matter(self) -> None:
        noisy = "\n".join(f"collected test {index}" for index in range(4000))
        output = {
            "exit_code": 1,
            "stdout": noisy,
            "stderr": "FAILED tests/test_math.py::test_add\nE   AssertionError: expected 3, got 4",
        }

        normalized = self.normalizer.normalize("command_runner", output)

        self.assertEqual(normalized["exit_code"], 1)
        self.assertIn("FAILED tests/test_math.py::test_add", normalized["error_summary"])
        self.assertIn("AssertionError: expected 3, got 4", " ".join(normalized["relevant_lines"]))
        self.assertLessEqual(len(normalized["relevant_lines"]), 8)
        self.assertGreater(normalized["lines"], 1000)
        self.assertTrue(normalized["truncated"])

    def test_a_successful_command_says_so_rather_than_nothing(self) -> None:
        normalized = self.normalizer.normalize("command_runner", {"exit_code": 0, "stdout": "ok"})
        self.assertEqual(normalized["exit_code"], 0)
        self.assertIn("success", normalized["error_summary"])

    def test_a_failure_with_no_error_line_still_reports_its_last_words(self) -> None:
        normalized = self.normalizer.normalize(
            "command_runner",
            {"exit_code": 2, "stdout": "line one\nline two\nline three"},
        )
        self.assertIn("line three", normalized["error_summary"])

    def test_a_single_enormous_line_cannot_explode_the_result(self) -> None:
        # Capping the NUMBER of lines is not enough: one minified line can carry
        # the whole output, which is how a "bounded" summary stayed 18 kB.
        output = {"exit_code": 1, "stdout": "x" * 9000, "stderr": "E ValueError: bad input"}

        normalized = self.normalizer.normalize("command_runner", output)

        self.assertLess(len(str(normalized)), 4000)
        self.assertIn("characters elided", normalized["stdout_tail"][0])
        self.assertTrue(normalized["truncated"])

    def test_long_text_is_bounded_and_counted(self) -> None:
        normalized = self.normalizer.normalize("reader", {"text": "x" * 5000})
        self.assertIn("characters elided", normalized["text"])
        self.assertEqual(normalized["elided"]["text"], 5000 - 2000)

    def test_a_long_list_is_bounded_and_counted(self) -> None:
        normalized = self.normalizer.normalize(
            "lister", {"files": [f"f{i}.txt" for i in range(100)]}
        )
        self.assertEqual(len(normalized["files"]), 19)  # 12 head + marker + 6 tail
        self.assertEqual(normalized["elided"]["files"], 82)

    def test_deep_structures_are_flattened_rather_than_exploded(self) -> None:
        deep = {"a": {"b": {"c": {"d": {"e": "deep value"}}}}}
        normalized = self.normalizer.normalize("nest", deep)
        self.assertIn("deep value", str(normalized))

    def test_structure_is_kept_beside_the_summary(self) -> None:
        normalized = self.normalizer.normalize(
            "command_runner",
            {"exit_code": 0, "stdout": "done", "matched": ["a.py", "b.py"]},
        )
        self.assertEqual(normalized["data"]["matched"], ["a.py", "b.py"])

    def test_an_exit_code_is_never_truncated_away(self) -> None:
        normalized = self.normalizer.normalize(
            "command_runner", {"exit_code": 7, "stdout": "noisy"}
        )
        self.assertEqual(normalized["exit_code"], 7)

    def test_the_limits_are_configurable(self) -> None:
        tiny = OutputNormalizer(NormalizationLimits(max_chars=10))
        self.assertIn("characters elided", tiny.normalize("reader", {"text": "y" * 100})["text"])
        with self.assertRaises(ValueError):
            NormalizationLimits(max_items=0)


class CacheTests(unittest.TestCase):
    """Reuse only what a tool has said is reusable."""

    def setUp(self) -> None:
        self.metadata = ToolMetadata(
            "system_monitor",
            "Read telemetry",
            read_only=True,
            cache_ttl_s=300.0,
            volatile_values={"metric": ("cpu",)},
        )
        self.cache = ToolResultCache(clock=clock_at([100.0]))

    def test_a_cacheable_call_is_stored_and_returned(self) -> None:
        self.assertTrue(
            self.cache.set(
                "system_monitor", {"metric": "storage"}, {"free_gb": 42}, metadata=self.metadata
            )
        )
        self.assertEqual(
            self.cache.get("system_monitor", {"metric": "storage"}, metadata=self.metadata),
            {"free_gb": 42},
        )
        self.assertEqual(self.cache.hits, 1)

    def test_a_volatile_operation_is_refused_and_says_why(self) -> None:
        stored = self.cache.set(
            "system_monitor", {"metric": "cpu"}, {"load": 12}, metadata=self.metadata
        )
        self.assertFalse(stored)
        self.assertIsNone(
            self.cache.get("system_monitor", {"metric": "cpu"}, metadata=self.metadata)
        )
        self.assertTrue(any("volatile" in reason for reason in self.cache.refusals))

    def test_a_write_is_refused(self) -> None:
        writer = ToolMetadata("write_file", "Write", read_only=False, cache_ttl_s=600)
        self.assertFalse(self.cache.set("write_file", {}, {"ok": True}, metadata=writer))

    def test_an_empty_result_is_not_remembered(self) -> None:
        self.assertFalse(
            self.cache.set("system_monitor", {"metric": "storage"}, {}, metadata=self.metadata)
        )

    def test_an_expired_entry_is_deleted_not_served(self) -> None:
        cache = ToolResultCache(clock=clock_at([0.0, 400.0]))
        cache.set("system_monitor", {"metric": "storage"}, {"free_gb": 42}, metadata=self.metadata)

        self.assertIsNone(
            cache.get("system_monitor", {"metric": "storage"}, metadata=self.metadata)
        )
        self.assertEqual(cache.size, 0)
        self.assertEqual(cache.misses, 1)

    def test_argument_order_does_not_create_a_second_entry(self) -> None:
        cache = ToolResultCache(clock=clock_at([0.0]))
        cache.set("system_monitor", {"a": 1, "b": 2}, {"value": 1}, metadata=self.metadata)
        self.assertEqual(
            cache.get("system_monitor", {"b": 2, "a": 1}, metadata=self.metadata), {"value": 1}
        )
        self.assertEqual(cache.size, 1)

    def test_metadata_is_required_and_absence_refuses_rather_than_guesses(self) -> None:
        self.assertFalse(self.cache.set("unknown", {}, {"value": 1}))
        self.assertIn(
            "no metadata",
            " ".join(reason for reason in self.cache.refusals if "no metadata" in reason),
        )

    def test_the_cache_is_bounded_and_evicts_oldest_first(self) -> None:
        cache = ToolResultCache(max_entries=2, clock=clock_at([0.0]))
        for index in range(3):
            cache.set(
                "system_monitor", {"metric": f"m{index}"}, {"value": index}, metadata=self.metadata
            )
        self.assertEqual(cache.size, 2)
        self.assertEqual(cache.evictions, 1)
        self.assertIsNone(cache.get("system_monitor", {"metric": "m0"}, metadata=self.metadata))

    def test_invalidate_and_prune_remove_what_they_should(self) -> None:
        cache = ToolResultCache(max_entries=4, clock=clock_at([0.0, 1000.0]))
        cache.set("system_monitor", {"metric": "storage"}, {"value": 1}, metadata=self.metadata)
        self.assertEqual(cache.prune(), 1)
        cache.set("system_monitor", {"metric": "storage"}, {"value": 1}, metadata=self.metadata)
        self.assertEqual(cache.invalidate("system_monitor"), 1)
        self.assertEqual(cache.invalidate(), 0)

    def test_the_report_counts_what_it_declined(self) -> None:
        self.cache.set("system_monitor", {"metric": "cpu"}, {"load": 1}, metadata=self.metadata)
        report = self.cache.to_dict()
        self.assertEqual(report["stores"], 0)
        self.assertTrue(report["refusals"])
        with self.assertRaises(ValueError):
            ToolResultCache(max_entries=0)


class ExecutorPipelineTests(unittest.IsolatedAsyncioTestCase):
    """The four stages in the order the specification gives them."""

    def build(
        self,
        *,
        cache: ToolResultCache | None = None,
        metadata: ToolMetadata | None = None,
        normalizer: OutputNormalizer | None = None,
        permissions: tuple[PermissionScope, ...] = (),
        handler: object = None,
    ) -> tuple[ToolExecutor, dict[str, int]]:
        calls = {"n": 0}

        def counting(args: dict[str, object]) -> dict[str, object]:
            calls["n"] += 1
            return {"value": calls["n"], "exit_code": 0, "stdout": "done"}

        registry = ToolRegistry()
        registry.register(
            tool(
                "system_monitor",
                parameters=(ToolParameter("metric", "string", required=True),),
                permissions=permissions,
                handler=handler or counting,
            )
        )
        catalog = ToolCatalog([metadata]) if metadata is not None else None
        executor = ToolExecutor(
            registry,
            approval_gateway=AllowGateway(),
            catalog=catalog,
            cache=cache,
            normalizer=normalizer,
        )
        return executor, calls

    async def test_an_undeclared_argument_never_reaches_the_tool(self) -> None:
        executor, calls = self.build()
        result = await executor.execute(
            ToolRequest("system_monitor", {"metric": "cpu", "recurse": True})
        )
        self.assertEqual(result.status, ToolStatus.FAILED)
        self.assertIn("Unexpected argument", result.error or "")
        self.assertEqual(calls["n"], 0)

    async def test_a_cacheable_result_is_reused_and_reported_as_cached(self) -> None:
        cache = ToolResultCache(clock=clock_at([0.0]))
        metadata = ToolMetadata("system_monitor", "Read", read_only=True, cache_ttl_s=60.0)
        executor, calls = self.build(cache=cache, metadata=metadata)

        first = await executor.execute(ToolRequest("system_monitor", {"metric": "storage"}))
        second = await executor.execute(ToolRequest("system_monitor", {"metric": "storage"}))

        self.assertEqual(calls["n"], 1)
        self.assertFalse(first.cached)
        self.assertTrue(second.cached)
        self.assertEqual(first.output, second.output)

    async def test_a_volatile_reading_is_taken_every_time(self) -> None:
        cache = ToolResultCache(clock=clock_at([0.0]))
        metadata = ToolMetadata(
            "system_monitor",
            "Read",
            read_only=True,
            cache_ttl_s=60.0,
            volatile_values={"metric": ("cpu",)},
        )
        executor, calls = self.build(cache=cache, metadata=metadata)

        await executor.execute(ToolRequest("system_monitor", {"metric": "cpu"}))
        await executor.execute(ToolRequest("system_monitor", {"metric": "cpu"}))

        self.assertEqual(calls["n"], 2)
        self.assertEqual(cache.stores, 0)

    async def test_a_denied_call_is_never_cached(self) -> None:
        cache = ToolResultCache(clock=clock_at([0.0]))
        metadata = ToolMetadata("system_monitor", "Read", read_only=True, cache_ttl_s=60.0)
        registry = ToolRegistry()
        registry.register(
            tool("system_monitor", permissions=(PermissionScope.DESKTOP_CONTROL,))
        )
        executor = ToolExecutor(
            registry, catalog=ToolCatalog([metadata]), cache=cache
        )

        result = await executor.execute(ToolRequest("system_monitor", {}))

        self.assertEqual(result.status, ToolStatus.DENIED)
        self.assertEqual(cache.size, 0)

    async def test_a_failure_is_never_cached(self) -> None:
        def explode(args: dict[str, object]) -> dict[str, object]:
            raise RuntimeError("the sensor is missing")

        cache = ToolResultCache(clock=clock_at([0.0]))
        metadata = ToolMetadata("system_monitor", "Read", read_only=True, cache_ttl_s=60.0)
        executor, _ = self.build(cache=cache, metadata=metadata, handler=explode)

        result = await executor.execute(ToolRequest("system_monitor", {"metric": "storage"}))

        self.assertEqual(result.status, ToolStatus.FAILED)
        self.assertEqual(cache.size, 0)

    async def test_without_a_catalogue_nothing_is_cached_even_when_the_work_repeats(self) -> None:
        cache = ToolResultCache(clock=clock_at([0.0]))
        executor, calls = self.build(cache=cache)

        await executor.execute(ToolRequest("system_monitor", {"metric": "storage"}))
        await executor.execute(ToolRequest("system_monitor", {"metric": "storage"}))

        self.assertEqual(calls["n"], 2)

    async def test_output_is_normalized_on_the_way_out(self) -> None:
        def noisy(args: dict[str, object]) -> dict[str, object]:
            return {"exit_code": 1, "stdout": "x" * 9000, "stderr": "E ValueError: bad input"}

        executor, _ = self.build(handler=noisy)
        result = await executor.execute(ToolRequest("system_monitor", {"metric": "storage"}))

        self.assertEqual(result.status, ToolStatus.COMPLETED)
        self.assertEqual(result.output["exit_code"], 1)
        self.assertIn("ValueError", result.output["error_summary"])
        self.assertLess(len(str(result.output)), 9000)

    async def test_the_executor_reports_what_its_cache_did(self) -> None:
        cache = ToolResultCache(clock=clock_at([0.0]))
        executor, _ = self.build(cache=cache)
        self.assertIn("entries", executor.cache_report())
        self.assertEqual(ToolExecutor(ToolRegistry()).cache_report(), {})


if __name__ == "__main__":
    unittest.main()
