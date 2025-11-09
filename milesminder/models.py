
from __future__ import annotations
from datetime import datetime
from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()

class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True, nullable=False)

    subcategories = relationship("Subcategory", back_populates="category", cascade="all, delete-orphan")
    cards = relationship("Card", back_populates="category")

class Subcategory(Base):
    __tablename__ = "subcategories"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id", ondelete="CASCADE"), nullable=False)

    category = relationship("Category", back_populates="subcategories")
    cards = relationship("Card", back_populates="subcategory")

    __table_args__ = (UniqueConstraint("category_id", "name", name="uq_subcategory_per_category"),)

class Card(Base):
    __tablename__ = "cards"
    id = Column(Integer, primary_key=True)
    card_number = Column(String, unique=True, nullable=False)
    question = Column(String, nullable=False)
    answer = Column(String, nullable=False)

    category_id = Column(Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    subcategory_id = Column(Integer, ForeignKey("subcategories.id", ondelete="SET NULL"), nullable=True)

    category = relationship("Category", back_populates="cards")
    subcategory = relationship("Subcategory", back_populates="cards")

class ReviewStat(Base):
    __tablename__ = "review_stats"
    id = Column(Integer, primary_key=True)
    user_id = Column(String, index=True, nullable=False)
    card_id = Column(Integer, ForeignKey("cards.id", ondelete="CASCADE"), nullable=False)
    rights = Column(Integer, default=0)
    wrongs = Column(Integer, default=0)
    last_reviewed_at = Column(DateTime, default=datetime.utcnow)

class SessionScore(Base):
    __tablename__ = "session_scores"
    id = Column(Integer, primary_key=True)
    user_id = Column(String, index=True, nullable=False)
    category_id = Column(Integer, nullable=True)
    subcategory_id = Column(Integer, nullable=True)
    points = Column(Integer, default=0)

class Streak(Base):
    __tablename__ = "streaks"
    id = Column(Integer, primary_key=True)
    user_id = Column(String, unique=True, nullable=False)
    current_streak = Column(Integer, default=0)
    longest_streak = Column(Integer, default=0)
    last_active_date = Column(String, nullable=True)  # ISO date string
