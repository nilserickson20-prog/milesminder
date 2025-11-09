from __future__ import annotations
import random
import string
import re
import uuid
from datetime import datetime
import pytz

from .models import Card, Category, Streak

# Eastern timezone for streak tracking
EASTERN = pytz.timezone("US/Eastern")


def slugify_prefix(name: str | None, length: int = 4) -> str:
    """
    Turn a category name into a short uppercase prefix like 'CRIM' or 'CIVI'.
    """
    if not name:
        return "GEN"
    letters = re.findall(r"[A-Za-z0-9]+", name.upper())
    if not letters:
        return "GEN"
    return "".join(letters)[0:length]


def generate_unique_card_number(db, category_name: str | None) -> str:
    """
    Generate a unique, human-friendly card number like: CRIM-YYYYMMDD-1234.
    Ensures uniqueness by checking the DB; falls back to a UUID suffix if needed.
    """
    prefix = slugify_prefix(category_name)
    ymd = datetime.utcnow().strftime("%Y%m%d")

    for _ in range(20):
        tail = "".join(random.choice(string.digits) for _ in range(4))
        candidate = f"{prefix}-{ymd}-{tail}"
        if not db.query(Card).filter(Card.card_number == candidate).first():
            return candidate

    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def get_or_create_category(db, name: str | None):
    """
    Returns an existing Category or creates a new one.
    """
    if not name:
        return None
    cat = db.query(Category).filter(Category.name.ilike(name.strip())).one_or_none()
    if not cat:
        cat = Category(name=name.strip())
        db.add(cat)
        db.commit()
        db.refresh(cat)
    return cat


def weighted_choice(cards, stats_by_id):
    """
    Picks a card giving higher weight to those marked wrong more often.
    """
    weights = []
    for c in cards:
        s = stats_by_id.get(c.id)
        wrongs = s.wrongs if s else 0
        rights = s.rights if s else 0
        weight = 1 + wrongs - rights * 0.5
        weight = max(weight, 0.2)
        weights.append(weight)
    return random.choices(cards, weights=weights, k=1)[0]


def mark_daily_activity(db, user_id: int):
    """
    Update or create a user's streak record for today's activity.
    """
    today = datetime.now(EASTERN).date()
    s = db.query(Streak).filter(Streak.user_id == str(user_id)).one_or_none()
    if not s:
        s = Streak(user_id=str(user_id), current_streak=1, longest_streak=1, last_active_date=today)
        db.add(s)
    else:
        if s.last_active_date == today:
            pass
        else:
            delta = (today - s.last_active_date).days
            if delta == 1:
                s.current_streak += 1
                s.longest_streak = max(s.longest_streak, s.current_streak)
            else:
                s.current_streak = 1
            s.last_active_date = today
    db.commit()
    db.refresh(s)
    return s
