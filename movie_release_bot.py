#!/usr/bin/env python3
"""
Movie and TV show release Telegram bot with all features including pagination and an advanced random movie feature.
Now focused on digital releases and unified message format.
"""

import os
import requests
import asyncio
import uuid
import random
from datetime import datetime, time, timezone, timedelta
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
    # Movie genres
    try:
        url = "https://api.themoviedb.org/3/genre/movie/list"
        params = {"api_key": TMDB_API_KEY, "language": "ru-RU"}
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        movie_genres = {g['id']: g['name'] for g in r.json()['genres']}
        context.bot_data['movie_genres'] = movie_genres
        context.bot_data['movie_genres_by_name'] = {v.lower(): k for k, v in movie_genres.items()}
        print(f"[INFO] Successfully cached {len(movie_genres)} movie genres.")
    except Exception as e:
        print(f"[ERROR] Could not cache movie genres: {e}")
        context.bot_data['movie_genres'], context.bot_data['movie_genres_by_name'] = {}, {}
    # TV genres
    try:
        url = "https://api.themoviedb.org/3/genre/tv/list"
        params = {"api_key": TMDB_API_KEY, "language": "ru-RU"}
        r = requests.get(url, params=params, timeout=15)
        r.raise_for_status()
        tv_genres = {g['id']: g['name'] for g in r.json()['genres']}
        context.bot_data['tv_genres'] = tv_genres
        context.bot_data['tv_genres_by_name'] = {v.lower(): k for k, v in tv_genres.items()}
        print(f"[INFO] Successfully cached {len(tv_genres)} tv genres.")
    except Exception as e:
        print(f"[ERROR] Could not cache tv genres: {e}")
        context.bot_data['tv_genres'], context.bot_data['tv_genres_by_name'] = {}, {}


# --- CONFIG ---
TELEGRAM_BOT_TOKEN, TMDB_API_KEY = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TMDB_API_KEY")
if not TELEGRAM_BOT_TOKEN or not TMDB_API_KEY:
    raise RuntimeError("One or more environment variables are not set!")

# --- Функции для работы с TMDb ---

def _get_item_details_blocking(item_id: int, item_type: str):
    """
    Получает подробную информацию о фильме или сериале.
    item_type: 'movie' or 'tv'
    """
    url = f"https://api.themoviedb.org/3/{item_type}/{item_id}"
    params = {"api_key": TMDB_API_KEY, "append_to_response": "videos,watch/providers"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def _parse_trailer(videos_data: dict) -> str | None:
    """Извлекает URL трейлера YouTube из данных видео."""
    for video in videos_data.get("results", []):
        if video.get("type") == "Trailer" and video.get("site") == "YouTube":
            return f"https://www.youtube.com/watch?v={video['key']}"
    return None

def _get_watch_status_string(watch_providers_data: dict) -> str:
    """
    Генерирует строку статуса просмотра для цифровых релизов,
    перечисляя доступные сервисы.
    """
    results = watch_providers_data.get("results", {}).get("RU", watch_providers_data.get("results", {}).get("US"))
    if not results: return "Статус релиза неизвестен"
    
    providers = []
    if results.get("flatrate"):
        providers.extend([p["provider_name"] for p in results["flatrate"][:2]])
    if results.get("buy") and not providers:
        providers.extend([p["provider_name"] for p in results["buy"][:2]])

    if providers:
        return f"📺 Онлайн: {', '.join(sorted(list(set(providers))))}"
    return "Статус релиза неизвестен"

async def _enrich_item_data(item: dict, item_type: str) -> dict:
    """Обогащает данные фильма/сериала деталями и переводом."""
    details = await asyncio.to_thread(_get_item_details_blocking, item['id'], item_type)
    overview_ru = await asyncio.to_thread(translate_text_blocking, item.get("overview", ""))
    await asyncio.sleep(0.4)
    
    enriched_item = {
        **item,
        "item_type": item_type,
        "overview": overview_ru,
        "watch_status": _get_watch_status_string(details.get("watch/providers", {})),
        "trailer_url": _parse_trailer(details.get("videos", {})),
        "poster_url": f"https://image.tmdb.org/t/p/w780{item['poster_path']}"
    }
    return enriched_item

# --- Функции для фильмов ---

async def _get_todays_top_digital_releases_blocking(limit=5):
    """Получает топ-N фильмов, чей ЦИФРОВОЙ релиз состоялся сегодня."""
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    url = "https://api.themoviedb.org/3/discover/movie"
    params = {
        "api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc",
        "include_adult": "false", "release_date.gte": today_str, "release_date.lte": today_str,
        "with_release_type": 4, "region": 'RU', "vote_count.gte": 10
    }
    
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    releases = [m for m in r.json().get("results", []) if m.get("poster_path")]
    if not releases:
        params['region'] = 'US'
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        releases = [m for m in r.json().get("results", []) if m.get("poster_path")]
    
    return [await _enrich_item_data(m, 'movie') for m in releases[:limit]]


async def _get_next_digital_releases_blocking(limit=5, search_days=90):
    """Находит ближайший день с цифровыми релизами фильмов."""
    start_date = datetime.now(timezone.utc) + timedelta(days=1)
    for i in range(search_days):
        target_date = start_date + timedelta(days=i)
        target_date_str = target_date.strftime('%Y-%m-%d')
        
        url = "https://api.themoviedb.org/3/discover/movie"
        params = {
            "api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc",
            "include_adult": "false", "release_date.gte": target_date_str, "release_date.lte": target_date_str,
            "with_release_type": 4, "region": 'RU', "vote_count.gte": 10
        }
        
        r = requests.get(url, params=params, timeout=20)
        releases = [m for m in r.json().get("results", []) if m.get("poster_path")]
        if not releases:
            params['region'] = 'US'
            r = requests.get(url, params=params, timeout=20)
            releases = [m for m in r.json().get("results", []) if m.get("poster_path")]
        
        if releases:
            enriched = [await _enrich_item_data(m, 'movie') for m in releases[:limit]]
            return enriched, target_date
    return [], None

# --- Функции для сериалов ---

async def _get_todays_top_series_premieres_blocking(limit=5):
    """Получает топ-N сериалов, чья премьера состоялась сегодня."""
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    url = "https://api.themoviedb.org/3/discover/tv"
    params = {
        "api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc",
        "include_adult": "false", "first_air_date.gte": today_str, "first_air_date.lte": today_str,
        "vote_count.gte": 10, "watch_region": "RU"
    }
    
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    releases = [s for s in r.json().get("results", []) if s.get("poster_path")]
    
    return [await _enrich_item_data(s, 'tv') for s in releases[:limit]]


async def _get_next_series_premieres_blocking(limit=5, search_days=90):
    """Находит ближайший день с премьерами сериалов."""
    start_date = datetime.now(timezone.utc) + timedelta(days=1)
    for i in range(search_days):
        target_date = start_date + timedelta(days=i)
        target_date_str = target_date.strftime('%Y-%m-%d')
        
        url = "https://api.themoviedb.org/3/discover/tv"
        params = {
            "api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc",
            "include_adult": "false", "first_air_date.gte": target_date_str, "first_air_date.lte": target_date_str,
            "vote_count.gte": 10, "watch_region": "RU"
        }
        
        r = requests.get(url, params=params, timeout=20)
        releases = [s for s in r.json().get("results", []) if s.get("poster_path")]
        
        if releases:
            enriched = [await _enrich_item_data(s, 'tv') for s in releases[:limit]]
            return enriched, target_date
    return [], None

# --- Общие функции ---

async def format_item_message(item_data: dict, context: ContextTypes.DEFAULT_TYPE, title_prefix: str, is_paginated: bool = False, current_index: int = 0, total_count: int = 1, list_id: str = "", reroll_data: str = None):
    """Форматирует данные фильма или сериала в сообщение Telegram."""
    title = item_data.get("title") or item_data.get("name")
    overview = item_data.get("overview")
    poster_url = item_data.get("poster_url")
    rating = item_data.get("vote_average", 0)
    genre_ids = item_data.get("genre_ids", [])
    
    genres_map = context.bot_data.get('movie_genres', {}) if item_data.get('item_type') == 'movie' else context.bot_data.get('tv_genres', {})
    genre_names = [genres_map.get(gid, "") for gid in genre_ids[:2]]
    genres_str = ", ".join(filter(None, genre_names))
    watch_status = item_data.get("watch_status")
    trailer_url = item_data.get("trailer_url")
    
    text = f"{title_prefix} *{title}*\n\n"
    if rating > 0: text += f"⭐ Рейтинг: {rating:.1f}/10\n"
    if watch_status: text += f"Статус: {watch_status}\n"
    if genres_str: text += f"Жанр: {genres_str}\n"
    text += f"\n{overview}"
    
    keyboard = []
    if is_paginated and total_count > 1:
        nav_buttons = [
            InlineKeyboardButton("⬅️ Назад", callback_data=f"page_{list_id}_{current_index - 1}") if current_index > 0 else InlineKeyboardButton(" ", callback_data="noop"),
            InlineKeyboardButton(f"[{current_index + 1}/{total_count}]", callback_data="noop"),
            InlineKeyboardButton("➡️ Вперед", callback_data=f"page_{list_id}_{current_index + 1}") if current_index < total_count - 1 else InlineKeyboardButton(" ", callback_data="noop")
        ]
        keyboard.append(nav_buttons)
    
    action_buttons = []
    if reroll_data: action_buttons.append(InlineKeyboardButton("🔄 Повторить", callback_data=reroll_data))
    if trailer_url: action_buttons.append(InlineKeyboardButton("🎬 Смотреть трейлер", url=trailer_url))
    if action_buttons: keyboard.append(action_buttons)
    
    return text, poster_url, InlineKeyboardMarkup(keyboard) if keyboard else None


# --- КОМАНДЫ И ОБРАБОТЧИКИ ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    context.bot_data.setdefault("chat_ids", set()).add(chat_id)
    await help_command(update, context)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = (
        "**Доступные команды:**\n\n"
        "🎬 **Фильмы**\n"
        "• `/releases_movie` — цифровые релизы фильмов сегодня.\n"
        "• `/next_movie` — ближайшие цифровые релизы фильмов.\n"
        "• `/random_movie` — случайный фильм по жанру.\n\n"
        "📺 **Сериалы**\n"
        "• `/releases_series` — премьеры новых сериалов сегодня.\n"
        "• `/next_series` — ближайшие премьеры сериалов.\n"
        "• `/random_series` — случайный сериал по жанру.\n\n"
        "🎲 **Прочее**\n"
        "• `/year <год>` — что выходило в этот день раньше.\n"
        "• `/stop` — отписаться от ежедневной рассылки.\n"
        "• `/help` — показать это сообщение."
    )
    await update.message.reply_text(msg, parse_mode=constants.ParseMode.MARKDOWN)

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if chat_id in context.bot_data.setdefault("chat_ids", set()):
        context.bot_data["chat_ids"].remove(chat_id)
        await update.message.reply_text("❌ Этот чат отписан от рассылки.")
    else:
        await update.message.reply_text("Этот чат и так не был подписан.")

async def releases_movie_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Ищу *цифровые релизы фильмов* на сегодня...")
    try:
        items = await _get_todays_top_digital_releases_blocking(limit=5)
        if not items:
            await update.message.reply_text("🎬 Значимых цифровых релизов фильмов на сегодня не найдено.")
            return
        
        list_id = str(uuid.uuid4())
        context.bot_data.setdefault('item_lists', {})[list_id] = items
        text, poster, markup = await format_item_message(items[0], context, "🎬 Сегодня в цифре (фильм):", is_paginated=True, current_index=0, total_count=len(items), list_id=list_id)
        await update.message.reply_photo(photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
    except Exception as e:
        print(f"[ERROR] releases_movie_command failed: {e}")
        await update.message.reply_text("Произошла ошибка при получении данных.")

async def releases_series_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Ищу *премьеры сериалов* на сегодня...")
    try:
        items = await _get_todays_top_series_premieres_blocking(limit=5)
        if not items:
            await update.message.reply_text("📺 Значимых премьер сериалов на сегодня не найдено.")
            return
        
        list_id = str(uuid.uuid4())
        context.bot_data.setdefault('item_lists', {})[list_id] = items
        text, poster, markup = await format_item_message(items[0], context, "📺 Сегодня премьера (сериал):", is_paginated=True, current_index=0, total_count=len(items), list_id=list_id)
        await update.message.reply_photo(photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
    except Exception as e:
        print(f"[ERROR] releases_series_command failed: {e}")
        await update.message.reply_text("Произошла ошибка при получении данных.")

async def next_movie_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Ищу ближайшие *цифровые релизы фильмов*...")
    try:
        items, release_date = await _get_next_digital_releases_blocking(limit=5)
        if not items:
            await update.message.reply_text("🎬 Не удалось найти цифровые релизы фильмов в ближайшие 3 месяца.")
            return
        
        list_id = str(uuid.uuid4())
        context.bot_data.setdefault('item_lists', {})[list_id] = items
        date_str = release_date.strftime('%d.%m.%Y')
        text, poster, markup = await format_item_message(items[0], context, f"🎬 Ближайший релиз фильмов ({date_str}):", is_paginated=True, current_index=0, total_count=len(items), list_id=list_id)
        await update.message.reply_photo(photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
    except Exception as e:
        print(f"[ERROR] next_movie_command failed: {e}")
        await update.message.reply_text("Произошла ошибка при поиске.")
        
async def next_series_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Ищу ближайшие *премьеры сериалов*...")
    try:
        items, release_date = await _get_next_series_premieres_blocking(limit=5)
        if not items:
            await update.message.reply_text("📺 Не удалось найти премьеры сериалов в ближайшие 3 месяца.")
            return
        
        list_id = str(uuid.uuid4())
        context.bot_data.setdefault('item_lists', {})[list_id] = items
        date_str = release_date.strftime('%d.%m.%Y')
        text, poster, markup = await format_item_message(items[0], context, f"📺 Ближайшая премьера сериалов ({date_str}):", is_paginated=True, current_index=0, total_count=len(items), list_id=list_id)
        await update.message.reply_photo(photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
    except Exception as e:
        print(f"[ERROR] next_series_command failed: {e}")
        await update.message.reply_text("Произошла ошибка при поиске.")

async def year_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # This command remains movie-focused for simplicity
    if not context.args:
        await update.message.reply_text("Укажите год после команды, например: `/year 1999`", parse_mode=constants.ParseMode.MARKDOWN)
        return
    try:
        year = int(context.args[0])
        if not (1970 <= year <= datetime.now().year): raise ValueError("Год вне диапазона")
    except (ValueError, IndexError):
        await update.message.reply_text("Введите корректный год (например, 1995).")
        return
    await update.message.reply_text(f"🔍 Ищу топ-3 *фильма*, вышедших в этот день в {year} году...")
    try:
        month_day = datetime.now(timezone.utc).strftime('%m-%d')
        # Using a simplified blocking call here, as it's a less critical feature
        url = f"https://api.themoviedb.org/3/discover/movie"
        params = {"api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc", "include_adult": "false", "primary_release_date.gte": f"{year}-{month_day}", "primary_release_date.lte": f"{year}-{month_day}"}
        r = requests.get(url, params=params, timeout=20)
        base_movies = [m for m in r.json().get("results", []) if m.get("poster_path")][:3]

        if not base_movies:
            await update.message.reply_text(f"🤷‍♂️ Не нашел значимых премьер фильмов за эту дату в {year} году.")
            return
        
        enriched_movies = [await _enrich_item_data(m, 'movie') for m in base_movies]
        
        list_id = str(uuid.uuid4())
        context.bot_data.setdefault('item_lists', {})[list_id] = enriched_movies
        text, poster, markup = await format_item_message(enriched_movies[0], context, f"🎞️ Релиз {year} года:", is_paginated=True, current_index=0, total_count=len(enriched_movies), list_id=list_id)
        await update.message.reply_photo(photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
    except Exception as e:
        print(f"[ERROR] year_command failed: {e}")
        await update.message.reply_text("Произошла ошибка при поиске по году.")

async def pagination_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    try:
        _, list_id, new_index_str = query.data.split("_")
        new_index = int(new_index_str)
    except (ValueError, IndexError): return
    
    items = context.bot_data.get('item_lists', {}).get(list_id)
    if not items or not (0 <= new_index < len(items)):
        await query.edit_message_text("Ошибка: список устарел. Запросите заново.")
        return
    
    item = items[new_index]
    item_type = item.get("item_type")
    
    date_str = item.get('release_date') or item.get('first_air_date', '????')
    try:
        item_date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
    except ValueError:
        item_date_obj = None

    title_prefix = "🎬"
    if item_date_obj:
        today = datetime.now(timezone.utc).date()
        if item_type == 'movie':
            if item_date_obj == today: title_prefix = "🎬 Сегодня в цифре (фильм):"
            elif item_date_obj > today: title_prefix = f"🎬 Ближайший релиз фильмов ({item_date_obj.strftime('%d.%m.%Y')}):"
            else: title_prefix = f"🎞️ Релиз {item_date_obj.year} года:"
        elif item_type == 'tv':
            if item_date_obj == today: title_prefix = "📺 Сегодня премьера (сериал):"
            else: title_prefix = f"📺 Ближайшая премьера сериалов ({item_date_obj.strftime('%d.%m.%Y')}):"

    text, poster, markup = await format_item_message(item, context, title_prefix, is_paginated=True, current_index=new_index, total_count=len(items), list_id=list_id)
    try:
        media = InputMediaPhoto(media=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
        await query.edit_message_media(media=media, reply_markup=markup)
    except Exception as e:
        print(f"[WARN] Failed to edit message media: {e}")

# --- Функции для случайного выбора ---

async def random_movie_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Предлагает выбрать жанр для случайного фильма."""
    genres_by_name = context.bot_data.get('movie_genres_by_name', {})
    if not genres_by_name:
        await update.message.reply_text("Жанры фильмов еще не загружены, попробуйте через минуту.")
        return

    target_genres = ["Боевик", "Комедия", "Ужасы", "Фантастика", "Триллер", "Драма", "Приключения", "Фэнтези", "Детектив", "Криминал"]
    keyboard = [[InlineKeyboardButton("Мультфильмы", callback_data="random_movie_cartoon"), InlineKeyboardButton("Аниме", callback_data="random_movie_anime")]]
    
    row = []
    for genre_name in target_genres:
        genre_id = genres_by_name.get(genre_name.lower())
        if genre_id:
            row.append(InlineKeyboardButton(genre_name, callback_data=f"random_movie_genre_{genre_id}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
    if row: keyboard.append(row)
    await update.message.reply_text("Выберите категорию или жанр фильма:", reply_markup=InlineKeyboardMarkup(keyboard))

async def random_series_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Предлагает выбрать жанр для случайного сериала."""
    genres_by_name = context.bot_data.get('tv_genres_by_name', {})
    if not genres_by_name:
        await update.message.reply_text("Жанры сериалов еще не загружены, попробуйте через минуту.")
        return

    target_genres = ["Боевик и Приключения", "Комедия", "Драма", "Sci-Fi & Fantasy", "Детектив", "Криминал", "Для детей", "Документальный"]
    keyboard = []
    row = []
    for genre_name in target_genres:
        genre_id = genres_by_name.get(genre_name.lower())
        if genre_id:
            row.append(InlineKeyboardButton(genre_name, callback_data=f"random_series_genre_{genre_id}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
    if row: keyboard.append(row)
    await update.message.reply_text("Выберите жанр сериала:", reply_markup=InlineKeyboardMarkup(keyboard))


async def find_and_send_random_item(query, context: ContextTypes.DEFAULT_TYPE):
    """Общая логика для поиска и отправки случайного фильма или сериала."""
    data = query.data
    action, item_type, selection_type, *rest = data.split("_")

    params = {}
    search_query_text = ""

    if item_type == "movie":
        genres_map = context.bot_data.get('movie_genres', {})
        animation_id = next((gid for gid, name in genres_map.items() if name.lower() == "мультфильм"), "16")
        anime_keyword_id = "210024" # Ключевое слово "anime"
        
        if selection_type == "genre":
            genre_id = rest[0]
            params = {"with_genres": genre_id, "without_genres": animation_id}
            search_query_text = f"'{genres_map.get(int(genre_id))}'"
        elif selection_type == "cartoon":
            params = {"with_genres": animation_id, "without_keywords": anime_keyword_id}
            search_query_text = "'Мультфильм'"
        elif selection_type == "anime":
            params = {"with_genres": animation_id, "with_keywords": anime_keyword_id}
            search_query_text = "'Аниме'"
    
    elif item_type == "series":
        genres_map = context.bot_data.get('tv_genres', {})
        if selection_type == "genre":
            genre_id = rest[0]
            params = {"with_genres": genre_id}
            search_query_text = f"'{genres_map.get(int(genre_id))}'"

    await query.edit_message_text(f"🔍 Подбираю случайный {'фильм' if item_type == 'movie' else 'сериал'} в категории {search_query_text}...")
    try:
        endpoint = "discover/movie" if item_type == "movie" else "discover/tv"
        url = f"https://api.themoviedb.org/3/{endpoint}"
        
        base_params = {
            "api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc",
            "include_adult": "false", "vote_average.gte": 7.5, "vote_count.gte": 150,
            "page": 1, **params
        }
        r = requests.get(url, params=base_params, timeout=20)
        r.raise_for_status()
        api_data = r.json()
        total_pages = min(api_data.get("total_pages", 1), 500)
        
        if total_pages == 0:
            await query.edit_message_text("🤷‍♂️ К сожалению, не удалось найти ничего подходящего. Попробуйте другой жанр.")
            return

        random_page = random.randint(1, total_pages)
        base_params["page"] = random_page
        r = requests.get(url, params=base_params, timeout=20)
        r.raise_for_status()
        results = [item for item in r.json().get("results", []) if item.get("poster_path")]
        
        if not results:
            await query.edit_message_text("🤷‍♂️ Не удалось найти подходящий вариант. Попробуйте еще раз.")
            return
            
        random_item = random.choice(results)
        enriched_item = await _enrich_item_data(random_item, item_type)
        
        reroll_callback_data = data.replace("random_", "reroll_")
        title_prefix = "🎲 Случайный фильм:" if item_type == 'movie' else "🎲 Случайный сериал:"
        
        text, poster, markup = await format_item_message(enriched_item, context, title_prefix, is_paginated=False, reroll_data=reroll_callback_data)
        
        media = InputMediaPhoto(media=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
        await query.edit_message_media(media=media, reply_markup=markup)

    except Exception as e:
        print(f"[ERROR] find_and_send_random_item failed: {e}")
        await query.edit_message_text("Произошла ошибка при поиске.")

async def random_selection_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ПЕРВЫЙ выбор жанра для случайного фильма/сериала."""
    query = update.callback_query
    await query.answer()
    await query.delete_message()    
    temp_message = await context.bot.send_message(query.message.chat_id, "🔍 Подбираю...")
    
    class FakeQuery:
        def __init__(self, msg): self.message = msg
        async def edit_message_text(self, text): return await self.message.edit_text(text)
        async def edit_message_media(self, media, reply_markup): return await self.message.edit_media(media=media, reply_markup=reply_markup)

    fake_query = FakeQuery(temp_message)
    fake_query.data = query.data # pass the original callback data
    await find_and_send_random_item(fake_query, context)

async def reroll_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает кнопку 'Повторить'."""
    query = update.callback_query
    await query.answer()
    await find_and_send_random_item(query, context)


# --- Ежедневные задачи ---

async def daily_movie_check_job(context: ContextTypes.DEFAULT_TYPE):
    print(f"[{datetime.now().isoformat()}] Running daily movie check job")
    chat_ids = context.bot_data.get("chat_ids", set())
    if not chat_ids: return
    try:
        items = await _get_todays_top_digital_releases_blocking(limit=5)
        if not items: return
        
        for chat_id in list(chat_ids):
            list_id = str(uuid.uuid4())
            context.bot_data.setdefault('item_lists', {})[list_id] = items
            text, poster, markup = await format_item_message(items[0], context, "🎬 Сегодня в цифре (фильм):", is_paginated=True, current_index=0, total_count=len(items), list_id=list_id)
            await context.bot.send_photo(chat_id, photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
            await asyncio.sleep(1)
    except Exception as e:
        print(f"[ERROR] Daily movie job failed: {e}")

async def daily_series_check_job(context: ContextTypes.DEFAULT_TYPE):
    print(f"[{datetime.now().isoformat()}] Running daily series check job")
    chat_ids = context.bot_data.get("chat_ids", set())
    if not chat_ids: return
    try:
        items = await _get_todays_top_series_premieres_blocking(limit=5)
        if not items: return
        
        for chat_id in list(chat_ids):
            list_id = str(uuid.uuid4())
            context.bot_data.setdefault('item_lists', {})[list_id] = items
            text, poster, markup = await format_item_message(items[0], context, "📺 Сегодня премьера (сериал):", is_paginated=True, current_index=0, total_count=len(items), list_id=list_id)
            await context.bot.send_photo(chat_id, photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
            await asyncio.sleep(1)
    except Exception as e:
        print(f"[ERROR] Daily series job failed: {e}")

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

    # Command handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("releases_movie", releases_movie_command))
    application.add_handler(CommandHandler("releases_series", releases_series_command))
    application.add_handler(CommandHandler("next_movie", next_movie_command))
    application.add_handler(CommandHandler("next_series", next_series_command))
    application.add_handler(CommandHandler("year", year_command))
    application.add_handler(CommandHandler("random_movie", random_movie_command))
    application.add_handler(CommandHandler("random_series", random_series_command))


    # Callback query handlers
    application.add_handler(CallbackQueryHandler(pagination_handler, pattern="^page_"))
    application.add_handler(CallbackQueryHandler(random_selection_handler, pattern="^random_"))
    application.add_handler(CallbackQueryHandler(reroll_handler, pattern="^reroll_"))
    application.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern="^noop$"))
    
    # Job queue
    tz = ZoneInfo("Europe/Moscow")
    application.job_queue.run_daily(daily_movie_check_job, time(hour=14, minute=0, tzinfo=tz), name="daily_movie_check")
    application.job_queue.run_daily(daily_series_check_job, time(hour=14, minute=5, tzinfo=tz), name="daily_series_check")

    print("[INFO] Starting bot...")
    application.run_polling()

if __name__ == "__main__":
    main()

