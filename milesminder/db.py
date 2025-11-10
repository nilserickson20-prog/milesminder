# milesminder/db.py

from __future__ import annotations

import os
import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# If your Base is declared in models.py:
from .models import Base

DB_PATH = os.environ.get("DB_PATH", "/data/milesminder.db")
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    future=True,
    connect_args={"check_same_thread": False},  # needed for SQLite + threads
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _table_exists(conn, name: str) -> bool:
    res = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": name},
    ).fetchone()
    return res is not None


def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)  # r[1] is the column name


def _apply_migrations():
    """Idempotent, safe migrations for subcategories support."""
    with engine.begin() as conn:
        # always good practice with SQLite
        conn.execute(text("PRAGMA foreign_keys=ON"))

        # Ensure base tables exist first (creates old schema as needed)
        Base.metadata.create_all(bind=conn.connection)

        # 1) Create subcategories table if missing
        if not _table_exists(conn, "subcategories"):
            logging.info("DB migrate: creating table subcategories")
            conn.execute(
                text(
                    """
                    CREATE TABLE IF NOT EXISTS subcategories (
                        id INTEGER PRIMARY KEY,
                        name TEXT NOT NULL,
                        category_id INTEGER,
                        UNIQUE(name, category_id),
                        FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE
                    )
                    """
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_subcategories_category ON subcategories(category_id)"))

        # 2) Add subcategory_id to cards if missing
        if not _column_exists(conn, "cards", "subcategory_id"):
            logging.info("DB migrate: adding column cards.subcategory_id")
            conn.execute(text("ALTER TABLE cards ADD COLUMN subcategory_id INTEGER"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cards_subcategory ON cards(subcategory_id)"))

        # 3) Make sure there’s an index on cards.category_id (older DBs might not have it)
        #    (CREATE INDEX IF NOT EXISTS is safe to run repeatedly)
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cards_category ON cards(category_id)"))

        # 4) Optional: ensure foreign_keys pragma is respected on future connections
        conn.execute(text("PRAGMA foreign_keys=ON"))

    logging.info("DB migrations applied (idempotent)")


def init_db():
    """Initialize DB and run lightweight migrations."""
    # ensure directory exists
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _apply_migrations()


@contextmanager
def get_session() -> Iterator[SessionLocal]:
    """Context-managed session (commit/rollback handled automatically)."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

