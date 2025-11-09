
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
from .models import Category, Subcategory, Card, ReviewStat, SessionScore, Streak
from .utils import (
    get_or_create_category,
    get_or_create_subcategory,
    generate_unique_card_number,
    weighted_choice,
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
STREAK_CHANNEL_ID = int(os.environ.get("STREAK_CHANNEL_ID", "0"))  # optional
REWARD_VIDS = os.environ.get("REWARD_VIDEO_URLS", "")  # optional, comma-separated URLs

# ---------------------------------------------------------------------------
# Discord Bot / Intents
# ---------------------------------------------------------------------------
intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.reactions = True
# message_content not required for slash commands / interactions
intents.message_content = False

bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------------------------------------------------------
# In-memory state
# ---------------------------------------------------------------------------
# message_id -> info about the active review message
active_reviews: Dict[int, dict] = {}

# (message_id, user_id) -> last ts handled (debounce for reactions)
_last_handle: Dict[Tuple[int, int], float] = {}

# (user_id, category_id, subcategory_id) -> set(card_id) seen in current session
REVIEW_SESSION_SEEN: Dict[Tuple[int, Optional[int], Optional[int]], set] = {}

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
    for pat in ("/app/assets/rewards/*.mp4", "/app/assets/rewards/*.mov", "/app/assets/rewards/*.webm"):
        candidates.extend([str(p) for p in pathlib.Path("/").glob(pat.lstrip("/"))])
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


def _candidate_cards(db, scope: str, category_id: Optional[int], subcategory_id: Optional[int]) -> List[Card]:
    q = db.query(Card)
    if scope == "all":
        return q.all()
    if scope == "category" and category_id:
        return q.filter(Card.category_id == category_id).all()
    if scope == "subcategory" and subcategory_id:
        return q.filter(Card.subcategory_id == subcategory_id).all()
    return []


def _pick_next_card(
    db,
    user_id: int,
    scope: str,
    category_id: Optional[int],
    subcategory_id: Optional[int],
    exclude_ids: Optional[set] = None
) -> Optional[Card]:
    exclude_ids = exclude_ids or set()
    all_cards = _candidate_cards(db, scope, category_id, subcategory_id)
    if not all_cards:
        return None
    candidates = [c for c in all_cards if c.id not in exclude_ids] or all_cards
    stats = db.query(ReviewStat).filter(
        ReviewStat.user_id == str(user_id),
        ReviewStat.card_id.in_([c.id for c in candidates])
    ).all()
    stats_by_id = {s.card_id: s for s in stats}
    return weighted_choice(candidates, stats_by_id)


def _embed_review(title: str, card: Card, points: int, streak_val: int) -> discord.Embed:
    e = discord.Embed(
        title=title,
        description=f"**Q:** {card.question}\n**A:** ||{card.answer}||",
        color=discord.Color.green(),
    )
    e.set_footer(text=f"Points: {points} — React ✅/❌ or use the buttons — Streak: {streak_val} day(s)")
    return e


def _embed_card_display(title: str, card: Card) -> discord.Embed:
    return discord.Embed(
        title=title,
        description=f"**Q:** {card.question}\n**A:** ||{card.answer}||",
        color=discord.Color.blurple(),
    )


async def _post_next_card(
    channel: discord.abc.MessageableChannel,
    user_id: int,
    scope: str,
    category_id: Optional[int],
    subcategory_id: Optional[int],
    points: int,
    streak_val: int,
):
    with get_session() as db:
        seen = REVIEW_SESSION_SEEN.get((user_id, category_id, subcategory_id), set())
        next_card = _pick_next_card(db, user_id, scope, category_id, subcategory_id, exclude_ids=seen)
        if next_card is None:
            await channel.send("No cards available for this selection.")
            return None, None

        title = "Review: All Categories"
        if scope == "category" and next_card.category:
            title = f"Review: {next_card.category.name}"
        if scope == "subcategory" and next_card.subcategory and next_card.category:
            title = f"Review: {next_card.category.name} ▸ {next_card.subcategory.name}"

        embed = _embed_review(title, next_card, points, streak_val)

    # Remember we've shown this in the current session
    REVIEW_SESSION_SEEN.setdefault((user_id, category_id, subcategory_id), set()).add(next_card.id)

    view = ReviewView(
        user_id=user_id,
        scope=scope,
        category_id=category_id,
        subcategory_id=subcategory_id,
        card_id=next_card.id
    )
    new_msg = await channel.send(embed=embed, view=view)

    active_reviews[new_msg.id] = {
        "user_id": user_id,
        "scope": scope,
        "card_id": next_card.id,
        "category_id": category_id,
        "subcategory_id": subcategory_id,
    }
    return new_msg, next_card


async def _score_and_advance(
    channel: discord.abc.MessageableChannel,
    old_message: Optional[discord.Message],
    user_id: int,
    scope: str,
    category_id: Optional[int],
    subcategory_id: Optional[int],
    card_id: int,
    correct: bool,
):
    with get_session() as db:
        card = db.query(Card).filter(Card.id == card_id).one_or_none()
        if not card:
            if old_message is not None:
                try:
                    await old_message.edit(content="Review expired.", embed=None, view=None)
                except Exception:
                    pass
            return

        # Get/create session score
        score = db.query(SessionScore).filter(
            SessionScore.user_id == str(user_id),
            SessionScore.category_id == (category_id or None),
            SessionScore.subcategory_id == (subcategory_id or None),
        ).one_or_none()
        if not score:
            score = SessionScore(user_id=str(user_id), category_id=category_id, subcategory_id=subcategory_id, points=0)
            db.add(score)

        # Per-card stat
        stat = db.query(ReviewStat).filter(
            ReviewStat.user_id == str(user_id),
            ReviewStat.card_id == card_id
        ).one_or_none()
        if not stat:
            stat = ReviewStat(user_id=str(user_id), card_id=card_id, rights=0, wrongs=0)
            db.add(stat)

        delta = 5 if correct else -5
        stat.rights = (stat.rights or 0) + (1 if correct else 0)
        stat.wrongs = (stat.wrongs or 0) + (0 if correct else 1)
        stat.last_reviewed_at = datetime.utcnow()
        score.points += delta

        # streak
        streak = mark_daily_activity(db, user_id)

        db.commit()

        # Win condition: >= 100 points
        if score.points >= 100:
            reward = _choose_reward_video()
            if reward:
                try:
                    if reward.lower().startswith(("http://", "https://")):
                        await channel.send(reward)
                    else:
                        await channel.send(file=discord.File(reward))
                except Exception:
                    logging.exception("Failed to send reward video")

            scope_label = "All Categories"
            if scope == "category" and card.category:
                scope_label = f"{card.category.name}"
            elif scope == "subcategory" and card.category and card.subcategory:
                scope_label = f"{card.category.name} ▸ {card.subcategory.name}"

            await channel.send(f"🎉 <@{user_id}> finished **{scope_label}** with 100 points!\n🔥 **Streak:** {streak.current_streak} day(s)")
            score.points = 0
            db.commit()

            if old_message is not None:
                try:
                    await old_message.edit(view=None)
                except Exception:
                    pass
            REVIEW_SESSION_SEEN.pop((user_id, category_id, subcategory_id), None)
            return

        # Continue with next card
        new_msg, _ = await _post_next_card(
            channel=channel,
            user_id=user_id,
            scope=scope,
            category_id=category_id,
            subcategory_id=subcategory_id,
            points=score.points,
            streak_val=streak.current_streak,
        )

    if old_message is not None:
        try:
            await old_message.edit(view=None)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# UI Views
# ---------------------------------------------------------------------------
class ReviewView(discord.ui.View):
    def __init__(self, user_id: int, scope: str, category_id: Optional[int], subcategory_id: Optional[int], card_id: int):
        super().__init__(timeout=1200)
        self.user_id = user_id
        self.scope = scope
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
        await _score_and_advance(
            channel=interaction.channel,
            old_message=interaction.message,
            user_id=self.user_id,
            scope=self.scope,
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


class EditCardModal(discord.ui.Modal, title="Edit Card"):
    def __init__(self, opener_user_id: int, card_id: int, original_message: discord.Message, title: str):
        super().__init__(timeout=300)
        self.opener_user_id = opener_user_id
        self.card_id = card_id
        self.original_message = original_message
        self.title_text = title

        with get_session() as db:
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
        with get_session() as db:
            c = db.query(Card).filter(Card.id == self.card_id).one_or_none()
            if not c:
                await interaction.response.send_message("This card no longer exists.", ephemeral=True)
                return
            c.question = new_q
            c.answer = new_a
            db.commit()
            db.refresh(c)
        try:
            await self.original_message.edit(
                embed=_embed_card_display(self.title_text, c),
                view=CardManageView(self.opener_user_id, self.card_id, self.title_text)
            )
        except Exception:
            pass
        await interaction.response.send_message("Saved changes.", ephemeral=True)


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
        with get_session() as db:
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
        await interaction.response.send_modal(EditCardModal(self.opener_user_id, self.card_id, interaction.message, self.title))

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger)
    async def delete_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.opener_user_id:
            await interaction.response.send_message("You didn’t open this card.", ephemeral=True)
            return
        await interaction.response.edit_message(view=ConfirmDeleteView(self.opener_user_id, self.card_id))


# pagination for list
def _chunk(lst: List, size: int) -> List[List]:
    return [lst[i:i + size] for i in range(0, len(lst), size)]


class ListCardsButtonsView(discord.ui.View):
    PAGE_SIZE = 10

    def __init__(self, user_id: int, pairs: List[tuple[int, str]], title: str):
        super().__init__(timeout=900)
        self.user_id = user_id
        self.title = title
        self.pages = _chunk(pairs, self.PAGE_SIZE) or [[]]
        self.page_index = 0
        self._rebuild()

    def _rebuild(self):
        # Clear existing
        for it in list(self.children):
            self.remove_item(it)

        # Card buttons
        for cid, q in self.pages[self.page_index]:
            label = (q[:72] + "…") if len(q) > 75 else q
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)

            async def _cb(interaction: discord.Interaction, card_id=cid):
                if interaction.user.id != self.user_id:
                    await interaction.response.send_message("This list belongs to someone else.", ephemeral=True)
                    return
                with get_session() as db:
                    card = db.query(Card).filter(Card.id == card_id).one_or_none()
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

        # Pagination controls
        if len(self.pages) > 1:
            prev_btn = discord.ui.Button(label="◀ Prev", style=discord.ButtonStyle.secondary)
            next_btn = discord.ui.Button(label="Next ▶", style=discord.ButtonStyle.secondary)
            page_label = discord.ui.Button(label=f"Page {self.page_index + 1}/{len(self.pages)}", style=discord.ButtonStyle.secondary, disabled=True)

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

        state = active_reviews.get(payload.message_id)
        if not state:
            return
        if state.get("user_id") != payload.user_id:
            return

        emoji = str(payload.emoji)
        if emoji not in ("✅", "❌"):
            return

        now = time.time()
        key = (payload.message_id, payload.user_id)
        last = _last_handle.get(key, 0.0)
        if last + 0.8 > now:
            return
        _last_handle[key] = now

        channel = bot.get_channel(payload.channel_id) or await bot.fetch_channel(payload.channel_id)
        old_message = None
        try:
            if isinstance(channel, (discord.TextChannel, discord.Thread)):
                old_message = await channel.fetch_message(payload.message_id)
        except Exception:
            pass

        correct = (emoji == "✅")
        user_id = payload.user_id
        scope = state.get("scope")
        category_id = state.get("category_id")
        subcategory_id = state.get("subcategory_id")
        card_id = state.get("card_id")

        await _score_and_advance(
            channel=channel,
            old_message=old_message,
            user_id=user_id,
            scope=scope,
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
@bot.tree.command(description="Show commands")
async def helpmiles(interaction: discord.Interaction):
    await interaction.response.send_message(
        "**Commands**\n"
        "/addcategory name\n"
        "/editcategory old_name [new_name] [delete]\n"
        "/addsubcategory category subcategory\n"
        "/editsubcategory category old_subcategory [new_subcategory] [delete]\n"
        "/addcard question answer [category] [subcategory]\n"
        "/listcards scope:[all|category|subcategory] [category] [subcategory]\n"
        "/reviewcards scope:[all|category|subcategory] [category] [subcategory]\n"
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
    scope = scope.lower().strip()
    with get_session() as db:
        title = "All Categories"
        if scope == "category":
            cat = db.query(Category).filter(Category.name.ilike((category or '').strip())).one_or_none()
            if not cat:
                await interaction.response.send_message("Category not found.", ephemeral=True)
                return
            title = cat.name
            cards = db.query(Card).filter(Card.category_id == cat.id).order_by(Card.question.asc()).all()
        elif scope == "subcategory":
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
        else:
            cards = db.query(Card).order_by(Card.question.asc()).all()

        if not cards:
            await interaction.response.send_message("No cards found.", ephemeral=True)
            return

        pairs = [(c.id, c.question) for c in cards]

    # Show a textual list (truncated) and a button panel to open any card
    lines = [f"{i + 1}. {q}" for i, (_, q) in enumerate(pairs)]
    text = f"**{title}** — {len(pairs)} card(s):\n" + "\n".join(lines[:50])
    if len(pairs) > 50:
        text += f"\n… ({len(pairs) - 50} more; use buttons to open any card)"
    await interaction.response.send_message(text)
    await interaction.followup.send(
        "Open a card by pressing its button:",
        view=ListCardsButtonsView(interaction.user.id, pairs, title),
        ephemeral=True
    )

@bot.tree.command(description="Review cards (scope: all/category/subcategory)")
async def reviewcards(interaction: discord.Interaction, scope: str, category: Optional[str] = None, subcategory: Optional[str] = None):
    scope = scope.lower().strip()
    user_id = interaction.user.id
    with get_session() as db:
        cat_id = None
        sub_id = None

        if scope == "category":
            cat = db.query(Category).filter(Category.name.ilike((category or '').strip())).one_or_none()
            if not cat:
                await interaction.response.send_message("Category not found.", ephemeral=True)
                return
            cat_id = cat.id
        elif scope == "subcategory":
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
            cat_id = cat.id
            sub_id = sub.id
        elif scope != "all":
            await interaction.response.send_message("Scope must be one of: all, category, subcategory.", ephemeral=True)
            return

        exist = _candidate_cards(db, scope, cat_id, sub_id)
        if not exist:
            await interaction.response.send_message("No cards available for that selection.", ephemeral=True)
            return

        # Ensure a session score row exists
        score = db.query(SessionScore).filter(
            SessionScore.user_id == str(user_id),
            SessionScore.category_id == (cat_id or None),
            SessionScore.subcategory_id == (sub_id or None),
        ).one_or_none()
        if not score:
            score = SessionScore(user_id=str(user_id), category_id=cat_id, subcategory_id=sub_id, points=0)
            db.add(score)
            db.commit()

        # fresh "seen" set for this scope to avoid repeats until pool exhaustion
        REVIEW_SESSION_SEEN[(user_id, cat_id, sub_id)] = set()
        first = _pick_next_card(db, user_id, scope, cat_id, sub_id, exclude_ids=REVIEW_SESSION_SEEN[(user_id, cat_id, sub_id)])
        if not first:
            await interaction.response.send_message("No cards available.", ephemeral=True)
            return
        REVIEW_SESSION_SEEN[(user_id, cat_id, sub_id)].add(first.id)

        streak = db.query(Streak).filter(Streak.user_id == str(user_id)).one_or_none()
        streak_val = streak.current_streak if streak else 0

        title = "Review: All Categories"
        if scope == "category" and first.category:
            title = f"Review: {first.category.name}"
        if scope == "subcategory" and first.subcategory and first.category:
            title = f"Review: {first.category.name} ▸ {first.subcategory.name}"
        embed = _embed_review(title, first, score.points, streak_val)

    await interaction.response.send_message("Review started. Use the buttons or react with ✅/❌.", ephemeral=True)
    view = ReviewView(user_id=user_id, scope=scope, category_id=cat_id, subcategory_id=sub_id, card_id=first.id)
    sent = await interaction.channel.send(embed=embed, view=view)
    active_reviews[sent.id] = {
        "user_id": user_id,
        "scope": scope,
        "card_id": first.id,
        "category_id": cat_id,
        "subcategory_id": sub_id,
    }

# ----------------------------
# Autocomplete handlers
# ----------------------------
@addcategory.autocomplete("name")
async def ac_addcategory_name(interaction: discord.Interaction, current: str):
    # Creating a new name - no suggestions needed
    return []

@listcards.autocomplete("category")
async def ac_listcards_category(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=n, value=n) for n in _category_names(current)]

@listcards.autocomplete("subcategory")
async def ac_listcards_subcategory(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=n, value=n.split(' ▸ ')[-1]) for n in _subcategory_names(current)]

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

