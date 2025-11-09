from __future__ import annotations
import os
import asyncio
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple

import discord
from discord import app_commands
from discord.ext import commands

from .db import SessionLocal, init_db
from .models import Category, Card, ReviewStat, SessionScore, Streak
from .utils import (
    get_or_create_category,
    generate_unique_card_number,
    weighted_choice,
    mark_daily_activity,
    EASTERN,
)

# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# -----------------------------------------------------------------------------
# Env
# -----------------------------------------------------------------------------
def _env_int(name: str, default: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default

TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID_RAW = os.environ.get("GUILD_ID")
STREAK_CHANNEL_ID = _env_int("STREAK_CHANNEL_ID", 0)

# -----------------------------------------------------------------------------
# Discord client
# -----------------------------------------------------------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.reactions = True
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)

STREAK_UP_LINES = [
    "Streak stays hot. Bring the heat today.",
    "Consistency is a superpower. Keep going.",
    "Another brick on the wall. 👷‍♀️",
    "Briefs before breakfast. Your future self approves.",
]
STREAK_RESET_LINES = [
    "Fresh docket. Start a new streak today.",
    "Yesterday’s gone—today’s your opening statement.",
    "Objection overruled: we resume!",
]

# -----------------------------------------------------------------------------
# Review state: message_id -> {user_id, card_id, category_id}
# -----------------------------------------------------------------------------
active_reviews: Dict[int, dict] = {}

# tiny de-dupe to avoid double-fires
_last_handle: Dict[Tuple[int, int], float] = {}  # (message_id, user_id) -> ts

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _fetch_category_names(prefix: str = "", limit: int = 25):
    with SessionLocal() as db:
        q = db.query(Category)
        if prefix:
            q = q.filter(Category.name.ilike(f"{prefix.strip()}%"))
        return [c.name for c in q.order_by(Category.name.asc()).limit(limit).all()]

def _embed(catname: str, card: Card, points: int, streak_val: int) -> discord.Embed:
    e = discord.Embed(
        title=f"Review: {catname}",
        description=f"**Q:** {card.question}\n**A:** ||{card.answer}||",
        color=discord.Color.green(),
    )
    e.set_footer(text=f"Points: {points} — React with ✅ or ❌ — Streak: {streak_val} day(s)")
    return e

def _pick_next_card(db, user_id: int, category_id: int) -> Card:
    cards = db.query(Card).filter(Card.category_id == category_id).all()
    stats = db.query(ReviewStat).filter(
        ReviewStat.user_id == str(user_id),
        ReviewStat.card_id.in_([c.id for c in cards])
    ).all()
    stats_by_id = {s.card_id: s for s in stats}
    return weighted_choice(cards, stats_by_id)

async def _remove_user_reaction(message: discord.Message, emoji: str, user: discord.abc.User | discord.Member):
    # Best-effort: this works without 'Manage Messages' on many servers; if not, we just ignore.
    try:
        await message.remove_reaction(emoji, user)
    except Exception:
        pass

# -----------------------------------------------------------------------------
# Events
# -----------------------------------------------------------------------------
@bot.event
async def on_ready():
    init_db()
    try:
        if GUILD_ID_RAW:
            try:
                gid = int(str(GUILD_ID_RAW).strip())
                guild = discord.Object(id=gid)
                try:
                    bot.tree.copy_global_to(guild=guild)
                except Exception:
                    pass
                await bot.tree.sync(guild=guild)
                logging.info("Slash commands synced to guild %s", gid)
            except ValueError:
                await bot.tree.sync()
                logging.warning("GUILD_ID not numeric; synced globally instead.")
        else:
            await bot.tree.sync()
            logging.info("Global slash commands synced")
    except Exception:
        logging.exception("Failed to sync commands")
    logging.info("Logged in as %s", bot.user)

    if not getattr(bot, "_recap_started", False):
        bot._recap_started = True
        asyncio.create_task(daily_streak_recap_loop())

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    try:
        # ignore bot's own reactions
        if payload.user_id == bot.user.id:
            return

        state = active_reviews.get(payload.message_id)
        if not state:
            return
        if state["user_id"] != payload.user_id:
            # Only the session owner can answer
            return

        emoji = str(payload.emoji)
        if emoji not in ("✅", "❌"):
            return

        # simple de-dupe (same user/message within ~0.8s)
        now = time.time()
        key = (payload.message_id, payload.user_id)
        if _last_handle.get(key, 0) + 0.8 > now:
            return
        _last_handle[key] = now

        channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
        try:
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            active_reviews.pop(payload.message_id, None)
            return

        correct = (emoji == "✅")
        user_id = payload.user_id
        category_id = state["category_id"]
        card_id = state["card_id"]

        with SessionLocal() as db:
            card = db.query(Card).filter(Card.id == card_id).one_or_none()
            if not card:
                # session invalid
                active_reviews.pop(message.id, None)
                try:
                    await message.edit(content="This review session expired.", embed=None)
                except Exception:
                    pass
                return

            # score row
            score = db.query(SessionScore).filter(
                SessionScore.user_id == str(user_id),
                SessionScore.category_id == category_id
            ).one_or_none()
            if not score:
                score = SessionScore(user_id=str(user_id), category_id=category_id, points=0)
                db.add(score)

            # stat row
            stat = db.query(ReviewStat).filter(
                ReviewStat.user_id == str(user_id),
                ReviewStat.card_id == card_id
            ).one_or_none()
            if not stat:
                stat = ReviewStat(user_id=str(user_id), card_id=card_id)
                db.add(stat)

            delta = 5 if correct else -5
            if correct:
                stat.rights += 1
            else:
                stat.wrongs += 1

            stat.last_reviewed_at = datetime.utcnow()
            score.points += delta
            streak = mark_daily_activity(db, user_id)
            db.commit()

            # win condition
            if score.points >= 100:
                catname = card.category.name if card.category else "Review"
                try:
                    await message.edit(
                        content=f"🎉 <@{user_id}> finished **{catname}** with 100 points! (Streak: {streak.current_streak}🔥)",
                        embed=None
                    )
                except Exception:
                    pass
                score.points = 0
                db.commit()
                active_reviews.pop(message.id, None)
                return

            # next card (edit same message)
            next_card = _pick_next_card(db, user_id, category_id)
            catname = next_card.category.name if next_card.category else "Cards"
            e = _embed(catname, next_card, score.points, streak.current_streak)

            # Update state
            active_reviews[message.id] = {
                "user_id": user_id,
                "card_id": next_card.id,
                "category_id": category_id,
            }

        # Try to remove the user's reaction so they can react again on the same message
        user = payload.member or bot.get_user(payload.user_id) or await bot.fetch_user(payload.user_id)
        await _remove_user_reaction(message, emoji, user)

        try:
            await message.edit(embed=e)
        except Exception:
            logging.exception("Failed to edit message to next card")

    except Exception:
        logging.exception("raw_reaction handler error")

# -----------------------------------------------------------------------------
# Slash Commands (no buttons anywhere)
# -----------------------------------------------------------------------------
@bot.tree.command(description="Add a new category")
@app_commands.describe(name="Category name, e.g. Criminal Law")
async def addcategory(interaction: discord.Interaction, name: str):
    with SessionLocal() as db:
        existing = db.query(Category).filter(Category.name.ilike(name)).one_or_none()
        if existing:
            await interaction.response.send_message(
                f"Category **{existing.name}** already exists.", ephemeral=True
            )
            return
        cat = Category(name=name.strip())
        db.add(cat)
        db.commit()
        await interaction.response.send_message(f"Created category **{cat.name}**.", ephemeral=True)

@bot.tree.command(description="Add a flashcard (auto-generates unique card number)")
@app_commands.describe(
    question="Question or definition",
    answer="Answer text",
    category="Optional category name",
)
async def addcard(
    interaction: discord.Interaction,
    question: str,
    answer: str,
    category: Optional[str] = None,
):
    with SessionLocal() as db:
        cat = get_or_create_category(db, category)
        auto_number = generate_unique_card_number(db, cat.name if cat else None)
        card = Card(
            card_number=auto_number,
            question=question.strip(),
            answer=answer.strip(),
            category=cat,
        )
        db.add(card)
        db.commit()
        await interaction.response.send_message(
            f"Added card **{card.card_number}** in "
            f"**{card.category.name if card.category else 'No Category'}**.",
            ephemeral=True,
        )

@addcard.autocomplete("category")
async def addcard_category_autocomplete(interaction, current: str):
    names = _fetch_category_names(current)
    return [app_commands.Choice(name=n, value=n) for n in names]

@bot.tree.command(description="List cards for a category")
async def listcards(interaction: discord.Interaction, category: str):
    with SessionLocal() as db:
        cat = db.query(Category).filter(Category.name.ilike(category.strip())).one_or_none()
        if not cat:
            await interaction.response.send_message("Category not found.", ephemeral=True)
            return
        cards = db.query(Card).filter(Card.category_id == cat.id).order_by(Card.question.asc()).all()
        if not cards:
            await interaction.response.send_message("No cards in that category.", ephemeral=True)
            return
        lines = [f"• **{c.card_number}** — {c.question}" for c in cards[:200]]
        await interaction.response.send_message(f"**{cat.name}** — {len(cards)} card(s):\n" + "\n".join(lines), ephemeral=True)

@bot.tree.command(description="Start a reaction-based review session (react with ✅ or ❌)")
async def reviewcards(interaction: discord.Interaction, category: str):
    user_id = interaction.user.id
    with SessionLocal() as db:
        cat = db.query(Category).filter(Category.name.ilike(category.strip())).one_or_none()
        if not cat:
            await interaction.response.send_message("Category not found.", ephemeral=True)
            return
        cards_exist = db.query(Card).filter(Card.category_id == cat.id).count() > 0
        if not cards_exist:
            await interaction.response.send_message("No cards in that category.", ephemeral=True)
            return

        # ensure a score row exists
        score = db.query(SessionScore).filter(
            SessionScore.user_id == str(user_id),
            SessionScore.category_id == cat.id
        ).one_or_none()
        if not score:
            score = SessionScore(user_id=str(user_id), category_id=cat.id, points=0)
            db.add(score)
            db.commit()
            db.refresh(score)

        first_card = _pick_next_card(db, user_id, cat.id)
        streak = db.query(Streak).filter(Streak.user_id == str(user_id)).one_or_none()
        streak_val = streak.current_streak if streak else 0

        embed = _embed(cat.name, first_card, score.points, streak_val)

    await interaction.response.send_message(
        "Review started. React to the card with **✅** for correct or **❌** for incorrect.",
        ephemeral=True,
    )
    sent = await interaction.channel.send(embed=embed)

    # IMPORTANT: do NOT pre-add reactions (per your request)
    active_reviews[sent.id] = {"user_id": user_id, "card_id": first_card.id, "category_id": cat.id}

# -----------------------------------------------------------------------------
# Streak + Recap Loop
# -----------------------------------------------------------------------------
@bot.tree.command(description="Show your current and longest streak")
async def streak(interaction: discord.Interaction):
    with SessionLocal() as db:
        s = db.query(Streak).filter(Streak.user_id == str(interaction.user.id)).one_or_none()
        if not s:
            await interaction.response.send_message("No streak yet. Start reviewing!", ephemeral=True)
            return
        await interaction.response.send_message(
            f"🔥 **Current:** {s.current_streak} days\n🏆 **Longest:** {s.longest_streak} days",
            ephemeral=True,
        )

@bot.tree.command(description="Show the longest streak leaderboard")
async def streakboard(interaction: discord.Interaction):
    with SessionLocal() as db:
        top = db.query(Streak).order_by(Streak.longest_streak.desc()).limit(10).all()
        if not top:
            await interaction.response.send_message("No streaks yet.", ephemeral=True)
            return
        lines = [
            f"**{i+1}.** <@{s.user_id}> — {s.longest_streak}d (current {s.current_streak})"
            for i, s in enumerate(top)
        ]
        await interaction.response.send_message("\n".join(lines))

async def sleep_until_next_3am_eastern():
    now_et = datetime.now(EASTERN)
    target = now_et.replace(hour=3, minute=0, second=0, microsecond=0)
    if now_et >= target:
        target += timedelta(days=1)
    await asyncio.sleep((target - now_et).total_seconds())

async def daily_streak_recap_loop():
    await bot.wait_until_ready()
    if STREAK_CHANNEL_ID <= 0:
        logging.info("No STREAK_CHANNEL_ID configured; skipping recap loop.")
        return
    while not bot.is_closed():
        await sleep_until_next_3am_eastern()
        try:
            channel = bot.get_channel(STREAK_CHANNEL_ID)
            if not channel:
                logging.warning("STREAK_CHANNEL_ID not found.")
                continue
            with SessionLocal() as db:
                today = datetime.now(EASTERN).date()
                yesterday = (today - timedelta(days=1)).isoformat()
                actives = db.query(Streak).filter(Streak.last_active_date == yesterday).all()
                resets = db.query(Streak).filter(Streak.last_active_date != yesterday).all()
                lines = []
                if actives:
                    lines.append(
                        "**🔥 Still rolling:** " +
                        ", ".join([f"<@{s.user_id}> ({s.current_streak}d)" for s in actives])
                    )
                    lines.append(random.choice(STREAK_UP_LINES))
                if resets:
                    lines.append(
                        "\n**↩️ Needs a new start:** " +
                        ", ".join([f"<@{s.user_id}>" for s in resets])
                    )
                    lines.append(random.choice(STREAK_RESET_LINES))
                if not lines:
                    lines = ["No streak data yet. Start with /reviewcards ✨"]
                await channel.send("\n".join(lines))
        except Exception:
            logging.exception("Error during streak recap loop")
            continue

# -----------------------------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------------------------
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
