#!/usr/bin/env python3
"""
Movie release Telegram bot with full pagination support (media & text).
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
    # Фильтруем фильмы без постера сразу
    return [m for m in r.json().get("results", []) if m.get("poster_path")][:limit]

def _get_movie_details_blocking(movie_id: int):
    url = f"https://api.themoviedb.org/3/movie/{movie_id}"
    params = {"api_key": TMDB_API_KEY, "append_to_response": "videos"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def _parse_trailer(videos_data: dict) -> str | None:
    for video in videos_data.get("results", []):
        if video.get("type") == "Trailer" and video.get("site") == "YouTube":
            return f"https://www.youtube.com/watch?v={video['key']}"
    return None

# --- НОВАЯ ЛОГИКА ФОРМАТИРОВАНИЯ И ПАГИНАЦИИ ---

async def format_movie_for_pagination(movie: dict, genres_map: dict, current_index: int, total_count: int, list_id: str):
    """Готовит все компоненты (текст, постер, кнопки) для одного фильма."""
    # Получаем детали (трейлер)
    details = await asyncio.to_thread(_get_movie_details_blocking, movie['id'])
    trailer_url = _parse_trailer(details.get("videos", {}))
    
    # Получаем базовую информацию
    title = movie.get("title", "No Title")
    overview = movie.get("overview", "Описание отсутствует.")
    poster_path = movie.get("poster_path")
    poster_url = f"https://image.tmdb.org/t/p/w780{poster_path}" if poster_path else None
    rating = movie.get("vote_average", 0)
    genre_names = [genres_map.get(gid, "") for gid in movie.get("genre_ids", [])[:2]]
    genres_str = ", ".join(filter(None, genre_names))

    # Переводим описание
    translated_overview = await asyncio.to_thread(translate_text_blocking, overview)

    # Формируем текст сообщения
    text = f"🎬 *{title}*\n\n"
    if rating > 0: text += f"⭐ Рейтинг: {rating:.1f}/10\n"
    if genres_str: text += f"Жанр: {genres_str}\n"
    text += f"\n{translated_overview}"
    
    # Формируем кнопки
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
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    return text, poster_url, reply_markup

# --- КОМАНДЫ И ОБРАБОТЧИКИ ---

async def premieres_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начинает сессию пагинации для премьер."""
    chat_id = update.effective_chat.id
    await update.message.reply_text("🔍 Ищу сегодняшние премьеры...")
    
    try:
        movies = await asyncio.to_thread(_get_todays_movie_premieres_blocking)
    except Exception as e:
        await context.bot.send_message(chat_id, text=f"Ошибка при получении данных: {e}")
        return

    if not movies:
        await context.bot.send_message(chat_id, text="🎬 Значимых премьер на сегодня не найдено.")
        return

    list_id = str(uuid.uuid4())
    context.bot_data.setdefault('movie_lists', {})[list_id] = movies
    
    text, poster, markup = await format_movie_for_pagination(
        movie=movies[0],
        genres_map=context.bot_data.get('genres', {}),
        current_index=0,
        total_count=len(movies),
        list_id=list_id
    )
    
    await context.bot.send_photo(chat_id, photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)

async def pagination_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия на кнопки 'Назад' и 'Вперед'."""
    query = update.callback_query
    await query.answer()

    try:
        _, list_id, new_index_str = query.data.split("_")
        new_index = int(new_index_str)
    except (ValueError, IndexError):
        return

    movies = context.bot_data.get('movie_lists', {}).get(list_id)

    if not movies or not (0 <= new_index < len(movies)):
        await query.edit_message_text("Ошибка: список фильмов устарел. Запросите релизы заново: /releases.")
        return
        
    text, poster, markup = await format_movie_for_pagination(
        movie=movies[new_index],
        genres_map=context.bot_data.get('genres', {}),
        current_index=new_index,
        total_count=len(movies),
        list_id=list_id
    )

    # --- ВАЖНОЕ ИЗМЕНЕНИЕ: Заменяем медиа-контент ---
    try:
        media = InputMediaPhoto(media=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
        await query.edit_message_media(media=media, reply_markup=markup)
    except Exception as e:
        print(f"[WARN] Failed to edit message media: {e}")


# --- СБОРКА И ЗАПУСК (остальные команды убраны для простоты) ---
def main():
    persistence = PicklePersistence(filepath="bot_data.pkl")
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .persistence(persistence)
        .post_init(on_startup)
        .build()
    )

    application.add_handler(CommandHandler("start", premieres_command)) # /start сразу показывает релизы
    application.add_handler(CommandHandler("releases", premieres_command))
    application.add_handler(CallbackQueryHandler(pagination_handler, pattern="^page_"))

    print("[INFO] Starting bot (run_polling).")
    application.run_polling()

if __name__ == "__main__":
    main()
