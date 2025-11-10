from __future__ import annotations

"""
MilesMinder Discord Bot
- Categories & Subcategories
- Add / List Cards
- Review modes: 20 / 50 / All
- Spoilered answers
- No repeats within a session
- Optional category/subcategory filters with autocomplete
- Reward video + daily streak upon successful review session
- SQLite + SQLAlchemy with safe boot and idempotent indices + runtime migration
- Guild-scoped fast sync if DISCORD_GUILD_ID is set
"""

import os
import sys
import random
import logging
import datetime as dt
from typing import Optional, List, Dict, Set

import discord
from discord.ext import commands
from discord import app_commands

from sqlalchemy import (
    create_engine, Column, Integer, String, ForeignKey, Text, UniqueConstraint, Date
)
from sqlalchemy.orm import sessionmaker, relationship, declarative_base, Session, joinedload  # ← joinedload added

# ------------------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------------------
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
log = logging.getLogger("milesminder")

# ------------------------------------------------------------------------------
# Environment
# ------------------------------------------------------------------------------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")

REWARD_VIDEOS_DIR = os.environ.get("REWARD_VIDEOS_DIR", "/data/rewards")

if not DISCORD_TOKEN:
    log.error("Missing DISCORD_TOKEN")
    sys.exit(1)

GUILD_FOR_SYNC: Optional[discord.Object] = None
if DISCORD_GUILD_ID and str(DISCORD_GUILD_ID).isdigit():
    GUILD_FOR_SYNC = discord.Object(id=int(DISCORD_GUILD_ID))

# ------------------------------------------------------------------------------
# Database Setup
# ------------------------------------------------------------------------------
DB_PATH = os.environ.get("MM_DB_PATH", "/data/milesminder.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

engine = create_engine(f"sqlite:///{DB_PATH}", echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
Base = declarative_base()


class Category(Base):
    __tablename__ = "categories"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False, unique=True)

    subcategories = relationship("Subcategory", back_populates="category", cascade="all, delete-orphan")
    cards = relationship("Card", back_populates="category")


class Subcategory(Base):
    __tablename__ = "subcategories"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=False)

    category = relationship("Category", back_populates="subcategories")
    cards = relationship("Card", back_populates="subcategory")

    __table_args__ = (UniqueConstraint("category_id", "name", name="uq_subcategory_per_category"),)


class Card(Base):
    __tablename__ = "cards"
    id = Column(Integer, primary_key=True)
    card_number = Column(String(64), nullable=False, unique=True)
    question = Column(Text, nullable=False)
    answer = Column(Text, nullable=False)
    category_id = Column(Integer, ForeignKey("categories.id"), nullable=True)
    subcategory_id = Column(Integer, ForeignKey("subcategories.id"), nullable=True)

    category = relationship("Category", back_populates="cards")
    subcategory = relationship("Subcategory", back_populates="cards")


class Streak(Base):
    __tablename__ = "streaks"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(32), nullable=False, unique=True)
    count = Column(Integer, nullable=False, default=0)
    last_reward_date = Column(Date, nullable=True)


def init_db():
    Base.metadata.create_all(engine)
    # idempotent indices
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_cards_category_id ON cards(category_id)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_cards_subcategory_id ON cards(subcategory_id)")

    # runtime migrations (safe on every start)
    with engine.begin() as conn:
        # Ensure streaks table exists and columns present
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS streaks ("
            "id INTEGER PRIMARY KEY, "
            "user_id VARCHAR(32) NOT NULL UNIQUE)"
        )
        # Now make sure required columns exist
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info('streaks')").fetchall()}
        if "count" not in cols:
            conn.exec_driver_sql("ALTER TABLE streaks ADD COLUMN count INTEGER NOT NULL DEFAULT 0")
        if "last_reward_date" not in cols:
            conn.exec_driver_sql("ALTER TABLE streaks ADD COLUMN last_reward_date DATE")


init_db()

# ------------------------------------------------------------------------------
# DB Helpers
# ------------------------------------------------------------------------------
def db() -> Session:
    return SessionLocal()


def ensure_category(sess: Session, name: str) -> Category:
    c = sess.query(Category).filter(Category.name.ilike(name)).one_or_none()
    if c:
        return c
    c = Category(name=name)
    sess.add(c)
    sess.commit()
    sess.refresh(c)
    return c


def ensure_subcategory(sess: Session, category: Category, sub_name: str) -> Subcategory:
    s = (
        sess.query(Subcategory)
        .filter(Subcategory.category_id == category.id, Subcategory.name.ilike(sub_name))
        .one_or_none()
    )
    if s:
        return s
    s = Subcategory(name=sub_name, category_id=category.id)
    sess.add(s)
    sess.commit()
    sess.refresh(s)
    return s


def next_card_number(sess: Session) -> str:
    base = sess.query(Card).count() + 1
    while True:
        candidate = f"C{base:06d}"
        if not sess.query(Card).filter_by(card_number=candidate).first():
            return candidate
        base += 1


def get_category_id_by_name(sess: Session, name: Optional[str]) -> Optional[int]:
    if not name:
        return None
    c = sess.query(Category).filter(Category.name.ilike(name)).one_or_none()
    return c.id if c else None


def get_subcategory_id_by_name(sess: Session, category_id: Optional[int], sub_name: Optional[str]) -> Optional[int]:
    if not sub_name or not category_id:
        return None
    s = (
        sess.query(Subcategory)
        .filter(Subcategory.category_id == category_id, Subcategory.name.ilike(sub_name))
        .one_or_none()
    )
    return s.id if s else None


def candidate_card_ids(sess: Session, cat_id: Optional[int], sub_id: Optional[int]) -> List[int]:
    q = sess.query(Card.id)
    if cat_id:
        q = q.filter(Card.category_id == cat_id)
    if sub_id:
        q = q.filter(Card.subcategory_id == sub_id)
    return [r[0] for r in q.all()]


def fetch_card_dict(sess: Session, card_id: int) -> Dict:
    c: Card = sess.query(Card).filter(Card.id == card_id).one()
    return {
        "id": c.id,
        "card_number": c.card_number,
        "question": c.question,
        "answer": c.answer,
        "category_name": c.category.name if c.category else None,
        "subcategory_name": c.subcategory.name if c.subcategory else None,
    }


def pick_reward_file() -> Optional[str]:
    try:
        if not os.path.isdir(REWARD_VIDEOS_DIR):
            return None
        files = [f for f in os.listdir(REWARD_VIDEOS_DIR)
                 if f.lower().endswith((".mp4", ".mov", ".webm", ".m4v"))]
        return os.path.join(REWARD_VIDEOS_DIR, random.choice(files)) if files else None
    except Exception as e:
        log.warning("Reward file pick failed: %s", e)
        return None


def increment_daily_streak(sess: Session, user_id: int) -> int:
    today = dt.date.today()
    row = sess.query(Streak).filter(Streak.user_id == str(user_id)).one_or_none()
    if not row:
        row = Streak(user_id=str(user_id), count=1, last_reward_date=today)
        sess.add(row)
        sess.commit()
        return row.count
    if row.last_reward_date == today:
        return row.count
    if row.last_reward_date == (today - dt.timedelta(days=1)):
        row.count += 1
    else:
        row.count = 1
    row.last_reward_date = today
    sess.commit()
    return row.count


# ------------------------------------------------------------------------------
# Discord Bot
# ------------------------------------------------------------------------------
intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)


@bot.event
async def setup_hook():
    if GUILD_FOR_SYNC:
        synced = await tree.sync(guild=GUILD_FOR_SYNC)
        log.info("setup_hook synced %d commands to guild %s", len(synced), GUILD_FOR_SYNC.id)
    else:
        synced = await tree.sync()
        log.info("setup_hook synced %d global commands", len(synced))


# ------------------------------------------------------------------------------
# Autocomplete providers
# ------------------------------------------------------------------------------
async def _ac_categories(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    with db() as sess:
        rows = sess.query(Category.name).order_by(Category.name.asc()).all()
    names = [r[0] for r in rows if current.lower() in r[0].lower()][:25]
    return [app_commands.Choice(name=n, value=n) for n in names]


async def _ac_subcategories(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    chosen_category = None
    try:
        chosen_category = interaction.namespace.category
    except Exception:
        pass

    with db() as sess:
        if chosen_category:
            cat_id = get_category_id_by_name(sess, chosen_category)
            if not cat_id:
                return []
            rows = (
                sess.query(Subcategory.name)
                .filter(Subcategory.category_id == cat_id)
                .order_by(Subcategory.name.asc())
                .all()
            )
        else:
            rows = sess.query(Subcategory.name).order_by(Subcategory.name.asc()).all()
    names = [r[0] for r in rows if current.lower() in r[0].lower()][:25]
    return [app_commands.Choice(name=n, value=n) for n in names]


# ------------------------------------------------------------------------------
# Slash Commands
# ------------------------------------------------------------------------------
@tree.command(name="ping", description="Health check", guild=GUILD_FOR_SYNC)
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!", ephemeral=True)


@tree.command(name="addcategory", description="Add a category", guild=GUILD_FOR_SYNC)
@app_commands.describe(name="Category name")
async def addcategory(interaction: discord.Interaction, name: str):
    with db() as sess:
        ensure_category(sess, name.strip())
    await interaction.response.send_message(f"✅ Category **{name}** added.", ephemeral=True)


@tree.command(name="addsubcategory", description="Add a subcategory", guild=GUILD_FOR_SYNC)
@app_commands.describe(category="Existing category", subcategory="New subcategory")
@app_commands.autocomplete(category=_ac_categories)
async def addsubcategory(interaction: discord.Interaction, category: str, subcategory: str):
    with db() as sess:
        c = ensure_category(sess, category.strip())
        ensure_subcategory(sess, c, subcategory.strip())
    await interaction.response.send_message(
        f"✅ Subcategory **{subcategory}** added under **{category}**.",
        ephemeral=True,
    )


@tree.command(name="addcard", description="Add a card", guild=GUILD_FOR_SYNC)
@app_commands.describe(
    question="Question",
    answer="Answer",
    category="Optional category",
    subcategory="Optional subcategory (requires category)"
)
@app_commands.autocomplete(category=_ac_categories, subcategory=_ac_subcategories)
async def addcard(
    interaction: discord.Interaction,
    question: str,
    answer: str,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
):
    with db() as sess:
        cat_obj = None
        sub_obj = None
        if category:
            cat_obj = ensure_category(sess, category.strip())
            if subcategory:
                sub_obj = ensure_subcategory(sess, cat_obj, subcategory.strip())

        number = next_card_number(sess)
        card = Card(
            card_number=number,
            question=question.strip(),
            answer=answer.strip(),
            category_id=cat_obj.id if cat_obj else None,
            subcategory_id=sub_obj.id if sub_obj else None,
        )
        sess.add(card)
        sess.commit()

    loc = subcategory or category or "No Category"
    await interaction.response.send_message(
        f"✅ Added **{number}** to **{loc}**.",
        ephemeral=True,
    )


# ---------- /listcards: improved (defer + eager load + pagination) ----------
MAX_PER_PAGE = 10

def _short(text: str, n: int = 140) -> str:
    if not text:
        return ""
    text = text.strip().replace("\n", " ")
    return text if len(text) <= n else text[: n - 1] + "…"


class ListCardsView(discord.ui.View):
    def __init__(self, items: List[str], title: str, *, timeout: int = 600):
        super().__init__(timeout=timeout)
        self.items = items
        self.page = 0
        self.title = title
        self.total_pages = max(1, (len(items) + MAX_PER_PAGE - 1) // MAX_PER_PAGE)
        # Disable if single page
        self.prev_btn.disabled = self.total_pages <= 1
        self.next_btn.disabled = self.total_pages <= 1

    def _slice(self) -> List[str]:
        start = self.page * MAX_PER_PAGE
        end = start + MAX_PER_PAGE
        return self.items[start:end]

    def _embed(self) -> discord.Embed:
        embed = discord.Embed(title=self.title, colour=discord.Colour.blurple())
        if not self.items:
            embed.description = "No cards found."
            return embed
        embed.description = "\n".join(self._slice())
        embed.set_footer(text=f"Page {self.page + 1}/{self.total_pages} • {len(self.items)} cards total")
        return embed

    async def _send_or_update(self, interaction: discord.Interaction):
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=self._embed(), view=self)
        else:
            await interaction.followup.send(embed=self._embed(), view=self, ephemeral=True)

    @discord.ui.button(label="⬅ Prev", style=discord.ButtonStyle.secondary)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await self._send_or_update(interaction)

    @discord.ui.button(label="Next ➡", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.total_pages - 1:
            self.page += 1
        await self._send_or_update(interaction)

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, discord.ui.Button):
                child.disabled = True


@tree.command(name="listcards", description="List cards (optionally filter)", guild=GUILD_FOR_SYNC)
@app_commands.describe(category="Filter by category", subcategory="Filter by subcategory")
@app_commands.autocomplete(category=_ac_categories, subcategory=_ac_subcategories)
async def listcards(interaction: discord.Interaction, category: Optional[str] = None, subcategory: Optional[str] = None):
    # Prevent 3s timeout while DB loads and we build output
    await interaction.response.defer(ephemeral=True)

    try:
        with db() as sess:
            cat_id = get_category_id_by_name(sess, category) if category else None
            sub_id = get_subcategory_id_by_name(sess, cat_id, subcategory) if (subcategory and cat_id) else None

            q = (
                sess.query(Card)
                .options(joinedload(Card.category), joinedload(Card.subcategory))
                .order_by(Card.card_number.asc())
            )
            if cat_id:
                q = q.filter(Card.category_id == cat_id)
            if sub_id:
                q = q.filter(Card.subcategory_id == sub_id)

            rows: List[Card] = q.all()

            if not rows:
                await interaction.followup.send("No cards found for that filter.", ephemeral=True)
                return

            # Build lines *inside* the session to avoid lazy-load after close.
            lines: List[str] = []
            for c in rows:
                cat = c.category.name if c.category else "No Category"
                sub = f" / *{c.subcategory.name}*" if c.subcategory else ""
                lines.append(f"`{c.card_number}` • **{cat}**{sub} — {_short(c.question)}")

        title = "Cards" if not category else f"Cards in {category}" + (f" / {subcategory}" if subcategory else "")
        view = ListCardsView(lines, title=title)
        await view._send_or_update(interaction)

    except Exception as e:
        log.exception("Error in /listcards")
        try:
            await interaction.followup.send(f"⚠️ Error listing cards: `{type(e).__name__}` — {e}", ephemeral=True)
        except Exception:
            pass
# ---------- end /listcards ----------


# ------------------------------------------------------------------------------
# Review Flow
# ------------------------------------------------------------------------------
def render_card_embed(card: Dict, index: int, total: int) -> discord.Embed:
    title = f"Question {index}/{total}"
    desc = f"**Q:** {card['question']}\n\n**A:** ||{card['answer']}||"
    embed = discord.Embed(title=title, description=desc, color=discord.Color.blurple())
    footer_bits = []
    if card.get("category_name"):
        footer_bits.append(card["category_name"])
    if card.get("subcategory_name"):
        footer_bits.append(card["subcategory_name"])
    if footer_bits:
        embed.set_footer(text=" • ".join(footer_bits))
    return embed


class ReviewView(discord.ui.View):
    """
    Button-driven review:
    - Always shows question + spoilered answer
    - ✅ / ❌ both advance to next
    - No repeats within the deck
    - On completion: reward video (if present) + streak update
    """
    def __init__(self, user_id: int, deck_ids: List[int], target_count: int):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.deck_ids = list(deck_ids)
        self.target = max(1, min(target_count, len(deck_ids)))
        self.asked: Set[int] = set()
        self.current_card_id: Optional[int] = None
        self.done: bool = False

    def _pick_next_id(self) -> Optional[int]:
        remaining = [cid for cid in self.deck_ids if cid not in self.asked]
        if not remaining or len(self.asked) >= self.target:
            return None
        return random.choice(remaining)

    async def start(self, interaction: discord.Interaction):
        next_id = self._pick_next_id()
        if next_id is None:
            await interaction.response.send_message("No cards available for review.", ephemeral=True)
            return
        self.current_card_id = next_id
        self.asked.add(next_id)
        with db() as sess:
            card = fetch_card_dict(sess, next_id)
        embed = render_card_embed(card, index=len(self.asked), total=self.target)
        await interaction.response.send_message(embed=embed, view=self, ephemeral=True)

    async def _advance(self, interaction: discord.Interaction):
        if self.done:
            await interaction.response.edit_message(content="Session already finished.", embed=None, view=None)
            return
        next_id = self._pick_next_id()
        if next_id is None:
            self.done = True
            # First, finish the original ephemeral message
            try:
                with db() as sess:
                    streak_val = increment_daily_streak(sess, self.user_id)
                await interaction.response.edit_message(
                    content=f"🎉 Review complete!",
                    embed=None,
                    view=None
                )
            except discord.InteractionResponded:
                # If already responded, best effort cleanup
                try:
                    await interaction.edit_original_response(content="🎉 Review complete!", attachments=[], view=None)
                except Exception:
                    pass

            # Then, send reward via follow-up (this is how we can upload a file)
            reward_path = pick_reward_file()
            streak_val_local = 0
            try:
                with db() as sess:
                    streak_val_local = sess.query(Streak).filter(Streak.user_id == str(self.user_id)).one().count
            except Exception:
                pass

            streak_text = f"🔥 Daily streak: **{streak_val_local}**" if streak_val_local else ""

            if reward_path and os.path.isfile(reward_path):
                try:
                    file = discord.File(reward_path, filename=os.path.basename(reward_path))
                    await interaction.followup.send(
                        content=f"🎬 Reward unlocked!\n{streak_text}",
                        file=file,
                        ephemeral=True
                    )
                    return
                except Exception as e:
                    log.warning("Failed attaching reward video via followup: %s", e)

            # Fallback: text-only follow-up
            try:
                await interaction.followup.send(
                    content=f"🎬 Reward unlocked! (no video found)\n{streak_text}",
                    ephemeral=True
                )
            except Exception:
                pass
            return

        self.current_card_id = next_id
        self.asked.add(next_id)
        with db() as sess:
            card = fetch_card_dict(sess, next_id)
        embed = render_card_embed(card, index=len(self.asked), total=self.target)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="✅ Correct", style=discord.ButtonStyle.success)
    async def correct_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._advance(interaction)

    @discord.ui.button(label="❌ Incorrect", style=discord.ButtonStyle.danger)
    async def wrong_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._advance(interaction)


@tree.command(name="reviewcards", description="Review cards", guild=GUILD_FOR_SYNC)
@app_commands.describe(
    mode="How many to review: 20, 50, or all",
    category="Optional category filter",
    subcategory="Optional subcategory filter",
)
@app_commands.choices(mode=[
    app_commands.Choice(name="Review 20", value="20"),
    app_commands.Choice(name="Review 50", value="50"),
    app_commands.Choice(name="Review All", value="all"),
])
@app_commands.autocomplete(category=_ac_categories, subcategory=_ac_subcategories)
async def reviewcards(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
):
    with db() as sess:
        cat_id = get_category_id_by_name(sess, category) if category else None
        sub_id = get_subcategory_id_by_name(sess, cat_id, subcategory) if (subcategory and cat_id) else None
        ids = candidate_card_ids(sess, cat_id, sub_id)

    if not ids:
        await interaction.response.send_message("No cards found for that selection.", ephemeral=True)
        return

    random.shuffle(ids)
    if mode.value == "20":
        target = min(20, len(ids))
    elif mode.value == "50":
        target = min(50, len(ids))
    else:
        target = len(ids)

    view = ReviewView(user_id=interaction.user.id, deck_ids=ids, target_count=target)
    await view.start(interaction)


# ------------------------------------------------------------------------------
# Main
# ------------------------------------------------------------------------------
def main():
    log.info("DISCORD_TOKEN present=%s len=%d", bool(DISCORD_TOKEN), len(DISCORD_TOKEN))
    log.info("DISCORD_CLIENT_ID present=%s", bool(DISCORD_CLIENT_ID))
    log.info("DISCORD_GUILD_ID present=%s value=%s", bool(DISCORD_GUILD_ID), DISCORD_GUILD_ID or "N/A")

    try:
        os.makedirs(REWARD_VIDEOS_DIR, exist_ok=True)
    except Exception:
        pass

    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
