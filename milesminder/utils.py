from __future__ import annotations
import random
import re
from datetime import datetime, date, timedelta
from typing import Optional, Dict

try:
    # Python 3.9+
    from zoneinfo import ZoneInfo
    EASTERN = ZoneInfo("America/New_York")
except Exception:
    # Fallback: if zoneinfo isn't available in the image
    EASTERN = None

from .models import Category, Card, ReviewStat, SessionScore, Streak


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------
def get_or_create_category(db, name: Optional[str]) -> Optional[Category]:
    """Return an existing Category by name (case-insensitive) or create one. None in -> None out."""
    if not name:
        return None
    nm = name.strip()
    cat = db.query(Category).filter(Category.name.ilike(nm)).one_or_none()
    if cat:
        return cat
    cat = Category(name=nm)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


# ---------------------------------------------------------------------------
# Card numbering
#   Generates a human-friendly unique number per category prefix:
#   <CAT>-<NNNN>, where CAT is up to 4 alphanum from the category (or 'GEN').
# ---------------------------------------------------------------------------
def generate_unique_card_number(db, category_name: Optional[str]) -> str:
    base = (category_name or "GEN").upper()
    base = re.sub(r"[^A-Z0-9]+", "", base)[:4] or "GEN"
    prefix = f"{base}-"

    existing = db.query(Card.card_number).filter(Card.card_number.ilike(f"{prefix}%")).all()
    max_n = 0
    for (cn,) in existing:
        m = re.match(rf"^{re.escape(prefix)}(\d+)$", cn or "")
        if m:
            try:
                max_n = max(max_n, int(m.group(1)))
            except ValueError:
                pass
    next_n = max_n + 1
    return f"{prefix}{next_n:04d}"


# ---------------------------------------------------------------------------
# Weighted selection for review
#   Heavier weight if a card has more wrongs, slightly lighter with many rights.
# ---------------------------------------------------------------------------
def weighted_choice(cards: list[Card], stats_by_id: Dict[int, ReviewStat]) -> Card:
    if not cards:
        raise ValueError("No cards to choose from")

    weights = []
    for c in cards:
        s = stats_by_id.get(c.id)
        rights = (s.rights or 0) if s else 0
        wrongs = (s.wrongs or 0) if s else 0

        # Base 1.0 + 2.0 per wrong - 0.3 per right; floor at 0.2 to keep it selectable
        w = max(0.2, 1.0 + 2.0 * wrongs - 0.3 * rights)
        weights.append(w)

    total = sum(weights)
    r = random.random() * total
    upto = 0.0
    for c, w in zip(cards, weights):
        if upto + w >= r:
            return c
        upto += w
    return cards[-1]


# ---------------------------------------------------------------------------
# Streak tracking
#   We store last_active_date as ISO string for compatibility with legacy rows.
#   On read, we parse strings to date objects.
# ---------------------------------------------------------------------------
def _today_et() -> date:
    if EASTERN:
        return datetime.now(EASTERN).date()
    # Fallback to UTC date if tz unavailable
    return datetime.utcnow().date()

def _as_date(d) -> Optional[date]:
    if d is None:
        return None
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        try:
            return date.fromisoformat(d)
        except Exception:
            return None
    return None

def mark_daily_activity(db, user_id: int | str) -> Streak:
    """Increment/maintain a user's daily streak; returns the Streak row."""
    uid = str(user_id)
    s = db.query(Streak).filter(Streak.user_id == uid).one_or_none()
    today = _today_et()

    if not s:
        s = Streak(
            user_id=uid,
            current_streak=1,
            longest_streak=1,
            last_active_date=today.isoformat(),  # store ISO string
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        return s

    last = _as_date(s.last_active_date)

    if last is None:
        # If legacy/bad data, normalise
        s.current_streak = max(1, s.current_streak or 0)
        s.longest_streak = max(s.current_streak, s.longest_streak or 0)
        s.last_active_date = today.isoformat()
        db.commit()
        db.refresh(s)
        return s

    delta_days = (today - last).days

    if delta_days <= 0:
        # Already counted today (or future/skew): ensure ISO stored
        s.last_active_date = today.isoformat()
    elif delta_days == 1:
        s.current_streak = (s.current_streak or 0) + 1
        if s.current_streak > (s.longest_streak or 0):
            s.longest_streak = s.current_streak
        s.last_active_date = today.isoformat()
    else:
        # Missed >= 1 day
        s.current_streak = 1
        if s.longest_streak is None:
            s.longest_streak = 1
        s.last_active_date = today.isoformat()

    db.commit()
    db.refresh(s)
    return s
