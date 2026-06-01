"""
UA ONLINE BOT — v2.1
Патч v2.1:
  - [1] Причина відхилення: при натисканні "Відхилити" куратор вводить причину (FSM)
  - [2] Надсилання заявок у канал (CLAIMS_CHANNEL_ID) + fallback у ПП кураторам
  - [3] Battle Pass: завдання підтверджуються куратором (заявка з доказами, не авто)
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
TOKEN      = "8905769390:AAHA1YUTQti2_diRFLa2f9KRYGc1QKdJ4ys"   # ← Встав свій токен
ADMIN_ID   = 1188859918
DB_FILE    = "ua_online_db.json"
LOG_FILE   = "ua_online_log.json"
BACKUP_DIR = "backups"

# [ПАТЧ 2] ID каналу/групи для заявок. Якщо None — шлемо тільки в ПП кураторам.
# Встав сюди від'ємний ID каналу, напр.: -1001234567890
CLAIMS_CHANNEL_ID: int | None = None

logging.basicConfig(level=logging.INFO)

ROLES = {
    "Молодший Модератор":             0,
    "Модератор":                      1,
    "Ст. модератор":                  2,
    "Куратор Заходів":                3,
    "Куратор модерації":              4,
    "Заступник головного модератора": 5,
    "Головний модератор":             6,
}

POINTS_TABLE = {
    "mute":   {"points": 10, "xp": 50},
    "ban":    {"points": 15, "xp": 70},
    "warn":   {"points": 5,  "xp": 25},
    "unmute": {"points": 5,  "xp": 20},
    "unban":  {"points": 5,  "xp": 20},
    "help":   {"points": 8,  "xp": 40},
    "ticket": {"points": 12, "xp": 60},
    "clear":  {"points": 3,  "xp": 15},
}

XP_PER_LEVEL = 1000
DAILY_BONUS  = 20

# ═══════════════════════════════════════════════
#  FSM СТАНИ
# ═══════════════════════════════════════════════
class Form(StatesGroup):
    edit_nick    = State()
    edit_bio     = State()
    edit_discord = State()
    edit_sex     = State()
    edit_age     = State()

    mod_action   = State()
    mod_target   = State()
    mod_reason   = State()
    mod_proof    = State()

    claim_action = State()
    claim_count  = State()
    claim_proof  = State()

    promo_reason = State()

    vacation_dates  = State()
    vacation_reason = State()

    bp_create_name = State()

    # [ПАТЧ 3] BP завдання — заявка від користувача
    bp_task_idx   = State()   # збережений індекс завдання
    bp_task_proof = State()   # докази виконання

    # [ПАТЧ 1] Причина відхилення — зберігаємо тип заявки + id
    reject_type = State()     # "mc" / "claim" / "vac" / "promo" / "bp_task"
    reject_id   = State()
    reject_reason = State()

    reprimand_target = State()
    reprimand_reason = State()

# ═══════════════════════════════════════════════
#  АНТИФЛУД
# ═══════════════════════════════════════════════
_flood: dict[int, list[float]] = defaultdict(list)
FLOOD_LIMIT  = 5
FLOOD_WINDOW = 10

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
        "bp": {"season": 1, "name": "Сезон 1", "tasks": [], "rewards": {}},
        "claims": [],
        "mod_claims": [],
        "bp_task_claims": [],   # [ПАТЧ 3] заявки на BP завдання
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
    if os.path.exists(DB_FILE):
        shutil.copy(DB_FILE, f"{BACKUP_DIR}/db_{ts}.json")

def log_action(actor_id, action, target=None, details=""):
    entry = {"ts": datetime.now().isoformat(), "actor": actor_id,
             "action": action, "target": target, "details": details}
    logs = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            logs = json.load(f)
    logs.append(entry)
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(logs[-5000:], f, indent=2, ensure_ascii=False)

def get_user(db, user_id, user_obj=None) -> dict:
    uid = str(user_id)
    if uid not in db["users"]:
        db["users"][uid] = {
            "nick":       user_obj.first_name if user_obj else "Unknown",
            "tag":        f"@{user_obj.username}".lower() if user_obj and user_obj.username else "немає",
            "discord":    "Не вказано",
            "bio":        "Персонал UA ONLINE",
            "sex":        "N/A",
            "age":        "N/A",
            "role":       "Молодший Модератор",
            "points":     0,
            "xp":         0,
            "level":      1,
            "reputation": 0,
            "stats":      {"mutes": 0, "bans": 0, "warns": 0, "help": 0, "tickets": 0, "complaints": 0},
            "achievements":    [],
            "bp_tasks_done":   [],
            "daily_last":      None,
            "vacation":        None,
            "reprimands":      0,
            "weekly_points":   0,
            "monthly_points":  0,
            "reg_date":        datetime.now().strftime("%d.%m.%Y"),
        }
        if int(uid) == ADMIN_ID:
            db["users"][uid]["role"] = "Головний модератор"
    return db["users"][uid]

def add_points(u: dict, action: str):
    p = POINTS_TABLE.get(action, {"points": 0, "xp": 0})
    u["points"]          += p["points"]
    u["xp"]              += p["xp"]
    u["weekly_points"]    = u.get("weekly_points",  0) + p["points"]
    u["monthly_points"]   = u.get("monthly_points", 0) + p["points"]
    while u["xp"] >= XP_PER_LEVEL:
        u["xp"]   -= XP_PER_LEVEL
        u["level"] += 1

def check_achievements(u: dict) -> list[str]:
    earned, ach, s = [], u["achievements"], u["stats"]
    checks = [
        ("first_mute",   s["mutes"]      >= 1,    "🔇 Перший мут"),
        ("mute_10",      s["mutes"]      >= 10,   "🔇 10 мутів"),
        ("ban_5",        s["bans"]       >= 5,    "🔨 5 банів"),
        ("help_100",     s["help"]       >= 100,  "🤝 100 допомог"),
        ("tickets_50",   s["tickets"]    >= 50,   "🎫 50 тікетів"),
        ("points_100",   u["points"]     >= 100,  "💰 100 балів"),
        ("points_500",   u["points"]     >= 500,  "💰 500 балів"),
        ("points_1000",  u["points"]     >= 1000, "💰 1000 балів"),
        ("complaints_10",s["complaints"] >= 10,   "📋 10 скарг"),
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

async def _find_target(db, mention: str):
    mention = mention.lower().lstrip("@")
    for uid, d in db["users"].items():
        if d["tag"].lstrip("@") == mention or uid == mention:
            return uid, d
    return None, None

# ═══════════════════════════════════════════════
#  [ПАТЧ 2] НАДСИЛАННЯ ЗАЯВОК У КАНАЛ + ПП
#  Якщо CLAIMS_CHANNEL_ID задано — шлемо в канал.
#  Завжди також шлемо в ПП кураторам (для кнопок).
# ═══════════════════════════════════════════════
async def _send_claim_to_channel(text: str, kb: InlineKeyboardMarkup, photo_id: str | None = None):
    """Надіслати заявку в канал (якщо налаштовано)."""
    if not CLAIMS_CHANNEL_ID:
        return
    try:
        if photo_id:
            await bot.send_photo(CLAIMS_CHANNEL_ID, photo=photo_id, caption=text, reply_markup=kb)
        else:
            await bot.send_message(CLAIMS_CHANNEL_ID, text, reply_markup=kb)
    except Exception as e:
        logging.warning(f"Не вдалося надіслати в канал: {e}")

async def _notify_curators(db, text: str, kb: InlineKeyboardMarkup, min_role="Куратор модерації"):
    """Надіслати в ПП кожному куратору."""
    curators = [
        uid for uid, d in db["users"].items()
        if ROLES.get(d["role"], 0) >= ROLES[min_role]
    ]
    for cuid in curators:
        try:
            await bot.send_message(int(cuid), text, reply_markup=kb)
        except Exception:
            pass

async def _notify_curators_with_photo(db, photo_id: str | None, caption: str,
                                      kb: InlineKeyboardMarkup, min_role="Куратор модерації"):
    """Надіслати фото+підпис або текст у ПП кураторам + у канал."""
    # Канал
    await _send_claim_to_channel(caption, kb, photo_id)
    # ПП кураторам
    curators = [
        uid for uid, d in db["users"].items()
        if ROLES.get(d["role"], 0) >= ROLES[min_role]
    ]
    for cuid in curators:
        try:
            if photo_id:
                await bot.send_photo(int(cuid), photo=photo_id, caption=caption, reply_markup=kb)
            else:
                await bot.send_message(int(cuid), caption, reply_markup=kb)
        except Exception:
            pass

def _claim_header(u: dict) -> str:
    """[ПАТЧ 2] Стандартний заголовок заявки з усіма даними відправника."""
    return (
        f"👤 <b>Подав:</b> {u['nick']}\n"
        f"🔹 <b>Telegram:</b> {u['tag']}\n"
        f"🎮 <b>Discord:</b> <code>{u['discord']}</code>\n"
        f"🎖 <b>Роль:</b> {u['role']}\n"
        f"🕐 <b>Дата:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
    )

# ═══════════════════════════════════════════════
#  [ПАТЧ 1] УНІВЕРСАЛЬНИЙ ХЕНДЛЕР ВІДХИЛЕННЯ
#  При натисканні "Відхилити" куратор вводить причину
# ═══════════════════════════════════════════════

def _reject_kb(claim_type: str, claim_id: int) -> InlineKeyboardMarkup:
    """Кнопки схвалення/відхилення з коректними префіксами."""
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Схвалити",  callback_data=f"{claim_type}_ok_{claim_id}"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data=f"{claim_type}_no_{claim_id}"),
    ]])

# Єдиний вхід для кнопок "Відхилити" будь-якого типу заявки
@dp.callback_query(F.data.regexp(r"^(mc|claim|vac|promo|bp_task)_no_(\d+)$"))
async def reject_ask_reason(call: CallbackQuery, state: FSMContext):
    """[ПАТЧ 1] Запитати причину відхилення перед тим як фіксувати."""
    db      = load_db()
    curator = get_user(db, call.from_user.id, call.from_user)

    # Перевірка прав залежно від типу
    import re
    m = re.match(r"^(mc|claim|vac|promo|bp_task)_no_(\d+)$", call.data)
    claim_type, claim_id = m.group(1), int(m.group(2))

    min_role_map = {
        "mc": "Куратор модерації", "claim": "Куратор модерації",
        "vac": "Куратор модерації", "promo": "Заступник головного модератора",
        "bp_task": "Куратор модерації",
    }
    if ROLES.get(curator["role"], 0) < ROLES[min_role_map[claim_type]]:
        return await call.answer("❌ Немає прав.", show_alert=True)

    # Перевірка що заявка ще pending
    db_key_map = {"mc": "mod_claims", "claim": "claims", "vac": "vacations",
                  "promo": "promotions", "bp_task": "bp_task_claims"}
    items = db.get(db_key_map[claim_type], [])
    item  = next((i for i in items if i["id"] == claim_id), None)
    if not item or item["status"] != "pending":
        return await call.answer("Вже оброблено.", show_alert=True)

    await state.set_state(Form.reject_reason)
    await state.update_data(reject_type=claim_type, reject_id=claim_id,
                            reject_msg_id=call.message.message_id,
                            reject_chat_id=call.message.chat.id,
                            reject_curator_nick=curator["nick"])
    await call.message.answer(
        f"📝 Введіть причину відхилення заявки <b>#{claim_id}</b>:"
    )
    await call.answer()

@dp.message(Form.reject_reason)
async def reject_with_reason(message: Message, state: FSMContext):
    """[ПАТЧ 1] Зафіксувати відхилення з причиною."""
    data        = await state.get_data()
    claim_type  = data["reject_type"]
    claim_id    = data["reject_id"]
    curator_nick = data["reject_curator_nick"]
    reason      = message.text
    await state.clear()

    db      = load_db()
    db_key_map = {"mc": "mod_claims", "claim": "claims", "vac": "vacations",
                  "promo": "promotions", "bp_task": "bp_task_claims"}
    items = db.get(db_key_map[claim_type], [])
    item  = next((i for i in items if i["id"] == claim_id), None)
    if not item or item["status"] != "pending":
        return await message.answer("⚠️ Заявку вже оброблено іншим куратором.")

    item["status"]        = "rejected"
    item["reject_reason"] = reason
    save_db(db)
    log_action(message.from_user.id, f"reject_{claim_type}", item.get("uid"), reason)

    # Повідомити відправника заявки з причиною
    type_labels = {
        "mc": "заявку на дію модерації", "claim": "заявку на бали",
        "vac": "заявку на відпустку",   "promo": "заявку на підвищення",
        "bp_task": "заявку на BP завдання",
    }
    try:
        await bot.send_message(
            int(item["uid"]),
            f"❌ <b>{type_labels.get(claim_type,'Заявку')} #{claim_id} відхилено.</b>\n\n"
            f"📝 <b>Причина:</b> {reason}\n"
            f"👮 <b>Куратор:</b> {curator_nick}"
        )
    except Exception:
        pass

    # Оновити повідомлення у куратора
    try:
        await bot.edit_message_text(
            f"❌ <b>Відхилено</b> {curator_nick}\n📝 Причина: {reason}",
            chat_id=data["reject_chat_id"],
            message_id=data["reject_msg_id"],
        )
    except Exception:
        pass

    await message.answer(f"✅ Заявку #{claim_id} відхилено. Причину надіслано відправнику.")

# ═══════════════════════════════════════════════
#  КЛАВІАТУРИ
# ═══════════════════════════════════════════════
def profile_markup(uid: str, is_owner: bool) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="🏆 Battle Pass", callback_data="bp_view"),
        InlineKeyboardButton(text="📊 Статистика",  callback_data=f"stats_{uid}"),
    ]]
    if is_owner:
        rows += [
            [InlineKeyboardButton(text="⚙️ Редагувати",    callback_data="edit_menu"),
             InlineKeyboardButton(text="📥 Заявка на бали", callback_data="claim_start")],
            [InlineKeyboardButton(text="🛡 Заявка на дію",  callback_data="mod_claim_start"),
             InlineKeyboardButton(text="📈 На підвищення",  callback_data="promo_start")],
            [InlineKeyboardButton(text="🏖 Відпустка",      callback_data="vacation_menu"),
             InlineKeyboardButton(text="🏅 Досягнення",     callback_data=f"ach_{uid}")],
        ]
    else:
        rows.append([
            InlineKeyboardButton(text="🏅 Досягнення", callback_data=f"ach_{uid}"),
            InlineKeyboardButton(text="👍 Репутація +1", callback_data=f"rep_{uid}"),
        ])
    return InlineKeyboardMarkup(inline_keyboard=rows)

def edit_menu_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🏷 Нікнейм", callback_data="set_n"),
         InlineKeyboardButton(text="🎮 Discord",  callback_data="set_d")],
        [InlineKeyboardButton(text="📝 Біо",     callback_data="set_b"),
         InlineKeyboardButton(text="⚧ Стать",    callback_data="set_s")],
        [InlineKeyboardButton(text="🎂 Вік",     callback_data="set_a")],
    ])

def admin_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📋 Заявки на бали",     callback_data="admin_claims"),
         InlineKeyboardButton(text="🛡 Заявки на дії",      callback_data="admin_mod_claims")],
        [InlineKeyboardButton(text="📈 На підвищення",      callback_data="admin_promos"),
         InlineKeyboardButton(text="🏖 Відпустки",          callback_data="admin_vacations")],
        [InlineKeyboardButton(text="🏆 BP Заявки",          callback_data="admin_bp_claims"),
         InlineKeyboardButton(text="📜 Журнал",             callback_data="admin_log")],
        [InlineKeyboardButton(text="💾 Backup",             callback_data="admin_backup"),
         InlineKeyboardButton(text="⚙️ Норми",              callback_data="admin_norms")],
    ])

# ═══════════════════════════════════════════════
#  /START  /HELP
# ═══════════════════════════════════════════════
@dp.message(Command("start"))
async def cmd_start(message: Message):
    db = load_db()
    u  = get_user(db, message.from_user.id, message.from_user)
    save_db(db)
    await message.answer(
        f"👋 Ласкаво просимо, <b>{u['nick']}</b>!\n\n"
        "🤖 <b>UA ONLINE BOT</b>\n\n"
        "/profile — профіль\n/top — рейтинг\n/daily — бонус\n/help — всі команди"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>КОМАНДИ UA ONLINE BOT</b>\n\n"
        "👤 <b>Профіль:</b>\n"
        "/profile [@user] · /editprofile · /setnick · /setbio · /stats · /search\n\n"
        "🏅 <b>Прогрес:</b>\n"
        "/top · /bp · /daily · /claimpoints · /achievements\n\n"
        "🛡 <b>Модерація (Модератор+):</b>\n"
        "/modclaim — подати заявку на дію модерації\n\n"
        "📋 <b>Персонал:</b>\n"
        "/promote @user [роль] · /demote · /reprimand · /vacation · /norms\n\n"
        "⚙️ <b>Адмін (ГМ / Заст. ГМ):</b>\n"
        "/admin · /backup · /setpoints · /setnorm\n"
        "/bp_create · /bp_tasks · /bp_rewards\n\n"
        "📌 <b>Ролі:</b>\n"
        "Молодший Модератор → Модератор → Ст. модератор\n"
        "→ Куратор Заходів → Куратор модерації\n"
        "→ Заступник головного модератора → Головний модератор"
    )

# ═══════════════════════════════════════════════
#  /PROFILE
# ═══════════════════════════════════════════════
@dp.message(Command("profile"))
async def cmd_profile(message: Message, command: CommandObject):
    db        = load_db()
    viewer_id = str(message.from_user.id)
    target_id = viewer_id

    if command.args:
        mention = command.args.split()[0].lower()
        found   = next(
            (uid for uid, d in db["users"].items()
             if d["tag"] == mention or uid == mention), None
        )
        if found:
            target_id = found
        else:
            return await message.answer("❌ Користувача не знайдено.")

    u        = get_user(db, target_id, message.from_user if target_id == viewer_id else None)
    is_owner = viewer_id == target_id

    xp_filled = int((u["xp"] / XP_PER_LEVEL) * 10)
    xp_bar    = "█" * xp_filled + "░" * (10 - xp_filled)

    vacation_str  = f"\n🏖 <b>Відпустка:</b> {u['vacation']}" if u.get("vacation") else ""
    reprimand_str = f"\n⚠️ <b>Догани:</b> {u['reprimands']}" if u.get("reprimands", 0) > 0 else ""

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
    await message.answer(text, reply_markup=profile_markup(target_id, is_owner))

# ═══════════════════════════════════════════════
#  РЕДАГУВАННЯ ПРОФІЛЮ
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
    await call.message.answer("🎮 Введіть Discord username:")
    await call.answer()

@dp.callback_query(F.data == "set_b")
async def set_b(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.edit_bio)
    await call.message.answer("📝 Введіть біографію (макс. 150 символів):")
    await call.answer()

@dp.callback_query(F.data == "set_s")
async def set_s(call: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="👨 Чоловік",  callback_data="sex_male"),
        InlineKeyboardButton(text="👩 Жінка",    callback_data="sex_female"),
        InlineKeyboardButton(text="🏳 N/A",      callback_data="sex_na"),
    ]])
    await call.message.answer("⚧ Виберіть стать:", reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data.startswith("sex_"))
async def process_sex(call: CallbackQuery, state: FSMContext):
    val = {"sex_male": "Чоловік", "sex_female": "Жінка", "sex_na": "N/A"}.get(call.data, "N/A")
    db  = load_db()
    db["users"][str(call.from_user.id)]["sex"] = val
    save_db(db)
    await state.clear()
    await call.message.answer(f"✅ Стать: <b>{val}</b>")
    await call.answer()

@dp.callback_query(F.data == "set_a")
async def set_a(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.edit_age)
    await call.message.answer("🎂 Введіть вік:")
    await call.answer()

@dp.message(Form.edit_nick)
async def process_nick(message: Message, state: FSMContext):
    db = load_db(); db["users"][str(message.from_user.id)]["nick"] = message.text[:25]; save_db(db)
    await state.clear(); await message.answer(f"✅ Нік: <b>{message.text[:25]}</b>")

@dp.message(Form.edit_discord)
async def process_discord(message: Message, state: FSMContext):
    db = load_db(); db["users"][str(message.from_user.id)]["discord"] = message.text[:50]; save_db(db)
    await state.clear(); await message.answer(f"✅ Discord: <code>{message.text[:50]}</code>")

@dp.message(Form.edit_bio)
async def process_bio(message: Message, state: FSMContext):
    db = load_db(); db["users"][str(message.from_user.id)]["bio"] = message.text[:150]; save_db(db)
    await state.clear(); await message.answer("✅ Біографію оновлено!")

@dp.message(Form.edit_age)
async def process_age(message: Message, state: FSMContext):
    if not message.text.isdigit() or not (10 <= int(message.text) <= 80):
        return await message.answer("❌ Введіть вік від 10 до 80.")
    db = load_db(); db["users"][str(message.from_user.id)]["age"] = message.text; save_db(db)
    await state.clear(); await message.answer(f"✅ Вік: <b>{message.text}</b>")

@dp.message(Command("setnick"))
async def cmd_setnick(message: Message, command: CommandObject):
    if not command.args: return await message.answer("📝: <code>/setnick [нік]</code>")
    db = load_db(); db["users"][str(message.from_user.id)]["nick"] = command.args[:25]; save_db(db)
    await message.answer(f"✅ Нік: <b>{command.args[:25]}</b>")

@dp.message(Command("setbio"))
async def cmd_setbio(message: Message, command: CommandObject):
    if not command.args: return await message.answer("📝: <code>/setbio [текст]</code>")
    db = load_db(); db["users"][str(message.from_user.id)]["bio"] = command.args[:150]; save_db(db)
    await message.answer("✅ Біо оновлено!")

# ═══════════════════════════════════════════════
#  /STATS
# ═══════════════════════════════════════════════
@dp.message(Command("stats"))
@dp.callback_query(F.data.startswith("stats_"))
async def cmd_stats(event):
    is_cb   = isinstance(event, CallbackQuery)
    message = event.message if is_cb else event
    db      = load_db()
    if is_cb:
        target_id = event.data.split("_")[1]
    else:
        args = event.text.split()[1:]
        if args:
            mention   = args[0].lower()
            target_id = next((uid for uid, d in db["users"].items() if d["tag"] == mention),
                             str(event.from_user.id))
        else:
            target_id = str(event.from_user.id)
    u     = get_user(db, target_id)
    s     = u["stats"]
    norms = db.get("norms", {"weekly": 50, "monthly": 200})
    w_pct = min(int(u.get("weekly_points",  0) / norms["weekly"]  * 100), 100)
    m_pct = min(int(u.get("monthly_points", 0) / norms["monthly"] * 100), 100)
    text = (
        f"📊 <b>Статистика: {u['nick']}</b>\n\n"
        f"🔇 Мути: {s['mutes']}  🔨 Бани: {s['bans']}\n"
        f"⚠️ Попередження: {s['warns']}\n"
        f"🤝 Допомога: {s['help']}  🎫 Тікети: {s['tickets']}\n"
        f"📋 Скарги: {s['complaints']}\n\n"
        f"📈 <b>Норми:</b>\n"
        f"  Тижнева: {u.get('weekly_points',0)}/{norms['weekly']} ({w_pct}%)\n"
        f"  Місячна: {u.get('monthly_points',0)}/{norms['monthly']} ({m_pct}%)\n\n"
        f"💰 Бали: {u['points']}  ⭐ Репутація: {u.get('reputation',0)}\n"
        f"🏆 BP Рівень: {u['level']}  ⚡ XP: {u['xp']}/{XP_PER_LEVEL}"
    )
    await message.answer(text)
    if is_cb: await event.answer()

# ═══════════════════════════════════════════════
#  /TOP
# ═══════════════════════════════════════════════
PAGE_SIZE = 10

def build_top(db, page=0):
    users       = sorted(db["users"].items(), key=lambda x: x[1]["points"], reverse=True)
    total_pages = max(1, (len(users) + PAGE_SIZE - 1) // PAGE_SIZE)
    page        = max(0, min(page, total_pages - 1))
    chunk       = users[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    medals      = {0: "🥇", 1: "🥈", 2: "🥉"}
    text        = f"📊 <b>ТОП ПЕРСОНАЛУ</b> (стор. {page+1}/{total_pages})\n\n"
    for i, (uid, d) in enumerate(chunk):
        pos   = page * PAGE_SIZE + i
        medal = medals.get(pos, f"{pos+1}.")
        text += f"{medal} <b>{d['nick']}</b> [{d['role'][:3]}] — {d['points']} 💰\n"
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀ Назад", callback_data=f"top_{page-1}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton(text="Вперед ▶", callback_data=f"top_{page+1}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[nav] if nav else [])
    return text, kb

@dp.message(Command("top"))
async def cmd_top(message: Message):
    db = load_db(); text, kb = build_top(db, 0)
    await message.answer(text, reply_markup=kb)

@dp.callback_query(F.data.startswith("top_"))
async def top_page(call: CallbackQuery):
    page = int(call.data.split("_")[1])
    db   = load_db(); text, kb = build_top(db, page)
    await call.message.edit_text(text, reply_markup=kb)
    await call.answer()

# ═══════════════════════════════════════════════
#  ЗАЯВКИ НА МОДЕРАЦІЮ
# ═══════════════════════════════════════════════
@dp.message(Command("modclaim"))
@dp.callback_query(F.data == "mod_claim_start")
async def mod_claim_start(event, state: FSMContext):
    msg = event.message if isinstance(event, CallbackQuery) else event
    db  = load_db()
    u   = get_user(db, event.from_user.id, event.from_user)
    if ROLES.get(u["role"], 0) < ROLES["Модератор"]:
        await msg.answer("❌ Потрібна роль: <b>Модератор</b> або вище.")
        if isinstance(event, CallbackQuery): await event.answer()
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔇 Мут",         callback_data="mca_mute"),
         InlineKeyboardButton(text="🔊 Зняти мут",   callback_data="mca_unmute")],
        [InlineKeyboardButton(text="🔨 Бан",         callback_data="mca_ban"),
         InlineKeyboardButton(text="✅ Зняти бан",   callback_data="mca_unban")],
        [InlineKeyboardButton(text="⚠️ Попередження",callback_data="mca_warn"),
         InlineKeyboardButton(text="👢 Кік",         callback_data="mca_kick")],
    ])
    await msg.answer("🛡 <b>Заявка на дію модерації</b>\n\nВиберіть тип дії:", reply_markup=kb)
    if isinstance(event, CallbackQuery): await event.answer()

@dp.callback_query(F.data.startswith("mca_"))
async def mca_choose(call: CallbackQuery, state: FSMContext):
    action = call.data.split("_")[1]
    await state.update_data(mod_action=action)
    await state.set_state(Form.mod_target)
    await call.message.answer("👤 Введіть @тег або нікнейм порушника:")
    await call.answer()

@dp.message(Form.mod_target)
async def mca_target(message: Message, state: FSMContext):
    await state.update_data(mod_target=message.text)
    await state.set_state(Form.mod_reason)
    await message.answer("📝 Введіть причину дії:")

@dp.message(Form.mod_reason)
async def mca_reason(message: Message, state: FSMContext):
    await state.update_data(mod_reason=message.text)
    await state.set_state(Form.mod_proof)
    await message.answer("📎 Надішліть скріншот (фото) або опишіть докази текстом:")

@dp.message(Form.mod_proof)
async def mca_proof(message: Message, state: FSMContext):
    data = await state.get_data()
    db   = load_db()
    u    = get_user(db, message.from_user.id, message.from_user)

    now      = datetime.now().timestamp()
    existing = [c for c in db["mod_claims"] if c["uid"] == str(message.from_user.id)]
    if existing and now - existing[-1].get("ts_raw", 0) < 300:
        await state.clear()
        return await message.answer("❌ Зачекайте 5 хвилин між заявками.")

    photo_id   = None
    proof_text = message.text or "[медіафайл]"
    if message.photo:
        photo_id   = message.photo[-1].file_id
        proof_text = message.caption or "[фото без підпису]"

    claim_id = len(db["mod_claims"]) + 1
    claim = {
        "id":       claim_id,
        "uid":      str(message.from_user.id),
        "nick":     u["nick"],
        "tag":      u["tag"],
        "action":   data["mod_action"],
        "target":   data["mod_target"],
        "reason":   data["mod_reason"],
        "proof":    proof_text,
        "photo_id": photo_id,
        "status":   "pending",
        "ts":       datetime.now().strftime("%d.%m.%Y %H:%M"),
        "ts_raw":   now,
    }
    db["mod_claims"].append(claim)
    save_db(db)
    await state.clear()

    action_ua = {
        "mute": "🔇 Мут", "unmute": "🔊 Зняти мут",
        "ban":  "🔨 Бан",  "unban":  "✅ Зняти бан",
        "warn": "⚠️ Попередження", "kick": "👢 Кік",
    }.get(data["mod_action"], data["mod_action"])

    # [ПАТЧ 2] Заголовок з усіма даними відправника
    notify_text = (
        f"🛡 <b>Заявка на дію модерації #{claim_id}</b>\n\n"
        f"{_claim_header(u)}"
        f"──────────────────────\n"
        f"🎯 <b>Дія:</b> {action_ua}\n"
        f"👤 <b>Порушник:</b> {data['mod_target']}\n"
        f"📝 <b>Причина:</b> {data['mod_reason']}\n"
        f"📎 <b>Докази:</b> {proof_text}"
    )
    kb = _reject_kb("mc", claim_id)
    await _notify_curators_with_photo(db, photo_id, notify_text, kb)
    await message.answer(f"✅ Заявку #{claim_id} надіслано кураторам!")

@dp.callback_query(F.data.startswith("mc_ok_"))
async def mc_approve(call: CallbackQuery):
    db      = load_db()
    curator = get_user(db, call.from_user.id, call.from_user)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Немає прав.", show_alert=True)
    cid   = int(call.data.split("_")[-1])
    claim = next((c for c in db["mod_claims"] if c["id"] == cid), None)
    if not claim or claim["status"] != "pending":
        return await call.answer("Вже оброблено.", show_alert=True)
    claim["status"] = "approved"
    tu = db["users"].get(claim["uid"])
    new_ach = []
    if tu:
        add_points(tu, claim["action"])
        action_stat = {"mute": "mutes", "ban": "bans", "warn": "warns"}.get(claim["action"])
        if action_stat:
            tu["stats"][action_stat] = tu["stats"].get(action_stat, 0) + 1
        new_ach = check_achievements(tu)
    save_db(db)
    log_action(call.from_user.id, f"approve_mod_{claim['action']}", claim["uid"])
    pts = POINTS_TABLE.get(claim["action"], {"points": 0, "xp": 0})
    try:
        if call.message.photo:
            await call.message.edit_caption(
                (call.message.caption or "") + f"\n\n✅ <b>Схвалено</b> {curator['nick']}"
            )
        else:
            await call.message.edit_text(
                call.message.text + f"\n\n✅ <b>Схвалено</b> {curator['nick']}"
            )
    except Exception: pass
    try:
        ach_str = ("\n🏅 Нові досягнення: " + ", ".join(new_ach)) if new_ach else ""
        await bot.send_message(
            int(claim["uid"]),
            f"✅ <b>Заявку #{cid} схвалено!</b>\n"
            f"💰 +{pts['points']} балів  ⚡ +{pts['xp']} XP{ach_str}"
        )
    except Exception: pass
    await call.answer("✅ Схвалено!")

# ═══════════════════════════════════════════════
#  ЗАЯВКА НА БАЛИ
# ═══════════════════════════════════════════════
@dp.message(Command("claimpoints"))
@dp.callback_query(F.data == "claim_start")
async def claim_start(event, state: FSMContext):
    msg = event.message if isinstance(event, CallbackQuery) else event
    await state.set_state(Form.claim_action)
    await msg.answer(
        "📥 <b>Заявка на нарахування балів</b>\n\n"
        "Що ви зробили? (опишіть дію — допомога, тікет, скарга тощо):"
    )
    if isinstance(event, CallbackQuery): await event.answer()

@dp.message(Form.claim_action)
async def claim_action_handler(message: Message, state: FSMContext):
    await state.update_data(action=message.text)
    await state.set_state(Form.claim_count)
    await message.answer("💰 Скільки балів ви хочете отримати?")

@dp.message(Form.claim_count)
async def claim_count_handler(message: Message, state: FSMContext):
    if not message.text.isdigit() or int(message.text) <= 0:
        return await message.answer("❌ Введіть позитивне число.")
    await state.update_data(points=int(message.text))
    await state.set_state(Form.claim_proof)
    await message.answer("📎 Надішліть скріншот (фото) або текстові докази:")

@dp.message(Form.claim_proof)
async def claim_proof_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    db   = load_db()
    u    = get_user(db, message.from_user.id, message.from_user)

    now = datetime.now().timestamp()
    existing = [c for c in db["claims"] if c["uid"] == str(message.from_user.id)]
    if existing and now - existing[-1].get("ts_raw", 0) < 600:
        await state.clear()
        return await message.answer("❌ Зачекайте 10 хвилин між заявками.")

    photo_id   = None
    proof_text = message.text or "[медіафайл]"
    if message.photo:
        photo_id   = message.photo[-1].file_id
        proof_text = message.caption or "[фото без підпису]"

    claim_id = len(db["claims"]) + 1
    claim = {
        "id":       claim_id,
        "uid":      str(message.from_user.id),
        "nick":     u["nick"],
        "tag":      u["tag"],
        "action":   data["action"],
        "points":   data["points"],
        "proof":    proof_text,
        "photo_id": photo_id,
        "status":   "pending",
        "ts":       datetime.now().strftime("%d.%m.%Y %H:%M"),
        "ts_raw":   now,
    }
    db["claims"].append(claim)
    save_db(db)
    await state.clear()

    # [ПАТЧ 2] Розширений заголовок
    notify_text = (
        f"📥 <b>Заявка на бали #{claim_id}</b>\n\n"
        f"{_claim_header(u)}"
        f"──────────────────────\n"
        f"📝 <b>Дія:</b> {data['action']}\n"
        f"💰 <b>Запит:</b> {data['points']} балів\n"
        f"📎 <b>Докази:</b> {proof_text}"
    )
    kb = _reject_kb("claim", claim_id)
    await _notify_curators_with_photo(db, photo_id, notify_text, kb)
    await message.answer(f"✅ Заявку #{claim_id} надіслано кураторам!")

@dp.callback_query(F.data.startswith("claim_ok_"))
async def claim_approve(call: CallbackQuery):
    db      = load_db()
    curator = get_user(db, call.from_user.id, call.from_user)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Немає прав.", show_alert=True)
    cid   = int(call.data.split("_")[-1])
    claim = next((c for c in db["claims"] if c["id"] == cid), None)
    if not claim or claim["status"] != "pending":
        return await call.answer("Вже оброблено.", show_alert=True)
    claim["status"] = "approved"
    tu = db["users"].get(claim["uid"])
    if tu:
        tu["points"]         += claim["points"]
        tu["weekly_points"]   = tu.get("weekly_points",  0) + claim["points"]
        tu["monthly_points"]  = tu.get("monthly_points", 0) + claim["points"]
        tu["xp"]             += claim["points"] * 3
        while tu["xp"] >= XP_PER_LEVEL:
            tu["xp"] -= XP_PER_LEVEL; tu["level"] += 1
    save_db(db)
    log_action(call.from_user.id, "claim_approve", claim["uid"], str(claim["points"]))
    try:
        await call.message.edit_text(call.message.text + f"\n\n✅ <b>Схвалено</b> {curator['nick']}")
    except Exception: pass
    try:
        await bot.send_message(int(claim["uid"]),
            f"🎉 Заявку #{cid} схвалено!\n💰 +{claim['points']} балів нараховано.")
    except Exception: pass
    await call.answer("✅")

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
        "Розкажіть про свої досягнення, активність та стаж:"
    )
    if isinstance(event, CallbackQuery): await event.answer()

@dp.message(Form.promo_reason)
async def promo_reason_handler(message: Message, state: FSMContext):
    db = load_db()
    u  = get_user(db, message.from_user.id, message.from_user)
    pid = len(db["promotions"]) + 1
    promo = {
        "id": pid, "uid": str(message.from_user.id),
        "nick": u["nick"], "tag": u["tag"],
        "role": u["role"], "reason": message.text,
        "status": "pending", "ts": datetime.now().strftime("%d.%m.%Y %H:%M"),
    }
    db["promotions"].append(promo)
    save_db(db)
    await state.clear()

    # [ПАТЧ 2]
    notify = (
        f"📈 <b>Заявка на підвищення #{pid}</b>\n\n"
        f"{_claim_header(u)}"
        f"──────────────────────\n"
        f"📝 <b>Обґрунтування:</b> {message.text}"
    )
    kb = _reject_kb("promo", pid)
    await _notify_curators(db, notify, kb, min_role="Заступник головного модератора")
    # Також у канал
    await _send_claim_to_channel(notify, kb)
    await message.answer(f"✅ Заявку #{pid} надіслано керівництву!")

@dp.callback_query(F.data.startswith("promo_ok_"))
async def promo_approve(call: CallbackQuery):
    db    = load_db()
    admin = get_user(db, call.from_user.id, call.from_user)
    if ROLES.get(admin["role"], 0) < ROLES["Заступник головного модератора"]:
        return await call.answer("❌ Немає прав.", show_alert=True)
    pid   = int(call.data.split("_")[-1])
    promo = next((p for p in db["promotions"] if p["id"] == pid), None)
    if not promo or promo["status"] != "pending":
        return await call.answer("Вже оброблено.", show_alert=True)
    promo["status"] = "approved"
    save_db(db)
    try:
        await call.message.edit_text(call.message.text + f"\n\n✅ <b>Схвалено</b> {admin['nick']}")
    except Exception: pass
    try:
        await bot.send_message(int(promo["uid"]),
            "🎉 Заявку на підвищення схвалено!\nЗверніться до куратора для призначення ролі.")
    except Exception: pass
    await call.answer("✅")

# ═══════════════════════════════════════════════
#  ВІДПУСТКА
# ═══════════════════════════════════════════════
@dp.message(Command("vacation"))
@dp.callback_query(F.data == "vacation_menu")
async def vacation_start(event, state: FSMContext):
    msg = event.message if isinstance(event, CallbackQuery) else event
    kb  = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📅 Подати заявку",   callback_data="vacation_apply"),
        InlineKeyboardButton(text="🔙 Зняти відпустку", callback_data="vacation_cancel"),
    ]])
    await msg.answer("🏖 <b>Система відпусток</b>", reply_markup=kb)
    if isinstance(event, CallbackQuery): await event.answer()

@dp.callback_query(F.data == "vacation_apply")
async def vacation_apply(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.vacation_dates)
    await call.message.answer("📅 Введіть дати відпустки (напр.: 01.07 — 15.07):")
    await call.answer()

@dp.message(Form.vacation_dates)
async def vacation_dates_handler(message: Message, state: FSMContext):
    await state.update_data(dates=message.text)
    await state.set_state(Form.vacation_reason)
    await message.answer("📝 Причина (або «-»):")

@dp.message(Form.vacation_reason)
async def vacation_reason_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    db   = load_db()
    u    = get_user(db, message.from_user.id, message.from_user)
    vid  = len(db["vacations"]) + 1
    vac  = {
        "id": vid, "uid": str(message.from_user.id),
        "nick": u["nick"], "tag": u["tag"],
        "dates": data["dates"], "reason": message.text,
        "status": "pending", "ts": datetime.now().strftime("%d.%m.%Y %H:%M"),
    }
    db["vacations"].append(vac)
    save_db(db)
    await state.clear()

    # [ПАТЧ 2]
    notify = (
        f"🏖 <b>Заявка на відпустку #{vid}</b>\n\n"
        f"{_claim_header(u)}"
        f"──────────────────────\n"
        f"📅 <b>Дати:</b> {data['dates']}\n"
        f"📝 <b>Причина:</b> {message.text}"
    )
    kb = _reject_kb("vac", vid)
    await _notify_curators(db, notify, kb)
    await _send_claim_to_channel(notify, kb)
    await message.answer(f"✅ Заявку #{vid} надіслано!")

@dp.callback_query(F.data.startswith("vac_ok_"))
async def vac_approve(call: CallbackQuery):
    db      = load_db()
    curator = get_user(db, call.from_user.id, call.from_user)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Немає прав.", show_alert=True)
    vid = int(call.data.split("_")[-1])
    vac = next((v for v in db["vacations"] if v["id"] == vid), None)
    if not vac or vac["status"] != "pending":
        return await call.answer("Вже оброблено.", show_alert=True)
    vac["status"] = "approved"
    tu = db["users"].get(vac["uid"])
    if tu: tu["vacation"] = vac["dates"]
    save_db(db)
    try:
        await call.message.edit_text(call.message.text + f"\n\n✅ <b>Схвалено</b> {curator['nick']}")
    except Exception: pass
    try:
        await bot.send_message(int(vac["uid"]), f"🏖 Відпустку схвалено: {vac['dates']}")
    except Exception: pass
    await call.answer("✅")

@dp.callback_query(F.data == "vacation_cancel")
async def vacation_cancel(call: CallbackQuery):
    db = load_db(); u = get_user(db, call.from_user.id, call.from_user)
    u["vacation"] = None; save_db(db)
    await call.message.answer("✅ Відпустку знято."); await call.answer()

# ═══════════════════════════════════════════════
#  ПІДВИЩЕННЯ / ПОНИЖЕННЯ РОЛІ
# ═══════════════════════════════════════════════
ROLES_LIST = list(ROLES.keys())

@dp.message(Command("promote"))
async def cmd_promote(message: Message, command: CommandObject):
    db    = load_db()
    admin = get_user(db, message.from_user.id, message.from_user)
    admin_level = ROLES.get(admin["role"], 0)
    if admin_level < ROLES["Куратор модерації"]:
        return await message.answer("❌ Потрібна роль: <b>Куратор модерації</b> або вище.")
    if not command.args or len(command.args.split()) < 2:
        roles_list = "\n".join(f"• {r}" for r in ROLES_LIST)
        return await message.answer(f"📝: <code>/promote @user [роль]</code>\n\nРолі:\n{roles_list}")
    args = command.args.split(None, 1)
    tag  = args[0].lower()
    role = args[1].strip()
    if role not in ROLES:
        return await message.answer("❌ Невірна роль. Перевір /help")
    if ROLES[role] >= admin_level:
        return await message.answer(
            f"❌ Ви не можете призначити роль <b>{role}</b>.\n"
            f"Можна призначати лише ролі нижче вашої (<b>{admin['role']}</b>)."
        )
    tid, tu = await _find_target(db, tag)
    if not tu:
        return await message.answer("❌ Користувача не знайдено.")
    if ROLES.get(tu["role"], 0) >= admin_level:
        return await message.answer(
            f"❌ Ви не можете змінити роль <b>{tu['nick']}</b>, "
            f"бо він має роль <b>{tu['role']}</b> (рівну або вищу за вашу)."
        )
    old_role = tu["role"]
    tu["role"] = role
    save_db(db)
    log_action(message.from_user.id, "promote", tid, f"{old_role} → {role}")
    await message.answer(f"✅ {tu['nick']}: <b>{old_role}</b> → <b>{role}</b>")

@dp.message(Command("demote"))
async def cmd_demote(message: Message, command: CommandObject):
    db    = load_db()
    admin = get_user(db, message.from_user.id, message.from_user)
    admin_level = ROLES.get(admin["role"], 0)
    if admin_level < ROLES["Заступник головного модератора"]:
        return await message.answer("❌ Потрібна роль: <b>Заступник головного модератора</b> або вище.")
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
    if ROLES.get(tu["role"], 0) >= admin_level:
        return await message.answer(
            f"❌ Ви не можете понизити <b>{tu['nick']}</b> з роллю <b>{tu['role']}</b>."
        )
    old_role = tu["role"]
    tu["role"] = role
    save_db(db)
    log_action(message.from_user.id, "demote", tid, f"{old_role} → {role}")
    await message.answer(f"✅ {tu['nick']}: <b>{old_role}</b> → <b>{role}</b>")

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
    args = command.args.split(None, 1)
    tid, tu = await _find_target(db, args[0].lower())
    if not tu: return await message.answer("❌ Користувача не знайдено.")
    tu["reprimands"] = tu.get("reprimands", 0) + 1
    db["reprimands"].append({
        "ts": datetime.now().isoformat(), "admin": str(message.from_user.id),
        "target": tid, "reason": args[1],
    })
    save_db(db)
    log_action(message.from_user.id, "reprimand", tid, args[1])
    await message.answer(
        f"⚠️ <b>Догану видано!</b>\n"
        f"👤 {tu['nick']} — догана #{tu['reprimands']}\n"
        f"📝 Причина: {args[1]}"
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
        left = datetime.now().replace(hour=0, minute=0, second=0) + timedelta(days=1) - datetime.now()
        h, r = divmod(int(left.total_seconds()), 3600)
        return await message.answer(f"⏳ Бонус вже отримано!\nНаступний через: <b>{h}г {r//60}хв</b>")
    u["daily_last"]    = now
    u["points"]       += DAILY_BONUS
    u["xp"]           += 30
    u["weekly_points"] = u.get("weekly_points",  0) + DAILY_BONUS
    u["monthly_points"]= u.get("monthly_points", 0) + DAILY_BONUS
    while u["xp"] >= XP_PER_LEVEL:
        u["xp"] -= XP_PER_LEVEL; u["level"] += 1
    save_db(db)
    await message.answer(
        f"🎁 <b>Щоденний бонус!</b>\n"
        f"💰 +{DAILY_BONUS} балів  ⚡ +30 XP\n"
        f"Всього балів: <b>{u['points']}</b>"
    )

# ═══════════════════════════════════════════════
#  [ПАТЧ 3] BATTLE PASS — завдання через куратора
# ═══════════════════════════════════════════════
@dp.message(Command("bp"))
@dp.callback_query(F.data == "bp_view")
async def cmd_bp(event):
    is_cb   = isinstance(event, CallbackQuery)
    message = event.message if is_cb else event
    uid     = str(event.from_user.id)
    db      = load_db()
    u       = get_user(db, uid, event.from_user if not is_cb else None)
    bp      = db["bp"]
    tasks   = bp.get("tasks", [])
    done    = u.get("bp_tasks_done", [])
    rewards = bp.get("rewards", {})

    # Які завдання вже в черзі на розгляд
    pending_idxs = {
        c["task_idx"] for c in db.get("bp_task_claims", [])
        if c["uid"] == uid and c["status"] == "pending"
    }

    xp_bar = "█" * int(u["xp"] / XP_PER_LEVEL * 10) + "░" * (10 - int(u["xp"] / XP_PER_LEVEL * 10))
    text = (
        f"🏆 <b>BATTLE PASS — {bp.get('name', 'Сезон '+str(bp['season']))}</b>\n\n"
        f"🎯 Рівень: {u['level']}  ⚡ [{xp_bar}] {u['xp']}/{XP_PER_LEVEL}\n\n"
    )
    if tasks:
        text += "📋 <b>Завдання:</b>\n"
        for i, t in enumerate(tasks):
            if i in done:
                status = "✅"
            elif i in pending_idxs:
                status = "⏳"   # чекає схвалення
            else:
                status = "⬜"
            text += f"{status} {t['text']} (+{t['xp']} XP)\n"
        text += "\n<i>⬜ — не виконано  ⏳ — на перевірці  ✅ — зараховано</i>\n"
    if rewards:
        text += "\n🎁 <b>Нагороди:</b>\n"
        for lvl, rew in sorted(rewards.items(), key=lambda x: int(x[0])):
            text += f"{'✅' if u['level'] >= int(lvl) else '🔒'} Рівень {lvl}: {rew}\n"

    # Показуємо кнопки лише для невиконаних і не в черзі
    undone = [i for i in range(len(tasks)) if i not in done and i not in pending_idxs]
    rows   = []
    for i in undone[:3]:
        rows.append([InlineKeyboardButton(
            text=f"📤 Подати завдання {i+1}", callback_data=f"bp_submit_{i}"
        )])
    kb = InlineKeyboardMarkup(inline_keyboard=rows) if rows else None
    save_db(db)
    await message.answer(text, reply_markup=kb)
    if is_cb: await event.answer()

@dp.callback_query(F.data.startswith("bp_submit_"))
async def bp_submit_task(call: CallbackQuery, state: FSMContext):
    """[ПАТЧ 3] Починаємо FSM для подачі заявки на BP завдання."""
    idx = int(call.data.split("_")[-1])
    db  = load_db()
    u   = get_user(db, call.from_user.id, call.from_user)

    tasks = db["bp"].get("tasks", [])
    if idx >= len(tasks):
        return await call.answer("Завдання не знайдено.", show_alert=True)

    # Перевірка дублікату
    already = any(
        c for c in db.get("bp_task_claims", [])
        if c["uid"] == str(call.from_user.id) and c["task_idx"] == idx and c["status"] == "pending"
    )
    if already:
        return await call.answer("⏳ Заявку вже подано, очікуйте перевірки.", show_alert=True)

    await state.update_data(bp_task_idx=idx)
    await state.set_state(Form.bp_task_proof)
    await call.message.answer(
        f"📤 <b>Завдання {idx+1}:</b> {tasks[idx]['text']}\n\n"
        f"Надішліть скріншот або опис виконання як доказ:"
    )
    await call.answer()

@dp.message(Form.bp_task_proof)
async def bp_task_proof_handler(message: Message, state: FSMContext):
    """[ПАТЧ 3] Отримуємо докази та створюємо заявку на BP завдання."""
    data = await state.get_data()
    idx  = data["bp_task_idx"]
    db   = load_db()
    u    = get_user(db, message.from_user.id, message.from_user)

    tasks = db["bp"].get("tasks", [])
    if idx >= len(tasks):
        await state.clear()
        return await message.answer("❌ Завдання не знайдено.")

    task     = tasks[idx]
    photo_id = None
    proof    = message.text or "[медіафайл]"
    if message.photo:
        photo_id = message.photo[-1].file_id
        proof    = message.caption or "[фото без підпису]"

    claim_id = len(db.get("bp_task_claims", [])) + 1
    claim = {
        "id":       claim_id,
        "uid":      str(message.from_user.id),
        "nick":     u["nick"],
        "tag":      u["tag"],
        "task_idx": idx,
        "task_text":task["text"],
        "task_xp":  task["xp"],
        "proof":    proof,
        "photo_id": photo_id,
        "status":   "pending",
        "ts":       datetime.now().strftime("%d.%m.%Y %H:%M"),
        "ts_raw":   datetime.now().timestamp(),
    }
    db.setdefault("bp_task_claims", []).append(claim)
    save_db(db)
    await state.clear()

    notify_text = (
        f"🏆 <b>Заявка на BP завдання #{claim_id}</b>\n\n"
        f"{_claim_header(u)}"
        f"──────────────────────\n"
        f"📋 <b>Завдання #{idx+1}:</b> {task['text']}\n"
        f"⚡ <b>XP:</b> +{task['xp']}\n"
        f"📎 <b>Докази:</b> {proof}"
    )
    kb = _reject_kb("bp_task", claim_id)
    await _notify_curators_with_photo(db, photo_id, notify_text, kb)
    await message.answer(
        f"✅ Заявку на завдання <b>{task['text']}</b> відправлено!\n"
        f"Куратор перевірить докази та нарахує +{task['xp']} XP."
    )

@dp.callback_query(F.data.startswith("bp_task_ok_"))
async def bp_task_approve(call: CallbackQuery):
    """[ПАТЧ 3] Куратор схвалює BP завдання → зараховуємо XP."""
    db      = load_db()
    curator = get_user(db, call.from_user.id, call.from_user)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Немає прав.", show_alert=True)

    cid   = int(call.data.split("_")[-1])
    claim = next((c for c in db.get("bp_task_claims", []) if c["id"] == cid), None)
    if not claim or claim["status"] != "pending":
        return await call.answer("Вже оброблено.", show_alert=True)

    claim["status"] = "approved"
    tu = db["users"].get(claim["uid"])
    if tu:
        tu.setdefault("bp_tasks_done", []).append(claim["task_idx"])
        tu["xp"] += claim["task_xp"]
        while tu["xp"] >= XP_PER_LEVEL:
            tu["xp"] -= XP_PER_LEVEL; tu["level"] += 1
    save_db(db)
    log_action(call.from_user.id, "bp_task_approve", claim["uid"],
               f"task={claim['task_idx']} xp={claim['task_xp']}")

    try:
        if call.message.photo:
            await call.message.edit_caption(
                (call.message.caption or "") + f"\n\n✅ <b>Схвалено</b> {curator['nick']}"
            )
        else:
            await call.message.edit_text(
                call.message.text + f"\n\n✅ <b>Схвалено</b> {curator['nick']}"
            )
    except Exception: pass
    try:
        await bot.send_message(
            int(claim["uid"]),
            f"🏆 <b>BP завдання підтверджено!</b>\n"
            f"📋 {claim['task_text']}\n"
            f"⚡ +{claim['task_xp']} XP нараховано!\n"
            f"👮 Куратор: {curator['nick']}"
        )
    except Exception: pass
    await call.answer("✅ Завдання зараховано!")

# ═══════════════════════════════════════════════
#  BATTLE PASS — адмін команди
# ═══════════════════════════════════════════════
@dp.message(Command("bp_create"))
async def bp_create(message: Message, state: FSMContext):
    db = load_db(); u = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(u["role"], 0) < ROLES["Заступник головного модератора"]:
        return await message.answer("❌ Потрібна роль: <b>Заступник головного модератора</b>.")
    await state.set_state(Form.bp_create_name)
    await message.answer("🏆 Введіть назву нового сезону Battle Pass:")

@dp.message(Form.bp_create_name)
async def bp_create_name(message: Message, state: FSMContext):
    db = load_db()
    db["bp"] = {"season": db["bp"]["season"] + 1, "name": message.text, "tasks": [], "rewards": {}}
    db["bp_task_claims"] = []   # [ПАТЧ 3] скидаємо заявки BP
    for u in db["users"].values():
        u["bp_tasks_done"] = []; u["level"] = 1; u["xp"] = 0
    save_db(db); await state.clear()
    await message.answer(f"✅ Сезон <b>{message.text}</b> розпочато! Прогрес скинуто.")

@dp.message(Command("bp_tasks"))
async def bp_tasks_cmd(message: Message, command: CommandObject):
    db = load_db(); u = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(u["role"], 0) < ROLES["Куратор модерації"]:
        return await message.answer("❌ Немає прав.")
    if not command.args:
        tasks = db["bp"].get("tasks", [])
        if not tasks: return await message.answer("📋 Завдань немає.\n<code>/bp_tasks [текст] [xp]</code>")
        return await message.answer("📋 <b>Завдання BP:</b>\n" + "\n".join(
            f"{i+1}. {t['text']} (+{t['xp']} XP)" for i, t in enumerate(tasks)
        ))
    parts = command.args.rsplit(None, 1)
    if len(parts) < 2 or not parts[1].isdigit():
        return await message.answer("📝: <code>/bp_tasks [текст] [xp]</code>")
    db["bp"].setdefault("tasks", []).append({"text": parts[0], "xp": int(parts[1])})
    save_db(db)
    await message.answer(f"✅ Завдання: <b>{parts[0]}</b> (+{parts[1]} XP)")

@dp.message(Command("bp_rewards"))
async def bp_rewards_cmd(message: Message, command: CommandObject):
    db = load_db(); u = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(u["role"], 0) < ROLES["Куратор модерації"]:
        return await message.answer("❌ Немає прав.")
    if not command.args:
        rewards = db["bp"].get("rewards", {})
        if not rewards: return await message.answer("🎁 Нагород немає.\n<code>/bp_rewards [рівень] [нагорода]</code>")
        return await message.answer("🎁 <b>Нагороди:</b>\n" + "\n".join(
            f"Рівень {lvl}: {rew}" for lvl, rew in sorted(rewards.items(), key=lambda x: int(x[0]))
        ))
    parts = command.args.split(None, 1)
    if len(parts) < 2 or not parts[0].isdigit():
        return await message.answer("📝: <code>/bp_rewards [рівень] [нагорода]</code>")
    db["bp"].setdefault("rewards", {})[parts[0]] = parts[1]
    save_db(db)
    await message.answer(f"✅ Нагорода рівня {parts[0]}: <b>{parts[1]}</b>")

# ═══════════════════════════════════════════════
#  ДОСЯГНЕННЯ
# ═══════════════════════════════════════════════
ACH_LABELS = {
    "first_mute":    "🔇 Перший мут",   "mute_10":    "🔇 10 мутів",
    "ban_5":         "🔨 5 банів",       "help_100":   "🤝 100 допомог",
    "tickets_50":    "🎫 50 тікетів",    "points_100": "💰 100 балів",
    "points_500":    "💰 500 балів",     "points_1000":"💰 1000 балів",
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
        args = event.text.split()[1:]
        target_id = next(
            (uid for uid, d in db["users"].items() if d["tag"] == args[0].lower()), str(event.from_user.id)
        ) if args else str(event.from_user.id)
    u   = get_user(db, target_id)
    ach = u.get("achievements", [])
    text = (f"🏅 <b>{u['nick']}</b> ще не має досягнень." if not ach else
            f"🏅 <b>Досягнення {u['nick']}:</b>\n\n" +
            "\n".join(f"✅ {ACH_LABELS.get(k, k)}" for k in ach) +
            f"\n\nВсього: {len(ach)}/{len(ACH_LABELS)}")
    await message.answer(text)
    if is_cb: await event.answer()

# ═══════════════════════════════════════════════
#  РЕПУТАЦІЯ
# ═══════════════════════════════════════════════
_rep_cd: dict[str, dict[str, float]] = defaultdict(dict)

@dp.callback_query(F.data.startswith("rep_"))
async def give_reputation(call: CallbackQuery):
    giver_id  = str(call.from_user.id)
    target_id = call.data.split("_")[1]
    if giver_id == target_id:
        return await call.answer("❌ Не можна давати репутацію собі.", show_alert=True)
    now = asyncio.get_event_loop().time()
    if now - _rep_cd[giver_id].get(target_id, 0) < 86400:
        return await call.answer("⏳ Раз на добу.", show_alert=True)
    db = load_db()
    tu = db["users"].get(target_id)
    if not tu: return await call.answer("Не знайдено.", show_alert=True)
    tu["reputation"] = tu.get("reputation", 0) + 1
    _rep_cd[giver_id][target_id] = now
    save_db(db)
    await call.answer(f"👍 +1 репутація {tu['nick']}!", show_alert=True)

# ═══════════════════════════════════════════════
#  НОРМИ
# ═══════════════════════════════════════════════
@dp.message(Command("norms"))
async def cmd_norms(message: Message):
    db    = load_db()
    u     = get_user(db, message.from_user.id, message.from_user)
    norms = db.get("norms", {"weekly": 50, "monthly": 200})
    w     = u.get("weekly_points",  0)
    m     = u.get("monthly_points", 0)
    w_pct = min(int(w / norms["weekly"]  * 100), 100)
    m_pct = min(int(m / norms["monthly"] * 100), 100)
    w_bar = "█" * (w_pct // 10) + "░" * (10 - w_pct // 10)
    m_bar = "█" * (m_pct // 10) + "░" * (10 - m_pct // 10)
    warn  = "\n\n⚠️ <b>Увага!</b> Тижнева норма < 50%!" if w_pct < 50 else ""
    await message.answer(
        f"📈 <b>Норми — {u['nick']}</b>\n\n"
        f"📅 Тижнева: {w}/{norms['weekly']}\n[{w_bar}] {w_pct}%\n\n"
        f"📆 Місячна: {m}/{norms['monthly']}\n[{m_bar}] {m_pct}%{warn}"
    )

# ═══════════════════════════════════════════════
#  АДМІН ПАНЕЛЬ
# ═══════════════════════════════════════════════
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    db = load_db(); u = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(u["role"], 0) < ROLES["Куратор модерації"]:
        return await message.answer("❌ Доступ заборонено.")
    await message.answer("⚙️ <b>ПАНЕЛЬ АДМІНІСТРАТОРА</b>", reply_markup=admin_markup())

@dp.callback_query(F.data == "admin_claims")
async def admin_claims(call: CallbackQuery):
    db = load_db()
    if ROLES.get(get_user(db, call.from_user.id)["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌", show_alert=True)
    pending = [c for c in db["claims"] if c["status"] == "pending"]
    if not pending:
        await call.message.answer("📭 Немає заявок на бали."); return await call.answer()
    for c in pending[:5]:
        kb   = _reject_kb("claim", c["id"])
        text = (f"📥 <b>Заявка #{c['id']}</b>\n👤 {c['nick']} ({c['tag']})\n"
                f"💰 {c['points']} балів\n📝 {c['action']}\n📎 {c['proof']}")
        if c.get("photo_id"):
            await call.message.answer_photo(c["photo_id"], caption=text, reply_markup=kb)
        else:
            await call.message.answer(text, reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "admin_mod_claims")
async def admin_mod_claims(call: CallbackQuery):
    db = load_db()
    if ROLES.get(get_user(db, call.from_user.id)["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌", show_alert=True)
    pending = [c for c in db["mod_claims"] if c["status"] == "pending"]
    if not pending:
        await call.message.answer("📭 Немає заявок на дії модерації."); return await call.answer()
    for c in pending[:5]:
        kb   = _reject_kb("mc", c["id"])
        text = (f"🛡 <b>Заявка #{c['id']}</b>\n👤 {c['nick']} ({c['tag']})\n"
                f"🎯 {c['action']} → {c['target']}\n📝 {c['reason']}\n📎 {c['proof']}")
        if c.get("photo_id"):
            await call.message.answer_photo(c["photo_id"], caption=text, reply_markup=kb)
        else:
            await call.message.answer(text, reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "admin_promos")
async def admin_promos(call: CallbackQuery):
    db      = load_db()
    pending = [p for p in db["promotions"] if p["status"] == "pending"]
    if not pending:
        await call.message.answer("📭 Немає заявок на підвищення."); return await call.answer()
    for p in pending[:5]:
        kb = _reject_kb("promo", p["id"])
        await call.message.answer(
            f"📈 <b>Заявка #{p['id']}</b>\n👤 {p['nick']} ({p['tag']})\n"
            f"🎖 {p['role']}\n📝 {p['reason']}", reply_markup=kb
        )
    await call.answer()

@dp.callback_query(F.data == "admin_vacations")
async def admin_vacations(call: CallbackQuery):
    db      = load_db()
    pending = [v for v in db["vacations"] if v["status"] == "pending"]
    if not pending:
        await call.message.answer("📭 Немає заявок на відпустку."); return await call.answer()
    for v in pending[:5]:
        kb = _reject_kb("vac", v["id"])
        await call.message.answer(
            f"🏖 <b>Заявка #{v['id']}</b>\n👤 {v['nick']} ({v['tag']})\n"
            f"📅 {v['dates']}\n📝 {v['reason']}", reply_markup=kb
        )
    await call.answer()

@dp.callback_query(F.data == "admin_bp_claims")
async def admin_bp_claims(call: CallbackQuery):
    """[ПАТЧ 3] Панель BP заявок в адмінці."""
    db = load_db()
    if ROLES.get(get_user(db, call.from_user.id)["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌", show_alert=True)
    pending = [c for c in db.get("bp_task_claims", []) if c["status"] == "pending"]
    if not pending:
        await call.message.answer("📭 Немає заявок на BP завдання."); return await call.answer()
    for c in pending[:5]:
        kb   = _reject_kb("bp_task", c["id"])
        text = (f"🏆 <b>BP Заявка #{c['id']}</b>\n👤 {c['nick']} ({c['tag']})\n"
                f"📋 {c['task_text']} (+{c['task_xp']} XP)\n📎 {c['proof']}")
        if c.get("photo_id"):
            await call.message.answer_photo(c["photo_id"], caption=text, reply_markup=kb)
        else:
            await call.message.answer(text, reply_markup=kb)
    await call.answer()

@dp.callback_query(F.data == "admin_log")
async def admin_log(call: CallbackQuery):
    db = load_db()
    if ROLES.get(get_user(db, call.from_user.id)["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌", show_alert=True)
    logs = []
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, "r", encoding="utf-8") as f:
            logs = json.load(f)
    last = logs[-15:][::-1]
    if not last:
        await call.message.answer("📜 Журнал порожній."); return await call.answer()
    text = "📜 <b>Останні 15 дій:</b>\n\n"
    for e in last:
        text += f"[{e['ts'][:16].replace('T',' ')}] <b>{e['action']}</b> → {e.get('target','—')}\n"
    await call.message.answer(text); await call.answer()

@dp.callback_query(F.data == "admin_backup")
async def admin_backup_cb(call: CallbackQuery):
    db = load_db()
    if ROLES.get(get_user(db, call.from_user.id)["role"], 0) < ROLES["Заступник головного модератора"]:
        return await call.answer("❌ Немає прав.", show_alert=True)
    backup_db(); await call.answer("💾 Backup створено!", show_alert=True)

@dp.callback_query(F.data == "admin_norms")
async def admin_norms(call: CallbackQuery):
    db    = load_db()
    norms = db.get("norms", {"weekly": 50, "monthly": 200})
    await call.message.answer(
        f"⚙️ Норми: тижнева={norms['weekly']}, місячна={norms['monthly']}\n"
        f"Змінити: <code>/setnorm weekly [N]</code> · <code>/setnorm monthly [N]</code>"
    ); await call.answer()

@dp.message(Command("setnorm"))
async def cmd_setnorm(message: Message, command: CommandObject):
    db = load_db(); u = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(u["role"], 0) < ROLES["Заступник головного модератора"]:
        return await message.answer("❌ Немає прав.")
    if not command.args: return await message.answer("📝: <code>/setnorm [weekly|monthly] [N]</code>")
    p = command.args.split()
    if len(p) < 2 or not p[1].isdigit(): return await message.answer("❌ Невірний формат.")
    db.setdefault("norms", {})[p[0]] = int(p[1]); save_db(db)
    await message.answer(f"✅ Норму <b>{p[0]}</b> = {p[1]}")

@dp.message(Command("setpoints"))
async def cmd_setpoints(message: Message, command: CommandObject):
    db = load_db(); u = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(u["role"], 0) < ROLES["Заступник головного модератора"]:
        return await message.answer("❌ Немає прав.")
    if not command.args or len(command.args.split()) < 2:
        return await message.answer("📝: <code>/setpoints @user [±число]</code>")
    args = command.args.split()
    if not args[1].lstrip("-").isdigit(): return await message.answer("❌ Введіть число.")
    tid, tu = await _find_target(db, args[0].lower())
    if not tu: return await message.answer("❌ Не знайдено.")
    old = tu["points"]; tu["points"] = max(0, old + int(args[1]))
    save_db(db); log_action(message.from_user.id, "setpoints", tid, f"{old}→{tu['points']}")
    await message.answer(f"✅ {tu['nick']}: {old} → <b>{tu['points']}</b>")

@dp.message(Command("backup"))
async def cmd_backup(message: Message):
    db = load_db(); u = get_user(db, message.from_user.id, message.from_user)
    if ROLES.get(u["role"], 0) < ROLES["Заступник головного модератора"]:
        return await message.answer("❌ Немає прав.")
    backup_db(); await message.answer("💾 Резервну копію збережено!")

# ═══════════════════════════════════════════════
#  ПОШУК
# ═══════════════════════════════════════════════
@dp.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject):
    if not command.args: return await message.answer("📝: <code>/search [нік або @тег]</code>")
    q  = command.args.lower(); db = load_db()
    found = [(uid, d) for uid, d in db["users"].items()
             if q in d["nick"].lower() or q in d["tag"].lower()]
    if not found: return await message.answer("❌ Нічого не знайдено.")
    text = f"🔍 <b>«{command.args}»:</b>\n\n"
    for uid, d in found[:10]:
        text += f"👤 <b>{d['nick']}</b> — {d['tag']} [{d['role']}]\n"
    if len(found) > 10: text += f"\n...та ще {len(found)-10}"
    await message.answer(text)

# ═══════════════════════════════════════════════
#  АНТИФЛУД (catch-all)
# ═══════════════════════════════════════════════
@dp.message()
async def flood_guard(message: Message):
    if is_flood(message.from_user.id):
        await message.answer("⏳ Занадто швидко! Зачекайте.")

# ═══════════════════════════════════════════════
#  АВТОЗАВДАННЯ
# ═══════════════════════════════════════════════
async def weekly_reset_task():
    while True:
        await asyncio.sleep(604800)
        db    = load_db()
        norms = db.get("norms", {"weekly": 50})
        for uid, u in db["users"].items():
            if u.get("weekly_points", 0) < norms["weekly"]:
                try:
                    await bot.send_message(int(uid),
                        f"⚠️ <b>Тижнева норма не виконана!</b>\n"
                        f"{u.get('weekly_points',0)}/{norms['weekly']} балів.")
                except Exception: pass
            u["weekly_points"] = 0
        backup_db(); save_db(db)

async def daily_norm_reminder():
    while True:
        await asyncio.sleep(172800)
        db    = load_db()
        norms = db.get("norms", {"weekly": 50})
        for uid, u in db["users"].items():
            if u.get("weekly_points", 0) < norms["weekly"] // 2:
                try:
                    await bot.send_message(int(uid),
                        f"📢 Виконано {u.get('weekly_points',0)}/{norms['weekly']} тижневої норми.")
                except Exception: pass

# ═══════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════
async def main():
    logging.info("💎 UA ONLINE BOT v2.1 STARTED")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    asyncio.create_task(weekly_reset_task())
    asyncio.create_task(daily_norm_reminder())
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
