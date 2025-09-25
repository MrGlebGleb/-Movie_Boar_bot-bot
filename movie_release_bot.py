#!/usr/bin/env python3
"""
Movie release Telegram bot.
Notifies about premieres with their watch availability status and a history button.
"""

import os
import requests
import asyncio
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from telegram import constants, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    PicklePersistence,
    ContextTypes,
    ConversationHandler,
    CallbackQueryHandler,
    filters,
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

# --- Определение состояний для диалога ---
GET_YEAR = 0

# --- Функции для работы с TMDb ---

def _get_todays_movie_premieres_blocking(limit=5):
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
    url = f"https://api.themoviedb.org/3/movie/{movie_id}"
    params = {"api_key": TMDB_API_KEY, "append_to_response": "watch/providers"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

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

def _parse_watch_providers(providers_data: dict) -> str:
    results = providers_data.get("results", {}).get("RU", providers_data.get("results", {}).get("US"))
    if not results:
        return "🍿 Только в кинотеатрах"
    flatrate = results.get("flatrate")
    buy = results.get("buy")
    if flatrate:
        provider_names = [p["provider_name"] for p in flatrate[:2]]
        return f"📺 Онлайн: {', '.join(provider_names)}"
    if buy:
        return "💻 Цифровой релиз"
    return "🍿 Только в кинотеатрах"

def _format_movie_message(movie: dict, watch_status: str) -> (str, str):
    title = movie.get("title", "No Title")
    overview = movie.get("overview", "Описание отсутствует.")
    poster_path = movie.get("poster_path")
    poster_url = f"https://image.tmdb.org/t/p/w780{poster_path}" if poster_path else None
    rating = movie.get("vote_average", 0)
    text = f"🎬 *Сегодня премьера: {title}*\n\n"
    if rating > 0:
        text += f"⭐ Рейтинг: {rating:.1f}/10\n"
    text += f"Статус: {watch_status}\n\n"
    text += overview
    return text, poster_url

# --- ОСНОВНАЯ ЛОГИКА ОТПРАВКИ ---

async def send_premieres_to_chat(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    app: Application = context.application
    try:
        movies = await asyncio.to_thread(_get_todays_movie_premieres_blocking)
    except Exception as e:
        print(f"[ERROR] TMDb discovery failed for chat {chat_id}: {e}")
        await app.bot.send_message(chat_id=chat_id, text="Не удалось получить данные о премьерах.")
        return

    keyboard = [[
        InlineKeyboardButton("📜 Посмотреть, что выходило в этот день раньше", callback_data="start_history")
    ]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    if not movies:
        await app.bot.send_message(
            chat_id=chat_id,
            text="🎬 Значимых англоязычных премьер на сегодня не найдено.",
            reply_markup=reply_markup
        )
        return

    for movie in movies:
        try:
            details = await asyncio.to_thread(_get_movie_details_blocking, movie['id'])
            watch_status = _parse_watch_providers(details.get("watch/providers", {}))
            movie["overview"] = await asyncio.to_thread(translate_text_blocking, movie.get("overview", ""))
            text, poster = _format_movie_message(movie, watch_status)
            await _send_to_chat(app, chat_id, text, poster)
            await asyncio.sleep(1.5)
        except Exception as e:
            print(f"[WARN] Failed to process movie ID {movie.get('id')}: {e}")
            continue

    await app.bot.send_message(chat_id=chat_id, text="Хотите заглянуть в прошлое?", reply_markup=reply_markup)

async def _send_to_chat(app: Application, chat_id: int, text: str, photo_url: str | None):
    try:
        if photo_url:
            await app.bot.send_photo(chat_id=chat_id, photo=photo_url, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
        else:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=constants.ParseMode.MARKDOWN)
    except Exception as e:
        print(f"[WARN] Failed to send to {chat_id}: {e}")

# --- ПЛАНИРОВЩИК И КОМАНДЫ ---

async def daily_check_job(context: ContextTypes.DEFAULT_TYPE):
    print(f"[{datetime.now().isoformat()}] Running scheduled daily_check_job")
    chat_ids = context.bot_data.get("chat_ids", set())
    if not chat_ids: return
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

# --- ДИАЛОГ ИСТОРИИ ---

async def history_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        text="📅 Какой год вас интересует? Отправьте год от 1970 до 2024.\n\n"
             "Чтобы отменить, введите /cancel."
    )
    return GET_YEAR

async def get_year_from_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    chat_id = update.effective_chat.id
    try:
        year = int(update.message.text)
        if not (1970 <= year <= 2024):
            raise ValueError("Год вне диапазона")
    except ValueError:
        await update.message.reply_text("Пожалуйста, введите корректный год (например, 1995).")
        return GET_YEAR
    month_day = datetime.now(timezone.utc).strftime('%m-%d')
    await update.message.reply_text(f"🔍 Ищу топ-3 релиза за {month_day}-{year}...")
    try:
        movies = await asyncio.to_thread(_get_historical_premieres_blocking, year, month_day)
        if not movies:
            await update.message.reply_text(f"🤷‍♂️ Не нашел значимых премьер за эту дату в {year} году.")
        else:
            response_text = f"📜 *Топ-3 релиза за {month_day}-{year}:*\n\n"
            for movie in movies:
                title = await asyncio.to_thread(translate_text_blocking, movie.get('title', 'Без названия'))
                rating = movie.get('vote_average', 0)
                response_text += f"• *{title}* (Рейтинг: {rating:.1f} ⭐)\n"
            await update.message.reply_text(response_text, parse_mode=constants.ParseMode.MARKDOWN)
    except Exception as e:
        print(f"[ERROR] Historical search failed for chat {chat_id}: {e}")
        await update.message.reply_text("Не удалось получить данные. Попробуйте позже.")
    return ConversationHandler.END

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("Действие отменено.")
    return ConversationHandler.END

# --- СБОРКА И ЗАПУСК ---
def main():
    persistence = PicklePersistence(filepath="bot_data.pkl")
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).persistence(persistence).build()

    history_conversation_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(history_start, pattern="^start_history$")],
        states={
            GET_YEAR: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_year_from_user)]
        },
        fallbacks=[CommandHandler("cancel", cancel_command)],
    )

    application.add_handler(history_conversation_handler)
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("releases", premieres_command))
    application.add_handler(CommandHandler("premieres", premieres_command))
    application.add_handler(CommandHandler("stop", stop_command))

    tz = ZoneInfo("Europe/Moscow")
    scheduled_time = time(hour=14, minute=0, tzinfo=tz)
    application.job_queue.run_daily(daily_check_job, scheduled_time, name="daily_movie_check")

    print("[INFO] Starting bot (run_polling).")
    application.run_polling()

if __name__ == "__main__":
    main()
