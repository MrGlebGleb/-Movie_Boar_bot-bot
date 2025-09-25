#!/usr/bin/env python3
"""
Movie release Telegram bot.
Provides detailed daily premieres and historical search.
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
    PicklePersistence,
    ContextTypes,
)
import translators as ts

# --- Вспомогательные функции ---

def translate_text_blocking(text: str, to_lang='ru') -> str:
    if not text: return ""
    try:
        return ts.translate_text(text, translator='google', to_language=to_lang)
    except Exception as e:
        print(f"[ERROR] Translators library failed: {e}")
        return text

async def on_startup(context: ContextTypes.DEFAULT_TYPE):
    """Загружает и кэширует список жанров при старте бота."""
    print("[INFO] Caching movie genres...")
    url = "https://api.themoviedb.org/3/genre/movie/list"
    params = {"api_key": TMDB_API_KEY, "language": "ru-RU"}
    try:
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        genres = {g['id']: g['name'] for g in r.json()['genres']}
        context.bot_data['genres'] = genres
        print(f"[INFO] Successfully cached {len(genres)} genres.")
    except Exception as e:
        print(f"[ERROR] Could not cache genres: {e}")
        context.bot_data['genres'] = {}

# --- CONFIG ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")

if not TELEGRAM_BOT_TOKEN or not TMDB_API_KEY:
    raise RuntimeError("One or more environment variables are not set!")

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
    params = {"api_key": TMDB_API_KEY, "append_to_response": "watch/providers,credits,videos"}
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
    if not results: return "🍿 Только в кинотеатрах"
    flatrate = results.get("flatrate")
    buy = results.get("buy")
    if flatrate:
        names = [p["provider_name"] for p in flatrate[:2]]
        return f"📺 Онлайн: {', '.join(names)}"
    if buy: return "💻 Цифровой релиз"
    return "🍿 Только в кинотеатрах"

def _parse_credits(credits_data: dict) -> (str, str):
    director = "Неизвестен"
    for member in credits_data.get("crew", []):
        if member.get("job") == "Director":
            director = member.get("name", "Неизвестен")
            break
    actors = [actor.get("name") for actor in credits_data.get("cast", [])[:2]]
    return director, ", ".join(actors)

def _parse_trailer(videos_data: dict) -> str | None:
    for video in videos_data.get("results", []):
        if video.get("type") == "Trailer" and video.get("site") == "YouTube":
            return f"https://www.youtube.com/watch?v={video['key']}"
    return None

def _format_movie_message(movie: dict, details: dict, genres_map: dict) -> (str, InlineKeyboardMarkup | None):
    # Извлекаем всю информацию
    title = movie.get("title", "No Title")
    overview = movie.get("overview", "Описание отсутствует.")
    poster_path = movie.get("poster_path")
    poster_url = f"https://image.tmdb.org/t/p/w780{poster_path}" if poster_path else None
    rating = movie.get("vote_average", 0)
    
    watch_status = _parse_watch_providers(details.get("watch/providers", {}))
    director, actors = _parse_credits(details.get("credits", {}))
    trailer_url = _parse_trailer(details.get("videos", {}))
    genre_names = [genres_map.get(gid, "") for gid in movie.get("genre_ids", [])[:2]]
    genres_str = ", ".join(filter(None, genre_names))

    # Формируем текст
    text = f"🎬 *Сегодня премьера: {title}*\n\n"
    if rating > 0: text += f"⭐ Рейтинг: {rating:.1f}/10\n"
    text += f"Статус: {watch_status}\n"
    if genres_str: text += f"Жанр: {genres_str}\n"
    if director: text += f"Режиссер: {director}\n"
    if actors: text += f"В ролях: {actors}\n"
    text += f"\n{overview}"

    # Формируем кнопку
    reply_markup = None
    if trailer_url:
        keyboard = [[InlineKeyboardButton("🎬 Смотреть трейлер", url=trailer_url)]]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
    return text, poster_url, reply_markup

# --- ОСНОВНАЯ ЛОГИКА ОТПРАВКИ ---

async def send_premieres_to_chat(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    app: Application = context.application
    genres_map = context.bot_data.get('genres', {})
    if not genres_map:
        await app.bot.send_message(chat_id=chat_id, text="Ошибка: не удалось загрузить список жанров. Уведомления могут быть неполными.")

    try:
        movies = await asyncio.to_thread(_get_todays_movie_premieres_blocking)
    except Exception as e:
        await app.bot.send_message(chat_id=chat_id, text="Не удалось получить данные о премьерах.")
        return
    if not movies:
        await app.bot.send_message(chat_id=chat_id, text="🎬 Значимых англоязычных премьер на сегодня не найдено.")
        return
        
    for movie in movies:
        try:
            details = await asyncio.to_thread(_get_movie_details_blocking, movie['id'])
            movie["overview"] = await asyncio.to_thread(translate_text_blocking, movie.get("overview", ""))
            
            text, poster, markup = _format_movie_message(movie, details, genres_map)
            await _send_to_chat(app, chat_id, text, poster, markup)
            await asyncio.sleep(1.5)
        except Exception as e:
            print(f"[WARN] Failed to process movie ID {movie.get('id')}: {e}")
            continue

async def _send_to_chat(app: Application, chat_id: int, text: str, photo_url: str | None, markup: InlineKeyboardMarkup | None):
    try:
        if photo_url:
            await app.bot.send_photo(chat_id=chat_id, photo=photo_url, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
        else:
            await app.bot.send_message(chat_id=chat_id, text=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
    except Exception as e:
        print(f"[WARN] Failed to send to {chat_id}: {e}")

# --- ПЛАНИРОВЩИК И КОМАНДЫ ---

async def daily_check_job(context: ContextTypes.DEFAULT_TYPE):
    print(f"[{datetime.now().isoformat()}] Running scheduled daily_check_job")
    chat_ids = context.bot_data.get("chat_ids", set())
    if not chat_ids: return
    for chat_id in list(chat_ids):
        await send_premieres_to_chat(chat_id, context)

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_ids = context.bot_data.setdefault("chat_ids", set())
    msg = (
        "✅ Бот готов к работе!\n\n"
        "Я буду ежедневно в 14:00 по МСК присылать сюда анонсы кинопремьер.\n\n"
        "**Доступные команды:**\n"
        "• `/releases` — показать премьеры на сегодня.\n"
        "• `/year <год>` — показать топ-3 фильма, вышедших в этот день в прошлом (например: `/year 1999`).\n"
        "• `/help` — показать это сообщение.\n"
        "• `/stop` — отписаться от рассылки."
    )
    if chat_id not in chat_ids:
        chat_ids.add(chat_id)
        await update.message.reply_text(msg, parse_mode=constants.ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("Этот чат уже есть в списке. " + msg, parse_mode=constants.ParseMode.MARKDOWN)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "**Список команд:**\n\n"
        "• `/releases` — показать премьеры на сегодня.\n"
        "• `/year <год>` — показать топ-3 фильма, вышедших в этот день в прошлом.\n"
        "• `/start` — подписаться на рассылку.\n"
        "• `/stop` — отписаться от рассылки.",
        parse_mode=constants.ParseMode.MARKDOWN
    )

async def premieres_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Ищу сегодняшние премьеры...")
    await send_premieres_to_chat(update.effective_chat.id, context)
    
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in context.bot_data.setdefault("chat_ids", set()):
        context.bot_data["chat_ids"].remove(chat_id)
        await update.message.reply_text("❌ Этот чат отписан от рассылки.")
    else:
        await update.message.reply_text("Этот чат и так не был подписан.")

async def year_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Укажите год после команды, например: `/year 1999`", parse_mode=constants.ParseMode.MARKDOWN)
        return
    try:
        year = int(context.args[0])
        if not (1970 <= year <= 2025): raise ValueError("Год вне диапазона")
    except (ValueError, IndexError):
        await update.message.reply_text("Введите корректный год (например, 1995).")
        return
    
    month_day = datetime.now(timezone.utc).strftime('%m-%d')
    await update.message.reply_text(f"🔍 Ищу топ-3 релиза за {month_day}-{year}...")
    try:
        movies = await asyncio.to_thread(_get_historical_premieres_blocking, year, month_day)
        if not movies:
            await update.message.reply_text(f"🤷‍♂️ Не нашел значимых премьер за эту дату в {year} году.")
            return

        for movie in movies:
            try:
                details = await asyncio.to_thread(_get_movie_details_blocking, movie['id'])
                overview = await asyncio.to_thread(translate_text_blocking, movie.get("overview", ""))
                trailer_url = _parse_trailer(details.get("videos", {}))
                
                text = f"🎞️ *{movie.get('title')}* ({year})\n⭐ Рейтинг: {movie.get('vote_average', 0):.1f}/10\n\n{overview}"
                markup = None
                if trailer_url:
                    markup = InlineKeyboardMarkup([[InlineKeyboardButton("🎬 Смотреть трейлер", url=trailer_url)]])
                
                await update.message.reply_text(text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
                await asyncio.sleep(0.8)
            except Exception as e:
                print(f"[WARN] Failed to process historical movie ID {movie.get('id')}: {e}")
                continue
    except Exception as e:
        print(f"[ERROR] Historical search failed: {e}")
        await update.message.reply_text("Не удалось получить данные. Попробуйте позже.")


# --- СБОРКА И ЗАПУСК ---
def main():
    persistence = PicklePersistence(filepath="bot_data.pkl")
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .persistence(persistence)
        .post_init(on_startup) # Выполняем кэширование жанров при запуске
        .build()
    )

    # Регистрируем команды
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("releases", premieres_command))
    application.add_handler(CommandHandler("premieres", premieres_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("year", year_command))

    # Настраиваем ежедневную задачу
    tz = ZoneInfo("Europe/Moscow")
    scheduled_time = time(hour=14, minute=0, tzinfo=tz)
    application.job_queue.run_daily(daily_check_job, scheduled_time, name="daily_movie_check")

    print("[INFO] Starting bot (run_polling).")
    application.run_polling()

if __name__ == "__main__":
    main()
