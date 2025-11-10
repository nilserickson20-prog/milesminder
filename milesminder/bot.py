from __future__ import annotations

import os
import asyncio
import logging
import random
import time
import pathlib
from datetime import datetime
from typing import Optional, Dict, Tuple, List

import discord
from discord import app_commands
from discord.ext import commands

from .db import init_db, get_session
from .models import Category, Subcategory, Card, ReviewStat, Streak
from .utils import (
    get_or_create_category,
    get_or_create_subcategory,
    generate_unique_card_number,
    mark_daily_activity,
    EASTERN,
)

# ---------------------------------------------------------------------------
# Logging & Environment
# ---------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = int(os.environ.get("GUILD_ID", "0"))
MY_GUILD = discord.Object(id=GUILD_ID) if GUILD_ID else None
REWARD_VIDS = os.environ.get("REWARD_VIDEO_URLS", "")  # optional, comma-separated URLs

# ---------------------------------------------------------------------------
# Discord Bot / Intents
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.reactions = True
intents.message_content = False  # not needed for slash commands

bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
# Active review messages: message_id -> session_key
active_reviews: Dict[int, Tuple[int, Optional[int], Optional[int]]] = {}

# Review sessions: (user_id, category_id, subcategory_id) -> {pool:set[int], target:int, completed:int, last:int|None}
REVIEW_SESSIONS: Dict[Tuple[int, Optional[int], Optional[int]], dict] = {}

# Debounce raw reactions: (message_id, user_id) -> last_ts
_last_handle: Dict[Tuple[int, int], float] = {}

# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def _choose_reward_video() -> Optional[str]:
    """
    Return a random reward video URL/file path if configured.
    Looks at REWARD_VIDEO_URLS env first; if empty, tries /app/assets/rewards/*.
    """
    csv = (REWARD_VIDS or "").strip()
    if csv:
        opts = [u.strip() for u in csv.split(",") if u.strip()]
        if opts:
            return random.choice(opts)

    # fallback to filesystem bundle
    candidates: List[str] = []
    for ext in ("mp4", "mov", "webm"):
        candidates.extend([str(p) for p in pathlib.Path("/app/assets/rewards").glob(f"*.{ext}")])
    if candidates:
        return random.choice(candidates)
    return None


def _category_names(prefix: str = "", limit: int = 25) -> List[str]:
    with get_session() as db:
        q = db.query(Category)
        if prefix:
            q = q.filter(Category.name.ilike(f"{prefix.strip()}%"))
        return [c.name for c in q.order_by(Category.name.asc()).limit(limit).all()]


def _subcategory_names(prefix: str = "", limit: int = 25) -> List[str]:
    with get_session() as db:
        q = db.query(Subcategory)
        if prefix:
            q = q.filter(Subcategory.name.ilike(f"{prefix.strip()}%"))
        subs = q.join(Category).order_by(Category.name.asc(), Subcategory.name.asc()).limit(limit).all()
        # render as "Category ▸ Subcategory" for clarity; value will be just the sub name
        return [f"{s.category.name} ▸ {s.name}" for s in subs]


def _candidate_cards(db, category_id: Optional[int], subcategory_id: Optional[int]) -> List[Card]:
    q = db.query(Card)
    if subcategory_id:
        q = q.filter(Card.subcategory_id == subcategory_id)
    elif category_id:
        q = q.filter(Card.category_id == category_id)
    return q.all()


def _embed_review(title: str, card: Card, remaining: int, completed: int, target: int) -> discord.Embed:
    e = discord.Embed(
        title=title,
        description=f"**Q:** {card.question}\n**A:** ||{card.answer}||",
        color=discord.Color.green(),
    )
    e.set_footer(text=f"Completed: {completed}/{target} — Remaining: {remaining}")
    return e


def _embed_card_display(title: str, card: Card) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=f"**Q:** {card.question}\n**A:** ||{card.answer}||",
        color=discord.Color.blurple(),
    )


def _session_key(user_id: int, category_id: Optional[int], subcategory_id: Optional[int]) -> Tuple[int, Optional[int], Optional[int]]:
    return (user_id, category_id, subcategory_id)


def _prepare_pool(card_ids: List[int], mode: str) -> Tuple[set, int]:
    """Return (pool, target) where pool is a set of ids to be answered correctly once."""
    if mode == "20":
        sample = random.sample(card_ids, k=min(20, len(card_ids)))
    elif mode == "50":
        sample = random.sample(card_ids, k=min(50, len(card_ids)))
    else:  # "all"
        sample = card_ids[:]
    pool = set(sample)
    target = len(pool)
    return pool, target


def _pick_from_pool(pool: set, last: Optional[int]) -> Optional[int]:
    if not pool:
        return None
    if len(pool) == 1:
        return next(iter(pool))
    # avoid repeating the last card if possible
    candidates = list(pool)
    if last in candidates:
        candidates = [c for c in candidates if c != last] or list(pool)
    return random.choice(candidates)


async def _post_next_card(
    channel: discord.abc.MessageableChannel,
    user_id: int,
    category_id: Optional[int],
    subcategory_id: Optional[int],
):
    key = _session_key(user_id, category_id, subcategory_id)
    sess = REVIEW_SESSIONS.get(key)
    if not sess or not sess["pool"]:
        await channel.send("No more cards remaining in this session.")
        return None, None

    next_id = _pick_from_pool(sess["pool"], sess.get("last"))
    if next_id is None:
        await channel.send("No more cards remaining in this session.")
        return None, None

    with get_session() as db:
        card = db.query(Card).filter(Card.id == next_id).one_or_none()
        if not card:
            # remove missing card from pool
            sess["pool"].discard(next_id)
            return await _post_next_card(channel, user_id, category_id, subcategory_id)

        title = "Review: All Categories"
        if card.category and not subcategory_id and category_id:
            title = f"Review: {card.category.name}"
        if card.category and card.subcategory and subcategory_id:
            title = f"Review: {card.category.name} ▸ {card.subcategory.name}"

        embed = _embed_review(title, card, remaining=len(sess["pool"]), completed=sess["completed"], target=sess["target"])

    view = ReviewView(user_id=user_id, category_id=category_id, subcategory_id=subcategory_id, card_id=card.id)
    msg = await channel.send(embed=embed, view=view)
    active_reviews[msg.id] = key
    sess["last"] = card.id
    return msg, card


async def _record_answer_and_maybe_advance(
    channel: discord.abc.MessageableChannel,
    message: Optional[discord.Message],
    user_id: int,
    category_id: Optional[int],
    subcategory_id: Optional[int],
    card_id: int,
    correct: bool,
):
    key = _session_key(user_id, category_id, subcategory_id)
    sess = REVIEW_SESSIONS.get(key)
    if not sess:
        if message is not None:
            try:
                await message.edit(content="Session expired.", embed=None, view=None)
            except Exception:
                pass
        return

    with get_session() as db:
        # update per-card stats
        stat = db.query(ReviewStat).filter(ReviewStat.user_id == str(user_id), ReviewStat.card_id == card_id).one_or_none()
        if not stat:
            stat = ReviewStat(user_id=str(user_id), card_id=card_id, rights=0, wrongs=0)
            db.add(stat)
        if correct:
            stat.rights = (stat.rights or 0) + 1
        else:
            stat.wrongs = (stat.wrongs or 0) + 1
        stat.last_reviewed_at = datetime.utcnow()
        db.commit()

    # Only decrement pool / count completion when correct
    if correct and card_id in sess["pool"]:
        sess["pool"].discard(card_id)
        sess["completed"] += 1

    # Completed?
    if not sess["pool"]:
        reward = _choose_reward_video()
        if reward:
            try:
                if reward.lower().startswith(("http://", "https://")):
                    await channel.send(reward)
                else:
                    await channel.send(file=discord.File(reward))
            except Exception:
                logging.exception("Failed to send reward video")

        # reward triggers daily streak
        with get_session() as db:
            streak = mark_daily_activity(db, user_id)

        await channel.send(f"🎉 <@{user_id}> finished the review!\n🔥 **Streak:** {streak.current_streak} day(s)")
        if message is not None:
            try:
                await message.edit(view=None)
            except Exception:
                pass
        # end session
        REVIEW_SESSIONS.pop(key, None)
        return

    # Otherwise advance
    new_msg, _ = await _post_next_card(channel, user_id, category_id, subcategory_id)
    if message is not None:
        try:
            await message.edit(view=None)
        except Exception:
            pass
    return new_msg

# ---------------------------------------------------------------------------
# UI Views
# ---------------------------------------------------------------------------
class ReviewView(discord.ui.View):
    def __init__(self, user_id: int, category_id: Optional[int], subcategory_id: Optional[int], card_id: int):
        super().__init__(timeout=1200)
        self.user_id = user_id
        self.category_id = category_id
        self.subcategory_id = subcategory_id
        self.card_id = card_id

    async def _handle(self, interaction: discord.Interaction, correct: bool):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn’t your review session.", ephemeral=True)
            return
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            pass
        await _record_answer_and_maybe_advance(
            channel=interaction.channel,
            message=interaction.message,
            user_id=self.user_id,
            category_id=self.category_id,
            subcategory_id=self.subcategory_id,
            card_id=self.card_id,
            correct=correct,
        )

    @discord.ui.button(label="✅ Correct", style=discord.ButtonStyle.success)
    async def btn_correct(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, True)

    @discord.ui.button(label="❌ Incorrect", style=discord.ButtonStyle.danger)
    async def btn_incorrect(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, False)

# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    init_db()
    try:
        if MY_GUILD:
            bot.tree.copy_global_to(guild=MY_GUILD)
            synced = await bot.tree.sync(guild=MY_GUILD)
            logging.info(f"Synced {len(synced)} commands to guild {GUILD_ID}")
        else:
            synced = await bot.tree.sync()
            logging.info(f"Synced {len(synced)} global commands (no GUILD_ID set)")
    except Exception as e:
        logging.exception(f"Failed to sync commands: {e}")
    logging.info(f"Logged in as {bot.user}")

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    try:
        if bot.user and payload.user_id == bot.user.id:
            return

        key = active_reviews.get(payload.message_id)
        if not key:
            return

        user_id, category_id, subcategory_id = key
        if user_id != payload.user_id:
            return

        emoji = str(payload.emoji)
        if emoji not in ("✅", "❌"):
            return

        now = time.time()
        dkey = (payload.message_id, payload.user_id)
        last = _last_handle.get(dkey, 0.0)
        if last + 0.8 > now:
            return
        _last_handle[dkey] = now

        channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
        old_message = None
        try:
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                old_message = await channel.fetch_message(payload.message_id)
        except Exception:
            pass

        correct = (emoji == "✅")
        # current card id is the session's "last"
        sess = REVIEW_SESSIONS.get(key)
        card_id = sess.get("last") if sess else None
        if card_id is None:
            return

        await _record_answer_and_maybe_advance(
            channel=channel,
            message=old_message,
            user_id=user_id,
            category_id=category_id,
            subcategory_id=subcategory_id,
            card_id=card_id,
            correct=correct,
        )

        active_reviews.pop(payload.message_id, None)

    except Exception:
        logging.exception("raw_reaction handler error")

# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
@bot.tree.command(description="Show MilesMinder commands")
async def helpmiles(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Commands**\n"
        "/addcategory name\n"
        "/editcategory old_name [new_name] [delete]\n"
        "/addsubcategory category subcategory\n"
        "/editsubcategory category old_subcategory [new_subcategory] [delete]\n"
        "/addcard question answer [category] [subcategory]\n"
        "/listcards scope:[all|category|subcategory] [category] [subcategory]\n"
        "/reviewcards mode:[20|50|all] [category] [subcategory]\n"
        "/sync — force resync commands",
        ephemeral=True
    )

@bot.tree.command(description="Force resync of application commands")
async def sync(interaction: discord.Interaction):
    try:
        if MY_GUILD and interaction.guild and interaction.guild.id == GUILD_ID:
            bot.tree.copy_global_to(guild=MY_GUILD)
            cmds = await bot.tree.sync(guild=MY_GUILD)
            await interaction.response.send_message(f"Synced {len(cmds)} commands to this guild.", ephemeral=True)
        else:
            cmds = await bot.tree.sync()
            await interaction.response.send_message(f"Synced {len(cmds)} commands globally.", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Sync failed: {e}", ephemeral=True)

# ----- Category & Subcategory management -----
@bot.tree.command(description="Add a new category")
async def addcategory(interaction: discord.Interaction, name: str):
    with get_session() as db:
        existing = db.query(Category).filter(Category.name.ilike(name)).one_or_none()
        if existing:
            await interaction.response.send_message(f"Category **{existing.name}** already exists.", ephemeral=True)
            return
        db.add(Category(name=name.strip()))
    await interaction.response.send_message(f"Created category **{name.strip()}**.", ephemeral=True)

@bot.tree.command(description="Rename or delete a category")
async def editcategory(interaction: discord.Interaction, old_name: str, new_name: Optional[str] = None, delete: Optional[bool] = False):
    with get_session() as db:
        cat = db.query(Category).filter(Category.name.ilike(old_name.strip())).one_or_none()
        if not cat:
            await interaction.response.send_message("Category not found.", ephemeral=True)
            return
        if delete:
            db.delete(cat)
            await interaction.response.send_message("Category deleted.", ephemeral=True)
            return
        if new_name:
            cat.name = new_name.strip()
            await interaction.response.send_message(f"Renamed to **{cat.name}**.", ephemeral=True)
            return
        await interaction.response.send_message("No changes provided.", ephemeral=True)

@bot.tree.command(description="Add a subcategory under a category")
async def addsubcategory(interaction: discord.Interaction, category: str, subcategory: str):
    with get_session() as db:
        cat = get_or_create_category(db, category)
        sub = get_or_create_subcategory(db, cat, subcategory)
        await interaction.response.send_message(f"Added **{cat.name} ▸ {sub.name}**.", ephemeral=True)

@bot.tree.command(description="Rename or delete a subcategory")
async def editsubcategory(interaction: discord.Interaction, category: str, old_subcategory: str, new_subcategory: Optional[str] = None, delete: Optional[bool] = False):
    with get_session() as db:
        cat = db.query(Category).filter(Category.name.ilike(category.strip())).one_or_none()
        if not cat:
            await interaction.response.send_message("Category not found.", ephemeral=True)
            return
        sub = db.query(Subcategory).filter(
            Subcategory.category_id == cat.id,
            Subcategory.name.ilike(old_subcategory.strip())
        ).one_or_none()
        if not sub:
            await interaction.response.send_message("Subcategory not found.", ephemeral=True)
            return
        if delete:
            db.delete(sub)
            await interaction.response.send_message("Subcategory deleted.", ephemeral=True)
            return
        if new_subcategory:
            sub.name = new_subcategory.strip()
            await interaction.response.send_message(f"Renamed to **{cat.name} ▸ {sub.name}**.", ephemeral=True)
            return
        await interaction.response.send_message("No changes provided.", ephemeral=True)

# ----- Cards -----
@bot.tree.command(description="Add a flashcard")
async def addcard(interaction: discord.Interaction, question: str, answer: str, category: Optional[str] = None, subcategory: Optional[str] = None):
    with get_session() as db:
        cat = get_or_create_category(db, category) if category else None
        sub = get_or_create_subcategory(db, cat, subcategory) if (cat and subcategory) else None
        card_num = generate_unique_card_number(db, cat.name if cat else None)
        c = Card(card_number=card_num, question=question.strip(), answer=answer.strip(), category=cat, subcategory=sub)
        db.add(c)
    await interaction.response.send_message(f"Added card **{question[:70]}**.", ephemeral=True)

@addcard.autocomplete("category")
async def ac_addcard_category(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=n, value=n) for n in _category_names(current)]

@addcard.autocomplete("subcategory")
async def ac_addcard_subcategory(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=n, value=n.split(' ▸ ')[-1]) for n in _subcategory_names(current)]

@bot.tree.command(description="List cards (scope: all/category/subcategory)")
async def listcards(interaction: discord.Interaction, scope: str, category: Optional[str] = None, subcategory: Optional[str] = None):
    scope = (scope or "all").lower().strip()
    with get_session() as db:
        if scope == "subcategory":
            cat = db.query(Category).filter(Category.name.ilike((category or '').strip())).one_or_none()
            if not cat:
                await interaction.response.send_message("Category not found.", ephemeral=True)
                return
            sub = db.query(Subcategory).filter(
                Subcategory.category_id == cat.id,
                Subcategory.name.ilike((subcategory or '').strip())
            ).one_or_none()
            if not sub:
                await interaction.response.send_message("Subcategory not found.", ephemeral=True)
                return
            title = f"{cat.name} ▸ {sub.name}"
            cards = db.query(Card).filter(Card.subcategory_id == sub.id).order_by(Card.question.asc()).all()
        elif scope == "category":
            cat = db.query(Category).filter(Category.name.ilike((category or '').strip())).one_or_none()
            if not cat:
                await interaction.response.send_message("Category not found.", ephemeral=True)
                return
            title = f"{cat.name}"
            cards = db.query(Card).filter(Card.category_id == cat.id).order_by(Card.question.asc()).all()
        else:
            title = "All Categories"
            cards = db.query(Card).order_by(Card.question.asc()).all()

        if not cards:
            await interaction.response.send_message("No cards found.", ephemeral=True)
            return

        lines = [f"{i+1}. {c.question}" for i, c in enumerate(cards[:50])]
        text = f"**{title}** — {len(cards)} card(s):\n" + "\n".join(lines)
        if len(cards) > 50:
            text += f"\n… ({len(cards) - 50} more not shown)"
    await interaction.response.send_message(text, ephemeral=True)

@listcards.autocomplete("category")
async def ac_listcards_category(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=n, value=n) for n in _category_names(current)]

@listcards.autocomplete("subcategory")
async def ac_listcards_subcategory(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=n, value=n.split(' ▸ ')[-1]) for n in _subcategory_names(current)]

# ----- Review -----
@bot.tree.command(description="Review cards: mode=[20|50|all] with optional category/subcategory")
async def reviewcards(interaction: discord.Interaction, mode: str, category: Optional[str] = None, subcategory: Optional[str] = None):
    mode = (mode or "20").lower().strip()
    if mode not in ("20", "50", "all"):
        await interaction.response.send_message("Mode must be one of: 20, 50, all.", ephemeral=True)
        return

    user_id = interaction.user.id

    with get_session() as db:
        cat_id = None
        sub_id = None
        cards: List[Card] = []

        if subcategory and not category:
            await interaction.response.send_message("Please provide a category when specifying a subcategory.", ephemeral=True)
            return

        if category:
            cat = db.query(Category).filter(Category.name.ilike(category.strip())).one_or_none()
            if not cat:
                await interaction.response.send_message("Category not found.", ephemeral=True)
                return
            cat_id = cat.id

        if category and subcategory:
            sub = db.query(Subcategory).filter(
                Subcategory.category_id == cat_id,
                Subcategory.name.ilike(subcategory.strip())
            ).one_or_none()
            if not sub:
                await interaction.response.send_message("Subcategory not found.", ephemeral=True)
                return
            sub_id = sub.id

        cards = _candidate_cards(db, cat_id, sub_id)
        if not cards:
            await interaction.response.send_message("No cards available for that selection.", ephemeral=True)
            return

from __future__ import annotations

import os
import asyncio
import logging
from typing import Optional, List, Dict, Set, Tuple
from dataclasses import dataclass, field
import random

import discord
from discord import app_commands
from discord.ext import commands

# Local modules
from .db import init_db, get_session
from .models import Category, Subcategory, Card
from .utils import (
    ensure_user_stats,
    mark_daily_activity,       # returns streak int
    next_reward_video_url,     # returns url or None
    slugify_name,              # small util to normalise names
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("milesminder.bot")

TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")  # optional: restrict sync to a guild
REWARD_VIDEO_URLS = os.environ.get("REWARD_VIDEO_URLS", "")  # comma-separated optional fallback

intents = discord.Intents.default()
intents.message_content = True  # for reaction fallbacks and content if needed
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ----------------------
# Helpers / DB utilities
# ----------------------

def _find_category_id_by_name(name: str) -> Optional[int]:
    if not name:
        return None
    with get_session() as db:
        cat = db.query(Category).filter(Category.name.ilike(name)).first()
        return cat.id if cat else None

def _find_subcategory_id_by_name(cat_id: int, sub_name: str) -> Optional[int]:
    if not sub_name or not cat_id:
        return None
    with get_session() as db:
        sub = (
            db.query(Subcategory)
            .filter(Subcategory.category_id == cat_id)
            .filter(Subcategory.name.ilike(sub_name))
            .first()
        )
        return sub.id if sub else None

async def _send_card_embed(
    destination: discord.abc.Messageable,
    card_id: int,
    include_buttons: bool = True,
) -> discord.Message:
    """Fetch a card inside a short-lived session and send an embed with spoilered answer."""
    with get_session() as db:
        c = db.get(Card, card_id)
        if not c:
            return await destination.send("Card no longer exists.")
        question = c.question
        answer = c.answer
        number = c.card_number
        category_id = c.category_id
        subcategory_id = c.subcategory_id

        # Resolve names without relationships to avoid lazy loads
        category_name = None
        if category_id:
            cat = db.query(Category).get(category_id)
            category_name = cat.name if cat else None
        subcategory_name = None
        if subcategory_id:
            sub = db.query(Subcategory).get(subcategory_id)
            subcategory_name = sub.name if sub else None

    title = f"Card #{number}"
    if category_name:
        title += f" · {category_name}"
        if subcategory_name:
            title += f" › {subcategory_name}"

    embed = discord.Embed(title=title, description=question)
    embed.add_field(name="Answer", value=f"||{answer}||", inline=False)

    if include_buttons:
        view = discord.ui.View(timeout=None)
        view.add_item(ShowEditButton(card_id=card_id))
        view.add_item(DeleteCardButton(card_id=card_id))
        return await destination.send(embed=embed, view=view)
    else:
        return await destination.send(embed=embed)

def _query_cards(
    category_id: Optional[int],
    subcategory_id: Optional[int],
) -> List[int]:
    """Return list of card IDs matching scope."""
    with get_session() as db:
        q = db.query(Card.id)
        if category_id:
            q = q.filter(Card.category_id == category_id)
        if subcategory_id:
            q = q.filter(Card.subcategory_id == subcategory_id)
        return [row[0] for row in q.all()]

async def _sync_commands_for_guild():
    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))
        await tree.sync(guild=guild)
        log.info("Slash commands synced to guild %s", GUILD_ID)
    else:
        await tree.sync()
        log.info("Slash commands globally synced (can take a bit).")

# ----------------------
# Autocomplete providers
# ----------------------

async def _ac_categories(interaction: discord.Interaction, current: str):
    with get_session() as db:
        q = db.query(Category).order_by(Category.name.asc())
        if current:
            q = q.filter(Category.name.ilike(f"%{current}%"))
        rows = q.limit(25).all()
        return [app_commands.Choice(name=c.name, value=c.name) for c in rows]

async def _ac_subcategories(interaction: discord.Interaction, current: str):
    # Needs the already-typed category to scope suggestions
    cat_value = interaction.namespace.get("category") if hasattr(interaction, "namespace") else None
    cat_id = _find_category_id_by_name(cat_value) if cat_value else None
    if not cat_id:
        return []
    with get_session() as db:
        q = (
            db.query(Subcategory)
            .filter(Subcategory.category_id == cat_id)
            .order_by(Subcategory.name.asc())
        )
        if current:
            q = q.filter(Subcategory.name.ilike(f"%{current}%"))
        rows = q.limit(25).all()
        return [app_commands.Choice(name=s.name, value=s.name) for s in rows]

# ----------------------
# Review session tracking
# ----------------------

@dataclass
class ReviewState:
    user_id: int
    mode: str  # "20" | "50" | "all"
    category_id: Optional[int] = None
    subcategory_id: Optional[int] = None
    pool: List[int] = field(default_factory=list)     # remaining candidates (IDs)
    answered: Set[int] = field(default_factory=set)   # correctly answered in this session
    used: Set[int] = field(default_factory=set)       # seen in this session (for variety)
    message_id: Optional[int] = None

# key by (channel_id, user_id) to keep one session per user per channel
active_reviews: Dict[Tuple[int, int], ReviewState] = {}

def _build_pool(all_ids: List[int], mode: str) -> List[int]:
    ids = list(all_ids)
    random.shuffle(ids)
    if mode == "20":
        return ids[:20]
    if mode == "50":
        return ids[:50]
    return ids

async def _finish_review(interaction: discord.Interaction, state: ReviewState):
    # Reward
    url = next_reward_video_url(REWARD_VIDEO_URLS)
    if url:
        await interaction.followup.send(url)
    # Streak
    streak = 0
    try:
        with get_session() as db:
            ensure_user_stats(db, interaction.user.id)
            streak = mark_daily_activity(db, interaction.user.id)
    except Exception as e:
        log.warning("Failed to mark streak: %s", e)
    await interaction.followup.send(f"Nice work — session complete! Streak: **{streak} 🔥**")

async def _post_next_card_interaction(
    interaction: discord.Interaction,
    state: ReviewState,
) -> None:
    """Pick a next not-yet-correct card from pool, avoiding immediate repeats if possible."""
    # Determine candidate
    candidates = [cid for cid in state.pool if cid not in state.answered]
    if not candidates:
        await _finish_review(interaction, state)
        active_reviews.pop((interaction.channel_id, interaction.user.id), None)
        return

    # Try to pick one not used very recently
    random.shuffle(candidates)
    for cid in candidates:
        if cid not in state.used:
            chosen = cid
            break
    else:
        # all have been used; allow reuse
        chosen = candidates[0]

    state.used.add(chosen)
    # Send the card
    msg = await _send_card_embed(interaction.followup, card_id=chosen, include_buttons=True)
    state.message_id = msg.id

# ----------------------
# UI Components (List view / Show / Edit / Delete)
# ----------------------

class ShowEditButton(discord.ui.Button):
    def __init__(self, card_id: int):
        super().__init__(label="Edit", style=discord.ButtonStyle.secondary, custom_id=f"edit:{card_id}")
        self.card_id = card_id

    async def callback(self, interaction: discord.Interaction):
        # Present a modal to edit question/answer
        with get_session() as db:
            c = db.get(Card, self.card_id)
            if not c:
                await interaction.response.send_message("Card no longer exists.", ephemeral=True)
                return
            question = c.question
            answer = c.answer

        modal = EditCardModal(card_id=self.card_id, question=question, answer=answer)
        await interaction.response.send_modal(modal)

class DeleteCardButton(discord.ui.Button):
    def __init__(self, card_id: int):
        super().__init__(label="Delete", style=discord.ButtonStyle.danger, custom_id=f"del:{card_id}")
        self.card_id = card_id

    async def callback(self, interaction: discord.Interaction):
        # Ask for confirmation via ephemeral buttons
        view = ConfirmDeleteView(card_id=self.card_id)
        await interaction.response.send_message("Delete this card?", view=view, ephemeral=True)

class ConfirmDeleteView(discord.ui.View):
    def __init__(self, card_id: int):
        super().__init__(timeout=30)
        self.card_id = card_id

    @discord.ui.button(label="Yes, delete", style=discord.ButtonStyle.danger)
    async def yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        with get_session() as db:
            c = db.get(Card, self.card_id)
            if not c:
                await interaction.response.edit_message(content="Already gone.", view=None)
                return
            db.delete(c)
        await interaction.response.edit_message(content="Deleted.", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)

class EditCardModal(discord.ui.Modal, title="Edit Card"):
    question = discord.ui.TextInput(label="Question", style=discord.TextStyle.paragraph, required=True, max_length=2000)
    answer = discord.ui.TextInput(label="Answer", style=discord.TextStyle.paragraph, required=True, max_length=2000)

    def __init__(self, card_id: int, question: str, answer: str):
        super().__init__()
        self.card_id = card_id
        self.question.default = question
        self.answer.default = answer

    async def on_submit(self, interaction: discord.Interaction) -> None:
        with get_session() as db:
            c = db.get(Card, self.card_id)
            if not c:
                await interaction.response.send_message("Card no longer exists.", ephemeral=True)
                return
            c.question = str(self.question)
            c.answer = str(self.answer)
        await interaction.response.send_message("Saved.", ephemeral=True)

# -------------
# Slash commands
# -------------

@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)
    try:
        init_db()
    except Exception as e:
        log.exception("DB init failed: %s", e)
    # Sync commands on startup
    try:
        await _sync_commands_for_guild()
    except Exception as e:
        log.warning("Slash sync failed: %s", e)

@tree.command(description="Create a category")
@app_commands.describe(name="Category name")
async def addcategory(interaction: discord.Interaction, name: str):
    name = name.strip()
    with get_session() as db:
        exists = db.query(Category).filter(Category.name.ilike(name)).first()
        if exists:
            await interaction.response.send_message("Category already exists.", ephemeral=True)
            return
        c = Category(name=name)
        db.add(c)
    await interaction.response.send_message(f"Added category **{name}**.", ephemeral=True)

@tree.command(description="Create a subcategory in a category")
@app_commands.describe(category="Pick a category", name="Subcategory name")
@app_commands.autocomplete(category=_ac_categories)
async def addsubcategory(interaction: discord.Interaction, category: str, name: str):
    cat_id = _find_category_id_by_name(category)
    if not cat_id:
        await interaction.response.send_message("Category not found.", ephemeral=True)
        return
    name = name.strip()
    with get_session() as db:
        exists = (
            db.query(Subcategory)
            .filter(Subcategory.category_id == cat_id)
            .filter(Subcategory.name.ilike(name))
            .first()
        )
        if exists:
            await interaction.response.send_message("Subcategory already exists.", ephemeral=True)
            return
        s = Subcategory(name=name, category_id=cat_id)
        db.add(s)
    await interaction.response.send_message(f"Added subcategory **{name}** in **{category}**.", ephemeral=True)

@tree.command(description="Add a flash card")
@app_commands.describe(
    question="Question/definition (visible)",
    answer="Answer (spoilered)",
    category="Optional category",
    subcategory="Optional subcategory (if category chosen)",
)
@app_commands.autocomplete(category=_ac_categories, subcategory=_ac_subcategories)
async def addcard(
    interaction: discord.Interaction,
    question: str,
    answer: str,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
):
    cat_id = _find_category_id_by_name(category) if category else None
    sub_id = _find_subcategory_id_by_name(cat_id, subcategory) if (cat_id and subcategory) else None

    with get_session() as db:
        # auto-generate a unique card_number
        last = db.query(Card.card_number).order_by(Card.card_number.desc()).first()
        next_num = (last[0] + 1) if last and last[0] is not None else 1
        c = Card(
            card_number=next_num,
            question=question.strip(),
            answer=answer.strip(),
            category_id=cat_id,
            subcategory_id=sub_id,
        )
        db.add(c)
        # read values before leaving session
        card_number = next_num

    location_bits = []
    if category:
        location_bits.append(category)
    if subcategory:
        location_bits.append(subcategory)
    location = " › ".join(location_bits) if location_bits else "No category"

    await interaction.response.send_message(
        f"Added card **#{card_number}** to **{location}**.", ephemeral=True
    )

@tree.command(description="List cards in a category/subcategory (paged)")
@app_commands.describe(category="Optional category", subcategory="Optional subcategory")
@app_commands.autocomplete(category=_ac_categories, subcategory=_ac_subcategories)
async def listcards(
    interaction: discord.Interaction,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
):
    cat_id = _find_category_id_by_name(category) if category else None
    sub_id = _find_subcategory_id_by_name(cat_id, subcategory) if (cat_id and subcategory) else None

    with get_session() as db:
        q = db.query(Card.id, Card.card_number, Card.question)
        if cat_id:
            q = q.filter(Card.category_id == cat_id)
        if sub_id:
            q = q.filter(Card.subcategory_id == sub_id)
        rows = q.order_by(Card.question.asc()).all()

    if not rows:
        await interaction.response.send_message("No cards found.", ephemeral=True)
        return

    # Paged list with 10 per page, each row has a "Show" button
    pages: List[List[Tuple[int, int, str]]] = []
    chunk = []
    for rid, num, qtext in rows:
        chunk.append((rid, num, qtext))
        if len(chunk) == 10:
            pages.append(chunk)
            chunk = []
    if chunk:
        pages.append(chunk)

    title = "Cards"
    if category:
        title += f" – {category}"
        if subcategory:
            title += f" › {subcategory}"

    view = CardListView(title=title, pages=pages, current=0)
    await interaction.response.send_message(embed=view.make_embed(), view=view, ephemeral=True)

class CardListView(discord.ui.View):
    def __init__(self, title: str, pages: List[List[Tuple[int, int, str]]], current: int = 0):
        super().__init__(timeout=180)
        self.title = title
        self.pages = pages
        self.current = current
        # Build dynamic buttons for the page
        self._refresh_buttons()

    def _refresh_buttons(self):
        self.clear_items()
        # Add card buttons
        for rid, num, qtext in self.pages[self.current]:
            label = f"#{num} · {qtext[:60]}"
            self.add_item(ShowCardButton(card_id=rid, label=label))
        # Nav row
        self.add_item(PrevPageButton())
        self.add_item(NextPageButton())

    def make_embed(self) -> discord.Embed:
        e = discord.Embed(title=self.title, description=f"Page {self.current+1}/{len(self.pages)}")
        for rid, num, qtext in self.pages[self.current]:
            e.add_field(name=f"#{num}", value=qtext[:200], inline=False)
        return e

class ShowCardButton(discord.ui.Button):
    def __init__(self, card_id: int, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary, custom_id=f"show:{card_id}")
        self.card_id = card_id

    async def callback(self, interaction: discord.Interaction):
        await _send_card_embed(interaction.followup, self.card_id, include_buttons=True)
        await interaction.response.defer()  # acknowledge button

class PrevPageButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="◀ Prev", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: CardListView = self.view  # type: ignore
        if view.current > 0:
            view.current -= 1
            view._refresh_buttons()
            await interaction.response.edit_message(embed=view.make_embed(), view=view)
        else:
            await interaction.response.defer()

class NextPageButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Next ▶", style=discord.ButtonStyle.secondary)

    async def callback(self, interaction: discord.Interaction):
        view: CardListView = self.view  # type: ignore
        if view.current < len(view.pages) - 1:
            view.current += 1
            view._refresh_buttons()
            await interaction.response.edit_message(embed=view.make_embed(), view=view)
        else:
            await interaction.response.defer()

# REVIEWCARDS

class ReviewCorrectButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="✅ Correct", style=discord.ButtonStyle.success, custom_id="rev:ok")

    async def callback(self, interaction: discord.Interaction):
        key = (interaction.channel_id, interaction.user.id)
        state = active_reviews.get(key)
        if not state or not state.message_id:
            await interaction.response.defer()
            return

        # Identify current card as the last message we sent — we don't strictly store it, but we can
        # resolve by reading the last embed? Simpler: store current as last used in state.used
        # We'll track current by setting on message send. For simplicity, pick last added in used.
        if not state.used:
            await interaction.response.defer()
            return
        current_id = next(iter(state.used.__reversed__())) if hasattr(state.used, "__reversed__") else list(state.used)[-1]
        state.answered.add(current_id)
        await interaction.response.defer()
        await _post_next_card_interaction(interaction, state)

class ReviewWrongButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="❌ Incorrect", style=discord.ButtonStyle.danger, custom_id="rev:nok")

    async def callback(self, interaction: discord.Interaction):
        key = (interaction.channel_id, interaction.user.id)
        state = active_reviews.get(key)
        if not state:
            await interaction.response.defer()
            return
        # incorrect: do nothing except move on
        await interaction.response.defer()
        await _post_next_card_interaction(interaction, state)

class ReviewView(discord.ui.View):
    def __init__(self, session_key: Tuple[int, int]):
        super().__init__(timeout=None)
        self.session_key = session_key
        self.add_item(ReviewCorrectButton())
        self.add_item(ReviewWrongButton())

@tree.command(description="Review cards (20, 50, or all) optionally by category/subcategory")
@app_commands.describe(
    mode="Pick a review length",
    category="Optional category to scope",
    subcategory="Optional subcategory (needs category)"
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
    await interaction.response.defer(thinking=True)

    cat_id = _find_category_id_by_name(category) if category else None
    sub_id = _find_subcategory_id_by_name(cat_id, subcategory) if (cat_id and subcategory) else None

    ids = _query_cards(cat_id, sub_id)
    if not ids:
        await interaction.followup.send("No cards found for that scope.")
        return

    pool = _build_pool(ids, mode.value)
    if not pool:
        await interaction.followup.send("No cards available for the chosen mode/scope.")
        return

    # Create/replace state
    key = (interaction.channel_id, interaction.user.id)
    state = ReviewState(
        user_id=interaction.user.id,
        mode=mode.value,
        category_id=cat_id,
        subcategory_id=sub_id,
        pool=pool,
    )
    active_reviews[key] = state

    # Post initial controls message with buttons (for review session)
    view = ReviewView(session_key=key)
    await interaction.followup.send("Review started. Use the buttons to grade each card.", view=view)

    # And post the first card
    await _post_next_card_interaction(interaction, state)

# Edit & delete slash commands (simple direct operations)

@tree.command(description="Delete a card by its number")
@app_commands.describe(card_number="The card number to delete")
async def deletecard(interaction: discord.Interaction, card_number: int):
    with get_session() as db:
        c = db.query(Card).filter(Card.card_number == card_number).first()
        if not c:
            await interaction.response.send_message("Not found.", ephemeral=True)
            return
        db.delete(c)
    await interaction.response.send_message(f"Deleted card #{card_number}.", ephemeral=True)

@tree.command(description="Edit a card by its number")
@app_commands.describe(card_number="Card number", question="New question (optional)", answer="New answer (optional)")
async def editcard(
    interaction: discord.Interaction,
    card_number: int,
    question: Optional[str] = None,
    answer: Optional[str] = None,
):
    with get_session() as db:
        c = db.query(Card).filter(Card.card_number == card_number).first()
        if not c:
            await interaction.response.send_message("Not found.", ephemeral=True)
            return
        if question:
            c.question = question
        if answer:
            c.answer = answer
    await interaction.response.send_message(f"Updated card #{card_number}.", ephemeral=True)

# Manual command sync if ever needed
@tree.command(description="Sync slash commands (admin)")
async def sync(interaction: discord.Interaction):
    await _sync_commands_for_guild()
    await interaction.response.send_message("Synced.", ephemeral=True)

# -------------
# Entrypoint
# -------------

def main():
    if not TOKEN:
        log.error("DISCORD_TOKEN is not set")
        raise SystemExit(1)
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
