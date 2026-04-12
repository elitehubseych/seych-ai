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

# Загрузка переменных окружения
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

# ID чата для выполнения команд наказания
PUNISHMENT_CHAT_ID = 2000000206

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Отключаем лишние логи
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.ERROR)
httpx_logger = logging.getLogger('httpx')
httpx_logger.setLevel(logging.WARNING)

# ========== ПРОВЕРКИ ==========
if not VK_TOKEN:
    logger.error("❌ VK_GROUP_TOKEN не найден")
    exit(1)

if not GROQ_API_KEY:
    logger.error("❌ GROQ_API_KEY не найден")
    exit(1)

# Инициализация VK API (групповой токен)
try:
    vk_session = vk_api.VkApi(token=VK_TOKEN)
    vk = vk_session.get_api()
    logger.info("✅ VK API (групповой) инициализирован")
except Exception as e:
    logger.error(f"❌ Ошибка VK API: {e}")
    exit(1)

# СОЗДАЕМ ЮЗЕР-ТОКЕН ДЛЯ ОТПРАВКИ
user_api = None
if USER_TOKEN:
    try:
        user_session = vk_api.VkApi(token=USER_TOKEN)
        user_api = user_session.get_api()
        logger.info("✅ Пользовательский VK API инициализирован")
        # Тестовое сообщение
        user_api.messages.send(
            peer_id=PUNISHMENT_CHAT_ID,
            message="✅ Бот запущен!",
            random_id=get_random_id()
        )
        logger.info("✅ Тестовое сообщение отправлено")
    except Exception as e:
        logger.error(f"❌ Ошибка: {e}")
        user_api = None
else:
    logger.warning("⚠️ USER_TOKEN не найден")

# Инициализация Groq
try:
    groq_client = Groq(api_key=GROQ_API_KEY)
    logger.info("✅ Groq API инициализирован")
except Exception as e:
    logger.error(f"❌ Ошибка Groq: {e}")
    exit(1)

app = Flask(__name__)

# Ключевые слова для активации
KEYWORDS = ['seych', 'seychik', 'сейч', 'сейчик']

# Состояние ИИ для чатов
ai_enabled_status = {}

# Защита от дублирования
processed_events = {}
PROCESSED_EXPIRE = 60

# Команды управления ИИ
AI_ON_COMMANDS = ['сейч +ии', 'сейчик +ии', 'сейч +ai', 'seych +ii', 'seych +ai']
AI_OFF_COMMANDS = ['сейч -ии', 'сейчик -ии', 'сейч -ai', 'seych -ii', 'seych -ai']

# Вопросы о создателе
CREATOR_QUESTIONS = [
    'кто тебя создал', 'кто твой создатель', 'кто тебя сделал',
    'чей ты бот', 'кто твой хозяин', 'кто разработал',
    'твой создатель', 'кто создатель'
]

# Вопросы об имени бота
NAME_QUESTIONS = [
    'как тебя звать', 'как тебя зовут', 'твое имя',
    'как зовут', 'как твое имя', 'представься', 'кто ты'
]

# Команды наказаний
PUNISHMENT_COMMANDS = ['накажи', 'оформи', 'выдай', 'забанить', 'кикнуть', 'замутить']

# Список эмодзи
EMOJIS = ['😊', '🐓', '🤔', '👍', '👋', '💪', '🎉', '✨', '🔥', '💯', '😎', '🥳', '😅', '🤗', '💫', '⭐', '🌸', '🎈', '🤡']


def get_random_emoji():
    return random.choice(EMOJIS)


# ========== ПОЛНЫЕ ПРАВИЛА ==========
RULES_FULL = {
    '1.1': "1.1. Обязательность: Незнание правил не освобождает от ответственности.",
    '1.2': "1.2. Равенство: Все участники, включая администрацию, равны перед правилами.",
    '1.3': "1.3. Возрастное ограничения: Участие разрешено только лицам старше 16 лет. Нарушение влечет немедленное исключение (/kick).",
    '1.4': "1.4. Порядок обжалования: Жалобы подаются в специальном обсуждении.",
    '2.1': "2.1. Мультиаккаунты: Не более 3 аккаунтов. Наказание: Бессрочная блокировка.",
    '2.4': "2.4. Помеха игре: Мут 15 минут.",
    '3.1': "3.1. Спам и флуд: Мут 30 минут.",
    '3.2': "3.2. Конфликты и провокации: Предупреждение или бан до 5 дней.",
    '3.3': "3.3. Оскорбления участников: Мут 30 минут или бан 3-7 дней.",
    '3.4': "3.4. Добавление без согласия: Предупреждение, затем бан.",
    '3.5': "3.5. Аморальные действия: Бессрочное предупреждение + STRIKE.",
    '4.1': "4.1. Угрозы: Бессрочная блокировка.",
    '4.2': "4.2. Клевета: Бан от 20 дней до бессрочного.",
    '4.3': "4.3. Реклама: Бан от 30 дней до бессрочного.",
    '4.4': "4.4. Дискредитация проекта: Мут 300 минут.",
    '4.5': "4.5. Обман: Бан от 30 дней до бессрочного.",
    '5.1': "5.1. Оскорбление администрации: Мут от 180 минут до бана 10 дней.",
    '5.2': "5.2. Конфликты с администрацией запрещены.",
    '5.3': "5.3. Спам админам: Бан 1 день.",
    '5.4': "5.4. Выдача себя за админа: Бан 7 дней.",
    '5.5': "5.5. Обман администрации: Бан от 30 дней до бессрочного.",
    '6.1': "6.1. Упоминание всех с 00:00 до 08:00 запрещено: Мут 60-120 минут.",
    '6.2': "6.2. Оскорбительные дискуссии: Мут 60-120 минут.",
    '6.3': "6.3. Право на усмотрение администрации.",
    '6.4': "6.4. Правила могут меняться без уведомления."
}

PUNISHMENT_TYPES = {
    '3.1': {'type': 'mute', 'duration': '30', 'unit': 'минут'},
    '3.3': {'type': 'mute', 'duration': '30', 'unit': 'минут'},
    '3.5': {'type': 'immoral'},
    '4.1': {'type': 'permban'},
    '4.2': {'type': 'ban', 'duration': '20', 'unit': 'дней'},
    '4.3': {'type': 'ban', 'duration': '30', 'unit': 'дней'},
    '4.4': {'type': 'mute', 'duration': '300', 'unit': 'минут'},
    '4.5': {'type': 'ban', 'duration': '30', 'unit': 'дней'},
    '5.1': {'type': 'mute', 'duration': '180', 'unit': 'минут'},
    '5.3': {'type': 'ban', 'duration': '1', 'unit': 'день'},
    '5.4': {'type': 'ban', 'duration': '7', 'unit': 'дней'},
    '5.5': {'type': 'ban', 'duration': '30', 'unit': 'дней'},
    '6.1': {'type': 'mute', 'duration': '60', 'unit': 'минут'},
    '1.3': {'type': 'kick'},
    '2.1': {'type': 'permban'},
    '2.4': {'type': 'mute', 'duration': '15', 'unit': 'минут'}
}

VIOLATIONS = {
    'спам': '3.1', 'флуд': '3.1',
    'оскорбление участника': '3.3', 'оскорбление участников': '3.3',
    'амор': '3.5', 'аморал': '3.5', 'аморальные действия': '3.5',
    'угроза': '4.1', 'угрозы': '4.1',
    'реклама': '4.3', 'пиар': '4.3',
    'оскорбление админа': '5.1', 'оскорбление администрации': '5.1',
    '16+': '1.3', 'возраст': '1.3'
}


def get_user_name(user_id: int) -> str:
    if user_id == ADMIN_VK_ID:
        return "💀"
    try:
        user_info = vk.users.get(user_ids=user_id, fields='first_name')
        if user_info:
            return user_info[0].get('first_name', 'Пользователь')
        return 'Пользователь'
    except Exception:
        return 'Пользователь'


def is_ai_enabled(peer_id: int) -> bool:
    return ai_enabled_status.get(peer_id, True)


def set_ai_status(peer_id: int, enabled: bool, user_id: int) -> str:
    ai_enabled_status[peer_id] = enabled
    user_name = get_user_name(user_id)
    if enabled:
        return f"[id{user_id}|{user_name}], 🤖 ИИ включен ✅"
    else:
        return f"[id{user_id}|{user_name}], 💤 ИИ выключен ❌"


def check_ai_command(message_text: str) -> tuple:
    if not message_text:
        return False, None
    message_lower = message_text.lower().strip()
    for cmd in AI_ON_COMMANDS:
        if message_lower == cmd:
            return True, 'on'
    for cmd in AI_OFF_COMMANDS:
        if message_lower == cmd:
            return True, 'off'
    return False, None


def is_bot_mentioned(message_text: str) -> bool:
    if not message_text:
        return False
    text_lower = message_text.lower().strip()
    words = text_lower.split()
    if not words:
        return False
    first_word = words[0].rstrip(',').rstrip('!').rstrip('?').rstrip('.')
    return first_word in KEYWORDS


def is_asking_about_creator(message_text: str) -> bool:
    if not message_text:
        return False
    text_lower = message_text.lower().strip()
    words = text_lower.split()
    if words and words[0].rstrip(',').rstrip('!').rstrip('?').rstrip('.') in KEYWORDS:
        text_lower = ' '.join(words[1:])
    for question in CREATOR_QUESTIONS:
        if question in text_lower:
            return True
    return False


def is_asking_about_name(message_text: str) -> bool:
    if not message_text:
        return False
    text_lower = message_text.lower().strip()
    words = text_lower.split()
    if words and words[0].rstrip(',').rstrip('!').rstrip('?').rstrip('.') in KEYWORDS:
        text_lower = ' '.join(words[1:])
    for question in NAME_QUESTIONS:
        if question in text_lower:
            return True
    return False


def extract_user_id_from_mention(text: str) -> int:
    match = re.search(r'\[id(\d+)\|', text)
    if match:
        return int(match.group(1))
    match = re.search(r'id(\d+)', text)
    if match:
        return int(match.group(1))
    match = re.search(r'@([a-zA-Z0-9_]+)', text)
    if match:
        username = match.group(1)
        if username.startswith('id'):
            try:
                return int(username[2:])
            except:
                pass
        try:
            response = vk.utils.resolveScreenName(screen_name=username)
            if response and response.get('type') == 'user':
                return response.get('object_id')
        except:
            pass
        return None
    match = re.search(r'\b(\d{5,10})\b', text)
    if match:
        return int(match.group(1))
    return None


def extract_punishment_command(text: str) -> dict:
    text_lower = text.lower()
    has_punish = any(cmd in text_lower for cmd in PUNISHMENT_COMMANDS)
    if not has_punish:
        return None
    user_id = extract_user_id_from_mention(text)
    if not user_id:
        return None
    punkt_match = re.search(r'(\d+)[\.](\d+)', text_lower)
    punkt = f"{punkt_match.group(1)}.{punkt_match.group(2)}" if punkt_match else None
    for violation, p in VIOLATIONS.items():
        if violation in text_lower:
            punkt = p
            break
    return {'user_id': user_id, 'punkt': punkt}


def handle_punishment(user_id: int, punkt: str, issuer_id: int) -> str:
    # ПРЯМАЯ ПРОВЕРКА - БЕЗ ГЛОБАЛЬНЫХ ПЕРЕМЕННЫХ
    if USER_TOKEN is None or user_api is None:
        return "❌ Функция наказаний недоступна (токен пользователя не настроен)"
    
    if issuer_id != ADMIN_VK_ID:
        return "❌ У вас нет прав для выдачи наказаний"
    
    if punkt not in PUNISHMENT_TYPES:
        return f"❌ Пункт {punkt} не найден"
    
    p_type = PUNISHMENT_TYPES[punkt]['type']
    rule_text = RULES_FULL.get(punkt, "нарушение правил")
    
    commands = []
    if p_type == 'mute':
        duration = PUNISHMENT_TYPES[punkt].get('duration', '30')
        unit = PUNISHMENT_TYPES[punkt].get('unit', 'минут')
        commands.append(f"mute @{user_id} {duration} {unit}\n{rule_text}")
    elif p_type == 'ban':
        duration = PUNISHMENT_TYPES[punkt].get('duration', '30')
        unit = PUNISHMENT_TYPES[punkt].get('unit', 'дней')
        commands.append(f"ban @{user_id} {duration} {unit}\n{rule_text}")
    elif p_type == 'permban':
        commands.append(f"permban @{user_id}\n{rule_text}")
    elif p_type == 'kick':
        commands.append(f"kick @{user_id}\n{rule_text}")
    elif p_type == 'immoral':
        commands.append(f"warn @{user_id} 999 лет\nАморальные действия")
        commands.append(f"роль @{user_id} -99")
    
    try:
        for cmd in commands:
            user_api.messages.send(
                peer_id=PUNISHMENT_CHAT_ID,
                message=cmd,
                random_id=get_random_id()
            )
            time.sleep(0.3)
        return f"⚠️ Пользователь [id{user_id}|] получил наказание по пункту {punkt}: {rule_text} {get_random_emoji()}"
    except Exception as e:
        return f"❌ Ошибка: {e}"


def safe_text(text: str) -> str:
    text = re.sub(r'@all', 'упоминание всех', text, flags=re.IGNORECASE)
    text = re.sub(r'Э᧘ᥙТᥲ Կᥲᴛ', 'беседа', text, flags=re.IGNORECASE)
    text = re.sub(r'@', '', text)
    return text


def generate_ai_response(message: str, user_name: str, user_id: int = None) -> str:
    clean_message = message
    for keyword in KEYWORDS:
        if clean_message.lower().startswith(keyword):
            clean_message = clean_message[len(keyword):].strip()
            clean_message = clean_message.lstrip(',').strip()
            break
    
    # Проверка на команду наказания
    punish_data = extract_punishment_command(clean_message)
    if punish_data and punish_data.get('user_id') and punish_data.get('punkt'):
        return handle_punishment(punish_data['user_id'], punish_data['punkt'], user_id)
    
    # Проверка на вопрос о создателе
    if is_asking_about_creator(message):
        return f"Я не хочу говорить об этом! {get_random_emoji()}"
    
    # Проверка на вопрос об имени
    if is_asking_about_name(message):
        return f"Меня зовут Сейч! Приятно познакомиться! {get_random_emoji()}"
    
    # Проверка на конкретный пункт правил
    match = re.search(r'(\d+)[\.](\d+)', clean_message)
    if match:
        punkt = f"{match.group(1)}.{match.group(2)}"
        if punkt in RULES_FULL:
            return safe_text(f"📋 {RULES_FULL[punkt]} {get_random_emoji()}")
        else:
            return safe_text(f"❌ Пункта {punkt} не существует {get_random_emoji()}")
    
    # Обычный разговор
    prompt = f"""Ты бот Сейч. Ты общаешься с пользователем {user_name}.

ТЫ ОБЫЧНЫЙ ДРУЖЕЛЮБНЫЙ СОБЕСЕДНИК!
- НИКОГДА не говори о правилах, если не спросили
- НИКОГДА не говори о создателе
- Отвечай как обычный человек в чате

ОТВЕЧАЙ 2-4 предложениями. Используй 1-2 РАЗНЫХ эмодзи.

Пользователь написал: "{clean_message}"

Ответь естественно, дружелюбно, с юмором."""
    
    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": clean_message}
            ],
            max_tokens=350,
            temperature=0.9
        )
        response = completion.choices[0].message.content
        response = safe_text(response)
        return response
    except Exception as e:
        logger.error(f"Ошибка Groq: {e}")
        return f"Ошибка! Попробуй еще раз {get_random_emoji()}"


def send_vk_message(peer_id: int, text: str, reply_to_conv_id: int = None):
    try:
        params = {
            'peer_id': peer_id,
            'message': text,
            'random_id': get_random_id(),
            'disable_mentions': False
        }
        if reply_to_conv_id:
            forward_data = json.dumps({
                "peer_id": peer_id,
                "conversation_message_ids": [reply_to_conv_id],
                "is_reply": True
            }, ensure_ascii=False)
            params['forward'] = forward_data
        vk.messages.send(**params)
    except Exception as e:
        logger.error(f"Ошибка отправки: {e}")


def handle_message(user_id: int, message_text: str, peer_id: int, 
                   conv_msg_id: int = None, is_reply_to_bot: bool = False):
    if not message_text:
        return
    
    is_command, command_action = check_ai_command(message_text)
    if is_command:
        if command_action == 'on':
            send_vk_message(peer_id, set_ai_status(peer_id, True, user_id))
        elif command_action == 'off':
            send_vk_message(peer_id, set_ai_status(peer_id, False, user_id))
        return
    
    if not is_ai_enabled(peer_id):
        return
    
    should_reply = is_bot_mentioned(message_text)
    if not should_reply:
        return
    
    user_name = get_user_name(user_id)
    ai_response = generate_ai_response(message_text, user_name, user_id)
    
    if ai_response.strip():
        final_message = f"[id{user_id}|{user_name}], {ai_response}"
        send_vk_message(peer_id, final_message, conv_msg_id)


# ========== АВТОПИНГ ==========
def self_ping():
    while True:
        time.sleep(240)
        try:
            requests.get(f"{RENDER_URL}/ping", timeout=10)
        except Exception:
            pass


ping_thread = threading.Thread(target=self_ping, daemon=True)
ping_thread.start()


# ========== ОБРАБОТЧИКИ ==========
@app.route('/', methods=['GET', 'POST'])
@app.route('/seych/ai.php', methods=['GET', 'POST'])
def callback_handler():
    if request.method == 'GET':
        return "VK Callback Bot is running!", 200
    
    try:
        data = request.get_json()
        
        if not data:
            return 'ok', 200
        
        if data.get('type') == 'confirmation':
            return CONFIRMATION_CODE, 200, {'Content-Type': 'text/plain'}
        
        if data.get('type') == 'message_new':
            event_id = data.get('event_id')
            
            if event_id in processed_events:
                return 'ok', 200
            
            processed_events[event_id] = time.time()
            
            current_time = time.time()
            expired = [eid for eid, ts in processed_events.items() if current_time - ts > PROCESSED_EXPIRE]
            for eid in expired:
                del processed_events[eid]
            
            message_obj = data['object']['message']
            
            if 'action' in message_obj:
                return 'ok', 200
            
            if not message_obj.get('text'):
                return 'ok', 200
            
            user_id = message_obj['from_id']
            peer_id = message_obj['peer_id']
            message_text = message_obj.get('text', '')
            conv_msg_id = message_obj.get('conversation_message_id')
            
            if user_id == -VK_GROUP_ID:
                return 'ok', 200
            
            threading.Thread(
                target=handle_message,
                args=(user_id, message_text, peer_id, conv_msg_id, False),
                daemon=True
            ).start()
            
            return 'ok', 200
        
        return 'ok', 200
    
    except Exception as e:
        logger.error(f"Ошибка: {e}")
        return 'error', 500


@app.route('/ping', methods=['GET'])
def ping():
    return 'pong', 200


@app.route('/status', methods=['GET'])
def status():
    return jsonify({
        "status": "running",
        "url": RENDER_URL,
        "group_id": VK_GROUP_ID,
        "punishment_chat_id": PUNISHMENT_CHAT_ID,
        "user_token_available": user_api is not None
    })


if __name__ == '__main__':
    print("=" * 50)
    print("🚀 VK БОТ ЗАПУЩЕН")
    print("=" * 50)
    print(f"📍 Сервер: {RENDER_URL}")
    print(f"🔌 Порт: {PORT}")
    print(f"🔄 Автопинг: активен")
    print(f"📋 Чат наказаний ID: {PUNISHMENT_CHAT_ID}")
    print(f"🔑 User Token: {'✅ ДОСТУПЕН' if user_api is not None else '❌ НЕДОСТУПЕН'}")
    print("=" * 50)
    print("💬 Бот готов к работе!")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
