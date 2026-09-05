"""Initial database schema

Revision ID: 001
Revises: None
Create Date: 2026-09-01
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Memory records
    op.create_table(
        "memory_records",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("namespace", sa.String(64), nullable=False, index=True),
        sa.Column("key", sa.String(256), nullable=False),
        sa.Column("value_json", sa.Text, nullable=False),
        sa.Column("text", sa.Text, nullable=False, server_default=""),
        sa.Column("metadata_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("importance", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.String(64), nullable=True),
        sa.UniqueConstraint("namespace", "key", name="uq_memory_namespace_key"),
    )

    # Projects
    op.create_table(
        "projects",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("data_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.String(64), nullable=False),
    )

    # Tasks
    op.create_table(
        "tasks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False, server_default="general"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("progress", sa.Float, nullable=False, server_default="0.0"),
        sa.Column("result_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("created_at", sa.String(64), nullable=False),
    )

    # Scheduled tasks
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("run_at", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(32), nullable=False, server_default="scheduled"),
    )

    # Knowledge articles
    op.create_table(
        "knowledge_articles",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("body", sa.Text, nullable=False),
        sa.Column("tags_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("source_url", sa.String(1024), nullable=True),
        sa.Column("created_at", sa.String(64), nullable=False),
    )

    # Automation workflows
    op.create_table(
        "automation_workflows",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("data_json", sa.Text, nullable=False, server_default="{}"),
    )

    # Settings
    op.create_table(
        "settings",
        sa.Column("id", sa.String(64), primary_key=True, server_default="default"),
        sa.Column("approval_mode", sa.String(32), nullable=False, server_default="ask"),
        sa.Column("detailed_explanations", sa.Boolean, nullable=False, server_default="1"),
        sa.Column("include_videos_in_explore", sa.Boolean, nullable=False, server_default="1"),
    )

    # Plugin install records
    op.create_table(
        "plugin_install_records",
        sa.Column("name", sa.String(256), primary_key=True),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="discovered"),
        sa.Column("trusted", sa.Boolean, nullable=False, server_default="0"),
        sa.Column("permissions_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("capabilities_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("installed_at", sa.String(64), nullable=False),
    )

    # Audit events
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("event_type", sa.String(64), nullable=False, index=True),
        sa.Column("action", sa.String(512), nullable=False),
        sa.Column("actor", sa.String(256), nullable=False, server_default="system"),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("details_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.String(64), nullable=False),
    )

    # Conversation messages
    op.create_table(
        "conversation_messages",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("session_id", sa.String(64), nullable=False, index=True),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("metadata_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.String(64), nullable=False),
    )

    # Research sources
    op.create_table(
        "research_sources",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("topic", sa.String(512), nullable=False, index=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("snippet", sa.Text, nullable=False, server_default=""),
        sa.Column("source_type", sa.String(32), nullable=False, server_default="web"),
        sa.Column("credibility", sa.Float, nullable=False, server_default="0.5"),
        sa.Column("retrieved_at", sa.String(64), nullable=False),
    )

    # Evolution experiments
    op.create_table(
        "evolution_experiments",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("goal", sa.Text, nullable=False),
        sa.Column("hypothesis", sa.Text, nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="proposed"),
        sa.Column("risk_level", sa.String(32), nullable=False, server_default="low"),
        sa.Column("result_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("lessons_learned", sa.Text, nullable=False, server_default=""),
        sa.Column("created_at", sa.String(64), nullable=False),
        sa.Column("completed_at", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_table("evolution_experiments")
    op.drop_table("research_sources")
    op.drop_table("conversation_messages")
    op.drop_table("audit_events")
    op.drop_table("plugin_install_records")
    op.drop_table("settings")
    op.drop_table("automation_workflows")
    op.drop_table("knowledge_articles")
    op.drop_table("scheduled_tasks")
    op.drop_table("tasks")
    op.drop_table("projects")
    op.drop_table("memory_records")
