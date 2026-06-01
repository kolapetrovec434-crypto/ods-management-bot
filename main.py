"""
UA ONLINE BOT — Повна версія
Всі системи зі специфікації реалізовані в одному файлі.
"""

import asyncio
import json
import os
import logging
import shutil
from datetime import datetime, timedelta
from collections import defaultdict

from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, ReplyKeyboardMarkup, KeyboardButton
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest

# ═══════════════════════════════════════════════
#  КОНФІГУРАЦІЯ
# ═══════════════════════════════════════════════
TOKEN    = "8905769390:AAHA1YUTQti2_diRFLa2f9KRYGc1QKdJ4ys"   # ← Встав свій токен
ADMIN_ID = 1188859918          # ← Твій Telegram ID
DB_FILE  = "ua_online_db.json"
LOG_FILE = "ua_online_log.json"
BACKUP_DIR = "backups"

logging.basicConfig(level=logging.INFO)

# Ієрархія ролей (чим більше число — тим вища роль)
ROLES = {
    "Стажер": 0,
    "Модератор": 1,
    "Старший модератор": 2,
    "Куратор модерації": 3,
    "Головний адміністратор": 4,
    "Керівник проєкту": 5,
}

# Бали за дії
POINTS_TABLE = {
    "mute":    {"points": 10, "xp": 50},
    "ban":     {"points": 15, "xp": 70},
    "warn":    {"points": 5,  "xp": 25},
    "unmute":  {"points": 5,  "xp": 20},
    "unban":   {"points": 5,  "xp": 20},
    "help":    {"points": 8,  "xp": 40},
    "ticket":  {"points": 12, "xp": 60},
    "clear":   {"points": 3,  "xp": 15},
}

XP_PER_LEVEL = 1000  # XP потрібно для нового рівня Battle Pass
DAILY_BONUS  = 20    # балів за щоденний бонус

# ═══════════════════════════════════════════════
#  FSM СТАНИ
# ═══════════════════════════════════════════════
class Form(StatesGroup):
    # Редагування профілю
    edit_nick    = State()
    edit_bio     = State()
    edit_discord = State()
    edit_sex     = State()
    edit_age     = State()

    # Подача заявки на бали
    claim_reason = State()
    claim_points = State()
    claim_proof  = State()

    # Заявка на підвищення
    promo_reason = State()

    # Відпустка
    vacation_dates = State()
    vacation_reason = State()

    # BP адмін
    bp_create_name  = State()
    bp_task_text    = State()
    bp_task_xp      = State()
    bp_reward_level = State()
    bp_reward_text  = State()

    # Догана
    reprimand_target = State()
    reprimand_reason = State()

# ═══════════════════════════════════════════════
#  АНТИСПАМ / АНТИФЛУД
# ═══════════════════════════════════════════════
_flood: dict[int, list[float]] = defaultdict(list)
FLOOD_LIMIT   = 5    # повідомлень
FLOOD_WINDOW  = 10   # секунд

def is_flood(user_id: int) -> bool:
    now = asyncio.get_event_loop().time()
    stamps = _flood[user_id]
    stamps[:] = [t for t in stamps if now - t < FLOOD_WINDOW]
    stamps.append(now)
    return len(stamps) > FLOOD_LIMIT

# ═══════════════════════════════════════════════
#  БАЗА ДАНИХ
# ═══════════════════════════════════════════════
def _default_db():
    return {
        "users": {},
        "bp": {
            "season": 1,
            "name": "Сезон 1",
            "tasks": [],
            "rewards": {}
        },
        "claims": [],
        "promotions": [],
        "vacations": [],
        "reprimands": [],
        "events": [],
        "norms": {"weekly": 50, "monthly": 200},
    }

def load_db() -> dict:
    if not os.path.exists(DB_FILE):
        return _default_db()
    with open(DB_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)
    # Міграція: якщо поле відсутнє — додати
    for key, val in _default_db().items():
        if key not in db:
            db[key] = val
    return db

def save_db(db: dict):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=2, ensure_ascii=False)

def backup_db():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    shutil.copy(DB_FILE, f"{BACKUP_DIR}/db_{ts}.json")

def log_action(actor_id, action, target=None, details=""):
    entry = {
        "ts":      datetime.now().isoformat(),
        "actor":   actor_id,
        "action":  action,
        "target":  target,
        "details": details,
    }
    logs = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            logs = json.load(f)
    logs.append(entry)
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(logs[-5000:], f, indent=2, ensure_ascii=False)  # зберігати останні 5000

def get_user(db, user_id, user_obj=None) -> dict:
    uid = str(user_id)
    if uid not in db["users"]:
        db["users"][uid] = {
            "nick":     user_obj.first_name if user_obj else "Unknown",
            "tag":      f"@{user_obj.username}".lower() if user_obj and user_obj.username else "немає",
            "discord":  "Не вказано",
            "bio":      "Персонал UA ONLINE",
            "sex":      "N/A",
            "age":      "N/A",
            "role":     "Стажер",
            "points":   0,
            "xp":       0,
            "level":    1,
            "reputation": 0,
            "stats": {
                "mutes": 0, "bans": 0, "warns": 0,
                "help": 0, "tickets": 0, "complaints": 0,
            },
            "achievements": [],
            "bp_tasks_done": [],
            "daily_last":    None,
            "vacation":      None,
            "reprimands":    0,
            "reg_date":      datetime.now().strftime("%d.%m.%Y"),
        }
        if int(uid) == ADMIN_ID:
            db["users"][uid]["role"] = "Керівник проєкту"
    return db["users"][uid]

def add_points(u: dict, action: str):
    """Нарахувати бали і XP за дію, перевірити підвищення рівня."""
    p = POINTS_TABLE.get(action, {"points": 0, "xp": 0})
    u["points"] += p["points"]
    u["xp"]     += p["xp"]
    while u["xp"] >= XP_PER_LEVEL:
        u["xp"]   -= XP_PER_LEVEL
        u["level"] += 1

def check_achievements(u: dict) -> list[str]:
    """Повертає список нових досягнень."""
    earned = []
    ach = u["achievements"]
    s = u["stats"]

    checks = [
        ("first_mute",     s["mutes"] >= 1,     "🔇 Перший мут"),
        ("mute_10",        s["mutes"] >= 10,     "🔇 10 мутів"),
        ("ban_5",          s["bans"]  >= 5,      "🔨 5 банів"),
        ("help_100",       s["help"]  >= 100,    "🤝 100 допомог"),
        ("tickets_50",     s["tickets"] >= 50,   "🎫 50 тікетів"),
        ("points_100",     u["points"] >= 100,   "💰 100 балів"),
        ("points_500",     u["points"] >= 500,   "💰 500 балів"),
        ("points_1000",    u["points"] >= 1000,  "💰 1000 балів"),
        ("complaints_10",  s["complaints"] >= 10,"📋 10 скарг"),
    ]
    for key, cond, title in checks:
        if cond and key not in ach:
            ach.append(key)
            earned.append(title)
    return earned

# ═══════════════════════════════════════════════
#  БОТ
# ═══════════════════════════════════════════════
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()

# ─── ДОПОМІЖНІ КЛАВІАТУРИ ───────────────────────
def profile_markup(uid: str, is_owner: bool, viewer_role_level: int = 0) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="🏆 Battle Pass",  callback_data="bp_view"),
            InlineKeyboardButton(text="📊 Статистика",   callback_data=f"stats_{uid}"),
        ]
    ]
    if is_owner:
        rows.append([
            InlineKeyboardButton(text="⚙️ Редагувати",   callback_data="edit_menu"),
            InlineKeyboardButton(text="📥 Подати на бали", callback_data="claim_start"),
        ])
        rows.append([
            InlineKeyboardButton(text="🏅 Досягнення",   callback_data=f"ach_{uid}"),
            InlineKeyboardButton(text="📅 Відпустка",    callback_data="vacation_menu"),
        ])
        rows.append([
            InlineKeyboardButton(text="📈 Заявка на підвищення", callback_data="promo_start"),
        ])
    else:
        rows.append([
            InlineKeyboardButton(text="🏅 Досягнення",   callback_data=f"ach_{uid}"),
            InlineKeyboardButton(text="👍 Репутація +1",  callback_data=f"rep_{uid}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def edit_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🏷 Нікнейм",  callback_data="set_n"),
            InlineKeyboardButton(text="🎮 Discord",   callback_data="set_d"),
        ],
        [
            InlineKeyboardButton(text="📝 Біо",      callback_data="set_b"),
            InlineKeyboardButton(text="⚧ Стать",     callback_data="set_s"),
        ],
        [
            InlineKeyboardButton(text="🎂 Вік",      callback_data="set_a"),
            InlineKeyboardButton(text="◀ Назад",     callback_data="back_profile"),
        ],
    ])

def admin_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Заявки на бали",  callback_data="admin_claims"),
            InlineKeyboardButton(text="🎖 Заявки на підвищення", callback_data="admin_promos"),
        ],
        [
            InlineKeyboardButton(text="🏖 Відпустки",       callback_data="admin_vacations"),
            InlineKeyboardButton(text="📜 Журнал",          callback_data="admin_log"),
        ],
        [
            InlineKeyboardButton(text="💾 Backup БД",       callback_data="admin_backup"),
            InlineKeyboardButton(text="⚙️ Норми",           callback_data="admin_norms"),
        ],
    ])

# ═══════════════════════════════════════════════
#  /START
# ═══════════════════════════════════════════════
@dp.message(Command("start"))
async def cmd_start(message: Message):
    db = load_db()
    u  = get_user(db, message.from_user.id, message.from_user)
    save_db(db)
    await message.answer(
        f"👋 Ласкаво просимо, <b>{u['nick']}</b>!\n\n"
        "🤖 <b>UA ONLINE BOT</b> — система управління персоналом.\n\n"
        "📌 Основні команди:\n"
        "/profile — профіль\n"
        "/top — рейтинг\n"
        "/stats — статистика\n"
        "/bp — Battle Pass\n"
        "/daily — щоденний бонус\n"
        "/help — всі команди"
    )

# ═══════════════════════════════════════════════
#  /HELP
# ═══════════════════════════════════════════════
@dp.message(Command("help"))
async def cmd_help(message: Message):
    text = (
        "📖 <b>КОМАНДИ UA ONLINE BOT</b>\n\n"
        "👤 <b>Профіль:</b>\n"
        "/profile [@user] — переглянути профіль\n"
        "/editprofile — меню редагування\n"
        "/setnick [нік] — змінити нікнейм\n"
        "/setbio [текст] — змінити біо\n"
        "/stats [@user] — детальна статистика\n\n"
        "🏅 <b>Прогрес:</b>\n"
        "/top — топ-10 персоналу\n"
        "/bp — Battle Pass\n"
        "/daily — щоденний бонус\n"
        "/claimpoints — подати на бали\n"
        "/achievements [@user] — досягнення\n\n"
        "🛡 <b>Модерація (Модератор+):</b>\n"
        "/mute @user [час] [причина]\n"
        "/unmute @user\n"
        "/ban @user [причина]\n"
        "/unban @user\n"
        "/warn @user [причина]\n"
        "/clear [кількість]\n\n"
        "📋 <b>Персонал:</b>\n"
        "/promote @user [роль] — підвищити (Куратор+)\n"
        "/demote @user [роль] — понизити (ГА+)\n"
        "/reprimand @user [причина] — догана (Куратор+)\n"
        "/vacation [дати] — заявка на відпустку\n"
        "/norms — перевірити норми\n\n"
        "⚙️ <b>Адмін:</b>\n"
        "/admin — панель адміна\n"
        "/backup — резервна копія\n"
        "/setpoints @user [кількість] — вручну змінити бали\n"
        "/bp_create — створити новий сезон BP\n"
        "/bp_tasks — керувати завданнями BP\n"
        "/bp_rewards — нагороди BP\n"
    )
    await message.answer(text)

# ═══════════════════════════════════════════════
#  /PROFILE
# ═══════════════════════════════════════════════
@dp.message(Command("profile"))
async def cmd_profile(message: Message, command: CommandObject):
    db = load_db()
    viewer_id  = str(message.from_user.id)
    target_id  = viewer_id

    if command.args:
        mention = command.args.split()[0].lower()
        found = next(
            (uid for uid, d in db["users"].items() if d["tag"] == mention or uid == mention),
            None
        )
        if found:
            target_id = found
        else:
            await message.answer("❌ Користувача не знайдено.")
            return

    u        = get_user(db, target_id, message.from_user if target_id == viewer_id else None)
    is_owner = viewer_id == target_id
    viewer_u = get_user(db, viewer_id, message.from_user)

    vacation_str = ""
    if u.get("vacation"):
        vacation_str = f"\n🏖 <b>Відпустка:</b> {u['vacation']}"

    reprimand_str = ""
    if u.get("reprimands", 0) > 0:
        reprimand_str = f"\n⚠️ <b>Догани:</b> {u['reprimands']}"

    xp_bar_filled = int((u["xp"] / XP_PER_LEVEL) * 10)
    xp_bar = "█" * xp_bar_filled + "░" * (10 - xp_bar_filled)

    text = (
        f"╔══════════════════════╗\n"
        f"  👤 <b>{u['nick']}</b>\n"
        f"  🎖 <code>{u['role']}</code>\n"
        f"╚══════════════════════╝\n\n"
        f"🔹 <b>Telegram:</b> {u['tag']}\n"
        f"🎮 <b>Discord:</b> <code>{u['discord']}</code>\n"
        f"📝 <b>Біо:</b> {u['bio']}\n"
        f"⚧ <b>Стать:</b> {u['sex']}  🎂 <b>Вік:</b> {u['age']}\n\n"
        f"──────────────────────\n"
        f"💰 <b>Бали:</b> {u['points']}\n"
        f"⭐ <b>Репутація:</b> {u.get('reputation', 0)}\n"
        f"🏆 <b>BP Рівень:</b> {u['level']}\n"
        f"📊 [{xp_bar}] {u['xp']}/{XP_PER_LEVEL} XP\n"
        f"──────────────────────\n"
        f"📅 <b>В команді з:</b> {u['reg_date']}"
        f"{vacation_str}{reprimand_str}"
    )
    save_db(db)
    await message.answer(
        text,
        reply_markup=profile_markup(target_id, is_owner, ROLES.get(viewer_u["role"], 0))
    )

# ═══════════════════════════════════════════════
#  РЕДАГУВАННЯ ПРОФІЛЮ (FSM + Callbacks)
# ═══════════════════════════════════════════════
@dp.message(Command("editprofile"))
@dp.callback_query(F.data == "edit_menu")
async def edit_menu_handler(event):
    msg = event.message if isinstance(event, CallbackQuery) else event
    await msg.answer("⚙️ <b>Налаштування профілю:</b>", reply_markup=edit_menu_markup())
    if isinstance(event, CallbackQuery):
        await event.answer()

@dp.callback_query(F.data == "set_n")
async def set_n(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.edit_nick)
    await call.message.answer("⌨️ Введіть новий нікнейм (макс. 25 символів):")
    await call.answer()

@dp.callback_query(F.data == "set_d")
async def set_d(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.edit_discord)
    await call.message.answer("🎮 Введіть ваш Discord username:")
    await call.answer()

@dp.callback_query(F.data == "set_b")
async def set_b(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.edit_bio)
    await call.message.answer("📝 Введіть біографію (макс. 150 символів):")
    await call.answer()

@dp.callback_query(F.data == "set_s")
async def set_s(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.edit_sex)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="👨 Чоловік",   callback_data="sex_male"),
            InlineKeyboardButton(text="👩 Жінка",     callback_data="sex_female"),
            InlineKeyboardButton(text="🏳 N/A",       callback_data="sex_na"),
        ]
    ])
    await call.message.answer("⚧ Виберіть стать:", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("sex_"))
async def process_sex(call: CallbackQuery, state: FSMContext):
    mapping = {"sex_male": "Чоловік", "sex_female": "Жінка", "sex_na": "N/A"}
    val = mapping.get(call.data, "N/A")
    db = load_db()
    db["users"][str(call.from_user.id)]["sex"] = val
    save_db(db)
    await state.clear()
    await call.message.answer(f"✅ Стать встановлено: <b>{val}</b>")
    await call.answer()

@dp.callback_query(F.data == "set_a")
async def set_a(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.edit_age)
    await call.message.answer("🎂 Введіть ваш вік (число):")
    await call.answer()

# FSM обробники введення
@dp.message(Form.edit_nick)
async def process_nick(message: Message, state: FSMContext):
    db = load_db()
    db["users"][str(message.from_user.id)]["nick"] = message.text[:25]
    save_db(db)
    await state.clear()
    await message.answer(f"✅ Нік змінено на <b>{message.text[:25]}</b>!")

@dp.message(Form.edit_discord)
async def process_discord(message: Message, state: FSMContext):
    db = load_db()
    db["users"][str(message.from_user.id)]["discord"] = message.text[:50]
    save_db(db)
    await state.clear()
    await message.answer(f"✅ Discord встановлено: <code>{message.text[:50]}</code>")

@dp.message(Form.edit_bio)
async def process_bio(message: Message, state: FSMContext):
    db = load_db()
    db["users"][str(message.from_user.id)]["bio"] = message.text[:150]
    save_db(db)
    await state.clear()
    await message.answer("✅ Біографію оновлено!")

@dp.message(Form.edit_age)
async def process_age(message: Message, state: FSMContext):
    if not message.text.isdigit() or not (10 <= int(message.text) <= 80):
        return await message.answer("❌ Введіть правильний вік (10–80).")
    db = load_db()
    db["users"][str(message.from_user.id)]["age"] = message.text
    save_db(db)
    await state.clear()
    await message.answer(f"✅ Вік встановлено: <b>{message.text}</b>")

# Швидкі команди редагування
@dp.message(Command("setnick"))
async def cmd_setnick(message: Message, command: CommandObject):
    if not command.args:
        return await message.answer("📝: <code>/setnick [новий нік]</code>")
    db = load_db()
    nick = command.args[:25]
    db["users"][str(message.from_user.id)]["nick"] = nick
    save_db(db)
    await message.answer(f"✅ Нік змінено на <b>{nick}</b>!")

@dp.message(Command("setbio"))
async def cmd_setbio(message: Message, command: CommandObject):
    if not command.args:
        return await message.answer("📝: <code>/setbio [текст біо]</code>")
    db = load_db()
    bio = command.args[:150]
    db["users"][str(message.from_user.id)]["bio"] = bio
    save_db(db)
    await message.answer("✅ Біографію оновлено!")

# ═══════════════════════════════════════════════
#  /STATS
# ═══════════════════════════════════════════════
@dp.message(Command("stats"))
@dp.callback_query(F.data.startswith("stats_"))
async def cmd_stats(event):
    is_cb = isinstance(event, CallbackQuery)
    message = event.message if is_cb else event
    user_id = event.from_user.id

    db = load_db()

    if is_cb:
        target_id = event.data.split("_")[1]
    else:
        cmd_args = event.text.split()[1:]
        if cmd_args:
            mention = cmd_args[0].lower()
            target_id = next(
                (uid for uid, d in db["users"].items() if d["tag"] == mention),
                str(user_id)
            )
        else:
            target_id = str(user_id)

    u = get_user(db, target_id)
    s = u["stats"]

    # Норми
    norms = db.get("norms", {"weekly": 50, "monthly": 200})
    weekly_done  = u.get("weekly_points",  0)
    monthly_done = u.get("monthly_points", 0)
    w_pct = min(int(weekly_done  / norms["weekly"]  * 100), 100)
    m_pct = min(int(monthly_done / norms["monthly"] * 100), 100)

    text = (
        f"📊 <b>Статистика: {u['nick']}</b>\n\n"
        f"🔇 <b>Мути:</b> {s['mutes']}\n"
        f"🔨 <b>Бани:</b> {s['bans']}\n"
        f"⚠️ <b>Попередження:</b> {s['warns']}\n"
        f"🤝 <b>Допомога:</b> {s['help']}\n"
        f"🎫 <b>Тікети:</b> {s['tickets']}\n"
        f"📋 <b>Скарги:</b> {s['complaints']}\n\n"
        f"📈 <b>Норми:</b>\n"
        f"  Тижнева: {weekly_done}/{norms['weekly']} ({w_pct}%)\n"
        f"  Місячна: {monthly_done}/{norms['monthly']} ({m_pct}%)\n\n"
        f"💰 Бали: {u['points']}  |  ⭐ Репутація: {u.get('reputation',0)}\n"
        f"🏆 BP Рівень: {u['level']}  |  🎯 XP: {u['xp']}/{XP_PER_LEVEL}"
    )
    await message.answer(text)
    if is_cb:
        await event.answer()

# ═══════════════════════════════════════════════
#  /TOP  (з пагінацією)
# ═══════════════════════════════════════════════
PAGE_SIZE = 10

def build_top_text(db, page=0) -> tuple[str, InlineKeyboardMarkup]:
    users = sorted(db["users"].items(), key=lambda x: x[1]["points"], reverse=True)
    total_pages = max(1, (len(users) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    chunk = users[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    text = f"📊 <b>ТОП ПЕРСОНАЛУ</b> (стор. {page+1}/{total_pages})\n\n"
    for i, (uid, d) in enumerate(chunk):
        pos   = page * PAGE_SIZE + i
        medal = medals.get(pos, f"{pos+1}.")
        role_abbr = d["role"][:2]
        text += f"{medal} <b>{d['nick']}</b> [{role_abbr}] — {d['points']} 💰\n"

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"top_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"top_{page+1}"))

    kb = InlineKeyboardMarkup(inline_keyboard=[nav] if nav else [])
    return text, kb

@dp.message(Command("top"))
async def cmd_top(message: Message):
    db = load_db()
    text, kb = build_top_text(db, 0)
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("top_"))
async def top_page(call: CallbackQuery):
    page = int(call.data.split("_")[1])
    db = load_db()
    text, kb = build_top_text(db, page)
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()

# ═══════════════════════════════════════════════
#  МОДЕРАЦІЯ
# ═══════════════════════════════════════════════
def _require_role(min_role: str):
    """Декоратор-обгортка для перевірки ролі."""
    async def check(message: Message) -> bool:
        db = load_db()
        u = get_user(db, message.from_user.id, message.from_user)
        if ROLES.get(u["role"], 0) < ROLES[min_role]:
            await message.answer(f"❌ Потрібна роль: <b>{min_role}</b> або вище.")
            return False
        return True
    return check

async def _find_target(db, mention: str) -> tuple[str | None, dict | None]:
    mention = mention.lower().lstrip("@")
    for uid, d in db["users"].items():
        if d["tag"].lstrip("@") == mention or uid == mention:
            return uid, d
    return None, None

async def _mod_action(message: Message, command: CommandObject, action: str, min_role="Модератор"):
    db   = load_db()
    admin = get_user(db, message.from_user.id, message.from_user)

    if ROLES.get(admin["role"], 0) < ROLES[min_role]:
        await message.answer(f"❌ Потрібна роль: <b>{min_role}</b> або вище.")
        return

    if not command.args:
        await message.answer(f"📝 Використання: <code>/{action} @user [причина]</code>")
        return

    args   = command.args.split(None, 1)
    target_tag = args[0].lower()
    reason = args[1] if len(args) > 1 else "Без причини"

    tid, tu = await _find_target(db, target_tag)
    if not tu:
        await message.answer("❌ Користувача не знайдено в базі.")
        return

    action_labels = {
        "mute":   ("🔇 Замучено",         "mutes"),
        "unmute": ("🔊 Розмучено",         None),
        "ban":    ("🔨 Забанено",          "bans"),
        "unban":  ("✅ Розбанено",         None),
        "warn":   ("⚠️ Попереджено",       "warns"),
    }
    label, stat_key = action_labels.get(action, (action, None))

    if stat_key:
        admin["stats"][stat_key] += 1
        add_points(admin, action)

    new_ach = check_achievements(admin)
    save_db(db)
    log_action(message.from_user.id, action, tid, reason)

    resp = (
        f"{label}!\n"
        f"👤 Хто: {tu['nick']} ({target_tag})\n"
        f"📝 Причина: {reason}\n"
        f"👮 Адмін: {admin['nick']}"
    )
    if stat_key:
        pts = POINTS_TABLE[action]
        resp += f"\n💰 +{pts['points']} балів  ⚡ +{pts['xp']} XP"
    if new_ach:
        resp += "\n\n🏅 Нові досягнення:\n" + "\n".join(new_ach)

    await message.answer(resp)

@dp.message(Command("mute"))
async def cmd_mute(message: Message, command: CommandObject):
    await _mod_action(message, command, "mute")

@dp.message(Command("unmute"))
async def cmd_unmute(message: Message, command: CommandObject):
    await _mod_action(message, command, "unmute")

@dp.message(Command("ban"))
async def cmd_ban(message: Message, command: CommandObject):
    await _mod_action(message, command, "ban")

@dp.message(Command("unban"))
async def cmd_unban(message: Message, command: CommandObject):
    await _mod_action(message, command, "unban")

@dp.message(Command("warn"))
async def cmd_warn(message: Message, command: CommandObject):
    await _mod_action(message, command, "warn")

@dp.message(Command("clear"))
async def cmd_clear(message: Message, command: CommandObject):
    db    = load_db()
    admin = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(admin["role"], 0) < ROLES["Модератор"]:
        return await message.answer("❌ Потрібна роль: <b>Модератор</b>.")

    count = int(command.args or 10)
    count = min(count, 100)

    try:
        deleted = await bot.delete_messages(
            message.chat.id,
            [message.message_id - i for i in range(count)]
        )
    except TelegramBadRequest:
        pass

    add_points(admin, "clear")
    admin["stats"].setdefault("cleared", 0)
    admin["stats"]["cleared"] = admin["stats"].get("cleared", 0) + count
    save_db(db)
    log_action(message.from_user.id, "clear", details=f"count={count}")
    await message.answer(f"🧹 Видалено до {count} повідомлень.")

# ═══════════════════════════════════════════════
#  ПІДВИЩЕННЯ / ПОНИЖЕННЯ РОЛІ
# ═══════════════════════════════════════════════
@dp.message(Command("promote"))
async def cmd_promote(message: Message, command: CommandObject):
    db    = load_db()
    admin = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(admin["role"], 0) < ROLES["Куратор модерації"]:
        return await message.answer("❌ Потрібна роль: <b>Куратор модерації</b>.")

    if not command.args or len(command.args.split()) < 2:
        roles_list = "\n".join(ROLES.keys())
        return await message.answer(f"📝: <code>/promote @user [роль]</code>\nРолі:\n{roles_list}")

    args  = command.args.split(None, 1)
    tag   = args[0].lower()
    role  = args[1].strip()

    if role not in ROLES:
        return await message.answer("❌ Невірна роль.")

    tid, tu = await _find_target(db, tag)
    if not tu:
        return await message.answer("❌ Користувача не знайдено.")

    if ROLES[role] >= ROLES.get(admin["role"], 0):
        return await message.answer("❌ Ви не можете призначити роль вищу або рівну своїй.")

    old_role = tu["role"]
    tu["role"] = role
    save_db(db)
    log_action(message.from_user.id, "promote", tid, f"{old_role} → {role}")
    await message.answer(f"✅ {tu['nick']} підвищено: <b>{old_role}</b> → <b>{role}</b>")

@dp.message(Command("demote"))
async def cmd_demote(message: Message, command: CommandObject):
    db    = load_db()
    admin = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(admin["role"], 0) < ROLES["Головний адміністратор"]:
        return await message.answer("❌ Потрібна роль: <b>Головний адміністратор</b>.")

    if not command.args or len(command.args.split()) < 2:
        return await message.answer("📝: <code>/demote @user [нова роль]</code>")

    args = command.args.split(None, 1)
    tag  = args[0].lower()
    role = args[1].strip()

    if role not in ROLES:
        return await message.answer("❌ Невірна роль.")

    tid, tu = await _find_target(db, tag)
    if not tu:
        return await message.answer("❌ Користувача не знайдено.")

    old_role = tu["role"]
    tu["role"] = role
    save_db(db)
    log_action(message.from_user.id, "demote", tid, f"{old_role} → {role}")
    await message.answer(f"✅ {tu['nick']} понижено: <b>{old_role}</b> → <b>{role}</b>")

# ═══════════════════════════════════════════════
#  ДОГАНИ
# ═══════════════════════════════════════════════
@dp.message(Command("reprimand"))
async def cmd_reprimand(message: Message, command: CommandObject):
    db    = load_db()
    admin = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(admin["role"], 0) < ROLES["Куратор модерації"]:
        return await message.answer("❌ Потрібна роль: <b>Куратор модерації</b>.")

    if not command.args or len(command.args.split()) < 2:
        return await message.answer("📝: <code>/reprimand @user [причина]</code>")

    args   = command.args.split(None, 1)
    tag    = args[0].lower()
    reason = args[1]

    tid, tu = await _find_target(db, tag)
    if not tu:
        return await message.answer("❌ Користувача не знайдено.")

    tu["reprimands"] = tu.get("reprimands", 0) + 1
    db["reprimands"].append({
        "ts":     datetime.now().isoformat(),
        "admin":  str(message.from_user.id),
        "target": tid,
        "reason": reason,
    })
    save_db(db)
    log_action(message.from_user.id, "reprimand", tid, reason)
    await message.answer(
        f"⚠️ <b>Догану видано!</b>\n"
        f"👤 {tu['nick']} — догана #{tu['reprimands']}\n"
        f"📝 Причина: {reason}"
    )

# ═══════════════════════════════════════════════
#  ЩОДЕННИЙ БОНУС
# ═══════════════════════════════════════════════
@dp.message(Command("daily"))
async def cmd_daily(message: Message):
    db  = load_db()
    u   = get_user(db, message.from_user.id, message.from_user)
    now = datetime.now().strftime("%d.%m.%Y")

    if u.get("daily_last") == now:
        next_time = (datetime.now().replace(hour=0, minute=0, second=0) + timedelta(days=1))
        left = next_time - datetime.now()
        h, rem = divmod(int(left.total_seconds()), 3600)
        m = rem // 60
        await message.answer(f"⏳ Бонус вже отримано сьогодні!\nНаступний через: <b>{h}г {m}хв</b>")
        return

    u["daily_last"] = now
    u["points"] += DAILY_BONUS
    u["xp"]     += 30
    while u["xp"] >= XP_PER_LEVEL:
        u["xp"]   -= XP_PER_LEVEL
        u["level"] += 1

    save_db(db)
    await message.answer(
        f"🎁 <b>Щоденний бонус отримано!</b>\n"
        f"💰 +{DAILY_BONUS} балів  ⚡ +30 XP\n\n"
        f"Загальна кількість балів: <b>{u['points']}</b>"
    )

# ═══════════════════════════════════════════════
#  ЗАЯВКА НА БАЛИ (/claimpoints)
# ═══════════════════════════════════════════════
@dp.message(Command("claimpoints"))
@dp.callback_query(F.data == "claim_start")
async def claim_start(event, state: FSMContext):
    msg = event.message if isinstance(event, CallbackQuery) else event
    await state.set_state(Form.claim_reason)
    await msg.answer("📥 <b>Заявка на нарахування балів</b>\n\nВведіть причину (що ви зробили):")
    if isinstance(event, CallbackQuery):
        await event.answer()

@dp.message(Form.claim_reason)
async def claim_reason_handler(message: Message, state: FSMContext):
    await state.update_data(reason=message.text)
    await state.set_state(Form.claim_points)
    await message.answer("💰 Скільки балів ви хочете отримати?")

@dp.message(Form.claim_points)
async def claim_points_handler(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        return await message.answer("❌ Введіть позитивне число.")
    await state.update_data(points=int(message.text))
    await state.set_state(Form.claim_proof)
    await message.answer("📎 Надішліть посилання або скріншот (або напишіть текст з доказами):")

@dp.message(Form.claim_proof)
async def claim_proof_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    db   = load_db()
    u    = get_user(db, message.from_user.id, message.from_user)

    # Перевірка на дублікати (не частіше ніж раз в 10 хв)
    now = datetime.now().timestamp()
    existing = [c for c in db["claims"] if c["uid"] == str(message.from_user.id)]
    if existing:
        last_ts = existing[-1].get("ts_raw", 0)
        if now - last_ts < 600:
            await state.clear()
            return await message.answer("❌ Зачекайте 10 хвилин між заявками.")

    claim = {
        "id":      len(db["claims"]) + 1,
        "uid":     str(message.from_user.id),
        "nick":    u["nick"],
        "reason":  data["reason"],
        "points":  data["points"],
        "proof":   message.text or "[медіафайл]",
        "status":  "pending",
        "ts":      datetime.now().strftime("%d.%m.%Y %H:%M"),
        "ts_raw":  now,
    }
    db["claims"].append(claim)
    save_db(db)
    await state.clear()

    # Сповіщення кураторів
    curators = [
        uid for uid, d in db["users"].items()
        if ROLES.get(d["role"], 0) >= ROLES["Куратор модерації"]
    ]
    notify_text = (
        f"📥 <b>Нова заявка на бали #{claim['id']}</b>\n"
        f"👤 {u['nick']} ({u['tag']})\n"
        f"💰 Запитує: {data['points']} балів\n"
        f"📝 Причина: {data['reason']}\n"
        f"📎 Докази: {claim['proof']}"
    )
    approve_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Схвалити", callback_data=f"claim_ok_{claim['id']}"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data=f"claim_no_{claim['id']}"),
    ]])
    for cuid in curators:
        try:
            await bot.send_message(int(cuid), notify_text, reply_markup=approve_kb)
        except Exception:
            pass

    await message.answer(
        f"✅ Заявку #{claim['id']} надіслано!\n"
        "Очікуйте підтвердження від куратора."
    )

@dp.callback_query(F.data.startswith("claim_ok_"))
async def claim_approve(call: CallbackQuery):
    db = load_db()
    curator = get_user(db, call.from_user.id, call.from_user)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Немає прав.", show_alert=True)

    cid = int(call.data.split("_")[-1])
    claim = next((c for c in db["claims"] if c["id"] == cid), None)
    if not claim or claim["status"] != "pending":
        return await call.answer("Заявка вже оброблена.", show_alert=True)

    claim["status"] = "approved"
    tu = db["users"].get(claim["uid"])
    if tu:
        tu["points"] += claim["points"]
        tu["xp"]     += claim["points"] * 3
        while tu["xp"] >= XP_PER_LEVEL:
            tu["xp"]   -= XP_PER_LEVEL
            tu["level"] += 1

    save_db(db)
    log_action(call.from_user.id, "claim_approve", claim["uid"], str(claim["points"]))
    await call.message.edit_text(
        call.message.text + f"\n\n✅ <b>Схвалено</b> куратором {curator['nick']}"
    )
    try:
        await bot.send_message(
            int(claim["uid"]),
            f"🎉 Заявку #{cid} схвалено!\n💰 +{claim['points']} балів нараховано."
        )
    except Exception:
        pass
    await call.answer("✅ Схвалено!")

@dp.callback_query(F.data.startswith("claim_no_"))
async def claim_reject(call: CallbackQuery):
    db = load_db()
    curator = get_user(db, call.from_user.id, call.from_user)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Немає прав.", show_alert=True)

    cid = int(call.data.split("_")[-1])
    claim = next((c for c in db["claims"] if c["id"] == cid), None)
    if not claim or claim["status"] != "pending":
        return await call.answer("Заявка вже оброблена.", show_alert=True)

    claim["status"] = "rejected"
    save_db(db)
    await call.message.edit_text(
        call.message.text + f"\n\n❌ <b>Відхилено</b> куратором {curator['nick']}"
    )
    try:
        await bot.send_message(
            int(claim["uid"]),
            f"❌ Заявку #{cid} відхилено."
        )
    except Exception:
        pass
    await call.answer("❌ Відхилено!")

# ═══════════════════════════════════════════════
#  ЗАЯВКА НА ПІДВИЩЕННЯ
# ═══════════════════════════════════════════════
@dp.message(Command("promotion"))
@dp.callback_query(F.data == "promo_start")
async def promo_start(event, state: FSMContext):
    msg = event.message if isinstance(event, CallbackQuery) else event
    await state.set_state(Form.promo_reason)
    await msg.answer(
        "📈 <b>Заявка на підвищення</b>\n\n"
        "Напишіть, чому ви заслуговуєте підвищення (досягнення, активність, стаж):"
    )
    if isinstance(event, CallbackQuery):
        await event.answer()

@dp.message(Form.promo_reason)
async def promo_reason_handler(message: Message, state: FSMContext):
    db = load_db()
    u  = get_user(db, message.from_user.id, message.from_user)

    promo = {
        "id":     len(db["promotions"]) + 1,
        "uid":    str(message.from_user.id),
        "nick":   u["nick"],
        "role":   u["role"],
        "reason": message.text,
        "status": "pending",
        "ts":     datetime.now().strftime("%d.%m.%Y %H:%M"),
    }
    db["promotions"].append(promo)
    save_db(db)
    await state.clear()

    # Сповіщення керівництва
    leaders = [uid for uid, d in db["users"].items() if ROLES.get(d["role"], 0) >= ROLES["Головний адміністратор"]]
    notify  = (
        f"📈 <b>Заявка на підвищення #{promo['id']}</b>\n"
        f"👤 {u['nick']} ({u['tag']})\n"
        f"🎖 Поточна роль: {u['role']}\n"
        f"📝 Причина: {message.text}"
    )
    approve_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Схвалити", callback_data=f"promo_ok_{promo['id']}"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data=f"promo_no_{promo['id']}"),
    ]])
    for luid in leaders:
        try:
            await bot.send_message(int(luid), notify, reply_markup=approve_kb)
        except Exception:
            pass

    await message.answer(f"✅ Заявку #{promo['id']} надіслано на розгляд керівництву!")

@dp.callback_query(F.data.startswith("promo_ok_"))
async def promo_approve(call: CallbackQuery):
    db = load_db()
    admin = get_user(db, call.from_user.id, call.from_user)
    if ROLES.get(admin["role"], 0) < ROLES["Головний адміністратор"]:
        return await call.answer("❌ Немає прав.", show_alert=True)

    pid = int(call.data.split("_")[-1])
    promo = next((p for p in db["promotions"] if p["id"] == pid), None)
    if not promo or promo["status"] != "pending":
        return await call.answer("Вже оброблено.", show_alert=True)

    promo["status"] = "approved"
    save_db(db)
    await call.message.edit_text(call.message.text + f"\n\n✅ <b>Схвалено</b> {admin['nick']}")
    try:
        await bot.send_message(
            int(promo["uid"]),
            f"🎉 Вашу заявку на підвищення схвалено!\nЗверніться до куратора для призначення ролі."
        )
    except Exception:
        pass
    await call.answer("✅")

@dp.callback_query(F.data.startswith("promo_no_"))
async def promo_reject(call: CallbackQuery):
    db = load_db()
    admin = get_user(db, call.from_user.id, call.from_user)
    if ROLES.get(admin["role"], 0) < ROLES["Головний адміністратор"]:
        return await call.answer("❌ Немає прав.", show_alert=True)

    pid = int(call.data.split("_")[-1])
    promo = next((p for p in db["promotions"] if p["id"] == pid), None)
    if not promo or promo["status"] != "pending":
        return await call.answer("Вже оброблено.", show_alert=True)

    promo["status"] = "rejected"
    save_db(db)
    await call.message.edit_text(call.message.text + f"\n\n❌ <b>Відхилено</b> {admin['nick']}")
    try:
        await bot.send_message(int(promo["uid"]), "❌ Вашу заявку на підвищення відхилено.")
    except Exception:
        pass
    await call.answer("❌")

# ═══════════════════════════════════════════════
#  ВІДПУСТКА
# ═══════════════════════════════════════════════
@dp.message(Command("vacation"))
@dp.callback_query(F.data == "vacation_menu")
async def vacation_start(event, state: FSMContext):
    msg = event.message if isinstance(event, CallbackQuery) else event
    kb  = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📅 Подати заявку",  callback_data="vacation_apply"),
        InlineKeyboardButton(text="🔙 Зняти відпустку", callback_data="vacation_cancel"),
    ]])
    await msg.answer("🏖 <b>Система відпусток</b>", reply_markup=kb)
    if isinstance(event, CallbackQuery):
        await event.answer()

@dp.callback_query(F.data == "vacation_apply")
async def vacation_apply(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.vacation_dates)
    await call.message.answer("📅 Введіть дати відпустки (напр.: 01.07 — 15.07):")
    await call.answer()

@dp.message(Form.vacation_dates)
async def vacation_dates_handler(message: Message, state: FSMContext):
    await state.update_data(dates=message.text)
    await state.set_state(Form.vacation_reason)
    await message.answer("📝 Причина відпустки (необов'язково, можна написати «-»):")

@dp.message(Form.vacation_reason)
async def vacation_reason_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    db   = load_db()
    u    = get_user(db, message.from_user.id, message.from_user)

    vac = {
        "id":     len(db["vacations"]) + 1,
        "uid":    str(message.from_user.id),
        "nick":   u["nick"],
        "dates":  data["dates"],
        "reason": message.text,
        "status": "pending",
        "ts":     datetime.now().strftime("%d.%m.%Y %H:%M"),
    }
    db["vacations"].append(vac)
    save_db(db)
    await state.clear()

    curators = [uid for uid, d in db["users"].items() if ROLES.get(d["role"], 0) >= ROLES["Куратор модерації"]]
    notify = (
        f"🏖 <b>Заявка на відпустку #{vac['id']}</b>\n"
        f"👤 {u['nick']} ({u['tag']})\n"
        f"📅 Дати: {data['dates']}\n"
        f"📝 Причина: {message.text}"
    )
    approve_kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Схвалити", callback_data=f"vac_ok_{vac['id']}"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data=f"vac_no_{vac['id']}"),
    ]])
    for cuid in curators:
        try:
            await bot.send_message(int(cuid), notify, reply_markup=approve_kb)
        except Exception:
            pass

    await message.answer(f"✅ Заявку на відпустку #{vac['id']} надіслано!")

@dp.callback_query(F.data.startswith("vac_ok_"))
async def vac_approve(call: CallbackQuery):
    db = load_db()
    curator = get_user(db, call.from_user.id, call.from_user)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Немає прав.", show_alert=True)

    vid = int(call.data.split("_")[-1])
    vac = next((v for v in db["vacations"] if v["id"] == vid), None)
    if not vac or vac["status"] != "pending":
        return await call.answer("Вже оброблено.", show_alert=True)

    vac["status"] = "approved"
    tu = db["users"].get(vac["uid"])
    if tu:
        tu["vacation"] = vac["dates"]
    save_db(db)
    await call.message.edit_text(call.message.text + f"\n\n✅ <b>Схвалено</b> {curator['nick']}")
    try:
        await bot.send_message(int(vac["uid"]), f"🏖 Відпустку схвалено: {vac['dates']}")
    except Exception:
        pass
    await call.answer("✅")

@dp.callback_query(F.data.startswith("vac_no_"))
async def vac_reject(call: CallbackQuery):
    db = load_db()
    curator = get_user(db, call.from_user.id, call.from_user)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Немає прав.", show_alert=True)

    vid = int(call.data.split("_")[-1])
    vac = next((v for v in db["vacations"] if v["id"] == vid), None)
    if not vac or vac["status"] != "pending":
        return await call.answer("Вже оброблено.", show_alert=True)

    vac["status"] = "rejected"
    save_db(db)
    await call.message.edit_text(call.message.text + f"\n\n❌ <b>Відхилено</b> {curator['nick']}")
    try:
        await bot.send_message(int(vac["uid"]), "❌ Заявку на відпустку відхилено.")
    except Exception:
        pass
    await call.answer("❌")

@dp.callback_query(F.data == "vacation_cancel")
async def vacation_cancel(call: CallbackQuery):
    db = load_db()
    u  = get_user(db, call.from_user.id, call.from_user)
    u["vacation"] = None
    save_db(db)
    await call.message.answer("✅ Відпустку знято.")
    await call.answer()

# ═══════════════════════════════════════════════
#  BATTLE PASS
# ═══════════════════════════════════════════════
@dp.message(Command("bp"))
@dp.callback_query(F.data == "bp_view")
async def cmd_bp(event):
    is_cb = isinstance(event, CallbackQuery)
    message = event.message if is_cb else event
    uid = str(event.from_user.id)

    db  = load_db()
    u   = get_user(db, uid, event.from_user if not is_cb else None)
    bp  = db["bp"]

    tasks = bp.get("tasks", [])
    done  = u.get("bp_tasks_done", [])
    rewards = bp.get("rewards", {})

    text = (
        f"🏆 <b>BATTLE PASS — {bp.get('name', 'Сезон ' + str(bp['season']))}</b>\n\n"
        f"🎯 <b>Твій рівень:</b> {u['level']}\n"
        f"⚡ <b>XP:</b> {u['xp']}/{XP_PER_LEVEL}\n\n"
    )

    if tasks:
        text += "📋 <b>Завдання сезону:</b>\n"
        for i, t in enumerate(tasks):
            status = "✅" if i in done else "⬜"
            text += f"{status} {t['text']} (+{t['xp']} XP)\n"
    else:
        text += "📋 Завдань ще немає.\n"

    if rewards:
        text += "\n🎁 <b>Нагороди:</b>\n"
        for lvl, rew in sorted(rewards.items(), key=lambda x: int(x[0])):
            claimed = u["level"] >= int(lvl)
            icon = "✅" if claimed else "🔒"
            text += f"{icon} Рівень {lvl}: {rew}\n"

    kb = None
    if tasks:
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=f"📌 Виконати завдання #{i+1}", callback_data=f"bp_task_{i}")
            for i in range(min(3, len(tasks))) if i not in done
        ]])

    save_db(db)
    await message.answer(text, reply_markup=kb)
    if is_cb:
        await event.answer()

@dp.callback_query(F.data.startswith("bp_task_"))
async def bp_complete_task(call: CallbackQuery):
    idx = int(call.data.split("_")[-1])
    db  = load_db()
    u   = get_user(db, call.from_user.id, call.from_user)

    if idx in u.get("bp_tasks_done", []):
        return await call.answer("Вже виконано!", show_alert=True)

    tasks = db["bp"].get("tasks", [])
    if idx >= len(tasks):
        return await call.answer("Завдання не знайдено.", show_alert=True)

    task = tasks[idx]
    u.setdefault("bp_tasks_done", []).append(idx)
    u["xp"] += task["xp"]
    while u["xp"] >= XP_PER_LEVEL:
        u["xp"]   -= XP_PER_LEVEL
        u["level"] += 1

    save_db(db)
    await call.answer(f"✅ +{task['xp']} XP!", show_alert=True)

# BP адмін команди
@dp.message(Command("bp_create"))
async def bp_create(message: Message, state: FSMContext):
    db = load_db()
    u  = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(u["role"], 0) < ROLES["Головний адміністратор"]:
        return await message.answer("❌ Потрібна роль: <b>Головний адміністратор</b>.")
    await state.set_state(Form.bp_create_name)
    await message.answer("🏆 Введіть назву нового сезону Battle Pass:")

@dp.message(Form.bp_create_name)
async def bp_create_name(message: Message, state: FSMContext):
    db = load_db()
    db["bp"]["season"] += 1
    db["bp"]["name"]    = message.text
    db["bp"]["tasks"]   = []
    db["bp"]["rewards"] = {}
    # Скидаємо прогрес усіх
    for u in db["users"].values():
        u["bp_tasks_done"] = []
        u["level"]         = 1
        u["xp"]            = 0
    save_db(db)
    await state.clear()
    await message.answer(f"✅ Новий сезон <b>{message.text}</b> розпочато! Прогрес скинуто.")

@dp.message(Command("bp_tasks"))
async def bp_tasks_cmd(message: Message, command: CommandObject):
    db = load_db()
    u  = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(u["role"], 0) < ROLES["Куратор модерації"]:
        return await message.answer("❌ Немає прав.")

    if not command.args:
        tasks = db["bp"].get("tasks", [])
        if not tasks:
            return await message.answer("📋 Завдань немає.\n/bp_tasks [текст] [xp] — додати завдання")
        text = "📋 <b>Завдання BP:</b>\n\n"
        for i, t in enumerate(tasks):
            text += f"{i+1}. {t['text']} (+{t['xp']} XP)\n"
        return await message.answer(text)

    parts = command.args.rsplit(None, 1)
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.answer("📝: <code>/bp_tasks [текст завдання] [xp]</code>")

    db["bp"].setdefault("tasks", []).append({
        "text": parts[0],
        "xp":   int(parts[1]),
    })
    save_db(db)
    await message.answer(f"✅ Завдання додано: <b>{parts[0]}</b> (+{parts[1]} XP)")

@dp.message(Command("bp_rewards"))
async def bp_rewards_cmd(message: Message, command: CommandObject):
    db = load_db()
    u  = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(u["role"], 0) < ROLES["Куратор модерації"]:
        return await message.answer("❌ Немає прав.")

    if not command.args:
        rewards = db["bp"].get("rewards", {})
        if not rewards:
            return await message.answer("🎁 Нагород немає.\n/bp_rewards [рівень] [нагорода]")
        text = "🎁 <b>Нагороди BP:</b>\n"
        for lvl, rew in sorted(rewards.items(), key=lambda x: int(x[0])):
            text += f"Рівень {lvl}: {rew}\n"
        return await message.answer(text)

    parts = command.args.split(None, 1)
    if len(parts) < 2 or not parts[0].isdigit():
        return await message.answer("📝: <code>/bp_rewards [рівень] [нагорода]</code>")

    db["bp"].setdefault("rewards", {})[parts[0]] = parts[1]
    save_db(db)
    await message.answer(f"✅ Нагороду рівня {parts[0]} встановлено: <b>{parts[1]}</b>")

# ═══════════════════════════════════════════════
#  ДОСЯГНЕННЯ
# ═══════════════════════════════════════════════
ACH_LABELS = {
    "first_mute":    "🔇 Перший мут",
    "mute_10":       "🔇 10 мутів",
    "ban_5":         "🔨 5 банів",
    "help_100":      "🤝 100 допомог",
    "tickets_50":    "🎫 50 тікетів",
    "points_100":    "💰 100 балів",
    "points_500":    "💰 500 балів",
    "points_1000":   "💰 1000 балів",
    "complaints_10": "📋 10 скарг",
}

@dp.message(Command("achievements"))
@dp.callback_query(F.data.startswith("ach_"))
async def cmd_achievements(event):
    is_cb   = isinstance(event, CallbackQuery)
    message = event.message if is_cb else event
    db      = load_db()

    if is_cb:
        target_id = event.data.split("_")[1]
    else:
        cmd_args = event.text.split()[1:]
        if cmd_args:
            mention = cmd_args[0].lower()
            target_id = next(
                (uid for uid, d in db["users"].items() if d["tag"] == mention),
                str(event.from_user.id)
            )
        else:
            target_id = str(event.from_user.id)

    u   = get_user(db, target_id)
    ach = u.get("achievements", [])

    if not ach:
        text = f"🏅 <b>{u['nick']}</b> ще не має досягнень."
    else:
        text = f"🏅 <b>Досягнення {u['nick']}:</b>\n\n"
        for key in ach:
            text += f"✅ {ACH_LABELS.get(key, key)}\n"
        text += f"\nВсього: {len(ach)}/{len(ACH_LABELS)}"

    await message.answer(text)
    if is_cb:
        await event.answer()

# ═══════════════════════════════════════════════
#  РЕПУТАЦІЯ
# ═══════════════════════════════════════════════
_rep_cooldowns: dict[str, dict[str, float]] = defaultdict(dict)

@dp.callback_query(F.data.startswith("rep_"))
async def give_reputation(call: CallbackQuery):
    giver_id  = str(call.from_user.id)
    target_id = call.data.split("_")[1]

    if giver_id == target_id:
        return await call.answer("❌ Не можна давати репутацію собі.", show_alert=True)

    now     = asyncio.get_event_loop().time()
    last_ts = _rep_cooldowns[giver_id].get(target_id, 0)
    if now - last_ts < 86400:
        return await call.answer("⏳ Можна давати репутацію раз на добу.", show_alert=True)

    db = load_db()
    tu = db["users"].get(target_id)
    if not tu:
        return await call.answer("Користувача не знайдено.", show_alert=True)

    tu["reputation"] = tu.get("reputation", 0) + 1
    _rep_cooldowns[giver_id][target_id] = now
    save_db(db)
    await call.answer(f"👍 Ви дали +1 репутацію {tu['nick']}!", show_alert=True)

# ═══════════════════════════════════════════════
#  НОРМИ
# ═══════════════════════════════════════════════
@dp.message(Command("norms"))
async def cmd_norms(message: Message):
    db    = load_db()
    u     = get_user(db, message.from_user.id, message.from_user)
    norms = db.get("norms", {"weekly": 50, "monthly": 200})

    weekly  = u.get("weekly_points",  0)
    monthly = u.get("monthly_points", 0)

    w_pct = min(int(weekly  / norms["weekly"]  * 100), 100)
    m_pct = min(int(monthly / norms["monthly"] * 100), 100)

    w_bar = "█" * (w_pct // 10) + "░" * (10 - w_pct // 10)
    m_bar = "█" * (m_pct // 10) + "░" * (10 - m_pct // 10)

    warn = ""
    if w_pct < 50:
        warn = "\n\n⚠️ <b>Увага!</b> Тижнева норма виконана менше ніж на 50%!"

    text = (
        f"📈 <b>Норми персоналу — {u['nick']}</b>\n\n"
        f"📅 <b>Тижнева:</b> {weekly}/{norms['weekly']}\n"
        f"[{w_bar}] {w_pct}%\n\n"
        f"📆 <b>Місячна:</b> {monthly}/{norms['monthly']}\n"
        f"[{m_bar}] {m_pct}%"
        f"{warn}"
    )
    await message.answer(text)

# ═══════════════════════════════════════════════
#  АДМІН ПАНЕЛЬ
# ═══════════════════════════════════════════════
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    db = load_db()
    u  = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(u["role"], 0) < ROLES["Куратор модерації"]:
        return await message.answer("❌ Доступ заборонено.")
    await message.answer("⚙️ <b>ПАНЕЛЬ АДМІНІСТРАТОРА</b>", reply_markup=admin_markup())

@dp.callback_query(F.data == "admin_claims")
async def admin_claims(call: CallbackQuery):
    db = load_db()
    admin = get_user(db, call.from_user.id, call.from_user)
    if ROLES.get(admin["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Немає прав.", show_alert=True)

    pending = [c for c in db["claims"] if c["status"] == "pending"]
    if not pending:
        await call.message.answer("📭 Немає заявок, що очікують розгляду.")
        return await call.answer()

    for c in pending[:5]:
        text = (
            f"📥 <b>Заявка #{c['id']}</b>\n"
            f"👤 {c['nick']}\n"
            f"💰 {c['points']} балів\n"
            f"📝 {c['reason']}\n"
            f"📎 {c['proof']}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅", callback_data=f"claim_ok_{c['id']}"),
            InlineKeyboardButton(text="❌", callback_data=f"claim_no_{c['id']}"),
        ]])
        await call.message.answer(text, reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "admin_promos")
async def admin_promos(call: CallbackQuery):
    db = load_db()
    pending = [p for p in db["promotions"] if p["status"] == "pending"]
    if not pending:
        await call.message.answer("📭 Немає заявок на підвищення.")
        return await call.answer()
    for p in pending[:5]:
        text = (
            f"📈 <b>Заявка #{p['id']}</b>\n"
            f"👤 {p['nick']} — {p['role']}\n"
            f"📝 {p['reason']}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅", callback_data=f"promo_ok_{p['id']}"),
            InlineKeyboardButton(text="❌", callback_data=f"promo_no_{p['id']}"),
        ]])
        await call.message.answer(text, reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "admin_vacations")
async def admin_vacations(call: CallbackQuery):
    db = load_db()
    pending = [v for v in db["vacations"] if v["status"] == "pending"]
    if not pending:
        await call.message.answer("📭 Немає заявок на відпустку.")
        return await call.answer()
    for v in pending[:5]:
        text = (
            f"🏖 <b>Заявка #{v['id']}</b>\n"
            f"👤 {v['nick']}\n"
            f"📅 {v['dates']}\n"
            f"📝 {v['reason']}"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅", callback_data=f"vac_ok_{v['id']}"),
            InlineKeyboardButton(text="❌", callback_data=f"vac_no_{v['id']}"),
        ]])
        await call.message.answer(text, reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "admin_log")
async def admin_log(call: CallbackQuery):
    db = load_db()
    admin = get_user(db, call.from_user.id, call.from_user)
    if ROLES.get(admin["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Немає прав.", show_alert=True)

    logs = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            logs = json.load(f)

    last = logs[-15:][::-1]
    if not last:
        await call.message.answer("📜 Журнал порожній.")
        return await call.answer()

    text = "📜 <b>Останні 15 дій:</b>\n\n"
    for entry in last:
        ts  = entry["ts"][:16].replace("T", " ")
        text += f"[{ts}] <b>{entry['action']}</b> → {entry.get('target','—')}\n"
    await call.message.answer(text)
    await call.answer()

@dp.callback_query(F.data == "admin_backup")
async def admin_backup_cb(call: CallbackQuery):
    db = load_db()
    admin = get_user(db, call.from_user.id, call.from_user)
    if ROLES.get(admin["role"], 0) < ROLES["Головний адміністратор"]:
        return await call.answer("❌ Немає прав.", show_alert=True)
    backup_db()
    await call.answer("💾 Backup створено!", show_alert=True)

@dp.callback_query(F.data == "admin_norms")
async def admin_norms(call: CallbackQuery):
    db = load_db()
    norms = db.get("norms", {"weekly": 50, "monthly": 200})
    await call.message.answer(
        f"⚙️ <b>Поточні норми:</b>\n"
        f"Тижнева: {norms['weekly']} балів\n"
        f"Місячна: {norms['monthly']} балів\n\n"
        f"Змінити: <code>/setnorm weekly [число]</code> або <code>/setnorm monthly [число]</code>"
    )
    await call.answer()

@dp.message(Command("setnorm"))
async def cmd_setnorm(message: Message, command: CommandObject):
    db = load_db()
    u  = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(u["role"], 0) < ROLES["Головний адміністратор"]:
        return await message.answer("❌ Немає прав.")
    if not command.args:
        return await message.answer("📝: <code>/setnorm [weekly|monthly] [число]</code>")
    parts = command.args.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.answer("❌ Невірний формат.")
    db.setdefault("norms", {})
    db["norms"][parts[0]] = int(parts[1])
    save_db(db)
    await message.answer(f"✅ Норму <b>{parts[0]}</b> встановлено: {parts[1]}")

# Ручна зміна балів адміном
@dp.message(Command("setpoints"))
async def cmd_setpoints(message: Message, command: CommandObject):
    db = load_db()
    u  = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(u["role"], 0) < ROLES["Головний адміністратор"]:
        return await message.answer("❌ Немає прав.")

    if not command.args or len(command.args.split()) < 2:
        return await message.answer("📝: <code>/setpoints @user [кількість]</code>")

    args = command.args.split()
    tag  = args[0].lower()
    pts  = args[1]
    if not pts.lstrip("-").isdigit():
        return await message.answer("❌ Введіть число.")

    tid, tu = await _find_target(db, tag)
    if not tu:
        return await message.answer("❌ Користувача не знайдено.")

    old = tu["points"]
    tu["points"] = max(0, old + int(pts))
    save_db(db)
    log_action(message.from_user.id, "setpoints", tid, f"{old} → {tu['points']}")
    await message.answer(f"✅ Бали {tu['nick']}: {old} → <b>{tu['points']}</b>")

# ═══════════════════════════════════════════════
#  BACKUP команда
# ═══════════════════════════════════════════════
@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    db = load_db()
    u  = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(u["role"], 0) < ROLES["Головний адміністратор"]:
        return await message.answer("❌ Немає прав.")
    backup_db()
    await message.answer("💾 Резервну копію збережено!")

# ═══════════════════════════════════════════════
#  ПОШУК ПРОФІЛІВ
# ═══════════════════════════════════════════════
@dp.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject):
    if not command.args:
        return await message.answer("📝: <code>/search [нік або @тег]</code>")
    query = command.args.lower()
    db    = load_db()
    found = [
        (uid, d) for uid, d in db["users"].items()
        if query in d["nick"].lower() or query in d["tag"].lower()
    ]
    if not found:
        return await message.answer("❌ Нічого не знайдено.")

    text = f"🔍 <b>Результати пошуку «{command.args}»:</b>\n\n"
    for uid, d in found[:10]:
        text += f"👤 <b>{d['nick']}</b> — {d['tag']} [{d['role']}]\n"
    if len(found) > 10:
        text += f"\n...та ще {len(found)-10} результатів"
    await message.answer(text)

# ═══════════════════════════════════════════════
#  АНТИФЛУД (middleware-подібний обробник)
# ═══════════════════════════════════════════════
@dp.message()
async def flood_guard(message: Message):
    if is_flood(message.from_user.id):
        await message.answer("⏳ Занадто багато повідомлень! Зачекайте.")

# ═══════════════════════════════════════════════
#  АВТОМАТИЧНІ ЗАВДАННЯ (scheduler)
# ═══════════════════════════════════════════════
async def weekly_reset_task():
    """Щотижневе скидання прогресу норм + нагадування."""
    while True:
        await asyncio.sleep(604800)  # 7 днів
        db = load_db()
        norms = db.get("norms", {"weekly": 50})
        for uid, u in db["users"].items():
            if u.get("weekly_points", 0) < norms["weekly"]:
                try:
                    await bot.send_message(
                        int(uid),
                        f"⚠️ <b>Тижнева норма не виконана!</b>\n"
                        f"Виконано: {u.get('weekly_points', 0)}/{norms['weekly']} балів."
                    )
                except Exception:
                    pass
            u["weekly_points"] = 0
        backup_db()
        save_db(db)

async def daily_norm_reminder():
    """Нагадування про норму кожні 48 год."""
    while True:
        await asyncio.sleep(172800)  # 2 дні
        db    = load_db()
        norms = db.get("norms", {"weekly": 50})
        for uid, u in db["users"].items():
            if u.get("weekly_points", 0) < norms["weekly"] // 2:
                try:
                    await bot.send_message(
                        int(uid),
                        f"📢 Нагадування: виконано лише "
                        f"{u.get('weekly_points',0)}/{norms['weekly']} тижневої норми."
                    )
                except Exception:
                    pass

# ═══════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════
async def main():
    logging.info("💎 UA ONLINE BOT STARTED")
    os.makedirs(BACKUP_DIR, exist_ok=True)

    # Запуск фонових задач
    asyncio.create_task(weekly_reset_task())
    asyncio.create_task(daily_norm_reminder())

    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
