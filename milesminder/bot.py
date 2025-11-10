# bot.py
from __future__ import annotations

import os
import io
import sqlite3
import random
import logging
import datetime as dt
from pathlib import Path
from typing import Optional, List

import discord
from discord import app_commands
from discord.ext import commands

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("milesminder")

# ---------------------------
# Env / Paths
# ---------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
GUILD_ID = os.getenv("DISCORD_GUILD_ID")  # optional but recommended for instant slash command availability
REWARD_DIR = Path(os.getenv("REWARD_VIDEOS_DIR", "./rewards")).resolve()
DB_PATH = Path(os.getenv("DB_PATH", "./data/milesminder.db")).resolve()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

if not TOKEN:
    log.error("Missing DISCORD_TOKEN. Exiting.")
    raise SystemExit(1)

# ---------------------------
# Discord client
# ---------------------------
intents = discord.Intents.default()
intents.message_content = False
bot = commands.Bot(command_prefix="!", intents=intents)

# ---------------------------
# SQLite helpers & schema
# ---------------------------
def db_connect() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    con.row_factory = sqlite3.Row
    return con

def migrate_schema() -> None:
    con = db_connect()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS categories(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT UNIQUE NOT NULL
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS subcategories(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      name TEXT NOT NULL,
      category_id INTEGER NOT NULL,
      UNIQUE(name, category_id),
      FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS cards(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      card_number TEXT UNIQUE NOT NULL,
      question TEXT NOT NULL,
      answer   TEXT NOT NULL,
      category_id INTEGER,
      subcategory_id INTEGER,
      FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL,
      FOREIGN KEY(subcategory_id) REFERENCES subcategories(id) ON DELETE SET NULL
    )""")

    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_category_id ON cards(category_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_subcategory_id ON cards(subcategory_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_cards_question ON cards(question)")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS user_streaks(
      user_id INTEGER PRIMARY KEY,
      last_reward_date TEXT,
      streak INTEGER NOT NULL DEFAULT 0
    )""")

    con.commit()
    con.close()

def today_str() -> str:
    return dt.datetime.now().date().isoformat()

def touch_category(name: str) -> int:
    con = db_connect()
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (name,))
    con.commit()
    cur.execute("SELECT id FROM categories WHERE name=?", (name,))
    cid = cur.fetchone()["id"]
    con.close()
    return cid

def touch_subcategory(category_id: int, name: str) -> int:
    con = db_connect()
    cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO subcategories(name, category_id) VALUES(?,?)", (name, category_id))
    con.commit()
    cur.execute("SELECT id FROM subcategories WHERE name=? AND category_id=?", (name, category_id))
    sid = cur.fetchone()["id"]
    con.close()
    return sid

def generate_card_number(con: sqlite3.Connection) -> str:
    cur = con.cursor()
    while True:
        n = f"MM-{random.randint(10000, 99999)}"
        cur.execute("SELECT 1 FROM cards WHERE card_number=?", (n,))
        if not cur.fetchone():
            return n

def list_categories_like(q: str) -> List[sqlite3.Row]:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT id, name FROM categories WHERE name LIKE ? ORDER BY name LIMIT 25", (f"%{q}%",))
    rows = cur.fetchall()
    con.close()
    return rows

def list_subcategories_like(category_id: Optional[int], q: str) -> List[sqlite3.Row]:
    con = db_connect()
    cur = con.cursor()
    if category_id:
        cur.execute("""
            SELECT id, name FROM subcategories
            WHERE category_id=? AND name LIKE ?
            ORDER BY name LIMIT 25
        """, (category_id, f"%{q}%"))
    else:
        cur.execute("""
            SELECT id, name FROM subcategories
            WHERE name LIKE ?
            ORDER BY name LIMIT 25
        """, (f"%{q}%",))
    rows = cur.fetchall()
    con.close()
    return rows

def fetch_cards(category_id: Optional[int], subcategory_id: Optional[int]) -> List[sqlite3.Row]:
    con = db_connect()
    cur = con.cursor()
    if subcategory_id:
        cur.execute("SELECT * FROM cards WHERE subcategory_id=? ORDER BY question COLLATE NOCASE", (subcategory_id,))
    elif category_id:
        cur.execute("SELECT * FROM cards WHERE category_id=? ORDER BY question COLLATE NOCASE", (category_id,))
    else:
        cur.execute("SELECT * FROM cards ORDER BY question COLLATE NOCASE")
    rows = cur.fetchall()
    con.close()
    return rows

def fetch_card(card_id: int) -> Optional[sqlite3.Row]:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT * FROM cards WHERE id=?", (card_id,))
    row = cur.fetchone()
    con.close()
    return row

def upsert_streak_and_maybe_increment(user_id: int) -> int:
    con = db_connect()
    cur = con.cursor()
    t = today_str()
    cur.execute("SELECT user_id, last_reward_date, streak FROM user_streaks WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    if not row:
        cur.execute("INSERT INTO user_streaks(user_id, last_reward_date, streak) VALUES (?,?,?)", (user_id, t, 1))
        con.commit()
        con.close()
        return 1
    last = row["last_reward_date"]
    streak = row["streak"]
    if last != t:
        streak += 1
        cur.execute("UPDATE user_streaks SET last_reward_date=?, streak=? WHERE user_id=?", (t, streak, user_id))
        con.commit()
    con.close()
    return streak

def get_streak(user_id: int) -> int:
    con = db_connect()
    cur = con.cursor()
    cur.execute("SELECT streak FROM user_streaks WHERE user_id=?", (user_id,))
    row = cur.fetchone()
    con.close()
    return row["streak"] if row else 0

# ---------------------------
# Autocomplete helpers
# ---------------------------
async def _ac_category(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    rows = list_categories_like(current or "")
    return [app_commands.Choice(name=r["name"], value=str(r["id"])) for r in rows]

async def _ac_subcategory(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    cat_val = interaction.namespace.category
    cat_id = int(cat_val) if cat_val else None
    rows = list_subcategories_like(cat_id, current or "")
    return [app_commands.Choice(name=r["name"], value=str(r["id"])) for r in rows]

# ---------------------------
# UI Views (open/edit/delete + review)
# ---------------------------
class CardView(discord.ui.View):
    def __init__(self, card_id: int, can_edit: bool = True, timeout: Optional[float] = 300):
        super().__init__(timeout=timeout)
        self.card_id = card_id
        if can_edit:
            self.add_item(EditCardButton(card_id))
            self.add_item(DeleteCardButton(card_id))

class EditCardButton(discord.ui.Button):
    def __init__(self, card_id: int):
        super().__init__(style=discord.ButtonStyle.secondary, label="Edit")
        self.card_id = card_id
    async def callback(self, interaction: discord.Interaction):
        card = fetch_card(self.card_id)
        if not card:
            return await interaction.response.send_message("Card not found.", ephemeral=True)
        modal = EditCardModal(self.card_id, card["question"], card["answer"])
        await interaction.response.send_modal(modal)

class DeleteCardButton(discord.ui.Button):
    def __init__(self, card_id: int):
        super().__init__(style=discord.ButtonStyle.danger, label="Delete")
        self.card_id = card_id
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message("Confirm delete?", view=ConfirmDeleteView(self.card_id), ephemeral=True)

class ConfirmDeleteView(discord.ui.View):
    def __init__(self, card_id: int):
        super().__init__(timeout=30)
        self.card_id = card_id
    @discord.ui.button(style=discord.ButtonStyle.danger, label="Yes, delete")
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        con = db_connect()
        cur = con.cursor()
        cur.execute("DELETE FROM cards WHERE id=?", (self.card_id,))
        con.commit()
        con.close()
        await interaction.response.edit_message(content="Card deleted.", view=None)
    @discord.ui.button(style=discord.ButtonStyle.secondary, label="Cancel")
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(content="Cancelled.", view=None)

class EditCardModal(discord.ui.Modal, title="Edit Card"):
    q = discord.ui.TextInput(label="Question", style=discord.TextStyle.paragraph, max_length=2000)
    a = discord.ui.TextInput(label="Answer", style=discord.TextStyle.paragraph, max_length=4000)
    def __init__(self, card_id: int, question: str, answer: str):
        super().__init__(timeout=300)
        self.card_id = card_id
        self.q.default = question
        self.a.default = answer
    async def on_submit(self, interaction: discord.Interaction):
        con = db_connect()
        cur = con.cursor()
        cur.execute("UPDATE cards SET question=?, answer=? WHERE id=?", (str(self.q), str(self.a), self.card_id))
        con.commit()
        con.close()
        await interaction.response.send_message("Card updated.", ephemeral=True)

class ReviewView(discord.ui.View):
    def __init__(self, user_id: int, deck_ids: List[int], target: int, timeout: Optional[float] = 900):
        super().__init__(timeout=timeout)
        self.user_id = user_id
        self.deck_ids = deck_ids[:]  # pool
        self.target = target
        self.asked: set[int] = set()
        self.current_card_id: Optional[int] = None

        self.correct = discord.ui.Button(style=discord.ButtonStyle.success, label="✅ Correct")
        self.wrong = discord.ui.Button(style=discord.ButtonStyle.secondary, label="❌ Wrong")
        self.correct.callback = self._mark_correct
        self.wrong.callback = self._mark_wrong
        self.add_item(self.correct)
        self.add_item(self.wrong)

    def _pick_next_id(self) -> Optional[int]:
        remaining = [cid for cid in self.deck_ids if cid not in self.asked]
        if not remaining:
            return None
        return random.choice(remaining)

    async def start_or_advance(self, interaction: discord.Interaction):
        if len(self.asked) >= self.target:
            await self._finish(interaction)
            return
        next_id = self._pick_next_id()
        if next_id is None:
            await self._finish(interaction)
            return
        self.current_card_id = next_id
        self.asked.add(next_id)
        card = fetch_card(next_id)
        if not card:
            await interaction.response.send_message("Card not found; skipping.", ephemeral=True)
            return
        embed = discord.Embed(title="Question", description=card["question"], color=discord.Color.blurple())
        await interaction.response.edit_message(embed=embed, view=self)

    async def _mark_correct(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This review is not for you.", ephemeral=True)
        await self._show_answer_then_next(interaction)

    async def _mark_wrong(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            return await interaction.response.send_message("This review is not for you.", ephemeral=True)
        await self._show_answer_then_next(interaction)

    async def _show_answer_then_next(self, interaction: discord.Interaction):
        card = fetch_card(self.current_card_id) if self.current_card_id else None
        if not card:
            return await interaction.response.send_message("Missing card.", ephemeral=True)
        embed = discord.Embed(title="Answer", description=card["answer"], color=discord.Color.green())
        await interaction.response.edit_message(embed=embed, view=self)

        # advance using original message edit
        try:
            if len(self.asked) >= self.target:
                await self._finish(interaction)
                return
            next_id = self._pick_next_id()
            if next_id is None:
                await self._finish(interaction)
                return
            self.current_card_id = next_id
            self.asked.add(next_id)
            ncard = fetch_card(next_id)
            if not ncard:
                return await interaction.followup.send("Card not found; skipping.", ephemeral=True)
            nembed = discord.Embed(title="Question", description=ncard["question"], color=discord.Color.blurple())
            await interaction.message.edit(embed=nembed, view=self)
        except Exception as e:
            log.exception("advance error: %s", e)

    async def _finish(self, interaction: discord.Interaction):
        files = []
        if REWARD_DIR.exists() and REWARD_DIR.is_dir():
            vids = [p for p in REWARD_DIR.iterdir() if p.suffix.lower() in {".mp4", ".mov", ".webm", ".m4v"}]
            if vids:
                chosen = random.choice(vids)
                try:
                    files.append(discord.File(fp=str(chosen), filename=chosen.name))
                except Exception as e:
                    log.warning("Could not attach reward video: %s", e)
        streak = upsert_streak_and_maybe_increment(self.user_id)
        msg = f"✅ Review complete! Streak: **{streak}🔥**"
        await interaction.response.edit_message(content=msg, embed=None, attachments=files or None, view=None)

# ---------------------------
# Commands
# ---------------------------
@bot.tree.command(name="ping", description="Basic liveness check")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong", ephemeral=True)

@bot.tree.command(name="addcategory", description="Create a category.")
@app_commands.describe(name="Category name")
async def addcategory(interaction: discord.Interaction, name: str):
    cid = touch_category(name.strip())
    await interaction.response.send_message(f"Category **{name}** created (id {cid}).", ephemeral=True)

@bot.tree.command(name="editcategory", description="Rename a category.")
@app_commands.describe(category="Choose a category", new_name="New category name")
@app_commands.autocomplete(category=_ac_category)
async def editcategory(interaction: discord.Interaction, category: Optional[str], new_name: str):
    if not category:
        return await interaction.response.send_message("Pick a category.", ephemeral=True)
    con = db_connect()
    cur = con.cursor()
    cur.execute("UPDATE categories SET name=? WHERE id=?", (new_name.strip(), int(category)))
    con.commit()
    con.close()
    await interaction.response.send_message("Category renamed.", ephemeral=True)

@bot.tree.command(name="deletecategory", description="Delete a category.")
@app_commands.describe(category="Choose a category")
@app_commands.autocomplete(category=_ac_category)
async def deletecategory(interaction: discord.Interaction, category: Optional[str]):
    if not category:
        return await interaction.response.send_message("Pick a category.", ephemeral=True)
    con = db_connect()
    cur = con.cursor()
    cur.execute("DELETE FROM categories WHERE id=?", (int(category),))
    con.commit()
    con.close()
    await interaction.response.send_message("Category deleted.", ephemeral=True)

@bot.tree.command(name="addsubcategory", description="Create a subcategory.")
@app_commands.describe(category="Choose a category", name="Subcategory name")
@app_commands.autocomplete(category=_ac_category)
async def addsubcategory(interaction: discord.Interaction, category: Optional[str], name: str):
    if not category:
        return await interaction.response.send_message("Pick a parent category.", ephemeral=True)
    sid = touch_subcategory(int(category), name.strip())
    await interaction.response.send_message(f"Subcategory **{name}** created (id {sid}).", ephemeral=True)

@bot.tree.command(name="editsubcategory", description="Rename a subcategory.")
@app_commands.describe(category="Choose the parent category", subcategory="Choose a subcategory", new_name="New name")
@app_commands.autocomplete(category=_ac_category, subcategory=_ac_subcategory)
async def editsubcategory(interaction: discord.Interaction, category: Optional[str], subcategory: Optional[str], new_name: str):
    if not category or not subcategory:
        return await interaction.response.send_message("Pick both category and subcategory.", ephemeral=True)
    con = db_connect()
    cur = con.cursor()
    cur.execute("UPDATE subcategories SET name=? WHERE id=?", (new_name.strip(), int(subcategory)))
    con.commit()
    con.close()
    await interaction.response.send_message("Subcategory renamed.", ephemeral=True)

@bot.tree.command(name="deletesubcategory", description="Delete a subcategory.")
@app_commands.describe(category="Choose the parent category", subcategory="Choose a subcategory")
@app_commands.autocomplete(category=_ac_category, subcategory=_ac_subcategory)
async def deletesubcategory(interaction: discord.Interaction, category: Optional[str], subcategory: Optional[str]):
    if not category or not subcategory:
        return await interaction.response.send_message("Pick both category and subcategory.", ephemeral=True)
    con = db_connect()
    cur = con.cursor()
    cur.execute("DELETE FROM subcategories WHERE id=?", (int(subcategory),))
    con.commit()
    con.close()
    await interaction.response.send_message("Subcategory deleted.", ephemeral=True)

@bot.tree.command(name="addcard", description="Add a flash card.")
@app_commands.describe(
    question="The question/definition",
    answer="The answer",
    category="Choose a category (optional)",
    subcategory="Choose a subcategory (optional)"
)
@app_commands.autocomplete(category=_ac_category, subcategory=_ac_subcategory)
async def addcard(
    interaction: discord.Interaction,
    question: str,
    answer: str,
    category: Optional[str] = None,
    subcategory: Optional[str] = None,
):
    con = db_connect()
    cur = con.cursor()
    card_number = generate_card_number(con)
    cat_id = int(category) if category else None
    sub_id = int(subcategory) if subcategory else None
    cur.execute(
        "INSERT INTO cards(card_number, question, answer, category_id, subcategory_id) VALUES (?,?,?,?,?)",
        (card_number, question.strip(), answer.strip(), cat_id, sub_id)
    )
    con.commit()
    new_id = cur.lastrowid
    con.close()

    embed = discord.Embed(title="Card Added", color=discord.Color.blurple())
    embed.add_field(name="Card #", value=card_number, inline=True)
    embed.add_field(name="Question", value=question[:1024], inline=False)
    embed.add_field(name="Answer", value=answer[:1024], inline=False)
    await interaction.response.send_message(embed=embed, view=CardView(new_id), ephemeral=True)

# ---- List & open cards ----
class ListPageView(discord.ui.View):
    def __init__(self, entries: List[sqlite3.Row], page: int, per_page: int, cat_id: Optional[int], sub_id: Optional[int]):
        super().__init__(timeout=300)
        self.entries = entries
        self.page = page
        self.per_page = per_page
        self.cat_id = cat_id
        self.sub_id = sub_id

        start = page * per_page
        page_slice = entries[start:start + per_page]

        for row in page_slice:
            label = row["question"][:80]
            self.add_item(OpenCardButton(row["id"], label))

        # Pagination
        if page > 0:
            self.add_item(NavButton("Prev", page - 1, cat_id, sub_id))
        if start + per_page < len(entries):
            self.add_item(NavButton("Next", page + 1, cat_id, sub_id))

class OpenCardButton(discord.ui.Button):
    def __init__(self, card_id: int, label_text: str):
        super().__init__(style=discord.ButtonStyle.primary, label=label_text)
        self.card_id = card_id
    async def callback(self, interaction: discord.Interaction):
        card = fetch_card(self.card_id)
        if not card:
            return await interaction.response.send_message("Card not found.", ephemeral=True)
        embed = discord.Embed(
            title="Card",
            description=f"**Q:** {card['question']}\n\n**A:** {card['answer']}",
            color=discord.Color.teal(),
        )
        await interaction.response.send_message(embed=embed, view=CardView(self.card_id), ephemeral=True)

class NavButton(discord.ui.Button):
    def __init__(self, label_text: str, target_page: int, cat_id: Optional[int], sub_id: Optional[int]):
        super().__init__(style=discord.ButtonStyle.secondary, label=label_text)
        self.target_page = target_page
        self.cat_id = cat_id
        self.sub_id = sub_id
    async def callback(self, interaction: discord.Interaction):
        entries = fetch_cards(self.cat_id, self.sub_id)
        per_page = 10
        view = ListPageView(entries, self.target_page, per_page, self.cat_id, self.sub_id)
        total = len(entries)
        start = self.target_page * per_page
        end = min(start + per_page, total)
        text = f"Showing {start+1}-{end} of {total} cards."
        await interaction.response.edit_message(content=text, view=view)

@bot.tree.command(name="listcards", description="List cards with optional filters; click a question to open it.")
@app_commands.describe(category="Choose a category (optional)", subcategory="Choose a subcategory (optional)")
@app_commands.autocomplete(category=_ac_category, subcategory=_ac_subcategory)
async def listcards(
    interaction: discord.Interaction,
    category: Optional[str] = None,
    subcategory: Optional[str] = None
):
    cat_id = int(category) if category else None
    sub_id = int(subcategory) if subcategory else None
    entries = fetch_cards(cat_id, sub_id)
    if not entries:
        return await interaction.response.send_message("No cards found.", ephemeral=True)

    per_page = 10
    view = ListPageView(entries, page=0, per_page=per_page, cat_id=cat_id, sub_id=sub_id)
    end = min(per_page, len(entries))
    await interaction.response.send_message(f"Showing 1-{end} of {len(entries)} cards.", view=view, ephemeral=True)

# ---- Review ----
REVIEW_CHOICES = [
    app_commands.Choice(name="review_20", value="20"),
    app_commands.Choice(name="review_50", value="50"),
    app_commands.Choice(name="review_all", value="all")
]

@bot.tree.command(name="reviewcards", description="Review 20, 50, or all cards. Optional category/subcategory.")
@app_commands.describe(
    mode="review_20 / review_50 / review_all",
    category="Choose a category (optional)",
    subcategory="Choose a subcategory (optional)"
)
@app_commands.choices(mode=REVIEW_CHOICES)
@app_commands.autocomplete(category=_ac_category, subcategory=_ac_subcategory)
async def reviewcards(
    interaction: discord.Interaction,
    mode: app_commands.Choice[str],
    category: Optional[str] = None,
    subcategory: Optional[str] = None
):
    cat_id = int(category) if category else None
    sub_id = int(subcategory) if subcategory else None

    pool = fetch_cards(cat_id, sub_id)
    if not pool:
        return await interaction.response.send_message("No cards found for that filter.", ephemeral=True)

    if mode.value == "20":
        target = min(20, len(pool))
    elif mode.value == "50":
        target = min(50, len(pool))
    else:
        target = len(pool)

    deck_ids = [row["id"] for row in pool]
    random.shuffle(deck_ids)

    view = ReviewView(user_id=interaction.user.id, deck_ids=deck_ids, target=target)
    await interaction.response.send_message("Review started.", view=view, ephemeral=True)

    try:
        await view.start_or_advance(interaction)
    except discord.errors.InteractionResponded:
        msg = await interaction.original_response()
        fake = interaction
        fake.message = msg
        await view._show_answer_then_next(fake)  # forces first advance path

# ---------------------------
# Lifecycle / Sync
# ---------------------------
@bot.event
async def setup_hook():
    migrate_schema()

    # Log pre-sync visibility
    pre_names = [c.name for c in bot.tree.get_commands()]
    log.info("Pre-sync local commands: %s", pre_names)

    if GUILD_ID:
        guild = discord.Object(id=int(GUILD_ID))

        # Hard reset guild commands, then add every local command to that guild explicitly
        bot.tree.clear_commands(guild=guild)
        for cmd in bot.tree.get_commands():  # local definitions
            bot.tree.add_command(cmd, guild=guild)

        synced = await bot.tree.sync(guild=guild)
        log.info("Force-synced %d commands to guild %s: %s",
                 len(synced), GUILD_ID, [c.name for c in synced])
    else:
        synced = await bot.tree.sync()
        log.info("Synced %d global commands (may take up to an hour to appear).", len(synced))

@bot.event
async def on_ready():
    log.info("Logged in as %s (%s)", bot.user, bot.user.id)

def main():
    bot.run(TOKEN)

if __name__ == "__main__":
    main()
