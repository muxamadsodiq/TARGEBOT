import os
import json
import asyncio
import aiosqlite
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    Message, FSInputFile, ChatMemberUpdated,
    InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery,
)
from aiogram.filters import ChatMemberUpdatedFilter, KICKED, LEFT, MEMBER, ADMINISTRATOR, CREATOR
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DEFAULT_GROUP_ID = os.getenv("TARGET_GROUP_ID", "").strip()
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8000"))

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "data.db"
STATIC_DIR = BASE_DIR / "static"
UPLOAD_DIR = STATIC_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ────────────────────────── DB ──────────────────────────
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS questions(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position INTEGER NOT NULL DEFAULT 0,
            text TEXT NOT NULL,
            options TEXT NOT NULL  -- JSON array of 3 strings
        );
        CREATE TABLE IF NOT EXISTS steps(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position INTEGER NOT NULL DEFAULT 0,
            text TEXT NOT NULL DEFAULT '',
            image TEXT,
            audio TEXT
        );
        CREATE TABLE IF NOT EXISTS zigzag(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            image TEXT
        );
        CREATE TABLE IF NOT EXISTS admin_groups(
            chat_id INTEGER PRIMARY KEY,
            title TEXT,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS leads(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            name TEXT, surname TEXT, phone TEXT,
            percent INTEGER,
            answers TEXT
        );
        """)
        await db.commit()
        # migration: add title to zigzag if missing
        try:
            await db.execute("ALTER TABLE zigzag ADD COLUMN title TEXT NOT NULL DEFAULT ''")
            await db.commit()
        except Exception:
            pass
        # migration: add badge_emoji and badge_text to zigzag
        try:
            await db.execute("ALTER TABLE zigzag ADD COLUMN badge_emoji TEXT NOT NULL DEFAULT ''")
            await db.commit()
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE zigzag ADD COLUMN badge_text TEXT NOT NULL DEFAULT ''")
            await db.commit()
        except Exception:
            pass
        try:
            await db.execute("ALTER TABLE zigzag ADD COLUMN badge_image TEXT NOT NULL DEFAULT ''")
            await db.commit()
        except Exception:
            pass
        # reviews table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS reviews(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position INTEGER NOT NULL DEFAULT 0,
            name TEXT NOT NULL DEFAULT '',
            text TEXT NOT NULL DEFAULT '',
            image TEXT,
            rating INTEGER NOT NULL DEFAULT 5
        )""")
        # voices table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS voices(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            position INTEGER NOT NULL DEFAULT 0,
            title TEXT NOT NULL DEFAULT '',
            caption TEXT NOT NULL DEFAULT '',
            audio TEXT NOT NULL DEFAULT '',
            duration INTEGER NOT NULL DEFAULT 0
        )""")
        await db.commit()
        # seed defaults
        defaults = {
            "profile_name": "Premium Quiz",
            "profile_photo": "/static/default-avatar.svg",
            "win_percent": "75",
            "group_id": DEFAULT_GROUP_ID or "",
            "welcome_subtitle": "online",
        }
        for k, v in defaults.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (k, v)
            )
        # seed questions if empty
        cur = await db.execute("SELECT COUNT(*) FROM questions")
        (n,) = await cur.fetchone()
        if n == 0:
            seeds = [
                ("Qaysi mahsulot sizni qiziqtiradi?", ["iPhone", "MacBook", "AirPods"]),
                ("Byudjetingiz qancha?", ["1-5 mln", "5-15 mln", "15+ mln"]),
                ("Qachon xarid qilmoqchisiz?", ["Bugun", "Shu hafta", "Keyinroq"]),
            ]
            for i, (t, opts) in enumerate(seeds):
                await db.execute(
                    "INSERT INTO questions(position,text,options) VALUES(?,?,?)",
                    (i, t, json.dumps(opts, ensure_ascii=False)),
                )
        await db.commit()
        # seed default steps if empty
        cur = await db.execute("SELECT COUNT(*) FROM steps")
        (sn,) = await cur.fetchone()
        if sn == 0:
            seed_steps = [
                "Assalomu alaykum! Bu yerda biz haqimizda qisqacha ma'lumot olasiz.",
                "Biz sifatli mahsulotlar va xizmatlarni eng yaxshi narxlarda taklif qilamiz.",
                "Pastdagi g'ildirakni aylantirib, shaxsiy chegirmangizni qo'lga kiriting!",
            ]
            for i, t in enumerate(seed_steps):
                await db.execute(
                    "INSERT INTO steps(position,text) VALUES(?,?)", (i, t)
                )
            await db.commit()
        # seed zigzag if empty
        cur = await db.execute("SELECT COUNT(*) FROM zigzag")
        (zn,) = await cur.fetchone()
        if zn == 0:
            seed_z = [
                "Bizning mahsulotlarimiz tabiiy va xavfsiz. Har bir tarkib sinovdan o'tgan.",
                "Tajribali mutaxassislar jamoasi sizning sog'lig'ingizni qadrlaydi.",
                "Minglab mamnun mijozlar — siz ham ularning safiga qo'shiling!",
            ]
            for i, t in enumerate(seed_z):
                await db.execute(
                    "INSERT INTO zigzag(position,text) VALUES(?,?)", (i, t)
                )
            await db.commit()
        # backgrounds table
        await db.execute("""
        CREATE TABLE IF NOT EXISTS backgrounds(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL DEFAULT 'image',
            value TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )""")
        await db.commit()
        cur = await db.execute("SELECT COUNT(*) FROM backgrounds WHERE label='__default__'")
        (bn,) = await cur.fetchone()
        if bn == 0:
            default_grad = (
                "radial-gradient(1200px 800px at 10% -10%,#5b2bff33 0%,transparent 60%),"
                "radial-gradient(900px 700px at 110% 110%,#ff2bd633 0%,transparent 60%),"
                "linear-gradient(160deg,#0d0b1f 0%,#1a1538 50%,#0d0b1f 100%)"
            )
            presets = [
                ("gradient", default_grad, "__default__", 1),
                ("gradient", "linear-gradient(135deg,#0f2027,#203a43,#2c5364)", "Tinch ko'k", 0),
                ("gradient", "linear-gradient(135deg,#1a1a2e,#16213e,#0f3460)", "Kosmos", 0),
                ("gradient", "linear-gradient(135deg,#000428,#004e92)", "Tungi dengiz", 0),
                ("gradient", "linear-gradient(135deg,#232526,#414345)", "Qora kulrang", 0),
                ("gradient", "linear-gradient(135deg,#1f1c2c,#928dab)", "Binafsha tutun", 0),
            ]
            for kind, val, label, active in presets:
                await db.execute(
                    "INSERT INTO backgrounds(kind,value,label,is_active) VALUES(?,?,?,?)",
                    (kind, val, label, active)
                )
            await db.commit()


async def get_zigzag():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,title,text,image,badge_emoji,badge_text,badge_image FROM zigzag ORDER BY position ASC, id ASC"
        )
        rows = await cur.fetchall()
    return [
        {
            "id": r[0],
            "title": r[1] or "",
            "text": r[2] or "",
            "image": r[3] or "",
            "badge_emoji": r[4] or "",
            "badge_text": r[5] or "",
            "badge_image": r[6] or "",
        }
        for r in rows
    ]


async def get_steps():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,text,image,audio FROM steps ORDER BY position ASC, id ASC"
        )
        rows = await cur.fetchall()
    return [
        {"id": r[0], "text": r[1] or "", "image": r[2] or "", "audio": r[3] or ""}
        for r in rows
    ]


async def add_step(text: str, image: str = "", audio: str = "") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COALESCE(MAX(position), -1) FROM steps")
        (mx,) = await cur.fetchone()
        cur = await db.execute(
            "INSERT INTO steps(position,text,image,audio) VALUES(?,?,?,?)",
            (mx + 1, text, image, audio),
        )
        await db.commit()
        return cur.lastrowid


async def delete_step(sid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM steps WHERE id=?", (sid,))
        await db.commit()


async def add_zigzag(title: str, text: str, image: str = "", badge_emoji: str = "", badge_text: str = "", badge_image: str = "") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COALESCE(MAX(position), -1) FROM zigzag")
        (mx,) = await cur.fetchone()
        cur = await db.execute(
            "INSERT INTO zigzag(position,title,text,image,badge_emoji,badge_text,badge_image) VALUES(?,?,?,?,?,?,?)",
            (mx + 1, title, text, image, badge_emoji, badge_text, badge_image),
        )
        await db.commit()
        return cur.lastrowid


async def update_zigzag_badge(zid: int, badge_emoji: str, badge_text: str, badge_image: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE zigzag SET badge_emoji=?, badge_text=?, badge_image=? WHERE id=?",
            (badge_emoji, badge_text, badge_image, zid),
        )
        await db.commit()


async def update_zigzag_field(zid: int, field: str, value: str):
    if field not in ("title", "text", "image"):
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE zigzag SET {field}=? WHERE id=?", (value, zid))
        await db.commit()


def parse_badge(raw: str):
    """Split user input into (emoji, text). Takes leading emoji(s) — including multi-codepoint flag emoji — as the badge symbol, rest as text."""
    import unicodedata
    s = (raw or "").strip()
    if not s:
        return "", ""
    # Walk while characters are emoji-like (symbols, regional indicators, ZWJ, variation selectors, surrogates)
    end = 0
    for i, ch in enumerate(s):
        cat = unicodedata.category(ch)
        cp = ord(ch)
        is_regional = 0x1F1E6 <= cp <= 0x1F1FF  # flag letters
        is_emoji_block = (
            0x1F000 <= cp <= 0x1FFFF
            or 0x2600 <= cp <= 0x27BF
            or 0x2B00 <= cp <= 0x2BFF
        )
        is_modifier = ch in ("\u200d", "\ufe0f", "\u20e3")
        if cat.startswith(("S", "M")) or is_regional or is_emoji_block or is_modifier:
            end = i + 1
        else:
            if end > 0 and ch == " ":
                # consume one separating space
                end = i + 1
                break
            break
    emoji = s[:end].strip()
    text = s[end:].strip()
    if not emoji and text:
        # whole input is text only
        return "", text
    return emoji, text


async def delete_zigzag(zid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM zigzag WHERE id=?", (zid,))
        await db.commit()


# ───── backgrounds ─────
async def bg_list():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,kind,value,label,is_active FROM backgrounds ORDER BY id ASC"
        )
        rows = await cur.fetchall()
    return [
        {"id": r[0], "kind": r[1], "value": r[2], "label": r[3], "is_active": bool(r[4])}
        for r in rows
    ]


async def bg_get_active():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,kind,value,label FROM backgrounds WHERE is_active=1 LIMIT 1"
        )
        r = await cur.fetchone()
        if not r:
            cur = await db.execute(
                "SELECT id,kind,value,label FROM backgrounds WHERE label='__default__' LIMIT 1"
            )
            r = await cur.fetchone()
    if not r:
        return None
    return {"id": r[0], "kind": r[1], "value": r[2], "label": r[3]}


async def bg_set_active(bid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE backgrounds SET is_active=0")
        await db.execute("UPDATE backgrounds SET is_active=1 WHERE id=?", (bid,))
        await db.commit()


async def bg_add(kind: str, value: str, label: str) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO backgrounds(kind,value,label,is_active) VALUES(?,?,?,0)",
            (kind, value, label)
        )
        await db.commit()
        return cur.lastrowid


async def bg_delete(bid: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT label,is_active FROM backgrounds WHERE id=?", (bid,))
        row = await cur.fetchone()
        if not row:
            return False
        label, is_active = row
        if label == "__default__":
            return False  # asl fonni hech qachon o'chirib bo'lmaydi
        await db.execute("DELETE FROM backgrounds WHERE id=?", (bid,))
        if is_active:
            await db.execute("UPDATE backgrounds SET is_active=1 WHERE label='__default__'")
        await db.commit()
        return True


async def get_reviews():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,name,text,image,rating FROM reviews ORDER BY position ASC, id ASC"
        )
        rows = await cur.fetchall()
        return [
            {"id": r[0], "name": r[1], "text": r[2], "image": r[3] or "", "rating": r[4]}
            for r in rows
        ]


async def add_review(name: str, text: str, image: str, rating: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COALESCE(MAX(position), -1) FROM reviews")
        (mx,) = await cur.fetchone()
        cur = await db.execute(
            "INSERT INTO reviews(position,name,text,image,rating) VALUES(?,?,?,?,?)",
            (mx + 1, name, text, image, rating),
        )
        await db.commit()
        return cur.lastrowid


async def delete_review(rid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM reviews WHERE id=?", (rid,))
        await db.commit()


async def get_voices():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,title,caption,audio,duration FROM voices ORDER BY position ASC, id ASC"
        )
        rows = await cur.fetchall()
        return [
            {"id": r[0], "title": r[1], "caption": r[2], "audio": r[3], "duration": r[4]}
            for r in rows
        ]


async def add_voice(title: str, caption: str, audio: str, duration: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COALESCE(MAX(position),0)+1 FROM voices")
        pos = (await cur.fetchone())[0]
        cur = await db.execute(
            "INSERT INTO voices(position,title,caption,audio,duration) VALUES(?,?,?,?,?)",
            (pos, title, caption, audio, duration),
        )
        await db.commit()
        return cur.lastrowid


async def delete_voice(vid: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM voices WHERE id=?", (vid,))
        await db.commit()


async def get_setting(key: str, default: str = "") -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row[0] if row else default


async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        await db.commit()


async def get_questions():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT id,text,options FROM questions ORDER BY position ASC, id ASC"
        )
        rows = await cur.fetchall()
    return [{"id": r[0], "text": r[1], "options": json.loads(r[2])} for r in rows]


async def add_admin_group(chat_id: int, title: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO admin_groups(chat_id,title) VALUES(?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET title=excluded.title,updated_at=CURRENT_TIMESTAMP",
            (chat_id, title or ""),
        )
        await db.commit()


async def remove_admin_group(chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM admin_groups WHERE chat_id=?", (chat_id,))
        await db.commit()


async def list_admin_groups():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT chat_id,title FROM admin_groups ORDER BY updated_at DESC")
        return await cur.fetchall()


# ────────────────────────── Telegram bot ──────────────────────────
bot: Optional[Bot] = None
dp: Optional[Dispatcher] = None


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


class AddQ(StatesGroup):
    text = State()
    options = State()


class AddStep(StatesGroup):
    text = State()
    image = State()
    audio = State()


class AddZig(StatesGroup):
    title = State()
    text = State()
    image = State()
    badge_emoji = State()
    badge_text = State()


class EditZigBadge(StatesGroup):
    waiting_emoji = State()
    waiting_text = State()


class EditZigField(StatesGroup):
    waiting_value = State()


class AddRev(StatesGroup):
    name = State()
    text = State()
    rating = State()
    image = State()


class AddVoice(StatesGroup):
    title = State()
    audio = State()
    caption = State()


class EditField(StatesGroup):
    waiting = State()  # generic: name / subtitle / percent / photo


class AddBg(StatesGroup):
    waiting_image = State()
    waiting_label = State()


class EditBgLabel(StatesGroup):
    pass


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏷 Sarlavha", callback_data="set:name"),
         InlineKeyboardButton(text="🎯 Yutuq foizi", callback_data="set:percent")],
        [InlineKeyboardButton(text="🔀 Zigzag", callback_data="z:menu"),
         InlineKeyboardButton(text="🎙 Ovozlik", callback_data="v:menu")],
        [InlineKeyboardButton(text="⭐ Otzivlar", callback_data="r:menu"),
         InlineKeyboardButton(text="📋 Guruhlar", callback_data="g:list")],
        [InlineKeyboardButton(text="📊 Holat", callback_data="status"),
         InlineKeyboardButton(text="📥 Lidlar", callback_data="leads")],
        [InlineKeyboardButton(text="🎨 Fon", callback_data="bg:menu")],
    ])


def back_kb(target: str = "menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Orqaga", callback_data=f"nav:{target}")]
    ])


def percent_kb() -> InlineKeyboardMarkup:
    rows, row = [], []
    for p in range(10, 101, 10):
        row.append(InlineKeyboardButton(text=f"{p}%", callback_data=f"pct:{p}"))
        if len(row) == 5:
            rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="nav:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def questions_menu_kb() -> InlineKeyboardMarkup:
    qs = await get_questions()
    rows = [[InlineKeyboardButton(text="➕ Yangi savol qo'shish", callback_data="q:add")]]
    for q in qs[:20]:
        short = q["text"][:35] + ("…" if len(q["text"]) > 35 else "")
        rows.append([
            InlineKeyboardButton(text=f"#{q['id']} {short}", callback_data=f"q:view:{q['id']}"),
        ])
    rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="nav:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def question_view_kb(qid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"q:del:{qid}")],
        [InlineKeyboardButton(text="◀️ Savollar ro'yxati", callback_data="q:menu")],
    ])


async def render_status_text() -> str:
    name = await get_setting("profile_name")
    sub = await get_setting("welcome_subtitle")
    photo = await get_setting("profile_photo")
    pct = await get_setting("win_percent")
    qs = await get_questions()
    groups = await list_admin_groups()
    return (
        f"📊 <b>Joriy holat</b>\n\n"
        f"👤 Ism: <b>{name}</b>\n"
        f"📝 Subtitle: <b>{sub}</b>\n"
        f"🖼 Foto: <code>{photo}</code>\n"
        f"🎯 Yutuq foizi: <b>{pct}%</b>\n"
        f"❓ Savollar: <b>{len(qs)}</b> ta\n"
        f"💬 Faol guruhlar: <b>{len(groups)}</b> ta"
    )


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())

    WELCOME = (
        "🛠 <b>Admin panel</b>\n\n"
        "Quyidagi tugmalar orqali boshqaring 👇\n"
        "ℹ️ Botni guruhga admin qilib qo'shsangiz — avtomatik ro'yxatga olinadi "
        "va lidlar shu guruhlarning <b>barchasiga</b> yuboriladi."
    )

    @dp.message(CommandStart())
    async def start(m: Message, state: FSMContext):
        await state.clear()
        if not is_admin(m.from_user.id):
            return await m.answer("👋 Salom! Bu admin bot.")
        await m.answer(WELCOME, reply_markup=main_menu_kb())

    @dp.message(Command("menu"))
    async def menu_cmd(m: Message, state: FSMContext):
        await state.clear()
        if not is_admin(m.from_user.id):
            return
        await m.answer(WELCOME, reply_markup=main_menu_kb())

    # ───── Navigation ─────
    @dp.callback_query(F.data == "nav:menu")
    async def nav_menu(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        await cb.message.edit_text(WELCOME, reply_markup=main_menu_kb())
        await cb.answer()

    @dp.callback_query(F.data == "status")
    async def cb_status(cb: CallbackQuery):
        await cb.message.edit_text(await render_status_text(), reply_markup=back_kb())
        await cb.answer()

    @dp.callback_query(F.data == "leads")
    async def cb_leads(cb: CallbackQuery):
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute(
                "SELECT id,created_at,name,surname,phone,percent FROM leads "
                "ORDER BY id DESC LIMIT 10"
            )
            rows = await cur.fetchall()
        if not rows:
            txt = "📥 <b>Lidlar</b>\n\nHozircha lidlar yo'q."
        else:
            def serial_of(lid):
                idx = (lid - 1) % (26 * 9999)
                return f"{chr(ord('A') + idx // 9999)}{(idx % 9999) + 1:04d}"
            body = "\n".join(
                f"<code>#{serial_of(r[0])}</code> {r[1]} — <b>{r[2]}</b> ({r[3]}) | {r[4]} | {r[5]}%"
                for r in rows
            )
            txt = f"📥 <b>Oxirgi 10 lid</b>\n\n{body}"
        await cb.message.edit_text(txt, reply_markup=back_kb())
        await cb.answer()

    # ───── set:name / subtitle / photo / percent ─────
    PROMPTS = {
        "name": ("🏷 Yangi <b>sarlavha</b>ni yuboring (qisqa, 4-6 so'z):", "profile_name"),
        "subtitle": ("📝 Yangi <b>subtitle</b> matnini yuboring (masalan: <i>online</i>):", "welcome_subtitle"),
    }

    @dp.callback_query(F.data.startswith("set:"))
    async def cb_set(cb: CallbackQuery, state: FSMContext):
        kind = cb.data.split(":", 1)[1]
        if kind == "percent":
            pct = await get_setting("win_percent")
            await cb.message.edit_text(
                f"🎯 <b>Yutuq foizi</b>\n\nHozirgi: <b>{pct}%</b>\n\nQuyidan birini tanlang:",
                reply_markup=percent_kb()
            )
            return await cb.answer()
        if kind == "photo":
            await state.set_state(EditField.waiting)
            await state.update_data(field="photo")
            await cb.message.edit_text(
                "🖼 <b>Profil rasmi</b>\n\nEndi rasmni yuboring (photo sifatida).",
                reply_markup=back_kb()
            )
            return await cb.answer()
        if kind in PROMPTS:
            prompt, field_key = PROMPTS[kind]
            await state.set_state(EditField.waiting)
            await state.update_data(field=kind, key=field_key)
            await cb.message.edit_text(prompt, reply_markup=back_kb())
            return await cb.answer()
        await cb.answer("Noma'lum amal", show_alert=True)

    @dp.callback_query(F.data.startswith("pct:"))
    async def cb_percent(cb: CallbackQuery):
        n = int(cb.data.split(":")[1])
        await set_setting("win_percent", str(n))
        await cb.answer(f"✅ Yutuq foizi: {n}%", show_alert=False)
        await cb.message.edit_text(
            f"✅ Yutuq foizi: <b>{n}%</b> qilib o'rnatildi.",
            reply_markup=back_kb()
        )

    @dp.message(EditField.waiting, F.photo)
    async def edit_photo(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        data = await state.get_data()
        if data.get("field") != "photo":
            return
        photo = m.photo[-1]
        file = await m.bot.get_file(photo.file_id)
        out = UPLOAD_DIR / f"avatar_{photo.file_unique_id}.jpg"
        await m.bot.download_file(file.file_path, destination=out)
        await set_setting("profile_photo", f"/static/uploads/{out.name}")
        await state.clear()
        await m.answer("✅ Profil rasmi yangilandi", reply_markup=main_menu_kb())

    @dp.message(EditField.waiting, F.text)
    async def edit_text(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        data = await state.get_data()
        field = data.get("field")
        key = data.get("key")
        if not key:
            return
        await set_setting(key, m.text.strip())
        await state.clear()
        await m.answer(f"✅ {field} yangilandi", reply_markup=main_menu_kb())

    # ───── Steps (info messages) ─────
    async def steps_menu_kb_local() -> InlineKeyboardMarkup:
        ss = await get_steps()
        rows = [[InlineKeyboardButton(text="➕ Yangi xabar qo'shish", callback_data="s:add")]]
        for i, s in enumerate(ss[:30]):
            short = (s["text"] or "(rasm/ovoz)")[:32]
            tag = ""
            if s["image"]: tag += " 🖼"
            if s["audio"]: tag += " 🎤"
            rows.append([
                InlineKeyboardButton(
                    text=f"{i+1}.{tag} {short}",
                    callback_data=f"s:view:{s['id']}",
                )
            ])
        rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="nav:menu")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @dp.callback_query(F.data == "s:menu")
    async def cb_s_menu(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        kb = await steps_menu_kb_local()
        await cb.message.edit_text(
            "💬 <b>Xabarlar (info)</b>\n\n"
            "Foydalanuvchiga ketma-ket ko'rsatiladigan xabarlar.\n"
            "Har xabar matn + ixtiyoriy rasm + ixtiyoriy ovoz bo'lishi mumkin.",
            reply_markup=kb,
        )
        await cb.answer()

    @dp.callback_query(F.data == "s:add")
    async def cb_s_add(cb: CallbackQuery, state: FSMContext):
        await state.set_state(AddStep.text)
        await cb.message.edit_text(
            "➕ <b>Yangi xabar</b>\n\n"
            "1-qadam: matnni yuboring (yoki <code>-</code> yozsangiz, matnsiz qoladi).",
            reply_markup=back_kb("s"),
        )
        await cb.answer()

    @dp.callback_query(F.data == "nav:s")
    async def nav_s(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        kb = await steps_menu_kb_local()
        await cb.message.edit_text("💬 <b>Xabarlar</b>", reply_markup=kb)
        await cb.answer()

    def step_view_kb(sid: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"s:del:{sid}")],
            [InlineKeyboardButton(text="◀️ Ro'yxat", callback_data="s:menu")],
        ])

    @dp.callback_query(F.data.startswith("s:view:"))
    async def cb_s_view(cb: CallbackQuery):
        sid = int(cb.data.split(":")[2])
        ss = await get_steps()
        s = next((x for x in ss if x["id"] == sid), None)
        if not s:
            return await cb.answer("Topilmadi", show_alert=True)
        no_text = "(matn yo'q)"
        info = f"<b>#{s['id']}</b>\n\n{s['text'] or no_text}\n"
        if s["image"]: info += f"\n🖼 Rasm: <code>{s['image']}</code>"
        if s["audio"]: info += f"\n🎤 Ovoz: <code>{s['audio']}</code>"
        await cb.message.edit_text(info, reply_markup=step_view_kb(sid))
        await cb.answer()

    @dp.callback_query(F.data.startswith("s:del:"))
    async def cb_s_del(cb: CallbackQuery):
        sid = int(cb.data.split(":")[2])
        await delete_step(sid)
        await cb.answer("🗑 O'chirildi")
        kb = await steps_menu_kb_local()
        await cb.message.edit_text("💬 <b>Xabarlar</b>", reply_markup=kb)

    SKIP_KB = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ O'tkazib yuborish", callback_data="s:skip")],
        [InlineKeyboardButton(text="◀️ Bekor qilish", callback_data="nav:s")],
    ])

    @dp.message(AddStep.text, F.text)
    async def addstep_text(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        txt = m.text.strip()
        if txt == "-":
            txt = ""
        await state.update_data(text=txt)
        await state.set_state(AddStep.image)
        await m.answer(
            "2-qadam: <b>rasm</b> yuboring (photo) yoki o'tkazib yuboring.",
            reply_markup=SKIP_KB,
        )

    @dp.message(AddStep.image, F.photo)
    async def addstep_image(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        photo = m.photo[-1]
        file = await m.bot.get_file(photo.file_id)
        out = UPLOAD_DIR / f"step_{photo.file_unique_id}.jpg"
        await m.bot.download_file(file.file_path, destination=out)
        await state.update_data(image=f"/static/uploads/{out.name}")
        await state.set_state(AddStep.audio)
        await m.answer(
            "3-qadam: <b>ovozli xabar</b> (voice) yuboring yoki o'tkazib yuboring.",
            reply_markup=SKIP_KB,
        )

    @dp.message(AddStep.audio, F.voice | F.audio)
    async def addstep_audio(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        if m.voice:
            f = m.voice
            ext = "ogg"
        else:
            f = m.audio
            ext = "mp3"
        file = await m.bot.get_file(f.file_id)
        out = UPLOAD_DIR / f"step_audio_{f.file_unique_id}.{ext}"
        await m.bot.download_file(file.file_path, destination=out)
        await state.update_data(audio=f"/static/uploads/{out.name}")
        data = await state.get_data()
        await add_step(data.get("text", ""), data.get("image", ""), data.get("audio", ""))
        await state.clear()
        kb = await steps_menu_kb_local()
        await m.answer("✅ Xabar qo'shildi", reply_markup=kb)

    @dp.callback_query(F.data == "s:skip")
    async def cb_s_skip(cb: CallbackQuery, state: FSMContext):
        cur_state = await state.get_state()
        data = await state.get_data()
        if cur_state == AddStep.image.state:
            await state.set_state(AddStep.audio)
            await cb.message.edit_text(
                "3-qadam: <b>ovozli xabar</b> (voice) yuboring yoki o'tkazib yuboring.",
                reply_markup=SKIP_KB,
            )
        elif cur_state == AddStep.audio.state:
            await add_step(data.get("text", ""), data.get("image", ""), data.get("audio", ""))
            await state.clear()
            kb = await steps_menu_kb_local()
            await cb.message.edit_text("✅ Xabar qo'shildi", reply_markup=kb)
        elif cur_state == AddZig.image.state:
            await state.update_data(image="")
            await state.set_state(AddZig.badge_emoji)
            await cb.message.edit_text(
                "4-qadam: 🏳 <b>bayroq</b>ni yuboring — emoji (masalan <code>🇵🇰</code>) yoki <b>rasm</b> sifatida yuklang.\n"
                "Yoki ⏭ tugmasini bosing — bayroqsiz qoladi.",
                reply_markup=ZIG_BADGE_SKIP_KB,
            )
        elif cur_state == AddZig.badge_emoji.state:
            # skipped emoji → finish without badge
            data = await state.get_data()
            await add_zigzag(
                data.get("title", ""),
                data.get("text", ""),
                data.get("image", ""),
                "",
                "",
                "",
            )
            await state.clear()
            kb = await zig_menu_kb_local()
            await cb.message.edit_text("✅ Zigzag qatori qo'shildi (bayroqsiz)", reply_markup=kb)
        elif cur_state == AddZig.badge_text.state:
            # skipped text → save with emoji/image only
            data = await state.get_data()
            await add_zigzag(
                data.get("title", ""),
                data.get("text", ""),
                data.get("image", ""),
                data.get("badge_emoji", ""),
                "",
                data.get("badge_image", ""),
            )
            await state.clear()
            kb = await zig_menu_kb_local()
            await cb.message.edit_text("✅ Zigzag qatori qo'shildi (faqat bayroq)", reply_markup=kb)
        else:
            await cb.answer()
            return
        await cb.answer()

    # ───── Zigzag (text+image rows) ─────
    async def zig_menu_kb_local() -> InlineKeyboardMarkup:
        zz = await get_zigzag()
        rows = [[InlineKeyboardButton(text="➕ Yangi qator qo'shish", callback_data="z:add")]]
        for z in zz[:20]:
            label = (z["text"][:35] + "…") if len(z["text"]) > 35 else (z["text"] or "(bo'sh)")
            rows.append([InlineKeyboardButton(text=f"#{z['id']} {label}", callback_data=f"z:view:{z['id']}")])
        rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="nav:menu")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @dp.callback_query(F.data == "z:menu")
    async def cb_z_menu(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        kb = await zig_menu_kb_local()
        await cb.message.edit_text(
            "🔀 <b>Zigzag bo'limi</b>\n\n"
            "Chat va g'ildirak orasida ko'rsatiladi.\n"
            "Har qator: matn + rasm. Tartib avtomatik almashadi (chap/o'ng).",
            reply_markup=kb,
        )
        await cb.answer()

    @dp.callback_query(F.data == "nav:z")
    async def nav_z(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        kb = await zig_menu_kb_local()
        await cb.message.edit_text("🔀 <b>Zigzag</b>", reply_markup=kb)
        await cb.answer()

    @dp.callback_query(F.data == "z:add")
    async def cb_z_add(cb: CallbackQuery, state: FSMContext):
        await state.set_state(AddZig.title)
        await cb.message.edit_text(
            "➕ <b>Yangi zigzag qatori</b>\n\n1-qadam: <b>sarlavha</b>ni yuboring (qalin matn).",
            reply_markup=back_kb("z"),
        )
        await cb.answer()

    def zig_view_kb(zid: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="✏️ Sarlavha", callback_data=f"z:edit:title:{zid}"),
             InlineKeyboardButton(text="✏️ Matn", callback_data=f"z:edit:text:{zid}")],
            [InlineKeyboardButton(text="🖼 Rasmni almashtirish", callback_data=f"z:edit:image:{zid}")],
            [InlineKeyboardButton(text="🏳 Bayroq+matn tahrirlash", callback_data=f"z:badge:{zid}")],
            [InlineKeyboardButton(text="🚫 Faqat bayroqni olish", callback_data=f"z:rmflag:{zid}"),
             InlineKeyboardButton(text="🚫 Faqat matnni olish", callback_data=f"z:rmtxt:{zid}")],
            [InlineKeyboardButton(text="🚫 Hammasini olib tashlash", callback_data=f"z:nobadge:{zid}")],
            [InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"z:del:{zid}")],
            [InlineKeyboardButton(text="◀️ Ro'yxat", callback_data="z:menu")],
        ])

    @dp.callback_query(F.data.startswith("z:view:"))
    async def cb_z_view(cb: CallbackQuery):
        zid = int(cb.data.split(":")[2])
        zz = await get_zigzag()
        z = next((x for x in zz if x["id"] == zid), None)
        if not z:
            return await cb.answer("Topilmadi", show_alert=True)
        empty = "(bo'sh)"
        title_line = f"📌 <b>{z['title']}</b>\n" if z.get("title") else ""
        info = f"<b>#{z['id']}</b>\n\n{title_line}{z['text'] or empty}\n"
        if z["image"]:
            info += f"\n🖼 Rasm: <code>{z['image']}</code>"
        if z.get("badge_emoji") or z.get("badge_text") or z.get("badge_image"):
            badge_disp = z.get('badge_emoji','') or ('🖼' if z.get('badge_image') else '')
            info += f"\n🏳 Bayroq: {badge_disp} {z.get('badge_text','')}"
            if z.get('badge_image'):
                info += f"\n📎 Bayroq rasmi: <code>{z['badge_image']}</code>"
        await cb.message.edit_text(info, reply_markup=zig_view_kb(zid))
        await cb.answer()

    @dp.callback_query(F.data.startswith("z:edit:"))
    async def cb_z_edit(cb: CallbackQuery, state: FSMContext):
        parts = cb.data.split(":")
        # z:edit:<field>:<zid>
        field = parts[2]
        zid = int(parts[3])
        await state.set_state(EditZigField.waiting_value)
        await state.update_data(zid=zid, field=field)
        labels = {
            "title": "yangi <b>sarlavha</b>ni yuboring (avtomatik KATTA harfga aylantiriladi)",
            "text": "yangi <b>matn</b>ni yuboring",
            "image": "yangi <b>rasm</b>ni yuboring (foto sifatida)",
        }
        await cb.message.edit_text(
            f"#{zid} uchun {labels.get(field, 'qiymat')}.",
            reply_markup=back_kb("z"),
        )
        await cb.answer()

    @dp.message(EditZigField.waiting_value, F.photo)
    async def edit_zig_field_photo(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        data = await state.get_data()
        if data.get("field") != "image":
            return await m.answer("Bu qadamda rasm kerak emas.")
        zid = int(data.get("zid"))
        photo = m.photo[-1]
        file = await m.bot.get_file(photo.file_id)
        out = UPLOAD_DIR / f"zig_{photo.file_unique_id}.jpg"
        await m.bot.download_file(file.file_path, destination=out)
        await update_zigzag_field(zid, "image", f"/static/uploads/{out.name}")
        await state.clear()
        kb = await zig_menu_kb_local()
        await m.answer("✅ Rasm yangilandi", reply_markup=kb)

    @dp.message(EditZigField.waiting_value, F.text)
    async def edit_zig_field_text(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        data = await state.get_data()
        zid = int(data.get("zid"))
        field = data.get("field")
        val = (m.text or "").strip()
        if field == "title":
            val = val.upper()
        if field == "image":
            return await m.answer("Iltimos, rasm yuboring.")
        await update_zigzag_field(zid, field, val)
        await state.clear()
        kb = await zig_menu_kb_local()
        await m.answer("✅ Yangilandi", reply_markup=kb)

    @dp.callback_query(F.data.startswith("z:badge:"))
    async def cb_z_badge(cb: CallbackQuery, state: FSMContext):
        zid = int(cb.data.split(":")[2])
        await state.set_state(EditZigBadge.waiting_emoji)
        await state.update_data(zid=zid)
        await cb.message.edit_text(
            f"🏳 #{zid} uchun yangi <b>bayroq</b>ni yuboring — emoji (<code>🇵🇰</code>) yoki <b>rasm</b> sifatida.\n"
            "Yoki ⏭ — bayroqni o'chirib, faqat matnni saqlash uchun.",
            reply_markup=ZIG_BADGE_SKIP_KB,
        )
        await cb.answer()

    @dp.message(EditZigBadge.waiting_emoji, F.text)
    async def edit_zig_badge_emoji(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        emoji = (m.text or "").strip()
        await state.update_data(badge_emoji=emoji, badge_image="")
        await state.set_state(EditZigBadge.waiting_text)
        await m.answer(
            "Endi ✍️ <b>matn</b>ni alohida yuboring.\n"
            "Masalan: <code>Pokistonda ishlab chiqarilgan</code>",
            reply_markup=back_kb("z"),
        )

    @dp.message(EditZigBadge.waiting_emoji, F.photo)
    async def edit_zig_badge_photo(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        photo = m.photo[-1]
        file = await m.bot.get_file(photo.file_id)
        out = UPLOAD_DIR / f"flag_{photo.file_unique_id}.jpg"
        await m.bot.download_file(file.file_path, destination=out)
        await state.update_data(badge_emoji="", badge_image=f"/static/uploads/{out.name}")
        await state.set_state(EditZigBadge.waiting_text)
        await m.answer(
            "Endi ✍️ <b>matn</b>ni alohida yuboring.",
            reply_markup=back_kb("z"),
        )

    @dp.message(EditZigBadge.waiting_text, F.text)
    async def edit_zig_badge_text(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        data = await state.get_data()
        zid = int(data.get("zid"))
        emoji = data.get("badge_emoji", "")
        badge_image = data.get("badge_image", "")
        txt = (m.text or "").strip()
        await update_zigzag_badge(zid, emoji, txt, badge_image)
        await state.clear()
        kb = await zig_menu_kb_local()
        await m.answer("✅ Bayroq saqlandi", reply_markup=kb)

    @dp.callback_query(F.data.startswith("z:nobadge:"))
    async def cb_z_nobadge(cb: CallbackQuery):
        zid = int(cb.data.split(":")[2])
        await update_zigzag_badge(zid, "", "", "")
        await cb.answer("🚫 Bayroq olindi")
        kb = await zig_menu_kb_local()
        await cb.message.edit_text("🔀 <b>Zigzag</b>", reply_markup=kb)

    @dp.callback_query(F.data.startswith("z:rmflag:"))
    async def cb_z_rmflag(cb: CallbackQuery):
        zid = int(cb.data.split(":")[2])
        zz = await get_zigzag()
        z = next((x for x in zz if x["id"] == zid), None)
        if not z:
            return await cb.answer("Topilmadi", show_alert=True)
        await update_zigzag_badge(zid, "", z.get("badge_text", ""), "")
        await cb.answer("🚫 Faqat bayroq olindi")
        kb = await zig_menu_kb_local()
        await cb.message.edit_text("🔀 <b>Zigzag</b>", reply_markup=kb)

    @dp.callback_query(F.data.startswith("z:rmtxt:"))
    async def cb_z_rmtxt(cb: CallbackQuery):
        zid = int(cb.data.split(":")[2])
        zz = await get_zigzag()
        z = next((x for x in zz if x["id"] == zid), None)
        if not z:
            return await cb.answer("Topilmadi", show_alert=True)
        await update_zigzag_badge(zid, z.get("badge_emoji", ""), "", z.get("badge_image", ""))
        await cb.answer("🚫 Faqat matn olindi")
        kb = await zig_menu_kb_local()
        await cb.message.edit_text("🔀 <b>Zigzag</b>", reply_markup=kb)

    @dp.callback_query(F.data.startswith("z:del:"))
    async def cb_z_del(cb: CallbackQuery):
        zid = int(cb.data.split(":")[2])
        await delete_zigzag(zid)
        await cb.answer("🗑 O'chirildi")
        kb = await zig_menu_kb_local()
        await cb.message.edit_text("🔀 <b>Zigzag</b>", reply_markup=kb)

    ZIG_SKIP_KB = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Rasmsiz qo'shish", callback_data="s:skip")],
        [InlineKeyboardButton(text="◀️ Bekor qilish", callback_data="nav:z")],
    ])

    ZIG_BADGE_SKIP_KB = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏭ Bayroqsiz qo'shish", callback_data="s:skip")],
        [InlineKeyboardButton(text="◀️ Bekor qilish", callback_data="nav:z")],
    ])

    @dp.message(AddZig.title, F.text)
    async def addzig_title(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        await state.update_data(title=m.text.strip().upper())
        await state.set_state(AddZig.text)
        await m.answer(
            "2-qadam: <b>matn</b>ni yuboring (sarlavha ostidagi tafsilot).",
            reply_markup=back_kb("z"),
        )

    @dp.message(AddZig.text, F.text)
    async def addzig_text(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        await state.update_data(text=m.text.strip())
        await state.set_state(AddZig.image)
        await m.answer(
            "3-qadam: <b>rasm</b> yuboring (photo) yoki rasmsiz qo'shing.",
            reply_markup=ZIG_SKIP_KB,
        )

    @dp.message(AddZig.image, F.photo)
    async def addzig_image(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        photo = m.photo[-1]
        file = await m.bot.get_file(photo.file_id)
        out = UPLOAD_DIR / f"zig_{photo.file_unique_id}.jpg"
        await m.bot.download_file(file.file_path, destination=out)
        await state.update_data(image=f"/static/uploads/{out.name}")
        await state.set_state(AddZig.badge_emoji)
        await m.answer(
            "4-qadam: 🏳 <b>bayroq emojini</b> alohida yuboring (masalan: <code>🇵🇰</code>).\n"
            "Yoki ⏭ tugmasini bosing — bayroqsiz qoladi.",
            reply_markup=ZIG_BADGE_SKIP_KB,
        )

    @dp.message(AddZig.badge_emoji, F.text)
    async def addzig_badge_emoji(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        emoji = (m.text or "").strip()
        await state.update_data(badge_emoji=emoji, badge_image="")
        await state.set_state(AddZig.badge_text)
        await m.answer(
            "5-qadam: ✍️ <b>matn</b>ni alohida yuboring (masalan: <code>Pokistonda ishlab chiqarilgan</code>).\n"
            "Yoki ⏭ tugmasini bosing — faqat bayroq qoladi.",
            reply_markup=ZIG_BADGE_SKIP_KB,
        )

    @dp.message(AddZig.badge_emoji, F.photo)
    async def addzig_badge_photo(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        photo = m.photo[-1]
        file = await m.bot.get_file(photo.file_id)
        out = UPLOAD_DIR / f"flag_{photo.file_unique_id}.jpg"
        await m.bot.download_file(file.file_path, destination=out)
        await state.update_data(badge_emoji="", badge_image=f"/static/uploads/{out.name}")
        await state.set_state(AddZig.badge_text)
        await m.answer(
            "5-qadam: ✍️ <b>matn</b>ni alohida yuboring (masalan: <code>Pokistonda ishlab chiqarilgan</code>).\n"
            "Yoki ⏭ tugmasini bosing — faqat bayroq rasmi qoladi.",
            reply_markup=ZIG_BADGE_SKIP_KB,
        )

    @dp.message(AddZig.badge_text, F.text)
    async def addzig_badge_text(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        txt = (m.text or "").strip()
        data = await state.get_data()
        await add_zigzag(
            data.get("title", ""),
            data.get("text", ""),
            data.get("image", ""),
            data.get("badge_emoji", ""),
            txt,
            data.get("badge_image", ""),
        )
        await state.clear()
        kb = await zig_menu_kb_local()
        await m.answer("✅ Zigzag qatori qo'shildi (bayroq bilan)", reply_markup=kb)


    # ───── Reviews (otzivlar) ─────
    async def rev_menu_kb_local() -> InlineKeyboardMarkup:
        revs = await get_reviews()
        rows = [
            [InlineKeyboardButton(text="➕ Yangi otziv", callback_data="r:add")],
        ]
        for r in revs[:20]:
            stars = "⭐" * int(r.get("rating") or 0)
            label = f"#{r['id']} {r['name']} {stars}"[:60]
            rows.append([
                InlineKeyboardButton(text=label, callback_data=f"r:view:{r['id']}"),
            ])
        rows.append([InlineKeyboardButton(text="◀️ Menyu", callback_data="nav:menu")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @dp.callback_query(F.data == "r:menu")
    async def cb_r_menu(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        kb = await rev_menu_kb_local()
        await cb.message.edit_text(
            "⭐ <b>Otzivlar</b>\n\n"
            "Spin oldida ko'rsatiladi.\n"
            "Har biri: ism, matn, baho (1–5 yulduz), rasm (ixtiyoriy).",
            reply_markup=kb,
        )
        await cb.answer()

    @dp.callback_query(F.data == "nav:r")
    async def nav_r(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        kb = await rev_menu_kb_local()
        await cb.message.edit_text("⭐ <b>Otzivlar</b>", reply_markup=kb)
        await cb.answer()

    @dp.callback_query(F.data == "r:add")
    async def cb_r_add(cb: CallbackQuery, state: FSMContext):
        await state.set_state(AddRev.name)
        await cb.message.edit_text(
            "➕ <b>Yangi otziv</b>\n\n1-qadam: <b>ism</b> (mijoz ismi).",
            reply_markup=back_kb("r"),
        )
        await cb.answer()

    @dp.message(AddRev.name, F.text)
    async def addrev_name(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        await state.update_data(name=m.text.strip())
        await state.set_state(AddRev.text)
        await m.answer("2-qadam: <b>otziv matni</b>.", reply_markup=back_kb("r"))

    @dp.message(AddRev.text, F.text)
    async def addrev_text(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        await state.update_data(text=m.text.strip())
        await state.set_state(AddRev.rating)
        rkb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="⭐"*i, callback_data=f"r:rate:{i}") for i in range(1, 6)
        ]])
        await m.answer("3-qadam: <b>baho</b> tanlang:", reply_markup=rkb)

    @dp.callback_query(F.data.startswith("r:rate:"))
    async def cb_r_rate(cb: CallbackQuery, state: FSMContext):
        rating = int(cb.data.split(":")[2])
        await state.update_data(rating=rating)
        await state.set_state(AddRev.image)
        skb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Rasmsiz qo'shish", callback_data="r:skip")],
            [InlineKeyboardButton(text="◀️ Bekor qilish", callback_data="nav:r")],
        ])
        await cb.message.edit_text(
            f"4-qadam: <b>rasm</b> yuboring (photo) yoki rasmsiz qo'shing.\nBaho: {'⭐'*rating}",
            reply_markup=skb,
        )
        await cb.answer()

    @dp.message(AddRev.image, F.photo)
    async def addrev_image(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        photo = m.photo[-1]
        file = await m.bot.get_file(photo.file_id)
        out = UPLOAD_DIR / f"rev_{photo.file_unique_id}.jpg"
        await m.bot.download_file(file.file_path, destination=out)
        data = await state.get_data()
        await add_review(
            data.get("name", ""),
            data.get("text", ""),
            f"/static/uploads/{out.name}",
            int(data.get("rating", 5)),
        )
        await state.clear()
        kb = await rev_menu_kb_local()
        await m.answer("✅ Otziv qo'shildi", reply_markup=kb)

    @dp.callback_query(F.data == "r:skip")
    async def cb_r_skip(cb: CallbackQuery, state: FSMContext):
        cur = await state.get_state()
        if cur != AddRev.image.state:
            return await cb.answer()
        data = await state.get_data()
        await add_review(
            data.get("name", ""),
            data.get("text", ""),
            "",
            int(data.get("rating", 5)),
        )
        await state.clear()
        kb = await rev_menu_kb_local()
        await cb.message.edit_text("✅ Otziv qo'shildi (rasmsiz)", reply_markup=kb)
        await cb.answer()

    def rev_view_kb(rid: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"r:del:{rid}")],
            [InlineKeyboardButton(text="◀️ Ro'yxat", callback_data="r:menu")],
        ])

    @dp.callback_query(F.data.startswith("r:view:"))
    async def cb_r_view(cb: CallbackQuery):
        rid = int(cb.data.split(":")[2])
        revs = await get_reviews()
        r = next((x for x in revs if x["id"] == rid), None)
        if not r:
            return await cb.answer("Topilmadi", show_alert=True)
        stars = "⭐" * int(r.get("rating") or 0)
        info = f"<b>#{r['id']}</b> — {r['name']}\n{stars}\n\n{r['text']}"
        await cb.message.edit_text(info, reply_markup=rev_view_kb(rid))
        await cb.answer()

    @dp.callback_query(F.data.startswith("r:del:"))
    async def cb_r_del(cb: CallbackQuery):
        rid = int(cb.data.split(":")[2])
        await delete_review(rid)
        await cb.answer("🗑 O'chirildi")
        kb = await rev_menu_kb_local()
        await cb.message.edit_text("⭐ <b>Otzivlar</b>", reply_markup=kb)


    # ───── Voices (ovozli xabarlar) ─────
    async def voice_menu_kb_local() -> InlineKeyboardMarkup:
        vs = await get_voices()
        rows = [[InlineKeyboardButton(text="➕ Yangi ovoz", callback_data="v:add")]]
        for v in vs[:20]:
            label = f"#{v['id']} {v.get('title') or '(sarlavhasiz)'}"[:60]
            rows.append([InlineKeyboardButton(text=label, callback_data=f"v:view:{v['id']}")])
        rows.append([InlineKeyboardButton(text="◀️ Menyu", callback_data="nav:menu")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @dp.callback_query(F.data == "v:menu")
    async def cb_v_menu(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        kb = await voice_menu_kb_local()
        await cb.message.edit_text(
            "🎙 <b>Ovozli xabarlar</b>\n\n"
            "Zigzag ostida ko'rsatiladi.\n"
            "Har biri: sarlavha + ovoz (voice/audio) + caption (ixtiyoriy).",
            reply_markup=kb,
        )
        await cb.answer()

    @dp.callback_query(F.data == "nav:v")
    async def nav_v(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        kb = await voice_menu_kb_local()
        await cb.message.edit_text("🎙 <b>Ovozli xabarlar</b>", reply_markup=kb)
        await cb.answer()

    @dp.callback_query(F.data == "v:add")
    async def cb_v_add(cb: CallbackQuery, state: FSMContext):
        await state.set_state(AddVoice.title)
        await cb.message.edit_text(
            "➕ <b>Yangi ovoz</b>\n\n1-qadam: <b>sarlavha</b> yuboring (yoki '-' belgisi sarlavhasiz uchun).",
            reply_markup=back_kb("v"),
        )
        await cb.answer()

    @dp.message(AddVoice.title, F.text)
    async def addvoice_title(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        title = m.text.strip()
        if title == "-":
            title = ""
        await state.update_data(title=title)
        await state.set_state(AddVoice.audio)
        await m.answer(
            "2-qadam: <b>ovoz</b> yuboring (voice yoki audio fayl: mp3/ogg/m4a).",
            reply_markup=back_kb("v"),
        )

    async def _save_voice_file(m: Message, state: FSMContext, file_id: str, ext: str, duration: int):
        file = await m.bot.get_file(file_id)
        out = UPLOAD_DIR / f"voice_{file.file_unique_id if hasattr(file,'file_unique_id') else file_id[-12:]}.{ext}"
        await m.bot.download_file(file.file_path, destination=out)
        await state.update_data(audio=f"/static/uploads/{out.name}", duration=int(duration or 0))
        await state.set_state(AddVoice.caption)
        ckb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏭ Captionsiz qo'shish", callback_data="v:skip")],
            [InlineKeyboardButton(text="◀️ Bekor qilish", callback_data="nav:v")],
        ])
        await m.answer("3-qadam: <b>caption</b> (ostidagi yozuv) yuboring yoki o'tkazib yuboring.", reply_markup=ckb)

    @dp.message(AddVoice.audio, F.voice)
    async def addvoice_voice(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        await _save_voice_file(m, state, m.voice.file_id, "ogg", m.voice.duration)

    @dp.message(AddVoice.audio, F.audio)
    async def addvoice_audio(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        ext = "mp3"
        if m.audio.mime_type:
            mt = m.audio.mime_type.lower()
            if "ogg" in mt: ext = "ogg"
            elif "mp4" in mt or "m4a" in mt or "aac" in mt: ext = "m4a"
            elif "wav" in mt: ext = "wav"
        await _save_voice_file(m, state, m.audio.file_id, ext, m.audio.duration)

    async def _finalize_voice(state: FSMContext, caption: str):
        data = await state.get_data()
        await add_voice(
            data.get("title", ""),
            caption,
            data.get("audio", ""),
            int(data.get("duration", 0)),
        )
        await state.clear()

    @dp.message(AddVoice.caption, F.text)
    async def addvoice_caption(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        await _finalize_voice(state, m.text.strip())
        kb = await voice_menu_kb_local()
        await m.answer("✅ Ovoz qo'shildi", reply_markup=kb)

    @dp.callback_query(F.data == "v:skip")
    async def cb_v_skip(cb: CallbackQuery, state: FSMContext):
        cur = await state.get_state()
        if cur != AddVoice.caption.state:
            return await cb.answer()
        await _finalize_voice(state, "")
        kb = await voice_menu_kb_local()
        await cb.message.edit_text("✅ Ovoz qo'shildi (captionsiz)", reply_markup=kb)
        await cb.answer()

    def voice_view_kb(vid: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"v:del:{vid}")],
            [InlineKeyboardButton(text="◀️ Ro'yxat", callback_data="v:menu")],
        ])

    @dp.callback_query(F.data.startswith("v:view:"))
    async def cb_v_view(cb: CallbackQuery):
        vid = int(cb.data.split(":")[2])
        vs = await get_voices()
        v = next((x for x in vs if x["id"] == vid), None)
        if not v:
            return await cb.answer("Topilmadi", show_alert=True)
        info = (
            f"<b>#{v['id']}</b>\n"
            f"📌 {v.get('title') or '(sarlavhasiz)'}\n"
            f"⏱ {v.get('duration', 0)}s\n"
            f"🔗 {v.get('audio') or '—'}\n\n"
            f"💬 {v.get('caption') or '(captionsiz)'}"
        )
        await cb.message.edit_text(info, reply_markup=voice_view_kb(vid))
        await cb.answer()

    @dp.callback_query(F.data.startswith("v:del:"))
    async def cb_v_del(cb: CallbackQuery):
        vid = int(cb.data.split(":")[2])
        await delete_voice(vid)
        await cb.answer("🗑 O'chirildi")
        kb = await voice_menu_kb_local()
        await cb.message.edit_text("🎙 <b>Ovozli xabarlar</b>", reply_markup=kb)


    # ───── Questions ─────
    @dp.callback_query(F.data == "q:menu")
    async def cb_q_menu(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        kb = await questions_menu_kb()
        await cb.message.edit_text(
            "❓ <b>Savollar</b>\n\nMavjud savollar (eng ko'pi 20 ta ko'rinadi). "
            "Yangi qo'shish yoki ko'rish/o'chirish uchun tanlang:",
            reply_markup=kb
        )
        await cb.answer()

    @dp.callback_query(F.data == "q:add")
    async def cb_q_add(cb: CallbackQuery, state: FSMContext):
        await state.set_state(AddQ.text)
        await cb.message.edit_text(
            "➕ <b>Yangi savol</b>\n\nSavol matnini yuboring:",
            reply_markup=back_kb("q")
        )
        await cb.answer()

    @dp.callback_query(F.data == "nav:q")
    async def nav_q(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        kb = await questions_menu_kb()
        await cb.message.edit_text("❓ <b>Savollar</b>", reply_markup=kb)
        await cb.answer()

    @dp.callback_query(F.data.startswith("q:view:"))
    async def cb_q_view(cb: CallbackQuery):
        qid = int(cb.data.split(":")[2])
        qs = await get_questions()
        q = next((x for x in qs if x["id"] == qid), None)
        if not q:
            return await cb.answer("Topilmadi", show_alert=True)
        opts = "\n".join(f"  • {o}" for o in q["options"])
        await cb.message.edit_text(
            f"<b>#{q['id']}</b>\n\n{q['text']}\n\n{opts}",
            reply_markup=question_view_kb(qid)
        )
        await cb.answer()

    @dp.callback_query(F.data.startswith("q:del:"))
    async def cb_q_del(cb: CallbackQuery):
        qid = int(cb.data.split(":")[2])
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("DELETE FROM questions WHERE id=?", (qid,))
            await db.commit()
        await cb.answer("🗑 O'chirildi")
        kb = await questions_menu_kb()
        await cb.message.edit_text("❓ <b>Savollar</b>", reply_markup=kb)

    @dp.message(AddQ.text)
    async def addq_text(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        await state.update_data(text=m.text.strip())
        await state.set_state(AddQ.options)
        await m.answer(
            "Endi <b>3 ta variantni</b> vergul bilan ajratib yuboring:\n"
            "Misol: <code>Ha, Yo'q, Bilmayman</code>",
            reply_markup=back_kb("q")
        )

    @dp.message(AddQ.options)
    async def addq_opts(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        opts = [x.strip() for x in m.text.split(",") if x.strip()]
        if len(opts) != 3:
            return await m.answer("⚠️ Aniq 3 ta variant kerak. Qaytadan yuboring.", reply_markup=back_kb("q"))
        data = await state.get_data()
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT COALESCE(MAX(position),-1)+1 FROM questions")
            (pos,) = await cur.fetchone()
            await db.execute(
                "INSERT INTO questions(position,text,options) VALUES(?,?,?)",
                (pos, data["text"], json.dumps(opts, ensure_ascii=False)),
            )
            await db.commit()
        await state.clear()
        kb = await questions_menu_kb()
        await m.answer("✅ Savol qo'shildi", reply_markup=kb)

    # ───── Groups ─────
    @dp.callback_query(F.data == "g:list")
    async def cb_groups(cb: CallbackQuery):
        rows = await list_admin_groups()
        if not rows:
            txt = (
                "📋 <b>Guruhlar</b>\n\nHozircha hech qanday guruh yo'q.\n\n"
                "👉 Botni guruhga <b>admin</b> qilib qo'shing — avtomatik ro'yxatga olinadi.\n"
                "👉 Yoki guruh ichida <code>/register</code> yuboring."
            )
        else:
            body = "\n".join(f"• <code>{cid}</code> — {title or '(no title)'}" for cid, title in rows)
            txt = f"📋 <b>Faol guruhlar ({len(rows)})</b>\n\n{body}\n\nLid yuborilganda barchasiga jo'natiladi."
        await cb.message.edit_text(txt, reply_markup=back_kb())
        await cb.answer()

    # ───── Auto-registration handlers ─────
    @dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=ADMINISTRATOR | CREATOR))
    async def bot_promoted(event: ChatMemberUpdated):
        if event.chat.type in ("group", "supergroup"):
            await add_admin_group(event.chat.id, event.chat.title or "")
            try:
                await event.bot.send_message(
                    event.chat.id,
                    "✅ Bot bu guruhga admin qilindi. Endi yangi lidlar shu guruhga ham tushadi."
                )
            except Exception:
                pass

    @dp.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=KICKED | LEFT | MEMBER))
    async def bot_demoted(event: ChatMemberUpdated):
        if event.chat.type in ("group", "supergroup"):
            await remove_admin_group(event.chat.id)

    @dp.message(Command("id"))
    async def chatid(m: Message):
        await m.answer(f"<code>{m.chat.id}</code>")

    @dp.message(Command("register"))
    async def register_group(m: Message):
        if m.chat.type not in ("group", "supergroup"):
            return await m.answer("Bu komandani guruhda yuboring.")
        try:
            me = await m.bot.get_chat_member(m.chat.id, (await m.bot.me()).id)
            if me.status not in ("administrator", "creator"):
                return await m.answer("⚠️ Avval botni guruhga admin qiling.")
        except Exception as e:
            return await m.answer(f"Xato: {e}")
        await add_admin_group(m.chat.id, m.chat.title or "")
        await m.answer("✅ Guruh ro'yxatga olindi. Yangi lidlar shu yerga ham tushadi.")

    # ───────── Fon (background) ─────────
    async def bg_menu_kb_local() -> InlineKeyboardMarkup:
        items = await bg_list()
        rows = []
        for b in items:
            label = b["label"] if b["label"] != "__default__" else "🌌 Asl fon"
            mark = "✅ " if b["is_active"] else ""
            icon = "🖼" if b["kind"] == "image" else "🎨"
            rows.append([
                InlineKeyboardButton(
                    text=f"{mark}{icon} {label}",
                    callback_data=f"bg:view:{b['id']}"
                )
            ])
        rows.append([InlineKeyboardButton(text="➕ Rasm yuklash", callback_data="bg:add")])
        rows.append([InlineKeyboardButton(text="◀️ Orqaga", callback_data="nav:menu")])
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @dp.callback_query(F.data == "bg:menu")
    async def cb_bg_menu(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        kb = await bg_menu_kb_local()
        await cb.message.edit_text(
            "🎨 <b>Fon boshqaruvi</b>\n\n"
            "Saqlangan fonlar ro'yxati. Bittasini tanlasangiz — saytda darhol qo'llanadi.\n"
            "Asl fon hech qachon o'chmaydi — istalgan vaqtda qaytarib qo'yishingiz mumkin.\n"
            "Yangi rasm yuklash ham mumkin.",
            reply_markup=kb
        )
        await cb.answer()

    @dp.callback_query(F.data == "nav:bg")
    async def nav_bg(cb: CallbackQuery, state: FSMContext):
        await state.clear()
        kb = await bg_menu_kb_local()
        await cb.message.edit_text("🎨 <b>Fon</b>", reply_markup=kb)
        await cb.answer()

    @dp.callback_query(F.data.startswith("bg:view:"))
    async def cb_bg_view(cb: CallbackQuery):
        bid = int(cb.data.split(":")[2])
        items = await bg_list()
        b = next((x for x in items if x["id"] == bid), None)
        if not b:
            return await cb.answer("Topilmadi", show_alert=True)
        label = b["label"] if b["label"] != "__default__" else "🌌 Asl fon"
        kind_label = "🖼 Rasm" if b["kind"] == "image" else "🎨 Gradient"
        active_line = "✅ <b>Hozir faol</b>\n\n" if b["is_active"] else ""
        rows = []
        if not b["is_active"]:
            rows.append([InlineKeyboardButton(text="✅ Faollashtirish", callback_data=f"bg:set:{bid}")])
        if b["label"] != "__default__":
            rows.append([InlineKeyboardButton(text="🗑 O'chirish", callback_data=f"bg:del:{bid}")])
        rows.append([InlineKeyboardButton(text="◀️ Ro'yxat", callback_data="bg:menu")])
        kb = InlineKeyboardMarkup(inline_keyboard=rows)
        text = (
            f"{active_line}"
            f"<b>{label}</b>\n"
            f"Turi: {kind_label}\n"
        )
        if b["kind"] == "image":
            try:
                await cb.message.delete()
            except Exception:
                pass
            await cb.message.answer_photo(
                photo=FSInputFile(str(BASE_DIR / b["value"].lstrip("/"))),
                caption=text, reply_markup=kb
            )
        else:
            await cb.message.edit_text(text, reply_markup=kb)
        await cb.answer()

    @dp.callback_query(F.data.startswith("bg:set:"))
    async def cb_bg_set(cb: CallbackQuery):
        bid = int(cb.data.split(":")[2])
        await bg_set_active(bid)
        await cb.answer("✅ Faollashtirildi")
        try:
            await cb.message.delete()
        except Exception:
            pass
        kb = await bg_menu_kb_local()
        await cb.message.answer("🎨 <b>Fon</b>", reply_markup=kb)

    @dp.callback_query(F.data.startswith("bg:del:"))
    async def cb_bg_del(cb: CallbackQuery):
        bid = int(cb.data.split(":")[2])
        ok = await bg_delete(bid)
        if not ok:
            return await cb.answer("Asl fonni o'chirib bo'lmaydi", show_alert=True)
        await cb.answer("🗑 O'chirildi")
        try:
            await cb.message.delete()
        except Exception:
            pass
        kb = await bg_menu_kb_local()
        await cb.message.answer("🎨 <b>Fon</b>", reply_markup=kb)

    @dp.callback_query(F.data == "bg:add")
    async def cb_bg_add(cb: CallbackQuery, state: FSMContext):
        await state.set_state(AddBg.waiting_image)
        await cb.message.edit_text(
            "📤 <b>Yangi fon rasmini yuboring</b>\n\n"
            "Rasm fon sifatida butun saytni qoplaydi. Eng yaxshisi — qorong'i, kam shovqinli rasm.",
            reply_markup=back_kb("bg")
        )
        await cb.answer()

    @dp.message(AddBg.waiting_image, F.photo)
    async def addbg_photo(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        photo = m.photo[-1]
        file = await m.bot.get_file(photo.file_id)
        out = UPLOAD_DIR / f"bg_{photo.file_unique_id}.jpg"
        await m.bot.download_file(file.file_path, destination=out)
        await state.update_data(image_path=f"/static/uploads/{out.name}")
        await state.set_state(AddBg.waiting_label)
        await m.answer("📝 Endi shu fon uchun nom yuboring (masalan: <i>Yangi fon</i>)")

    @dp.message(AddBg.waiting_label, F.text)
    async def addbg_label(m: Message, state: FSMContext):
        if not is_admin(m.from_user.id):
            return
        data = await state.get_data()
        label = m.text.strip()[:60] or "Fon"
        await bg_add("image", data["image_path"], label)
        await state.clear()
        kb = await bg_menu_kb_local()
        await m.answer(
            f"✅ <b>{label}</b> qo'shildi. Faollashtirish uchun ro'yxatdan tanlang.",
            reply_markup=kb
        )

    return dp


# ────────────────────────── FastAPI ──────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    global bot, dp
    polling_task = None
    if BOT_TOKEN and BOT_TOKEN != "PUT_YOUR_BOT_TOKEN_HERE":
        bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        dp = build_dispatcher()
        polling_task = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        print("✅ Telegram bot ishga tushdi")
    else:
        print("⚠️  BOT_TOKEN yo'q — faqat web ishlaydi")
    try:
        yield
    finally:
        if polling_task:
            polling_task.cancel()
            try:
                await polling_task
            except Exception:
                pass
        if bot:
            await bot.session.close()


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/api/config")
async def api_config():
    return {
        "name": await get_setting("profile_name"),
        "subtitle": await get_setting("welcome_subtitle"),
        "photo": await get_setting("profile_photo"),
        "win_percent": int(await get_setting("win_percent", "75")),
        "steps": await get_steps(),
        "zigzag": await get_zigzag(),
        "voices": await get_voices(),
        "reviews": await get_reviews(),
        "questions": await get_questions(),
        "background": await bg_get_active(),
    }


@app.get("/api/bg")
async def api_bg():
    return await bg_get_active() or {}


class SubmitPayload(BaseModel):
    name: str
    surname: str
    phone: str
    percent: int
    answers: list = []  # legacy, may be empty


@app.post("/api/submit")
async def api_submit(p: SubmitPayload):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT INTO leads(name,surname,phone,percent,answers) VALUES(?,?,?,?,?)",
            (p.name, p.surname, p.phone, p.percent, json.dumps(p.answers, ensure_ascii=False)),
        )
        lead_id = cur.lastrowid
        await db.commit()

    # Serial: A0001..A9999, B0001..B9999, ... Z9999, then wraps
    idx = (lead_id - 1) % (26 * 9999)
    letter = chr(ord("A") + idx // 9999)
    num = (idx % 9999) + 1
    serial = f"{letter}{num:04d}"

    if bot:
        text = (
            f"🎉 <b>Yangi lid!</b>  <code>#{serial}</code>\n\n"
            f"👤 <b>{p.name}</b>\n"
            f"📍 Viloyat: <b>{p.surname}</b>\n"
            f"📞 <code>{p.phone}</code>\n"
            f"🎯 Yutuq: <b>{p.percent}%</b>"
        )
        targets = await list_admin_groups()
        chat_ids = [cid for cid, _ in targets]
        # fallback to legacy single-group setting if list empty
        if not chat_ids:
            gid = await get_setting("group_id")
            if gid:
                try:
                    chat_ids = [int(gid)]
                except ValueError:
                    chat_ids = []
        sent = 0
        for cid in chat_ids:
            try:
                await bot.send_message(cid, text)
                sent += 1
            except Exception as e:
                print(f"⚠️ Send to {cid} failed: {e}")
        print(f"📨 Lid yuborildi: {sent}/{len(chat_ids)} guruhga")

    return {"ok": True}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=HOST, port=PORT, reload=False)
