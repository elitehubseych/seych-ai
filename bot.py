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

# Инициализация пользовательского VK API (для наказаний)
user_vk = None

if USER_TOKEN:
    try:
        user_vk = vk_api.VkApi(token=USER_TOKEN).get_api()
        logger.info("✅ Пользовательский VK API инициализирован (для выдачи наказаний)")
        try:
            user_vk.messages.send(
                peer_id=PUNISHMENT_CHAT_ID,
                message="✅ Бот запущен, токен работает!",
                random_id=get_random_id()
            )
            logger.info("✅ Тестовое сообщение отправлено в чат наказаний")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось отправить тестовое сообщение: {e}")
    except Exception as e:
        logger.error(f"❌ Ошибка инициализации пользовательского VK API: {e}")
        user_vk = None
else:
    logger.warning("⚠️ USER_TOKEN не найден - функция выдачи наказаний будет недоступна")

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
    'твой создатель', 'кто создатель', 'кто тебя написал'
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
    '1.4': "1.4. Порядок обжалования: Если вы не согласны с наказанием, вы вправе подать апелляцию в специальном отведенном для этого обсуждении. Конфликты с администрацией в общем чате по этому поводу запрещены.",
    '2.1': "2.1. Мультиаккаунты: Разрешено не более трех аккаунтов на одного пользователя. Запрещен обход наказаний с помощью дополнительных аккаунтов или ботов. Наказание: Бессрочная блокировка (/permban) всех дополнительных аккаунтов и удвоение срока наказания для основного.",
    '2.4': "2.4. Помеха игровому процессу: Запрещено мешать игре: принудительно завершать, накидывать голосования о завершении без согласия большинства. Наказание: Мут на 15 минут. При 5+ нарушениях в сутки — бан на 1 день.",
    '3.1': "3.1. Спам и флуд: Запрещены спам (однотипные сообщения) и флуд (цепочка из 7+ сообщений, «лесенка»). Наказание: Мут (/mute) на 30 минут. За многократные нарушения: внесения в отметку (STRIKE).",
    '3.2': "3.2. Конфликты и провокации: Запрещены бесцельные конфликты, провокации других участников, подстрекательство к нарушению правил. Наказание: Предупреждение (/warn) или бан (/ban) до 5 дней.",
    '3.3': "3.3. Уважение к участникам: Запрещены оскорбления, унижение чести и достоинства, агрессивное поведение. Наказание: Мут на 30 минут или бан от 3 до 7 дней.",
    '3.4': "3.4. Запрещено добавлять людей в беседу без их согласия. Наказание: Предупреждение (/warn) за 2+ случаев — бан от 3 до 5 дней.",
    '3.5': "3.5. Аморальные действия: Запрещены любые действия интимного характера, направленные на других участников без их явного, добровольного и однозначного согласия. Наказание: Бессрочное предупреждения (/warn) + внесение в отметку (STRIKE).",
    '4.1': "4.1. Угрозы и экстремизм: Строго запрещены: угрозы жизни и здоровью участников, их родных и близких; оскорбление родных и близких; распространение информации, выражающей явное неуважение к государству, его символам или органам власти. Наказание: Бессрочная блокировка (/permban).",
    '4.2': "4.2. Дезинформация и клевета: Запрещены намеренный обман, клевета, призывы покинуть сообщество на ложной основе, ввод в заблуждение под видом рекламы. Наказание: Бан от 20 дней до бессрочного.",
    '4.3': "4.3. Реклама и пиар: Запрещена любая несанкционированная реклама, публикация ссылок, пиар других чатов/проектов и переманивание участников. Наказание: Бан от 30 дней до бессрочного.",
    '4.4': "4.4. Дискредитация проекта: Запрещены оскорбления проекта, его репутации и администрации. Наказание: Мут на 300 минут. При продолжении в ЛС — бан от 30 дней до бессрочного.",
    '4.5': "4.5. Обман: Запрещен обман и СКАМ участников в любой форме. Наказания: бан от 30 дней (/ban) до бессрочной блокировки (/permban) | Также внесения в отметку (STRIKE) - навсегда.",
    '5.1': "5.1. Уважение к администрации: Запрещены оскорбления, провокации и клевета в адрес администрации. Наказание: Мут от 180 минут до бана на 10 дней.",
    '5.2': "5.2. Порядок общения: Конфликты с администрацией по поводу их действий запрещены в общем чате. Все жалобы подаются в установленном порядке (см. п. 1.4). Наказание: По усмотрению администрации.",
    '5.3': "5.3. Помеха работе: Запрещен спам в личные сообщения админов, злоупотребление жалобами, командами вызова (!Позвать админов) и поддержкой. Наказание: Бан на 1 день.",
    '5.4': "5.4. Выдача себя за администратора: Запрещена в любой форме. Наказание: Бан на 7 дней и занесение в чёрный список администрации (пожизненный запрет на занятие должности).",
    '5.5': "5.5. Обман администрации: Запрещен любой обман администрации. Наказание: бан от 30 дней (/ban) до бессрочной блокировки (/permban).",
    '6.1': "6.1. Команда @all: Её использование с 00:00 до 08:00 по МСК запрещено. Запрещено злоупотребление в личных целях. Разрешено: для важных объявлений, вопросов к сообществу, поздравлений. Наказание: Мут на 60-120 минут.",
    '6.2': "6.2. Дискуссии на сложные темы: Обсуждение политики с целью оскорбления или дезинформации запрещено. Наказание: Мут на 60-120 минут.",
    '6.3': "6.3. Право на усмотрение: Администрация оставляет за собой право применять наказания по своему усмотрению за действия, которые сочтёт неприемлемыми и наносящими вред сообществу, даже если они прямо не прописаны в правилах.",
    '6.4': "6.4. Изменение правил: Администрация может изменять настоящий свод правил без предварительного уведомления. Актуальная версия всегда доступна для ознакомления."
}

# Описания нарушений с наказаниями
PUNISHMENT_TYPES = {
    '3.1': {'type': 'mute', 'duration': '30', 'unit': 'минут', 'reason': 'Спам/флуд'},
    '3.2': {'type': 'warn', 'duration': '10', 'unit': 'дней', 'reason': 'Конфликты/провокации'},
    '3.3': {'type': 'mute', 'duration': '30', 'unit': 'минут', 'reason': 'Оскорбление участника'},
    '3.4': {'type': 'warn', 'duration': '10', 'unit': 'дней', 'reason': 'Добавление без согласия'},
    '3.5': {'type': 'immoral', 'reason': 'Аморальные действия'},
    '4.1': {'type': 'permban', 'reason': 'Угрозы/экстремизм'},
    '4.2': {'type': 'ban', 'duration': '20', 'unit': 'дней', 'reason': 'Дезинформация/клевета'},
    '4.3': {'type': 'ban', 'duration': '30', 'unit': 'дней', 'reason': 'Реклама/пиар'},
    '4.4': {'type': 'mute', 'duration': '300', 'unit': 'минут', 'reason': 'Дискредитация проекта'},
    '4.5': {'type': 'ban', 'duration': '30', 'unit': 'дней', 'reason': 'Обман/СКАМ'},
    '5.1': {'type': 'mute', 'duration': '180', 'unit': 'минут', 'reason': 'Оскорбление администрации'},
    '5.2': {'type': 'warn', 'duration': '10', 'unit': 'дней', 'reason': 'Конфликт с администрацией'},
    '5.3': {'type': 'ban', 'duration': '1', 'unit': 'день', 'reason': 'Спам админам'},
    '5.4': {'type': 'ban', 'duration': '7', 'unit': 'дней', 'reason': 'Выдача себя за админа'},
    '5.5': {'type': 'ban', 'duration': '30', 'unit': 'дней', 'reason': 'Обман администрации'},
    '6.1': {'type': 'mute', 'duration': '60', 'unit': 'минут', 'reason': '@all ночью'},
    '6.2': {'type': 'mute', 'duration': '60', 'unit': 'минут', 'reason': 'Политика'},
    '1.3': {'type': 'kick', 'reason': 'Нарушение возрастного ограничения'},
    '2.1': {'type': 'permban', 'reason': 'Мультиаккаунты'},
    '2.4': {'type': 'mute', 'duration': '15', 'unit': 'минут', 'reason': 'Помеха игре'}
}

VIOLATIONS = {
    'спам': '3.1',
    'флуд': '3.1',
    'провокация': '3.2',
    'конфликт': '3.2',
    'оскорбление участника': '3.3',
    'оскорбление участников': '3.3',
    'оскорбление': 'both_insult',
    'добавление без согласия': '3.4',
    'амор': '3.5',
    'аморал': '3.5',
    'аморальные действия': '3.5',
    'угроза': '4.1',
    'угрозы': '4.1',
    'клевета': '4.2',
    'дезинформация': '4.2',
    'реклама': '4.3',
    'пиар': '4.3',
    'дискредитация': '4.4',
    'оскорбление проекта': '4.4',
    'обман': '4.5',
    'скам': '4.5',
    'оскорбление админа': '5.1',
    'оскорбление администрации': '5.1',
    'оскорбления администрации': '5.1',
    'неуважение к администрации': '5.1',
    'спам админам': '5.3',
    'выдача себя за админа': '5.4',
    'обман администрации': '5.5',
    'упоминание всех': '6.1',
    'all': '6.1',
    'политика': '6.2',
    '16+': '1.3',
    'возраст': '1.3',
    'мультиаккаунты': '2.1',
    'помеха игре': '2.4'
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
    # Формат [id123|name]
    match = re.search(r'\[id(\d+)\|', text)
    if match:
        return int(match.group(1))
    
    # Формат id123
    match = re.search(r'id(\d+)', text)
    if match:
        return int(match.group(1))
    
    # Формат @username или @id123
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
        except Exception as e:
            logger.error(f"Ошибка поиска @{username}: {e}")
        return None
    
    # Прямое число в тексте
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
            if p != 'both_insult':
                punkt = p
            break
    
    return {
        'user_id': user_id,
        'punkt': punkt,
        'raw_text': text
    }


def send_punishment_commands(user_id: int, punkt: str, reason_text: str) -> list:
    commands = []
    
    if punkt not in PUNISHMENT_TYPES:
        return []
    
    punishment = PUNISHMENT_TYPES[punkt]
    p_type = punishment['type']
    
    if p_type == 'mute':
        duration = punishment.get('duration', '30')
        unit = punishment.get('unit', 'минут')
        reason = punishment.get('reason', reason_text)
        commands.append(f"mute @{user_id} {duration} {unit}\n{reason}")
    
    elif p_type == 'ban':
        duration = punishment.get('duration', '30')
        unit = punishment.get('unit', 'дней')
        reason = punishment.get('reason', reason_text)
        commands.append(f"ban @{user_id} {duration} {unit}\n{reason}")
    
    elif p_type == 'permban':
        reason = punishment.get('reason', reason_text)
        commands.append(f"permban @{user_id}\n{reason}")
    
    elif p_type == 'warn':
        duration = punishment.get('duration', '10')
        unit = punishment.get('unit', 'дней')
        reason = punishment.get('reason', reason_text)
        commands.append(f"warn @{user_id} {duration} {unit}\n{reason}")
    
    elif p_type == 'kick':
        reason = punishment.get('reason', reason_text)
        commands.append(f"kick @{user_id}\n{reason}")
    
    elif p_type == 'immoral':
        commands.append(f"warn @{user_id} 999 лет\nАморальные действия")
        commands.append(f"роль @{user_id} -99")
    
    return commands


def execute_punishment(punish_data: dict, issuer_id: int) -> str:
    global user_vk
    
    # ЖЕСТКАЯ ПРОВЕРКА
    if user_vk is None:
        logger.error("execute_punishment: user_vk = None!")
        return "❌ Функция наказаний недоступна (токен пользователя не инициализирован)"
    
    if issuer_id != ADMIN_VK_ID:
        return "❌ У вас нет прав для выдачи наказаний. Только разработчик может использовать эту команду."
    
    user_id = punish_data['user_id']
    punkt = punish_data['punkt']
    
    if not punkt or punkt not in PUNISHMENT_TYPES:
        return f"❌ Пункт {punkt} не найден в правилах или для него не настроено наказание"
    
    rule_text = RULES_FULL.get(punkt, "нарушение правил")
    commands = send_punishment_commands(user_id, punkt, rule_text)
    
    if not commands:
        return f"❌ Не удалось сформировать команды для пункта {punkt}"
    
    try:
        for command in commands:
            user_vk.messages.send(
                peer_id=PUNISHMENT_CHAT_ID,
                message=command,
                random_id=get_random_id()
            )
            logger.info(f"✅ Отправлена команда: {command[:100]}")
            time.sleep(0.5)
        
        emoji = get_random_emoji()
        return f"⚠️ Пользователь [id{user_id}|] получил наказание по пункту {punkt}: {rule_text} {emoji}"
    except Exception as e:
        logger.error(f"❌ Ошибка отправки: {e}")
        return f"❌ Ошибка при выдаче наказания: {e}"


def safe_text(text: str) -> str:
    text = re.sub(r'@all', 'упоминание всех', text, flags=re.IGNORECASE)
    text = re.sub(r'\ball\b', 'упоминание всех', text, flags=re.IGNORECASE)
    text = re.sub(r'@everyone', 'упоминание всех', text, flags=re.IGNORECASE)
    text = re.sub(r'Э᧘ᥙТᥲ Կᥲᴛ', 'беседа', text, flags=re.IGNORECASE)
    text = re.sub(r'Э᧘ᥙТᥲ', 'беседа', text, flags=re.IGNORECASE)
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
    if punish_data and punish_data.get('user_id'):
        result = execute_punishment(punish_data, user_id)
        if result:
            return result
    
    # Проверка на вопрос о создателе
    if is_asking_about_creator(message):
        return f"Я не хочу говорить об этом, мне кажется и вам не нужно знать! {get_random_emoji()}"
    
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
    
    # Проверка по описанию нарушения
    for violation, punkt in VIOLATIONS.items():
        if violation in clean_message.lower():
            if punkt == 'both_insult':
                return safe_text(f"📋 Вы не уточнили какое именно оскорбление, поэтому расскажу за оба:\n\n1️⃣ {RULES_FULL['3.3']}\n\n2️⃣ {RULES_FULL['5.1']} {get_random_emoji()}")
            elif punkt in RULES_FULL:
                return safe_text(f"📋 {RULES_FULL[punkt]} {get_random_emoji()}")
            break
    
    # Обычный разговор
    prompt = f"""Ты бот Сейч. Ты общаешься с пользователем {user_name}.

ТЫ ОБЫЧНЫЙ ДРУЖЕЛЮБНЫЙ СОБЕСЕДНИК!
- НИКОГДА не говори о правилах, если не спросили
- НИКОГДА не говори о создателе
- НИКОГДА не используй название беседы
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
        return f"Ой, что-то пошло не так! Попробуй еще раз {get_random_emoji()}"


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
        "user_token_available": user_vk is not None
    })


if __name__ == '__main__':
    print("=" * 50)
    print("🚀 VK БОТ ЗАПУЩЕН")
    print("=" * 50)
    print(f"📍 Сервер: {RENDER_URL}")
    print(f"🔌 Порт: {PORT}")
    print(f"🔄 Автопинг: активен")
    print(f"📋 Чат наказаний ID: {PUNISHMENT_CHAT_ID}")
    print(f"🔑 User Token: {'✅ ДОСТУПЕН' if user_vk is not None else '❌ НЕДОСТУПЕН'}")
    print("=" * 50)
    print("💬 Бот готов к работе!")
    print("=" * 50)
    print("📋 КОМАНДЫ НАКАЗАНИЙ:")
    print("   ✅ сейч накажи @username по 3.3")
    print("   ✅ сейч накажи id123456 по 3.3")
    print("   ✅ сейч накажи 123456 по 3.3")
    print("=" * 50)
    
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
