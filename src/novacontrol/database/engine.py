"""Database engine and session factory for NovaControl."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from novacontrol.database.models import Base


class DatabaseEngine:
    """Manages the SQLAlchemy engine and session factory."""

    def __init__(self, url: str | None = None) -> None:
        self.url = url or self._default_url()
        self._engine = create_engine(self.url, echo=False, future=True)
        self._session_factory: sessionmaker[Session] = sessionmaker(bind=self._engine, expire_on_commit=False)

        # Enable WAL mode for SQLite
        if self.url.startswith("sqlite"):
            @event.listens_for(self._engine, "connect")
            def _set_sqlite_pragma(dbapi_connection: Any, connection_record: Any) -> None:  # noqa: ARG001
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.close()

    def create_tables(self) -> None:
        """Create all tables defined in the models."""
        Base.metadata.create_all(self._engine)

    def drop_tables(self) -> None:
        """Drop all tables."""
        Base.metadata.drop_all(self._engine)

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        """Provide a transactional session scope."""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @property
    def engine(self) -> Any:
        """Access the underlying SQLAlchemy engine."""
        return self._engine

    @staticmethod
    def _default_url() -> str:
        """Determine the default database URL from environment or filesystem."""
        env_url = os.getenv("NOVACONTROL_DATABASE_URL")
        if env_url:
            return env_url
        data_dir = Path("data")
        data_dir.mkdir(exist_ok=True)
        return f"sqlite:///{data_dir / 'novacontrol.sqlite3'}"


def get_session_factory(url: str | None = None) -> sessionmaker[Session]:
    """Get a session factory for the given database URL."""
    engine = DatabaseEngine(url)
    engine.create_tables()
    return engine._session_factory
