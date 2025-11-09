from __future__ import annotations
import os
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from .models import Base

DB_PATH = os.environ.get("DB_PATH", "/data/milesminder.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)  # 👈 make sure /data exists

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    future=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)

def init_db():
    Base.metadata.create_all(bind=engine)
