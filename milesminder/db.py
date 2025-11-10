# milesminder/db.py

from __future__ import annotations

import os
import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from .models import Base

# ---- Configuration ----
DB_PATH = os.environ.get("DB_PATH", "/data/milesminder.db")
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

# Engine/Session
engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    future=True,
    connect_args={"check_same_thread": False},  # SQLite threading
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)  # r[1] is column name


def _table_exists(conn, name: str) -> bool:
    row = conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": name},
    ).fetchone()
    return row is not None


def _log_table_info(conn, table: str, prefix: str = ""):
    try:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        cols = ", ".join([f"{r[1]}({r[2]})" for r in rows])
        logging.info("%sTable %s columns: %s", prefix, table, cols or "<none>")
    except Exception as e:
        logging.info("Could not read PRAGMA table_info(%s): %s", table, e)


def _apply_migrations():
    logging.info("DB_PATH = %s", DB_PATH)

    # First: ensure base tables exist (as defined in models.py)
    # Use the Engine so SQLAlchemy manages a proper Connection
    Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        # Make sure foreign keys are enabled
        conn.execute(text("PRAGMA foreign_keys=ON"))

        # Log current schema
        _log_table_info(conn, "categories", prefix="[pre] ")
        _log_table_info(conn, "subcategories", prefix="[pre] ")
        _log_table_info(conn, "cards", prefix="[pre] ")

        # Create subcategories if missing
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

        # Add cards.subcategory_id if missing
        if not _column_exists(conn, "cards", "subcategory_id"):
            logging.info("DB migrate: adding column cards.subcategory_id")
            conn.execute(text("ALTER TABLE cards ADD COLUMN subcategory_id INTEGER"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cards_subcategory ON cards(subcategory_id)"))

        # Ensure index on cards.category_id
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cards_category ON cards(category_id)"))

        # Final schema log
        _log_table_info(conn, "subcategories", prefix="[post] ")
        _log_table_info(conn, "cards", prefix="[post] ")


def init_db():
    # Ensure the folder exists for the SQLite file
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    _apply_migrations()


@contextmanager
def get_session() -> Iterator[SessionLocal]:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


