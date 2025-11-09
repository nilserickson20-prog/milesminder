from __future__ import annotations
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()


class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    cards = relationship("Card", back_populates="category", cascade="all, delete-orphan")


class Card(Base):
    __tablename__ = "cards"
    id = Column(Integer, primary_key=True)
    card_number = Column(String(64), unique=True, nullable=False)
    question = Column(String(4000), nullable=False)
    answer = Column(String(8000), nullable=False)

    category_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    category = relationship("Category", back_populates="cards")

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ReviewStat(Base):
    __tablename__ = "review_stats"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), nullable=False)
    card_id = Column(Integer, ForeignKey("cards.id"), nullable=False)
    rights = Column(Integer, default=0)
    wrongs = Column(Integer, default=0)
    last_reviewed_at = Column(DateTime)
    __table_args__ = (UniqueConstraint("user_id", "card_id", name="uq_user_card"),)
    card = relationship("Card")


class SessionScore(Base):
    __tablename__ = "session_scores"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    points = Column(Integer, default=0)
    started_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    category = relationship("Category")


class Streak(Base):
    __tablename__ = "streaks"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(64), unique=True, nullable=False)
    current_streak = Column(Integer, default=0)
    longest_streak = Column(Integer, default=0)
    last_active_date = Column(String(10), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
