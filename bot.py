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
    logger.error("❌ VK_GROUP_TOKEN не найден")
    exit(1)

if not GROQ_API_KEY:
    logger.error("❌ GROQ_API_KEY не найден")
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

# Ключевые слова для активации (ТОЛЬКО ОНИ!)
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
    'твой создатель', 'кто создатель', 'кто тебя написал'
]

# Вопросы об имени бота
NAME_QUESTIONS = [
    'как тебя звать', 'как тебя зовут', 'твое имя',
    'как зовут', 'как твое имя', 'представься', 'кто ты'
]

# Ключевые слова для вопросов о правилах
RULES_KEYWORDS = [
    'правил', 'правило', 'пункт', 'нарушение', 'наказание', 'что будет если',
    'сколько', 'перечисли', 'какие правила', 'за что', 'мут', 'бан',
    '1.', '2.', '3.', '4.', '5.', '6.', '1.1', '1.2', '1.3', '1.4',
    '2.1', '2.4', '3.1', '3.2', '3.3', '3.4', '3.5', '4.1', '4.2',
    '4.3', '4.4', '4.5', '5.1', '5.2', '5.3', '5.4', '5.5', '6.1', '6.2', '6.3', '6.4'
]

# Список эмодзи для разных ситуаций
EMOJIS = ['😊', '🐓', '🤔', '👍', '👋', '💪', '🎉', '✨', '🔥', '💯', '😎', '🥳', '😅', '🤗', '💫', '⭐', '🌸', '🎈', '🤡']


def get_random_emoji():
    return random.choice(EMOJIS)


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
    emoji = get_random_emoji()
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
    """Активация ТОЛЬКО если первое слово Сейч или Сейчик"""
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


def is_asking_about_rules(message_text: str) -> bool:
    """Проверяет, спрашивает ли пользователь о правилах"""
    if not message_text:
        return False
    
    text_lower = message_text.lower().strip()
    
    words = text_lower.split()
    if words and words[0].rstrip(',').rstrip('!').rstrip('?').rstrip('.') in KEYWORDS:
        text_lower = ' '.join(words[1:])
    
    for keyword in RULES_KEYWORDS:
        if keyword in text_lower:
            return True
    return False


def safe_text(text: str) -> str:
    """Убирает @all и название беседы"""
    text = re.sub(r'@all', 'упоминание всех', text, flags=re.IGNORECASE)
    text = re.sub(r'\ball\b', 'упоминание всех', text, flags=re.IGNORECASE)
    text = re.sub(r'@everyone', 'упоминание всех', text, flags=re.IGNORECASE)
    text = re.sub(r'Э᧘ᥙТᥲ Կᥲᴛ', 'беседа', text, flags=re.IGNORECASE)
    text = re.sub(r'Э᧘ᥙТᥲ', 'беседа', text, flags=re.IGNORECASE)
    text = re.sub(r'@', '', text)
    return text


def get_rule_by_punkt(punkt: str) -> str:
    """Возвращает текст правила по номеру пункта"""
    RULES_DICT = {
        '1.1': "Незнание правил не освобождает от ответственности.",
        '1.2': "Все участники равны перед правилами.",
        '1.3': "Участие только с 16 лет. Нарушение - исключение.",
        '1.4': "Жалобы подаются в спец. обсуждении.",
        '2.1': "Не более 3 аккаунтов. Наказание: бессрочная блокировка.",
        '2.4': "Помеха игре: мут 15 минут.",
        '3.1': "Спам и флуд: мут 30 минут.",
        '3.2': "Провокации: предупреждение или бан до 5 дней.",
        '3.3': "Оскорбления участников: мут 30 минут или бан 3-7 дней.",
        '3.4': "Добавление без согласия: предупреждение, затем бан.",
        '3.5': "Аморальные действия: бессрочное предупреждение.",
        '4.1': "Угрозы: бессрочная блокировка.",
        '4.2': "Клевета: бан от 20 дней до бессрочного.",
        '4.3': "Реклама: бан от 30 дней до бессрочного.",
        '4.4': "Дискредитация проекта: мут 300 минут.",
        '4.5': "Обман: бан от 30 дней до бессрочного.",
        '5.1': "Оскорбление администрации: мут 180 минут до бана 10 дней.",
        '5.2': "Конфликты с администрацией в чате запрещены.",
        '5.3': "Спам в ЛС админам: бан 1 день.",
        '5.4': "Выдача себя за админа: бан 7 дней.",
        '5.5': "Обман администрации: бан от 30 дней до бессрочного.",
        '6.1': "Упоминание всех с 00:00 до 08:00 запрещено: мут 60-120 минут.",
        '6.2': "Оскорбительные дискуссии: мут 60-120 минут.",
        '6.3': "Право на усмотрение администрации.",
        '6.4': "Правила могут меняться без уведомления."
    }
    return RULES_DICT.get(punkt, None)


def generate_ai_response(message: str, user_name: str) -> str:
    """Генерация ответа через Groq"""
    
    # Получаем текст без ключевого слова
    clean_message = message
    for keyword in KEYWORDS:
        if clean_message.lower().startswith(keyword):
            clean_message = clean_message[len(keyword):].strip()
            clean_message = clean_message.lstrip(',').strip()
            break
    
    # Проверяем, спрашивают ли о создателе
    if is_asking_about_creator(message):
        emoji = get_random_emoji()
        return f"Я не хочу говорить об этом, мне кажется и вам не нужно знать! {emoji}"
    
    # Проверяем, спрашивают ли как зовут
    if is_asking_about_name(message):
        emoji = get_random_emoji()
        return f"Меня зовут Сейч! Приятно познакомиться! {emoji}"
    
    # Проверяем, спрашивают ли о конкретном пункте правил
    match = re.search(r'(\d+)[\.](\d+)', clean_message)
    if match:
        punkt = f"{match.group(1)}.{match.group(2)}"
        rule = get_rule_by_punkt(punkt)
        if rule:
            emoji = get_random_emoji()
            return safe_text(f"📋 Пункт {punkt}: {rule} {emoji}")
    
    # Проверяем, спрашивают ли о правилах в целом
    if is_asking_about_rules(message):
        prompt = f"""Ты бот Сейч. Пользователь {user_name} спросил о правилах: "{clean_message}"

Кратко ответь на вопрос о правилах (3-4 предложения). Используй 1-2 разных эмодзи в конце.

Не упоминай создателя. Не упоминай название беседы."""
    else:
        # Обычный разговор - обычный собеседник!
        prompt = f"""Ты бот Сейч. Ты общаешься с пользователем {user_name}.

ТЫ ОБЫЧНЫЙ ДРУГ/СОБЕСЕДНИК!
- НИКОГДА не говори о правилах
- НИКОГДА не говори о создателе
- НИКОГДА не говори "меня создал"
- НИКОГДА не используй название беседы
- Отвечай как обычный человек в чате

ОТВЕЧАЙ 2-4 предложениями. Используй 1-2 РАЗНЫХ эмодзи (например 😊 и 😉 или 🐓 и 🔥 также можешь везде говорить). Не используй один и тот же эмодзи каждый раз!

Пользователь написал: "{clean_message}"

Ответь естественно, дружелюбно, с юмором. Используй 2 разных эмодзи."""
    
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
        
        # Убираем упоминание правил если не спрашивали
        if 'правил' in response.lower() and not is_asking_about_rules(message):
            emoji = get_random_emoji()
            response = f"Продолжаем общение! {emoji}"
        
        # Добавляем второй эмодзи если только один
        emoji_count = len(re.findall(r'[\U0001F600-\U0001F64F\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F700-\U0001F77F]', response))
        if emoji_count == 1 and not is_asking_about_rules(message):
            response += f" {get_random_emoji()}"
        
        return response
    except Exception as e:
        logger.error(f"Ошибка Groq: {e}")
        emoji = get_random_emoji()
        return f"Ой, что-то пошло не так! Попробуй еще раз {emoji}"


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
    
    # ВАЖНО: бот отвечает ТОЛЬКО если есть ключевое слово в начале
    # РЕПЛАЙ НЕ АКТИВИРУЕТ БОТА!
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
            
            # ИГНОРИРУЕМ РЕПЛАИ - проверка на реплай не активирует бота
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
    print("📋 ПРАВИЛА АКТИВАЦИИ:")
    print("   ✅ 'Сейч привет' - говорит")
    print("   ❌ Реплай на бота - МОЛЧИТ")
    print("   ❌ 'привет' без Сейч - МОЛЧИТ")
    print("=" * 50)
    print("📋 ЭМОДЗИ:")
    print("   ✅ Использует 1-2 РАЗНЫХ эмодзи")
    print("   ✅ Не повторяет одни и те же")
    print("=" * 50)
    print("📋 ЧТО НЕ ГОВОРИТ:")
    print("   ❌ Название беседы")
    print("   ❌ О создателе (если не спросили)")
    print("   ❌ О правилах (если не спросили)")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
