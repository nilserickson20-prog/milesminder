from __future__ import annotations
import random
from datetime import datetime, date
from typing import List, Optional

from sqlalchemy.orm import Session
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Card, Category, ReviewStat, Streak


# --- Time zone setup with fallback ---
try:
    EASTERN = ZoneInfo("America/New_York")
except ZoneInfoNotFoundError:
    # Fallback to UTC if tzdata isn't available in the container
    EASTERN = ZoneInfo("UTC")


# --- Category helper ---
def get_or_create_category(db: Session, name: Optional[str]) -> Optional[Category]:
    """Return an existing Category by name or create a new one."""
    if not name:
        return None
    cat = db.query(Category).filter(Category.name.ilike(name.strip())).one_or_none()
    if cat:
        return cat
    cat = Category(name=name.strip())
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


# --- Weighted flashcard selection ---
def weight_for_stat(stat: Optional[ReviewStat]) -> float:
    """Heavier weight for cards the user gets wrong more often."""
    if stat is None:
        return 1.5  # unseen cards get mild priority
    w = 1.0 + (stat.wrongs * 2.0) - (stat.rights * 0.5)
    return max(1.0, min(6.0, w))


def weighted_choice(cards: List[Card], stats_by_id: dict[int, ReviewStat]) -> Optional[Card]:
    """Pick a card with weighted randomness based on review stats."""
    if not cards:
        return None
    weights = [weight_for_stat(stats_by_id.get(c.id)) for c in cards]
    total = sum(weights)
    r = random.uniform(0, total)
    upto = 0
    for card, w in zip(cards, weights):
        if upto + w >= r:
            return card
        upto += w
    return cards[-1]  # fallback


# --- Streak tracking ---
def mark_daily_activity(db: Session, user_id: int) -> Streak:
    """
    Update or start a user's daily streak (based on Eastern time).
    Increments streak if the user reviewed today; resets if skipped a day.
    """
    today_et = datetime.now(EASTERN).date().isoformat()
    s = db.query(Streak).filter(Streak.user_id == str(user_id)).one_or_none()
    if not s:
        s = Streak(user_id=str(user_id), current_streak=1, longest_streak=1, last_active_date=today_et)
        db.add(s)
        db.commit()
        return s

    # If already active today, do nothing
    if s.last_active_date == today_et:
        return s

    yesterday = (datetime.now(EASTERN).date().fromordinal(date.today().toordinal() - 1)).isoformat()

    # Continue streak if active yesterday
    if s.last_active_date == yesterday:
        s.current_streak += 1
    else:
        s.current_streak = 1

    s.longest_streak = max(s.longest_streak, s.current_streak)
    s.last_active_date = today_et
    db.commit()
    return s
