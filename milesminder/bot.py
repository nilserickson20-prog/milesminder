from __future__ import annotations
import os
import asyncio
import logging
import random
import time
from datetime import datetime, timedelta
from typing import Optional, Dict

import discord
from discord import app_commands
from discord.ext import commands

from .db import SessionLocal, init_db, SessionLocal as DBSession
from .models import Category, Card, ReviewStat, SessionScore, Streak
from .utils import (
    get_or_create_category,
    generate_unique_card_number,
    weighted_choice,
    mark_daily_activity,
    EASTERN,
)

# -----------------------------------------------------------
# Logging
# -----------------------------------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# -----------------------------------------------------------
# Environment
# -----------------------------------------------------------
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

# -----------------------------------------------------------
# Discord client
# -----------------------------------------------------------
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

# message_id -> {"user_id": int, "card_id": int, "category_id": int}
active_reviews: Dict[int, dict] = {}

# -----------------------------------------------------------
# Helpers
# -----------------------------------------------------------
def _fetch_category_names(prefix: str = "", limit: int = 25):
    with DBSession() as db:
        q = db.query(Category)
        if prefix:
            q = q.filter(Category.name.ilike(f"{prefix.strip()}%"))
        return [c.name for c in q.order_by(Category.name.asc()).limit(limit).all()]

def _build_embed(catname: str, card: Card, points: int, streak_val: int) -> discord.Embed:
    embed = discord.Embed(
        title=f"Review: {catname}",
        description=f"**Q:** {card.question}\n**A:** ||{card.answer}||",
        color=discord.Color.green(),
    )
    embed.set_footer(
        text=f"Points: {points} — Click Correct/Incorrect or react ✅/❌ — Streak: {streak_val} day(s)"
    )
    return embed

async def _send_next_card(channel: discord.abc.Messageable, user_id: int, category_id: int):
    """Picks a weighted random next card, sends a NEW message with buttons+reactions, returns (message, card)."""
    with DBSession() as db:
        cat = db.query(Category).filter(Category.id == category_id).one_or_none()
        if not cat:
            return None, None

        cards = db.query(Card).filter(Card.category_id == category_id).all()
        stats = db.query(ReviewStat).filter(
            ReviewStat.user_id == str(user_id),
            ReviewStat.card_id.in_([c.id for c in cards])
        ).all()
        stats_by_id = {s.card_id: s for s in stats}
        next_card = weighted_choice(cards, stats_by_id)

        score = db.query(SessionScore).filter(
            SessionScore.user_id == str(user_id),
            SessionScore.category_id == category_id
        ).one_or_none()
        if not score:
            score = SessionScore(user_id=str(user_id), category_id=category_id, points=0)
            db.add(score)
            db.commit()
            db.refresh(score)

        s = db.query(Streak).filter(Streak.user_id == str(user_id)).one_or_none()
        streak_val = s.current_streak if s else 0

        embed = _build_embed(cat.name, next_card, score.points, streak_val)

    view = ReviewView(user_id=user_id, category_id=category_id, card_id=next_card.id)
    msg = await channel.send(embed=embed, view=view)
    try:
        await msg.add_reaction("✅")
        await msg.add_reaction("❌")
    except discord.Forbidden:
        pass

    active_reviews[msg.id] = {"user_id": user_id, "card_id": next_card.id, "category_id": category_id}
    return msg, next_card

async def _apply_answer_and_advance(
    *,
    message: discord.Message,
    user_id: int,
    category_id: int,
    card_id: int,
    correct: bool,
):
    """Scores the answer, checks end condition, then sends a NEW message with the next card; deletes old one."""
    with DBSession() as db:
        card = db.query(Card).filter(Card.id == card_id).one_or_none()
        if not card:
            # Old message becomes invalid
            try:
                await message.edit(content="This review session expired.", embed=None, view=None)
            except Exception:
                pass
            active_reviews.pop(message.id, None)
            return

        stat = db.query(ReviewStat).filter(
            ReviewStat.user_id == str(user_id),
            ReviewStat.card_id == card.id
        ).one_or_none()
        if not stat:
            stat = ReviewStat(user_id=str(user_id), card_id=card.id)
            db.add(stat)

        score = db.query(SessionScore).filter(
            SessionScore.user_id == str(user_id),
            SessionScore.category_id == category_id
        ).one_or_none()
        if not score:
            score = SessionScore(user_id=str(user_id), category_id=category_id, points=0)
            db.add(score)

        delta = 5 if correct else -5
        if correct:
            stat.rights += 1
        else:
            stat.wrongs += 1

        stat.last_reviewed_at = datetime.utcnow()
        score.points += delta
        streak = mark_daily_activity(db, user_id)
        db.commit()

        # Win condition
        if score.points >= 100:
            catname = card.category.name if card.category else "Review"
            try:
                await message.edit(
                    content=f"🎉 <@{user_id}> finished **{catname}** with 100 points! (Streak: {streak.current_streak}🔥)",
                    embed=None,
                    view=None,
                )
            except Exception:
                pass
            score.points = 0
            db.commit()
            active_reviews.pop(message.id, None)
            return

    # Otherwise, post a NEW message with the next card and clean up the old one
    channel = message.channel
    new_msg, _ = await _send_next_card(channel, user_id, category_id)

    # Replace mapping (new message is now the active one)
    if new_msg:
        active_reviews.pop(message.id, None)

    # Tidy up old message
    try:
        await message.delete()
    except Exception:
        # If we can't delete, at least clear its view
        try:
            await message.edit(view=None)
        except Exception:
            pass

# -----------------------------------------------------------
# UI: Buttons View (stateless across messages; state lives in active_reviews)
# -----------------------------------------------------------
class ReviewView(discord.ui.View):
    def __init__(self, user_id: int, category_id: int, card_id: int):
        super().__init__(timeout=1200)  # 20 minutes
        self.user_id = user_id
        self.category_id = category_id
        self.card_id = card_id

    async def _go(self, interaction: discord.Interaction, correct: bool):
        # Only the initiating user may answer
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn’t your review session.", ephemeral=True)
            return
        # Defer quickly to avoid 3s timeout
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            pass

        await _apply_answer_and_advance(
            message=interaction.message,
            user_id=self.user_id,
            category_id=self.category_id,
            card_id=self.card_id,
            correct=correct,
        )

    @discord.ui.button(label="✅ Correct", style=discord.ButtonStyle.success)
    async def btn_correct(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, True)

    @discord.ui.button(label="❌ Incorrect", style=discord.ButtonStyle.danger)
    async def btn_incorrect(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._go(interaction, False)

# -----------------------------------------------------------
# Events
# -----------------------------------------------------------
@bot.event
async def on_ready():
    init_db()
    try:
        if GUILD_ID_RAW:
            try:
                guild_id = int(str(GUILD_ID_RAW).strip())
                guild = discord.Object(id=guild_id)
                try:
                    bot.tree.copy_global_to(guild=guild)
                except Exception:
                    pass
                await bot.tree.sync(guild=guild)
                logging.info("Slash commands synced to guild %s", guild_id)
            except ValueError:
                await bot.tree.sync()
                logging.warning("GUILD_ID not numeric; synced globally instead.")
        else:
            await bot.tree.sync()
            logging.info("Global slash commands synced")
    except Exception as e:
        logging.exception("Failed to sync commands: %s", e)
    logging.info("Logged in as %s", bot.user)

    if not getattr(bot, "_recap_started", False):
        bot._recap_started = True
        asyncio.create_task(daily_streak_recap_loop())

@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """Advance via reactions as well; posts a NEW message and removes the old."""
    try:
        if payload.user_id == bot.user.id:
            return

        state = active_reviews.get(payload.message_id)
        if not state or state["user_id"] != payload.user_id:
            return

        emoji = str(payload.emoji)
        correct = True if emoji == "✅" else False if emoji == "❌" else None
        if correct is None:
            return

        # Need the message to delete/edit
        channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
        try:
            message = await channel.fetch_message(payload.message_id)
        except Exception:
            active_reviews.pop(payload.message_id, None)
            return

        await _apply_answer_and_advance(
            message=message,
            user_id=state["user_id"],
            category_id=state["category_id"],
            card_id=state["card_id"],
            correct=correct,
        )
    except Exception:
        logging.exception("raw_reaction handler error")

# -----------------------------------------------------------
# Slash Commands
# -----------------------------------------------------------
@bot.tree.command(description="Add a new category")
@app_commands.describe(name="Category name, e.g. Criminal Law")
async def addcategory(interaction: discord.Interaction, name: str):
    with DBSession() as db:
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
    with DBSession() as db:
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
    with DBSession() as db:
        cat = db.query(Category).filter(Category.name.ilike(category.strip())).one_or_none()
        if not cat:
            await interaction.response.send_message("Category not found.", ephemeral=True)
            return
        cards = db.query(Card).filter(Card.category_id == cat.id).order_by(Card.question.asc()).all()
        if not cards:
            await interaction.response.send_message("No cards in that category.", ephemeral=True)
            return
        lines = [f"• **{c.card_number}** — {c.question}" for c in cards[:50]]
        await interaction.response.send_message(f"**{cat.name}** — {len(cards)} card(s):\n" + "\n".join(lines), ephemeral=True)

@bot.tree.command(description="Review cards from a category")
async def reviewcards(interaction: discord.Interaction, category: str):
    user_id = interaction.user.id
    with DBSession() as db:
        cat = db.query(Category).filter(Category.name.ilike(category.strip())).one_or_none()
        if not cat:
            await interaction.response.send_message("Category not found.", ephemeral=True)
            return

    # Post the FIRST card as a brand-new message (public), with reactions + buttons
    await interaction.response.send_message(f"Starting review for **{category}**…", ephemeral=True)
    channel = interaction.channel
    msg, _ = await _send_next_card(channel, user_id, cat.id)  # type: ignore[arg-type]

@reviewcards.autocomplete("category")
async def reviewcards_category_autocomplete(interaction, current: str):
    names = _fetch_category_names(current)
    return [app_commands.Choice(name=n, value=n) for n in names]

# -----------------------------------------------------------
# Streak commands + recap loop
# -----------------------------------------------------------
@bot.tree.command(description="Show your current and longest streak")
async def streak(interaction: discord.Interaction):
    with DBSession() as db:
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
    with DBSession() as db:
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
            with DBSession() as db:
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

# -----------------------------------------------------------
# Entrypoint
# -----------------------------------------------------------
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
