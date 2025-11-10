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

        card_ids = [c.id for c in cards]
        pool, target = _prepare_pool(card_ids, mode)

    key = _session_key(user_id, cat_id, sub_id)
    REVIEW_SESSIONS[key] = {"pool": pool, "target": target, "completed": 0, "last": None}

    await interaction.response.send_message(
        f"Review started: **mode {mode}** — {target} unique card(s) to complete.",
        ephemeral=True
    )

    await _post_next_card(
        channel=interaction.channel,
        user_id=user_id,
        category_id=cat_id,
        subcategory_id=sub_id,
    )

@reviewcards.autocomplete("category")
async def ac_review_category(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=n, value=n) for n in _category_names(current)]

@reviewcards.autocomplete("subcategory")
async def ac_review_subcategory(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=n, value=n.split(' ▸ ')[-1]) for n in _subcategory_names(current)]

# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not TOKEN:
        logging.error("DISCORD_TOKEN is missing (set it in Fly secrets).")
        time.sleep(60)
        raise SystemExit(1)
    try:
        bot.run(TOKEN)
    except Exception:
        logging.exception("Fatal error during startup")
        time.sleep(60)
        raise
