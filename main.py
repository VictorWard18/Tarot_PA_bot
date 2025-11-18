import os
import random
import datetime
from io import BytesIO

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
# пример: https://raw.githubusercontent.com/<user>/<repo>/<branch>/assets
BASE_CDN = os.getenv(
    "BASE_CDN",
    "https://raw.githubusercontent.com/VictorWard18/Tarot_PA_bot/main/assets",
)

# Простое in-memory хранилище сессий (для локального MVP)
STATE = {}  # key: (user_id, date_str) -> {"sphere": ..., "choices": [...], "picked": int|None}

# =====================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# =====================

def today_str() -> str:
    """Дата как строка (можно потом привязать к Asia/Dubai)."""
    return datetime.date.today().isoformat()


def load_card_filenames() -> list[str]:
    """
    Возвращает список файлов карт из папки assets.
    Для GitHub-версии важно, чтобы имена в локальной папке совпадали с теми,
    что лежат в репозитории.
    """
    assets_dir = os.path.join(os.path.dirname(__file__), "assets")
    files = [
        f
        for f in os.listdir(assets_dir)
        if f.lower().endswith((".jpg", ".jpeg", ".png"))
    ]
    files.sort()  # чтобы был стабильный порядок
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
    picks = [{"idx": i, "rev": random.random() < 0.5} for i in idxs]
    return picks


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
    # формат берём JPEG, можно оставить исходный (img.format), но JPEG надёжнее для ТГ
    img.save(output, format="JPEG")
    output.seek(0)
    return output


def session_key(user_id: int) -> tuple[int, str]:
    """Ключ для STATE: (user_id, сегодняшняя дата)."""
    return (user_id, today_str())


# =====================
# ХЕНДЛЕРЫ БОТА
# =====================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Старт / карта дня — выбор сферы."""
    kb = [
        [InlineKeyboardButton("Работа", callback_data="sphere:work")],
        [InlineKeyboardButton("Личная жизнь", callback_data="sphere:love")],
        [InlineKeyboardButton("Здоровье", callback_data="sphere:health")],
        [InlineKeyboardButton("Общая", callback_data="sphere:general")],
    ]
    text = "Выбери сферу, для которой хочешь получить карту дня:"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка нажатий на кнопки (сфера / выбор карты 1–3)."""
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

    # Пользователь выбрал 1 / 2 / 3
    if data.startswith("pick:"):
        idx_in_three = int(data.split(":", 1)[1])

        sess = STATE.get(key)
        if not sess:
            await q.edit_message_text("Сессия не найдена. Нажми /start, чтобы начать заново.")
            return

        # уже выбирал карту сегодня
        if sess["picked"] is not None:
            await q.answer("Карта уже выбрана на сегодня ✨", show_alert=True)
            return

        if idx_in_three not in (0, 1, 2):
            await q.answer("Неверный выбор", show_alert=True)
            return

        pick = sess["choices"][idx_in_three]
        sess["picked"] = idx_in_three  # фиксируем, чтобы второй раз не открыть

        card_idx = pick["idx"]
        is_reversed = pick["rev"]
        filename = get_card_filename(card_idx)

        # Получаем картинку, повёрнутую при необходимости
        photo_data = fetch_and_rotate_image(filename, is_reversed)

        # Пока делаем заглушку по тексту
        pos_text = "перевёрнутая" if is_reversed else "прямая"
        caption = (
            f"Твоя карта дня: {filename}\n"
            f"Положение: {pos_text}\n\n"
            f"Пока это тестовый текст. Позже сюда добавим красивую трактовку под выбранную сферу ✨"
        )

        await q.message.reply_photo(photo=photo_data, caption=caption)

        await q.edit_message_text(
            "Карта выбрана. Возвращайся завтра за новой 🃏"
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

