
from __future__ import annotations
import random
from typing import Optional, Dict, List
from datetime import datetime
import pytz

from .models import Category, Subcategory, Card, ReviewStat, Streak

EASTERN = pytz.timezone("America/New_York")

def get_or_create_category(db, name: Optional[str]) -> Optional[Category]:
    if not name: return None
    name = name.strip()
    cat = db.query(Category).filter(Category.name.ilike(name)).one_or_none()
    if cat: return cat
    cat = Category(name=name); db.add(cat); db.flush(); return cat

def get_or_create_subcategory(db, category: Category, sub_name: Optional[str]) -> Optional[Subcategory]:
    if not category or not sub_name: return None
    sub_name = sub_name.strip()
    sub = db.query(Subcategory).filter(Subcategory.category_id==category.id, Subcategory.name.ilike(sub_name)).one_or_none()
    if sub: return sub
    sub = Subcategory(name=sub_name, category_id=category.id); db.add(sub); db.flush(); return sub

def generate_unique_card_number(db, scope_hint: Optional[str] = None) -> str:
    while True:
        num = f"{random.randint(100000, 999999)}"
        exists = db.query(Card).filter(Card.card_number == num).first()
        if not exists: return num

def weighted_choice(candidates: List[Card], stats_by_id: Dict[int, ReviewStat]) -> Card:
    weights = []
    for c in candidates:
        s = stats_by_id.get(c.id)
        if not s: w = 3.0
        else:
            wrongs = s.wrongs or 0; rights = s.rights or 0
            w = 1.0 + wrongs * 2.0 - rights * 0.2
            if w < 0.2: w = 0.2
        weights.append(w)
    total = sum(weights); r = random.random() * total; upto = 0.0
    for cand, w in zip(candidates, weights):
        if upto + w >= r: return cand
        upto += w
    return candidates[-1]

def mark_daily_activity(db, user_id: int):
    today = datetime.now(EASTERN).date()
    s = db.query(Streak).filter(Streak.user_id == str(user_id)).one_or_none()
    if not s:
        s = Streak(user_id=str(user_id), current_streak=1, longest_streak=1, last_active_date=today.isoformat())
        db.add(s); db.flush(); return s
    last_iso = s.last_active_date
    if not last_iso:
        s.current_streak = 1; s.longest_streak = max(s.longest_streak or 0, s.current_streak)
        s.last_active_date = today.isoformat(); db.flush(); return s
    try: from_iso = datetime.fromisoformat(last_iso).date()
    except Exception: from_iso = today
    delta = (today - from_iso).days
    if delta == 0: return s
    if delta == 1:
        s.current_streak = (s.current_streak or 0) + 1; s.longest_streak = max(s.longest_streak or 0, s.current_streak)
    else:
        s.current_streak = 1
    s.last_active_date = today.isoformat(); db.flush(); return s
