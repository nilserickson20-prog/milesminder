import os, logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def _has(v): 
    x = os.environ.get(v)
    return f"{v} present={bool(x)} len={len(x) if x else 0}"

logging.info(_has("DISCORD_TOKEN"))
logging.info(_has("DISCORD_CLIENT_ID"))
logging.info(_has("DISCORD_GUILD_ID"))

if not (os.environ.get("DISCORD_TOKEN") and os.environ.get("DISCORD_CLIENT_ID") and os.environ.get("DISCORD_GUILD_ID")):
    print("Missing one of: DISCORD_TOKEN, DISCORD_CLIENT_ID, DISCORD_GUILD_ID")
    raise SystemExit(1)
from __future__ import annotations

import os
import sqlite3
import logging
import random
import textwrap
from typing import List, Optional, Tuple, Dict, Any

import discord
from discord import app_commands
from discord.ext import commands

# -------------------------
# Basic config & environment
# -------------------------
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"),
                    format="%(asctime)s [%(levelname)s] %(message)s")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = int(os.getenv("DISCORD_CLIENT_ID", "0"))
GUILD_ID = int(os.getenv("DISCORD_GUILD_ID", "0"))
REWARD_VIDEO_URL = os.getenv("REWARD_VIDEO_URL", "")

if not DISCORD_TOKEN or not CLIENT_ID or not GUILD_ID:
    raise SystemExit("Missing one of: DISCORD_TOKEN, DISCORD_CLIENT_ID, DISCORD_GUILD_ID")

DB_PATH = os.getenv("DB_PATH", "/data/milesminder.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

intents = discord.Intents.default()  # slash commands don't require message_content
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# ---------------------------------
# SQLite helpers (no SQLAlchemy/ORM)
# ---------------------------------
def get_db() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

def migrate_schema():
    con = get_db()
    cur = con.cursor()
    # Categories
    cur.execute("""
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL
    )
    """)
    # Subcategories
    cur.execute("""
    CREATE TABLE IF NOT EXISTS subcategories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        category_id INTEGER,
        UNIQUE(name, category_id),
        FOREIGN KEY(category_id) REFERENCES categories(id)
    )
    """)
    # Cards
    cur.execute("""
    CREATE TABLE IF NOT EXISTS cards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        card_number INTEGER UNIQUE NOT NULL,
        question TEXT NOT NULL,
        answer TEXT NOT NULL,
        category_id INTEGER,
        subcategory_id INTEGER,
        FOREIGN KEY(category_id) REFERENCES categories(id),
        FOREIGN KEY(subcategory_id) REFERENCES subcategories(id)
    )
    """)
    # Ensure subcategory_id exists (for older DBs)
    cols = {r[1] for r in cur.execute("PRAGMA table_info(cards)").fetchall()}
    if "subcategory_id" not in cols:
        cur.execute("ALTER TABLE cards ADD COLUMN subcategory_id INTEGER")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_subcategory_id ON cards(subcategory_id)")
    # Helpful indexes
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_category_id ON cards(category_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_question ON cards(question)")
    con.commit()
    con.close()

def fetchall(q: str, params: Tuple = ()) -> List[sqlite3.Row]:
    con = get_db()
    cur = con.cursor()
    cur.execute(q, params)
    rows = cur.fetchall()
    con.close()
    return rows

def fetchone(q: str, params: Tuple = ()) -> Optional[sqlite3.Row]:
    con = get_db()
    cur = con.cursor()
    cur.execute(q, params)
    row = cur.fetchone()
    con.close()
    return row

def execute(q: str, params: Tuple = ()) -> int:
    con = get_db()
    cur = con.cursor()
    cur.execute(q, params)
    con.commit()
    rowid = cur.lastrowid
    con.close()
    return rowid

def get_or_create_category(name: str) -> int:
    row = fetchone("SELECT id FROM categories WHERE name = ?", (name.strip(),))
    if row:
        return int(row["id"])
    return execute("INSERT INTO categories(name) VALUES(?)", (name.strip(),))

def get_or_create_subcategory(cat_id: int, name: str) -> int:
    row = fetchone("SELECT id FROM subcategories WHERE name=? AND category_id=?", (name.strip(), cat_id))
    if row:
        return int(row["id"])
    return execute("INSERT INTO subcategories(name, category_id) VALUES(?, ?)", (name.strip(), cat_id))

def next_card_number() -> int:
    row = fetchone("SELECT MAX(card_number) AS maxnum FROM cards")
    maxnum = row["maxnum"] if row and row["maxnum"] is not None else 999
    return int(maxnum) + 1

def list_categories(prefix: str = "", limit: int = 25) -> List[sqlite3.Row]:
    if prefix:
        return fetchall("SELECT id, name FROM categories WHERE name LIKE ? ORDER BY name LIMIT ?",
                        (f"%{prefix}%", limit))
    return fetchall("SELECT id, name FROM categories ORDER BY name LIMIT ?", (limit,))

def list_subcategories(cat_id: int, prefix: str = "", limit: int = 25) -> List[sqlite3.Row]:
    if cat_id <= 0:
        return []
    if prefix:
        return fetchall("""SELECT id, name FROM subcategories
                           WHERE category_id=? AND name LIKE ?
                           ORDER BY name LIMIT ?""",
                        (cat_id, f"%{prefix}%", limit))
    return fetchall("""SELECT id, name FROM subcategories
                       WHERE category_id=?
                       ORDER BY name LIMIT ?""",
                    (cat_id, limit))

def find_category_by_name(name: str) -> Optional[sqlite3.Row]:
    return fetchone("SELECT id, name FROM categories WHERE name = ?", (name.strip(),))

def find_subcategory_by_name(cat_id: int, name: str) -> Optional[sqlite3.Row]:
    return fetchone("SELECT id, name FROM subcategories WHERE category_id=? AND name=?",
                    (cat_id, name.strip()))

def find_card_by_number(card_number: int) -> Optional[sqlite3.Row]:
    return fetchone("""SELECT c.*, cat.name AS category_name, sub.name AS subcategory_name
                       FROM cards c
                       LEFT JOIN categories cat ON cat.id = c.category_id
                       LEFT JOIN subcategories sub ON sub.id = c.subcategory_id
                       WHERE c.card_number = ?""", (card_number,))

def search_cards(category_id: Optional[int], subcategory_id: Optional[int]) -> List[sqlite3.Row]:
    base = """SELECT c.*, cat.name AS category_name, sub.name AS subcategory_name
              FROM cards c
              LEFT JOIN categories cat ON cat.id = c.category_id
              LEFT JOIN subcategories sub ON sub.id = c.subcategory_id"""
    where = []
    params: List[Any] = []
    if category_id:
        where.append("c.category_id = ?")
        params.append(category_id)
    if subcategory_id:
        where.append("c.subcategory_id = ?")
        params.append(subcategory_id)
    if where:
        base += " WHERE " + " AND ".join(where)
    base += " ORDER BY c.question COLLATE NOCASE ASC"
    return fetchall(base, tuple(params))

def insert_card(question: str, answer: str,
                category_id: Optional[int], subcategory_id: Optional[int]) -> int:
    number = next_card_number()
    execute("""INSERT INTO cards(card_number, question, answer, category_id, subcategory_id)
               VALUES (?, ?, ?, ?, ?)""",
            (number, question.strip(), answer.strip(), category_id, subcategory_id))
    return number

# -------------------------
# Views (Buttons for UI)
# -------------------------
ACTIVE_REVIEWS: Dict[int, Dict[str, Any]] = {}  # message_id -> state

def truncate_label(s: str, max_chars: int = 80) -> str:
    s = " ".join(s.split())
    return (s[:max_chars - 1] + "…") if len(s) > max_chars else s

class ListPageView(discord.ui.View):
    def __init__(self, cards: List[sqlite3.Row], page: int, per_page: int,
                 category_id: Optional[int], subcategory_id: Optional[int]):
        super().__init__(timeout=300)
        self.cards = cards
        self.page = page
        self.per_page = per_page
        self.category_id = category_id
        self.subcategory_id = subcategory_id

        start = page * per_page
        end = min(len(cards), start + per_page)
        slice_rows = cards[start:end]

        # Up to 25 components allowed; we add one button per card + nav
        for row in slice_rows:
            label = truncate_label(row["question"], 80)
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary)
            async def on_click(interaction: discord.Interaction, card_number=row["card_number"]):
                await send_card_embed(interaction, card_number)
            btn.callback = on_click
            self.add_item(btn)

        # Nav row (only if needed)
        can_prev = page > 0
        can_next = end < len(cards)
        if can_prev or can_next:
            if can_prev:
                self.add_item(self._nav_button("Prev", -1))
            if can_next:
                self.add_item(self._nav_button("Next", +1))

    def _nav_button(self, label: str, delta: int) -> discord.ui.Button:
        btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary)
        async def on_click(interaction: discord.Interaction):
            new_page = self.page + delta
            await send_list_page(interaction, self.cards, new_page, self.per_page,
                                 self.category_id, self.subcategory_id, replace=True)
        btn.callback = on_click
        return btn

async def send_list_page(interaction: discord.Interaction,
                         cards: List[sqlite3.Row], page: int, per_page: int,
                         category_id: Optional[int], subcategory_id: Optional[int],
                         replace: bool = False):
    page = max(0, page)
    start = page * per_page
    end = min(len(cards), start + per_page)
    if start >= len(cards) and len(cards) > 0:
        page = 0
        start = 0
        end = min(len(cards), per_page)

    title = "Cards"
    if category_id:
        cat = fetchone("SELECT name FROM categories WHERE id=?", (category_id,))
        if cat:
            title += f" · {cat['name']}"
    if subcategory_id:
        sub = fetchone("SELECT name FROM subcategories WHERE id=?", (subcategory_id,))
        if sub:
            title += f" · {sub['name']}"

    desc_lines = []
    for row in cards[start:end]:
        desc_lines.append(f"• **#{row['card_number']}** — {row['question']}")
    if not desc_lines:
        desc_lines = ["*(No cards found)*"]

    embed = discord.Embed(title=title,
                          description="\n".join(desc_lines),
                          colour=discord.Colour.blurple())
    embed.set_footer(text=f"Page {page+1} of {max(1, (len(cards)+per_page-1)//per_page)} · {len(cards)} total")

    view = ListPageView(cards, page, per_page, category_id, subcategory_id)
    if replace and interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    elif replace:
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

async def send_card_embed(interaction: discord.Interaction, card_number: int):
    row = find_card_by_number(card_number)
    if not row:
        await interaction.response.send_message("Card not found.", ephemeral=True)
        return

    title = f"Card #{row['card_number']}"
    if row["category_name"]:
        title += f" · {row['category_name']}"
    if row["subcategory_name"]:
        title += f" · {row['subcategory_name']}"

    # Answer in spoiler
    desc = f"**Q:** {row['question']}\n**A:** ||{row['answer']}||"
    embed = discord.Embed(title=title, description=desc, colour=discord.Colour.green())
    await interaction.response.send_message(embed=embed, ephemeral=True)

# --- Review (study) UI ---

class ReviewView(discord.ui.View):
    def __init__(self, message_id: int, user_id: int):
        super().__init__(timeout=600)
        self.mid = message_id
        self.uid = user_id

    @discord.ui.button(label="✅ Correct", style=discord.ButtonStyle.success)
    async def correct(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._advance(interaction, result="correct")

    @discord.ui.button(label="❌ Incorrect", style=discord.ButtonStyle.danger)
    async def incorrect(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._advance(interaction, result="incorrect")

    @discord.ui.button(label="⏭ Skip", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._advance(interaction, result="skip")

    async def _advance(self, interaction: discord.Interaction, result: str):
        state = ACTIVE_REVIEWS.get(self.mid)
        if not state:
            await interaction.response.send_message("This review session expired.", ephemeral=True)
            return
        if interaction.user.id != state["user_id"]:
            await interaction.response.send_message("This isn’t your review session.", ephemeral=True)
            return

        # Mark current as seen
        current = state.get("current_card")
        if current:
            state["seen"].add(current)

        # Decide next or finish
        remaining = [c for c in state["pool"] if c not in state["seen"]]
        need = state["target_count"]
        if need > 0 and len(state["seen"]) >= need:
            await self._finish(interaction, state)
            return
        if not remaining:
            await self._finish(interaction, state)
            return

        next_card = random.choice(remaining)
        state["current_card"] = next_card
        row = find_card_by_number(next_card)
        if not row:
            await interaction.response.send_message("Next card not found; ending session.", ephemeral=True)
            ACTIVE_REVIEWS.pop(self.mid, None)
            return

        title = f"Review ({len(state['seen'])}/{need if need>0 else len(state['pool'])})"
        if row["category_name"]:
            title += f" · {row['category_name']}"
        if row["subcategory_name"]:
            title += f" · {row['subcategory_name']}"

        desc = f"**Q:** {row['question']}\n**A:** ||{row['answer']}||"
        embed = discord.Embed(title=title, description=desc, colour=discord.Colour.orange())

        await interaction.response.edit_message(embed=embed, view=self)

    async def _finish(self, interaction: discord.Interaction, state: Dict[str, Any]):
        # Congratulate + reward video if set
        msg = "🎉 Review complete — nice work!"
        files = []
        if REWARD_VIDEO_URL:
            # We can only post links unless you host and download file server-side.
            msg = f"{msg}\n{REWARD_VIDEO_URL}"
        await interaction.response.edit_message(content=msg, embed=None, view=None)
        ACTIVE_REVIEWS.pop(self.mid, None)

# ---------------
# Autocomplete
# ---------------
async def ac_category(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    rows = list_categories(prefix=current or "", limit=25)
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]

async def ac_subcategory(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    # We’ll try to resolve the category param (if present) to filter subcategories
    cat_name = None
    try:
        # depending on which command, the option may be present
        cat_name = interaction.namespace.get("category")  # type: ignore[attr-defined]
    except Exception:
        pass

    cat_id = 0
    if cat_name:
        cat = find_category_by_name(cat_name)
        if cat:
            cat_id = int(cat["id"])
    rows = list_subcategories(cat_id, prefix=current or "", limit=25)
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]

# ----------------
# Slash commands
# ----------------

@tree.command(name="addcategory", description="Add a new category")
@app_commands.describe(name="Category name")
async def addcategory(interaction: discord.Interaction, name: str):
    cid = get_or_create_category(name)
    await interaction.response.send_message(f"✅ Category **{name}** (id {cid}) added/exists.", ephemeral=True)

@tree.command(name="addsubcategory", description="Add a new subcategory under a category")
@app_commands.describe(category="Parent category", name="Subcategory name")
@app_commands.autocomplete(category=ac_category)
async def addsubcategory(interaction: discord.Interaction, category: str, name: str):
    cat = find_category_by_name(category)
    if not cat:
        await interaction.response.send_message("Category not found.", ephemeral=True)
        return
    sid = get_or_create_subcategory(int(cat["id"]), name)
    await interaction.response.send_message(
        f"✅ Subcategory **{name}** added under **{cat['name']}** (id {sid}).", ephemeral=True
    )

@tree.command(name="addcard", description="Add a new flash card (auto-numbered)")
@app_commands.describe(
    question="Question/Prompt",
    answer="Answer/Definition",
    category="Optional existing category",
    subcategory="Optional existing subcategory (filtered by category if provided)"
)
@app_commands.autocomplete(category=ac_category, subcategory=ac_subcategory)
async def addcard(interaction: discord.Interaction,
                  question: str,
                  answer: str,
                  category: Optional[str] = None,
                  subcategory: Optional[str] = None):
    category_id = None
    subcategory_id = None
    if category:
        cat = find_category_by_name(category)
        if not cat:
            await interaction.response.send_message("Category not found.", ephemeral=True)
            return
        category_id = int(cat["id"])
        if subcategory:
            sub = find_subcategory_by_name(category_id, subcategory)
            if not sub:
                await interaction.response.send_message("Subcategory not found.", ephemeral=True)
                return
            subcategory_id = int(sub["id"])

    num = insert_card(question, answer, category_id, subcategory_id)
    where = []
    if category:
        where.append(category)
    if subcategory:
        where.append(subcategory)
    suffix = f" in **{' · '.join(where)}**" if where else ""
    await interaction.response.send_message(f"✅ Added card **#{num}**{suffix}.", ephemeral=True)

@tree.command(name="listcards", description="List cards, optionally by category/subcategory")
@app_commands.describe(
    category="Optional category",
    subcategory="Optional subcategory (filtered by category)"
)
@app_commands.autocomplete(category=ac_category, subcategory=ac_subcategory)
async def listcards(interaction: discord.Interaction,
                    category: Optional[str] = None,
                    subcategory: Optional[str] = None):
    cat_id = None
    sub_id = None
    if category:
        cat = find_category_by_name(category)
        if not cat:
            await interaction.response.send_message("Category not found.", ephemeral=True)
            return
        cat_id = int(cat["id"])
        if subcategory:
            sub = find_subcategory_by_name(cat_id, subcategory)
            if not sub:
                await interaction.response.send_message("Subcategory not found.", ephemeral=True)
                return
            sub_id = int(sub["id"])

    cards = search_cards(cat_id, sub_id)
    await send_list_page(interaction, cards, page=0, per_page=10,
                         category_id=cat_id, subcategory_id=sub_id, replace=False)

@tree.command(name="showcard", description="Show a specific card by its number")
@app_commands.describe(card_number="Numeric card number")
async def showcard(interaction: discord.Interaction, card_number: int):
    await send_card_embed(interaction, card_number)

# Review modes
REVIEW_CHOICES = [
    app_commands.Choice(name="review_20", value="20"),
    app_commands.Choice(name="review_50", value="50"),
    app_commands.Choice(name="review_all", value="all"),
]

def _pool_for_review(cat_id: Optional[int], sub_id: Optional[int]) -> List[int]:
    rows = search_cards(cat_id, sub_id)
    return [int(r["card_number"]) for r in rows]

async def _start_review(inter: discord.Interaction, mode_value: str,
                        cat_id: Optional[int], sub_id: Optional[int]):
    pool = _pool_for_review(cat_id, sub_id)
    if not pool:
        await inter.response.send_message("No cards found for that filter.", ephemeral=True)
        return
    random.shuffle(pool)
    target = 0
    if mode_value == "20":
        target = min(20, len(pool))
        pool = pool[:target]
    elif mode_value == "50":
        target = min(50, len(pool))
        pool = pool[:target]
    else:
        target = 0  # all

    # Post first card
    first = pool[0]
    row = find_card_by_number(first)
    title = "Review"
    if row["category_name"]:
        title += f" · {row['category_name']}"
    if row["subcategory_name"]:
        title += f" · {row['subcategory_name']}"
    desc = f"**Q:** {row['question']}\n**A:** ||{row['answer']}||"
    embed = discord.Embed(title=title, description=desc, colour=discord.Colour.orange())

    await inter.response.send_message("Review started.", ephemeral=True)
    msg = await inter.followup.send(embed=embed, ephemeral=False)

    state = {
        "user_id": inter.user.id,
        "pool": pool,
        "seen": set(),          # card_numbers shown
        "current_card": first,  # card_number
        "target_count": target  # 0 == all
    }
    ACTIVE_REVIEWS[msg.id] = state
    view = ReviewView(message_id=msg.id, user_id=inter.user.id)
    await msg.edit(view=view)

@tree.command(name="reviewcards", description="Review 20, 50, or all cards; optionally filter by category/subcategory")
@app_commands.describe(
    mode="Choose how many to review",
    category="Optional category filter",
    subcategory="Optional subcategory filter (filtered by category)"
)
@app_commands.choices(mode=REVIEW_CHOICES)
@app_commands.autocomplete(category=ac_category, subcategory=ac_subcategory)
async def reviewcards(interaction: discord.Interaction,
                      mode: app_commands.Choice[str],
                      category: Optional[str] = None,
                      subcategory: Optional[str] = None):
    cat_id = None
    sub_id = None
    if category:
        cat = find_category_by_name(category)
        if not cat:
            await interaction.response.send_message("Category not found.", ephemeral=True)
            return
        cat_id = int(cat["id"])
        if subcategory:
            sub = find_subcategory_by_name(cat_id, subcategory)
            if not sub:
                await interaction.response.send_message("Subcategory not found.", ephemeral=True)
                return
            sub_id = int(sub["id"])

    await _start_review(interaction, mode.value, cat_id, sub_id)

# -------------------------
# Bot lifecycle & sync
# -------------------------
@bot.event
async def on_ready():
    logging.info("Logged in as %s", bot.user)
    guild = discord.Object(id=GUILD_ID)
    cmds = await tree.sync(guild=guild)  # fast, guild-scoped sync
    logging.info("Synced %d commands to guild %s", len(cmds), GUILD_ID)

# -------------------------
# Main
# -------------------------
def main():
    migrate_schema()
    bot.run(DISCORD_TOKEN, log_handler=None)

if __name__ == "__main__":
    main()

