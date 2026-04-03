"""
AI АГРОНОМ — Telegram бот
Версия для Railway — токены читаются из переменных окружения
"""

import logging
import base64
import requests
import os
from datetime import datetime
from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler
)

# ─────────────────────────────────────────────
# НАСТРОЙКИ — берутся из переменных Railway
# ─────────────────────────────────────────────

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
YANDEX_API_KEY   = os.environ.get("YANDEX_API_KEY", "")
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID", "")
APPS_SCRIPT_URL  = os.environ.get("APPS_SCRIPT_URL", "")

# ─────────────────────────────────────────────

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

CHOOSING_PLANT, WAITING_PHOTO = range(2)
PLANTS = ["🍅 Томат", "🥒 Огурец", "🌶 Перец", "🥬 Салат", "🌿 Базилик"]

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
# ЯНДЕКС GPT VISION
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


def analyze_photo(image_bytes: bytes, prompt: str) -> dict:
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    headers = {
        "Authorization": f"Api-Key {YANDEX_API_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "modelUri": f"gpt://{YANDEX_FOLDER_ID}/yandexgpt/latest",
        "completionOptions": {"stream": False, "temperature": 0.2, "maxTokens": 600},
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            {"type": "text", "text": prompt}
        ]}]
    }
    try:
        resp = requests.post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            headers=headers, json=payload, timeout=30
        )
        resp.raise_for_status()
        raw = resp.json()["result"]["alternatives"][0]["message"]["text"]
        return parse_response(raw)
    except requests.exceptions.Timeout:
        return {"error": "Яндекс не ответил. Попробуй ещё раз."}
    except Exception as e:
        return {"error": str(e)}


def parse_response(text: str) -> dict:
    result  = {"state": "", "problem": "", "recommendation": "", "next_days": "", "raw": text}
    current = None
    markers = {
        "1.": "state",        "СОСТОЯНИЕ": "state",
        "2.": "problem",      "ПРОБЛЕМА": "problem",
        "3.": "recommendation","РЕКОМЕНДАЦИЯ": "recommendation",
        "4.": "next_days",    "СЛЕДУЮЩЕЕ": "next_days",
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
