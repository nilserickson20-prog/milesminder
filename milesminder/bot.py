from __future__ import annotations

"""
MilesMinder Discord Bot
- Categories & Subcategories
- Add / List Cards (clickable list; open edit/delete view)
- Review modes: 20 / 50 / All
- Review missed (only cards the user marked ❌)
- Spoilered answers
- No repeats within a session
- Optional category/subcategory filters with autocomplete
- Reward video + daily streak upon successful review session
- Tracks daily streak + lifetime reviews completed
- SQLite + SQLAlchemy with safe boot and idempotent indices + runtime migration
- Guild-scoped fast sync if DISCORD_GUILD_ID is set
"""

import os
import sys
import random
import logging
import datetime as dt
from typing import Optional, List, Dict, Set, Tuple

import discord
from discord.ext import commands
from discord import app_commands

from sqlalchemy import (
    create_engine, Column, Integer, String, ForeignKey, Text, UniqueConstraint,
    Date, DateTime, func
)
from sqlalchemy.orm import sessionmaker, relationship, declarative_base, Session, joinedload

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
    # lifetime total reviews completed
    reviews_completed = Column(Integer, nullable=False, default=0)


class Missed(Base):
    """
    Tracks cards the user missed (❌). We delete the row on a later ✅ to keep the deck fresh.
    """
    __tablename__ = "missed_cards"
    id = Column(Integer, primary_key=True)
    user_id = Column(String(32), nullable=False)
    card_id = Column(Integer, ForeignKey("cards.id"), nullable=False)
    missed_count = Column(Integer, nullable=False, default=0)
    last_missed_at = Column(DateTime, nullable=True)

    __table_args__ = (UniqueConstraint("user_id", "card_id", name="uq_missed_user_card"),)

# -------------------- NEW MODEL FOR /task --------------------
class TaskMessage(Base):
    __tablename__ = "task_messages"
    id = Column(Integer, primary_key=True)
    channel_id = Column(String(32), nullable=False)
    message_id = Column(String(32), nullable=False, unique=True)
    creator_user_id = Column(String(32), nullable=False)
    task_text = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=dt.datetime.utcnow)
# -------------------------------------------------------------

def init_db():
    Base.metadata.create_all(engine)
    # idempotent indices
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_cards_category_id ON cards(category_id)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_cards_subcategory_id ON cards(subcategory_id)")

    # runtime migrations (safe on every start)
    with engine.begin() as conn:
        # streaks table and columns
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS streaks ("
            "id INTEGER PRIMARY KEY, "
            "user_id VARCHAR(32) NOT NULL UNIQUE)"
        )
        cols = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info('streaks')").fetchall()}
        if "count" not in cols:
            conn.exec_driver_sql("ALTER TABLE streaks ADD COLUMN count INTEGER NOT NULL DEFAULT 0")
        if "last_reward_date" not in cols:
            conn.exec_driver_sql("ALTER TABLE streaks ADD COLUMN last_reward_date DATE")
        if "reviews_completed" not in cols:
            conn.exec_driver_sql("ALTER TABLE streaks ADD COLUMN reviews_completed INTEGER NOT NULL DEFAULT 0")

        # missed_cards table
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS missed_cards ("
            "id INTEGER PRIMARY KEY, "
            "user_id VARCHAR(32) NOT NULL, "
            "card_id INTEGER NOT NULL, "
            "missed_count INTEGER NOT NULL DEFAULT 0, "
            "last_missed_at DATETIME, "
            "UNIQUE(user_id, card_id))"
        )

        # -------------------- NEW TABLE FOR /task --------------------
        conn.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS task_messages ("
            "id INTEGER PRIMARY KEY, "
            "channel_id VARCHAR(32) NOT NULL, "
            "message_id VARCHAR(32) NOT NULL UNIQUE, "
            "creator_user_id VARCHAR(32) NOT NULL, "
            "task_text TEXT NOT NULL, "
            "created_at DATETIME NOT NULL)"
        )
        # ------------------------------------------------------------

init_db()

# -------------------- NEW: COMPLIMENTS FOR /task --------------------
COMPLIMENTS: List[str] = [
    "Verdict’s in: you’re guilty of being brilliant and distractingly attractive.",
    "I’d say ‘case closed,’ but I kind of hope you keep working...I like watching you.",
    "Well, well, someone’s earning extra credit in charm and effort today.",
    "Careful, Counselor — at this rate I’ll be the one falling under your cross-examination.",
    "Focus like that should be illegal. I might have to file a motion for distraction.",
    "You call that studying? Looked a lot like seduction from here.",
    "I hereby sentence you to one very flirty congratulations message. Consider it served.",
    "You’re so on top of your work — and it’s making me a little jealous I’m not what’s under you.",
    "You passed that task with flying colours...and one shade of red (mine).",
    "If discipline were a crime, you’d be doing life without parole — and I’d happily visit.",
    "I’d say ‘well done,’ but you make it sound a lot better when I say it slowly.",
    "You’re dangerously close to earning a reward that’s not multiple choice.",
    "That kind of productivity should come with a warning label: highly attractive.",
    "Good thing you’re studying law, because you just stole my attention.",
    "You make closing arguments sound like love letters.",
]
# --------------------------------------------------------------------

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
        row = Streak(user_id=str(user_id), count=1, last_reward_date=today, reviews_completed=0)
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


def increment_reviews_completed(sess: Session, user_id: int, amount: int):
    row = sess.query(Streak).filter(Streak.user_id == str(user_id)).one_or_none()
    if not row:
        row = Streak(user_id=str(user_id), count=0, last_reward_date=None, reviews_completed=0)
        sess.add(row)
        sess.flush()
    row.reviews_completed = (row.reviews_completed or 0) + max(0, int(amount))
    sess.commit()


def mark_miss(sess: Session, user_id: int, card_id: int):
    m = sess.query(Missed).filter(Missed.user_id == str(user_id), Missed.card_id == card_id).one_or_none()
    if not m:
        m = Missed(user_id=str(user_id), card_id=card_id, missed_count=1, last_missed_at=dt.datetime.utcnow())
        sess.add(m)
    else:
        m.missed_count += 1
        m.last_missed_at = dt.datetime.utcnow()
    sess.commit()


def clear_miss(sess: Session, user_id: int, card_id: int):
    m = sess.query(Missed).filter(Missed.user_id == str(user_id), Missed.card_id == card_id).one_or_none()
    if m:
        sess.delete(m)
        sess.commit()


def missed_card_ids(sess: Session, user_id: int, cat_id: Optional[int], sub_id: Optional[int]) -> List[int]:
    q = (
        sess.query(Missed.card_id)
        .join(Card, Card.id == Missed.card_id)
        .filter(Missed.user_id == str(user_id))
    )
    if cat_id:
        q = q.filter(Card.category_id == cat_id)
    if sub_id:
        q = q.filter(Card.subcategory_id == sub_id)
    q = q.order_by(Missed.last_missed_at.desc())
    return [r[0] for r in q.all()]

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
# ---------- Remove Category / Subcategory ----------

@tree.command(name="removecategory", description="Remove a category (cards will be uncategorised)", guild=GUILD_FOR_SYNC)
@app_commands.describe(name="Category to remove")
@app_commands.autocomplete(name=_ac_categories)
async def removecategory(interaction: discord.Interaction, name: str):
    # Make it quick on the client
    await interaction.response.defer(ephemeral=True)

    with db() as sess:
        cat = sess.query(Category).filter(Category.name.ilike(name.strip())).one_or_none()
        if not cat:
            await interaction.followup.send(f"Category **{name}** not found.", ephemeral=True)
            return

        # Count impacts first (for a nice message)
        affected_cards = sess.query(Card).filter(Card.category_id == cat.id).count()
        subcat_count = sess.query(Subcategory).filter(Subcategory.category_id == cat.id).count()

        # Null out category & subcategory on affected cards
        sess.query(Card).filter(Card.category_id == cat.id).update(
            {Card.category_id: None, Card.subcategory_id: None},
            synchronize_session=False
        )

        # Delete subcategories under the category (delete-orphan would handle if removing via relationship,
        # but this is explicit and avoids loading)
        sess.query(Subcategory).filter(Subcategory.category_id == cat.id).delete(synchronize_session=False)

        # Finally delete the category
        sess.delete(cat)
        sess.commit()

    await interaction.followup.send(
        f"🗑️ Removed category **{name}**.\n"
        f"• Uncategorised cards: **{affected_cards}**\n"
        f"• Subcategories deleted: **{subcat_count}**",
        ephemeral=True
    )


@tree.command(name="removesubcategory", description="Remove a subcategory (cards will lose the subcategory)", guild=GUILD_FOR_SYNC)
@app_commands.describe(
    category="The parent category",
    subcategory="The subcategory to remove"
)
@app_commands.autocomplete(category=_ac_categories, subcategory=_ac_subcategories)
async def removesubcategory(
    interaction: discord.Interaction,
    category: str,
    subcategory: str
):
    await interaction.response.defer(ephemeral=True)

    with db() as sess:
        # Find category
        cat = sess.query(Category).filter(Category.name.ilike(category.strip())).one_or_none()
        if not cat:
            await interaction.followup.send(f"Category **{category}** not found.", ephemeral=True)
            return

        # Find subcategory under that category
        sub = (
            sess.query(Subcategory)
            .filter(Subcategory.category_id == cat.id, Subcategory.name.ilike(subcategory.strip()))
            .one_or_none()
        )
        if not sub:
            await interaction.followup.send(
                f"Subcategory **{subcategory}** not found under **{category}**.",
                ephemeral=True
            )
            return

        # Count & null out references on cards
        affected_cards = sess.query(Card).filter(Card.subcategory_id == sub.id).count()
        sess.query(Card).filter(Card.subcategory_id == sub.id).update(
            {Card.subcategory_id: None},
            synchronize_session=False
        )

        # Delete subcategory
        sess.delete(sub)
        sess.commit()

    await interaction.followup.send(
        f"🗑️ Removed subcategory **{subcategory}** under **{category}**.\n"
        f"• Cards cleared of this subcategory: **{affected_cards}**",
        ephemeral=True
    )
# ---------- end remove commands ----------

# Slash Commands: addcategory / addsubcategory / addcard
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
    # Create the card, then take a safe snapshot for rendering
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
        sess.refresh(card)

        # Snapshot with names while session is active (avoids DetachedInstanceError)
        snap = fetch_card_dict(sess, card.id)
        card_id = card.id
        card_number = card.card_number

    # Build the scope title and embed from the snapshot (no relationship access here)
    if snap["category_name"] and snap["subcategory_name"]:
        scope_title = f'{snap["category_name"]} ▸ {snap["subcategory_name"]}'
    elif snap["category_name"]:
        scope_title = snap["category_name"]
    else:
        scope_title = "Cards"

    desc = f"**Q**: {snap['question']}\n\n**A**: ||{snap['answer']}||"
    footer_bits = []
    footer_bits.append(snap["category_name"] or "No Category")
    if snap["subcategory_name"]:
        footer_bits.append(snap["subcategory_name"])
    footer = " ▸ ".join(footer_bits)

    embed = discord.Embed(title=scope_title, description=desc, colour=discord.Colour.blurple())
    embed.set_footer(text=f"{footer} • {card_number}")

    # Send the card to the user ephemerally, then attach the edit view immediately
    await interaction.response.send_message(embed=embed, ephemeral=True)
    msg = await interaction.original_response()

    try:
        view = EditCatSubView(interaction.user.id, card_id, msg, scope_title)
        await msg.edit(view=view)
    except Exception as e:
        log.warning("Failed to attach EditCatSubView after addcard: %s", e)


# ------------------------------------------------------------------------------
# Button-based list + editing helpers
# ------------------------------------------------------------------------------
def _chunk(lst: List, size: int) -> List[List]:
    return [lst[i:i + size] for i in range(0, len(lst), size)]

def _embed_card_display(scope_title: str, c: Card) -> discord.Embed:
    cat = c.category.name if c.category else "No Category"
    sub = f" ▸ {c.subcategory.name}" if c.subcategory else ""
    title = scope_title or (cat + sub if sub else cat)
    desc = f"**Q**: {c.question}\n\n**A**: ||{c.answer}||"
    emb = discord.Embed(title=title, description=desc, colour=discord.Colour.blurple())
    emb.set_footer(text=f"{cat}{sub} • {c.card_number}")
    return emb

class EditCardModal(discord.ui.Modal, title="Edit Card"):
    def __init__(self, opener_user_id: int, card_id: int, original_message: discord.Message, title: str):
        super().__init__(timeout=300)
        self.opener_user_id = opener_user_id
        self.card_id = card_id
        self.original_message = original_message
        self.title_text = title

        # Pull current Q/A values
        with db() as sess:
            c = (
                sess.query(Card)
                .options(joinedload(Card.category), joinedload(Card.subcategory))
                .filter(Card.id == self.card_id)
                .one_or_none()
            )
            q_val = c.question if c else ""
            a_val = c.answer if c else ""

        # Labels must be <= 45 chars
        self.q = discord.ui.TextInput(
            label="Question",
            style=discord.TextStyle.paragraph,
            default=q_val,
            required=True,
            max_length=2000,
        )
        self.a = discord.ui.TextInput(
            label="Answer",
            style=discord.TextStyle.paragraph,
            default=a_val,
            required=True,
            max_length=2000,
        )

        self.add_item(self.q)
        self.add_item(self.a)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.opener_user_id:
            await interaction.response.send_message("You didn’t open this card.", ephemeral=True)
            return

        new_q = str(self.q.value).strip()
        new_a = str(self.a.value).strip()

        with db() as sess:
            c = (
                sess.query(Card)
                .options(joinedload(Card.category), joinedload(Card.subcategory))
                .filter(Card.id == self.card_id)
                .one_or_none()
            )
            if not c:
                await interaction.response.send_message("This card no longer exists.", ephemeral=True)
                return

            # Update only Q/A
            c.question = new_q
            c.answer = new_a
            sess.commit()
            sess.refresh(c)

        # Refresh the already-posted card embed + restore manage view
        try:
            scope_title = c.category.name if c.category else "Cards"
            if c.category and c.subcategory:
                scope_title = f"{c.category.name} ▸ {c.subcategory.name}"
            await self.original_message.edit(
                embed=_embed_card_display(scope_title, c),
                view=CardManageView(self.opener_user_id, self.card_id, scope_title),
            )
        except Exception:
            pass

        await interaction.response.send_message("Saved changes.", ephemeral=True)


def _category_options(sess: Session) -> List[discord.SelectOption]:
    rows = sess.query(Category.name).order_by(Category.name.asc()).all()
    opts = [discord.SelectOption(label="None", value="__none__")]
    for (name,) in rows:
        opts.append(discord.SelectOption(label=name, value=name))
    return opts

def _subcategory_options(sess: Session, category_name: Optional[str]) -> List[discord.SelectOption]:
    opts = [discord.SelectOption(label="None", value="__none__")]
    if not category_name or category_name == "__none__":
        return opts
    cat = sess.query(Category).filter(Category.name.ilike(category_name)).one_or_none()
    if not cat:
        return opts
    rows = (
        sess.query(Subcategory.name)
        .filter(Subcategory.category_id == cat.id)
        .order_by(Subcategory.name.asc())
        .all()
    )
    for (name,) in rows:
        opts.append(discord.SelectOption(label=name, value=name))
    return opts

class CategorySelect(discord.ui.Select):
    def __init__(self, parent_view: "EditCatSubView", current_value: Optional[str]):
        self.parent_view = parent_view
        with db() as sess:
            options = _category_options(sess)
        default_val = current_value if current_value else "__none__"
        for opt in options:
            opt.default = (opt.value == default_val)
        super().__init__(placeholder="Select Category", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        new_val = self.values[0]
        # 1) Update stored selection
        self.parent_view.selected_category = new_val

        # 2) Visually reflect the new selection in THIS dropdown
        for opt in self.options:
            opt.default = (opt.value == new_val)

        # 3) Rebuild subcategory options for the newly selected category
        with db() as sess:
            new_sub_opts = _subcategory_options(
                sess,
                None if new_val == "__none__" else new_val
            )

        # 4) Apply the rebuilt subcategory options and set its default to "None"
        self.parent_view.sub_select.options = new_sub_opts
        self.parent_view.selected_subcategory = "__none__"
        for opt in self.parent_view.sub_select.options:
            opt.default = (opt.value == "__none__")

        # 5) Re-render the message so both dropdowns show the new defaults immediately
        await interaction.response.edit_message(view=self.parent_view)

class SubcategorySelect(discord.ui.Select):
    def __init__(self, parent_view: "EditCatSubView", current_cat: Optional[str], current_sub: Optional[str]):
        self.parent_view = parent_view
        with db() as sess:
            options = _subcategory_options(sess, current_cat)
        default_val = current_sub if (current_sub and current_sub.strip()) else "__none__"
        for opt in options:
            opt.default = (opt.value == default_val)
        super().__init__(placeholder="Select Subcategory", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        new_val = self.values[0]
        # 1) Update stored selection
        self.parent_view.selected_subcategory = new_val

        # 2) Visually reflect the new selection in THIS dropdown
        for opt in self.options:
            opt.default = (opt.value == new_val)

        # 3) Re-render so the new default is shown before Save
        await interaction.response.edit_message(view=self.parent_view)


class EditCatSubView(discord.ui.View):
    """Dropdown edits of Category/Subcategory + button to open Q/A modal."""
    def __init__(self, opener_user_id: int, card_id: int, original_message: discord.Message, title: str):
        super().__init__(timeout=600)
        self.opener_user_id = opener_user_id
        self.card_id = card_id
        self.original_message = original_message
        self.title_text = title

        # Load current values
        with db() as sess:
            c = (
                sess.query(Card)
                .options(joinedload(Card.category), joinedload(Card.subcategory))
                .filter(Card.id == card_id)
                .one_or_none()
            )
        cur_cat = c.category.name if (c and c.category) else None
        cur_sub = c.subcategory.name if (c and c.subcategory) else None

        self.selected_category = cur_cat if cur_cat else "__none__"
        self.selected_subcategory = cur_sub if cur_sub else "__none__"

        # Build selects
        self.cat_select = CategorySelect(self, cur_cat)
        self.sub_select = SubcategorySelect(self, cur_cat, cur_sub)
        self.add_item(self.cat_select)
        self.add_item(self.sub_select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opener_user_id:
            await interaction.response.send_message("This editor belongs to someone else.", ephemeral=True)
            return False
        return True

    async def on_timeout(self):
        for child in self.children:
            if isinstance(child, (discord.ui.Button, discord.ui.Select)):
                child.disabled = True
        try:
            await self.original_message.edit(view=self)
        except Exception:
            pass

    @discord.ui.button(label="Open Q/A Modal", style=discord.ButtonStyle.primary)
    async def open_qa(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            EditCardModal(self.opener_user_id, self.card_id, self.original_message, self.title_text)
        )

    @discord.ui.button(label="Save Category/Subcategory", style=discord.ButtonStyle.success)
    async def save_cat_sub(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)

        cat_val = None if self.selected_category in (None, "__none__") else self.selected_category
        sub_val = None if self.selected_subcategory in (None, "__none__") else self.selected_subcategory

        with db() as sess:
            card = (
                sess.query(Card)
                .options(joinedload(Card.category), joinedload(Card.subcategory))
                .filter(Card.id == self.card_id)
                .one_or_none()
            )
            if not card:
                await interaction.followup.send("This card no longer exists.", ephemeral=True)
                return

            if not cat_val:
                card.category_id = None
                card.subcategory_id = None
            else:
                cat_obj = sess.query(Category).filter(Category.name.ilike(cat_val)).one_or_none()
                if not cat_obj:
                    await interaction.followup.send("Selected category no longer exists.", ephemeral=True)
                    return
                card.category_id = cat_obj.id

                if sub_val:
                    sub_obj = (
                        sess.query(Subcategory)
                        .filter(Subcategory.category_id == cat_obj.id, Subcategory.name.ilike(sub_val))
                        .one_or_none()
                    )
                    if not sub_obj:
                        await interaction.followup.send("Selected subcategory no longer exists.", ephemeral=True)
                        return
                    card.subcategory_id = sub_obj.id
                else:
                    card.subcategory_id = None

            sess.commit()
            sess.refresh(card)

        try:
            scope_title = card.category.name if card.category else "Cards"
            if card.category and card.subcategory:
                scope_title = f"{card.category.name} ▸ {card.subcategory.name}"
            await self.original_message.edit(
                embed=_embed_card_display(scope_title, card),
                view=CardManageView(self.opener_user_id, self.card_id, scope_title),
            )
            await interaction.followup.send("Saved.", ephemeral=True)
        except Exception:
            try:
                await interaction.followup.send("Saved (couldn’t refresh message).", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_edit(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        try:
            await self.original_message.edit(view=CardManageView(self.opener_user_id, self.card_id, self.title_text))
        except Exception:
            pass

class ConfirmDeleteView(discord.ui.View):
    def __init__(self, opener_user_id: int, card_id: int):
        super().__init__(timeout=60)
        self.opener_user_id = opener_user_id
        self.card_id = card_id

    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opener_user_id:
            await interaction.response.send_message("You didn’t open this card.", ephemeral=True)
            return
        with db() as sess:
            c = sess.query(Card).filter(Card.id == self.card_id).one_or_none()
            if not c:
                await interaction.response.send_message("Already deleted.", ephemeral=True)
                return
            sess.delete(c)
            sess.commit()
        await interaction.message.edit(content="🗑️ Card deleted.", embed=None, view=None)
        await interaction.response.send_message("Deleted.", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.message.edit(view=None)

class CardManageView(discord.ui.View):
    def __init__(self, opener_user_id: int, card_id: int, title: str):
        super().__init__(timeout=900)
        self.opener_user_id = opener_user_id
        self.card_id = card_id
        self.title = title

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opener_user_id:
            await interaction.response.send_message("You didn’t open this card.", ephemeral=True)
            return
        view = EditCatSubView(self.opener_user_id, self.card_id, interaction.message, self.title)
        try:
            await interaction.response.edit_message(view=view)
        except discord.InteractionResponded:
            await interaction.message.edit(view=view)

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opener_user_id:
            await interaction.response.send_message("You didn’t open this card.", ephemeral=True)
            return
        await interaction.response.edit_message(view=ConfirmDeleteView(self.opener_user_id, self.card_id))

class ListCardsButtonsView(discord.ui.View):
    PAGE_SIZE = 10
    def __init__(self, user_id: int, pairs: List[Tuple[int, str]], title: str):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.title = title
        self.pages = _chunk(pairs, self.PAGE_SIZE) or [[]]
        self.page_index = 0
        self._rebuild()

    def _rebuild(self):
        for it in list(self.children):
            self.remove_item(it)

        # Card buttons – label is the question (truncated), no ID shown
        for cid, q in self.pages[self.page_index]:
            label = (q[:72] + "…") if len(q) > 75 else q
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)

            async def _cb(interaction: discord.Interaction, card_id=cid):
                if interaction.user.id != self.user_id:
                    await interaction.response.send_message("This list belongs to someone else.", ephemeral=True)
                    return
                with db() as sess:
                    card = (
                        sess.query(Card)
                        .options(joinedload(Card.category), joinedload(Card.subcategory))
                        .filter(Card.id == card_id)
                        .one_or_none()
                    )
                    if not card:
                        await interaction.response.send_message("That card was not found.", ephemeral=True)
                        return
                    scope_title = card.category.name if card.category else "Cards"
                    if card.subcategory and card.category:
                        scope_title = f"{card.category.name} ▸ {card.subcategory.name}"
                    embed = _embed_card_display(scope_title, card)
                view = CardManageView(self.user_id, card_id, scope_title)
                await interaction.channel.send(embed=embed, view=view)
                try:
                    await interaction.response.defer()
                except discord.InteractionResponded:
                    pass

            btn.callback = _cb
            self.add_item(btn)

        if len(self.pages) > 1:
            prev_btn = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary)
            next_btn = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary)
            page_label = discord.ui.Button(
                label=f"Page {self.page_index + 1}/{len(self.pages)}",
                style=discord.ButtonStyle.secondary,
                disabled=True
            )

            async def prev_cb(interaction: discord.Interaction):
                if interaction.user.id != self.user_id:
                    await interaction.response.send_message("This list belongs to someone else.", ephemeral=True)
                    return
                self.page_index = (self.page_index - 1) % len(self.pages)
                self._rebuild()
                await interaction.response.edit_message(view=self)

            async def next_cb(interaction: discord.Interaction):
                if interaction.user.id != self.user_id:
                    await interaction.response.send_message("This list belongs to someone else.", ephemeral=True)
                    return
                self.page_index = (self.page_index + 1) % len(self.pages)
                self._rebuild()
                await interaction.response.edit_message(view=self)

            prev_btn.callback = prev_cb
            next_btn.callback = next_cb
            self.add_item(prev_btn)
            self.add_item(page_label)
            self.add_item(next_btn)

# ------------------------------------------------------------------------------
# /listcards (clickable; sorted by question A→Z)
# ------------------------------------------------------------------------------
@tree.command(name="listcards", description="List cards (optionally filter)", guild=GUILD_FOR_SYNC)
@app_commands.describe(category="Filter by category", subcategory="Filter by subcategory")
@app_commands.autocomplete(category=_ac_categories, subcategory=_ac_subcategories)
async def listcards(interaction: discord.Interaction, category: Optional[str] = None, subcategory: Optional[str] = None):
    await interaction.response.defer(ephemeral=True)

    try:
        with db() as sess:
            cat_id = get_category_id_by_name(sess, category) if category else None
            sub_id = get_subcategory_id_by_name(sess, cat_id, subcategory) if (subcategory and cat_id) else None

            q = (
                sess.query(Card)
                .options(joinedload(Card.category), joinedload(Card.subcategory))
                .order_by(func.lower(Card.question).asc())
            )
            if cat_id:
                q = q.filter(Card.category_id == cat_id)
            if sub_id:
                q = q.filter(Card.subcategory_id == sub_id)

            rows: List[Card] = q.all()

        if not rows:
            await interaction.followup.send("No cards found for that filter.", ephemeral=True)
            return

        pairs: List[Tuple[int, str]] = [(c.id, c.question) for c in rows]

        title = "Cards"
        if category:
            title = f"{category}"
            if subcategory:
                title = f"{category} / {subcategory}"

        view = ListCardsButtonsView(interaction.user.id, pairs, title=title)
        summary = f"Found **{len(rows)}** cards. Tap a question to open it."
        await interaction.followup.send(summary, view=view, ephemeral=True)

    except Exception as e:
        log.exception("Error in /listcards")
        try:
            await interaction.followup.send(f"⚠️ Error listing cards: `{type(e).__name__}` — {e}", ephemeral=True)
        except Exception:
            pass

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
    - Records misses (❌) and clears them on later ✅
    - No repeats within the deck
    - On completion: reward video (if present) + streak update + increment total reviews completed
    """
    def __init__(self, user_id: int, deck_ids: List[int], target_count: int):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.deck_ids = list(deck_ids)
        self.target = max(1, min(target_count, len(deck_ids)))
        self.asked: Set[int] = set()
        self.current_card_id: Optional[int] = None
        self.done: bool = False
        self.answered: int = 0

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

    async def _finish(self, interaction: discord.Interaction):
        self.done = True
        try:
            with db() as sess:
                increment_daily_streak(sess, self.user_id)
                increment_reviews_completed(sess, self.user_id, self.answered)
            await interaction.response.edit_message(
                content="🎉 Review complete!",
                embed=None,
                view=None
            )
        except discord.InteractionResponded:
            try:
                await interaction.edit_original_response(content="🎉 Review complete!", attachments=[], view=None)
            except Exception:
                pass

        reward_path = pick_reward_file()
        streak_val_local = 0
        total_reviews = 0
        try:
            with db() as sess:
                s = sess.query(Streak).filter(Streak.user_id == str(self.user_id)).one_or_none()
                if s:
                    streak_val_local = s.count or 0
                    total_reviews = s.reviews_completed or 0
        except Exception:
            pass

        streak_text = (
            f"🔥 Daily streak: **{streak_val_local}**\n"
            f"📈 Reviews completed: **{total_reviews}**"
        )

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

        try:
            await interaction.followup.send(
                content=f"🎬 Reward unlocked! (no video found)\n{streak_text}",
                ephemeral=True
            )
        except Exception:
            pass

    async def _advance(self, interaction: discord.Interaction):
        if self.done:
            await interaction.response.edit_message(content="Session already finished.", embed=None, view=None)
            return

        self.answered += 1

        next_id = self._pick_next_id()
        if next_id is None:
            await self._finish(interaction)
            return

        self.current_card_id = next_id
        self.asked.add(next_id)
        with db() as sess:
            card = fetch_card_dict(sess, next_id)
        embed = render_card_embed(card, index=len(self.asked), total=self.target)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="✅ Correct", style=discord.ButtonStyle.success)
    async def correct_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            with db() as sess:
                if self.current_card_id is not None:
                    clear_miss(sess, self.user_id, self.current_card_id)
        except Exception as e:
            log.debug("clear_miss failed: %s", e)
        await self._advance(interaction)

    @discord.ui.button(label="❌ Incorrect", style=discord.ButtonStyle.danger)
    async def wrong_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            with db() as sess:
                if self.current_card_id is not None:
                    mark_miss(sess, self.user_id, self.current_card_id)
        except Exception as e:
            log.debug("mark_miss failed: %s", e)
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

# ---------- /reviewmissed ----------
@tree.command(name="reviewmissed", description="Review only the cards you marked as ❌", guild=GUILD_FOR_SYNC)
@app_commands.describe(
    mode="How many to review: 10, 20, or all",
    category="Optional category filter",
    subcategory="Optional subcategory filter",
)
@app_commands.choices(mode=[
    app_commands.Choice(name="Review 10", value="10"),
    app_commands.Choice(name="Review 20", value="20"),
    app_commands.Choice(name="Review All Missed", value="all"),
])
@app_commands.autocomplete(category=_ac_categories, subcategory=_ac_subcategories)
async def reviewmissed(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
):
    with db() as sess:
        cat_id = get_category_id_by_name(sess, category) if category else None
        sub_id = get_subcategory_id_by_name(sess, cat_id, subcategory) if (subcategory and cat_id) else None
        ids = missed_card_ids(sess, interaction.user.id, cat_id, sub_id)

    if not ids:
        await interaction.response.send_message("🎉 You have no missed cards for that selection.", ephemeral=True)
        return

    random.shuffle(ids)
    if mode.value == "10":
        target = min(10, len(ids))
    elif mode.value == "20":
        target = min(20, len(ids))
    else:
        target = len(ids)

    view = ReviewView(user_id=interaction.user.id, deck_ids=ids, target_count=target)
    await view.start(interaction)

# ---------- /mystats ----------
@tree.command(name="mystats", description="Show your daily streak and total reviews completed", guild=GUILD_FOR_SYNC)
async def mystats(interaction: discord.Interaction):
    with db() as sess:
        s = sess.query(Streak).filter(Streak.user_id == str(interaction.user.id)).one_or_none()
    if not s:
        await interaction.response.send_message(
            "No stats yet. Start a review session to begin your streak!",
            ephemeral=True
        )
        return
    embed = discord.Embed(title="📊 Your Study Stats", color=discord.Color.blurple())
    embed.add_field(name="🔥 Daily Streak", value=str(s.count or 0), inline=True)
    embed.add_field(name="📈 Reviews Completed", value=str(s.reviews_completed or 0), inline=True)
    if s.last_reward_date:
        embed.set_footer(text=f"Last streak update: {s.last_reward_date.isoformat()}")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# -------------------- /task (no "user used /task" banner) --------------------
@tree.command(
    name="task",
    description="Post a public task for everyone to complete (react with ✅ when done).",
    guild=GUILD_FOR_SYNC
)
@app_commands.describe(text="The task to post")
async def task(interaction: discord.Interaction, text: str):
    # Silently acknowledge the slash command so no "user used /task" banner shows
    await interaction.response.defer(ephemeral=True)

    # Compose the visible task message the bot will post to the channel
    embed = discord.Embed(
        title="📝 Task",
        description=text,
        color=discord.Color.blurple()
    )
    embed.set_footer(text="React with ✅ when done")

    # Post publicly as the bot (not as the interaction response)
    channel = interaction.channel
    msg = await channel.send(embed=embed)

    # Persist so we can catch reactions after restarts
    with db() as sess:
        rec = TaskMessage(
            channel_id=str(msg.channel.id),
            message_id=str(msg.id),
            creator_user_id=str(interaction.user.id),
            task_text=text,
        )
        sess.add(rec)
        sess.commit()

    # Quiet confirmation back to the command invoker only
    await interaction.followup.send("✅ Task posted to the channel.", ephemeral=True)
# ----------------------------------------------------------------------------- 


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # Ignore bot reactions
    if payload.user_id == bot.user.id:
        return

    # We only care about ✅
    try:
        if str(payload.emoji) != "✅":
            return
    except Exception:
        return

    # Check if it's one of our task messages
    with db() as sess:
        rec: TaskMessage | None = (
            sess.query(TaskMessage)
            .filter(TaskMessage.message_id == str(payload.message_id))
            .one_or_none()
        )

    if not rec:
        return

    # Fetch channel/message and reply with a random compliment
    channel = bot.get_channel(int(rec.channel_id))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(rec.channel_id))
        except Exception:
            return

    compliment = random.choice(COMPLIMENTS)
    user_mention = f"<@{payload.user_id}>"
    try:
        msg = await channel.fetch_message(int(rec.message_id))
        await msg.reply(f"{user_mention} {compliment}")
    except Exception:
        try:
            await channel.send(f"{user_mention} {compliment}")
        except Exception:
            pass
# -----------------------------------------------------------------------------------

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
