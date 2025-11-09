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

from .db import SessionLocal, init_db
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

active_reviews: Dict[int, dict] = {}

# -----------------------------------------------------------
# on_ready
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

# -----------------------------------------------------------
# Category autocomplete helper
# -----------------------------------------------------------
def _fetch_category_names(prefix: str = "", limit: int = 25):
    with SessionLocal() as db:
        q = db.query(Category)
        if prefix:
            q = q.filter(Category.name.ilike(f"{prefix.strip()}%"))
        return [c.name for c in q.order_by(Category.name.asc()).limit(limit).all()]

# -----------------------------------------------------------
# Slash Commands
# -----------------------------------------------------------
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

@bot.tree.command(description="Review cards from a category")
async def reviewcards(interaction: discord.Interaction, category: str):
    user_id = interaction.user.id
    with SessionLocal() as db:
        cat = db.query(Category).filter(Category.name.ilike(category.strip())).one_or_none()
        if not cat:
            await interaction.response.send_message("Category not found.", ephemeral=True)
            return
        cards = db.query(Card).filter(Card.category_id == cat.id).all()
        if not cards:
            await interaction.response.send_message("No cards in that category.", ephemeral=True)
            return

        stats = db.query(ReviewStat).filter(
            ReviewStat.user_id == str(user_id),
            ReviewStat.card_id.in_([c.id for c in cards])
        ).all()
        stats_by_id = {s.card_id: s for s in stats}

        score = db.query(SessionScore).filter(
            SessionScore.user_id == str(user_id),
            SessionScore.category_id == cat.id
        ).one_or_none()
        if not score:
            score = SessionScore(user_id=str(user_id), category_id=cat.id, points=0)
            db.add(score)
            db.commit()
            db.refresh(score)

        card = weighted_choice(cards, stats_by_id)
        streak = db.query(Streak).filter(Streak.user_id == str(user_id)).one_or_none()
        streak_val = streak.current_streak if streak else 0

        embed = discord.Embed(
            title=f"Review: {cat.name}",
            description=f"**Q:** {card.question}\n**A:** ||{card.answer}||",
            color=discord.Color.green(),
        )
        embed.set_footer(
            text=f"Points: {score.points} — React ✅ if right, ❌ if wrong — Streak: {streak_val} day(s)"
        )
        await interaction.response.send_message(embed=embed, ephemeral=False)
        sent = await interaction.original_response()
        try:
            await sent.add_reaction("✅")
            await sent.add_reaction("❌")
        except discord.Forbidden:
            pass
        active_reviews[sent.id] = {"user_id": user_id, "card_id": card.id, "category_id": cat.id}

@reviewcards.autocomplete("category")
async def reviewcards_category_autocomplete(interaction, current: str):
    names = _fetch_category_names(current)
    return [app_commands.Choice(name=n, value=n) for n in names]

# -----------------------------------------------------------
# Reaction handler (robust)
# -----------------------------------------------------------
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    if payload.user_id == bot.user.id:
        return

    review_state = active_reviews.get(payload.message_id)
    if not review_state or review_state["user_id"] != payload.user_id:
        return

    guild = bot.get_guild(payload.guild_id) if payload.guild_id else None
    channel = bot.get_channel(payload.channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(payload.channel_id)
        except Exception:
            return

    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        active_reviews.pop(payload.message_id, None)
        return
    except Exception:
        return

    emoji = str(payload.emoji)

    with SessionLocal() as db:
        card = db.query(Card).filter(Card.id == review_state["card_id"]).one_or_none()
        if not card:
            active_reviews.pop(payload.message_id, None)
            return

        stat = db.query(ReviewStat).filter(
            ReviewStat.user_id == str(payload.user_id),
            ReviewStat.card_id == card.id
        ).one_or_none()
        if not stat:
            stat = ReviewStat(user_id=str(payload.user_id), card_id=card.id)
            db.add(stat)

        score = db.query(SessionScore).filter(
            SessionScore.user_id == str(payload.user_id),
            SessionScore.category_id == review_state["category_id"]
        ).one_or_none()
        if not score:
            score = SessionScore(user_id=str(payload.user_id),
                                  category_id=review_state["category_id"],
                                  points=0)
            db.add(score)

        delta = 0
        if emoji == "✅":
            stat.rights += 1
            delta = 5
        elif emoji == "❌":
            stat.wrongs += 1
            delta = -5
        else:
            return

        stat.last_reviewed_at = datetime.utcnow()
        score.points += delta
        streak = mark_daily_activity(db, payload.user_id)
        db.commit()

        try:
            if message.embeds:
                embed = message.embeds[0]
                embed.set_footer(
                    text=f"Points: {score.points} — React ✅ if right, ❌ if wrong — Streak: {streak.current_streak} day(s)"
                )
                await message.edit(embed=embed)
        except Exception:
            pass

        if score.points >= 100:
            cat = card.category.name if card.category else "Review"
            await channel.send(
                f"🎉 <@{payload.user_id}> finished **{cat}** with 100 points! "
                f"(Streak: {streak.current_streak}🔥)"
            )
            score.points = 0
            db.commit()
            active_reviews.pop(payload.message_id, None)
            return

        cards = db.query(Card).filter(Card.category_id == review_state["category_id"]).all()
        stats = db.query(ReviewStat).filter(
            ReviewStat.user_id == str(payload.user_id),
            ReviewStat.card_id.in_([c.id for c in cards])
        ).all()
        stats_by_id = {s.card_id: s for s in stats}
        next_card = weighted_choice(cards, stats_by_id)

        embed = discord.Embed(
            title=f"Review: {card.category.name if card.category else 'Cards'}",
            description=f"**Q:** {next_card.question}\n**A:** ||{next_card.answer}||",
            color=discord.Color.green(),
        )
        embed.set_footer(
            text=f"Points: {score.points} — React ✅ if right, ❌ if wrong — Streak: {streak.current_streak} day(s)"
        )

        new_msg = await channel.send(embed=embed)
        try:
            await new_msg.add_reaction("✅")
            await new_msg.add_reaction("❌")
        except discord.Forbidden:
            pass

        active_reviews[new_msg.id] = {
            "user_id": payload.user_id,
            "card_id": next_card.id,
            "category_id": review_state["category_id"],
        }
        active_reviews.pop(payload.message_id, None)
        try:
            await message.delete()
        except Exception:
            pass

# -----------------------------------------------------------
# Streak commands + recap loop
# -----------------------------------------------------------
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
