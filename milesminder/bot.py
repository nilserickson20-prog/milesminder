from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import random
import sqlite3
import string
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, List, Tuple

import discord
from discord import app_commands
from discord.ext import commands

# ---------------------------- Logging ---------------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("milesminder")

# ------------------------- Environment --------------------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "").strip()
GUILD_ID_STR = os.getenv("DISCORD_GUILD_ID", "").strip()
GUILD_ID = int(GUILD_ID_STR) if GUILD_ID_STR.isdigit() else None

# Path for sqlite on Fly io volume (/data is mounted writeable)
DB_PATH = Path(os.getenv("DB_PATH", "/data/milesminder.db"))

REWARDS_DIR = Path(os.getenv("REWARDS_DIR", "/app/rewards"))
REWARD_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".gif"}

# ------------------------ Database layer -------------------------------
def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def migrate_schema() -> None:
    conn = get_conn()
    cur = conn.cursor()

    # Core tables
    cur.execute("""
        CREATE TABLE IF NOT EXISTS categories(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS subcategories(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            UNIQUE(category_id, name),
            FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cards(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            card_number TEXT UNIQUE NOT NULL,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            category_id INTEGER,
            subcategory_id INTEGER,
            FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL,
            FOREIGN KEY(subcategory_id) REFERENCES subcategories(id) ON DELETE SET NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users(
            user_id TEXT PRIMARY KEY,
            streak INTEGER NOT NULL DEFAULT 0,
            last_reward_date TEXT
        )
    """)

    # Helpful indices
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_category ON cards(category_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_subcategory ON cards(subcategory_id)")

    conn.commit()
    conn.close()

# Category helpers
def ensure_category(name: str) -> int:
    conn = get_conn(); cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (name.strip(),))
    conn.commit()
    cur.execute("SELECT id FROM categories WHERE name=?", (name.strip(),))
    row = cur.fetchone()
    conn.close()
    return int(row["id"])

def ensure_subcategory(category_id: int, name: str) -> int:
    conn = get_conn(); cur = conn.cursor()
    cur.execute("""
        INSERT OR IGNORE INTO subcategories(category_id, name) VALUES (?, ?)
    """, (category_id, name.strip()))
    conn.commit()
    cur.execute("SELECT id FROM subcategories WHERE category_id=? AND name=?", (category_id, name.strip()))
    row = cur.fetchone()
    conn.close()
    return int(row["id"])

def list_categories(prefix: str = "") -> List[sqlite3.Row]:
    conn = get_conn(); cur = conn.cursor()
    if prefix:
        cur.execute("SELECT * FROM categories WHERE name LIKE ? ORDER BY name LIMIT 25", (f"%{prefix}%",))
    else:
        cur.execute("SELECT * FROM categories ORDER BY name LIMIT 25")
    rows = cur.fetchall()
    conn.close()
    return rows

def list_subcategories(category_id: int, prefix: str = "") -> List[sqlite3.Row]:
    conn = get_conn(); cur = conn.cursor()
    if prefix:
        cur.execute("""
            SELECT * FROM subcategories
            WHERE category_id=? AND name LIKE ?
            ORDER BY name LIMIT 25
        """, (category_id, f"%{prefix}%"))
    else:
        cur.execute("""
            SELECT * FROM subcategories
            WHERE category_id=?
            ORDER BY name LIMIT 25
        """, (category_id,))
    rows = cur.fetchall()
    conn.close()
    return rows

# Card helpers
def generate_card_number() -> str:
    # Short unique token: 8 chars base36 + 4 hex
    a = uuid.uuid4().int % (36**8)
    token = base36(a).rjust(8, "0")
    return f"{token}-{uuid.uuid4().hex[:4]}"

def base36(num: int) -> str:
    chars = string.digits + string.ascii_lowercase
    if num == 0: return "0"
    out = []
    while num:
        num, r = divmod(num, 36)
        out.append(chars[r])
    return "".join(reversed(out))

def add_card(question: str, answer: str, category_id: Optional[int], subcategory_id: Optional[int]) -> str:
    conn = get_conn(); cur = conn.cursor()
    card_number = generate_card_number()
    cur.execute("""
        INSERT INTO cards(card_number, question, answer, category_id, subcategory_id)
        VALUES (?, ?, ?, ?, ?)
    """, (card_number, question.strip(), answer.strip(), category_id, subcategory_id))
    conn.commit()
    conn.close()
    return card_number

def get_candidate_cards(category_id: Optional[int], subcategory_id: Optional[int]) -> List[sqlite3.Row]:
    conn = get_conn(); cur = conn.cursor()
    if category_id and subcategory_id:
        cur.execute("SELECT * FROM cards WHERE category_id=? AND subcategory_id=?",
                    (category_id, subcategory_id))
    elif category_id:
        cur.execute("SELECT * FROM cards WHERE category_id=?", (category_id,))
    else:
        cur.execute("SELECT * FROM cards")
    rows = cur.fetchall()
    conn.close()
    return rows

def get_card(card_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT * FROM cards WHERE id=?", (card_id,))
    row = cur.fetchone()
    conn.close()
    return row

# Users / streak logic
def mark_reward_and_get_streak(user_id: int) -> int:
    today = dt.date.today().isoformat()
    conn = get_conn(); cur = conn.cursor()
    cur.execute("SELECT user_id, streak, last_reward_date FROM users WHERE user_id=?", (str(user_id),))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO users(user_id, streak, last_reward_date) VALUES (?, ?, ?)",
                    (str(user_id), 1, today))
        conn.commit()
        conn.close()
        return 1

    streak = int(row["streak"] or 0)
    last = row["last_reward_date"]
    # Only increment once per day
    if last != today:
        streak += 1
        cur.execute("UPDATE users SET streak=?, last_reward_date=? WHERE user_id=?",
                    (streak, today, str(user_id)))
        conn.commit()
    conn.close()
    return streak

# ------------------------ Discord bot setup ----------------------------
intents = discord.Intents.default()
intents.guilds = True  # slash commands primarily

class MilesBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        # ensure DB migration before command sync
        try:
            migrate_schema()
        except Exception as e:
            log.error(f"Schema migration failed: {e}")
            raise

        pre = [c.name for c in self.tree.get_commands()]
        log.info(f"Pre-sync global commands: {pre}")
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            pre_g = [c.name for c in self.tree.get_commands(guild=guild)]
            log.info(f"Pre-sync guild commands ({GUILD_ID}): {pre_g}")

        # Sync
        if GUILD_ID:
            synced = await self.tree.sync(guild=discord.Object(id=GUILD_ID))
            log.info(f"Synced {len(synced)} commands to guild {GUILD_ID}")
        else:
            synced = await self.tree.sync()
            log.info(f"Synced {len(synced)} global commands.")

bot = MilesBot()

@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user}")

# ---------------------------- Commands ---------------------------------

# /ping test
@bot.tree.command(name="ping", description="Test that the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong", ephemeral=True)

# /addcategory
@bot.tree.command(name="addcategory", description="Create a new category")
@app_commands.describe(name="Category name")
async def addcategory(interaction: discord.Interaction, name: str):
    cid = ensure_category(name)
    await interaction.response.send_message(f"Category **{name}** created (id {cid}).", ephemeral=True)

# /addsubcategory
@bot.tree.command(name="addsubcategory", description="Create a new subcategory")
@app_commands.describe(category="Existing category", name="Subcategory name")
async def addsubcategory(interaction: discord.Interaction, category: str, name: str):
    # resolve category
    rows = list_categories(prefix=category)
    match = next((r for r in rows if r["name"].lower() == category.lower()), None)
    if not match:
        await interaction.response.send_message("Category not found. Use autocomplete.", ephemeral=True)
        return
    sid = ensure_subcategory(match["id"], name)
    await interaction.response.send_message(f"Subcategory **{name}** created under **{match['name']}** (id {sid}).", ephemeral=True)

@addsubcategory.autocomplete("category")
async def ac_category_for_subcat(interaction: discord.Interaction, current: str):
    options = [app_commands.Choice(name=row["name"], value=row["name"]) for row in list_categories(current)]
    return options

# /addcard
@bot.tree.command(name="addcard", description="Add a flashcard (category/subcategory optional)")
@app_commands.describe(
    question="Question text (required)",
    answer="Answer text (required)",
    category="Optional category",
    subcategory="Optional subcategory (requires category)"
)
async def addcard(interaction: discord.Interaction, question: str, answer: str, category: Optional[str] = None, subcategory: Optional[str] = None):
    category_id = None
    subcategory_id = None
    if category:
        rows = list_categories(prefix=category)
        match = next((r for r in rows if r["name"].lower() == category.lower()), None)
        if not match:
            category_id = ensure_category(category)
        else:
            category_id = match["id"]
        if subcategory:
            subcategory_id = ensure_subcategory(category_id, subcategory)

    card_no = add_card(question, answer, category_id, subcategory_id)
    cat_name = None
    if category_id:
        rows = list_categories()
        cat_row = next((r for r in rows if r["id"] == category_id), None)
        cat_name = cat_row["name"] if cat_row else None
    msg = f"Added card **{card_no}**"
    if cat_name:
        msg += f" in **{cat_name}**"
        if subcategory:
            msg += f" › **{subcategory}**"
    await interaction.response.send_message(msg + ".", ephemeral=True)

@addcard.autocomplete("category")
async def ac_category_for_addcard(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in list_categories(current)]

@addcard.autocomplete("subcategory")
async def ac_subcategory_for_addcard(interaction: discord.Interaction, current: str):
    # pull category param from interaction namespace if present
    ns = interaction.namespace if hasattr(interaction, "namespace") else None
    cat = getattr(ns, "category", None) if ns else None
    if not cat:
        return []
    rows = list_categories(prefix=cat)
    match = next((r for r in rows if r["name"].lower() == str(cat).lower()), None)
    if not match:
        return []
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in list_subcategories(match["id"], current)]

# /listcards (simple textual list)
@bot.tree.command(name="listcards", description="List cards by optional category/subcategory")
@app_commands.describe(category="Optional category", subcategory="Optional subcategory")
async def listcards(interaction: discord.Interaction, category: Optional[str] = None, subcategory: Optional[str] = None):
    cat_id = None; sub_id = None
    if category:
        rows = list_categories(prefix=category)
        match = next((r for r in rows if r["name"].lower() == category.lower()), None)
        if not match:
            await interaction.response.send_message("Category not found.", ephemeral=True); return
        cat_id = match["id"]
        if subcategory:
            subs = list_subcategories(cat_id, prefix=subcategory)
            sm = next((r for r in subs if r["name"].lower() == subcategory.lower()), None)
            if not sm:
                await interaction.response.send_message("Subcategory not found.", ephemeral=True); return
            sub_id = sm["id"]

    cards = get_candidate_cards(cat_id, sub_id)
    if not cards:
        await interaction.response.send_message("No cards found.", ephemeral=True); return

    # Sort alphabetically by question
    cards_sorted = sorted(cards, key=lambda r: r["question"].lower())
    # Build a compact list
    lines = []
    for r in cards_sorted[:200]:  # hard cap to avoid huge messages
        lines.append(f"• {r['question']}  _(#{r['card_number']})_")
    text = "\n".join(lines)
    if len(cards_sorted) > 200:
        text += f"\n…and {len(cards_sorted) - 200} more."

    await interaction.response.send_message(text or "No cards.", ephemeral=True)

@listcards.autocomplete("category")
async def ac_category_for_list(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in list_categories(current)]

@listcards.autocomplete("subcategory")
async def ac_subcategory_for_list(interaction: discord.Interaction, current: str):
    ns = interaction.namespace if hasattr(interaction, "namespace") else None
    cat = getattr(ns, "category", None) if ns else None
    if not cat:
        return []
    rows = list_categories(prefix=str(cat))
    match = next((r for r in rows if r["name"].lower() == str(cat).lower()), None)
    if not match:
        return []
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in list_subcategories(match["id"], current)]

# ----------------------- Review flow (20/50/all) ----------------------
REVIEW_SESSION: dict[int, "Session"] = {}  # key: message_id

@dataclass
class Session:
    user_id: int
    remaining: List[int]   # card ids
    category_id: Optional[int]
    subcategory_id: Optional[int]
    total: int

class ReviewView(discord.ui.View):
    def __init__(self, session_key: int, *, timeout: int = 600):
        super().__init__(timeout=timeout)
        self.session_key = session_key

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        sess = REVIEW_SESSION.get(self.session_key)
        if not sess:
            await interaction.response.send_message("This session has ended.", ephemeral=True)
            return False
        if interaction.user.id != sess.user_id:
            await interaction.response.send_message("Only the quiz owner can interact.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="I was right ✅", style=discord.ButtonStyle.success)
    async def right_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await advance_review(interaction, self.session_key, was_right=True)

    @discord.ui.button(label="I was wrong ❌", style=discord.ButtonStyle.danger)
    async def wrong_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await advance_review(interaction, self.session_key, was_right=False)

async def advance_review(interaction: discord.Interaction, session_key: int, was_right: bool):
    # Pop next card or finish
    sess = REVIEW_SESSION.get(session_key)
    if not sess:
        await interaction.response.edit_message(content="Session ended.", view=None)
        return

    if not sess.remaining:
        # Finished: reward + streak
        await post_reward_and_streak(interaction, sess.user_id, sess.total)
        REVIEW_SESSION.pop(session_key, None)
        return

    # Next card
    next_id = sess.remaining.pop()
    row = get_card(next_id)
    if not row:
        return await advance_review(interaction, session_key, was_right)  # skip missing

    embed = discord.Embed(title="Flashcard", description=row["question"], colour=discord.Colour.blurple())
    embed.add_field(name="Answer", value=f"||{row['answer']}||", inline=False)

    view = ReviewView(session_key)
    await interaction.response.edit_message(embed=embed, view=view)

async def post_reward_and_streak(interaction: discord.Interaction, user_id: int, count: int):
    # Post reward video if exists
    reward_file = None
    if REWARDS_DIR.exists():
        vids = [p for p in REWARDS_DIR.iterdir() if p.suffix.lower() in REWARD_EXTS]
        if vids:
            reward_file = random.choice(vids)

    files = []
    content = f"🎉 Review complete! You finished **{count}** cards."
    if reward_file and reward_file.exists():
        try:
            files = [discord.File(str(reward_file), filename=reward_file.name)]
            content = f"🎉 Review complete! Enjoy your reward: **{reward_file.name}**"
        except Exception as e:
            log.warning(f"Failed attaching reward: {e}")

    # Increment daily streak
    streak = mark_reward_and_get_streak(user_id)
    content2 = f"🔥 Daily streak: **{streak}**"

    if files:
        await interaction.response.edit_message(content=content + "\n" + content2, attachments=files, embed=None, view=None)
    else:
        await interaction.response.edit_message(content=content + "\n" + content2, embed=None, view=None)

@bot.tree.command(name="reviewcards", description="Review 20, 50, or all cards (optionally filter by category/subcategory).")
@app_commands.describe(
    mode="How many to review: 20 / 50 / all",
    category="Optional category filter",
    subcategory="Optional subcategory (requires category)"
)
@app_commands.choices(mode=[
    app_commands.Choice(name="Review 20", value="20"),
    app_commands.Choice(name="Review 50", value="50"),
    app_commands.Choice(name="Review all", value="all")
])
async def reviewcards(interaction: discord.Interaction, mode: app_commands.Choice[str], category: Optional[str] = None, subcategory: Optional[str] = None):
    # Resolve filters
    cat_id = None; sub_id = None
    if category:
        rows = list_categories(prefix=category)
        match = next((r for r in rows if r["name"].lower() == category.lower()), None)
        if not match:
            await interaction.response.send_message("Category not found.", ephemeral=True); return
        cat_id = match["id"]
        if subcategory:
            subs = list_subcategories(cat_id, prefix=subcategory)
            sm = next((r for r in subs if r["name"].lower() == subcategory.lower()), None)
            if not sm:
                await interaction.response.send_message("Subcategory not found.", ephemeral=True); return
            sub_id = sm["id"]

    cards = get_candidate_cards(cat_id, sub_id)
    if not cards:
        await interaction.response.send_message("No cards available for this filter.", ephemeral=True); return

    # Build randomized set with no immediate repeats in this session
    random.shuffle(cards)
    if mode.value == "20":
        chosen = cards[:20]
    elif mode.value == "50":
        chosen = cards[:50]
    else:
        chosen = cards

    remaining_ids = [r["id"] for r in chosen]

    # Post first card placeholder
    await interaction.response.send_message(f"Starting review of **{len(remaining_ids)}** cards…", ephemeral=False)
    msg = await interaction.original_response()

    # Pop first and show
    if remaining_ids:
        first_id = remaining_ids.pop()
        row = get_card(first_id)
        embed = discord.Embed(title="Flashcard", description=row["question"], colour=discord.Colour.blurple())
        embed.add_field(name="Answer", value=f"||{row['answer']}||", inline=False)
    else:
        embed = discord.Embed(title="Flashcard", description="No cards found.", colour=discord.Colour.red())

    # Store session by message id so buttons can edit same message
    session_key = msg.id
    REVIEW_SESSION[session_key] = Session(
        user_id=interaction.user.id,
        remaining=remaining_ids,
        category_id=cat_id,
        subcategory_id=sub_id,
        total=len(chosen)
    )
    await msg.edit(embed=embed, view=ReviewView(session_key))

@reviewcards.autocomplete("category")
async def ac_category_for_review(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in list_categories(current)]

@reviewcards.autocomplete("subcategory")
async def ac_subcategory_for_review(interaction: discord.Interaction, current: str):
    ns = interaction.namespace if hasattr(interaction, "namespace") else None
    cat = getattr(ns, "category", None) if ns else None
    if not cat:
        return []
    rows = list_categories(prefix=str(cat))
    match = next((r for r in rows if r["name"].lower() == str(cat).lower()), None)
    if not match:
        return []
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in list_subcategories(match["id"], current)]

# ----------------------------- Entrypoint ------------------------------
def main():
    # Env checks with helpful logging
    log.info(f"DISCORD_TOKEN present={bool(DISCORD_TOKEN)} len={len(DISCORD_TOKEN)}")
    log.info(f"DISCORD_CLIENT_ID present={bool(CLIENT_ID)} len={len(CLIENT_ID)}")
    log.info(f"DISCORD_GUILD_ID present={bool(GUILD_ID)} value={GUILD_ID}")

    if not DISCORD_TOKEN or not CLIENT_ID or not GUILD_ID:
        print("Missing one of: DISCORD_TOKEN, DISCORD_CLIENT_ID, DISCORD_GUILD_ID")
        raise SystemExit(1)

    # Ensure DB exists & migrate
    try:
        migrate_schema()
    except sqlite3.OperationalError as e:
        log.error(f"DB migrate failed: {e}")
        raise

    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    main()


