"""
UA ONLINE BOT — v2.3
Патч v2.3:
  - [1] Прибрано блок статистики (мути/бани/попередження/кіки/допомога/тікети/скарги) з профілю
  - [2] Магазин: додано товар "Попередження" (warn) через заявку куратору
  - [3] Магазин: /shop_edit — редагування цін/назв товарів (тільки Головний модератор)
  - [4] При будь-якій покупці — авто-сповіщення всіх кураторів і вище
  - [5] Виправлено BP заявки: тепер коректно обробляються і фото, і текст
"""

import asyncio
import json
import os
import logging
import shutil
from datetime import datetime, timedelta
from collections import defaultdict
from typing import Callable, Dict, Any, Awaitable

from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton,
    CallbackQuery, TelegramObject
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# ═══════════════════════════════════════════════
#  КОНФІГУРАЦІЯ
# ═══════════════════════════════════════════════
TOKEN      = "8905769390:AAHA1YUTQti2_diRFLa2f9KRYGc1QKdJ4ys"
ADMIN_ID   = 1188859918
DB_FILE    = "ua_online_db.json"
LOG_FILE   = "ua_online_log.json"
BACKUP_DIR = "backups"

CLAIMS_CHANNEL_ID: int | None = None
INACTIVITY_DAYS = 7

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
    "kick":   {"points": 5,  "xp": 20},
}

XP_PER_LEVEL = 1000
DAILY_BONUS  = 20

# ═══════════════════════════════════════════════
#  FSM СТАНИ
# ═══════════════════════════════════════════════
class Form(StatesGroup):
    # Профіль
    edit_nick    = State()
    edit_bio     = State()
    edit_discord = State()
    edit_sex     = State()
    edit_age     = State()

    # Пропозиція покарання
    punish_target  = State()
    punish_action  = State()
    punish_reason  = State()
    punish_proof   = State()

    # Репорт багу
    bug_desc  = State()
    bug_proof = State()

    # Заявка на бали
    claim_action = State()
    claim_count  = State()
    claim_proof  = State()

    # Підвищення
    promo_reason = State()

    # Відпустка
    vacation_dates  = State()
    vacation_reason = State()

    # BP
    bp_create_name = State()
    bp_task_idx    = State()
    bp_task_proof  = State()   # ← ВИПРАВЛЕНО: окремий стан для доказу BP

    # Причина відхилення
    reject_type   = State()
    reject_id     = State()
    reject_reason = State()

    # Догани
    reprimand_target = State()
    reprimand_reason = State()

    # [v2.3] Магазин — редагування
    shop_edit_item  = State()
    shop_edit_field = State()
    shop_edit_value = State()

# ═══════════════════════════════════════════════
#  АНТИФЛУД
# ═══════════════════════════════════════════════
_flood: dict[int, list[float]] = defaultdict(list)
FLOOD_LIMIT  = 5
FLOOD_WINDOW = 10

def is_flood(user_id: int) -> bool:
    now    = asyncio.get_event_loop().time()
    stamps = _flood[user_id]
    stamps[:] = [t for t in stamps if now - t < FLOOD_WINDOW]
    stamps.append(now)
    return len(stamps) > FLOOD_LIMIT

# ═══════════════════════════════════════════════
#  [1] ЗАХИСТ FSM — ДОДАЙ В MIDDLEWARE
#  Замінює FSMTimeoutMiddleware
# ═══════════════════════════════════════════════

class FSMTimeoutMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any]
    ) -> Any:
        state: FSMContext = data.get("state")
        if state:
            current_state = await state.get_state()
            if current_state:
                state_data = await state.get_data()
                ts = state_data.get("fsm_ts", 0)
                now = datetime.now().timestamp()
                if ts and now - ts > 600:
                    await state.clear()
                    if isinstance(event, Message):
                        await event.answer("⏰ Час дії вийшов. Почни заново.")
                    return
                await state.update_data(fsm_ts=now)

                # Перевірка власника FSM для Message
                fsm_owner = state_data.get("fsm_user_id")
                if fsm_owner and isinstance(event, Message):
                    if event.from_user.id != fsm_owner:
                        return  # мовчки ігноруємо чужі повідомлення

        return await handler(event, data)

# ═══════════════════════════════════════════════
#  БАЗА ДАНИХ
# ═══════════════════════════════════════════════
def _default_db():
    return {
        "users":          {},
        "bp":             {"season": 1, "name": "Сезон 1", "tasks": [], "rewards": {}},
        "claims":         [],
        "punish_claims":  [],
        "bug_reports":    [],
        "bp_task_claims": [],
        "promotions":     [],
        "vacations":      [],
        "reprimands":     [],
        "events":         [],
        "norms":          {"weekly": 50, "monthly": 200},
        # ─── МАГАЗИН v2.3 ───────────────────────────────────────────────
        "shop": {
            "items": {
                "promote_mod": {
                    "name":         "Підвищення до Модератора",
                    "price":        800,
                    "cooldown_days": 30,
                    "type":         "role",
                    "role":         "Модератор",
                    "notify_curators": True,
                    "require_claim":   True,   # вимагає схвалення куратора
                },
                "promote_senior": {
                    "name":         "Підвищення до Ст. модератора",
                    "price":        1500,
                    "cooldown_days": 45,
                    "type":         "role",
                    "role":         "Ст. модератор",
                    "notify_curators": True,
                    "require_claim":   True,
                },
                "remove_reprimand": {
                    "name":         "Зняти 1 догану",
                    "price":        400,
                    "cooldown_days": 7,
                    "type":         "remove_reprimand",
                    "notify_curators": True,
                    "require_claim":   False,  # виконується одразу
                },
                # ─── [v2.3] НОВИЙ ТОВАР ──────────────────────────────────
                "remove_warning": {
                    "name":         "Зняти 1 попередження",
                    "price":        200,
                    "cooldown_days": 3,
                    "type":         "remove_warning",
                    "notify_curators": True,
                    "require_claim":   False,
                },
            }
        }
    }

def load_db() -> dict:
    if not os.path.exists(DB_FILE):
        return _default_db()
    with open(DB_FILE, "r", encoding="utf-8") as f:
        db = json.load(f)
    default = _default_db()
    for key, val in default.items():
        if key not in db:
            db[key] = val
    # Підмергуємо нові товари магазину без затирання старих
    for item_id, item_data in default["shop"]["items"].items():
        if item_id not in db["shop"]["items"]:
            db["shop"]["items"][item_id] = item_data
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
            "nick":          user_obj.first_name if user_obj else "Unknown",
            "tag":           f"@{user_obj.username}".lower() if user_obj and user_obj.username else "немає",
            "discord":       "Не вказано",
            "bio":           "Персонал UA ONLINE",
            "sex":           "N/A",
            "age":           "N/A",
            "role":          "Молодший Модератор",
            "saved_role":    None,
            "points":        0,
            "xp":            0,
            "level":         1,
            "reputation":    0,
            # статистика залишається у БД (для /stats), але НЕ показується в профілі
            "stats":         {"mutes": 0, "bans": 0, "warns": 0, "help": 0,
                              "tickets": 0, "complaints": 0, "kicks": 0},
            "achievements":       [],
            "bp_tasks_done":      [],
            "daily_last":         None,
            "last_active":        datetime.now().isoformat(),
            "vacation":           None,
            "reprimands":         0,
            "warnings":           0,   # [v2.3] окремий лічильник попереджень
            "weekly_points":      0,
            "monthly_points":     0,
            "reg_date":           datetime.now().strftime("%d.%m.%Y"),
            "inactivity_warned":  False,
            "shop_purchases":     {},  # item_id → last_purchase ISO
        }
        if int(uid) == ADMIN_ID:
            db["users"][uid]["role"] = "Головний модератор"
    # Міграція: додаємо нові поля до існуючих юзерів
    u = db["users"][uid]
    if "warnings" not in u:
        u["warnings"] = 0
    if "shop_purchases" not in u:
        u["shop_purchases"] = {}
    return u

def touch_active(db, user_id):
    uid = str(user_id)
    if uid in db["users"]:
        db["users"][uid]["last_active"]       = datetime.now().isoformat()
        db["users"][uid]["inactivity_warned"] = False

def add_points(u: dict, action: str):
    p = POINTS_TABLE.get(action, {"points": 0, "xp": 0})
    u["points"]         += p["points"]
    u["xp"]             += p["xp"]
    u["weekly_points"]   = u.get("weekly_points",  0) + p["points"]
    u["monthly_points"]  = u.get("monthly_points", 0) + p["points"]
    while u["xp"] >= XP_PER_LEVEL:
        u["xp"]   -= XP_PER_LEVEL
        u["level"] += 1

# ═══════════════════════════════════════════════
#  БОТ
# ═══════════════════════════════════════════════
bot = Bot(token=TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp  = Dispatcher()
dp.message.middleware(FSMTimeoutMiddleware())

async def _find_target(db, mention: str):
    mention = mention.lower().lstrip("@")
    for uid, d in db["users"].items():
        if d["tag"].lstrip("@") == mention or uid == mention:
            return uid, d
    return None, None

# ═══════════════════════════════════════════════
#  НАДСИЛАННЯ ЗАЯВОК
# ═══════════════════════════════════════════════
async def _send_claim_to_channel(text: str, kb: InlineKeyboardMarkup, photo_id: str | None = None):
    if not CLAIMS_CHANNEL_ID:
        return
    try:
        if photo_id:
            await bot.send_photo(CLAIMS_CHANNEL_ID, photo=photo_id, caption=text, reply_markup=kb)
        else:
            await bot.send_message(CLAIMS_CHANNEL_ID, text, reply_markup=kb)
    except Exception as e:
        logging.warning(f"Канал: {e}")

async def _notify_curators(db, text: str, kb: InlineKeyboardMarkup | None = None,
                           min_role="Куратор модерації"):
    curators = [uid for uid, d in db["users"].items()
                if ROLES.get(d["role"], 0) >= ROLES[min_role]]
    for cuid in curators:
        try:
            await bot.send_message(int(cuid), text, reply_markup=kb)
        except Exception:
            pass

async def _notify_curators_with_photo(db, photo_id: str | None, caption: str,
                                      kb: InlineKeyboardMarkup, min_role="Куратор модерації"):
    await _send_claim_to_channel(caption, kb, photo_id)
    curators = [uid for uid, d in db["users"].items()
                if ROLES.get(d["role"], 0) >= ROLES[min_role]]
    for cuid in curators:
        try:
            if photo_id:
                await bot.send_photo(int(cuid), photo=photo_id, caption=caption, reply_markup=kb)
            else:
                await bot.send_message(int(cuid), caption, reply_markup=kb)
        except Exception:
            pass

def _claim_header(u: dict) -> str:
    return (
        f"👤 <b>Подав:</b> {u['nick']}\n"
        f"🔹 <b>Telegram:</b> {u['tag']}\n"
        f"🎮 <b>Discord:</b> <code>{u['discord']}</code>\n"
        f"🎖 <b>Роль:</b> {u['role']}\n"
        f"🕐 <b>Дата:</b> {datetime.now().strftime('%d.%m.%Y %H:%M')}\n"
    )

# ═══════════════════════════════════════════════
#  КЛАВІАТУРИ
# ═══════════════════════════════════════════════
def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="❌ Скасувати", callback_data="fsm_cancel")
    ]])

def profile_markup(uid: str, is_owner: bool) -> InlineKeyboardMarkup:
    rows = [[
        InlineKeyboardButton(text="🏆 Battle Pass", callback_data="bp_view"),
        InlineKeyboardButton(text="🏅 Досягнення",  callback_data=f"ach_{uid}"),
    ]]
    if is_owner:
        rows += [
            [InlineKeyboardButton(text="⚙️ Редагувати",     callback_data="edit_menu"),
             InlineKeyboardButton(text="📥 Заявка на бали",  callback_data="claim_start")],
            [InlineKeyboardButton(text="📈 На підвищення",   callback_data="promo_start"),
             InlineKeyboardButton(text="🏖 Відпустка",       callback_data="vacation_menu")],
            [InlineKeyboardButton(text="🛒 Магазин",         callback_data="shop_view"),
             InlineKeyboardButton(text="🐛 Репорт багу",     callback_data="bug_start")],
        ]
    else:
        rows += [
            [InlineKeyboardButton(text="👍 Репутація +1",    callback_data=f"rep_{uid}"),
             InlineKeyboardButton(text="🛒 Магазин",         callback_data="shop_view")],
            [InlineKeyboardButton(text="⚖️ Запропонувати покарання",
                                  callback_data=f"punish_start_{uid}")],
        ]
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
        [InlineKeyboardButton(text="📋 Заявки на бали",      callback_data="admin_claims"),
         InlineKeyboardButton(text="⚖️ Пропозиції покарань", callback_data="admin_punish")],
        [InlineKeyboardButton(text="📈 На підвищення",       callback_data="admin_promos"),
         InlineKeyboardButton(text="🏖 Відпустки",           callback_data="admin_vacations")],
        [InlineKeyboardButton(text="🏆 BP Заявки",           callback_data="admin_bp_claims"),
         InlineKeyboardButton(text="🐛 Репорти багів",       callback_data="admin_bugs")],
        [InlineKeyboardButton(text="😴 Неактивні",           callback_data="admin_inactive"),
         InlineKeyboardButton(text="📜 Журнал",              callback_data="admin_log")],
        [InlineKeyboardButton(text="💾 Backup",              callback_data="admin_backup"),
         InlineKeyboardButton(text="⚙️ Норми",               callback_data="admin_norms")],
        [InlineKeyboardButton(text="🛒 Редагувати магазин",  callback_data="admin_shop_edit")],
    ])

# ==========================================
# СКАСУВАННЯ ФОРМ
# ==========================================

@dp.message(Command("cancel"))
async def cmd_cancel_global(message: Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state is None:
        return
    await state.clear()
    await message.answer("🚫 Дію скасовано.")

@dp.callback_query(F.data == "fsm_cancel")
async def cancel_fsm_button(call: CallbackQuery, state: FSMContext):
    state_data = await state.get_data()
    fsm_owner = state_data.get("fsm_user_id")

    if fsm_owner and call.from_user.id != fsm_owner:
        return await call.answer("❌ Скасувати може тільки власник!")

    await state.clear()
    try:
        await call.message.edit_text(
            call.message.text + "\n\n<i>🚫 Дію скасовано.</i>",
            reply_markup=None
        )
    except Exception:
        await call.message.answer("🚫 Дію скасовано.")
    
    await call.answer("Скасовано")

# ═══════════════════════════════════════════════
#  ЗАХИСТ cancel кнопки — замінює існуючий
# ═══════════════════════════════════════════════

@dp.callback_query(F.data == "fsm_cancel")
async def cancel_fsm_button(call: CallbackQuery, state: FSMContext):
    state_data = await state.get_data()
    fsm_owner  = state_data.get("fsm_user_id")

    if fsm_owner and call.from_user.id != fsm_owner:
        return await call.answer("❌ Скасувати може тільки автор дії.", show_alert=True)

    await state.clear()
    try:
        await call.message.edit_text(
            call.message.text + "\n\n<i>🚫 Дію скасовано.</i>",
            reply_markup=None
        )
    except Exception:
        await call.message.answer("🚫 Дію скасовано.")
    await call.answer("Скасовано")

# ═══════════════════════════════════════════════
#  /START  /HELP
# ═══════════════════════════════════════════════
@dp.message(Command("start"))
async def cmd_start(message: Message):
    db = load_db()
    u  = get_user(db, message.from_user.id, message.from_user)
    touch_active(db, message.from_user.id)
    save_db(db)
    await message.answer(
        f"👋 Ласкаво просимо, <b>{u['nick']}</b>!\n\n"
        "🤖 <b>UA ONLINE BOT v2.3 by.Kalimanov</b>\n\n"
        "/profile — профіль\n/top — рейтинг\n/daily — бонус\n"
        "/shop — магазин\n/punish — запропонувати покарання\n"
        "/bugreport — репорт багу\n/help — всі команди"
    )

@dp.message(Command("help"))
async def cmd_help(message: Message):
    await message.answer(
        "📖 <b>КОМАНДИ UA ONLINE BOT v2.3</b>\n\n"
        "👤 <b>Профіль:</b>\n"
        "/profile [@user] · /editprofile · /stats\n\n"
        "🏅 <b>Прогрес:</b>\n"
        "/top · /bp · /daily · /claimpoints · /achievements\n\n"
        "🛒 <b>Магазин:</b>\n"
        "/shop — переглянути товари та купити\n"
        "/shop_edit — редагування магазину (тільки Головний модератор)\n\n"
        "⚖️ <b>Покарання:</b>\n"
        "/punish @user — запропонувати покарання → куратор вирішує\n\n"
        "🐛 <b>Репорт багу:</b>\n"
        "/bugreport\n\n"
        "📋 <b>Персонал:</b>\n"
        "/promote @user [роль] · /demote · /reprimand · /vacation\n"
        "/restore_role @user — відновити збережену роль\n\n"
        "😴 <b>Неактив:</b>\n"
        "/inactive · /reset_inactive @user\n\n"
        "⚙️ <b>Адмін:</b>\n"
        "/admin · /backup · /setpoints · /setnorm\n"
        "/bp_create · /bp_tasks · /bp_rewards"
    )

# ═══════════════════════════════════════════════
#  /PROFILE — [v2.3] БЕЗ БЛОКУ СТАТИСТИКИ
# ═══════════════════════════════════════════════

@dp.message(Command("profile"))
async def cmd_profile(message: Message, command: CommandObject):
    db        = load_db()
    viewer_id = str(message.from_user.id)
    target_id = viewer_id

    touch_active(db, message.from_user.id)
    get_user(db, message.from_user.id, message.from_user)

    if command.args:
        mention = command.args.split()[0].lower().lstrip("@")
        found   = next(
            (uid for uid, d in db["users"].items()
             if d.get("tag", "").lstrip("@").lower() == mention or uid == mention),
            None
        )
        if found:
            target_id = found
        else:
            save_db(db)
            return await message.answer("❌ Користувача не знайдено.")

    u = db["users"].get(target_id)
    if not u:
        save_db(db)
        return await message.answer("❌ Профіль не знайдено.")

    is_owner  = viewer_id == target_id
    caller    = get_user(db, message.from_user.id)
    can_see_details = (
        is_owner or
        ROLES.get(caller["role"], 0) >= ROLES["Куратор модерації"] or
        message.from_user.id == ADMIN_ID
    )

    xp_filled = int((u.get("xp", 0) / XP_PER_LEVEL) * 10)
    xp_bar    = "█" * xp_filled + "░" * (10 - xp_filled)

    vacation_str  = f"\n🏖 <b>Відпустка:</b> {u['vacation']}" if u.get("vacation") else ""

    # Догани
    rep_count = u.get("reprimands", 0)
    if rep_count > 0:
        if can_see_details:
            last_rep = u.get("reprimands_history", [])
            if last_rep:
                lr = last_rep[-1]
                reprimand_str = (
                    f"\n🔴 <b>Догани:</b> {rep_count} "
                    f"(остання: {lr.get('ts','?')} від {lr.get('by_nick','?')} — {lr.get('reason','—')})"
                )
            else:
                reprimand_str = f"\n🔴 <b>Догани:</b> {rep_count}"
        else:
            reprimand_str = f"\n🔴 <b>Догани:</b> {rep_count}"
    else:
        reprimand_str = ""

    # Попередження
    warn_count = u.get("warnings", 0)
    if warn_count > 0:
        if can_see_details:
            last_warn = u.get("warnings_history", [])
            if last_warn:
                lw = last_warn[-1]
                warning_str = (
                    f"\n⚠️ <b>Попередження:</b> {warn_count} "
                    f"(останнє: {lw.get('ts','?')} від {lw.get('by_nick','?')} — {lw.get('reason','—')})"
                )
            else:
                warning_str = f"\n⚠️ <b>Попередження:</b> {warn_count}"
        else:
            warning_str = f"\n⚠️ <b>Попередження:</b> {warn_count}"
    else:
        warning_str = ""

    inactive_str = ""
    last_active  = u.get("last_active")
    if last_active:
        try:
            delta = datetime.now() - datetime.fromisoformat(last_active)
            if delta.days >= INACTIVITY_DAYS and not u.get("vacation"):
                inactive_str = f"\n😴 <b>Неактивний:</b> {delta.days} дн."
        except Exception:
            pass

    text = (
        f"╔══════════════════════╗\n"
        f"  👤 <b>{u.get('nick','Unknown')}</b>\n"
        f"  🎖 <code>{u.get('role','—')}</code>\n"
        f"╚══════════════════════╝\n\n"
        f"📋 <b>Telegram:</b> {u.get('tag','—')}\n"
        f"🎮 <b>Discord:</b> <code>{u.get('discord','—')}</code>\n"
        f"⚧ <b>Стать:</b> {u.get('sex','—')}  🎂 <b>Вік:</b> {u.get('age','—')}\n"
        f"📝 <b>Біо:</b> {u.get('bio','—')}\n\n"
        f"⭐ <b>Рівень:</b> {u.get('level',1)}  🏅 <b>Бали:</b> {u.get('points',0)}\n"
        f"📊 XP: [{xp_bar}] {u.get('xp',0)}/{XP_PER_LEVEL}\n"
        f"👥 <b>Репутація:</b> {u.get('reputation',0)}\n"
        f"📅 <b>Реєстрація:</b> {u.get('reg_date','N/A')}"
        f"{vacation_str}{reprimand_str}{warning_str}{inactive_str}"
    )

    # Кнопки "детальніше" якщо є догани/попередження
    extra_kb = []
    if rep_count > 0 and can_see_details:
        extra_kb.append(InlineKeyboardButton(
            text=f"🔴 Всі догани ({rep_count})",
            callback_data=f"show_reprimands_{target_id}"
        ))
    if warn_count > 0 and can_see_details:
        extra_kb.append(InlineKeyboardButton(
            text=f"⚠️ Всі попередження ({warn_count})",
            callback_data=f"show_warns_{target_id}"
        ))

    markup = profile_markup(target_id, is_owner)
    if extra_kb:
        markup.inline_keyboard.append(extra_kb)

    save_db(db)
    await message.answer(text, reply_markup=markup)


# Callback кнопки для перегляду повної історії з профілю
@dp.callback_query(F.data.startswith("show_reprimands_"))
async def cb_show_reprimands(call: CallbackQuery):
    uid = call.data.split("show_reprimands_")[1]
    db  = load_db()
    u   = db["users"].get(uid)
    if not u:
        return await call.answer("❌ Не знайдено.", show_alert=True)

    history = u.get("reprimands_history", [])
    count   = u.get("reprimands", 0)
    lines   = [f"🔴 <b>Догани {u['nick']}</b> — всього: {count}\n"]
    for i, r in enumerate(history, 1):
        lines.append(
            f"{i}. 👮 {r.get('by_nick','?')} | 📅 {r.get('ts','?')}\n"
            f"   📝 {r.get('reason','—')}"
        )
    await call.message.answer("\n".join(lines) if history else f"✅ У {u['nick']} немає доган.")
    await call.answer()


@dp.callback_query(F.data.startswith("show_warns_"))
async def cb_show_warns(call: CallbackQuery):
    uid = call.data.split("show_warns_")[1]
    db  = load_db()
    u   = db["users"].get(uid)
    if not u:
        return await call.answer("❌ Не знайдено.", show_alert=True)

    history = u.get("warnings_history", [])
    count   = u.get("warnings", 0)
    lines   = [f"⚠️ <b>Попередження {u['nick']}</b> — всього: {count}\n"]
    for i, w in enumerate(history, 1):
        lines.append(
            f"{i}. 👮 {w.get('by_nick','?')} | 📅 {w.get('ts','?')}\n"
            f"   📝 {w.get('reason','—')}"
        )
    await call.message.answer("\n".join(lines) if history else f"✅ У {u['nick']} немає попереджень.")
    await call.answer()

# ═══════════════════════════════════════════════
#  МАГАЗИН — ПЕРЕГЛЯД
# ═══════════════════════════════════════════════
def _shop_text(db: dict) -> str:
    items = db["shop"]["items"]
    lines = ["🛒 <b>МАГАЗИН UA ONLINE</b>\n"]
    for item_id, item in items.items():
        cooldown = item.get("cooldown_days", 0)
        flag     = "📋 Потребує схвалення" if item.get("require_claim") else "✅ Одразу"
        lines.append(
            f"<b>{item['name']}</b>\n"
            f"💰 Ціна: <code>{item['price']}</code> балів\n"
            f"⏳ Кулдаун: {cooldown} дн.  |  {flag}\n"
            f"🆔 ID: <code>{item_id}</code>\n"
        )
    lines.append("\nДля покупки: /buy &lt;item_id&gt;")
    return "\n".join(lines)

@dp.message(Command("shop"))
async def cmd_shop(message: Message):
    db = load_db()
    await message.answer(_shop_text(db))

@dp.callback_query(F.data == "shop_view")
async def cb_shop_view(call: CallbackQuery):
    db = load_db()
    await call.message.answer(_shop_text(db))
    await call.answer()

# ==========================================
# [5] /BUY - ОБРОБКА ПОКУПКИ
# ==========================================

@dp.message(Command("buy"))
async def cmd_buy(message: Message, command: CommandObject):
    db = load_db()
    user_id = str(message.from_user.id)
    item_id = command.args

    if not item_id:
        return await message.answer("⚠️ Введіть ID товару: <code>/buy remove_warning</code>")

    items = db.get("shop", {}).get("items", {})
    item = items.get(item_id)
    u = db["users"].get(user_id)

    if not item:
        return await message.answer("❌ Такого товару не існує в магазині.")
    if not u:
        return await message.answer("❌ Вас не знайдено в базі даних.")

    user_balance = u.get("points", 0)
    item_price = item.get("price", 0)

    if user_balance < item_price:
        return await message.answer(f"❌ Недостатньо балів! Потрібно: {item_price}, у вас: {user_balance}")

    success_action = False

    if item_id == "remove_warning":
        if u.get("warnings", 0) > 0:
            u["warnings"] -= 1
            success_action = True
            response_text = "✅ Ви купили зняття попередження!"
        else:
            return await message.answer("❌ У вас немає попереджень для зняття.")

    elif item_id == "remove_reprimand":
        if u.get("reprimands", 0) > 0:
            u["reprimands"] -= 1
            success_action = True
            response_text = "✅ Ви купили зняття догани!"
        else:
            return await message.answer("❌ У вас немає доган для зняття.")
    
    else:
        success_action = True
        response_text = f"📦 Запит на купівлю <b>{item['name']}</b> надіслано кураторам!"

    if success_action:
        u["points"] -= item_price
        save_db(db)
        await message.answer(response_text)

        if item.get("notify_curators"):
            from datetime import datetime
            notify_text = (
                f"🛒 <b>Покупка в магазині</b>\n\n"
                f"👤 <b>{u.get('nick', 'Без ніка')}</b> (<code>{u.get('tag', 'ID:'+user_id)}</code>)\n"
                f"🎖 Роль: {u.get('role', 'Користувач')}\n"
                f"📦 Товар: <b>{item['name']}</b>\n"
                f"💰 Витрачено: <b>{item_price}</b> балів\n"
                f"🕒 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
            )
            
            kb = None
            if item.get("type") == "role" and item.get("require_approval"):
                kb = InlineKeyboardMarkup(inline_keyboard=[[
                    InlineKeyboardButton(text="✅ Схвалити", callback_data=f"approve_{user_id}_{item_id}"),
                    InlineKeyboardButton(text="❌ Відхилити", callback_data=f"decline_{user_id}_{item_id}")
                ]])
            
            await _notify_curators(db, notify_text, kb)

# ==========================================
# ДОПОМІЖНА ФУНКЦІЯ (має бути окремо)
# ==========================================
async def _notify_curators(db, text: str, kb: InlineKeyboardMarkup = None, min_role="Куратор модерації"):
    curators = [uid for uid, d in db["users"].items() 
                if ROLES.get(d.get("role", ""), 0) >= ROLES.get(min_role, 0)]
    
    for cuid in curators:
        try:
            await bot.send_message(int(cuid), text, reply_markup=kb)
        except Exception:
            pass


# ═══════════════════════════════════════════════
#  СХВАЛЕННЯ/ВІДХИЛЕННЯ ПОКУПКИ РОЛІ
# ═══════════════════════════════════════════════
@dp.callback_query(F.data.startswith("shop_approve_"))
async def cb_shop_approve(call: CallbackQuery):
    db       = load_db()
    curator  = get_user(db, call.from_user.id)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Тільки куратори можуть схвалювати.", show_alert=True)

    claim_id = int(call.data.split("_")[-1])
    claims   = db.get("shop_claims", [])
    claim    = next((c for c in claims if c["id"] == claim_id), None)
    if not claim or claim["status"] != "pending":
        return await call.answer("❌ Заявку вже оброблено або не знайдено.", show_alert=True)

    claim["status"] = "approved"
    uid  = claim["uid"]
    u    = db["users"].get(uid)
    item = claim["item"]

    if u and item.get("role"):
        u["role"] = item["role"]
        save_db(db)
        log_action(call.from_user.id, "shop_approve", target=uid, details=claim["item_id"])

        await call.message.edit_text(call.message.text + f"\n\n✅ Схвалено: {curator['nick']}", reply_markup=None)
        try:
            await bot.send_message(int(uid),
                f"✅ Твою заявку на <b>{item['name']}</b> схвалено!\n"
                f"🎖 Нова роль: <b>{item['role']}</b>")
        except Exception:
            pass
    await call.answer("Схвалено!")

@dp.callback_query(F.data.startswith("shop_reject_"))
async def cb_shop_reject(call: CallbackQuery):
    db      = load_db()
    curator = get_user(db, call.from_user.id)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Тільки куратори можуть відхиляти.", show_alert=True)

    claim_id = int(call.data.split("_")[-1])
    claims   = db.get("shop_claims", [])
    claim    = next((c for c in claims if c["id"] == claim_id), None)
    if not claim or claim["status"] != "pending":
        return await call.answer("❌ Заявку вже оброблено.", show_alert=True)

    claim["status"] = "rejected"
    uid  = claim["uid"]
    u    = db["users"].get(uid)
    item = claim["item"]

    # Повертаємо бали
    if u:
        u["points"] += item["price"]
        if claim["item_id"] in u.get("shop_purchases", {}):
            del u["shop_purchases"][claim["item_id"]]

    save_db(db)
    log_action(call.from_user.id, "shop_reject", target=uid, details=claim["item_id"])
    await call.message.edit_text(call.message.text + f"\n\n❌ Відхилено: {curator['nick']}", reply_markup=None)
    try:
        await bot.send_message(int(uid),
            f"❌ Твою заявку на <b>{item['name']}</b> відхилено.\n"
            f"💰 Бали повернено: <b>{item['price']}</b>")
    except Exception:
        pass
    await call.answer("Відхилено, бали повернено.")

# ═══════════════════════════════════════════════
#  [v2.3] РЕДАГУВАННЯ МАГАЗИНУ — тільки Головний модератор
# ═══════════════════════════════════════════════
def _is_head_mod(db, user_id: int) -> bool:
    u = db["users"].get(str(user_id))
    return u is not None and u.get("role") == "Головний модератор"

@dp.message(Command("shop_edit"))
async def cmd_shop_edit(message: Message):
    db = load_db()
    if not _is_head_mod(db, message.from_user.id):
        return await message.answer("❌ Тільки Головний модератор може редагувати магазин.")

    items = db["shop"]["items"]
    lines = ["⚙️ <b>Редагування магазину</b>\n\nОберіть товар:"]
    kb_rows = []
    for item_id, item in items.items():
        lines.append(f"• <code>{item_id}</code> — {item['name']} ({item['price']} балів)")
        kb_rows.append([InlineKeyboardButton(
            text=f"✏️ {item['name']}",
            callback_data=f"shopedit_{item_id}"
        )])
    kb_rows.append([InlineKeyboardButton(text="➕ Додати товар", callback_data="shopedit_new")])

    await message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )

@dp.callback_query(F.data == "admin_shop_edit")
async def cb_admin_shop_edit(call: CallbackQuery):
    db = load_db()
    if not _is_head_mod(db, call.from_user.id):
        return await call.answer("❌ Тільки Головний модератор.", show_alert=True)

    items = db["shop"]["items"]
    kb_rows = []
    for item_id, item in items.items():
        kb_rows.append([InlineKeyboardButton(
            text=f"✏️ {item['name']} ({item['price']} б.)",
            callback_data=f"shopedit_{item_id}"
        )])
    kb_rows.append([InlineKeyboardButton(text="➕ Додати товар", callback_data="shopedit_new")])

    await call.message.edit_text(
        "⚙️ <b>Редагування магазину</b>\nОберіть товар:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows)
    )
    await call.answer()

@dp.callback_query(F.data.startswith("shopedit_"))
async def cb_shopedit_item(call: CallbackQuery, state: FSMContext):
    db = load_db()
    if not _is_head_mod(db, call.from_user.id):
        return await call.answer("❌ Тільки Головний модератор.", show_alert=True)

    item_id = call.data[len("shopedit_"):]

    if item_id == "new":
        await state.set_state(Form.shop_edit_item)
        await state.update_data(shop_action="new", fsm_user_id=call.from_user.id)
        await call.message.edit_text(
            "➕ <b>Новий товар</b>\n\n"
            "Введи дані у форматі:\n"
            "<code>item_id | Назва | Ціна | Кулдаун_днів | Тип</code>\n\n"
            "Типи: <code>remove_reprimand</code>, <code>remove_warning</code>, <code>role</code>\n\n"
            "Приклад:\n<code>my_item | Мій товар | 300 | 7 | remove_warning</code>",
            reply_markup=cancel_kb()
        )
        await call.answer()
        return

    if item_id not in db["shop"]["items"]:
        return await call.answer("❌ Товар не знайдено.", show_alert=True)

    item = db["shop"]["items"][item_id]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💰 Змінити ціну",    callback_data=f"shopfield_{item_id}_price"),
         InlineKeyboardButton(text="🏷 Змінити назву",   callback_data=f"shopfield_{item_id}_name")],
        [InlineKeyboardButton(text="⏳ Змінити кулдаун", callback_data=f"shopfield_{item_id}_cooldown"),
         InlineKeyboardButton(text="🗑 Видалити товар",  callback_data=f"shopdelete_{item_id}")],
        [InlineKeyboardButton(text="◀️ Назад",           callback_data="admin_shop_edit")],
    ])
    await call.message.edit_text(
        f"✏️ <b>{item['name']}</b>\n\n"
        f"💰 Ціна: <b>{item['price']}</b> балів\n"
        f"⏳ Кулдаун: <b>{item.get('cooldown_days', 0)}</b> дн.\n"
        f"🆔 ID: <code>{item_id}</code>",
        reply_markup=kb
    )
    await call.answer()

@dp.callback_query(F.data.startswith("shopfield_"))
async def cb_shopfield(call: CallbackQuery, state: FSMContext):
    db = load_db()
    if not _is_head_mod(db, call.from_user.id):
        return await call.answer("❌ Тільки Головний модератор.", show_alert=True)

    parts   = call.data.split("_")
    # shopfield_{item_id}_{field}
    field   = parts[-1]
    item_id = "_".join(parts[1:-1])

    field_names = {"price": "ціну (число)", "name": "назву", "cooldown": "кулдаун у днях (число)"}
    await state.set_state(Form.shop_edit_value)
    await state.update_data(
        shop_action="edit",
        shop_item_id=item_id,
        shop_field=field,
        fsm_user_id=call.from_user.id
    )
    await call.message.edit_text(
        f"✏️ Введи нове значення для <b>{field_names.get(field, field)}</b>:",
        reply_markup=cancel_kb()
    )
    await call.answer()

@dp.message(Form.shop_edit_value)
async def fsm_shop_edit_value(message: Message, state: FSMContext):
    db   = load_db()
    data = await state.get_data()

    if data.get("fsm_user_id") != message.from_user.id:
        return

    action  = data.get("shop_action")
    item_id = data.get("shop_item_id")
    field   = data.get("shop_field")
    value   = message.text.strip()

    if action == "new":
        # Парсимо рядок нового товару
        parts = [p.strip() for p in value.split("|")]
        if len(parts) < 5:
            return await message.answer(
                "❌ Невірний формат. Потрібно 5 полів через |:\n"
                "<code>item_id | Назва | Ціна | Кулдаун_днів | Тип</code>",
                reply_markup=cancel_kb()
            )
        new_id, name, price_s, cool_s, itype = parts[0], parts[1], parts[2], parts[3], parts[4]
        if not price_s.isdigit() or not cool_s.isdigit():
            return await message.answer("❌ Ціна та кулдаун мають бути числами.", reply_markup=cancel_kb())
        if new_id in db["shop"]["items"]:
            return await message.answer("❌ Товар з таким ID вже існує.", reply_markup=cancel_kb())

        db["shop"]["items"][new_id] = {
            "name":            name,
            "price":           int(price_s),
            "cooldown_days":   int(cool_s),
            "type":            itype,
            "notify_curators": True,
            "require_claim":   False,
        }
        save_db(db)
        log_action(message.from_user.id, "shop_add_item", details=new_id)
        await state.clear()
        return await message.answer(f"✅ Товар <b>{name}</b> додано до магазину!")

    # Редагування існуючого поля
    if item_id not in db["shop"]["items"]:
        await state.clear()
        return await message.answer("❌ Товар не знайдено.")

    item = db["shop"]["items"][item_id]

    if field == "price":
        if not value.isdigit():
            return await message.answer("❌ Введи число.", reply_markup=cancel_kb())
        item["price"] = int(value)
    elif field == "name":
        item["name"] = value
    elif field == "cooldown":
        if not value.isdigit():
            return await message.answer("❌ Введи число (дні).", reply_markup=cancel_kb())
        item["cooldown_days"] = int(value)
    else:
        await state.clear()
        return await message.answer("❌ Невідоме поле.")

    save_db(db)
    log_action(message.from_user.id, "shop_edit", details=f"{item_id}.{field}={value}")
    await state.clear()
    await message.answer(
        f"✅ <b>{item['name']}</b> оновлено!\n"
        f"💰 Ціна: <b>{item['price']}</b> | ⏳ Кулдаун: <b>{item.get('cooldown_days', 0)}</b> дн."
    )

@dp.callback_query(F.data.startswith("shopdelete_"))
async def cb_shopdelete(call: CallbackQuery):
    db = load_db()
    if not _is_head_mod(db, call.from_user.id):
        return await call.answer("❌ Тільки Головний модератор.", show_alert=True)

    item_id = call.data[len("shopdelete_"):]
    if item_id not in db["shop"]["items"]:
        return await call.answer("❌ Товар не знайдено.", show_alert=True)

    name = db["shop"]["items"][item_id]["name"]
    del db["shop"]["items"][item_id]
    save_db(db)
    log_action(call.from_user.id, "shop_delete", details=item_id)
    await call.message.edit_text(f"🗑 Товар <b>{name}</b> видалено з магазину.")
    await call.answer("Видалено!")

# ═══════════════════════════════════════════════
#  [v2.3] ВИПРАВЛЕННЯ BP ЗАЯВОК
# ═══════════════════════════════════════════════
@dp.callback_query(F.data.startswith("bp_submit_"))
async def cb_bp_submit_task(call: CallbackQuery, state: FSMContext):
    task_idx = int(call.data.split("_")[-1])
    await state.set_state(Form.bp_task_proof)
    await state.update_data(bp_task_idx=task_idx, fsm_user_id=call.from_user.id)
    await call.message.answer(
        "📸 <b>Надішли доказ виконання задачі</b>\n\n"
        "Це може бути фото або текстовий опис.\n"
        "Надішли одне повідомлення (фото або текст).",
        reply_markup=cancel_kb()
    )
    await call.answer()

@dp.message(Form.bp_task_proof)
async def fsm_bp_task_proof(message: Message, state: FSMContext):
    data     = await state.get_data()

    if data.get("fsm_user_id") != message.from_user.id:
        return

    task_idx = data.get("bp_task_idx")
    db       = load_db()
    u        = get_user(db, message.from_user.id, message.from_user)
    tasks    = db["bp"].get("tasks", [])

    if task_idx is None or task_idx >= len(tasks):
        await state.clear()
        return await message.answer("❌ Задачу не знайдено. Можливо BP оновився.")

    task = tasks[task_idx]

    if task_idx in u.get("bp_tasks_done", []):
        await state.clear()
        return await message.answer("ℹ️ Цю задачу ти вже виконав раніше.")

    photo_id = None
    proof_text = ""

    if message.photo:
        photo_id   = message.photo[-1].file_id
        proof_text = message.caption or "📸 Фото-доказ"
    elif message.text:
        proof_text = message.text
    else:
        return await message.answer(
            "❌ Надішли фото або текст як доказ.",
            reply_markup=cancel_kb()
        )

    claim_id = len(db["bp_task_claims"]) + 1
    db["bp_task_claims"].append({
        "id":        claim_id,
        "uid":       str(message.from_user.id),
        "task_idx":  task_idx,
        "task_name": task.get("name", f"Задача #{task_idx + 1}"),
        "proof":     proof_text,
        "photo_id":  photo_id,
        "status":    "pending",
        "ts":        datetime.now().isoformat(),
    })
    save_db(db)
    await state.clear()

    caption = (
        f"🏆 <b>BP Заявка #{claim_id}</b>\n\n"
        f"{_claim_header(u)}\n"
        f"📋 <b>Задача:</b> {task.get('name', f'#{task_idx+1}')}\n"
        f"📝 <b>Доказ:</b> {proof_text}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Зарахувати", callback_data=f"bp_approve_{claim_id}"),
        InlineKeyboardButton(text="❌ Відхилити",  callback_data=f"bp_reject_{claim_id}"),
    ]])

    await _notify_curators_with_photo(db, photo_id, caption, kb)
    log_action(message.from_user.id, "bp_claim", details=f"task={task_idx}, claim={claim_id}")

    await message.answer(
        f"✅ <b>Заявку #{claim_id} надіслано!</b>\n\n"
        f"📋 Задача: <b>{task.get('name', f'#{task_idx+1}')}</b>\n"
        f"Очікуй рішення куратора."
    )

@dp.callback_query(F.data.startswith("bp_approve_"))
async def cb_bp_approve(call: CallbackQuery):
    db      = load_db()
    curator = get_user(db, call.from_user.id)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Тільки куратори можуть схвалювати.", show_alert=True)

    claim_id = int(call.data.split("_")[-1])
    claim    = next((c for c in db["bp_task_claims"] if c["id"] == claim_id), None)
    if not claim or claim["status"] != "pending":
        return await call.answer("❌ Заявку вже оброблено.", show_alert=True)

    claim["status"] = "approved"
    uid  = claim["uid"]
    u    = db["users"].get(uid)
    task_idx = claim["task_idx"]

    if u:
        if "bp_tasks_done" not in u:
            u["bp_tasks_done"] = []
        if task_idx not in u["bp_tasks_done"]:
            u["bp_tasks_done"].append(task_idx)
        tasks = db["bp"].get("tasks", [])
        if task_idx < len(tasks):
            xp_reward = tasks[task_idx].get("xp", 100)
            u["xp"] += xp_reward
            while u["xp"] >= XP_PER_LEVEL:
                u["xp"]   -= XP_PER_LEVEL
                u["level"] += 1

    save_db(db)
    log_action(call.from_user.id, "bp_approve", target=uid, details=f"claim={claim_id}")

    task_name = claim.get("task_name", f"#{task_idx+1}")
    await call.message.edit_text(
        call.message.text + f"\n\n✅ Зараховано: {curator['nick']}",
        reply_markup=None
    )
    try:
        await bot.send_message(int(uid),
            f"✅ <b>BP задачу зараховано!</b>\n📋 {task_name}")
    except Exception:
        pass
    await call.answer("Зараховано!")

@dp.callback_query(F.data.startswith("bp_reject_"))
async def cb_bp_reject(call: CallbackQuery):
    db      = load_db()
    curator = get_user(db, call.from_user.id)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Тільки куратори можуть відхиляти.", show_alert=True)

    claim_id = int(call.data.split("_")[-1])
    claim    = next((c for c in db["bp_task_claims"] if c["id"] == claim_id), None)
    if not claim or claim["status"] != "pending":
        return await call.answer("❌ Заявку вже оброблено.", show_alert=True)

    claim["status"] = "rejected"
    uid      = claim["uid"]
    task_name = claim.get("task_name", "")
    save_db(db)
    log_action(call.from_user.id, "bp_reject", target=uid, details=f"claim={claim_id}")

    await call.message.edit_text(
        call.message.text + f"\n\n❌ Відхилено: {curator['nick']}",
        reply_markup=None
    )
    try:
        await bot.send_message(int(uid),
            f"❌ <b>BP заявку відхилено.</b>\n📋 {task_name}")
    except Exception:
        pass
    await call.answer("Відхилено.")

# ═══════════════════════════════════════════════
#  /ADMIN
# ═══════════════════════════════════════════════
@dp.message(Command("admin"))
async def cmd_admin(message: Message):
    db = load_db()
    if not _is_head_mod(db, message.from_user.id) and message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Доступ заборонено.")
    await message.answer("⚙️ <b>Панель адміністратора</b>", reply_markup=admin_markup())

# ═══════════════════════════════════════════════
#  /DAILY — ЩОДЕННИЙ БОНУС
# ═══════════════════════════════════════════════
@dp.message(Command("daily"))
async def cmd_daily(message: Message):
    db   = load_db()
    u    = get_user(db, message.from_user.id, message.from_user)
    today = datetime.now().strftime("%Y-%m-%d")

    if u["daily_last"] == today:
        return await message.answer("⏳ Щоденний бонус вже отримано. Повертайся завтра!")

    u["daily_last"]   = today
    u["points"]      += DAILY_BONUS
    u["xp"]          += DAILY_BONUS * 2
    touch_active(db, message.from_user.id)
    save_db(db)
    await message.answer(
        f"🎁 <b>Щоденний бонус!</b>\n\n"
        f"💰 +{DAILY_BONUS} балів\n"
        f"⭐ +{DAILY_BONUS * 2} XP\n\n"
        f"Всього балів: <b>{u['points']}</b>"
    )

# ═══════════════════════════════════════════════
#  /TOP — РЕЙТИНГ
# ═══════════════════════════════════════════════
@dp.message(Command("top"))
async def cmd_top(message: Message):
    db      = load_db()
    users   = [(uid, d) for uid, d in db["users"].items()]
    users.sort(key=lambda x: x[1].get("points", 0), reverse=True)
    lines   = ["🏆 <b>ТОП-10 UA ONLINE</b>\n"]
    medals  = ["🥇", "🥈", "🥉"]
    for i, (uid, d) in enumerate(users[:10]):
        medal = medals[i] if i < 3 else f"{i+1}."
        lines.append(f"{medal} <b>{d['nick']}</b> — {d.get('points',0)} балів | {d['role']}")
    await message.answer("\n".join(lines))


# ═══════════════════════════════════════════════
#  ДОДАНІ ТЕКСТОВІ КОМАНДИ (ВИПРАВЛЕННЯ)
# ═══════════════════════════════════════════════

@dp.message(Command("promote"))
async def cmd_promote(message: Message, command: CommandObject):
    db = load_db()
    if message.from_user.id != ADMIN_ID and ROLES.get(get_user(db, message.from_user.id)["role"], 0) < ROLES["Головний модератор"]: return
    if not command.args: return await message.answer("❌ Вкажи користувача і роль: /promote @user Назва Ролі")
    args = command.args.split(maxsplit=1)
    uid, target = await _find_target(db, args[0])
    if not target: return await message.answer("❌ Користувача не знайдено.")
    if len(args) < 2: return await message.answer("❌ Вкажи роль.")
    target["role"] = args[1]
    save_db(db)
    await message.answer(f"✅ Роль <b>{target['nick']}</b> змінена на <b>{args[1]}</b>")

@dp.message(Command("demote"))
async def cmd_demote(message: Message, command: CommandObject):
    db = load_db()
    if message.from_user.id != ADMIN_ID and ROLES.get(get_user(db, message.from_user.id)["role"], 0) < ROLES["Головний модератор"]: return
    if not command.args: return await message.answer("❌ Вкажи користувача: /demote @user")
    uid, target = await _find_target(db, command.args.split()[0])
    if not target: return await message.answer("❌ Користувача не знайдено.")
    target["role"] = "Молодший Модератор"
    save_db(db)
    await message.answer(f"⬇️ Роль <b>{target['nick']}</b> понижено до Молодший Модератор.")

@dp.message(Command("reprimand"))
async def cmd_reprimand(message: Message, command: CommandObject):
    db = load_db()
    caller = get_user(db, message.from_user.id)
    if ROLES.get(caller["role"], 0) < ROLES["Головний модератор"] and message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Тільки Головний модератор може видавати догани.")

    if not command.args:
        return await message.answer("❌ Формат: /reprimand @user причина")

    parts = command.args.split(maxsplit=1)
    uid, target = await _find_target(db, parts[0])
    if not target:
        return await message.answer("❌ Користувача не знайдено.")

    reason = parts[1] if len(parts) > 1 else "Не вказано"

    if "reprimands_history" not in target:
        target["reprimands_history"] = []

    target["reprimands"] = target.get("reprimands", 0) + 1
    target["reprimands_history"].append({
        "by":      str(message.from_user.id),
        "by_nick": caller["nick"],
        "reason":  reason,
        "ts":      datetime.now().strftime("%d.%m.%Y %H:%M"),
    })

    save_db(db)
    log_action(message.from_user.id, "reprimand", target=uid, details=reason)

    await message.answer(
        f"🔴 <b>{target['nick']}</b> отримує догану.\n"
        f"📝 Причина: {reason}\n"
        f"Всього доган: <b>{target['reprimands']}</b>"
    )
    try:
        await bot.send_message(
            int(uid),
            f"🔴 Ти отримав догану від <b>{caller['nick']}</b>\n"
            f"📝 Причина: {reason}\n"
            f"Всього доган: <b>{target['reprimands']}</b>"
        )
    except Exception:
        pass

@dp.message(Command("vacation"))
async def cmd_vacation(message: Message, command: CommandObject):
    db = load_db()
    u = get_user(db, message.from_user.id)
    if not command.args: return await message.answer("❌ Вкажи дати: /vacation з ДД.ММ по ДД.ММ")
    u["vacation"] = command.args
    save_db(db)
    await message.answer(f"🏖 Відпустку встановлено: <b>{command.args}</b>")

@dp.message(Command("inactive"))
async def cmd_inactive(message: Message):
    db = load_db()
    if message.from_user.id != ADMIN_ID and ROLES.get(get_user(db, message.from_user.id)["role"], 0) < ROLES["Головний модератор"]: return
    res = "😴 <b>Неактивні (>7 днів):</b>\n\n"
    found = False
    now = datetime.now()
    for uid, u in db["users"].items():
        last = u.get("last_active")
        if last:
            delta = now - datetime.fromisoformat(last)
            if delta.days >= INACTIVITY_DAYS and not u.get("vacation"):
                res += f"• {u['nick']} ({u['tag']}) — {delta.days} дн.\n"
                found = True
    await message.answer(res if found else "✅ Всі активні!")

@dp.message(Command("reset_inactive"))
async def cmd_reset_inactive(message: Message, command: CommandObject):
    db = load_db()
    if message.from_user.id != ADMIN_ID and ROLES.get(get_user(db, message.from_user.id)["role"], 0) < ROLES["Головний модератор"]: return
    if not command.args: return await message.answer("❌ Вкажи користувача: /reset_inactive @user")
    uid, target = await _find_target(db, command.args.split()[0])
    if target:
        target["last_active"] = datetime.now().isoformat()
        save_db(db)
        await message.answer(f"✅ Активність <b>{target['nick']}</b> скинуто до поточної.")

@dp.message(Command("backup"))
async def cmd_backup_db(message: Message):
    db = load_db()
    if message.from_user.id != ADMIN_ID and ROLES.get(get_user(db, message.from_user.id)["role"], 0) < ROLES["Головний модератор"]: return
    backup_db()
    await message.answer("💾 Резервну копію бази даних (backup) створено.")

@dp.message(Command("setpoints"))
async def cmd_setpoints(message: Message, command: CommandObject):
    db = load_db()
    if message.from_user.id != ADMIN_ID and ROLES.get(get_user(db, message.from_user.id)["role"], 0) < ROLES["Головний модератор"]: return
    if not command.args: return await message.answer("❌ Формат: /setpoints @user 100")
    args = command.args.split()
    if len(args) < 2 or not args[1].lstrip("-").isdigit(): return await message.answer("❌ Вкажи правильну кількість балів (число).")
    uid, target = await _find_target(db, args[0])
    if not target: return await message.answer("❌ Користувача не знайдено.")
    amount = int(args[1])
    target["points"] += amount
    save_db(db)
    await message.answer(f"💰 Користувачу <b>{target['nick']}</b> додано {amount} балів. (Всього: {target['points']})")

@dp.message(Command("setnorm"))
async def cmd_setnorm(message: Message, command: CommandObject):
    db = load_db()
    if message.from_user.id != ADMIN_ID and ROLES.get(get_user(db, message.from_user.id)["role"], 0) < ROLES["Головний модератор"]: return
    if not command.args or not command.args.isdigit(): return await message.answer("❌ Формат: /setnorm 50")
    db["norms"]["weekly"] = int(command.args)
    save_db(db)
    await message.answer(f"✅ Щотижневу норму встановлено на <b>{command.args}</b>")

@dp.message(Command("bp_tasks"))
async def cmd_bp_tasks_list(message: Message):
    db = load_db()
    tasks = db["bp"].get("tasks", [])
    if not tasks: return await message.answer("❌ Завдань BP поки немає.")
    res = "🏆 <b>Завдання Battle Pass:</b>\n\n"
    for i, t in enumerate(tasks):
        res += f"{i+1}. {t.get('name', 'Задача')} ({t.get('xp', 100)} XP)\n"
    await message.answer(res)

@dp.message(Command("bp_rewards"))
async def cmd_bp_rewards_list(message: Message):
    db = load_db()
    rewards = db["bp"].get("rewards", {})
    if not rewards: return await message.answer("❌ Нагород BP поки немає.")
    res = "🎁 <b>Нагороди Battle Pass:</b>\n\n"
    for lvl, rew in rewards.items():
        res += f"Рівень {lvl}: {rew}\n"
    await message.answer(res)

@dp.message(Command("bp_create"))
async def cmd_bp_create_cmd(message: Message, state: FSMContext):
    db = load_db()
    if message.from_user.id != ADMIN_ID and ROLES.get(get_user(db, message.from_user.id)["role"], 0) < ROLES["Головний модератор"]: return
    await state.set_state(Form.bp_create_name)
    await message.answer("📝 Введіть назву нового завдання для BP:", reply_markup=cancel_kb())

@dp.message(Form.bp_create_name)
async def fsm_bp_create_name_step(message: Message, state: FSMContext):
    db = load_db()
    if "tasks" not in db["bp"]: db["bp"]["tasks"] = []
    db["bp"]["tasks"].append({"name": message.text, "xp": 100})
    save_db(db)
    await state.clear()
    await message.answer(f"✅ Завдання «{message.text}» успішно додано до BP.")

@dp.message(Command("editprofile"))
async def cmd_editprofile_cmd(message: Message):
    await message.answer("⚙️ <b>Редагування профілю:</b>\nОберіть, що бажаєте змінити:", reply_markup=edit_menu_markup())

@dp.message(Command("achievements"))
async def cmd_achievements_cmd(message: Message):
    db = load_db()
    u = get_user(db, message.from_user.id)
    ach = []
    if u["level"] >= 5: ach.append("🌟 5 Рівень")
    if u["level"] >= 10: ach.append("🔥 10 Рівень")
    if u["points"] >= 500: ach.append("💰 Багатій (500+ балів)")
    if not ach: ach.append("Поки що немає досягнень. Активнічай більше!")
    res = "🏅 <b>Ваші досягнення:</b>\n\n" + "\n".join([f"• {a}" for a in ach])
    await message.answer(res)

@dp.message(Command("punish"))
async def cmd_punish_cmd(message: Message, command: CommandObject):
    if not command.args: return await message.answer("❌ Формат: /punish @user. Далі бот запропонує обрати покарання через меню.")
    db = load_db()
    uid, target = await _find_target(db, command.args.split()[0])
    if not target: return await message.answer("❌ Користувача не знайдено.")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Mute", callback_data=f"punish_mute_{uid}"), InlineKeyboardButton(text="Warn", callback_data=f"punish_warn_{uid}")]
    ])
    await message.answer(f"⚖️ Оберіть покарання для <b>{target['nick']}</b>:", reply_markup=kb)

@dp.message(Command("bugreport"))
async def cmd_bugreport_cmd(message: Message, state: FSMContext):
    await state.set_state(Form.bug_desc)
    await message.answer("🐞 Опишіть знайдений баг детально:", reply_markup=cancel_kb())

@dp.message(Form.bug_desc)
async def fsm_bug_desc_step(message: Message, state: FSMContext):
    await bot.send_message(ADMIN_ID, f"‼️ <b>БАГ-РЕПОРТ</b> від @{message.from_user.username}:\n\n{message.text}")
    await state.clear()
    await message.answer("✅ Баг-репорт надіслано розробникам/адміністрації. Дякуємо!")

# ═══════════════════════════════════════════════
#  АДМІН ПАНЕЛЬ — CALLBACK ОБРОБНИКИ
# ═══════════════════════════════════════════════

@dp.callback_query(F.data == "admin_claims")
async def cb_admin_claims(call: CallbackQuery):
    db = load_db()
    if not _is_head_mod(db, call.from_user.id) and call.from_user.id != ADMIN_ID:
        return await call.answer("❌ Доступ заборонено.", show_alert=True)

    claims = [c for c in db.get("claims", []) if c.get("status") == "pending"]
    if not claims:
        await call.message.edit_text("📋 <b>Заявки на бали</b>\n\nПендінг-заявок немає.", reply_markup=_back_to_admin_kb())
        return await call.answer()

    for c in claims[:10]:
        uid = c.get("uid")
        u   = db["users"].get(uid, {})
        kb  = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Схвалити", callback_data=f"claim_approve_{c['id']}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"claim_reject_{c['id']}"),
        ]])
        text = (
            f"📋 <b>Заявка на бали #{c['id']}</b>\n\n"
            f"👤 {u.get('nick','?')} ({u.get('tag','?')})\n"
            f"🎖 {u.get('role','?')}\n"
            f"📌 Дія: <b>{c.get('action','?')}</b> × {c.get('count','?')}\n"
            f"📝 {c.get('proof','—')}\n"
            f"🕐 {c.get('ts','?')}"
        )
        await call.message.answer(text, reply_markup=kb)

    await call.answer()


@dp.callback_query(F.data == "admin_punish")
async def cb_admin_punish(call: CallbackQuery):
    db = load_db()
    if not _is_head_mod(db, call.from_user.id) and call.from_user.id != ADMIN_ID:
        return await call.answer("❌ Доступ заборонено.", show_alert=True)

    claims = [c for c in db.get("punish_claims", []) if c.get("status") == "pending"]
    if not claims:
        await call.message.edit_text("⚖️ <b>Пропозиції покарань</b>\n\nПендінг-заявок немає.", reply_markup=_back_to_admin_kb())
        return await call.answer()

    for c in claims[:10]:
        uid = c.get("uid")
        u   = db["users"].get(uid, {})
        tgt = db["users"].get(c.get("target_uid", ""), {})
        kb  = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Схвалити", callback_data=f"punish_approve_{c['id']}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"punish_reject_{c['id']}"),
        ]])
        text = (
            f"⚖️ <b>Пропозиція покарання #{c['id']}</b>\n\n"
            f"👤 <b>Подав:</b> {u.get('nick','?')} ({u.get('tag','?')})\n"
            f"🎯 <b>Ціль:</b> {tgt.get('nick','?')} ({tgt.get('tag','?')})\n"
            f"🔨 <b>Дія:</b> {c.get('action','?')}\n"
            f"📝 <b>Причина:</b> {c.get('reason','—')}\n"
            f"🕐 {c.get('ts','?')}"
        )
        await call.message.answer(text, reply_markup=kb)

    await call.answer()


@dp.callback_query(F.data == "admin_promos")
async def cb_admin_promos(call: CallbackQuery):
    db = load_db()
    if not _is_head_mod(db, call.from_user.id) and call.from_user.id != ADMIN_ID:
        return await call.answer("❌ Доступ заборонено.", show_alert=True)

    promos = [p for p in db.get("promotions", []) if p.get("status") == "pending"]
    if not promos:
        await call.message.edit_text("📈 <b>Заявки на підвищення</b>\n\nПендінг-заявок немає.", reply_markup=_back_to_admin_kb())
        return await call.answer()

    for p in promos[:10]:
        uid = p.get("uid")
        u   = db["users"].get(uid, {})
        kb  = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Схвалити", callback_data=f"promo_approve_{p['id']}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"promo_reject_{p['id']}"),
        ]])
        text = (
            f"📈 <b>Заявка на підвищення #{p['id']}</b>\n\n"
            f"👤 {u.get('nick','?')} ({u.get('tag','?')})\n"
            f"🎖 Поточна роль: {u.get('role','?')}\n"
            f"📝 Причина: {p.get('reason','—')}\n"
            f"🕐 {p.get('ts','?')}"
        )
        await call.message.answer(text, reply_markup=kb)

    await call.answer()


@dp.callback_query(F.data == "admin_vacations")
async def cb_admin_vacations(call: CallbackQuery):
    db = load_db()
    if not _is_head_mod(db, call.from_user.id) and call.from_user.id != ADMIN_ID:
        return await call.answer("❌ Доступ заборонено.", show_alert=True)

    vacs = [v for v in db.get("vacations", []) if v.get("status") == "pending"]
    if not vacs:
        await call.message.edit_text("🏖 <b>Відпустки</b>\n\nПендінг-заявок немає.", reply_markup=_back_to_admin_kb())
        return await call.answer()

    for v in vacs[:10]:
        uid = v.get("uid")
        u   = db["users"].get(uid, {})
        kb  = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Схвалити", callback_data=f"vac_approve_{v['id']}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"vac_reject_{v['id']}"),
        ]])
        text = (
            f"🏖 <b>Відпустка #{v['id']}</b>\n\n"
            f"👤 {u.get('nick','?')} ({u.get('tag','?')})\n"
            f"📅 Дати: {v.get('dates','—')}\n"
            f"📝 Причина: {v.get('reason','—')}\n"
            f"🕐 {v.get('ts','?')}"
        )
        await call.message.answer(text, reply_markup=kb)

    await call.answer()


@dp.callback_query(F.data == "admin_bp_claims")
async def cb_admin_bp_claims(call: CallbackQuery):
    db = load_db()
    if not _is_head_mod(db, call.from_user.id) and call.from_user.id != ADMIN_ID:
        return await call.answer("❌ Доступ заборонено.", show_alert=True)

    claims = [c for c in db.get("bp_task_claims", []) if c.get("status") == "pending"]
    if not claims:
        await call.message.edit_text("🏆 <b>BP Заявки</b>\n\nПендінг-заявок немає.", reply_markup=_back_to_admin_kb())
        return await call.answer()

    for c in claims[:10]:
        uid = c.get("uid")
        u   = db["users"].get(uid, {})
        kb  = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Зарахувати", callback_data=f"bp_approve_{c['id']}"),
            InlineKeyboardButton(text="❌ Відхилити",  callback_data=f"bp_reject_{c['id']}"),
        ]])
        caption = (
            f"🏆 <b>BP Заявка #{c['id']}</b>\n\n"
            f"👤 {u.get('nick','?')} ({u.get('tag','?')})\n"
            f"📋 Задача: {c.get('task_name','?')}\n"
            f"📝 Доказ: {c.get('proof','—')}\n"
            f"🕐 {c.get('ts','?')}"
        )
        photo_id = c.get("photo_id")
        if photo_id:
            await call.message.answer_photo(photo_id, caption=caption, reply_markup=kb)
        else:
            await call.message.answer(caption, reply_markup=kb)

    await call.answer()


@dp.callback_query(F.data == "admin_bugs")
async def cb_admin_bugs(call: CallbackQuery):
    db = load_db()
    if not _is_head_mod(db, call.from_user.id) and call.from_user.id != ADMIN_ID:
        return await call.answer("❌ Доступ заборонено.", show_alert=True)

    reports = [r for r in db.get("bug_reports", []) if r.get("status") == "pending"]
    if not reports:
        await call.message.edit_text("🐛 <b>Репорти багів</b>\n\nНових репортів немає.", reply_markup=_back_to_admin_kb())
        return await call.answer()

    for r in reports[:10]:
        uid = r.get("uid")
        u   = db["users"].get(uid, {})
        kb  = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="✅ Прийнято",  callback_data=f"bug_accept_{r['id']}"),
            InlineKeyboardButton(text="❌ Відхилити", callback_data=f"bug_decline_{r['id']}"),
        ]])
        text = (
            f"🐛 <b>Репорт #{r['id']}</b>\n\n"
            f"👤 {u.get('nick','?')} ({u.get('tag','?')})\n"
            f"📝 {r.get('desc','—')}\n"
            f"🕐 {r.get('ts','?')}"
        )
        photo_id = r.get("photo_id")
        if photo_id:
            await call.message.answer_photo(photo_id, caption=text, reply_markup=kb)
        else:
            await call.message.answer(text, reply_markup=kb)

    await call.answer()


@dp.callback_query(F.data == "admin_inactive")
async def cb_admin_inactive(call: CallbackQuery):
    db = load_db()
    if not _is_head_mod(db, call.from_user.id) and call.from_user.id != ADMIN_ID:
        return await call.answer("❌ Доступ заборонено.", show_alert=True)

    now   = datetime.now()
    lines = ["😴 <b>Неактивні (&gt;7 днів):</b>\n"]
    found = False
    for uid, u in db["users"].items():
        last = u.get("last_active")
        if last:
            delta = now - datetime.fromisoformat(last)
            if delta.days >= INACTIVITY_DAYS and not u.get("vacation"):
                lines.append(f"• <b>{u['nick']}</b> ({u['tag']}) — {delta.days} дн.")
                found = True

    text = "\n".join(lines) if found else "✅ Всі активні!"
    await call.message.edit_text(text, reply_markup=_back_to_admin_kb())
    await call.answer()


@dp.callback_query(F.data == "admin_log")
async def cb_admin_log(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        db = load_db()
        if not _is_head_mod(db, call.from_user.id):
            return await call.answer("❌ Доступ заборонено.", show_alert=True)

    if not os.path.exists(LOG_FILE):
        await call.message.edit_text("📜 <b>Журнал порожній.</b>", reply_markup=_back_to_admin_kb())
        return await call.answer()

    with open(LOG_FILE, "r", encoding="utf-8") as f:
        logs = json.load(f)

    last = logs[-20:]
    lines = ["📜 <b>Останні 20 дій:</b>\n"]
    for entry in reversed(last):
        ts     = entry.get("ts", "?")[:16].replace("T", " ")
        actor  = entry.get("actor", "?")
        action = entry.get("action", "?")
        target = entry.get("target", "")
        detail = entry.get("details", "")
        line   = f"• [{ts}] <code>{actor}</code> → <b>{action}</b>"
        if target:
            line += f" → {target}"
        if detail:
            line += f" ({detail})"
        lines.append(line)

    await call.message.edit_text("\n".join(lines), reply_markup=_back_to_admin_kb())
    await call.answer()


@dp.callback_query(F.data == "admin_backup")
async def cb_admin_backup(call: CallbackQuery):
    db = load_db()
    if not _is_head_mod(db, call.from_user.id) and call.from_user.id != ADMIN_ID:
        return await call.answer("❌ Доступ заборонено.", show_alert=True)
    backup_db()
    await call.answer("💾 Backup створено!", show_alert=True)


@dp.callback_query(F.data == "admin_norms")
async def cb_admin_norms(call: CallbackQuery):
    db = load_db()
    if not _is_head_mod(db, call.from_user.id) and call.from_user.id != ADMIN_ID:
        return await call.answer("❌ Доступ заборонено.", show_alert=True)

    norms = db.get("norms", {})
    text  = (
        f"⚙️ <b>Норми активності</b>\n\n"
        f"📅 Щотижнева: <b>{norms.get('weekly', 50)}</b> балів\n"
        f"📆 Щомісячна: <b>{norms.get('monthly', 200)}</b> балів\n\n"
        f"Щоб змінити, використовуй:\n"
        f"/setnorm 50 — тижнева\n"
        f"/setnorm_monthly 200 — місячна"
    )
    await call.message.edit_text(text, reply_markup=_back_to_admin_kb())
    await call.answer()


# ═══════════════════════════════════════════════
#  СХВАЛЕННЯ / ВІДХИЛЕННЯ ЗАЯВОК НА БАЛИ
# ═══════════════════════════════════════════════

@dp.callback_query(F.data == "claim_start")
async def cb_claim_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.claim_proof)
    await state.update_data(fsm_user_id=call.from_user.id)
    await call.message.answer(
        "📥 <b>Заявка на нарахування балів</b>\n\n"
        "Напиши що ти зробив і прикріпи скріншот як доказ.\n\n"
        "Надішли <b>фото з підписом</b> або просто <b>текст</b>.",
        reply_markup=cancel_kb()
    )
    await call.answer()


@dp.message(Form.claim_proof)
async def fsm_claim_proof(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("fsm_user_id") != message.from_user.id:
        return

    photo_id   = None
    proof_text = ""

    if message.photo:
        photo_id   = message.photo[-1].file_id
        proof_text = message.caption or ""
        if not proof_text:
            await message.answer(
                "📝 Додай підпис до фото — що саме ти зробив?\n"
                "Надішли фото <b>з підписом</b>.",
                reply_markup=cancel_kb()
            )
            return
    elif message.text:
        proof_text = message.text
    else:
        return await message.answer(
            "❌ Надішли фото з підписом або текстовий опис.",
            reply_markup=cancel_kb()
        )

    db       = load_db()
    u        = get_user(db, message.from_user.id, message.from_user)
    claim_id = len(db["claims"]) + 1

    db["claims"].append({
        "id":       claim_id,
        "uid":      str(message.from_user.id),
        "action":   "manual",
        "count":    1,
        "proof":    proof_text,
        "photo_id": photo_id,
        "status":   "pending",
        "ts":       datetime.now().isoformat(),
    })
    save_db(db)
    await state.clear()

    caption = (
        f"📥 <b>Заявка на бали #{claim_id}</b>\n\n"
        f"{_claim_header(u)}"
        f"📝 <b>Опис:</b> {proof_text}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Нарахувати", callback_data=f"claim_approve_{claim_id}"),
        InlineKeyboardButton(text="❌ Відхилити",  callback_data=f"claim_reject_{claim_id}"),
    ]])
    await _notify_curators_with_photo(db, photo_id, caption, kb)
    log_action(message.from_user.id, "claim_submit", details=f"claim={claim_id}")

    await message.answer(
        f"✅ <b>Заявку #{claim_id} надіслано!</b>\n\n"
        f"📝 {proof_text[:100]}{'...' if len(proof_text) > 100 else ''}\n\n"
        f"Очікуй рішення куратора."
    )

@dp.callback_query(F.data.startswith("claim_approve_"))
async def cb_claim_approve(call: CallbackQuery, state: FSMContext):
    db       = load_db()
    curator  = get_user(db, call.from_user.id)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Тільки куратори можуть схвалювати.", show_alert=True)

    claim_id = int(call.data.split("_")[-1])
    claim    = next((c for c in db["claims"] if c["id"] == claim_id), None)
    if not claim or claim["status"] != "pending":
        return await call.answer("❌ Заявку вже оброблено.", show_alert=True)

    await state.set_state(Form.claim_count)
    await state.update_data(
        approve_claim_id=claim_id,
        fsm_user_id=call.from_user.id
    )
    await call.message.answer(
        f"💰 Заявка #{claim_id}\n\nВведи кількість балів для нарахування:",
        reply_markup=cancel_kb()
    )
    await call.answer()


@dp.message(Form.claim_count)
async def fsm_claim_count(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("fsm_user_id") != message.from_user.id:
        return
    if not message.text or not message.text.strip().isdigit():
        return await message.answer("❌ Введи ціле число.", reply_markup=cancel_kb())

    points   = int(message.text.strip())
    claim_id = data.get("approve_claim_id")

    db      = load_db()
    curator = get_user(db, message.from_user.id)
    claim   = next((c for c in db["claims"] if c["id"] == claim_id), None)

    if not claim or claim["status"] != "pending":
        await state.clear()
        return await message.answer("❌ Заявку вже оброблено.")

    claim["status"]         = "approved"
    claim["points_granted"] = points
    uid = claim["uid"]
    u   = db["users"].get(uid)

    if u:
        u["points"]         = u.get("points", 0) + points
        u["xp"]             = u.get("xp", 0) + points * 2
        u["weekly_points"]  = u.get("weekly_points", 0) + points
        u["monthly_points"] = u.get("monthly_points", 0) + points
        while u["xp"] >= XP_PER_LEVEL:
            u["xp"]   -= XP_PER_LEVEL
            u["level"] += 1

    save_db(db)
    await state.clear()
    log_action(message.from_user.id, "claim_approve", target=uid, details=f"id={claim_id}, pts={points}")

    await message.answer(f"✅ Нараховано <b>{points}</b> балів по заявці #{claim_id}.")
    try:
        await bot.send_message(
            int(uid),
            f"✅ Твою заявку #{claim_id} схвалено!\n"
            f"💰 Нараховано: <b>{points}</b> балів\n"
            f"📝 {claim.get('proof', '')[:100]}"
        )
    except Exception:
        pass


@dp.callback_query(F.data.startswith("claim_reject_"))
async def cb_claim_reject(call: CallbackQuery):
    db       = load_db()
    curator  = get_user(db, call.from_user.id)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Тільки куратори можуть відхиляти.", show_alert=True)

    claim_id = int(call.data.split("_")[-1])
    claim    = next((c for c in db["claims"] if c["id"] == claim_id), None)
    if not claim or claim["status"] != "pending":
        return await call.answer("❌ Заявку вже оброблено.", show_alert=True)

    claim["status"] = "rejected"
    uid = claim["uid"]
    save_db(db)
    log_action(call.from_user.id, "claim_reject", target=uid, details=str(claim_id))

    try:
        await call.message.edit_text(call.message.text + f"\n\n❌ Відхилено: {curator['nick']}", reply_markup=None)
    except Exception:
        pass
    try:
        await bot.send_message(int(uid), f"❌ Твою заявку #{claim_id} на бали відхилено.")
    except Exception:
        pass
    await call.answer("Відхилено.")

# ═══════════════════════════════════════════════
#  СХВАЛЕННЯ / ВІДХИЛЕННЯ ПРОПОЗИЦІЙ ПОКАРАНЬ
# ═══════════════════════════════════════════════

@dp.callback_query(F.data.startswith("punish_approve_"))
async def cb_punish_approve(call: CallbackQuery):
    db       = load_db()
    curator  = get_user(db, call.from_user.id)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Тільки куратори.", show_alert=True)

    claim_id = int(call.data.split("_")[-1])
    claim    = next((c for c in db.get("punish_claims", []) if c["id"] == claim_id), None)
    if not claim or claim["status"] != "pending":
        return await call.answer("❌ Заявку вже оброблено.", show_alert=True)

    claim["status"] = "approved"
    uid = claim["uid"]
    u   = db["users"].get(uid)
    if u:
        add_points(u, claim.get("action", "warn"))

    save_db(db)
    log_action(call.from_user.id, "punish_approve", target=uid, details=str(claim_id))

    try:
        await call.message.edit_text(call.message.text + f"\n\n✅ Схвалено: {curator['nick']}", reply_markup=None)
    except Exception:
        pass
    try:
        await bot.send_message(int(uid), f"✅ Твою пропозицію покарання схвалено!")
    except Exception:
        pass
    await call.answer("Схвалено!")


@dp.callback_query(F.data.startswith("punish_reject_"))
async def cb_punish_reject(call: CallbackQuery):
    db       = load_db()
    curator  = get_user(db, call.from_user.id)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Тільки куратори.", show_alert=True)

    claim_id = int(call.data.split("_")[-1])
    claim    = next((c for c in db.get("punish_claims", []) if c["id"] == claim_id), None)
    if not claim or claim["status"] != "pending":
        return await call.answer("❌ Заявку вже оброблено.", show_alert=True)

    claim["status"] = "rejected"
    uid = claim["uid"]
    save_db(db)
    log_action(call.from_user.id, "punish_reject", target=uid, details=str(claim_id))

    try:
        await call.message.edit_text(call.message.text + f"\n\n❌ Відхилено: {curator['nick']}", reply_markup=None)
    except Exception:
        pass
    try:
        await bot.send_message(int(uid), f"❌ Твою пропозицію покарання відхилено.")
    except Exception:
        pass
    await call.answer("Відхилено.")


# ═══════════════════════════════════════════════
#  СХВАЛЕННЯ / ВІДХИЛЕННЯ ПІДВИЩЕНЬ
# ═══════════════════════════════════════════════

@dp.callback_query(F.data.startswith("promo_approve_"))
async def cb_promo_approve(call: CallbackQuery):
    db       = load_db()
    curator  = get_user(db, call.from_user.id)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Тільки куратори.", show_alert=True)

    promo_id = int(call.data.split("_")[-1])
    promo    = next((p for p in db.get("promotions", []) if p["id"] == promo_id), None)
    if not promo or promo["status"] != "pending":
        return await call.answer("❌ Заявку вже оброблено.", show_alert=True)

    promo["status"] = "approved"
    uid = promo["uid"]
    u   = db["users"].get(uid)

    # Підвищуємо на наступний рівень
    if u:
        current_level = ROLES.get(u["role"], 0)
        next_role     = next((r for r, lvl in ROLES.items() if lvl == current_level + 1), None)
        if next_role:
            u["saved_role"] = u["role"]
            u["role"]       = next_role
            promo["new_role"] = next_role

    save_db(db)
    log_action(call.from_user.id, "promo_approve", target=uid, details=str(promo_id))

    try:
        await call.message.edit_text(call.message.text + f"\n\n✅ Схвалено: {curator['nick']}", reply_markup=None)
    except Exception:
        pass
    try:
        new_role = promo.get("new_role", "вищу роль")
        await bot.send_message(int(uid), f"🎉 Твою заявку на підвищення схвалено!\n🎖 Нова роль: <b>{new_role}</b>")
    except Exception:
        pass
    await call.answer("Схвалено!")


@dp.callback_query(F.data.startswith("promo_reject_"))
async def cb_promo_reject(call: CallbackQuery):
    db       = load_db()
    curator  = get_user(db, call.from_user.id)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Тільки куратори.", show_alert=True)

    promo_id = int(call.data.split("_")[-1])
    promo    = next((p for p in db.get("promotions", []) if p["id"] == promo_id), None)
    if not promo or promo["status"] != "pending":
        return await call.answer("❌ Заявку вже оброблено.", show_alert=True)

    promo["status"] = "rejected"
    uid = promo["uid"]
    save_db(db)
    log_action(call.from_user.id, "promo_reject", target=uid, details=str(promo_id))

    try:
        await call.message.edit_text(call.message.text + f"\n\n❌ Відхилено: {curator['nick']}", reply_markup=None)
    except Exception:
        pass
    try:
        await bot.send_message(int(uid), f"❌ Твою заявку на підвищення відхилено.")
    except Exception:
        pass
    await call.answer("Відхилено.")


# ═══════════════════════════════════════════════
#  СХВАЛЕННЯ / ВІДХИЛЕННЯ ВІДПУСТОК
# ═══════════════════════════════════════════════

@dp.callback_query(F.data.startswith("vac_approve_"))
async def cb_vac_approve(call: CallbackQuery):
    db       = load_db()
    curator  = get_user(db, call.from_user.id)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Тільки куратори.", show_alert=True)

    vac_id = int(call.data.split("_")[-1])
    vac    = next((v for v in db.get("vacations", []) if v["id"] == vac_id), None)
    if not vac or vac["status"] != "pending":
        return await call.answer("❌ Заявку вже оброблено.", show_alert=True)

    vac["status"] = "approved"
    uid = vac["uid"]
    u   = db["users"].get(uid)
    if u:
        u["vacation"] = vac.get("dates", "—")

    save_db(db)
    log_action(call.from_user.id, "vac_approve", target=uid, details=str(vac_id))

    try:
        await call.message.edit_text(call.message.text + f"\n\n✅ Схвалено: {curator['nick']}", reply_markup=None)
    except Exception:
        pass
    try:
        await bot.send_message(int(uid), f"🏖 Відпустку схвалено!\n📅 {vac.get('dates','—')}")
    except Exception:
        pass
    await call.answer("Схвалено!")


@dp.callback_query(F.data.startswith("vac_reject_"))
async def cb_vac_reject(call: CallbackQuery):
    db       = load_db()
    curator  = get_user(db, call.from_user.id)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Тільки куратори.", show_alert=True)

    vac_id = int(call.data.split("_")[-1])
    vac    = next((v for v in db.get("vacations", []) if v["id"] == vac_id), None)
    if not vac or vac["status"] != "pending":
        return await call.answer("❌ Заявку вже оброблено.", show_alert=True)

    vac["status"] = "rejected"
    uid = vac["uid"]
    save_db(db)
    log_action(call.from_user.id, "vac_reject", target=uid, details=str(vac_id))

    try:
        await call.message.edit_text(call.message.text + f"\n\n❌ Відхилено: {curator['nick']}", reply_markup=None)
    except Exception:
        pass
    try:
        await bot.send_message(int(uid), f"❌ Заявку на відпустку відхилено.")
    except Exception:
        pass
    await call.answer("Відхилено.")


# ═══════════════════════════════════════════════
#  БАГ-РЕПОРТ: ПРИЙНЯТИ / ВІДХИЛИТИ
# ═══════════════════════════════════════════════

@dp.callback_query(F.data.startswith("bug_accept_"))
async def cb_bug_accept(call: CallbackQuery):
    db      = load_db()
    curator = get_user(db, call.from_user.id)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Тільки куратори.", show_alert=True)

    report_id = int(call.data.split("_")[-1])
    report    = next((r for r in db.get("bug_reports", []) if r["id"] == report_id), None)
    if not report or report["status"] != "pending":
        return await call.answer("❌ Вже оброблено.", show_alert=True)

    report["status"] = "accepted"
    uid = report["uid"]
    save_db(db)
    log_action(call.from_user.id, "bug_accept", target=uid, details=str(report_id))

    try:
        await call.message.edit_text(call.message.text + f"\n\n✅ Прийнято: {curator['nick']}", reply_markup=None)
    except Exception:
        pass
    try:
        await bot.send_message(int(uid), f"✅ Твій баг-репорт #{report_id} прийнято. Дякуємо!")
    except Exception:
        pass
    await call.answer("Прийнято!")


@dp.callback_query(F.data.startswith("bug_decline_"))
async def cb_bug_decline(call: CallbackQuery):
    db      = load_db()
    curator = get_user(db, call.from_user.id)
    if ROLES.get(curator["role"], 0) < ROLES["Куратор модерації"]:
        return await call.answer("❌ Тільки куратори.", show_alert=True)

    report_id = int(call.data.split("_")[-1])
    report    = next((r for r in db.get("bug_reports", []) if r["id"] == report_id), None)
    if not report or report["status"] != "pending":
        return await call.answer("❌ Вже оброблено.", show_alert=True)

    report["status"] = "declined"
    uid = report["uid"]
    save_db(db)

    try:
        await call.message.edit_text(call.message.text + f"\n\n❌ Відхилено: {curator['nick']}", reply_markup=None)
    except Exception:
        pass
    try:
        await bot.send_message(int(uid), f"❌ Баг-репорт #{report_id} відхилено.")
    except Exception:
        pass
    await call.answer("Відхилено.")


# ═══════════════════════════════════════════════
#  FSM: ЗАЯВКА НА БАЛИ (claim_start)
# ═══════════════════════════════════════════════

@dp.callback_query(F.data == "claim_start")
async def cb_claim_start(call: CallbackQuery, state: FSMContext):
    actions = list(POINTS_TABLE.keys())
    kb_rows = [[InlineKeyboardButton(
        text=f"{a} (+{POINTS_TABLE[a]['points']} б.)",
        callback_data=f"claim_action_{a}"
    )] for a in actions]
    kb_rows.append([InlineKeyboardButton(text="❌ Скасувати", callback_data="fsm_cancel")])
    await state.set_state(Form.claim_action)
    await state.update_data(fsm_user_id=call.from_user.id)
    await call.message.answer("📥 <b>Заявка на бали</b>\n\nОберіть тип дії:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows))
    await call.answer()


@dp.callback_query(F.data.startswith("claim_action_"))
async def cb_claim_action(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("fsm_user_id") != call.from_user.id:
        return await call.answer("❌ Не твоя форма.", show_alert=True)

    action = call.data.split("claim_action_")[1]
    await state.update_data(action=action)
    await state.set_state(Form.claim_count)
    await call.message.edit_text(
        f"✅ Обрано: <b>{action}</b>\n\n📊 Введи кількість (число):",
        reply_markup=cancel_kb()
    )
    await call.answer()


@dp.message(Form.claim_count)
async def fsm_claim_count(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("fsm_user_id") != message.from_user.id:
        return
    if not message.text or not message.text.strip().isdigit():
        return await message.answer("❌ Введи ціле число.", reply_markup=cancel_kb())

    await state.update_data(count=int(message.text.strip()))
    await state.set_state(Form.claim_proof)
    await message.answer("📸 Надішли доказ (фото або текст):", reply_markup=cancel_kb())


@dp.message(Form.claim_proof)
async def fsm_claim_proof(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("fsm_user_id") != message.from_user.id:
        return

    db     = load_db()
    u      = get_user(db, message.from_user.id, message.from_user)
    action = data.get("action")
    count  = data.get("count", 1)

    photo_id   = None
    proof_text = ""
    if message.photo:
        photo_id   = message.photo[-1].file_id
        proof_text = message.caption or "📸 Фото"
    elif message.text:
        proof_text = message.text
    else:
        return await message.answer("❌ Надішли фото або текст.", reply_markup=cancel_kb())

    claim_id = len(db["claims"]) + 1
    db["claims"].append({
        "id":      claim_id,
        "uid":     str(message.from_user.id),
        "action":  action,
        "count":   count,
        "proof":   proof_text,
        "photo_id": photo_id,
        "status":  "pending",
        "ts":      datetime.now().isoformat(),
    })
    save_db(db)
    await state.clear()

    pts = POINTS_TABLE.get(action, {}).get("points", 0) * count
    caption = (
        f"📥 <b>Заявка на бали #{claim_id}</b>\n\n"
        f"{_claim_header(u)}\n"
        f"📌 Дія: <b>{action}</b> × {count}\n"
        f"💰 Очікувані бали: <b>{pts}</b>\n"
        f"📝 {proof_text}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Схвалити", callback_data=f"claim_approve_{claim_id}"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data=f"claim_reject_{claim_id}"),
    ]])
    await _notify_curators_with_photo(db, photo_id, caption, kb)
    log_action(message.from_user.id, "claim_submit", details=f"action={action}, count={count}")

    await message.answer(
        f"✅ <b>Заявку #{claim_id} надіслано!</b>\n\n"
        f"📌 Дія: <b>{action}</b> × {count}\n"
        f"💰 Очікується: <b>{pts}</b> балів\n"
        f"Очікуй рішення куратора."
    )


# ═══════════════════════════════════════════════
#  FSM: ЗАЯВКА НА ПІДВИЩЕННЯ (promo_start)
# ═══════════════════════════════════════════════

@dp.callback_query(F.data == "promo_start")
async def cb_promo_start(call: CallbackQuery, state: FSMContext):
    await state.set_state(Form.promo_reason)
    await state.update_data(fsm_user_id=call.from_user.id)
    await call.message.answer(
        "📈 <b>Заявка на підвищення</b>\n\nЧому ти заслуговуєш підвищення?\nОпиши причину:",
        reply_markup=cancel_kb()
    )
    await call.answer()


@dp.message(Form.promo_reason)
async def fsm_promo_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("fsm_user_id") != message.from_user.id:
        return

    db   = load_db()
    u    = get_user(db, message.from_user.id, message.from_user)
    promo_id = len(db.get("promotions", [])) + 1

    if "promotions" not in db:
        db["promotions"] = []

    db["promotions"].append({
        "id":     promo_id,
        "uid":    str(message.from_user.id),
        "reason": message.text,
        "status": "pending",
        "ts":     datetime.now().isoformat(),
    })
    save_db(db)
    await state.clear()

    caption = (
        f"📈 <b>Заявка на підвищення #{promo_id}</b>\n\n"
        f"{_claim_header(u)}\n"
        f"📝 Причина: {message.text}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Схвалити", callback_data=f"promo_approve_{promo_id}"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data=f"promo_reject_{promo_id}"),
    ]])
    await _notify_curators(db, caption, kb)
    log_action(message.from_user.id, "promo_submit", details=str(promo_id))

    await message.answer(f"✅ <b>Заявку #{promo_id} на підвищення надіслано!</b>\nОчікуй рішення куратора.")


# ═══════════════════════════════════════════════
#  FSM: ВІДПУСТКА (vacation_menu)
# ═══════════════════════════════════════════════

@dp.callback_query(F.data == "vacation_menu")
async def cb_vacation_menu(call: CallbackQuery, state: FSMContext):
    db = load_db()
    u  = get_user(db, call.from_user.id)

    if u.get("vacation"):
        kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="🏠 Повернутися з відпустки", callback_data="vacation_end"),
        ]])
        await call.message.answer(
            f"🏖 Ти зараз у відпустці: <b>{u['vacation']}</b>",
            reply_markup=kb
        )
    else:
        await state.set_state(Form.vacation_dates)
        await state.update_data(fsm_user_id=call.from_user.id)
        await call.message.answer(
            "🏖 <b>Заявка на відпустку</b>\n\nВведи дати відпустки (напр. 01.06 - 10.06):",
            reply_markup=cancel_kb()
        )
    await call.answer()


@dp.callback_query(F.data == "vacation_end")
async def cb_vacation_end(call: CallbackQuery):
    db = load_db()
    u  = get_user(db, call.from_user.id)
    u["vacation"] = None
    save_db(db)
    await call.message.edit_text("🏠 Ти повернувся з відпустки. Ласкаво просимо назад!")
    await call.answer()


@dp.message(Form.vacation_dates)
async def fsm_vacation_dates(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("fsm_user_id") != message.from_user.id:
        return
    await state.update_data(dates=message.text)
    await state.set_state(Form.vacation_reason)
    await message.answer("📝 Вкажи причину відпустки:", reply_markup=cancel_kb())


@dp.message(Form.vacation_reason)
async def fsm_vacation_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("fsm_user_id") != message.from_user.id:
        return

    db     = load_db()
    u      = get_user(db, message.from_user.id, message.from_user)
    dates  = data.get("dates", "—")
    vac_id = len(db.get("vacations", [])) + 1

    if "vacations" not in db:
        db["vacations"] = []

    db["vacations"].append({
        "id":     vac_id,
        "uid":    str(message.from_user.id),
        "dates":  dates,
        "reason": message.text,
        "status": "pending",
        "ts":     datetime.now().isoformat(),
    })
    save_db(db)
    await state.clear()

    caption = (
        f"🏖 <b>Заявка на відпустку #{vac_id}</b>\n\n"
        f"{_claim_header(u)}\n"
        f"📅 Дати: {dates}\n"
        f"📝 Причина: {message.text}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Схвалити", callback_data=f"vac_approve_{vac_id}"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data=f"vac_reject_{vac_id}"),
    ]])
    await _notify_curators(db, caption, kb)
    log_action(message.from_user.id, "vacation_submit", details=str(vac_id))

    await message.answer(f"✅ <b>Заявку #{vac_id} на відпустку надіслано!</b>\nОчікуй схвалення куратора.")


# ═══════════════════════════════════════════════
#  FSM: ПРОПОЗИЦІЯ ПОКАРАННЯ (punish_start)
# ═══════════════════════════════════════════════

@dp.callback_query(F.data.startswith("punish_start_"))
async def cb_punish_start(call: CallbackQuery, state: FSMContext):
    target_uid = call.data.split("punish_start_")[1]
    db = load_db()
    target = db["users"].get(target_uid)
    if not target:
        return await call.answer("❌ Користувача не знайдено.", show_alert=True)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⚠️ Warn",  callback_data=f"paction_{target_uid}_warn"),
         InlineKeyboardButton(text="🔇 Mute",  callback_data=f"paction_{target_uid}_mute")],
        [InlineKeyboardButton(text="🚫 Ban",   callback_data=f"paction_{target_uid}_ban"),
         InlineKeyboardButton(text="👟 Kick",  callback_data=f"paction_{target_uid}_kick")],
        [InlineKeyboardButton(text="❌ Скасувати", callback_data="fsm_cancel")],
    ])
    await state.set_state(Form.punish_action)
    await state.update_data(target_uid=target_uid, fsm_user_id=call.from_user.id)
    await call.message.answer(
        f"⚖️ <b>Пропозиція покарання для {target['nick']}</b>\n\nОберіть тип покарання:",
        reply_markup=kb
    )
    await call.answer()


@dp.callback_query(F.data.startswith("paction_"))
async def cb_punish_action(call: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if data.get("fsm_user_id") != call.from_user.id:
        return await call.answer("❌ Не твоя форма.", show_alert=True)

    parts      = call.data.split("_")
    target_uid = parts[1]
    action     = parts[2]

    await state.update_data(target_uid=target_uid, action=action)
    await state.set_state(Form.punish_reason)
    await call.message.edit_text(
        f"📝 Вкажи причину покарання (<b>{action}</b>):",
        reply_markup=cancel_kb()
    )
    await call.answer()


@dp.message(Form.punish_reason)
async def fsm_punish_reason(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("fsm_user_id") != message.from_user.id:
        return
    await state.update_data(reason=message.text)
    await state.set_state(Form.punish_proof)
    await message.answer("📸 Надішли доказ (фото або текст):", reply_markup=cancel_kb())


@dp.message(Form.punish_proof)
async def fsm_punish_proof(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("fsm_user_id") != message.from_user.id:
        return

    db         = load_db()
    u          = get_user(db, message.from_user.id, message.from_user)
    target_uid = data.get("target_uid")
    action     = data.get("action", "warn")
    reason     = data.get("reason", "—")
    target     = db["users"].get(target_uid, {})

    photo_id   = None
    proof_text = ""
    if message.photo:
        photo_id   = message.photo[-1].file_id
        proof_text = message.caption or "📸 Фото"
    elif message.text:
        proof_text = message.text
    else:
        return await message.answer("❌ Надішли фото або текст.", reply_markup=cancel_kb())

    claim_id = len(db.get("punish_claims", [])) + 1
    if "punish_claims" not in db:
        db["punish_claims"] = []

    db["punish_claims"].append({
        "id":         claim_id,
        "uid":        str(message.from_user.id),
        "target_uid": target_uid,
        "action":     action,
        "reason":     reason,
        "proof":      proof_text,
        "photo_id":   photo_id,
        "status":     "pending",
        "ts":         datetime.now().isoformat(),
    })
    save_db(db)
    await state.clear()

    caption = (
        f"⚖️ <b>Пропозиція покарання #{claim_id}</b>\n\n"
        f"{_claim_header(u)}\n"
        f"🎯 <b>Ціль:</b> {target.get('nick','?')} ({target.get('tag','?')})\n"
        f"🔨 <b>Дія:</b> {action}\n"
        f"📝 <b>Причина:</b> {reason}\n"
        f"📸 {proof_text}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Схвалити", callback_data=f"punish_approve_{claim_id}"),
        InlineKeyboardButton(text="❌ Відхилити", callback_data=f"punish_reject_{claim_id}"),
    ]])
    await _notify_curators_with_photo(db, photo_id, caption, kb)
    log_action(message.from_user.id, "punish_submit", target=target_uid, details=f"action={action}")

    await message.answer(f"✅ <b>Пропозицію покарання #{claim_id} надіслано куратору!</b>")


# ═══════════════════════════════════════════════
#  ПРОФІЛЬ: РЕДАГУВАННЯ (edit_menu)
# ═══════════════════════════════════════════════

@dp.callback_query(F.data == "edit_menu")
async def cb_edit_menu(call: CallbackQuery):
    await call.message.answer("⚙️ <b>Редагування профілю:</b>", reply_markup=edit_menu_markup())
    await call.answer()


@dp.callback_query(F.data.in_({"set_n", "set_d", "set_b", "set_s", "set_a"}))
async def cb_edit_field(call: CallbackQuery, state: FSMContext):
    mapping = {
        "set_n": (Form.edit_nick,    "🏷 Введи новий нікнейм:"),
        "set_d": (Form.edit_discord, "🎮 Введи Discord (напр. user#0000):"),
        "set_b": (Form.edit_bio,     "📝 Введи нове біо:"),
        "set_s": (Form.edit_sex,     "⚧ Введи стать (Чол / Жін / Інше):"),
        "set_a": (Form.edit_age,     "🎂 Введи вік (число):"),
    }
    fsm_state, prompt = mapping[call.data]
    await state.set_state(fsm_state)
    await state.update_data(fsm_user_id=call.from_user.id)
    await call.message.answer(prompt, reply_markup=cancel_kb())
    await call.answer()


@dp.message(Form.edit_nick)
async def fsm_edit_nick(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("fsm_user_id") != message.from_user.id:
        return
    if len(message.text) > 32:
        return await message.answer("❌ Нікнейм занадто довгий (макс. 32 символи).", reply_markup=cancel_kb())
    db = load_db()
    u  = get_user(db, message.from_user.id)
    u["nick"] = message.text.strip()
    save_db(db)
    await state.clear()
    await message.answer(f"✅ Нікнейм змінено на <b>{u['nick']}</b>")


@dp.message(Form.edit_discord)
async def fsm_edit_discord(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("fsm_user_id") != message.from_user.id:
        return
    db = load_db()
    u  = get_user(db, message.from_user.id)
    u["discord"] = message.text.strip()
    save_db(db)
    await state.clear()
    await message.answer(f"✅ Discord оновлено: <code>{u['discord']}</code>")


@dp.message(Form.edit_bio)
async def fsm_edit_bio(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("fsm_user_id") != message.from_user.id:
        return
    if len(message.text) > 200:
        return await message.answer("❌ Біо занадто довге (макс. 200 символів).", reply_markup=cancel_kb())
    db = load_db()
    u  = get_user(db, message.from_user.id)
    u["bio"] = message.text.strip()
    save_db(db)
    await state.clear()
    await message.answer("✅ Біо оновлено!")


@dp.message(Form.edit_sex)
async def fsm_edit_sex(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("fsm_user_id") != message.from_user.id:
        return
    db = load_db()
    u  = get_user(db, message.from_user.id)
    u["sex"] = message.text.strip()
    save_db(db)
    await state.clear()
    await message.answer(f"✅ Стать оновлено: <b>{u['sex']}</b>")


@dp.message(Form.edit_age)
async def fsm_edit_age(message: Message, state: FSMContext):
    data = await state.get_data()
    if data.get("fsm_user_id") != message.from_user.id:
        return
    if not message.text.strip().isdigit():
        return await message.answer("❌ Введи число.", reply_markup=cancel_kb())
    age = int(message.text.strip())
    if not (1 <= age <= 120):
        return await message.answer("❌ Невалідний вік.", reply_markup=cancel_kb())
    db = load_db()
    u  = get_user(db, message.from_user.id)
    u["age"] = str(age)
    save_db(db)
    await state.clear()
    await message.answer(f"✅ Вік оновлено: <b>{age}</b>")


# ═══════════════════════════════════════════════
#  РЕПУТАЦІЯ
# ═══════════════════════════════════════════════

@dp.callback_query(F.data.startswith("rep_"))
async def cb_reputation(call: CallbackQuery):
    target_uid = call.data.split("rep_")[1]
    caller_uid = str(call.from_user.id)

    if caller_uid == target_uid:
        return await call.answer("❌ Не можна підвищити власну репутацію.", show_alert=True)

    db     = load_db()
    caller = get_user(db, call.from_user.id)
    target = db["users"].get(target_uid)
    if not target:
        return await call.answer("❌ Користувача не знайдено.", show_alert=True)

    # Перевірка: не частіше ніж раз на добу для одного юзера
    rep_key = f"rep_given_{target_uid}"
    last_rep = caller.get("shop_purchases", {}).get(rep_key)
    if last_rep:
        last_dt = datetime.fromisoformat(last_rep)
        if datetime.now() - last_dt < timedelta(hours=24):
            return await call.answer("⏳ Можна давати репутацію раз на 24 год.", show_alert=True)

    target["reputation"] = target.get("reputation", 0) + 1
    if "shop_purchases" not in caller:
        caller["shop_purchases"] = {}
    caller["shop_purchases"][rep_key] = datetime.now().isoformat()
    save_db(db)
    log_action(call.from_user.id, "reputation", target=target_uid)

    await call.answer(f"👍 +1 репутація для {target['nick']}!", show_alert=True)
    try:
        await bot.send_message(int(target_uid), f"👍 {caller['nick']} підвищив твою репутацію!")
    except Exception:
        pass


# ═══════════════════════════════════════════════
#  ДОСЯГНЕННЯ
# ═══════════════════════════════════════════════

@dp.callback_query(F.data.startswith("ach_"))
async def cb_achievements(call: CallbackQuery):
    uid = call.data.split("ach_")[1]
    db  = load_db()
    u   = db["users"].get(uid)
    if not u:
        return await call.answer("❌ Користувача не знайдено.", show_alert=True)

    ach = []
    if u.get("level", 1) >= 5:  ach.append("🌟 Рівень 5")
    if u.get("level", 1) >= 10: ach.append("🔥 Рівень 10")
    if u.get("level", 1) >= 20: ach.append("💎 Рівень 20")
    if u.get("points", 0) >= 500:  ach.append("💰 Багатій (500+ балів)")
    if u.get("points", 0) >= 2000: ach.append("🏦 Мільярдер (2000+ балів)")
    if u.get("reputation", 0) >= 10: ach.append("⭐ Популярний (10+ репутації)")
    if u.get("reprimands", 0) == 0 and u.get("level", 1) >= 3:
        ach.append("😇 Бездоганний (немає доган)")
    if not ach:
        ach.append("Поки що немає досягнень. Активнічай більше!")

    text = f"🏅 <b>Досягнення {u['nick']}</b>\n\n" + "\n".join(f"• {a}" for a in ach)
    await call.message.answer(text)
    await call.answer()


# ═══════════════════════════════════════════════
#  BP: ПЕРЕГЛЯД
# ═══════════════════════════════════════════════

@dp.callback_query(F.data == "bp_view")
async def cb_bp_view(call: CallbackQuery):
    db    = load_db()
    u     = get_user(db, call.from_user.id)
    bp    = db.get("bp", {})
    tasks = bp.get("tasks", [])

    if not tasks:
        await call.message.answer("🏆 <b>Battle Pass</b>\n\nЗавдань поки немає.")
        return await call.answer()

    done = u.get("bp_tasks_done", [])
    lines = [f"🏆 <b>Battle Pass — {bp.get('name', 'Сезон 1')}</b>\n"]

    kb_rows = []
    for i, t in enumerate(tasks):
        status = "✅" if i in done else "🔲"
        lines.append(f"{status} {t.get('name','Задача')} ({t.get('xp',100)} XP)")
        if i not in done:
            kb_rows.append([InlineKeyboardButton(
                text=f"📤 Здати: {t.get('name','Задача')}",
                callback_data=f"bp_submit_{i}"
            )])

    if not kb_rows:
        lines.append("\n🎉 Ти виконав всі завдання сезону!")

    await call.message.answer(
        "\n".join(lines),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None
    )
    await call.answer()


# ═══════════════════════════════════════════════
#  /RESTORE_ROLE — відновлення збереженої ролі
# ═══════════════════════════════════════════════

@dp.message(Command("restore_role"))
async def cmd_restore_role(message: Message, command: CommandObject):
    db = load_db()
    if not _is_head_mod(db, message.from_user.id) and message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Доступ заборонено.")
    if not command.args:
        return await message.answer("❌ Формат: /restore_role @user")

    uid, target = await _find_target(db, command.args.split()[0])
    if not target:
        return await message.answer("❌ Користувача не знайдено.")

    saved = target.get("saved_role")
    if not saved:
        return await message.answer(f"ℹ️ У <b>{target['nick']}</b> немає збереженої ролі.")

    target["role"]       = saved
    target["saved_role"] = None
    save_db(db)
    await message.answer(f"✅ Роль <b>{target['nick']}</b> відновлено до <b>{saved}</b>")


# ═══════════════════════════════════════════════
#  /STATS — статистика (окрема від профілю)
# ═══════════════════════════════════════════════

@dp.message(Command("stats"))
async def cmd_stats(message: Message, command: CommandObject):
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

    u = db["users"].get(target_id)
    if not u:
        return await message.answer("❌ Профіль не знайдено.")

    s = u.get("stats", {})
    text = (
        f"📊 <b>Статистика {u['nick']}</b>\n\n"
        f"🔇 Мути: <b>{s.get('mutes', 0)}</b>\n"
        f"🚫 Бани: <b>{s.get('bans', 0)}</b>\n"
        f"⚠️ Попередження: <b>{s.get('warns', 0)}</b>\n"
        f"👟 Кіки: <b>{s.get('kicks', 0)}</b>\n"
        f"🆘 Допомога: <b>{s.get('help', 0)}</b>\n"
        f"🎫 Тікети: <b>{s.get('tickets', 0)}</b>\n"
        f"📣 Скарги: <b>{s.get('complaints', 0)}</b>\n\n"
        f"💰 Тижневі бали: <b>{u.get('weekly_points', 0)}</b>\n"
        f"📆 Місячні бали: <b>{u.get('monthly_points', 0)}</b>"
    )
    await message.answer(text)


# ═══════════════════════════════════════════════
#  ДОПОМІЖНА КЛАВІАТУРА "НАЗАД"
# ═══════════════════════════════════════════════

def _back_to_admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="◀️ Назад", callback_data="back_admin")
    ]])


@dp.callback_query(F.data == "back_admin")
async def cb_back_admin(call: CallbackQuery):
    db = load_db()
    if not _is_head_mod(db, call.from_user.id) and call.from_user.id != ADMIN_ID:
        return await call.answer("❌ Доступ заборонено.", show_alert=True)
    await call.message.edit_text("⚙️ <b>Панель адміністратора</b>", reply_markup=admin_markup())
    await call.answer()

# ═══════════════════════════════════════════════
#  /REPRIMANDS — перегляд доган
# ═══════════════════════════════════════════════

@dp.message(Command("reprimands"))
async def cmd_reprimands(message: Message, command: CommandObject):
    db        = load_db()
    viewer_id = str(message.from_user.id)
    target_id = viewer_id

    if command.args:
        uid, target = await _find_target(db, command.args.split()[0])
        if not target:
            return await message.answer("❌ Користувача не знайдено.")
        target_id = uid

    u = db["users"].get(target_id)
    if not u:
        return await message.answer("❌ Профіль не знайдено.")

    caller = get_user(db, message.from_user.id)
    if target_id != viewer_id and ROLES.get(caller["role"], 0) < ROLES["Куратор модерації"] and message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Тільки куратори можуть переглядати чужі догани.")

    history = u.get("reprimands_history", [])
    count   = u.get("reprimands", 0)

    if not history and count == 0:
        return await message.answer(f"✅ У <b>{u['nick']}</b> немає доган.")

    lines = [f"🔴 <b>Догани {u['nick']}</b> — всього: {count}\n"]
    for i, r in enumerate(history, 1):
        lines.append(
            f"{i}. 👮 {r.get('by_nick','?')} | 📅 {r.get('ts','?')}\n"
            f"   📝 {r.get('reason','—')}"
        )

    await message.answer("\n".join(lines))


# ═══════════════════════════════════════════════
#  /REMOVEREPRIMAND — зняти догану
# ═══════════════════════════════════════════════

@dp.message(Command("removereprimand"))
async def cmd_remove_reprimand(message: Message, command: CommandObject):
    db = load_db()
    caller = get_user(db, message.from_user.id)
    if ROLES.get(caller["role"], 0) < ROLES["Головний модератор"] and message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Тільки Головний модератор може знімати догани.")

    if not command.args:
        return await message.answer("❌ Формат: /removereprimand @user")

    uid, target = await _find_target(db, command.args.split()[0])
    if not target:
        return await message.answer("❌ Користувача не знайдено.")

    if target.get("reprimands", 0) <= 0:
        return await message.answer(f"ℹ️ У <b>{target['nick']}</b> немає доган.")

    target["reprimands"] -= 1
    history = target.get("reprimands_history", [])
    if history:
        history.pop()

    save_db(db)
    log_action(message.from_user.id, "remove_reprimand", target=uid)
    await message.answer(
        f"✅ Догану знято з <b>{target['nick']}</b>.\n"
        f"Залишилось: <b>{target['reprimands']}</b>"
    )


# ═══════════════════════════════════════════════
#  [2] /REMOVEPOINTS — зняти бали
# ═══════════════════════════════════════════════

@dp.message(Command("removepoints"))
async def cmd_removepoints(message: Message, command: CommandObject):
    db = load_db()
    caller = get_user(db, message.from_user.id)
    if ROLES.get(caller["role"], 0) < ROLES["Головний модератор"] and message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Тільки Головний модератор може знімати бали.")

    if not command.args:
        return await message.answer("❌ Формат: /removepoints @user 50")

    args = command.args.split()
    if len(args) < 2 or not args[1].isdigit():
        return await message.answer("❌ Вкажи правильну кількість балів.")

    uid, target = await _find_target(db, args[0])
    if not target:
        return await message.answer("❌ Користувача не знайдено.")

    amount = int(args[1])
    target["points"] = max(0, target.get("points", 0) - amount)
    save_db(db)
    log_action(message.from_user.id, "removepoints", target=uid, details=str(amount))

    await message.answer(
        f"➖ З <b>{target['nick']}</b> знято <b>{amount}</b> балів.\n"
        f"Залишилось: <b>{target['points']}</b>"
    )
    try:
        await bot.send_message(
            int(uid),
            f"➖ З тебе знято <b>{amount}</b> балів.\n"
            f"Залишилось: <b>{target['points']}</b>"
        )
    except Exception:
        pass
# ═══════════════════════════════════════════════
#  [2] /WARN — видати попередження
# ═══════════════════════════════════════════════

@dp.message(Command("warn"))
async def cmd_warn(message: Message, command: CommandObject):
    db = load_db()
    caller = get_user(db, message.from_user.id)
    if ROLES.get(caller["role"], 0) < ROLES["Куратор модерації"] and message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Тільки куратори можуть видавати попередження.")

    if not command.args:
        return await message.answer("❌ Формат: /warn @user причина")

    parts   = command.args.split(maxsplit=1)
    uid, target = await _find_target(db, parts[0])
    if not target:
        return await message.answer("❌ Користувача не знайдено.")

    reason = parts[1] if len(parts) > 1 else "Не вказано"

    # Додаємо попередження з історією
    if "warnings_history" not in target:
        target["warnings_history"] = []

    target["warnings"] = target.get("warnings", 0) + 1
    target["warnings_history"].append({
        "by":     str(message.from_user.id),
        "by_nick": caller["nick"],
        "reason": reason,
        "ts":     datetime.now().strftime("%d.%m.%Y %H:%M"),
    })

    save_db(db)
    log_action(message.from_user.id, "warn", target=uid, details=reason)

    await message.answer(
        f"⚠️ <b>{target['nick']}</b> отримує попередження.\n"
        f"📝 Причина: {reason}\n"
        f"Всього попереджень: <b>{target['warnings']}</b>"
    )
    try:
        await bot.send_message(
            int(uid),
            f"⚠️ Ти отримав попередження від <b>{caller['nick']}</b>\n"
            f"📝 Причина: {reason}\n"
            f"Всього попереджень: <b>{target['warnings']}</b>"
        )
    except Exception:
        pass


# ═══════════════════════════════════════════════
#  /UNWARN — зняти попередження
# ═══════════════════════════════════════════════

@dp.message(Command("unwarn"))
async def cmd_unwarn(message: Message, command: CommandObject):
    db = load_db()
    caller = get_user(db, message.from_user.id)
    if ROLES.get(caller["role"], 0) < ROLES["Куратор модерації"] and message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Тільки куратори можуть знімати попередження.")

    if not command.args:
        return await message.answer("❌ Формат: /unwarn @user")

    uid, target = await _find_target(db, command.args.split()[0])
    if not target:
        return await message.answer("❌ Користувача не знайдено.")

    if target.get("warnings", 0) <= 0:
        return await message.answer(f"ℹ️ У <b>{target['nick']}</b> немає попереджень.")

    target["warnings"] -= 1
    history = target.get("warnings_history", [])
    if history:
        history.pop()

    save_db(db)
    log_action(message.from_user.id, "unwarn", target=uid)
    await message.answer(
        f"✅ Попередження знято з <b>{target['nick']}</b>.\n"
        f"Залишилось: <b>{target['warnings']}</b>"
    )


# ═══════════════════════════════════════════════
#  /WARNS — перегляд попереджень
# ═══════════════════════════════════════════════

@dp.message(Command("warns"))
async def cmd_warns(message: Message, command: CommandObject):
    db        = load_db()
    viewer_id = str(message.from_user.id)
    target_id = viewer_id

    if command.args:
        uid, target = await _find_target(db, command.args.split()[0])
        if not target:
            return await message.answer("❌ Користувача не знайдено.")
        target_id = uid
    
    u = db["users"].get(target_id)
    if not u:
        return await message.answer("❌ Профіль не знайдено.")

    # Тільки куратори можуть дивитись чужі попередження
    caller = get_user(db, message.from_user.id)
    if target_id != viewer_id and ROLES.get(caller["role"], 0) < ROLES["Куратор модерації"] and message.from_user.id != ADMIN_ID:
        return await message.answer("❌ Тільки куратори можуть переглядати чужі попередження.")

    history = u.get("warnings_history", [])
    count   = u.get("warnings", 0)

    if not history and count == 0:
        return await message.answer(f"✅ У <b>{u['nick']}</b> немає попереджень.")

    lines = [f"⚠️ <b>Попередження {u['nick']}</b> — всього: {count}\n"]
    for i, w in enumerate(history, 1):
        lines.append(
            f"{i}. 👮 {w.get('by_nick','?')} | 📅 {w.get('ts','?')}\n"
            f"   📝 {w.get('reason','—')}"
        )

    await message.answer("\n".join(lines))

# ═══════════════════════════════════════════════
#  ЗАПУСК
# ═══════════════════════════════════════════════
async def main():
    logging.info("UA ONLINE BOT v2.3 by.Kalimanov запущено")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
