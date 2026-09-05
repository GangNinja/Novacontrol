"""Tests for the SQLAlchemy database layer."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from novacontrol.database.engine import DatabaseEngine
from novacontrol.database.models import (
    AuditEventModel,
    Base,
    KnowledgeArticleModel,
    MemoryRecordModel,
    ProjectModel,
    SettingsModel,
    TaskModel,
)


def _make_engine(tmpdir: str) -> DatabaseEngine:
    engine = DatabaseEngine(f"sqlite:///{Path(tmpdir) / 'test.db'}")
    engine.create_tables()
    return engine


class DatabaseEngineTests(unittest.TestCase):

    def test_engine_creates_tables(self) -> None:
        with TemporaryDirectory() as tmpdir:
            engine = _make_engine(tmpdir)
            tables = list(Base.metadata.tables.keys())
            self.assertGreater(len(tables), 0)
            self.assertIn("memory_records", tables)
            self.assertIn("projects", tables)
            engine.engine.dispose()


class ModelCrudTests(unittest.TestCase):
    """Table-driven: one subTest per model type."""

    def _crud(self, name, record, lookup_kwargs, field, expected):
        with TemporaryDirectory() as tmpdir:
            engine = _make_engine(tmpdir)
            with engine.session() as session:
                session.add(record)
            with engine.session() as session:
                queried = session.query(type(record)).filter_by(**lookup_kwargs).one()
                self.assertEqual(getattr(queried, field), expected)
            engine.engine.dispose()

    def test_models(self) -> None:
        now = datetime.now(UTC).isoformat()
        cases = [
            ("memory_record",
             MemoryRecordModel(id="mem-1", namespace="conversation", key="k",
                 value_json=json.dumps({}), text="hello", metadata_json="{}",
                 importance=0.8, created_at=now, updated_at=now),
             {"id": "mem-1"}, "namespace", "conversation"),
            ("project",
             ProjectModel(id="proj-1", name="Test", description="d",
                 status="active", data_json="{}", created_at=now),
             {"id": "proj-1"}, "name", "Test"),
            ("task",
             TaskModel(id="task-1", title="Task", kind="ask",
                 status="pending", progress=0.0, result_json="{}", created_at=now),
             {"id": "task-1"}, "title", "Task"),
            ("knowledge_article",
             KnowledgeArticleModel(id="art-1", title="Article", body="body",
                 tags_json="[]", source_url="https://example.com", created_at=now),
             {"id": "art-1"}, "title", "Article"),
            ("audit_event",
             AuditEventModel(event_type="tool.exec", action="Run tool",
                 actor="user", status="done", details_json="{}", created_at=now),
             {"event_type": "tool.exec"}, "action", "Run tool"),
            ("settings",
             SettingsModel(id="default", approval_mode="ask",
                 detailed_explanations=True, include_videos_in_explore=True),
             {"id": "default"}, "approval_mode", "ask"),
        ]
        for name, record, lookup, field, expected in cases:
            with self.subTest(model=name):
                self._crud(name, record, lookup, field, expected)

    def test_rollback_on_error(self) -> None:
        with TemporaryDirectory() as tmpdir:
            engine = _make_engine(tmpdir)
            now = datetime.now(UTC).isoformat()
            try:
                with engine.session() as session:
                    session.add(MemoryRecordModel(
                        id="rollback-test", namespace="test", key="key",
                        value_json="{}", text="text", metadata_json="{}",
                        importance=0.0, created_at=now, updated_at=now,
                    ))
                    raise ValueError("Simulated error")
            except ValueError:
                pass
            with engine.session() as session:
                self.assertEqual(
                    session.query(MemoryRecordModel).filter_by(id="rollback-test").count(), 0
                )
            engine.engine.dispose()


if __name__ == "__main__":
    unittest.main()
