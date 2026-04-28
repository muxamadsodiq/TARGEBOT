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


class EditField(StatesGroup):
    waiting = State()  # generic: name / subtitle / percent / photo


def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👤 Ismni o'zgartirish", callback_data="set:name"),
         InlineKeyboardButton(text="📝 Subtitle", callback_data="set:subtitle")],
        [InlineKeyboardButton(text="🖼 Profil rasmi", callback_data="set:photo"),
         InlineKeyboardButton(text="🎯 Yutuq foizi", callback_data="set:percent")],
        [InlineKeyboardButton(text="❓ Savollar", callback_data="q:menu"),
         InlineKeyboardButton(text="📋 Guruhlar", callback_data="g:list")],
        [InlineKeyboardButton(text="📊 Holat", callback_data="status"),
         InlineKeyboardButton(text="📥 Lidlar", callback_data="leads")],
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
                "SELECT created_at,name,surname,phone,percent FROM leads "
                "ORDER BY id DESC LIMIT 10"
            )
            rows = await cur.fetchall()
        if not rows:
            txt = "📥 <b>Lidlar</b>\n\nHozircha lidlar yo'q."
        else:
            body = "\n".join(f"• {r[0]} — <b>{r[1]} {r[2]}</b> | {r[3]} | {r[4]}%" for r in rows)
            txt = f"📥 <b>Oxirgi 10 lid</b>\n\n{body}"
        await cb.message.edit_text(txt, reply_markup=back_kb())
        await cb.answer()

    # ───── set:name / subtitle / photo / percent ─────
    PROMPTS = {
        "name": ("👤 Yangi <b>ismni</b> yuboring:", "profile_name"),
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
        "questions": await get_questions(),
    }


class SubmitPayload(BaseModel):
    name: str
    surname: str
    phone: str
    percent: int
    answers: list  # [{question, answer}]


@app.post("/api/submit")
async def api_submit(p: SubmitPayload):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO leads(name,surname,phone,percent,answers) VALUES(?,?,?,?,?)",
            (p.name, p.surname, p.phone, p.percent, json.dumps(p.answers, ensure_ascii=False)),
        )
        await db.commit()

    if bot:
        ans_txt = "\n".join(
            f"  • {a.get('question','?')} → <b>{a.get('answer','?')}</b>"
            for a in p.answers
        )
        text = (
            "🎉 <b>Yangi lid!</b>\n\n"
            f"👤 <b>{p.name} {p.surname}</b>\n"
            f"📞 <code>{p.phone}</code>\n"
            f"🎯 Yutuq: <b>{p.percent}%</b>\n\n"
            f"📝 Javoblar:\n{ans_txt}"
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
