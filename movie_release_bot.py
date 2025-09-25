#!/usr/bin/env python3
"""
Movie release Telegram bot with random movie feature.
"""

import os
import requests
import asyncio
import uuid
import random
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
        # Создаем обратный словарь для поиска ID по имени
        context.bot_data['genres_by_name'] = {v: k for k, v in genres.items()}
        print(f"[INFO] Successfully cached {len(genres)} genres.")
    except Exception as e:
        print(f"[ERROR] Could not cache genres: {e}")
        context.bot_data['genres'] = {}
        context.bot_data['genres_by_name'] = {}


# --- CONFIG ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TMDB_API_KEY = os.environ.get("TMDB_API_KEY")

if not TELEGRAM_BOT_TOKEN or not TMDB_API_KEY:
    raise RuntimeError("One or more environment variables are not set!")

# --- Функции для работы с TMDb ---

def _get_todays_movie_premieres_blocking(limit=10):
    # ... (без изменений)
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

def _get_random_movie_blocking(genre_id: int):
    """Находит случайный фильм по жанру, году и рейтингу."""
    # Шаг 1: Делаем первый запрос, чтобы узнать, сколько всего страниц с результатами
    discover_url = "https://api.themoviedb.org/3/discover/movie"
    params = {
        "api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc",
        "include_adult": "false", "with_genres": str(genre_id),
        "vote_average.gte": 7.0, "vote_count.gte": 100, # Добавляем минимальное кол-во голосов
        "primary_release_date.gte": "1985-01-01", "primary_release_date.lte": "2025-12-31",
        "page": 1
    }
    r = requests.get(discover_url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    total_pages = data.get("total_pages", 1)
    
    # TMDb не позволяет смотреть дальше 500-й страницы
    random_page = random.randint(1, min(total_pages, 500))

    # Шаг 2: Делаем второй запрос на случайной странице
    params["page"] = random_page
    r = requests.get(discover_url, params=params, timeout=20)
    r.raise_for_status()
    results = [m for m in r.json().get("results", []) if m.get("poster_path")]
    
    if not results:
        return None
        
    # Шаг 3: Выбираем случайный фильм с этой страницы
    return random.choice(results)


def _get_movie_details_blocking(movie_id: int):
    url = f"https://api.themoviedb.org/3/movie/{movie_id}"
    params = {"api_key": TMDB_API_KEY, "append_to_response": "videos,watch/providers"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def _parse_watch_providers(providers_data: dict) -> str:
    # ... (без изменений)
    results = providers_data.get("results", {}).get("RU", providers_data.get("results", {}).get("US"))
    if not results: return "🍿 Только в кинотеатрах"
    flatrate, buy = results.get("flatrate"), results.get("buy")
    if flatrate:
        names = [p["provider_name"] for p in flatrate[:2]]
        return f"📺 Онлайн: {', '.join(names)}"
    if buy: return "💻 Цифровой релиз"
    return "🍿 Только в кинотеатрах"


def _parse_trailer(videos_data: dict) -> str | None:
    # ... (без изменений)
    for video in videos_data.get("results", []):
        if video.get("type") == "Trailer" and video.get("site") == "YouTube":
            return f"https://www.youtube.com/watch?v={video['key']}"
    return None

async def _enrich_movie_data(movie: dict) -> dict:
    """Асинхронно обогащает данные одного фильма деталями."""
    details = await asyncio.to_thread(_get_movie_details_blocking, movie['id'])
    overview_ru = await asyncio.to_thread(translate_text_blocking, movie.get("overview", ""))
    await asyncio.sleep(0.4)
    return {
        **movie,
        "overview": overview_ru,
        "watch_status": _parse_watch_providers(details.get("watch/providers", {})),
        "trailer_url": _parse_trailer(details.get("videos", {})),
        "poster_url": f"https://image.tmdb.org/t/p/w780{movie['poster_path']}"
    }

# --- ФОРМАТИРОВАНИЕ И ПАГИНАЦИЯ ---

async def format_movie_for_pagination(movie_data: dict, genres_map: dict, current_index: int, total_count: int, list_id: str, title_prefix: str):
    # ... (без изменений)
    title, overview, poster_url = movie_data.get("title"), movie_data.get("overview"), movie_data.get("poster_url")
    rating, genre_ids = movie_data.get("vote_average", 0), movie_data.get("genre_ids", [])
    genre_names = [genres_map.get(gid, "") for gid in genre_ids[:2]]
    genres_str, watch_status, trailer_url = ", ".join(filter(None, genre_names)), movie_data.get("watch_status"), movie_data.get("trailer_url")

    text = f"{title_prefix} *{title}*\n\n"
    if rating > 0: text += f"⭐ Рейтинг: {rating:.1f}/10\n"
    if watch_status: text += f"Статус: {watch_status}\n"
    if genres_str: text += f"Жанр: {genres_str}\n"
    text += f"\n{overview}"
    
    keyboard = []
    nav_buttons = []
    if current_index > 0: nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page_{list_id}_{current_index - 1}"))
    nav_buttons.append(InlineKeyboardButton(f"[{current_index + 1}/{total_count}]", callback_data="noop"))
    if current_index < total_count - 1: nav_buttons.append(InlineKeyboardButton("➡️ Вперед", callback_data=f"page_{list_id}_{current_index + 1}"))
    keyboard.append(nav_buttons)
    if trailer_url: keyboard.append([InlineKeyboardButton("🎬 Смотреть трейлер", url=trailer_url)])
    
    return text, poster_url, InlineKeyboardMarkup(keyboard)

# --- КОМАНДЫ И ОБРАБОТЧИКИ ---

async def premieres_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (без изменений)
    await update.message.reply_text("🔍 Ищу и обрабатываю сегодняшние премьеры...")
    try:
        base_movies = await asyncio.to_thread(_get_todays_movie_premieres_blocking)
        if not base_movies:
            await update.message.reply_text("🎬 Значимых премьер на сегодня не найдено.")
            return

        enriched_movies = await asyncio.gather(*[_enrich_movie_data(movie) for movie in base_movies])
        list_id = str(uuid.uuid4())
        context.bot_data.setdefault('movie_lists', {})[list_id] = enriched_movies
        
        text, poster, markup = await format_movie_for_pagination(
            enriched_movies[0], context.bot_data.get('genres', {}), 0, len(enriched_movies), list_id, "🎬 Сегодня выходит:"
        )
        await update.message.reply_photo(photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
    except Exception as e:
        print(f"[ERROR] premieres_command failed: {e}")
        await update.message.reply_text("Произошла ошибка при получении данных.")

async def pagination_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # ... (без изменений)
    query = update.callback_query
    await query.answer()
    try:
        _, list_id, new_index_str = query.data.split("_")
        new_index = int(new_index_str)
    except (ValueError, IndexError): return

    movies = context.bot_data.get('movie_lists', {}).get(list_id)
    if not movies or not (0 <= new_index < len(movies)):
        await query.edit_message_text("Ошибка: список устарел. Запросите заново.")
        return
        
    title_prefix = "🎬 Сегодня выходит:" if len(movies) > 3 else f"🎞️ Релиз {movies[new_index].get('release_date', '????')[:4]} года:"

    text, poster, markup = await format_movie_for_pagination(
        movies[new_index], context.bot_data.get('genres', {}), new_index, len(movies), list_id, title_prefix
    )
    try:
        media = InputMediaPhoto(media=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
        await query.edit_message_media(media=media, reply_markup=markup)
    except Exception as e:
        print(f"[WARN] Failed to edit message media: {e}")

async def random_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет кнопки для выбора жанра случайного фильма."""
    genres_by_name = context.bot_data.get('genres_by_name', {})
    if not genres_by_name:
        await update.message.reply_text("Жанры еще не загружены, попробуйте через минуту.")
        return

    # Выбираем популярные жанры
    target_genres = ["Комедия", "Ужасы", "Боевик", "Фантастика", "Триллер", "Мелодрама"]
    keyboard = []
    row = []
    for genre_name in target_genres:
        genre_id = genres_by_name.get(genre_name)
        if genre_id:
            row.append(InlineKeyboardButton(genre_name, callback_data=f"random_{genre_id}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
    if row: keyboard.append(row)

    if not keyboard:
        await update.message.reply_text("Не удалось создать список жанров.")
        return

    await update.message.reply_text("Выберите жанр, чтобы я подобрал для вас случайный фильм:", reply_markup=InlineKeyboardMarkup(keyboard))

async def random_genre_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатие на кнопку жанра, ищет и отправляет фильм."""
    query = update.callback_query
    await query.answer()
    
    try:
        _, genre_id_str = query.data.split("_")
        genre_id = int(genre_id_str)
    except (ValueError, IndexError): return
    
    await query.edit_message_text(f"🔍 Подбираю случайный фильм в жанре '{context.bot_data['genres'].get(genre_id)}'...")

    try:
        random_movie = await asyncio.to_thread(_get_random_movie_blocking, genre_id)
        if not random_movie:
            await query.edit_message_text("🤷‍♂️ К сожалению, не удалось найти подходящий фильм. Попробуйте другой жанр.")
            return

        enriched_movie = await _enrich_movie_data(random_movie)
        
        # Используем ту же функцию форматирования, но без пагинации
        text, poster, markup = await format_movie_for_pagination(
            enriched_movie, context.bot_data.get('genres', {}), 0, 1, "random", "🎲 Случайный фильм:"
        )
        
        # Удаляем сообщение "Подбираю..." и отправляем новое с картинкой
        await query.delete_message()
        await context.bot.send_photo(query.message.chat_id, photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)

    except Exception as e:
        print(f"[ERROR] random_genre_handler failed: {e}")
        await query.edit_message_text("Произошла ошибка при поиске фильма.")

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

    # Регистрируем команды (оставим только основные для простоты)
    application.add_handler(CommandHandler("start", premieres_command))
    application.add_handler(CommandHandler("releases", premieres_command))
    application.add_handler(CommandHandler("random", random_command))

    # Регистрируем обработчики кнопок
    application.add_handler(CallbackQueryHandler(pagination_handler, pattern="^page_"))
    application.add_handler(CallbackQueryHandler(random_genre_handler, pattern="^random_"))
    application.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern="^noop$"))

    print("[INFO] Starting bot...")
    application.run_polling()

if __name__ == "__main__":
    main()
