
from __future__ import annotations

import os
import random
import logging
from typing import Optional, List, Dict, Set

import discord
from discord.ext import commands
from discord import app_commands

from sqlalchemy import (
    create_engine, Column, Integer, String, ForeignKey, Text, UniqueConstraint
)
from sqlalchemy.orm import sessionmaker, relationship, declarative_base, Session

# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# -----------------------------
# Environment
# -----------------------------
DISCORD_TOKEN = os.environ.get("DISCORD_TOKEN", "")
DISCORD_CLIENT_ID = os.environ.get("DISCORD_CLIENT_ID", "")
DISCORD_GUILD_ID = os.environ.get("DISCORD_GUILD_ID", "")

if not DISCORD_TOKEN:
    log.error("Missing DISCORD_TOKEN")
    raise SystemExit(1)

# Guild sync (optional but recommended for fast command availability)
GUILD_FOR_SYNC: Optional[discord.Object] = None
if DISCORD_GUILD_ID and DISCORD_GUILD_ID.isdigit():
    GUILD_FOR_SYNC = discord.Object(id=int(DISCORD_GUILD_ID))

# -----------------------------
# DB Setup (SQLite)
# -----------------------------
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


def init_db():
    Base.metadata.create_all(engine)
    # Helpful indices (no-op if exist)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_cards_category_id ON cards(category_id)")
        conn.exec_driver_sql("CREATE INDEX IF NOT EXISTS idx_cards_subcategory_id ON cards(subcategory_id)")


init_db()

# -----------------------------
# DB Helpers
# -----------------------------
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
    # Very simple generator; feel free to swap for something fancier.
    base = sess.query(Card).count() + 1
    while True:
        candidate = f"C{base:06d}"
        exists = sess.query(Card).filter_by(card_number=candidate).first()
        if not exists:
            return candidate
        base += 1


def get_category_id_by_name(sess: Session, name: str) -> Optional[int]:
    if not name:
        return None
    c = sess.query(Category).filter(Category.name.ilike(name)).one_or_none()
    return c.id if c else None


def get_subcategory_id_by_name(sess: Session, category_id: int, sub_name: str) -> Optional[int]:
    if not sub_name or not category_id:
        return None
    s = (
        sess.query(Subcategory)
        .filter(Subcategory.category_id == category_id, Subcategory.name.ilike(sub_name))
        .one_or_none()
    )
    return s.id if s else None


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


def candidate_card_ids(sess: Session, cat_id: Optional[int], sub_id: Optional[int]) -> List[int]:
    q = sess.query(Card.id)
    if cat_id:
        q = q.filter(Card.category_id == cat_id)
    if sub_id:
        q = q.filter(Card.subcategory_id == sub_id)
    return [r[0] for r in q.all()]


# -----------------------------
# Discord Bot
# -----------------------------
intents = discord.Intents.default()
intents.message_content = False  # Not needed for slash commands/buttons
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


async def sync_commands_for_guild():
    if GUILD_FOR_SYNC:
        try:
            synced = await tree.sync(guild=GUILD_FOR_SYNC)
            log.info("Synced %d commands to guild %s", len(synced), GUILD_FOR_SYNC.id)
        except Exception as e:
            log.exception("Guild sync failed: %s", e)
    else:
        synced = await tree.sync()
        log.info("Synced %d global commands", len(synced))


@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)


@bot.event
async def setup_hook():
    # Fast local guild sync if provided
    if GUILD_FOR_SYNC:
        await tree.sync(guild=GUILD_FOR_SYNC)
        log.info("setup_hook synced commands to guild %s", GUILD_FOR_SYNC.id)
    else:
        await tree.sync()
        log.info("setup_hook synced global commands")


# -----------------------------
# Autocomplete helpers
# -----------------------------
async def _ac_categories(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    with db() as sess:
        rows = sess.query(Category.name).order_by(Category.name.asc()).all()
    names = [r[0] for r in rows if current.lower() in r[0].lower()][:25]
    return [app_commands.Choice(name=n, value=n) for n in names]


async def _ac_subcategories(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    # Subcategory autocomplete depends on category option in the same interaction (if any)
    cat_val = None
    try:
        cat_val = interaction.namespace.category  # may be None
    except Exception:
        pass

    with db() as sess:
        if cat_val:
            cat_id = get_category_id_by_name(sess, cat_val)
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


# -----------------------------
# Slash Commands
# -----------------------------
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
@app_commands.describe(category="Existing category", subcategory="New subcategory name")
@app_commands.autocomplete(category=_ac_categories)
async def addsubcategory(interaction: discord.Interaction, category: str, subcategory: str):
    with db() as sess:
        c = ensure_category(sess, category.strip())
        ensure_subcategory(sess, c, subcategory.strip())
    await interaction.response.send_message(
        f"✅ Subcategory **{subcategory}** added under **{category}**.", ephemeral=True
    )


@tree.command(name="addcard", description="Add a flash card", guild=GUILD_FOR_SYNC)
@app_commands.describe(
    question="Card question (required)",
    answer="Card answer (required)",
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

    where = (subcategory or category or "No Category")
    await interaction.response.send_message(
        f"✅ Added **{number}** to **{where}**.", ephemeral=True
    )


@tree.command(name="listcards", description="List cards (optionally filter)", guild=GUILD_FOR_SYNC)
@app_commands.describe(category="Filter by category", subcategory="Filter by subcategory")
@app_commands.autocomplete(category=_ac_categories, subcategory=_ac_subcategories)
async def listcards(
    interaction: discord.Interaction,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
):
    with db() as sess:
        cat_id = get_category_id_by_name(sess, category) if category else None
        sub_id = get_subcategory_id_by_name(sess, cat_id, subcategory) if (subcategory and cat_id) else None

        q = sess.query(Card).order_by(Card.question.asc())
        if cat_id:
            q = q.filter(Card.category_id == cat_id)
        if sub_id:
            q = q.filter(Card.subcategory_id == sub_id)

        cards = q.all()

        if not cards:
            await interaction.response.send_message("No cards found for that filter.", ephemeral=True)
            return

        lines = []
        for c in cards[:100]:  # cap display
            cat = c.category.name if c.category else "No Category"
            sub = f" • {c.subcategory.name}" if c.subcategory else ""
            lines.append(f"• **{c.question}**  _(#{c.card_number}; {cat}{sub})_")

        more = "" if len(cards) <= 100 else f"\n…and {len(cards) - 100} more."
        await interaction.response.send_message(
            f"Found **{len(cards)}** cards.\n\n" + "\n".join(lines) + more,
            ephemeral=True
        )


# -----------------------------
# Review Flow (buttons, spoiler answers)
# -----------------------------
def _render_card_embed(card: Dict, index: int, total: int) -> discord.Embed:
    title = f"Question {index}/{total}"
    desc = f"**Q:** {card['question']}\n\n**A:** ||{card['answer']}||"
    embed = discord.Embed(title=title, description=desc, color=discord.Color.blurple())
    footer = []
    if card.get("category_name"):
        footer.append(card["category_name"])
    if card.get("subcategory_name"):
        footer.append(card["subcategory_name"])
    if footer:
        embed.set_footer(text=" • ".join(footer))
    return embed


class ReviewView(discord.ui.View):
    def __init__(self, user_id: int, deck_ids: List[int], target: int):
        super().__init__(timeout=600)
        self.user_id = user_id
        self.deck_ids: List[int] = list(deck_ids)
        self.target: int = target if target > 0 else len(self.deck_ids)
        self.asked: Set[int] = set()
        self.current_card_id: Optional[int] = None

    def _pick_next_id(self) -> Optional[int]:
        remaining = [cid for cid in self.deck_ids if cid not in self.asked]
        if not remaining or len(self.asked) >= self.target:
            return None
        return random.choice(remaining)

    def _finish_text(self) -> str:
        # Hook in reward video / streaks if you like.
        # Example plain finish text:
        return "🎉 Review complete! Great work."

    async def start_first(self, interaction: discord.Interaction):
        next_id = self._pick_next_id()
        if next_id is None:
            await interaction.response.send_message("No cards available for review.", ephemeral=True)
            return
        self.current_card_id = next_id
        self.asked.add(next_id)
        with db() as sess:
            card = fetch_card_dict(sess, next_id)
        embed = _render_card_embed(card, index=len(self.asked), total=self.target)
        await interaction.response.send_message(embed=embed, view=self, ephemeral=True)

    async def _advance(self, interaction: discord.Interaction):
        next_id = self._pick_next_id()
        if next_id is None:
            await interaction.response.edit_message(content=self._finish_text(), embed=None, view=None)
            return
        self.current_card_id = next_id
        self.asked.add(next_id)
        with db() as sess:
            card = fetch_card_dict(sess, next_id)
        embed = _render_card_embed(card, index=len(self.asked), total=self.target)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="✅ Correct", style=discord.ButtonStyle.success)
    async def correct_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._advance(interaction)

    @discord.ui.button(label="❌ Incorrect", style=discord.ButtonStyle.danger)
    async def wrong_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._advance(interaction)


@tree.command(name="reviewcards", description="Review cards", guild=GUILD_FOR_SYNC)
@app_commands.describe(
    mode="How many to review: 20, 50 or all",
    category="Optional category filter",
    subcategory="Optional subcategory filter"
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

    target = len(ids)
    if mode.value == "20":
        target = min(20, len(ids))
    elif mode.value == "50":
        target = min(50, len(ids))
    else:
        target = len(ids)

    random.shuffle(ids)
    view = ReviewView(user_id=interaction.user.id, deck_ids=ids, target=target)
    await view.start_first(interaction)


# -----------------------------
# Main
# -----------------------------
def main():
    # A quick console note about envs
    log.info("DISCORD_TOKEN present=%s len=%d", bool(DISCORD_TOKEN), len(DISCORD_TOKEN))
    log.info("DISCORD_CLIENT_ID present=%s", bool(DISCORD_CLIENT_ID))
    log.info("DISCORD_GUILD_ID present=%s value=%s", bool(DISCORD_GUILD_ID), DISCORD_GUILD_ID or "N/A")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
