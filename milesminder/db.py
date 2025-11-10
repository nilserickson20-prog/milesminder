from __future__ import annotations
import os
import logging
from contextlib import contextmanager
from typing import Iterator
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from .models import Base

DB_PATH = os.environ.get("DB_PATH", "/data/milesminder.db")
SQLALCHEMY_DATABASE_URL = f"sqlite:///{DB_PATH}"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL,
    future=True,
    connect_args={"check_same_thread": False},
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    future=True,
    expire_on_commit=False  # prevent DetachedInstanceError
)

def _table_exists(conn, name: str) -> bool:
    return conn.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
        {"n": name}
    ).fetchone() is not None

def _column_exists(conn, table: str, column: str) -> bool:
    rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
    return any(r[1] == column for r in rows)

def _log_table_info(conn, table: str):
    try:
        rows = conn.execute(text(f"PRAGMA table_info({table})")).fetchall()
        cols = ", ".join([f"{r[1]}({r[2]})" for r in rows])
        logging.info(f"Table {table} columns: {cols or '<none>'}")
    except Exception as e:
        logging.warning(f"Could not read table info for {table}: {e}")

def _apply_migrations():
    logging.info(f"DB_PATH = {DB_PATH}")
    Base.metadata.create_all(bind=engine)

    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys=ON"))

        if not _table_exists(conn, "subcategories"):
            logging.info("Creating subcategories table...")
            conn.execute(text("""
                CREATE TABLE IF NOT EXISTS subcategories (
                    id INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    category_id INTEGER,
                    UNIQUE(name, category_id),
                    FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE
                )
            """))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_subcategories_category ON subcategories(category_id)"))

        if not _column_exists(conn, "cards", "subcategory_id"):
            logging.info("Adding column cards.subcategory_id")
            conn.execute(text("ALTER TABLE cards ADD COLUMN subcategory_id INTEGER"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cards_subcategory ON cards(subcategory_id)"))

        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_cards_category ON cards(category_id)"))
        _log_table_info(conn, "cards")

def init_db():
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

