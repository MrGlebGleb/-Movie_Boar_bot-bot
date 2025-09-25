#!/usr/bin/env python3
"""
Movie release Telegram bot with full pagination and pre-caching.
"""

import os
import requests
import asyncio
import uuid
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo
from telegram import constants, Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    PicklePersistence,
    ContextTypes,
)
import translators as ts

# --- Вспомогательные функции ---
def translate_text_blocking(text: str, to_lang='ru') -> str:
    if not text: return ""
    try: return ts.translate_text(text, translator='google', to_language=to_lang)
    except Exception as e:
        print(f"[ERROR] Translators library failed: {e}")
        return text

async def on_startup(context: ContextTypes.DEFAULT_TYPE):
    """Кэширует список жанров при старте бота."""
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

def _get_todays_movie_premieres_blocking(limit=10):
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    url = "https://api.themoviedb.org/3/discover/movie"
    params = {
        "api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc",
        "include_adult": "false", "with_original_language": "en|es|fr|de|it",
        "primary_release_date.gte": today_str, "primary_release_date.lte": today_str,
    }
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return [m for m in r.json().get("results", []) if m.get("poster_path")][:limit]

def _get_movie_details_blocking(movie_id: int):
    url = f"https://api.themoviedb.org/3/movie/{movie_id}"
    params = {"api_key": TMDB_API_KEY, "append_to_response": "videos,watch/providers"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

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

def _parse_trailer(videos_data: dict) -> str | None:
    for video in videos_data.get("results", []):
        if video.get("type") == "Trailer" and video.get("site") == "YouTube":
            return f"https://www.youtube.com/watch?v={video['key']}"
    return None

# --- ФОРМАТИРОВАНИЕ И ПАГИНАЦИЯ ---

async def format_movie_for_pagination(movie_data: dict, genres_map: dict, current_index: int, total_count: int, list_id: str):
    title = movie_data.get("title", "No Title")
    overview = movie_data.get("overview", "Описание отсутствует.")
    poster_url = movie_data.get("poster_url")
    rating = movie_data.get("vote_average", 0)
    genre_names = [genres_map.get(gid, "") for gid in movie_data.get("genre_ids", [])[:2]]
    genres_str = ", ".join(filter(None, genre_names))
    watch_status = movie_data.get("watch_status", "Статус неизвестен")
    trailer_url = movie_data.get("trailer_url")

    text = f"🎬 *Сегодня выходит: {title}*\n\n"
    if rating > 0: text += f"⭐ Рейтинг: {rating:.1f}/10\n"
    text += f"Статус: {watch_status}\n"
    if genres_str: text += f"Жанр: {genres_str}\n"
    text += f"\n{overview}"
    
    keyboard = []
    nav_buttons = []
    if current_index > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page_{list_id}_{current_index - 1}"))
    nav_buttons.append(InlineKeyboardButton(f"[{current_index + 1}/{total_count}]", callback_data="noop"))
    if current_index < total_count - 1:
        nav_buttons.append(InlineKeyboardButton("➡️ Вперед", callback_data=f"page_{list_id}_{current_index + 1}"))
    keyboard.append(nav_buttons)
    if trailer_url:
        keyboard.append([InlineKeyboardButton("🎬 Смотреть трейлер", url=trailer_url)])
    
    return text, poster_url, InlineKeyboardMarkup(keyboard)

# --- КОМАНДЫ И ОБРАБОТЧИКИ ---

async def premieres_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔍 Ищу и обрабатываю сегодняшние премьеры... Это может занять несколько секунд.")
    
    try:
        base_movies = await asyncio.to_thread(_get_todays_movie_premieres_blocking)
        if not base_movies:
            await context.bot.send_message(chat_id, text="🎬 Значимых премьер на сегодня не найдено.")
            return

        enriched_movies = []
        for movie in base_movies:
            details = await asyncio.to_thread(_get_movie_details_blocking, movie['id'])
            overview_ru = await asyncio.to_thread(translate_text_blocking, movie.get("overview", ""))
            
            enriched_movie_data = {
                **movie,
                "overview": overview_ru,
                "watch_status": _parse_watch_providers(details.get("watch/providers", {})),
                "trailer_url": _parse_trailer(details.get("videos", {})),
                "poster_url": f"https://image.tmdb.org/t/p/w780{movie['poster_path']}"
            }
            enriched_movies.append(enriched_movie_data)
            await asyncio.sleep(0.4) # <-- ИЗМЕНЕНИЕ: Увеличена задержка для стабильности
        
        list_id = str(uuid.uuid4())
        context.bot_data.setdefault('movie_lists', {})[list_id] = enriched_movies
        
        text, poster, markup = await format_movie_for_pagination(
            movie_data=enriched_movies[0],
            genres_map=context.bot_data.get('genres', {}),
            current_index=0,
            total_count=len(enriched_movies),
            list_id=list_id
        )
        await context.bot.send_photo(chat_id, photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)

    except Exception as e:
        print(f"[ERROR] Full premiere processing failed: {e}")
        await context.bot.send_message(chat_id, text=f"Произошла ошибка при получении данных.")

async def pagination_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    try:
        _, list_id, new_index_str = query.data.split("_")
        new_index = int(new_index_str)
    except (ValueError, IndexError): return

    movies = context.bot_data.get('movie_lists', {}).get(list_id)
    if not movies or not (0 <= new_index < len(movies)):
        await query.edit_message_text("Ошибка: список устарел. Запросите заново: /releases.")
        return
        
    text, poster, markup = await format_movie_for_pagination(
        movie_data=movies[new_index],
        genres_map=context.bot_data.get('genres', {}),
        current_index=new_index,
        total_count=len(movies),
        list_id=list_id
    )

    try:
        media = InputMediaPhoto(media=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
        await query.edit_message_media(media=media, reply_markup=markup)
    except Exception as e:
        print(f"[WARN] Failed to edit message media: {e}")

# --- СБОРКА И ЗАПУСК ---
def main():
    persistence = PicklePersistence(filepath="bot_data.pkl")
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .persistence(persistence)
        .post_init(on_startup)
        .build()
    )

    application.add_handler(CommandHandler("start", premieres_command))
    application.add_handler(CommandHandler("releases", premieres_command))
    application.add_handler(CallbackQueryHandler(pagination_handler, pattern="^page_"))

    print("[INFO] Starting bot (run_polling).")
    application.run_polling()

if __name__ == "__main__":
    main()
