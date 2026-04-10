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

# Ключевые слова для активации
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
    'кто тебя создал', 'кто твой создатель', 'кто тебя сделал',
    'чей ты бот', 'кто твой хозяин', 'кто разработал',
    'твой создатель', 'кто создатель'
]

# Вопросы об имени бота
NAME_QUESTIONS = [
    'как тебя звать', 'как тебя зовут', 'твое имя',
    'как зовут', 'как твое имя', 'представься', 'кто ты'
]


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
    for keyword in KEYWORDS:
        if text_lower.startswith(keyword):
            remaining = text_lower[len(keyword):].strip()
            if remaining:
                remaining = remaining.lstrip(',').strip()
                if remaining:
                    return True
    return False


def is_asking_about_creator(message_text: str) -> bool:
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


def safe_text(text: str) -> str:
    """Заменяет all и @all на 'упоминание всех'"""
    # Заменяем @all на упоминание всех
    text = re.sub(r'@all', 'упоминание всех', text, flags=re.IGNORECASE)
    text = re.sub(r'all', 'упоминание всех', text, flags=re.IGNORECASE)
    text = re.sub(r'@everyone', 'упоминание всех', text, flags=re.IGNORECASE)
    # Удаляем оставшиеся @
    text = re.sub(r'@', '', text)
    return text


def find_rule_by_query(question: str) -> str:
    """Ищет правило по запросу пользователя (локально)"""
    question_lower = question.lower()
    
    # Возрастные ограничения
    if 'меньше 16' in question_lower or '16 лет' in question_lower or 'возраст' in question_lower:
        return safe_text("📋 **Пункт 1.3**: Участие разрешено только лицам старше 16 лет. Нарушение влечет немедленное исключение (/kick).")
    
    # Вопрос про all ночью
    if ('all' in question_lower or 'упоминание всех' in question_lower) and ('ночь' in question_lower or '00:00' in question_lower or '08:00' in question_lower):
        return safe_text("📋 **Пункт 6.1**: Команда упоминание всех запрещена с 00:00 до 08:00 по МСК. Нарушитель получит мут на 60-120 минут. Я не рекомендую использовать упоминание всех ночью, так как это нарушение правил! ⚠️")
    
    # Вопрос "сколько пунктов" или "перечисли все"
    if 'сколько пунктов' in question_lower or 'перечисли все' in question_lower or 'какие пункты' in question_lower:
        return safe_text("""📋 **В беседе Э᧘ᥙТᥲ Կᥲᴛ всего 25 пунктов правил:**

**[1] Общие положения:** 1.1, 1.2, 1.3, 1.4
**[2] Аккаунты:** 2.1, 2.4
**[3] Поведение и общение:** 3.1, 3.2, 3.3, 3.4, 3.5
**[4] Недопустимый контент:** 4.1, 4.2, 4.3, 4.4, 4.5
**[5] Отношения к администрации:** 5.1, 5.2, 5.3, 5.4, 5.5
**[6] Прочее:** 6.1, 6.2, 6.3, 6.4

Хочешь узнать подробнее о каком-то пункте? Напиши его номер, например "пункт 3.3" 😊""")
    
    # Поиск по конкретному пункту (цифра.цифра)
    match = re.search(r'(\d+)[\.](\d+)', question_lower)
    if match:
        section = match.group(1)
        point = match.group(2)
        key = f"{section}.{point}"
        
        rules_dict = {
            '1.1': "1.1. Обязательность: Незнание правил не освобождает от ответственности.",
            '1.2': "1.2. Равенство: Все участники, включая администрацию, равны перед правилами.",
            '1.3': "1.3. Возрастное ограничения: Участие разрешено только лицам старше 16 лет. Нарушение влечет немедленное исключение (/kick).",
            '1.4': "1.4. Порядок обжалования: Жалобы подаются в специальном обсуждении. Конфликты с администрацией запрещены.",
            '2.1': "2.1. Мультиаккаунты: Не более 3 аккаунтов. Наказание: Бессрочная блокировка всех доп. аккаунтов.",
            '2.4': "2.4. Помеха игре: Мут на 15 минут. При 5+ нарушениях в сутки — бан на 1 день.",
            '3.1': "3.1. Спам и флуд: Мут на 30 минут.",
            '3.2': "3.2. Конфликты и провокации: Предупреждение или бан до 5 дней.",
            '3.3': "3.3. Оскорбления участников: Мут на 30 минут или бан от 3 до 7 дней.",
            '3.4': "3.4. Добавление без согласия: Предупреждение, за 2+ случаев — бан от 3 до 5 дней.",
            '3.5': "3.5. Аморальные действия: Бессрочное предупреждение + STRIKE.",
            '4.1': "4.1. Угрозы и экстремизм: Бессрочная блокировка.",
            '4.2': "4.2. Дезинформация и клевета: Бан от 20 дней до бессрочного.",
            '4.3': "4.3. Реклама и пиар: Бан от 30 дней до бессрочного.",
            '4.4': "4.4. Дискредитация проекта: Мут на 300 минут.",
            '4.5': "4.5. Обман и СКАМ: Бан от 30 дней до бессрочного + STRIKE навсегда.",
            '5.1': "5.1. Оскорбление администрации: Мут от 180 минут до бана на 10 дней.",
            '5.2': "5.2. Конфликты с администрацией в общем чате запрещены.",
            '5.3': "5.3. Спам в ЛС админам: Бан на 1 день.",
            '5.4': "5.4. Выдача себя за администратора: Бан на 7 дней + черный список.",
            '5.5': "5.5. Обман администрации: Бан от 30 дней до бессрочного.",
            '6.1': "6.1. Команда упоминание всех с 00:00 до 08:00 запрещена: Мут на 60-120 минут.",
            '6.2': "6.2. Дискуссии на сложные темы с целью оскорбления: Мут на 60-120 минут.",
            '6.3': "6.3. Право на усмотрение администрации.",
            '6.4': "6.4. Изменение правил без предварительного уведомления."
        }
        
        if key in rules_dict:
            return safe_text(f"📋 **Пункт {key}**: {rules_dict[key]} ⚠️")
    
    return None


def generate_ai_response(message: str, user_name: str) -> str:
    """Генерация ответа через Groq с учетом правил"""
    
    # Проверяем, спрашивают ли о создателе
    if is_asking_about_creator(message):
        response = f"Меня создал [id{ADMIN_VK_ID}|Разработчик] 👨‍💻"
        return safe_text(response)
    
    # Проверяем, спрашивают ли как зовут
    if is_asking_about_name(message):
        response = "Меня зовут Сейч! Я бот-помощник в беседе Э᧘ᥙТᥲ Կᥲᴛ. А тебя как зовут? 😊"
        return safe_text(response)
    
    # Получаем текст без ключевого слова
    clean_message = message
    for keyword in KEYWORDS:
        if clean_message.lower().startswith(keyword):
            clean_message = clean_message[len(keyword):].strip()
            clean_message = clean_message.lstrip(',').strip()
            break
    
    # Сначала проверяем локально по правилам
    rule_answer = find_rule_by_query(clean_message)
    if rule_answer:
        return safe_text(rule_answer)
    
    prompt = f"""Ты — бот по имени Сейч в беседе Э᧘ᥙТᥲ Կᥲᴛ. Ты общаешься с пользователем {user_name}.

ВАЖНЕЙШЕЕ ПРАВИЛО:
- ЗАПРЕЩЕНО использовать слова "all", "@all" в ответах.
- ВСЕГДА заменяй их на "упоминание всех".
- Например: "команда all" → "команда упоминание всех"

ОТВЕЧАЙ КРАТКО И ПО ДЕЛУ:

1. Если спрашивают "можно ли использовать all ночью?" - ответь: "Согласно пункту 6.1 правил, команда упоминание всех запрещена с 00:00 до 08:00 по МСК. За это дают мут на 60-120 минут. Лучше не рисковать! ⚠️"

2. Если просто общаются - отвечай дружелюбно, 2-3 предложения.

Сейчас пользователь написал: "{clean_message}"
"""

    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": clean_message}
            ],
            max_tokens=300,
            temperature=0.7
        )
        response = completion.choices[0].message.content
        
        # Принудительно заменяем all на упоминание всех
        response = safe_text(response)
        
        # Защита от упоминания создателя
        if not is_asking_about_creator(message):
            creator_keywords = ['разработчик', 'создал', 'создатель', 'меня создал']
            if any(keyword in response.lower() for keyword in creator_keywords):
                response = "Ха-ха, забавно! 😄 А по правилам беседы Э᧘ᥙТᥲ Կᥲᴛ что-нибудь интересное узнать хочешь?"
        
        return response
    except Exception as e:
        logger.error(f"Ошибка Groq: {e}")
        return "Ой, что-то пошло не так! Попробуй еще раз 😊"


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
    
    should_reply = is_reply_to_bot or is_bot_mentioned(message_text)
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
    print("💬 Бот готов к работе в беседе Э᧘ᥙТᥲ Կᥲᴛ!")
    print("=" * 50)
    print("📋 ОСОБЕННОСТИ:")
    print("   ✅ Бот НЕ использует @all")
    print("   ✅ Бот НЕ использует слово 'all'")
    print("   ✅ Пишет 'упоминание всех' вместо всего")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
