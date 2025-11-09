from __future__ import annotations
import os
import asyncio
import logging
import random
from datetime import datetime
from typing import Optional, Dict, List

import discord
from discord import app_commands
from discord.ext import commands

from .db import SessionLocal, init_db
from .models import Category, Card, ReviewStat, SessionScore, Streak
from .utils import (
    get_or_create_category,
    weighted_choice,
    mark_daily_activity,
    EASTERN,
)

logging.basicConfig(level=logging.INFO)

TOKEN = os.environ.get("DISCORD_TOKEN")
GUILD_ID = os.environ.get("GUILD_ID")  # optional: limit to one server
STREAK_CHANNEL_ID = int(os.environ.get("STREAK_CHANNEL_ID", "0"))

intents = discord.Intents.default()
intents.message_content = False
intents.reactions = True

bot = commands.Bot(command_prefix="!", intents=intents)

# Rotating streak recap messages
STREAK_UP_LINES: List[str] = [
    "Streak stays hot. Bring the heat today.",
    "Consistency is a superpower. Keep going.",
    "Another brick on the wall. 👷‍♀️",
    "Briefs before breakfast. Your future self approves.",
]
STREAK_RESET_LINES: List[str] = [
    "Fresh docket. Start a new streak today.",
    "Yesterday’s gone—today’s your opening statement.",
    "Objection overruled: we resume!",
]

active_reviews: Dict[int, dict] = {}


@bot.event
async def on_ready():
    init_db()
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            await bot.tree.sync(guild=guild)
            logging.info("Slash commands synced to guild %s", GUILD_ID)
        else:
            await bot.tree.sync()
            logging.info("Global slash commands synced")
    except Exception as e:
        logging.exception("Failed to sync commands: %s", e)
    logging.info("Logged in as %s", bot.user)


# ------------------------------------------------------------
# Slash Commands
# ------------------------------------------------------------

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
        await interaction.response.send_message(
            f"Created category **{cat.name}**.", ephemeral=True
        )


@bot.tree.command(description="Add a flashcard")
@app_commands.describe(
    card_number="Unique ID you choose (e.g. CRIM-001)",
    question="The prompt/question/definition",
    answer="The answer text",
    category="Optional category name (must exist, or will be created)",
)
async def addcard(
    interaction: discord.Interaction,
    card_number: str,
    question: str,
    answer: str,
    category: Optional[str] = None,
):
    with SessionLocal() as db:
        if db.query(Card).filter(Card.card_number == card_number).first():
            await interaction.response.send_message(
                f"A card with number `{card_number}` already exists.", ephemeral=True
            )
            return
        cat = get_or_create_category(db, category)
        card = Card(
            card_number=card_number.strip(),
            question=question.strip(),
            answer=answer.strip(),
            category=cat,
        )
        db.add(card)
        db.commit()
        await interaction.response.send_message(
            f"Added card **{card.card_number}** in category **{card.category.name if card.category else 'None'}**.",
            ephemeral=True,
        )


@bot.tree.command(description="Edit a flashcard by its unique number")
@app_commands.describe(
    card_number="Unique card number to edit",
    question="New question (optional)",
    answer="New answer (optional)",
    category="New category (optional)",
)
async def editcard(
    interaction: discord.Interaction,
    card_number: str,
    question: Optional[str] = None,
    answer: Optional[str] = None,
    category: Optional[str] = None,
):
    with SessionLocal() as db:
        card = db.query(Card).filter(Card.card_number == card_number.strip()).one_or_none()
        if not card:
            await interaction.response.send_message("Card not found.", ephemeral=True)
            return
        if question:
            card.question = question.strip()
        if answer:
            card.answer = answer.strip()
        if category is not None:
            cat = get_or_create_category(db, category)
            card.category = cat
        db.commit()
        await interaction.response.send_message(
            f"Updated card **{card.card_number}**.", ephemeral=True
        )


@bot.tree.command(description="Delete a flashcard by its unique number")
@app_commands.describe(card_number="Unique card number to delete")
async def deletecard(interaction: discord.Interaction, card_number: str):
    with SessionLocal() as db:
        card = db.query(Card).filter(Card.card_number == card_number.strip()).one_or_none()
        if not card:
            await interaction.response.send_message("Card not found.", ephemeral=True)
            return
        db.delete(card)
        db.commit()
        await interaction.response.send_message(f"Deleted card **{card_number}**.", ephemeral=True)


class CardButton(discord.ui.Button):
    def __init__(self, label: str, card_number: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary)
        self.card_number = card_number

    async def callback(self, interaction: discord.Interaction):
        with SessionLocal() as db:
            card = db.query(Card).filter(Card.card_number == self.card_number).one_or_none()
            if not card:
                await interaction.response.send_message("That card no longer exists.", ephemeral=True)
                return
            embed = discord.Embed(
                title=f"Card {card.card_number}",
                description=f"**Q:** {card.question}\n**A:** ||{card.answer}||",
                color=discord.Color.blurple(),
            )
            if card.category:
                embed.set_footer(text=f"Category: {card.category.name}")
            await interaction.response.send_message(embed=embed, ephemeral=True)


class ListView(discord.ui.View):
    def __init__(self, cards: list[Card]):
        super().__init__(timeout=60)
        for c in cards[:25]:
            self.add_item(CardButton(label=c.question[:80], card_number=c.card_number))


@bot.tree.command(description="List cards for a category (alphabetical by question)")
@app_commands.describe(category="Category name to list")
async def listcards(interaction: discord.Interaction, category: str):
    with SessionLocal() as db:
        cat = db.query(Category).filter(Category.name.ilike(category.strip())).one_or_none()
        if not cat:
            await interaction.response.send_message("Category not found.", ephemeral=True)
            return
        cards = db.query(Card).filter(Card.category_id == cat.id).order_by(Card.question.asc()).all()
        if not cards:
            await interaction.response.send_message("No cards in that category yet.", ephemeral=True)
            return
        view = ListView(cards)
        await interaction.response.send_message(
            f"**{cat.name}** — {len(cards)} card(s). Click a question to open:",
            view=view,
            ephemeral=True,
        )


@bot.tree.command(description="Review cards from a category with weighted randomness")
@app_commands.describe(category="Category to review (required)")
async def reviewcards(interaction: discord.Interaction, category: str):
    user_id = interaction.user.id
    with SessionLocal() as db:
        cat = db.query(Category).filter(Category.name.ilike(category.strip())).one_or_none()
        if not cat:
            await interaction.response.send_message("Category not found.", ephemeral=True)
            return
        cards = db.query(Card).filter(Card.category_id == cat.id).all()
        if not cards:
            await interaction.response.send_message("No cards in that category yet.", ephemeral=True)
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
        embed.set_footer(text=f"Points: {score.points} — React ✅ if right, ❌ if wrong — Streak: {streak_val} day(s)")
        await interaction.response.send_message(embed=embed)
        sent = await interaction.original_response()
        try:
            await sent.add_reaction("✅")
            await sent.add_reaction("❌")
        except discord.Forbidden:
            pass
        active_reviews[sent.id] = {"user_id": user_id, "card_id": card.id, "category_id": cat.id}


@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User | discord.Member):
    if user.bot:
        return
    payload = active_reviews.get(reaction.message.id)
    if not payload or payload["user_id"] != user.id:
        return

    emoji = str(reaction.emoji)
    with SessionLocal() as db:
        card = db.query(Card).filter(Card.id == payload["card_id"]).one_or_none()
        if not card:
            return

        stat = db.query(ReviewStat).filter(
            ReviewStat.user_id == str(user.id), ReviewStat.card_id == card.id
        ).one_or_none()
        if not stat:
            stat = ReviewStat(user_id=str(user.id), card_id=card.id)
            db.add(stat)

        score = db.query(SessionScore).filter(
            SessionScore.user_id == str(user.id),
            SessionScore.category_id == payload["category_id"]
        ).one_or_none()
        if not score:
            score = SessionScore(user_id=str(user.id), category_id=payload["category_id"], points=0)
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
        streak = mark_daily_activity(db, user.id)
        db.commit()

        try:
            embed = reaction.message.embeds[0]
            embed.set_footer(
                text=f"Points: {score.points} — React ✅ if right, ❌ if wrong — Streak: {streak.current_streak} day(s)"
            )
            await reaction.message.edit(embed=embed)
        except Exception:
            pass

        if score.points >= 100:
            await reaction.message.channel.send(
                f"🎉 <@{user.id}> finished **{card.category.name if card.category else 'Review'}** "
                f"with 100 points! (Streak: {streak.current_streak}🔥)"
            )
            score.points = 0
            db.commit()
            active_reviews.pop(reaction.message.id, None)
            return

        cards = db.query(Card).filter(Card.category_id == payload["category_id"]).all()
        stats = db.query(ReviewStat).filter(
            ReviewStat.user_id == str(user.id),
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
        new_msg = await reaction.message.channel.send(embed=embed)
        try:
            await new_msg.add_reaction("✅")
            await new_msg.add_reaction("❌")
        except discord.Forbidden:
            pass
        active_reviews[new_msg.id] = {
            "user_id": user.id,
            "card_id": next_card.id,
            "category_id": payload["category_id"],
        }
        active_reviews.pop(reaction.message.id, None)


# ------------------------------------------------------------
# Streak commands and recap loop
# ------------------------------------------------------------

@bot.tree.command(description="Show your review streak and longest streak")
async def streak(interaction: discord.Interaction):
    with SessionLocal() as db:
        s = db.query(Streak).filter(Streak.user_id == str(interaction.user.id)).one_or_none()
        if not s:
            await interaction.response.send_message(
                "No streak yet. Start a /reviewcards to begin!", ephemeral=True
            )
            return
        await interaction.response.send_message(
            f"🔥 **Current streak:** {s.current_streak} day(s)\n🏆 **Longest streak:** {s.longest_streak} day(s)",
            ephemeral=True,
        )


@bot.tree.command(description="Show the leaderboard for longest streaks")
async def streakboard(interaction: discord.Interaction):
    with SessionLocal() as db:
        top = db.query(Streak).order_by(Streak.longest_streak.desc()).limit(10).all()
        if not top:
            await interaction.response.send_message("No streaks yet.", ephemeral=True)
            return
        lines = [
            f"**{i+1}.** <@{s.user_id}> — {s.longest_streak} day(s) (current {s.current_streak})"
            for i, s in enumerate(top)
        ]
        await interaction.response.send_message("\n".join(lines))


async def sleep_until_next_3am_eastern():
    now_et = datetime.now(EASTERN)
    target = now_et.replace(hour=3, minute=0, second=0, microsecond=0)
    if now_et >= target:
        from datetime import timedelta
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
            if channel is None:
                logging.warning("STREAK_CHANNEL_ID not found in cache.")
                continue
            with SessionLocal() as db:
                from datetime import timedelta
                today = datetime.now(EASTERN).date()
                yesterday = (today - timedelta(days=1)).isoformat()
                actives = db.query(Streak).filter(Streak.last_active_date == yesterday).all()
                resets = db.query(Streak).filter(Streak.last_active_date != yesterday).all()
                lines: list[str] = []
                if actives:
                    lines.append(
                        "**🔥 Still rolling:** "
                        + ", ".join([f"<@{s.user_id}> ({s.current_streak}d)" for s in actives])
                    )
                    lines.append(random.choice(STREAK_UP_LINES))
                if resets:
                    lines.append(
                        "\n**↩️ Needs a new start:** "
                        + ", ".join([f"<@{s.user_id}>" for s in resets])
                    )
                    lines.append(random.choice(STREAK_RESET_LINES))
                if not lines:
                    lines = ["No streak data yet. Start with /reviewcards ✨"]
                await channel.send("\n".join(lines))
        except Exception:
            logging.exception("Error during streak recap loop")
            continue


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN env var is required")
    bot.loop.create_task(daily_streak_recap_loop())
    bot.run(TOKEN)
