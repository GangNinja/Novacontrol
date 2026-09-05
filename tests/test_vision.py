from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from novacontrol.core.events import Event, EventBus
from novacontrol.vision import BasicScreenUnderstandingProcessor, VisionModule, VisionTaskType


class VisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_basic_ocr_reads_text_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "screen.txt"
            path.write_text("Window title\nImportant text", encoding="utf-8")
            processor = BasicScreenUnderstandingProcessor()

            result = await processor.ocr(str(path))

            self.assertIn("Important text", result.text)

    async def test_screen_understanding_includes_window(self) -> None:
        processor = BasicScreenUnderstandingProcessor()

        result = await processor.understand_screen("nova_control_dashboard.png")

        self.assertEqual(result.windows[0].title, "Nova Control Dashboard")

    async def test_document_understanding_extracts_sections(self) -> None:
        with TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "guide.md"
            path.write_text("# Title\nBody\n## Details", encoding="utf-8")
            processor = BasicScreenUnderstandingProcessor()

            result = await processor.understand_document(str(path))

            self.assertEqual(result.sections, ("# Title", "## Details"))

    async def test_vision_module_emits_completed_event(self) -> None:
        from conftest import collect_events
        bus = EventBus()
        module = VisionModule()
        seen = await collect_events(
            bus, "vision.task_completed",
            "vision.task_requested",
            {"task_type": VisionTaskType.IMAGE_UNDERSTANDING, "source": "sample_image.png"},
            start_fn=module.start,
        )
        self.assertEqual(seen[0].payload["task_type"], VisionTaskType.IMAGE_UNDERSTANDING.value)
        self.assertIn("sample", seen[0].payload["result"]["labels"])


if __name__ == "__main__":
    unittest.main()
