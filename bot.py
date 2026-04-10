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

# Вопросы о создателе
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
    
    for keyword in KEYWORDS:
        if text_lower.startswith(keyword):
            remaining = text_lower[len(keyword):].strip()
            if remaining:
                remaining = remaining.lstrip(',').strip()
                if remaining:
                    return True
    
    return False


def is_asking_about_creator(message_text: str) -> bool:
    """Проверяет, спрашивает ли пользователь о создателе бота"""
    if not message_text:
        return False
    
    text_lower = message_text.lower().strip()
    
    for keyword in KEYWORDS:
        if text_lower.startswith(keyword):
            text_lower = text_lower[len(keyword):].strip()
            text_lower = text_lower.lstrip(',').strip()
            break
    
    for question in CREATOR_QUESTIONS:
        if question in text_lower:
            return True
    
    return False


def is_asking_about_name(message_text: str) -> bool:
    """Проверяет, спрашивает ли пользователь как зовут бота"""
    if not message_text:
        return False
    
    text_lower = message_text.lower().strip()
    
    for keyword in KEYWORDS:
        if text_lower.startswith(keyword):
            text_lower = text_lower[len(keyword):].strip()
            text_lower = text_lower.lstrip(',').strip()
            break
    
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
        return "Меня зовут Сейч! Я дружелюбный бот-помощник. А тебя как зовут? 😊"
    
    # Получаем текст без ключевого слова для лучшего понимания
    clean_message = message
    for keyword in KEYWORDS:
        if clean_message.lower().startswith(keyword):
            clean_message = clean_message[len(keyword):].strip()
            clean_message = clean_message.lstrip(',').strip()
            break
    
    prompt = f"""Ты — бот по имени Сейч в беседе ВКонтакте. Ты общаешься с пользователем {user_name}.

ИНФОРМАЦИЯ О ТЕБЕ:
- Тебя зовут Сейч или Сейчик (ласковое имя)
- Ты дружелюбный, веселый, с чувством юмора
- Ты понимаешь шутки и можешь посмеяться вместе с пользователем
- Ты умеешь поддерживать разговор на разные темы

ВАЖНЫЕ ПРАВИЛА:
1. Отвечай РАЗВЕРНУТО, 2-4 предложения. Не отвечай одним словом!
2. Если пользователь смеется (ахахаха, лол, кек, :)), посмейся вместе с ним
3. Если пользователь пишет что-то непонятное или странное, отреагируй с юмором
4. НИКОГДА не упоминай кто тебя создал, если не спросили
5. Используй 1-2 эмодзи в ответе
6. Будь естественным, как в реальном общении

Сейчас пользователь написал: "{clean_message}"

Подумай о контексте и ответь ЕСТЕСТВЕННО, РАЗВЕРНУТО (2-4 предложения), как в реальном диалоге.
"""

    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": clean_message}
            ],
            max_tokens=250,
            temperature=0.8  # Повысил для более креативных ответов
        )
        response = completion.choices[0].message.content
        
        # Защита от слишком коротких ответов
        if len(response.split()) < 3 and len(clean_message) > 5:
            response += " А что насчет тебя? 😊"
        
        # Защита от упоминания создателя
        creator_keywords = ['разработчик', 'создал', 'создатель', 'меня создал']
        has_creator_mention = any(keyword in response.lower() for keyword in creator_keywords)
        
        if has_creator_mention and not is_asking_about_creator(message):
            response = "Ха-ха, забавно! 😄 А давай лучше поговорим о чем-нибудь интересном?"
        
        return response
    except Exception as e:
        logger.error(f"Ошибка Groq: {e}")
        return "Ой, что-то пошло не так! Попробуй еще раз 😊"


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
    
    if is_reply_to_bot:
        should_reply = True
    else:
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
    print(f"🔄 Автопинг: активен")
    print("=" * 50)
    print("💬 Бот готов к работе!")
    print("=" * 50)
    print("📋 НОВЫЕ ВОЗМОЖНОСТИ:")
    print("   ✅ Отвечает 2-4 предложениями")
    print("   ✅ Понимает шутки и смех")
    print("   ✅ Поддерживает разговор")
    print("   ✅ Не отвечает однообразно")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
