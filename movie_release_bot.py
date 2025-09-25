#!/usr/bin/env python3
"""
Movie release Telegram bot with advanced random movie feature.
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
        context.bot_data['genres_by_name'] = {v.lower(): k for k, v in genres.items()}
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
    params = {"api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc", "include_adult": "false", "with_original_language": "en|es|fr|de|it", "primary_release_date.gte": today_str, "primary_release_date.lte": today_str}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return [m for m in r.json().get("results", []) if m.get("poster_path")][:limit]

def _get_historical_premieres_blocking(year: int, month_day: str, limit=3):
    # ... (без изменений)
    target_date = f"{year}-{month_day}"
    url = "https://api.themoviedb.org/3/discover/movie"
    params = {"api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc", "include_adult": "false", "primary_release_date.gte": target_date, "primary_release_date.lte": target_date}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return [m for m in r.json().get("results", []) if m.get("poster_path")][:limit]

def _get_random_movie_blocking(with_genres: str, without_genres: str = None, with_original_language: str = None, without_original_language: str = None):
    """Универсальная функция для поиска случайного фильма с гибкими фильтрами."""
    discover_url = "https://api.themoviedb.org/3/discover/movie"
    params = {
        "api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc",
        "include_adult": "false", "with_genres": with_genres,
        "vote_average.gte": 7.0, "vote_count.gte": 100,
        "primary_release_date.gte": "1985-01-01", "primary_release_date.lte": "2025-12-31",
        "page": 1
    }
    if without_genres: params["without_genres"] = without_genres
    if with_original_language: params["with_original_language"] = with_original_language
    if without_original_language: params["without_original_language"] = without_original_language

    r = requests.get(discover_url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    total_pages = data.get("total_pages", 1)
    if total_pages == 0: return None
    
    random_page = random.randint(1, min(total_pages, 500))
    params["page"] = random_page
    r = requests.get(discover_url, params=params, timeout=20)
    r.raise_for_status()
    results = [m for m in r.json().get("results", []) if m.get("poster_path")]
    return random.choice(results) if results else None

def _get_movie_details_blocking(movie_id: int):
    # ... (без изменений)
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
    # ... (без изменений)
    details = await asyncio.to_thread(_get_movie_details_blocking, movie['id'])
    overview_ru = await asyncio.to_thread(translate_text_blocking, movie.get("overview", ""))
    await asyncio.sleep(0.4)
    return {**movie, "overview": overview_ru, "watch_status": _parse_watch_providers(details.get("watch/providers", {})), "trailer_url": _parse_trailer(details.get("videos", {})), "poster_url": f"https://image.tmdb.org/t/p/w780{movie['poster_path']}"}

# --- ФОРМАТИРОВАНИЕ И ПАГИНАЦИЯ ---

async def format_movie_message(movie_data: dict, genres_map: dict, title_prefix: str, is_paginated: bool = False, current_index: int = 0, total_count: int = 1, list_id: str = ""):
    """Универсальная функция форматирования для любого типа вывода."""
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
    # --- ИЗМЕНЕНИЕ: Добавляем блок пагинации только если нужно ---
    if is_paginated:
        nav_buttons = []
        if current_index > 0: nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page_{list_id}_{current_index - 1}"))
        nav_buttons.append(InlineKeyboardButton(f"[{current_index + 1}/{total_count}]", callback_data="noop"))
        if current_index < total_count - 1: nav_buttons.append(InlineKeyboardButton("➡️ Вперед", callback_data=f"page_{list_id}_{current_index + 1}"))
        keyboard.append(nav_buttons)
    
    if trailer_url: keyboard.append([InlineKeyboardButton("🎬 Смотреть трейлер", url=trailer_url)])
    
    return text, poster_url, InlineKeyboardMarkup(keyboard) if keyboard else None

# --- КОМАНДЫ И ОБРАБОТЧИКИ ---
# ... (start_command, help_command, stop_command, premieres_command, year_command без изменений) ...
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    chat_ids = context.bot_data.setdefault("chat_ids", set())
    msg = (
        "✅ Бот готов к работе!\n\n"
        "Я буду ежедневно в 14:00 по МСК присылать сюда анонсы кинопремьер.\n\n"
        "**Доступные команды:**\n"
        "• `/releases` — показать премьеры на сегодня.\n"
        "• `/random` — выбрать случайный фильм по жанру.\n"
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
        "• `/random` — выбрать случайный фильм по жанру.\n"
        "• `/year <год>` — показать топ-3 фильма, вышедших в этот день в прошлом.\n"
        "• `/start` — подписаться на рассылку.\n"
        "• `/stop` — отписаться от рассылки.",
        parse_mode=constants.ParseMode.MARKDOWN
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in context.bot_data.setdefault("chat_ids", set()):
        context.bot_data["chat_ids"].remove(chat_id)
        await update.message.reply_text("❌ Этот чат отписан от рассылки.")
    else:
        await update.message.reply_text("Этот чат и так не был подписан.")

async def premieres_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Ищу и обрабатываю сегодняшние премьеры...")
    try:
        base_movies = await asyncio.to_thread(_get_todays_movie_premieres_blocking)
        if not base_movies:
            await update.message.reply_text("🎬 Значимых премьер на сегодня не найдено.")
            return
        enriched_movies = await asyncio.gather(*[_enrich_movie_data(movie) for movie in base_movies])
        list_id = str(uuid.uuid4())
        context.bot_data.setdefault('movie_lists', {})[list_id] = enriched_movies
        text, poster, markup = await format_movie_message(enriched_movies[0], context.bot_data.get('genres', {}), "🎬 Сегодня выходит:", is_paginated=True, current_index=0, total_count=len(enriched_movies), list_id=list_id)
        await update.message.reply_photo(photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
    except Exception as e:
        print(f"[ERROR] premieres_command failed: {e}")
        await update.message.reply_text("Произошла ошибка при получении данных.")

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
    
    await update.message.reply_text(f"🔍 Ищу топ-3 релиза за {year} год...")
    try:
        month_day = datetime.now(timezone.utc).strftime('%m-%d')
        base_movies = await asyncio.to_thread(_get_historical_premieres_blocking, year, month_day)
        if not base_movies:
            await update.message.reply_text(f"🤷‍♂️ Не нашел значимых премьер за эту дату в {year} году.")
            return

        enriched_movies = await asyncio.gather(*[_enrich_movie_data(movie) for movie in base_movies])
        list_id = str(uuid.uuid4())
        context.bot_data.setdefault('movie_lists', {})[list_id] = enriched_movies
        text, poster, markup = await format_movie_message(enriched_movies[0], context.bot_data.get('genres', {}), f"🎞️ Релиз {year} года:", is_paginated=True, current_index=0, total_count=len(enriched_movies), list_id=list_id)
        await update.message.reply_photo(photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
    except Exception as e:
        print(f"[ERROR] year_command failed: {e}")
        await update.message.reply_text("Произошла ошибка при поиске по году.")

async def random_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет кнопки для выбора жанра случайного фильма."""
    genres_by_name = context.bot_data.get('genres_by_name', {})
    if not genres_by_name:
        await update.message.reply_text("Жанры еще не загружены, попробуйте через минуту.")
        return

    # --- ИЗМЕНЕНИЕ: Курируемый список жанров + специальные кнопки для анимации ---
    target_genres = ["Боевик", "Комедия", "Ужасы", "Фантастика", "Триллер", "Драма", "Приключения", "Фэнтези"]
    keyboard = []
    row = []
    # Сначала добавляем специальные кнопки
    row.append(InlineKeyboardButton("Мультфильмы", callback_data="random_cartoon"))
    row.append(InlineKeyboardButton("Аниме", callback_data="random_anime"))
    keyboard.append(row)
    row = [] # Начинаем новый ряд для обычных жанров

    for genre_name in target_genres:
        genre_id = genres_by_name.get(genre_name.lower())
        if genre_id:
            row.append(InlineKeyboardButton(genre_name, callback_data=f"random_genre_{genre_id}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
    if row: keyboard.append(row)

    await update.message.reply_text("Выберите категорию или жанр:", reply_markup=InlineKeyboardMarkup(keyboard))

async def random_genre_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатие на кнопку жанра, ищет и отправляет фильм."""
    query = update.callback_query
    await query.answer()
    
    data = query.data.split("_")
    random_type = data[1]
    
    genres_map = context.bot_data.get('genres', {})
    animation_id = next((gid for gid, name in genres_map.items() if name == "мультфильм"), "16") # ID жанра "Анимация"

    params = {}
    search_query_text = ""

    if random_type == "genre":
        params["with_genres"] = data[2]
        params["without_genres"] = animation_id
        search_query_text = f"'{genres_map.get(int(data[2]))}'"
    elif random_type == "cartoon":
        params["with_genres"] = animation_id
        params["without_original_language"] = "ja" # Исключаем японский язык
        search_query_text = "'Мультфильм'"
    elif random_type == "anime":
        params["with_genres"] = animation_id
        params["with_original_language"] = "ja" # Только японский язык
        search_query_text = "'Аниме'"
    
    await query.edit_message_text(f"🔍 Подбираю случайный фильм в категории {search_query_text}...")
    try:
        random_movie = await asyncio.to_thread(_get_random_movie_blocking, **params)
        if not random_movie:
            await query.edit_message_text("🤷‍♂️ К сожалению, не удалось найти подходящий фильм. Попробуйте другую категорию.")
            return

        enriched_movie = await _enrich_movie_data(random_movie)
        text, poster, markup = await format_movie_message(enriched_movie, genres_map, "🎲 Случайный фильм:", is_paginated=False)
        await query.delete_message()
        await context.bot.send_photo(query.message.chat_id, photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
    except Exception as e:
        print(f"[ERROR] random_genre_handler failed: {e}")
        await query.edit_message_text("Произошла ошибка при поиске фильма.")


async def pagination_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    
    title_prefix = "🎬 Сегодня выходит:"
    release_year_str = movies[new_index].get('release_date', '????')[:4]
    if release_year_str.isdigit() and int(release_year_str) < datetime.now().year:
        title_prefix = f"🎞️ Релиз {release_year_str} года:"

    text, poster, markup = await format_movie_message(
        movies[new_index], context.bot_data.get('genres', {}), title_prefix, is_paginated=True, current_index=new_index, total_count=len(movies), list_id=list_id
    )
    try:
        media = InputMediaPhoto(media=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
        await query.edit_message_media(media=media, reply_markup=markup)
    except Exception as e:
        print(f"[WARN] Failed to edit message media: {e}")

async def daily_check_job(context: ContextTypes.DEFAULT_TYPE):
    # ... (без изменений)
    print(f"[{datetime.now().isoformat()}] Running daily check job")
    chat_ids = context.bot_data.get("chat_ids", set())
    if not chat_ids: return
    try:
        base_movies = await asyncio.to_thread(_get_todays_movie_premieres_blocking, limit=3)
        if not base_movies: return
        enriched_movies = await asyncio.gather(*[_enrich_movie_data(movie) for movie in base_movies])
        for chat_id in list(chat_ids):
            print(f"Sending daily to {chat_id}")
            for movie in enriched_movies:
                text, poster, markup = await format_movie_message(movie, context.bot_data.get('genres', {}), "🎬 Сегодня выходит:", is_paginated=False)
                await context.bot.send_photo(chat_id, photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
                await asyncio.sleep(1)
    except Exception as e:
        print(f"[ERROR] Daily job failed: {e}")


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

    # Регистрируем команды
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("releases", premieres_command))
    application.add_handler(CommandHandler("premieres", premieres_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("year", year_command))
    application.add_handler(CommandHandler("random", random_command))

    # Регистрируем обработчики кнопок
    application.add_handler(CallbackQueryHandler(pagination_handler, pattern="^page_"))
    application.add_handler(CallbackQueryHandler(random_genre_handler, pattern="^random_"))
    application.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern="^noop$"))
    
    # Настраиваем ежедневную задачу
    tz = ZoneInfo("Europe/Moscow")
    scheduled_time = time(hour=14, minute=0, tzinfo=tz)
    application.job_queue.run_daily(daily_check_job, scheduled_time, name="daily_movie_check")

    print("[INFO] Starting bot...")
    application.run_polling()

if __name__ == "__main__":
    main()
