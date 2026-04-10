import os
import logging
import json
import time
import threading
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

CONFIRMATION_CODE = "b58f1e09"
PORT = int(os.getenv('PORT', 5000))

# Настройка логирования - ТОЛЬКО ОШИБКИ и ВАЖНОЕ
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Отключаем лишние логи от werkzeug
werkzeug_logger = logging.getLogger('werkzeug')
werkzeug_logger.setLevel(logging.ERROR)

# Отключаем httpx логи
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

# Ключевые слова
KEYWORDS = ['seych', 'seychik', 'сейч', 'сейчик']

# Состояние ИИ для чатов
ai_enabled_status = {}

# Защита от дублирования
processed_events = {}
PROCESSED_EXPIRE = 60

# Команды управления ИИ
AI_ON_COMMANDS = ['сейч +ии', 'сейчик +ии', 'сейч +ai', 'seych +ii', 'seych +ai']
AI_OFF_COMMANDS = ['сейч -ии', 'сейчик -ии', 'сейч -ai', 'seych -ii', 'seych -ai']

# ========== ПРАВИЛА ==========
RULES_TEXT = """
ПРАВИЛА БЕСЕДЫ:

[1] - Общие положения
1.1. Незнание правил не освобождает от ответственности.
1.2. Все участники, включая администрацию, равны перед правилами.
1.3. Участие разрешено только лицам старше 16 лет. Нарушение влечет немедленное исключение (/kick).
1.4. Жалобы подаются в специальном обсуждении. Конфликты с администрацией в общем чате запрещены.

[2] - Аккаунты
2.1. Не более 3 аккаунтов на пользователя. Запрещен обход наказаний. Наказание: Бессрочная блокировка (/permban) всех доп. аккаунтов и удвоение срока для основного.
2.4. Запрещено мешать игре: принудительно завершать, накидывать голосования без согласия. Наказание: Мут на 15 минут. При 5+ нарушениях в сутки — бан на 1 день.

[3] - Поведение и общение
3.1. Спам и флуд: запрещены спам (однотипные сообщения) и флуд (7+ сообщений, «лесенка»). Наказание: Мут (/mute) на 30 минут. При многократных: внесение в отметку (STRIKE).
3.2. Конфликты и провокации: запрещены бесцельные конфликты, провокации, подстрекательство. Наказание: Предупреждение (/warn) или бан (/ban) до 5 дней.
3.3. Уважение к участникам: запрещены оскорбления, унижение чести и достоинства, агрессивное поведение в отношении ОБЫЧНЫХ УЧАСТНИКОВ (не администрации). Наказание: Мут на 30 минут или бан от 3 до 7 дней.
3.4. Запрещено добавлять людей в беседу без их согласия. Наказание: Предупреждение (/warn). За 2+ случаев — бан от 3 до 5 дней.
3.5. Аморальные действия: запрещены действия интимного характера без явного согласия. Наказание: Бессрочное предупреждение (/warn) + внесение в отметку (STRIKE).

[4] - Недопустимый контент и тяжкие нарушения
4.1. Угрозы и экстремизм: запрещены угрозы жизни и здоровью, оскорбления родных и близких, неуважение к государству. Наказание: Бессрочная блокировка (/permban).
4.2. Дезинформация и клевета: запрещены обман, клевета, призывы покинуть сообщество. Наказание: Бан от 20 дней до бессрочного.
4.3. Реклама и пиар: запрещена несанкционированная реклама, ссылки, пиар других чатов. Наказание: Бан от 30 дней до бессрочного.
4.4. Дискредитация проекта: запрещены оскорбления проекта, репутации и администрации. Наказание: Мут на 300 минут. При продолжении в ЛС — бан от 30 дней до бессрочного.
4.5. Обман: запрещен обман и СКАМ участников. Наказание: Бан от 30 дней до бессрочной блокировки + внесение в отметку (STRIKE) навсегда.

[5] - Отношения к администрации
5.1. Уважение к администрации: ЗАПРЕЩЕНЫ оскорбления, провокации и клевета в адрес АДМИНИСТРАЦИИ (админов, модераторов, создателя). Наказание: Мут от 180 минут до бана на 10 дней.
5.2. Конфликты с администрацией в общем чате запрещены. Жалобы подаются в установленном порядке.
5.3. Помеха работе: запрещен спам в ЛС админам, злоупотребление жалобами, командами вызова. Наказание: Бан на 1 день.
5.4. Выдача себя за администратора: запрещена. Наказание: Бан на 7 дней + черный список администрации.
5.5. Обман администрации: запрещен. Наказание: Бан от 30 дней до бессрочной блокировки.

[6] - Прочее
6.1. Команда @all: запрещена с 00:00 до 08:00 МСК. Запрещено злоупотребление. Наказание: Мут на 60-120 минут.
6.2. Дискуссии на сложные темы: обсуждение политики с целью оскорбления запрещено. Наказание: Мут на 60-120 минут.
6.3. Администрация может применять наказания за действия, вредящие сообществу, даже если они не прописаны в правилах.
6.4. Администрация может изменять правила без уведомления.

ИНФОРМАЦИЯ ОБ АДМИНИСТРАЦИИ:
- Администраторы - это люди, которые следят за порядком в беседе, выдают наказания за нарушения правил и помогают участникам.
- Создатель бота - разработчик. Ссылка на него: [id{ADMIN_VK_ID}|Разработчик]
"""


def get_user_name(user_id: int) -> str:
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
    return any(keyword in message_text.lower() for keyword in KEYWORDS)


def generate_ai_response(message: str, user_name: str) -> str:
    prompt = f"""Ты — бот Сейч в ВК. Тебя создал разработчик [id{ADMIN_VK_ID}|Разработчик].

Правила беседы:
{RULES_TEXT}

ПРАВИЛА ОТВЕТА:
1. "кто такие администраторы" → "Администраторы следят за порядком, выдают наказания и помогают участникам 👑"
2. "кто тебя создал" → "Меня создал [id{ADMIN_VK_ID}|Разработчик] 👨‍💻"
3. Про оскорбление АДМИНА → пункт 5.1 (мут 180 мин до бана 10 дней) ⚠️
4. Про оскорбление УЧАСТНИКА → пункт 3.3 (мут 30 мин или бан 3-7 дней) ⚠️
5. Всегда используй 1-2 эмодзи в ответе 😊👍
"""

    try:
        completion = groq_client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"{user_name}: {message}"}
            ],
            max_tokens=350,
            temperature=0.5
        )
        return completion.choices[0].message.content
    except Exception as e:
        logger.error(f"Ошибка Groq: {e}")
        return "Ошибка, попробуйте позже 😔"


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
    
    should_reply = is_reply_to_bot or is_bot_mentioned(message_text)
    if not should_reply:
        return
    
    user_name = get_user_name(user_id)
    ai_response = generate_ai_response(message_text, user_name)
    
    if ai_response.strip():
        final_message = f"[id{user_id}|{user_name}], {ai_response}"
        send_vk_message(peer_id, final_message, conv_msg_id)


# ========== АВТОПИНГ (чтобы Render не усыпил бота) ==========
def self_ping():
    """Пинг самого себя каждые 4 минуты, чтобы Render не отключал"""
    while True:
        time.sleep(240)  # 4 минуты
        try:
            response = requests.get(f"{RENDER_URL}/ping", timeout=10)
            if response.status_code == 200:
                pass  # Молча пингуем, не засоряем консоль
        except Exception:
            pass  # Ошибки не логируем


# Запускаем автопинг в отдельном потоке
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
        
        if data.get('type') == 'confirmation':
            return CONFIRMATION_CODE, 200, {'Content-Type': 'text/plain'}
        
        if data.get('type') == 'message_new':
            event_id = data.get('event_id')
            
            if event_id in processed_events:
                return 'ok', 200
            
            processed_events[event_id] = time.time()
            
            # Очистка старых
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
            
            # Проверка реплая
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
            
            # Запускаем обработку в отдельном потоке, чтобы не блокировать ответ
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
    """Эндпоинт для автопинга"""
    return 'pong', 200


@app.route('/status', methods=['GET'])
def status():
    """Статус бота"""
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
    print(f"🔄 Автопинг: активен (каждые 4 минуты)")
    print(f"✅ Callback URL: {RENDER_URL}/")
    print(f"✅ Альтернативный: {RENDER_URL}/seych/ai.php")
    print("=" * 50)
    print("💬 Бот готов к работе!")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
