import telebot
from telebot import types

# === НАЛАШТУВАННЯ ===
TOKEN = '8375179275:AAGcDmG1ITiVXIJ1mXy1lZmy81bbjXwe5l8'  # ←←← Твій токен вже вставлений!
bot = telebot.TeleBot(TOKEN)

ALLOWED_TYPES = ['jail', 'ban', 'warn', 'mute']

def parse_form(form: str):
    form = form.strip()
    if not form.startswith('/') or '//' not in form:
        return None
    
    cmd_part, player = form.split('//', 1)
    player = player.strip()
    
    cmd_parts = cmd_part.split()
    cmd_type = cmd_parts[0][1:].lower()
    
    if cmd_type not in ALLOWED_TYPES:
        return None
    
    args = cmd_parts[1:]
    
    if args:
        reason = args[-1]
        params = ' '.join(args[:-1])
    else:
        reason = ''
        params = ''
    
    return {
        'type': cmd_type,
        'player': player,
        'params': params,
        'reason': reason
    }

@bot.message_handler(func=lambda m: True)
def handle_message(message):
    if not message.text or not message.text.startswith('/'):
        return
    
    data = parse_form(message.text)
    if not data:
        bot.reply_to(message, "❌ Невірний формат. Приклад:\n/jail 30 20 dm//K.Kalimanov\n/ban 7 dm//K.Kalimanov")
        return
    
    requester = message.from_user.username
    if not requester:
        requester = message.from_user.first_name or "невідомий"
    requester = f"@{requester}" if not requester.startswith('@') else requester
    
    reason_with_log = f"{data['reason']} (за запитом {requester})" if data['reason'] else f"(за запитом {requester})"
    
    if data['params']:
        command = f"/{data['type']} {data['player']} {data['params']} {reason_with_log}"
    else:
        command = f"/{data['type']} {data['player']} {reason_with_log}"
    
    response = f"✅ **Готово до видачі!**\n\n" \
               f"**Команда:** `{command}`\n\n" \
               f"Копіюй і встав у чат гри UA Online.\n" \
               f"Подав: {requester}"
    
    bot.reply_to(message, response, parse_mode='Markdown')

print("✅ Бот запущений! Інші адміни можуть писати форми сюди.")
bot.infinity_polling()

