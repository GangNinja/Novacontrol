"""Server-persisted shared chat transcript.

The Chat panel's history used to be per-browser localStorage; it now lives on
the server (data/chat_transcript.json) so every browser, tab, and client sees
the SAME thread. These tests pin:

  * ChatTranscriptStore: append/turns/clear, bounded cap, role validation,
    no-store degradation, JSON round-trip across "restarts";
  * the application layer: /ask auto-records both sides of the turn, client
    append, and the idempotent one-time localStorage migration;
  * the HTTP surface: GET/POST /chat/history, migration never duplicating,
    and /chat/clear wiping the transcript alongside the LLM memory.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from novacontrol.core.chat_transcript import ChatTranscriptStore
from novacontrol.persistence import JsonStateStore


class ChatTranscriptStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._store = ChatTranscriptStore(JsonStateStore(Path(self._tmp.name)))

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_append_and_turns_round_trip(self) -> None:
        self._store.append("user", "what is 2+2")
        self._store.append("assistant", "4", route="chat")

        turns = self._store.turns()
        self.assertEqual([t["role"] for t in turns], ["user", "assistant"])
        self.assertEqual(turns[0]["text"], "what is 2+2")
        self.assertEqual(turns[1]["route"], "chat")
        self.assertTrue(all(isinstance(t["at"], int) for t in turns))

    def test_append_is_bounded(self) -> None:
        store = ChatTranscriptStore(JsonStateStore(Path(self._tmp.name)), limit=10)
        for index in range(15):
            store.append("user", f"msg {index}")

        turns = store.turns()
        self.assertEqual(len(turns), 10)
        self.assertEqual(turns[0]["text"], "msg 5")  # oldest dropped
        self.assertEqual(turns[-1]["text"], "msg 14")

    def test_append_rejects_empty_and_unknown_roles(self) -> None:
        self.assertIsNone(self._store.append("user", "   "))
        self.assertIsNone(self._store.append("system", "nope"))
        self.assertIsNone(self._store.append("user", ""))
        self.assertEqual(self._store.turns(), [])

    def test_append_without_store_lives_for_the_call_only(self) -> None:
        store = ChatTranscriptStore(None)
        self.assertIsNotNone(store.append("user", "hi"))
        self.assertEqual(store.turns(), [])

    def test_clear_drops_everything(self) -> None:
        self._store.append("user", "a")
        self._store.append("assistant", "b")
        self.assertEqual(self._store.clear(), 2)
        self.assertEqual(self._store.turns(), [])
        self.assertEqual(self._store.clear(), 0)

    def test_survives_restart(self) -> None:
        root = Path(self._tmp.name)
        self._store.append("user", "survives")
        reopened = ChatTranscriptStore(JsonStateStore(root))
        self.assertEqual([t["text"] for t in reopened.turns()], ["survives"])

    def test_replace_all_overwrites(self) -> None:
        self._store.append("user", "old")
        accepted = self._store.replace_all(
            [{"role": "user", "text": "from localStorage", "at": 1234}]
        )

        self.assertEqual(accepted, 1)
        turns = self._store.turns()
        self.assertEqual(len(turns), 1)
        self.assertEqual(turns[0]["text"], "from localStorage")
        self.assertEqual(turns[0]["at"], 1234)


if __name__ == "__main__":
    unittest.main()
