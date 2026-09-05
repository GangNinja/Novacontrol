"""SQLAlchemy ORM models for NovaControl."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all NovaControl ORM models."""
    pass


class MemoryRecordModel(Base):
    """Memory record stored in the database."""
    __tablename__ = "memory_records"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    namespace: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    key: Mapped[str] = mapped_column(String(256), nullable=False)
    value_json: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    importance: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        {"sqlite_autoincrement": True},
    )


class ProjectModel(Base):
    """Project stored in the database."""
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    data_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class TaskModel(Base):
    """Task tracked in the database."""
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="general")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    progress: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    result_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class ScheduledTaskModel(Base):
    """Scheduled task stored in the database."""
    __tablename__ = "scheduled_tasks"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    run_at: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="scheduled")


class KnowledgeArticleModel(Base):
    """Knowledge article stored in the database."""
    __tablename__ = "knowledge_articles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    tags_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class AutomationWorkflowModel(Base):
    """Automation workflow stored in the database."""
    __tablename__ = "automation_workflows"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    data_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class SettingsModel(Base):
    """User settings stored in the database."""
    __tablename__ = "settings"

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default="default")
    approval_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="ask")
    detailed_explanations: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    include_videos_in_explore: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PluginInstallRecordModel(Base):
    """Plugin install record stored in the database."""
    __tablename__ = "plugin_install_records"

    name: Mapped[str] = mapped_column(String(256), primary_key=True)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="discovered")
    trusted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    permissions_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    capabilities_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    installed_at: Mapped[str] = mapped_column(String(64), nullable=False)


class AuditEventModel(Base):
    """Audit event stored in the database."""
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(512), nullable=False)
    actor: Mapped[str] = mapped_column(String(256), nullable=False, default="system")
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class ConversationMessageModel(Base):
    """Conversation message stored in the database."""
    __tablename__ = "conversation_messages"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)


class ResearchSourceModel(Base):
    """Research source stored in the database."""
    __tablename__ = "research_sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    snippet: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="web")
    credibility: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    retrieved_at: Mapped[str] = mapped_column(String(64), nullable=False)


class EvolutionExperimentModel(Base):
    """Self-evolution experiment stored in the database."""
    __tablename__ = "evolution_experiments"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    goal: Mapped[str] = mapped_column(Text, nullable=False)
    hypothesis: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="proposed")
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False, default="low")
    result_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    lessons_learned: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[str] = mapped_column(String(64), nullable=False)
    completed_at: Mapped[str | None] = mapped_column(String(64), nullable=True)
