#!/usr/bin/env python3
"""
Movie release Telegram bot.
Notifies about premieres with their watch availability status.
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

# --- Функции для работы с TMDb ---

def _get_todays_movie_premieres_blocking(limit=5):
    """Находит топ-5 англоязычных премьер на сегодня."""
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

def _get_movie_details_blocking(movie_id: int):
    """Получает детальную информацию о фильме, включая где его посмотреть."""
    url = f"https://api.themoviedb.org/3/movie/{movie_id}"
    params = {
        "api_key": TMDB_API_KEY,
        "append_to_response": "watch/providers"
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def _parse_watch_providers(providers_data: dict) -> str:
    """Анализирует данные о провайдерах и возвращает статус просмотра."""
    # Проверяем провайдеров для России, если нет - для США как запасной вариант
    results = providers_data.get("results", {}).get("RU", providers_data.get("results", {}).get("US"))
    if not results:
        return "🍿 Только в кинотеатрах"

    flatrate = results.get("flatrate") # Онлайн-кинотеатры по подписке
    buy = results.get("buy") # Покупка в цифре

    if flatrate:
        provider_names = [p["provider_name"] for p in flatrate[:2]] # Берем не больше 2
        return f"📺 Онлайн: {', '.join(provider_names)}"
    
    if buy:
        return "💻 Цифровой релиз"
        
    return "🍿 Только в кинотеатрах"


def _format_movie_message(movie: dict, watch_status: str) -> (str, str):
    """Форматирует сообщение о фильме с учетом статуса просмотра."""
    title = movie.get("title", "No Title") # Название не переводим
    overview = movie.get("overview", "Описание отсутствует.") # Описание переведено
    poster_path = movie.get("poster_path")
    poster_url = f"https://image.tmdb.org/t/p/w780{poster_path}" if poster_path else None
    rating = movie.get("vote_average", 0)

    text = f"🎬 *{title}*\n\n"
    if rating > 0:
        text += f"⭐ Рейтинг: {rating:.1f}/10\n"
    
    text += f"สถานะ: {watch_status}\n\n" # Добавляем статус просмотра
    text += overview
        
    return text, poster_url

# --- ОСНОВНАЯ ЛОГИКА ОТПРАВКИ ---

async def send_premieres_to_chat(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    """Находит премьеры, получает детали и отправляет в чат."""
    app: Application = context.application
    try:
        movies = await asyncio.to_thread(_get_todays_movie_premieres_blocking)
    except Exception as e:
        print(f"[ERROR] TMDb discovery failed for chat {chat_id}: {e}")
        await app.bot.send_message(chat_id=chat_id, text="Не удалось получить данные о премьерах.")
        return

    if not movies:
        await app.bot.send_message(chat_id=chat_id, text="🎬 Значимых англоязычных премьер на сегодня не найдено.")
        return
        
    for movie in movies:
        try:
            # Делаем дополнительный запрос для каждого фильма
            details = await asyncio.to_thread(_get_movie_details_blocking, movie['id'])
            watch_status = _parse_watch_providers(details.get("watch/providers", {}))
            
            # Переводим только описание
            movie["overview"] = await asyncio.to_thread(translate_text_blocking, movie.get("overview", ""))

            # Формируем и отправляем сообщение
            text, poster = _format_movie_message(movie, watch_status)
            await _send_to_chat(app, chat_id, text, poster)
            await asyncio.sleep(1.5) # Увеличим задержку из-за доп. запросов
        except Exception as e:
            print(f"[WARN] Failed to process movie ID {movie.get('id')}: {e}")
            continue # Пропускаем фильм, если с ним возникла проблема

async def _send_to_chat(app: Application, chat_id: int, text: str, photo_url: str | None):
    try:
        if photo_url:
            await app.bot.send_photo(chat_id=chat_id, photo=photo_url, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
        else:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=constants.ParseMode.MARKDOWN)
    except Exception as e:
        print(f"[WARN] Failed to send to {chat_id}: {e}")

# --- ЗАДАЧА ДЛЯ ПЛАНИРОВЩИКА И ОБРАБОТЧИКИ КОМАНД ---
# (Этот раздел остается без изменений)

async def daily_check_job(context: ContextTypes.DEFAULT_TYPE):
    print(f"[{datetime.now().isoformat()}] Running scheduled daily_check_job")
    chat_ids = context.bot_data.get("chat_ids", set())
    if not chat_ids:
        print("[INFO] No registered chats; skipping.")
        return
    print(f"[INFO] Sending daily premieres to {len(chat_ids)} chats.")
    for chat_id in list(chat_ids):
        await send_premieres_to_chat(chat_id, context)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_ids = context.bot_data.setdefault("chat_ids", set())
    if chat_id not in chat_ids:
        chat_ids.add(chat_id)
        await update.message.reply_text("✅ Бот готов к работе! Я буду присылать сюда ежедневные анонсы кинопремьер.")
        print(f"[INFO] Registered chat_id {chat_id}")
    else:
        await update.message.reply_text("Этот чат уже есть в списке рассылки.")

async def premieres_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔍 Ищу сегодняшние премьеры...")
    await send_premieres_to_chat(chat_id, context)
    
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    persistence = PicklePersistence(filepath="bot_data.pkl")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).persistence(persistence).build()

    # Регистрируем команды
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
