import telebot

# Твій токен та налаштування
TOKEN = '8375179275:AAGcDmG1ITiVXIJ1mXy1lZmy81bbjXwe5l8'
bot = telebot.TeleBot(TOKEN)
ALLOWED_TYPES = ['jail', 'ban', 'warn', 'mute']

def parse_form(form: str):
    if '//' not in form: return None
    parts = form.split('//')
    cmd_section = parts[0].strip()
    player_nick = parts[1].strip()
    words = cmd_section.split()
    if not words or not words[0].startswith('/'): return None
    cmd_type = words[0][1:].lower()
    if cmd_type not in ALLOWED_TYPES: return None
    args = " ".join(words[1:])
    return {'type': cmd_type, 'player': player_nick, 'args': args}

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "✅ Бот 04 сервера готовий!\nФормат: `/jail 30 20 DM // Nick_Name`", parse_mode='Markdown')

@bot.message_handler(func=lambda m: True)
def handle(message):
    if '//' not in message.text: return
    data = parse_form(message.text)
    if not data:
        bot.reply_to(message, "❌ Помилка. Приклад: `/jail 60 DM // Nick_Name`", parse_mode='Markdown')
        return
    req = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    final_cmd = f"/{data['type']} {data['player']} {data['args']} (by {req})"
    bot.reply_to(message, f"✅ **Копіюй:**\n\n`{final_cmd}`", parse_mode='Markdown')

print("🚀 Бот запущений!")
bot.infinity_polling()
import telebot

# Твій токен та налаштування
TOKEN = '8375179275:AAGcDmG1ITiVXIJ1mXy1lZmy81bbjXwe5l8'
bot = telebot.TeleBot(TOKEN)
ALLOWED_TYPES = ['jail', 'ban', 'warn', 'mute']

def parse_form(form: str):
    if '//' not in form: return None
    parts = form.split('//')
    cmd_section = parts[0].strip()
    player_nick = parts[1].strip()
    words = cmd_section.split()
    if not words or not words[0].startswith('/'): return None
    cmd_type = words[0][1:].lower()
    if cmd_type not in ALLOWED_TYPES: return None
    args = " ".join(words[1:])
    return {'type': cmd_type, 'player': player_nick, 'args': args}

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "✅ Бот 04 сервера готовий!\nФормат: `/jail 30 20 DM // Nick_Name`", parse_mode='Markdown')

@bot.message_handler(func=lambda m: True)
def handle(message):
    if '//' not in message.text: return
    data = parse_form(message.text)
    if not data:
        bot.reply_to(message, "❌ Помилка. Приклад: `/jail 60 DM // Nick_Name`", parse_mode='Markdown')
        return
    req = f"@{message.from_user.username}" if message.from_user.username else message.from_user.first_name
    final_cmd = f"/{data['type']} {data['player']} {data['args']} (by {req})"
    bot.reply_to(message, f"✅ **Копіюй:**\n\n`{final_cmd}`", parse_mode='Markdown')

print("🚀 Бот запущений!")
bot.infinity_polling()

