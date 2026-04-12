import os
import logging
import json
import time
import threading
import re
import requests
import random

from flask import Flask, request, jsonify
import vk_api
from vk_api.utils import get_random_id
from dotenv import load_dotenv
from groq import Groq

load_dotenv()

# ========== КОНФИГУРАЦИЯ ==========
VK_TOKEN = os.getenv('VK_GROUP_TOKEN')
USER_TOKEN = os.getenv('USER_TOKEN')
VK_GROUP_ID = int(os.getenv('VK_GROUP_ID', '0'))
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
ADMIN_VK_ID = int(os.getenv('ADMIN_VK_ID', '0'))
RENDER_URL = os.getenv('RENDER_URL', 'https://seych-ai.onrender.com')
CONFIRMATION_CODE = "eb59e42a"
PORT = int(os.getenv('PORT', 5000))
PUNISHMENT_CHAT_ID = 2000000206

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# ========== ПРОВЕРКИ ==========
if not VK_TOKEN or not GROQ_API_KEY:
    logger.error("❌ Токены не найдены")
    exit(1)

# Инициализация VK API
vk = vk_api.VkApi(token=VK_TOKEN).get_api()
logger.info("✅ VK API инициализирован")

# ГЛОБАЛЬНАЯ ПЕРЕМЕННАЯ ДЛЯ ОТПРАВКИ СООБЩЕНИЙ
USER_VK = None
if USER_TOKEN:
    try:
        USER_VK = vk_api.VkApi(token=USER_TOKEN).get_api()
        USER_VK.messages.send(peer_id=PUNISHMENT_CHAT_ID, message="✅ Бот запущен", random_id=get_random_id())
        logger.info("✅ User API готов")
    except Exception as e:
        logger.error(f"❌ Ошибка User API: {e}")

# Groq
groq_client = Groq(api_key=GROQ_API_KEY)
logger.info("✅ Groq инициализирован")

app = Flask(__name__)

# ========== НАСТРОЙКИ ==========
KEYWORDS = ['сейч', 'сейчик']
ai_enabled_status = {}
processed_events = {}
PROCESSED_EXPIRE = 60
EMOJIS = ['😊', '🐓', '🤔', '👍', '👋', '💪', '🎉', '✨', '🔥', '💯', '😎', '🥳', '😅', '🤗', '💫', '⭐', '🌸', '🎈', '🤡']

# ========== ПРАВИЛА И НАКАЗАНИЯ ==========
RULES_FULL = {
    '1.3': "1.3. Участие разрешено только лицам старше 16 лет. Нарушение - исключение.",
    '3.1': "3.1. Спам и флуд: Мут 30 минут.",
    '3.3': "3.3. Оскорбления участников: Мут 30 минут или бан 3-7 дней.",
    '3.5': "3.5. Аморальные действия: Бессрочное предупреждение + STRIKE.",
    '4.1': "4.1. Угрозы: Бессрочная блокировка.",
    '4.3': "4.3. Реклама: Бан от 30 дней.",
    '5.1': "5.1. Оскорбление администрации: Мут от 180 минут до бана 10 дней.",
    '6.1': "6.1. all ночью: Мут 60-120 минут."
}

PUNISHMENTS = {
    '3.1': ('mute', '30 минут'),
    '3.3': ('mute', '30 минут'),
    '3.5': ('immoral', None),
    '4.1': ('permban', None),
    '4.3': ('ban', '30 дней'),
    '5.1': ('mute', '180 минут'),
    '6.1': ('mute', '60 минут'),
    '1.3': ('kick', None)
}

VIOLATIONS = {'спам': '3.1', 'флуд': '3.1', 'оскорбление участника': '3.3', 'оскорбление участников': '3.3',
              'амор': '3.5', 'аморал': '3.5', 'угроза': '4.1', 'реклама': '4.3', 'оскорбление админа': '5.1',
              'all': '6.1', '16+': '1.3', 'возраст': '1.3'}

def get_random_emoji():
    return random.choice(EMOJIS)

def get_user_name(user_id):
    if user_id == ADMIN_VK_ID:
        return "💀"
    try:
        return vk.users.get(user_ids=user_id)[0].get('first_name', 'Пользователь')
    except:
        return 'Пользователь'

def is_bot_mentioned(text):
    if not text:
        return False
    words = text.lower().strip().split()
    return words and words[0].rstrip(',').rstrip('!').rstrip('?') in KEYWORDS

def extract_user_id(text):
    m = re.search(r'\[id(\d+)\|', text)
    if m: return int(m.group(1))
    m = re.search(r'id(\d+)', text)
    if m: return int(m.group(1))
    m = re.search(r'@([a-zA-Z0-9_]+)', text)
    if m:
        try:
            r = vk.utils.resolveScreenName(screen_name=m.group(1))
            if r and r.get('type') == 'user':
                return r.get('object_id')
        except: pass
    m = re.search(r'\b(\d{5,10})\b', text)
    if m: return int(m.group(1))
    return None

def send_punishment(user_id, punkt, issuer_id, user_vk_obj):
    if user_vk_obj is None:
        return "❌ Токен пользователя не настроен"
    
    if issuer_id != ADMIN_VK_ID:
        return "❌ Нет прав"
    
    if punkt not in PUNISHMENTS:
        return f"❌ Пункт {punkt} не найден"
    
    ptype, duration = PUNISHMENTS[punkt]
    rule_text = RULES_FULL.get(punkt, "нарушение")
    
    try:
        if ptype == 'mute':
            user_vk_obj.messages.send(peer_id=PUNISHMENT_CHAT_ID, message=f"mute @{user_id} {duration}\n{rule_text}", random_id=get_random_id())
        elif ptype == 'ban':
            user_vk_obj.messages.send(peer_id=PUNISHMENT_CHAT_ID, message=f"ban @{user_id} {duration}\n{rule_text}", random_id=get_random_id())
        elif ptype == 'permban':
            user_vk_obj.messages.send(peer_id=PUNISHMENT_CHAT_ID, message=f"permban @{user_id}\n{rule_text}", random_id=get_random_id())
        elif ptype == 'kick':
            user_vk_obj.messages.send(peer_id=PUNISHMENT_CHAT_ID, message=f"kick @{user_id}\n{rule_text}", random_id=get_random_id())
        elif ptype == 'immoral':
            user_vk_obj.messages.send(peer_id=PUNISHMENT_CHAT_ID, message=f"warn @{user_id} 999 лет\nАморальные действия", random_id=get_random_id())
            time.sleep(0.5)
            user_vk_obj.messages.send(peer_id=PUNISHMENT_CHAT_ID, message=f"роль @{user_id} -99", random_id=get_random_id())
        
        return f"⚠️ [id{user_id}|] получил наказание по пункту {punkt}: {rule_text} {get_random_emoji()}"
    except Exception as e:
        return f"❌ Ошибка: {e}"

def safe_text(text):
    text = re.sub(r'@all', 'упоминание всех', text)
    text = re.sub(r'Э᧘ᥙТᥲ Կᥲᴛ', 'беседа', text)
    return text

def generate_response(message, user_name, user_id):
    clean = message
    for kw in KEYWORDS:
        if clean.lower().startswith(kw):
            clean = clean[len(kw):].strip().lstrip(',').strip()
            break
    
    # Команда наказания
    uid = extract_user_id(clean)
    if uid:
        punkt = None
        m = re.search(r'(\d+\.\d+)', clean)
        if m:
            punkt = m.group(1)
        else:
            for v, p in VIOLATIONS.items():
                if v in clean.lower():
                    punkt = p
                    break
        if punkt:
            return send_punishment(uid, punkt, user_id, USER_VK)
    
    # Вопрос о правиле
    m = re.search(r'(\d+\.\d+)', clean)
    if m and m.group(1) in RULES_FULL:
        return f"📋 {RULES_FULL[m.group(1)]} {get_random_emoji()}"
    
    # Обычный диалог
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "system", "content": f"Ты бот Сейч. Общайся с {user_name}. Отвечай 2-4 предложениями с юмором. Используй эмодзи. НЕ ГОВОРИ о правилах и создателе."},
                      {"role": "user", "content": clean}],
            max_tokens=300
        )
        return safe_text(resp.choices[0].message.content)
    except:
        return f"Привет! {get_random_emoji()}"

def send_message(peer_id, text, reply_id=None):
    params = {'peer_id': peer_id, 'message': text, 'random_id': get_random_id(), 'disable_mentions': False}
    if reply_id:
        params['forward'] = json.dumps({"peer_id": peer_id, "conversation_message_ids": [reply_id], "is_reply": True})
    vk.messages.send(**params)

def handle_message(user_id, text, peer_id, conv_id):
    if not text:
        return
    
    tl = text.lower().strip()
    if tl in ['сейч +ии', 'сейчик +ии', 'сейч +ai']:
        ai_enabled_status[peer_id] = True
        send_message(peer_id, f"[id{user_id}|{get_user_name(user_id)}], 🤖 ИИ включен ✅")
        return
    if tl in ['сейч -ии', 'сейчик -ии', 'сейч -ai']:
        ai_enabled_status[peer_id] = False
        send_message(peer_id, f"[id{user_id}|{get_user_name(user_id)}], 💤 ИИ выключен ❌")
        return
    
    if not ai_enabled_status.get(peer_id, True):
        return
    if not is_bot_mentioned(text):
        return
    
    resp = generate_response(text, get_user_name(user_id), user_id)
    if resp:
        send_message(peer_id, f"[id{user_id}|{get_user_name(user_id)}], {resp}", conv_id)

# ========== СЕРВЕР ==========
@app.route('/', methods=['GET', 'POST'])
@app.route('/seych/ai.php', methods=['GET', 'POST'])
def callback():
    if request.method == 'GET':
        return "Bot is running!", 200
    try:
        data = request.get_json()
        if data.get('type') == 'confirmation':
            return CONFIRMATION_CODE, 200, {'Content-Type': 'text/plain'}
        if data.get('type') == 'message_new':
            msg = data['object']['message']
            if msg.get('action') or not msg.get('text') or msg['from_id'] == -VK_GROUP_ID:
                return 'ok', 200
            event_id = data.get('event_id')
            if event_id in processed_events:
                return 'ok', 200
            processed_events[event_id] = time.time()
            for eid in list(processed_events.keys()):
                if time.time() - processed_events[eid] > PROCESSED_EXPIRE:
                    del processed_events[eid]
            threading.Thread(target=handle_message, args=(msg['from_id'], msg['text'], msg['peer_id'], msg.get('conversation_message_id')), daemon=True).start()
            return 'ok', 200
        return 'ok', 200
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return 'error', 500

@app.route('/ping')
def ping():
    return 'pong', 200

if __name__ == '__main__':
    print("=" * 50)
    print("🚀 БОТ ЗАПУЩЕН")
    print(f"📍 {RENDER_URL}")
    print(f"🔑 User Token: {'✅ ДОСТУПЕН' if USER_VK else '❌ НЕДОСТУПЕН'}")
    print("=" * 50)
    
    def ping_self():
        while True:
            time.sleep(240)
            try:
                requests.get(f"{RENDER_URL}/ping")
            except:
                pass
    threading.Thread(target=ping_self, daemon=True).start()
    
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
