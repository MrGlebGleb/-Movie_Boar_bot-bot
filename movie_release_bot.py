#!/usr/bin/env python3
"""
Movie release Telegram bot.
Notifies about today's movie premieres.
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
    PicklePersistence,
    ContextTypes,
)

# --- CONFIG (from env) ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY") # <-- ИЗМЕНЕНО: используем ключ TMDb

if not TELEGRAM_BOT_TOKEN or not TMDB_API_KEY:
    raise RuntimeError("One or more environment variables are not set!")

# --- НОВЫЕ ФУНКЦИИ для работы с TMDb ---

def _get_todays_movie_premieres_blocking(limit=5):
    """
    Makes a request to TMDb API to get today's movie premieres,
    sorted by popularity.
    """
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    
    # Документация по эндпоинту: https://developer.themoviedb.org/reference/discover-movie
    url = "https://api.themoviedb.org/3/discover/movie"
    params = {
        "api_key": TMDB_API_KEY,
        "language": "ru-RU", # Сразу запрашиваем данные на русском
        "sort_by": "popularity.desc",
        "include_adult": "false",
        "primary_release_date.gte": today_str,
        "primary_release_date.lte": today_str,
    }
    
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    results = r.json().get("results", [])
    
    # Ограничиваем количество результатов
    return results[:limit]

def _format_movie_message(movie: dict):
    """Formats a message text and poster URL for a given movie."""
    title = movie.get("title", "Без названия")
    overview = movie.get("overview", "Описание отсутствует.")
    poster_path = movie.get("poster_path")
    
    # Формируем полную ссылку на постер
    poster_url = f"https://image.tmdb.org/t/p/w780{poster_path}" if poster_path else None
    
    rating = movie.get("vote_average", 0)
    movie_id = movie.get("id")
    movie_url = f"https://www.themoviedb.org/movie/{movie_id}" if movie_id else None

    # Формируем текст сообщения
    text = f"🎬 *Сегодня премьера: {title}*\n\n"
    if rating > 0:
        text += f"*Рейтинг:* {rating:.1f}/10 ⭐\n\n"
    text += overview
    if movie_url:
        text += f"\n\n[Подробнее на TMDb]({movie_url})"
        
    return text, poster_url

# --- ОСНОВНАЯ ЛОГИКА ОТПРАВКИ ---

async def send_premieres_to_chat(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Fetches and sends movie premieres to a specific chat."""
    app: Application = context.application
    
    try:
        # Запускаем блокирующий сетевой запрос в отдельном потоке
        movies = await asyncio.to_thread(_get_todays_movie_premieres_blocking)
    except Exception as e:
        print(f"[ERROR] TMDb request failed for chat {chat_id}: {e}")
        await app.bot.send_message(chat_id=chat_id, text="Не удалось получить данные о премьерах.")
        return

    if not movies:
        await app.bot.send_message(chat_id=chat_id, text="🎬 Значимых премьер на сегодня не найдено.")
        return
        
    for movie in movies:
        text, poster = _format_movie_message(movie)
        await _send_to_chat(app, chat_id, text, poster)
        await asyncio.sleep(0.8) # Небольшая задержка между сообщениями

async def _send_to_chat(app: Application, chat_id: int, text: str, photo_url: str | None):
    """A helper function to send a message with or without a photo."""
    try:
        if photo_url:
            await app.bot.send_photo(chat_id=chat_id, photo=photo_url, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
        else:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=constants.ParseMode.MARKDOWN)
    except Exception as e:
        print(f"[WARN] Failed to send to {chat_id}: {e}")

# --- ЗАДАЧА ДЛЯ ПЛАНИРОВЩИКА ---

async def daily_check_job(context: ContextTypes.DEFAULT_TYPE):
    """The actual job that runs daily."""
    print(f"[{datetime.now().isoformat()}] Running scheduled daily_check_job")
    # bot_data хранится благодаря PicklePersistence
    chat_ids = context.bot_data.get("chat_ids", set())
    if not chat_ids:
        print("[INFO] No registered chats; skipping.")
        return
        
    print(f"[INFO] Sending daily premieres to {len(chat_ids)} chats.")
    for chat_id in chat_ids:
        await send_premieres_to_chat(chat_id, context)

# --- ОБРАБОТЧИКИ КОМАНД TELEGRAM ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command and registers the chat."""
    chat_id = update.effective_chat.id
    # Используем set() для автоматического контроля уникальности ID
    chat_ids = context.bot_data.setdefault("chat_ids", set())

    if chat_id not in chat_ids:
        chat_ids.add(chat_id)
        await update.message.reply_text(
            f"✅ Бот готов к работе! Я запомнил этот чат ({chat_id}) и буду присылать сюда ежедневные анонсы кинопремьер."
        )
        print(f"[INFO] Registered chat_id {chat_id}")
    else:
        await update.message.reply_text("Этот чат уже есть в списке рассылки.")

async def premieres_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /releases command to get today's premieres on demand."""
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔍 Ищу сегодняшние премьеры...")
    await send_premieres_to_chat(chat_id, context)
    
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /stop command to unsubscribe a chat."""
    chat_id = update.effective_chat.id
    chat_ids = context.bot_data.setdefault("chat_ids", set())
    
    if chat_id in chat_ids:
        chat_ids.remove(chat_id)
        await update.message.reply_text("❌ Этот чат отписан от рассылки. Чтобы возобновить, используйте /start.")
        print(f"[INFO] Unregistered chat_id {chat_id}")
    else:
        await update.message.reply_text("Этот чат и так не был подписан на рассылку.")

# --- СБОРКА И ЗАПУСК ПРИЛОЖЕНИЯ ---
def main():
    """Builds and runs the Telegram bot application."""
    # PicklePersistence будет сохранять данные (chat_ids) в файле bot_data.pkl
    persistence = PicklePersistence(filepath="bot_data.pkl")
    
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .persistence(persistence)
        .build()
    )

    # Регистрируем команды
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("releases", premieres_command)) # <-- Команду оставил /releases для удобства
    application.add_handler(CommandHandler("premieres", premieres_command)) # <-- Добавил синоним /premieres
    application.add_handler(CommandHandler("stop", stop_command))

    # Настраиваем ежедневную задачу
    tz = ZoneInfo("Europe/Amsterdam") # Можете поменять на свой часовой пояс
    # Время можно поменять, например на 10 утра
    scheduled_time = time(hour=10, minute=0, tzinfo=tz) 
    
    job_queue = application.job_queue
    job_queue.run_daily(daily_check_job, scheduled_time, name="daily_movie_check")

    print("[INFO] Starting bot (run_polling).")
    application.run_polling()


if __name__ == "__main__":
    main()