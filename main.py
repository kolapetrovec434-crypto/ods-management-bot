import telebot
from telebot import types
import json
import os

# --- ТВОЇ ДАНІ (Беремо токен із системи для безпеки) ---
TOKEN = os.getenv('BOT_TOKEN') 
ADMIN_IDS = [1188859918, 517866646]
# -----------------

bot = telebot.TeleBot(TOKEN)
# Шлях для бази даних на Railway (якщо підключиш Volume)
DB_FILE = 'users_db.json' 
user_states = {}

def load_db():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f:
                return json.load(f)
        except: return {}
    return {}

def save_db(db):
    with open(DB_FILE, 'w') as f:
        json.dump(db, f)

users_db = load_db()

@bot.message_handler(commands=['start'])
def start(message):
    username = message.from_user.username
    if username:
        users_db[username.lower()] = message.chat.id
        save_db(users_db)
    
    if message.chat.id in ADMIN_IDS:
        markup = types.ReplyKeyboardMarkup(resize_keyboard=True)
        markup.add(types.KeyboardButton("📝 Задати завдання"))
        bot.send_message(message.chat.id, "⚡️ Доступ адміністратора підтверджено!", reply_markup=markup)
    else:
        bot.send_message(message.chat.id, "👋 Вітаю! Чекайте на завдання від керівництва.")

@bot.message_handler(func=lambda m: m.text == "📝 Задати завдання" and m.chat.id in ADMIN_IDS)
def ask_task(message):
    user_states[message.chat.id] = "WAITING_FOR_TASK"
    bot.send_message(message.chat.id, "📌 Введіть завдання.\n\nКому можна відправити:\n1. Одному: @user1 Текст\n2. Кільком: @user1 @user2 Текст\n3. Усім: @all Текст")

@bot.message_handler(func=lambda m: user_states.get(m.chat.id) == "WAITING_FOR_TASK" and m.chat.id in ADMIN_IDS)
def process_task(message):
    try:
        words = message.text.split()
        if not words: return

        targets = []
        task_text_start_idx = 0

        for i, word in enumerate(words):
            if word.startswith("@"):
                targets.append(word.replace("@", "").lower())
                task_text_start_idx = i + 1
            else:
                break
        
        task_text = " ".join(words[task_text_start_idx:])
        if not task_text:
            bot.send_message(message.chat.id, "❌ Ви не ввели текст завдання!")
            return

        markup = types.InlineKeyboardMarkup()
        markup.add(types.InlineKeyboardButton("📤 Здати звітність", callback_data="send_report"))

        success_count = 0
        fail_targets = []

        if "all" in targets:
            for username, chat_id in users_db.items():
                try:
                    bot.send_message(chat_id, f"📋 ЗАВДАННЯ ДЛЯ ВСІХ:\n\n{task_text}", reply_markup=markup)
                    success_count += 1
                except: continue
        else:
            for target in targets:
                if target in users_db:
                    try:
                        bot.send_message(users_db[target], f"📋 НОВЕ ЗАВДАННЯ:\n\n{task_text}", reply_markup=markup)
                        success_count += 1
                    except: fail_targets.append(f"@{target}")
                else:
                    fail_targets.append(f"@{target}")

        res = f"✅ Відправлено успішно: {success_count}"
        if fail_targets:
            res += f"\n❌ Не в базі/блок: {', '.join(fail_targets)}"
        
        bot.send_message(message.chat.id, res)
        user_states[message.chat.id] = None

    except Exception as e:
        bot.send_message(message.chat.id, f"⚠️ Помилка: {e}")

@bot.callback_query_handler(func=lambda call: call.data == "send_report")
def report_call(call):
    user_states[call.message.chat.id] = "WAIT_REPORT"
    bot.send_message(call.message.chat.id, "📩 Надішліть ваш звіт:")

@bot.message_handler(content_types=['text', 'photo', 'document', 'video'], func=lambda m: user_states.get(m.chat.id) == "WAIT_REPORT")
def get_report(message):
    user = f"@{message.from_user.username}" if message.from_user.username else f"ID {message.from_user.id}"
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, f"🆕 ЗВІТ ВІД {user}:")
            bot.forward_message(admin_id, message.chat.id, message.message_id)
        except: pass
    bot.send_message(message.chat.id, "✅ Ваш звіт прийнято!")
    user_states[message.chat.id] = None

if __name__ == '__main__':
    print("Бот запущений!")
    bot.infinity_polling(skip_pending=True)
