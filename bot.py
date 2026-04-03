"""
AI АГРОНОМ — Telegram бот с GigaChat
Токен GigaChat обновляется автоматически каждые 25 минут.
"""

import logging
import base64
import requests
import os
import uuid
import time
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

# ─────────────────────────────────────────────
# НАСТРОЙКИ — берутся из переменных Railway
# ─────────────────────────────────────────────

TELEGRAM_TOKEN       = os.environ.get("TELEGRAM_TOKEN", "")
GIGACHAT_AUTH_KEY    = os.environ.get("GIGACHAT_AUTH_KEY", "")  # Authorization key из личного кабинета
APPS_SCRIPT_URL      = os.environ.get("APPS_SCRIPT_URL", "")

# ─────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

CHOOSING_PLANT, WAITING_PHOTO = range(2)
PLANTS = ["🍅 Томат", "🥒 Огурец", "🌶 Перец", "🥬 Салат", "🌿 Базилик"]

# ─────────────────────────────────────────────
# GIGACHAT — токен (обновляется автоматически)
# ─────────────────────────────────────────────

_gigachat_token = None
_gigachat_token_expires = 0  # время когда истекает


def get_gigachat_token() -> str:
    """Возвращает актуальный токен. Если истёк — получает новый."""
    global _gigachat_token, _gigachat_token_expires

    # Если токен ещё действует — возвращаем его
    if _gigachat_token and time.time() < _gigachat_token_expires:
        return _gigachat_token

    logger.info("Получаем новый токен GigaChat...")

    try:
        resp = requests.post(
            "https://ngw.devices.sberbank.ru:9443/api/v2/oauth",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
                "RqUID": str(uuid.uuid4()),
                "Authorization": f"Basic {GIGACHAT_AUTH_KEY}"
            },
            data={"scope": "GIGACHAT_API_PERS"},
            verify=False,  # Сбер использует свой сертификат
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        _gigachat_token = data["access_token"]
        # Токен живёт 30 минут, обновляем через 25
        _gigachat_token_expires = time.time() + 25 * 60
        logger.info("Токен GigaChat получен успешно")
        return _gigachat_token

    except Exception as e:
        logger.error(f"Ошибка получения токена GigaChat: {e}")
        return ""


# ─────────────────────────────────────────────
# GOOGLE SHEETS через Apps Script
# ─────────────────────────────────────────────

def get_plant_history(plant_name: str) -> str:
    try:
        resp = requests.get(APPS_SCRIPT_URL, params={"plant": plant_name}, timeout=15)
        data = resp.json()
        if not data.get("found"):
            return "первое наблюдение"
        days  = data.get("daysAgo", 0)
        state = data.get("state", "")
        return f"{days} дней назад (состояние было: {state})"
    except Exception as e:
        logger.error(f"Ошибка чтения истории: {e}")
        return "история недоступна"


def save_observation(plant, state, problem, recommendation, next_days):
    try:
        requests.post(APPS_SCRIPT_URL, json={
            "plant": plant, "state": state,
            "problem": problem, "recommendation": recommendation,
            "next_days": next_days
        }, timeout=15)
    except Exception as e:
        logger.error(f"Ошибка сохранения: {e}")


# ─────────────────────────────────────────────
# GIGACHAT — анализ фото
# ─────────────────────────────────────────────

def build_prompt(plant_name: str, history: str) -> str:
    return f"""Ты — опытный агроном. Пользователь прислал фотографию растения из теплицы.

Растение: {plant_name}
Последнее наблюдение: {history}

Твоя задача — строго по фотографии дать структурированный ответ в 4 пункта:

1. СОСТОЯНИЕ — опиши что видишь: цвет листьев, форма, есть ли пятна, увядание, деформации. Одно-два предложения.

2. ПРОБЛЕМА — есть ли признаки болезни, нехватки питания или полива. Если всё хорошо — напиши "Растение здорово". Если есть проблема — назови её точно (например: хлороз, корневая гниль, недостаток азота).

3. РЕКОМЕНДАЦИЯ — одно конкретное действие, которое нужно сделать сегодня или завтра.

4. СЛЕДУЮЩЕЕ ФОТО — через сколько дней прислать следующий снимок и почему именно столько.

Отвечай по-русски. Только эти 4 пункта, никакого лишнего текста."""


def upload_image_to_gigachat(image_bytes: bytes, token: str) -> str:
    """Загружает фото в GigaChat и возвращает file_id."""
    try:
        resp = requests.post(
            "https://gigachat.devices.sberbank.ru/api/v1/files",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json"
            },
            files={"file": ("plant.jpg", image_bytes, "image/jpeg")},
            data={"purpose": "general"},
            verify=False,
            timeout=30
        )
        if resp.status_code != 200:
            logger.error(f"Ошибка загрузки фото: {resp.status_code} — {resp.text}")
            return ""
        return resp.json().get("id", "")
    except Exception as e:
        logger.error(f"Ошибка загрузки фото: {e}")
        return ""


def analyze_photo(image_bytes: bytes, prompt: str) -> dict:
    """Отправляем фото + промпт в GigaChat."""
    token = get_gigachat_token()
    if not token:
        return {"error": "Не удалось получить токен GigaChat. Проверь GIGACHAT_AUTH_KEY."}

    # Загружаем фото
    file_id = upload_image_to_gigachat(image_bytes, token)
    if not file_id:
        return {"error": "Не удалось загрузить фото в GigaChat."}

    # Отправляем запрос с фото
    try:
        payload = {
            "model": "GigaChat-Pro",
            "messages": [{
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": file_id}
                    },
                    {
                        "type": "text",
                        "text": prompt
                    }
                ]
            }],
            "temperature": 0.2,
            "max_tokens": 600
        }

        resp = requests.post(
            "https://gigachat.devices.sberbank.ru/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept": "application/json"
            },
            json=payload,
            verify=False,
            timeout=30
        )

        if resp.status_code != 200:
            logger.error(f"GigaChat ответил: {resp.status_code} — {resp.text}")
            return {"error": f"Ошибка GigaChat: {resp.status_code}"}

        raw = resp.json()["choices"][0]["message"]["content"]
        return parse_response(raw)

    except requests.exceptions.Timeout:
        return {"error": "GigaChat не ответил. Попробуй ещё раз."}
    except Exception as e:
        logger.error(f"Ошибка GigaChat: {e}")
        return {"error": str(e)}


def parse_response(text: str) -> dict:
    result  = {"state": "", "problem": "", "recommendation": "", "next_days": "", "raw": text}
    current = None
    markers = {
        "1.": "state",         "СОСТОЯНИЕ": "state",
        "2.": "problem",       "ПРОБЛЕМА": "problem",
        "3.": "recommendation","РЕКОМЕНДАЦИЯ": "recommendation",
        "4.": "next_days",     "СЛЕДУЮЩЕЕ": "next_days",
    }
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        matched = False
        for key, field in markers.items():
            if line.upper().startswith(key):
                current = field
                sep = "—" if "—" in line else (":" if ":" in line else None)
                result[field] = line.split(sep, 1)[1].strip() if sep else ""
                matched = True
                break
        if not matched and current:
            result[current] += (" " + line) if result[current] else line
    return result


# ─────────────────────────────────────────────
# ОБРАБОТЧИКИ TELEGRAM
# ─────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyboard = [[p] for p in PLANTS]
    await update.message.reply_text(
        "🌱 *AI Агроном* — умный помощник для теплицы\n\nВыбери растение:",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True),
        parse_mode="Markdown"
    )
    return CHOOSING_PLANT


async def plant_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    name  = update.message.text
    clean = name.split(" ", 1)[-1] if " " in name else name
    context.user_data["plant"] = clean
    await update.message.reply_text(
        f"Отлично! Растение: *{clean}*\n\n📸 Пришли фото растения.",
        reply_markup=ReplyKeyboardRemove(),
        parse_mode="Markdown"
    )
    return WAITING_PHOTO


async def photo_received(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    plant = context.user_data.get("plant", "Растение")
    msg   = await update.message.reply_text("🔍 Анализирую фото... 10-20 секунд.")

    photo       = update.message.photo[-1]
    file        = await context.bot.get_file(photo.file_id)
    image_bytes = bytes(await file.download_as_bytearray())

    history = get_plant_history(plant)
    prompt  = build_prompt(plant, history)
    result  = analyze_photo(image_bytes, prompt)

    if "error" in result:
        await msg.edit_text(f"❌ {result['error']}")
        return WAITING_PHOTO

    save_observation(
        plant=plant,
        state=result.get("state", ""),
        problem=result.get("problem", ""),
        recommendation=result.get("recommendation", ""),
        next_days=result.get("next_days", "")
    )

    answer = (
        f"🌿 *{plant}* — результат анализа\n"
        f"_{datetime.now().strftime('%d.%m.%Y %H:%M')}_\n\n"
        f"*1. Состояние*\n{result.get('state', '—')}\n\n"
        f"*2. Проблема*\n{result.get('problem', '—')}\n\n"
        f"*3. Рекомендация*\n{result.get('recommendation', '—')}\n\n"
        f"*4. Следующее фото*\n{result.get('next_days', '—')}\n\n"
        f"✅ _Сохранено в дневник_"
    )
    await msg.edit_text(answer, parse_mode="Markdown")

    keyboard = [[p] for p in PLANTS]
    await update.message.reply_text(
        "Проверить другое растение?",
        reply_markup=ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
    )
    return CHOOSING_PLANT


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("До свидания! Напиши /start чтобы начать.",
                                    reply_markup=ReplyKeyboardRemove())
    return ConversationHandler.END


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            CHOOSING_PLANT: [MessageHandler(filters.TEXT & ~filters.COMMAND, plant_chosen)],
            WAITING_PHOTO: [
                MessageHandler(filters.PHOTO, photo_received),
                MessageHandler(filters.TEXT & ~filters.COMMAND,
                               lambda u, c: u.message.reply_text(
                                   "📸 Пришли *фото* растения.", parse_mode="Markdown"))
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)
    print("✅ AI Агроном запущен.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
