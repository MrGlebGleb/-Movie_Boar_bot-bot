#!/usr/bin/env python3
"""
Movie release Telegram bot.
Notifies about today's premieres and allows searching for historical releases.
"""

import os
import requests
import asyncio
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from telegram import constants, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    PicklePersistence,
    ContextTypes,
    ConversationHandler,
    filters, # <-- НОВЫЙ ИМПОРТ
)
import translators as ts

# --- Вспомогательная функция для перевода ---
def translate_text_blocking(text: str, to_lang='ru') -> str:
    if not text: return ""
    try:
        return ts.translate_text(text, translator='google', to_language=to_lang)
    except Exception as e:
        print(f"[ERROR] Translators library failed: {e}")
        return text

# --- CONFIG (from env) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")

if not TELEGRAM_BOT_TOKEN or not TMDB_API_KEY:
    raise RuntimeError("One or more environment variables are not set!")

# --- НОВОЕ: Определение состояний для диалога ---
GET_YEAR = 0 # Единственное состояние, в котором мы ждем от пользователя год

# --- Функции для работы с TMDb ---

def _get_todays_movie_premieres_blocking(limit=5):
    # ... (эта функция остается без изменений)
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    url = "https://api.themoviedb.org/3/discover/movie"
    params = {
        "api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc",
        "include_adult": "false", "with_original_language": "en",
        "primary_release_date.gte": today_str, "primary_release_date.lte": today_str,
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("results", [])[:limit]

# --- НОВАЯ ФУНКЦИЯ для поиска по дате в прошлом ---
def _get_historical_premieres_blocking(year: int, month_day: str, limit=3):
    target_date = f"{year}-{month_day}"
    url = "https://api.themoviedb.org/3/discover/movie"
    params = {
        "api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc",
        "include_adult": "false", "primary_release_date.gte": target_date,
        "primary_release_date.lte": target_date,
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json().get("results", [])[:limit]


def _format_movie_message(movie: dict):
    # ... (эта функция остается без изменений)
    title = movie.get("title", "Без названия")
    overview = movie.get("overview", "Описание отсутствует.")
    poster_path = movie.get("poster_path")
    poster_url = f"https://image.tmdb.org/t/p/w780{poster_path}" if poster_path else None
    rating = movie.get("vote_average", 0)
    movie_id = movie.get("id")
    movie_url = f"https://www.themoviedb.org/movie/{movie_id}" if movie_id else None
    text = f"🎬 *Сегодня премьера: {title}*\n\n"
    if rating > 0: text += f"*Рейтинг:* {rating:.1f}/10 ⭐\n\n"
    text += overview
    if movie_url: text += f"\n\n[Подробнее на TMDb]({movie_url})"
    return text, poster_url

# --- ОСНОВНАЯ ЛОГИКА ОТПРАВКИ ---

async def send_premieres_to_chat(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    # ... (эта функция остается без изменений)
    app: Application = context.application
    try:
        movies = await asyncio.to_thread(_get_todays_movie_premieres_blocking)
    except Exception as e:
        print(f"[ERROR] TMDb request failed for chat {chat_id}: {e}")
        await app.bot.send_message(chat_id=chat_id, text="Не удалось получить данные о премьерах.")
        return
    if not movies:
        await app.bot.send_message(chat_id=chat_id, text="🎬 Значимых англоязычных премьер на сегодня не найдено.")
        return
    for movie in movies:
        original_title = movie.get("title", "No Title")
        original_overview = movie.get("overview", "No overview available.")
        translated_title = await asyncio.to_thread(translate_text_blocking, original_title)
        translated_overview = await asyncio.to_thread(translate_text_blocking, original_overview)
        movie["title"] = translated_title
        movie["overview"] = translated_overview
        text, poster = _format_movie_message(movie)
        await _send_to_chat(app, chat_id, text, poster)
        await asyncio.sleep(1.0)

async def _send_to_chat(app: Application, chat_id: int, text: str, photo_url: str | None):
    # ... (эта функция остается без изменений)
    try:
        if photo_url:
            await app.bot.send_photo(chat_id=chat_id, photo=photo_url, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
        else:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=constants.ParseMode.MARKDOWN)
    except Exception as e:
        print(f"[WARN] Failed to send to {chat_id}: {e}")

# --- ЗАДАЧА ДЛЯ ПЛАНИРОВЩИКА ---

async def daily_check_job(context: ContextTypes.DEFAULT_TYPE):
    # ... (эта функция остается без изменений)
    print(f"[{datetime.now().isoformat()}] Running scheduled daily_check_job")
    chat_ids = context.bot_data.get("chat_ids", set())
    if not chat_ids:
        print("[INFO] No registered chats; skipping.")
        return
    print(f"[INFO] Sending daily premieres to {len(chat_ids)} chats.")
    for chat_id in list(chat_ids):
        await send_premieres_to_chat(chat_id, context)

# --- ОБРАБОТЧИКИ КОМАНД TELEGRAM (без диалога) ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (эта функция остается без изменений)
    chat_id = update.effective_chat.id
    chat_ids = context.bot_data.setdefault("chat_ids", set())
    if chat_id not in chat_ids:
        chat_ids.add(chat_id)
        await update.message.reply_text("✅ Бот готов к работе! Я буду присылать сюда ежедневные анонсы кинопремьер.")
        print(f"[INFO] Registered chat_id {chat_id}")
    else:
        await update.message.reply_text("Этот чат уже есть в списке рассылки.")

async def premieres_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (эта функция остается без изменений)
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔍 Ищу сегодняшние премьеры...")
    await send_premieres_to_chat(chat_id, context)
    
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (эта функция остается без изменений)
    chat_id = update.effective_chat.id
    chat_ids = context.bot_data.setdefault("chat_ids", set())
    if chat_id in chat_ids:
        chat_ids.remove(chat_id)
        await update.message.reply_text("❌ Этот чат отписан от рассылки. Чтобы возобновить, используйте /start.")
        print(f"[INFO] Unregistered chat_id {chat_id}")
    else:
        await update.message.reply_text("Этот чат и так не был подписан на рассылку.")

# --- НОВЫЙ РАЗДЕЛ: Обработчики для диалога /history ---

async def history_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Начинает диалог и просит пользователя ввести год."""
    await update.message.reply_text(
        "📅 Какой год вас интересует? Отправьте год от 1970 до 2024.\n\n"
        "Чтобы отменить, введите /cancel."
    )
    return GET_YEAR

async def get_year_from_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Получает год, ищет фильмы и завершает диалог."""
    chat_id = update.effective_chat.id
    try:
        year = int(update.message.text)
        if not (1970 <= year <= 2024):
            raise ValueError("Год вне допустимого диапазона")
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите корректный год (например, 1995).")
        return GET_YEAR # Остаемся в том же состоянии, ждем корректного ввода

    month_day = datetime.now(timezone.utc).strftime('%m-%d')
    await update.message.reply_text(f"🔍 Ищу топ-3 релиза за {month_day}-{year}...")

    try:
        movies = await asyncio.to_thread(_get_historical_premieres_blocking, year, month_day)
        if not movies:
            await update.message.reply_text(f"🤷‍♂️ Не нашел значимых премьер за эту дату в {year} году.")
        else:
            response_text = f"📜 *Топ-3 релиза за {month_day}-{year}:*\n\n"
            for movie in movies:
                # Переводим только название для исторического поиска
                title = await asyncio.to_thread(translate_text_blocking, movie.get('title', 'Без названия'))
                rating = movie.get('vote_average', 0)
                response_text += f"• *{title}* (Рейтинг: {rating:.1f} ⭐)\n"
            await update.message.reply_text(response_text, parse_mode=constants.ParseMode.MARKDOWN)

    except Exception as e:
        print(f"[ERROR] Historical search failed for chat {chat_id}: {e}")
        await update.message.reply_text("Не удалось получить данные. Попробуйте позже.")

    return ConversationHandler.END # Завершаем диалог

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Отменяет текущий диалог."""
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END

# --- СБОРКА И ЗАПУСК ПРИЛОЖЕНИЯ ---
def main():
    persistence = PicklePersistence(filepath="bot_data.pkl")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).persistence(persistence).build()

    # --- ИЗМЕНЕНИЕ: Создаем обработчик диалога ---
    history_conversation_handler = ConversationHandler(
        entry_points=[CommandHandler("history", history_command)],
        states={
            GET_YEAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_year_from_user)]
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )

    # Регистрируем обработчик диалога
    application.add_handler(history_conversation_handler)

    # Регистрируем остальные команды
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("releases", premieres_command))
    application.add_handler(CommandHandler("premieres", premieres_command))
    application.add_handler(CommandHandler("stop", stop_command))

    # Настраиваем ежедневную задачу
    tz = ZoneInfo("Europe/Amsterdam")
    scheduled_time = time(hour=10, minute=0, tzinfo=tz) 
    application.job_queue.run_daily(daily_check_job, scheduled_time, name="daily_movie_check")

    print("[INFO] Starting bot (run_polling).")
    application.run_polling()


if __name__ == "__main__":
    main()
