import os
import logging
import json
import time
import threading
import re
import requests

from flask import Flask, request, jsonify
import vk_api
from vk_api.utils import get_random_id
from dotenv import load_dotenv
from groq import Groq

# Загрузка переменных окружения (для локальной разработки)
load_dotenv()

# ========== КОНФИГУРАЦИЯ ==========
VK_TOKEN = os.getenv('VK_GROUP_TOKEN')
VK_GROUP_ID = int(os.getenv('VK_GROUP_ID', '0'))
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
ADMIN_VK_ID = int(os.getenv('ADMIN_VK_ID', '0'))
RENDER_URL = os.getenv('RENDER_URL', 'https://seych-ai.onrender.com')

CONFIRMATION_CODE = "eb59e42a"
PORT = int(os.getenv('PORT', 5000))

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
    logger.error("❌ VK_GROUP_TOKEN не найден в переменных окружения")
    exit(1)

if not GROQ_API_KEY:
    logger.error("❌ GROQ_API_KEY не найден в переменных окружения")
    exit(1)

# Инициализация VK API
try:
    vk_session = vk_api.VkApi(token=VK_TOKEN)
    vk = vk_session.get_api()
    logger.info("✅ VK API инициализирован")
except Exception as e:
    logger.error(f"❌ Ошибка VK API: {e}")
    exit(1)

# Инициализация Groq
try:
    groq_client = Groq(api_key=GROQ_API_KEY)
    logger.info("✅ Groq API инициализирован")
except Exception as e:
    logger.error(f"❌ Ошибка Groq: {e}")
    exit(1)

app = Flask(__name__)

# Ключевые слова для активации (включая варианты с запятой)
KEYWORDS = ['seych', 'seychik', 'сейч', 'сейчик', 'сейч,', 'сейчик,']

# Состояние ИИ для чатов
ai_enabled_status = {}

# Защита от дублирования
processed_events = {}
PROCESSED_EXPIRE = 60

# Команды управления ИИ
AI_ON_COMMANDS = ['сейч +ии', 'сейчик +ии', 'сейч +ai', 'seych +ii', 'seych +ai']
AI_OFF_COMMANDS = ['сейч -ии', 'сейчик -ии', 'сейч -ai', 'seych -ii', 'seych -ai']

# Вопросы о создателе (точное совпадение)
CREATOR_QUESTIONS = [
    'кто тебя создал',
    'кто твой создатель',
    'кто тебя сделал',
    'чей ты бот',
    'кто твой хозяин',
    'кто тебя программировал',
    'кто разработал',
    'твой создатель',
    'кто создатель'
]

# Вопросы об имени бота
NAME_QUESTIONS = [
    'как тебя звать',
    'как тебя зовут',
    'твое имя',
    'как зовут',
    'как твое имя',
    'как тебя называть',
    'представься',
    'кто ты'
]


def get_user_name(user_id: int) -> str:
    """Получает имя пользователя по VK ID"""
    if user_id == ADMIN_VK_ID:
        return "Разработчик"
    try:
        user_info = vk.users.get(user_ids=user_id, fields='first_name')
        if user_info:
            return user_info[0].get('first_name', 'Пользователь')
        return 'Пользователь'
    except Exception:
        return 'Пользователь'


def is_ai_enabled(peer_id: int) -> bool:
    """Проверяет, включен ли ИИ для чата"""
    return ai_enabled_status.get(peer_id, True)


def set_ai_status(peer_id: int, enabled: bool, user_id: int) -> str:
    """Устанавливает статус ИИ для чата"""
    ai_enabled_status[peer_id] = enabled
    user_name = get_user_name(user_id)
    if enabled:
        return f"[id{user_id}|{user_name}], 🤖 ИИ включен ✅"
    else:
        return f"[id{user_id}|{user_name}], 💤 ИИ выключен ❌"


def check_ai_command(message_text: str) -> tuple:
    """Проверяет, является ли сообщение командой управления ИИ"""
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
    """
    Проверяет, нужно ли активировать бота.
    
    Поддерживает:
    - "Сейч текст"
    - "Сейч, текст" (с запятой)
    - "Сейчик текст"
    - "Сейчик, текст"
    """
    if not message_text:
        return False
    
    text_lower = message_text.lower().strip()
    
    # Проверяем варианты:
    # 1. "сейч привет"
    # 2. "сейч, привет"
    # 3. "сейчик привет"
    # 4. "сейчик, привет"
    
    for keyword in KEYWORDS:
        # Если сообщение начинается с ключевого слова
        if text_lower.startswith(keyword):
            # Получаем остаток после ключевого слова
            remaining = text_lower[len(keyword):].strip()
            # Если есть остаток (даже пустая строка после запятой)
            if remaining:
                # Убираем запятую в начале если есть
                remaining = remaining.lstrip(',')
                remaining = remaining.strip()
                # Если после запятой есть текст - активируем
                if remaining:
                    return True
    
    # Проверка для случая "сейч," без пробела
    for keyword in KEYWORDS:
        if text_lower == keyword or text_lower == keyword.rstrip(','):
            return False
    
    return False


def is_asking_about_creator(message_text: str) -> bool:
    """Проверяет, спрашивает ли пользователь о создателе бота"""
    if not message_text:
        return False
    
    text_lower = message_text.lower().strip()
    
    # Удаляем имя бота из начала сообщения для проверки
    for keyword in KEYWORDS:
        if text_lower.startswith(keyword):
            text_lower = text_lower[len(keyword):].strip()
            text_lower = text_lower.lstrip(',').strip()
            break
    
    # Проверяем точные фразы
    for question in CREATOR_QUESTIONS:
        if question in text_lower:
            return True
    
    return False


def is_asking_about_name(message_text: str) -> bool:
    """Проверяет, спрашивает ли пользователь как зовут бота"""
    if not message_text:
        return False
    
    text_lower = message_text.lower().strip()
    
    # Удаляем имя бота из начала сообщения для проверки
    for keyword in KEYWORDS:
        if text_lower.startswith(keyword):
            text_lower = text_lower[len(keyword):].strip()
            text_lower = text_lower.lstrip(',').strip()
            break
    
    # Проверяем точные фразы
    for question in NAME_QUESTIONS:
        if question in text_lower:
            return True
    
    return False


def generate_ai_response(message: str, user_name: str) -> str:
    """Генерация ответа через Groq"""
    
    # Проверяем, спрашивают ли о создателе
    if is_asking_about_creator(message):
        return f"Меня создал [id{ADMIN_VK_ID}|Разработчик] 👨‍💻"
    
    # Проверяем, спрашивают ли как зовут
    if is_asking_about_name(message):
        return "Меня зовут Сейч! А тебя? 😊"
    
    # Для всего остального - обычный ответ
    prompt = f"""Ты — бот по имени Сейч в беседе ВКонтакте.

ИНФОРМАЦИЯ О ТЕБЕ:
- Тебя зовут Сейч (или Сейчик - это ласковое имя)
- Ты дружелюбный бот

САМОЕ ВАЖНОЕ ПРАВИЛО (ТЫ ДОЛЖЕН ЕГО ЖЕСТКО СОБЛЮДАТЬ):
- ТЫ НИКОГДА НЕ УПОМИНАЕШЬ кто тебя создал.
- ТЫ НИКОГДА НЕ ПИШЕШЬ "меня создал разработчик".
- ТЫ НИКОГДА НЕ ГОВОРИШЬ о разработчике.
- Если тебя спросят "как тебя зовут" - отвечай "Меня зовут Сейч"

ОСТАЛЬНЫЕ ПРАВИЛА:

1️⃣ На приветствия ("привет", "здарова", "утютю", "как дела") отвечай дружелюбно.

2️⃣ На вопросы о правилах беседы отвечай по пунктам.

3️⃣ Всегда используй 1-2 эмодзи в ответе.

ЗАПОМНИ: ТЕБЯ ЗОВУТ СЕЙЧ! НЕ РАЗРАБОТЧИК!
"""

    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"Пользователь {user_name} написал: {message}"}
            ],
            max_tokens=250,
            temperature=0.7
        )
        response = completion.choices[0].message.content
        
        # Финальная защита: если в ответе есть упоминание создателя
        creator_keywords = ['разработчик', 'создал', 'создатель', 'меня создал']
        has_creator_mention = any(keyword in response.lower() for keyword in creator_keywords)
        
        was_asking_about_creator = is_asking_about_creator(message)
        
        # Если упомянул создателя, но не спрашивали - заменяем ответ
        if has_creator_mention and not was_asking_about_creator:
            response = "😊 Просто общаемся! Что тебя интересует?"
        
        return response
    except Exception as e:
        logger.error(f"Ошибка Groq: {e}")
        return "Извините, ошибка 😔"


def send_vk_message(peer_id: int, text: str, reply_to_conv_id: int = None):
    """Отправляет сообщение в VK"""
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
    """Основная логика обработки сообщения"""
    if not message_text:
        return
    
    # Проверка команд ИИ
    is_command, command_action = check_ai_command(message_text)
    if is_command:
        if command_action == 'on':
            send_vk_message(peer_id, set_ai_status(peer_id, True, user_id))
        elif command_action == 'off':
            send_vk_message(peer_id, set_ai_status(peer_id, False, user_id))
        return
    
    if not is_ai_enabled(peer_id):
        return
    
    # Проверяем, нужно ли отвечать
    should_reply = False
    
    # Если это реплай на бота - отвечаем всегда
    if is_reply_to_bot:
        should_reply = True
    else:
        # Если не реплай - проверяем упоминание бота
        should_reply = is_bot_mentioned(message_text)
    
    if not should_reply:
        return
    
    user_name = get_user_name(user_id)
    ai_response = generate_ai_response(message_text, user_name)
    
    if ai_response.strip():
        final_message = f"[id{user_id}|{user_name}], {ai_response}"
        send_vk_message(peer_id, final_message, conv_msg_id)


# ========== АВТОПИНГ ==========
def self_ping():
    """Пинг самого себя каждые 4 минуты"""
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
            
            # Очистка старых событий
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
            
            # Проверка реплая на бота
            is_reply_to_bot = False
            
            if 'reply_message' in message_obj:
                reply_msg = message_obj['reply_message']
                if reply_msg and reply_msg.get('from_id') == -VK_GROUP_ID:
                    is_reply_to_bot = True
            
            if not is_reply_to_bot and 'fwd_messages' in message_obj:
                for fwd in message_obj['fwd_messages']:
                    if fwd.get('from_id') == -VK_GROUP_ID:
                        is_reply_to_bot = True
                        break
            
            # Запускаем обработку в отдельном потоке
            threading.Thread(
                target=handle_message,
                args=(user_id, message_text, peer_id, conv_msg_id, is_reply_to_bot),
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
        "group_id": VK_GROUP_ID
    })


if __name__ == '__main__':
    print("=" * 50)
    print("🚀 VK БОТ ЗАПУЩЕН")
    print("=" * 50)
    print(f"📍 Сервер: {RENDER_URL}")
    print(f"🔌 Порт: {PORT}")
    print(f"👤 Разработчик: [id{ADMIN_VK_ID}|ссылка]")
    print(f"🔄 Автопинг: активен")
    print(f"✅ Callback URL: {RENDER_URL}/")
    print("=" * 50)
    print("💬 Бот готов к работе!")
    print("=" * 50)
    print("📋 ПРАВИЛА АКТИВАЦИИ:")
    print("   ❌ 'Сейч' - молчит")
    print("   ✅ 'Сейч привет' - говорит")
    print("   ✅ 'Сейч, привет' - говорит (с запятой)")
    print("   ✅ 'Сейчик, привет!' - говорит")
    print("   ❌ 'Сейчас' - молчит")
    print("=" * 50)
    print("📋 ВОПРОСЫ К БОТУ:")
    print("   👤 'как тебя зовут?' - 'Меня зовут Сейч!'")
    print("   👨‍💻 'кто тебя создал?' - ответ о разработчике")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
