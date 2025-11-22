import os
import random
import datetime
from io import BytesIO
import re
import json

import sqlite3

# Создаем БД для статистики
conn = sqlite3.connect("stats.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
CREATE TABLE IF NOT EXISTS stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    username TEXT,
    command TEXT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
)
""")
conn.commit()


from PIL import Image
import requests

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# =====================
# НАСТРОЙКИ
# =====================

BOT_TOKEN = os.getenv("BOT_TOKEN")  # перед запуском: BOT_TOKEN=... python main.py

# raw-URL до папки assets в GitHub
BASE_CDN = os.getenv(
    "BASE_CDN",
    "https://raw.githubusercontent.com/VictorWard18/Tarot_PA_bot/main/assets",
)

# Простое in-memory хранилище сессий (для локального MVP)
STATE = {}  # key: (user_id, date_str) -> {"sphere": ..., "choices": [...], "picked": int|None}


def today_str() -> str:
    """Дата как строка (можно потом привязать к часовому поясу Дубая)."""
    return datetime.date.today().isoformat()


# =====================
# ЗАГРУЗКА И ПАРСИНГ meanings.json
# (работаем даже если файл состоит из склеенных JSON-блоков)
# =====================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(SCRIPT_DIR, "data")
MEANINGS_PATH = os.path.join(DATA_DIR, "meanings.json")


def split_concatenated_json_objects(text: str):
    """
    Разбиваем строку на последовательность независимых JSON-объектов {...}{...}{...}.
    Корректно учитываем строки и экранирование.
    """
    objects = []
    depth = 0
    start = None
    in_string = False
    escape = False

    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
        else:
            if ch == '"':
                in_string = True
            elif ch == "{":
                if depth == 0:
                    start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and start is not None:
                    objects.append(text[start : i + 1])
                    start = None

    return objects


def infer_card_id(obj: dict) -> str:
    """
    Строим card_id (например '2ofcups', 'themagician') по meta.titles и arcana/suit.
    """
    meta = obj.get("meta", {})
    titles = meta.get("titles", {})
    en = (titles.get("en") or "").strip()
    ru = (titles.get("ru") or "").strip()
    arcana = meta.get("arcana")

    if arcana == "major":
        base = en or ru
        base = base.lower()
        # убираем всё, кроме латинских букв и цифр
        cid = re.sub(r"[^a-z0-9]+", "", base)
        return cid or "majorarcana"

    if arcana == "minor":
        suit = meta.get("suit")
        if not suit:
            if "Кубк" in ru:
                suit = "cups"
            elif "Пентакл" in ru:
                suit = "pentacles"
            elif "Меч" in ru:
                suit = "swords"
            elif "Жезл" in ru:
                suit = "wands"

        rank_word = None
        if en:
            rank_word = en.split()[0].lower()
        else:
            first_ru = ru.split()[0] if ru else ""
            mapping_ru = {
                "Туз": "ace",
                "2": "2",
                "3": "3",
                "4": "4",
                "5": "5",
                "6": "6",
                "7": "7",
                "8": "8",
                "9": "9",
                "10": "10",
                "Паж": "page",
                "Рыцарь": "knight",
                "Королева": "queen",
                "Король": "king",
            }
            rank_word = mapping_ru.get(first_ru)

        mapping_en = {
            "ace": "ace",
            "two": "2",
            "three": "3",
            "four": "4",
            "five": "5",
            "six": "6",
            "seven": "7",
            "eight": "8",
            "nine": "9",
            "ten": "10",
            "page": "page",
            "knight": "knight",
            "queen": "queen",
            "king": "king",
        }

        rank = mapping_en.get(rank_word, rank_word or "card")
        if not suit:
            suit = "unknown"

        return f"{rank}of{suit}"

    # запасной вариант
    base = en or ru or "card"
    return re.sub(r"[^a-z0-9]+", "", base.lower()) or "card"


def load_meanings(path: str) -> dict:
    """
    Загружаем meanings из файла, даже если там много склеенных JSON-блоков.
    Собираем единый словарь вида { card_id: {meta, upright, reversed}, ... }.
    """
    if not os.path.exists(path):
        print(f"Файл meanings.json не найден по пути: {path}")
        return {}

    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    objects_text = split_concatenated_json_objects(text)
    if not objects_text:
        print("В meanings.json не найдено ни одного JSON-объекта.")
        return {}

    result = {}
    for idx, obj_text in enumerate(objects_text):
        try:
            obj = json.loads(obj_text)
        except json.JSONDecodeError as e:
            print(f"Блок #{idx} в meanings.json не распознан как JSON: {e}")
            continue

        keys = list(obj.keys())
        # Вариант 1: уже есть card_id на верхнем уровне: {"7ofcups": {...}}
        if len(keys) == 1 and keys[0] not in ("meta", "upright", "reversed"):
            card_id = keys[0]
            card_data = obj[card_id]
        # Вариант 2: голый блок карты: {"meta": {...}, "upright": {...}, "reversed": {...}}
        elif set(keys) == {"meta", "upright", "reversed"}:
            card_id = infer_card_id(obj)
            card_data = obj
        else:
            # неожиданный формат — пропускаем
            print(f"Неожиданный формат блока #{idx} в meanings.json, ключи: {keys}")
            continue

        if card_id in result:
            print(
                f"Предупреждение: дублируется карта '{card_id}' в meanings.json, "
                f"используется последнее определение."
            )
        result[card_id] = card_data

    print(f"Загружено значений карт: {len(result)}")
    return result


MEANINGS = load_meanings(MEANINGS_PATH)


def get_card_text(filename: str, sphere: str, is_reversed: bool, lang: str = "ru"):
    """
    Возвращаем (title, text) для карты из MEANINGS.

    filename: имя файла карты (например, '7ofcups_upright.jpg')
    sphere: 'general' | 'work' | 'love' | 'health'
    is_reversed: True, если карта перевёрнута
    lang: 'ru' или 'en'
    """
    base = os.path.splitext(os.path.basename(filename))[0]

    # card_id из имени файла
    if base.endswith("_upright"):
        card_id = base[: -len("_upright")]
    elif base.endswith("_reversed"):
        card_id = base[: -len("_reversed")]
    else:
        card_id = base

    orientation = "reversed" if is_reversed else "upright"

    card_data = MEANINGS.get(card_id)
    if not card_data:
        # Фолбэк, если карта не найдена
        title = card_id
        text = "Описание этой карты пока не добавлено."
        return title, text

    meta = card_data.get("meta", {})
    titles = meta.get("titles", {})

    title = (
        titles.get(lang)
        or titles.get("ru")
        or (next(iter(titles.values())) if titles else card_id)
    )

    block = card_data.get(orientation, {})
    sphere_key = sphere if sphere in ("general", "work", "love", "health") else "general"
    sphere_block = block.get(sphere_key) or block.get("general") or {}
    text = sphere_block.get(lang) or sphere_block.get("ru") or ""

    if not text:
        text = "Описание этой карты пока не добавлено."

    return title, text


# =====================
# РАБОТА С КАРТИНКАМИ
# =====================


def load_card_filenames():
    """
    Возвращает список файлов карт из папки assets.
    Важно, чтобы имена совпадали с теми, что лежат в GitHub.
    """
    assets_dir = os.path.join(os.path.dirname(__file__), "assets")
    if not os.path.isdir(assets_dir):
        raise RuntimeError(f"Папка assets не найдена по пути: {assets_dir}")

    files = [
        f
        for f in os.listdir(assets_dir)
        if f.lower().endswith((".jpg", ".jpeg", ".png"))
    ]
    files.sort()
    if not files:
        raise RuntimeError("В папке assets нет файлов карт.")
    return files


CARD_FILES = load_card_filenames()
NUM_CARDS = len(CARD_FILES)


def draw_three_cards():
    """
    Выбираем 3 уникальных карты по индексам и случайно решаем, перевёрнутые они или нет.
    Возвращаем список вида: [{"idx": int, "rev": bool}, ...]
    """
    idxs = random.sample(range(NUM_CARDS), 3)
    return [{"idx": i, "rev": random.random() < 0.5} for i in idxs]


def get_card_filename(card_idx: int) -> str:
    """Получаем имя файла по индексу карты."""
    return CARD_FILES[card_idx]


def fetch_and_rotate_image(filename: str, reversed_card: bool) -> BytesIO:
    """
    Скачиваем картинку по raw-URL из GitHub и при необходимости переворачиваем на 180°.
    Возвращаем BytesIO, готовый для отправки в Telegram.
    """
    url = f"{BASE_CDN}/{filename}"
    resp = requests.get(url)
    resp.raise_for_status()

    img = Image.open(BytesIO(resp.content))

    if reversed_card:
        img = img.rotate(180, expand=True)

    output = BytesIO()
    img.save(output, format="JPEG")
    output.seek(0)
    return output


def session_key(user_id: int):
    """Ключ для STATE: (user_id, сегодняшняя дата)."""
    return (user_id, today_str())


# =====================
# ХЕНДЛЕРЫ БОТА
# =====================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Старт / выбор сферы для карты дня."""
    kb = [
        [InlineKeyboardButton("Работа", callback_data="sphere:work")],
        [InlineKeyboardButton("Личная жизнь", callback_data="sphere:love")],
        [InlineKeyboardButton("Здоровье", callback_data="sphere:health")],
        [InlineKeyboardButton("Общая", callback_data="sphere:general")],
    ]
    text = "Выбери сферу, для которой хочешь получить карту дня:"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на кнопки (выбор сферы и выбор карты 1–3)."""
    q = update.callback_query
    await q.answer()
    data = q.data
    user_id = q.from_user.id
    key = session_key(user_id)

    # Пользователь выбрал сферу
    if data.startswith("sphere:"):
        sphere = data.split(":", 1)[1]

        # генерируем тройку карт
        picks = draw_three_cards()
        STATE[key] = {
            "sphere": sphere,
            "choices": picks,
            "picked": None,
        }

        kb = [[
            InlineKeyboardButton("1️⃣", callback_data="pick:0"),
            InlineKeyboardButton("2️⃣", callback_data="pick:1"),
            InlineKeyboardButton("3️⃣", callback_data="pick:2"),
        ]]

        sphere_ru = {
            "work": "Работа",
            "love": "Личная жизнь",
            "health": "Здоровье",
            "general": "Общая",
        }.get(sphere, "Общая")

        await q.edit_message_text(
            f"Сфера: {sphere_ru}\n\nТеперь выбери одну из трёх закрытых карт:",
            reply_markup=InlineKeyboardMarkup(kb),
        )
        return

    # Пользователь выбрал одну из трёх карт
    if data.startswith("pick:"):
        idx_in_three = int(data.split(":", 1)[1])

        sess = STATE.get(key)
        if not sess:
            await q.edit_message_text("Сессия не найдена. Нажми /start, чтобы начать заново.")
            return

        # уже выбирал карту сегодня
        if sess.get("picked") is not None:
            await q.answer("Карта уже выбрана на сегодня ✨", show_alert=True)
            return

        if idx_in_three not in (0, 1, 2):
            await q.answer("Неверный выбор", show_alert=True)
            return

        pick = sess["choices"][idx_in_three]
        sess["picked"] = idx_in_three  # фиксируем выбор

        card_idx = pick["idx"]
        is_reversed = pick["rev"]
        filename = get_card_filename(card_idx)

        # Получаем картинку, повёрнутую при необходимости
        photo_data = fetch_and_rotate_image(filename, is_reversed)

        # Текст из meanings.json
        title, text = get_card_text(filename, sess["sphere"], is_reversed, lang="ru")

        # Если текст уже содержит заголовок, не дублируем
        if text.startswith("Карта дня —"):
            caption = text
        else:
            caption = f"Карта дня — {title}\n\n{text}"

        await q.message.reply_photo(
            photo=photo_data,
            caption=caption,
        )
        return


def main():
    if not BOT_TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN (переменная окружения).")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("day", start))  # /day как алиас
    app.add_handler(CallbackQueryHandler(callback_handler))

    print("Бот запущен. Нажми Ctrl+C для остановки.")
    app.run_polling()


if __name__ == "__main__":
    main()
