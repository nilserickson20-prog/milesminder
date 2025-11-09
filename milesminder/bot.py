from __future__ import annotations
import os
import asyncio
import logging
import random
import time
import pathlib
from datetime import datetime, timedelta
from typing import Optional, Dict, Tuple, List

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
# Review state
# -----------------------------------------------------------------------------
# message_id -> {user_id, card_id, category_id}
active_reviews: Dict[int, dict] = {}
# tiny de-dupe for reactions
_last_handle: Dict[Tuple[int, int], float] = {}
# Track cards shown in the *current* review session (per user+category)
# Key = (user_id, category_id) -> set(card_id)
REVIEW_SESSION_SEEN: Dict[Tuple[int, int], set] = {}

# -----------------------------------------------------------------------------
# Reward video helper
# -----------------------------------------------------------------------------
def _choose_reward_video() -> Optional[str]:
    """
    Returns either:
      - a URL (string) if REWARD_VIDEO_URLS env is set, or
      - a local file path (string) if assets are present, else None.
    """
    # Option A: URLs in env var
    urls_csv = os.environ.get("REWARD_VIDEO_URLS", "").strip()
    if urls_csv:
        urls = [u.strip() for u in urls_csv.split(",") if u.strip()]
        if urls:
            return random.choice(urls)

    # Option B: local files baked into image
    candidates: List[str] = []
    for pat in ("/app/assets/rewards/*.mp4", "/app/assets/rewards/*.mov", "/app/assets/rewards/*.webm"):
        candidates.extend([str(p) for p in pathlib.Path("/").glob(pat.lstrip("/"))])
    if candidates:
        return random.choice(candidates)

    return None

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _fetch_category_names(prefix: str = "", limit: int = 25):
    with SessionLocal() as db:
        q = db.query(Category)
        if prefix:
            q = q.filter(Category.name.ilike(f"{prefix.strip()}%"))
        return [c.name for c in q.order_by(Category.name.asc()).limit(limit).all()]

def _embed_review(catname: str, card: Card, points: int, streak_val: int) -> discord.Embed:
    e = discord.Embed(
        title=f"Review: {catname}",
        description=f"**Q:** {card.question}\n**A:** ||{card.answer}||",
        color=discord.Color.green(),
    )
    e.set_footer(text=f"Points: {points} — React ✅/❌ or use buttons — Streak: {streak_val} day(s)")
    return e

def _embed_card_display(catname: str, card: Card) -> discord.Embed:
    return discord.Embed(
        title=f"{catname} — Card",
        description=f"**Q:** {card.question}\n**A:** ||{card.answer}||",
        color=discord.Color.blurple(),
    )

def _pick_next_card(db, user_id: int, category_id: int, exclude_ids: Optional[set] = None) -> Optional[Card]:
    """Choose next card, preferring ones you got wrong more often, while excluding `exclude_ids` if provided.
       If all cards are excluded, we fall back to the full set (allow repeats after a full pass)."""
    exclude_ids = exclude_ids or set()
    all_cards = db.query(Card).filter(Card.category_id == category_id).all()
    if not all_cards:
        return None

    candidates = [c for c in all_cards if c.id not in exclude_ids]
    if not candidates:
        candidates = all_cards  # allow repeats after all seen once

    stats = db.query(ReviewStat).filter(
        ReviewStat.user_id == str(user_id),
        ReviewStat.card_id.in_([c.id for c in candidates])
    ).all()
    stats_by_id = {s.card_id: s for s in stats}
    return weighted_choice(candidates, stats_by_id)

async def _post_next_card(channel: discord.abc.Messageable, user_id: int, category_id: int, points: int, streak_val: int):
    with SessionLocal() as db:
        seen = REVIEW_SESSION_SEEN.get((user_id, category_id), set())
        next_card = _pick_next_card(db, user_id, category_id, exclude_ids=seen)
        if next_card is None:
            await channel.send("No cards available in this category.")
            return None, None
        catname = next_card.category.name if next_card.category else "Cards"
        embed = _embed_review(catname, next_card, points, streak_val)

    # record as seen
    REVIEW_SESSION_SEEN.setdefault((user_id, category_id), set()).add(next_card.id)

    view = ReviewView(user_id=user_id, category_id=category_id, card_id=next_card.id)
    new_msg = await channel.send(embed=embed, view=view)
    active_reviews[new_msg.id] = {"user_id": user_id, "card_id": next_card.id, "category_id": category_id}
    return new_msg, next_card

async def _score_and_advance(
    *,
    channel: discord.abc.Messageable,
    old_message: Optional[discord.Message],
    user_id: int,
    category_id: int,
    card_id: int,
    correct: bool,
):
    with SessionLocal() as db:
        card = db.query(Card).filter(Card.id == card_id).one_or_none()
        if not card:
            if old_message:
                try:
                    await old_message.edit(content="This review session expired.", embed=None, view=None)
                except Exception:
                    pass
            return

        score = db.query(SessionScore).filter(
            SessionScore.user_id == str(user_id),
            SessionScore.category_id == category_id
        ).one_or_none()
        if not score:
            score = SessionScore(user_id=str(user_id), category_id=category_id, points=0)
            db.add(score)

        stat = db.query(ReviewStat).filter(
            ReviewStat.user_id == str(user_id),
            ReviewStat.card_id == card_id
        ).one_or_none()
        if not stat:
            stat = ReviewStat(user_id=str(user_id), card_id=card_id, rights=0, wrongs=0)
            db.add(stat)
        else:
            stat.rights = stat.rights or 0
            stat.wrongs = stat.wrongs or 0

        delta = 5 if correct else -5
        if correct:
            stat.rights += 1
        else:
            stat.wrongs += 1

        stat.last_reviewed_at = datetime.utcnow()
        score.points += delta
        streak = mark_daily_activity(db, user_id)
        db.commit()

        if score.points >= 100:
            catname = card.category.name if card.category else "Review"

            # 1) Play reward video if available
            reward = _choose_reward_video()
            if reward:
                try:
                    if reward.lower().startswith(("http://", "https://")):
                        await channel.send(reward)
                    else:
                        await channel.send(file=discord.File(reward))
                except Exception:
                    logging.exception("Failed to send reward video")

            # 2) Show streak text
            await channel.send(
                f"🎉 <@{user_id}> finished **{catname}** with 100 points!\n"
                f"🔥 **Streak:** {streak.current_streak} day(s)"
            )
            score.points = 0
            db.commit()
            if old_message:
                try:
                    await old_message.edit(view=None)
                except Exception:
                    pass
            # Reset the session-seen set at the end of a 100-point run
            REVIEW_SESSION_SEEN.pop((user_id, category_id), None)
            return

        new_msg, _ = await _post_next_card(
            channel=channel,
            user_id=user_id,
            category_id=category_id,
            points=score.points,
            streak_val=streak.current_streak,
        )

    if old_message:
        try:
            await old_message.edit(view=None)
        except Exception:
            pass

# -----------------------------------------------------------------------------
# Views: Review buttons
# -----------------------------------------------------------------------------
class ReviewView(discord.ui.View):
    def __init__(self, user_id: int, category_id: int, card_id: int):
        super().__init__(timeout=1200)
        self.user_id = user_id
        self.category_id = category_id
        self.card_id = card_id

    async def _handle(self, interaction: discord.Interaction, correct: bool):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn’t your review session.", ephemeral=True)
            return
        try:
            await interaction.response.defer()
        except discord.InteractionResponded:
            pass

        await _score_and_advance(
            channel=interaction.channel,
            old_message=interaction.message,
            user_id=self.user_id,
            category_id=self.category_id,
            card_id=self.card_id,
            correct=correct,
        )

    @discord.ui.button(label="✅ Correct", style=discord.ButtonStyle.success)
    async def btn_correct(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, True)

    @discord.ui.button(label="❌ Incorrect", style=discord.ButtonStyle.danger)
    async def btn_incorrect(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle(interaction, False)

# -----------------------------------------------------------------------------
# Card Management (Edit / Delete) for plain display
# -----------------------------------------------------------------------------
class EditCardModal(discord.ui.Modal, title="Edit Card"):
    def __init__(self, opener_user_id: int, card_id: int, original_message: discord.Message, category_name: str):
        super().__init__(timeout=300)
        self.opener_user_id = opener_user_id
        self.card_id = card_id
        self.original_message = original_message
        self.category_name = category_name

        with SessionLocal() as db:
            c = db.query(Card).filter(Card.id == self.card_id).one_or_none()
            q_val = c.question if c else ""
            a_val = c.answer if c else ""

        self.q = discord.ui.TextInput(label="Question", style=discord.TextStyle.paragraph, default=q_val, required=True, max_length=2000)
        self.a = discord.ui.TextInput(label="Answer", style=discord.TextStyle.paragraph, default=a_val, required=True, max_length=2000)
        self.add_item(self.q)
        self.add_item(self.a)

    async def on_submit(self, interaction: discord.Interaction):
        if interaction.user.id != self.opener_user_id:
            await interaction.response.send_message("You didn’t open this card.", ephemeral=True)
            return

        new_q = str(self.q.value).strip()
        new_a = str(self.a.value).strip()
        with SessionLocal() as db:
            c = db.query(Card).filter(Card.id == self.card_id).one_or_none()
            if not c:
                await interaction.response.send_message("This card no longer exists.", ephemeral=True)
                return
            c.question = new_q
            c.answer = new_a
            db.commit()
            db.refresh(c)
            catname = c.category.name if c.category else self.category_name

        try:
            await self.original_message.edit(embed=_embed_card_display(catname, c), view=CardManageView(self.opener_user_id, self.card_id, catname))
        except Exception:
            pass
        await interaction.response.send_message("Saved changes.", ephemeral=True)

class ConfirmDeleteView(discord.ui.View):
    def __init__(self, opener_user_id: int, card_id: int, category_name: str):
        super().__init__(timeout=60)
        self.opener_user_id = opener_user_id
        self.card_id = card_id
        self.category_name = category_name

    @discord.ui.button(label="Confirm Delete", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opener_user_id:
            await interaction.response.send_message("You didn’t open this card.", ephemeral=True)
            return
        with SessionLocal() as db:
            c = db.query(Card).filter(Card.id == self.card_id).one_or_none()
            if not c:
                await interaction.response.send_message("Already deleted.", ephemeral=True)
                return
            db.delete(c)
            db.commit()
        await interaction.message.edit(content="🗑️ Card deleted.", embed=None, view=None)
        await interaction.response.send_message("Deleted.", ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        with SessionLocal() as db:
            c = db.query(Card).filter(Card.id == self.card_id).one_or_none()
            if not c:
                await interaction.message.edit(content="This card no longer exists.", embed=None, view=None)
                await interaction.response.send_message("Card not found.", ephemeral=True)
                return
            catname = c.category.name if c.category else self.category_name
            await interaction.message.edit(embed=_embed_card_display(catname, c), view=CardManageView(self.opener_user_id, self.card_id, catname))
        await interaction.response.defer()

class CardManageView(discord.ui.View):
    """Shown on plain (non-review) card displays."""
    def __init__(self, opener_user_id: int, card_id: int, category_name: str):
        super().__init__(timeout=900)
        self.opener_user_id = opener_user_id
        self.card_id = card_id
        self.category_name = category_name

    @discord.ui.button(label="✏️ Edit", style=discord.ButtonStyle.primary)
    async def edit_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opener_user_id:
            await interaction.response.send_message("You didn’t open this card.", ephemeral=True)
            return
        modal = EditCardModal(self.opener_user_id, self.card_id, interaction.message, self.category_name)
        await interaction.response.send_modal(modal)

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opener_user_id:
            await interaction.response.send_message("You didn’t open this card.", ephemeral=True)
            return
        await interaction.response.edit_message(view=ConfirmDeleteView(self.opener_user_id, self.card_id, self.category_name))

# -----------------------------------------------------------------------------
# ListCards Buttons Paginator (no dropdowns; scalable)
# -----------------------------------------------------------------------------
def _chunk(lst: List, size: int) -> List[List]:
    return [lst[i:i+size] for i in range(0, len(lst), size)]

class ListCardsButtonsView(discord.ui.View):
    """
    Ephemeral paginator that shows up to 10 question-buttons per page.
    Clicking a question posts that single card (plain display) to the channel, with Edit/Delete buttons.
    """
    PAGE_SIZE = 10  # leave room for nav buttons

    def __init__(self, user_id: int, category_id: int, questions: List[tuple[int, str]], category_name: str):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.category_id = category_id
        self.category_name = category_name
        self.pages: List[List[tuple[int, str]]] = _chunk(questions, self.PAGE_SIZE) or [[]]
        self.page_index = 0
        self._rebuild()

    def _rebuild(self):
        for item in list(self.children):
            self.remove_item(item)

        current = self.pages[self.page_index]
        for cid, q in current:
            label = (q[:72] + "…") if len(q) > 75 else q
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
            async def _cb(interaction: discord.Interaction, card_id=cid):
                if interaction.user.id != self.user_id:
                    await interaction.response.send_message("This list belongs to someone else.", ephemeral=True)
                    return
                with SessionLocal() as db:
                    card = db.query(Card).filter(Card.id == card_id).one_or_none()
                    if not card:
                        await interaction.response.send_message("That card was not found.", ephemeral=True)
                        return
                    catname = card.category.name if card.category else self.category_name
                    embed = _embed_card_display(catname, card)
                view = CardManageView(self.user_id, card_id, catname)
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
            page_label = discord.ui.Button(label=f"Page {self.page_index+1}/{len(self.pages)}", style=discord.ButtonStyle.secondary, disabled=True)

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
        if payload.user_id == bot.user.id:
            return

        state = active_reviews.get(payload.message_id)
        if not state:
            return
        if state["user_id"] != payload.user_id:
            return

        emoji = str(payload.emoji)
        if emoji not in ("✅", "❌"):
            return

        now = time.time()
        key = (payload.message_id, payload.user_id)
        if _last_handle.get(key, 0) + 0.8 > now:
            return
        _last_handle[key] = now

        channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
        correct = (emoji == "✅")
        user_id = payload.user_id
        category_id = state["category_id"]
        card_id = state["card_id"]

        old_message = None
        try:
            ch = channel
            if isinstance(ch, (discord.TextChannel, discord.Thread)):
                old_message = await ch.fetch_message(payload.message_id)
        except Exception:
            pass

        await _score_and_advance(
            channel=channel,
            old_message=old_message,
            user_id=user_id,
            category_id=category_id,
            card_id=card_id,
            correct=correct,
        )
        active_reviews.pop(payload.message_id, None)

    except Exception:
        logging.exception("raw_reaction handler error")

# -----------------------------------------------------------------------------
# Slash Commands
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
        cat_name = cat.name if cat else "No Category"
        auto_number = generate_unique_card_number(db, cat_name if cat else None)
        card = Card(card_number=auto_number, question=question.strip(), answer=answer.strip(), category=cat)
        db.add(card)
        db.commit()
        db.refresh(card)

    await interaction.response.send_message(
        f"Added card **{auto_number}** in **{cat_name}**.",
        ephemeral=True,
    )

# ---- AUTOCOMPLETE: addcard(category)
@addcard.autocomplete("category")
async def addcard_category_autocomplete(interaction: discord.Interaction, current: str):
    names = _fetch_category_names(current)
    return [app_commands.Choice(name=n, value=n) for n in names]

@bot.tree.command(description="List cards for a category (alphabetical; click a button to open a card)")
@app_commands.describe(category="Category to list")
async def listcards(interaction: discord.Interaction, category: str):
    """
    Posts a public, alphabetised list (no IDs).
    Provides an ephemeral buttons paginator; clicking a name posts that card (plain display) with Edit/Delete.
    """
    user_id = interaction.user.id
    with SessionLocal() as db:
        cat = db.query(Category).filter(Category.name.ilike(category.strip())).one_or_none()
        if not cat:
            await interaction.response.send_message("Category not found.", ephemeral=True)
            return
        cards = db.query(Card).filter(Card.category_id == cat.id).order_by(Card.question.asc()).all()
        if not cards:
            await interaction.response.send_message("No cards in that category.", ephemeral=True)
            return
        qpairs = [(c.id, c.question) for c in cards]
        cat_name = cat.name

    lines = [f"{i+1}. {q}" for i, (_, q) in enumerate(qpairs)]
    text = f"**{cat_name}** — {len(qpairs)} card(s):\n" + "\n".join(lines)
    if len(text) > 1900:
        text = text[:1800] + "\n… (truncated; use the pager to open any card)"
    await interaction.response.send_message(text)

    view = ListCardsButtonsView(user_id=user_id, category_id=cat.id, questions=qpairs, category_name=cat_name)
    await interaction.followup.send("Open a card by pressing its button:", view=view, ephemeral=True)

# ---- AUTOCOMPLETE: listcards(category)
@listcards.autocomplete("category")
async def listcards_category_autocomplete(interaction: discord.Interaction, current: str):
    names = _fetch_category_names(current)
    return [app_commands.Choice(name=n, value=n) for n in names]

@bot.tree.command(description="Start a review session (buttons + optional reactions)")
@app_commands.describe(category="Category to review")
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

        score = db.query(SessionScore).filter(
            SessionScore.user_id == str(user_id),
            SessionScore.category_id == cat.id
        ).one_or_none()
        if not score:
            score = SessionScore(user_id=str(user_id), category_id=cat.id, points=0)
            db.add(score)
            db.commit()
            db.refresh(score)

        # Start a fresh “seen this session” set
        REVIEW_SESSION_SEEN[(user_id, cat.id)] = set()

        first_card = _pick_next_card(db, user_id, cat.id, exclude_ids=REVIEW_SESSION_SEEN[(user_id, cat.id)])
        if first_card is None:
            await interaction.response.send_message("No cards available to review in that category.", ephemeral=True)
            return
        REVIEW_SESSION_SEEN[(user_id, cat.id)].add(first_card.id)

        streak = db.query(Streak).filter(Streak.user_id == str(user_id)).one_or_none()
        streak_val = streak.current_streak if streak else 0
        embed = _embed_review(cat.name, first_card, score.points, streak_val)

    await interaction.response.send_message(
        "Review started. Use the buttons **or** react with ✅/❌.",
        ephemeral=True,
    )
    view = ReviewView(user_id=user_id, category_id=cat.id, card_id=first_card.id)
    sent = await interaction.channel.send(embed=embed, view=view)

    active_reviews[sent.id] = {"user_id": user_id, "card_id": first_card.id, "category_id": cat.id}

# ---- AUTOCOMPLETE: reviewcards(category)
@reviewcards.autocomplete("category")
async def reviewcards_category_autocomplete(interaction: discord.Interaction, current: str):
    names = _fetch_category_names(current)
    return [app_commands.Choice(name=n, value=n) for n in names]

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
    now_et = datetime.now(EASTERN) if EASTERN else datetime.utcnow()
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
                today = (datetime.now(EASTERN) if EASTERN else datetime.utcnow()).date()
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
