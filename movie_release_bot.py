#!/usr/bin/env python3
"""
Movie release Telegram bot with all features including pagination and an advanced random movie feature.
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
        context.bot_data['genres'], context.bot_data['genres_by_name'] = {}, {}

# --- CONFIG ---
TELEGRAM_BOT_TOKEN, TMDB_API_KEY = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TMDB_API_KEY")
if not TELEGRAM_BOT_TOKEN or not TMDB_API_KEY:
    raise RuntimeError("One or more environment variables are not set!")

# --- Функции для работы с TMDb ---

def _get_movie_details_blocking(movie_id: int):
    """
    Получает подробную информацию о фильме, включая трейлеры и провайдеров просмотра.
    """
    url = f"https://api.themoviedb.org/3/movie/{movie_id}"
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
    results_ru = watch_providers_data.get("results", {}).get("RU")
    results_us = watch_providers_data.get("results", {}).get("US")

    providers = []
    # Приоритизируем RU
    if results_ru:
        if results_ru.get("flatrate"):
            providers.extend([p["provider_name"] for p in results_ru["flatrate"][:2]])
        if results_ru.get("buy") and not providers: # Только покупка, если нет подписки
            providers.extend([p["provider_name"] for p in results_ru["buy"][:2]])
    
    # Запасной вариант - US, если нет RU провайдеров
    if not providers and results_us:
        if results_us.get("flatrate"):
            providers.extend([p["provider_name"] for p in results_us["flatrate"][:2]])
        if results_us.get("buy") and not providers:
            providers.extend([p["provider_name"] for p in results_us["buy"][:2]])

    if providers:
        # Используем set, чтобы избежать дублирования имен провайдеров
        return f"📺 Онлайн: {', '.join(sorted(list(set(providers))))}"
    return "🍿 Статус цифрового релиза неизвестен"


async def _get_todays_top_digital_releases_blocking(limit=5):
    """
    Получает топ-N самых значимых фильмов, чей ЦИФРОВОЙ релиз состоялся сегодня.
    Ищет по дате цифрового релиза, а не театральной премьеры.
    """
    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    url = "https://api.themoviedb.org/3/discover/movie"
    params = {
        "api_key": TMDB_API_KEY,
        "language": "en-US",
        "sort_by": "popularity.desc",
        "include_adult": "false",
        "release_date.gte": today_str,
        "release_date.lte": today_str,
        "with_release_type": 4, # 4 = Digital Release
        "region": 'RU',
        "vote_count.gte": 10
    }
    
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        potential_releases = [m for m in r.json().get("results", []) if m.get("poster_path")]
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] TMDb discover API for digital releases failed: {e}")
        return []

    if not potential_releases:
        print("[INFO] No digital releases found for RU, trying US region as a fallback.")
        params['region'] = 'US'
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            potential_releases = [m for m in r.json().get("results", []) if m.get("poster_path")]
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] TMDb discover API fallback for US region failed: {e}")
            return []

    top_releases = potential_releases[:limit]
    if not top_releases:
        return []
        
    enriched_releases = []
    
    # Обогащаем топ-N фильмов полной информацией (трейлер, провайдеры)
    details_tasks = [asyncio.to_thread(_get_movie_details_blocking, movie['id']) for movie in top_releases]
    all_details = await asyncio.gather(*details_tasks)

    for i, movie in enumerate(top_releases):
        details = all_details[i]
        enriched_movie = {
            **movie,
            "overview": details.get("overview", movie.get("overview")),
            "watch_status": _get_watch_status_string(details.get("watch/providers", {})),
            "trailer_url": _parse_trailer(details.get("videos", {})),
            "poster_url": f"https://image.tmdb.org/t/p/w780{movie['poster_path']}"
        }
        enriched_releases.append(enriched_movie)
    
    return enriched_releases


async def _get_next_digital_releases_blocking(limit=5, search_days=90):
    """
    Находит ближайший день с цифровыми релизами и возвращает топ-N релизов за этот день.
    """
    start_date = datetime.now(timezone.utc) + timedelta(days=1)
    
    for i in range(search_days):
        target_date = start_date + timedelta(days=i)
        target_date_str = target_date.strftime('%Y-%m-%d')
        
        url = "https://api.themoviedb.org/3/discover/movie"
        params = {
            "api_key": TMDB_API_KEY,
            "language": "en-US",
            "sort_by": "popularity.desc",
            "include_adult": "false",
            "release_date.gte": target_date_str,
            "release_date.lte": target_date_str,
            "with_release_type": 4, # Digital Release
            "region": 'RU',
            "vote_count.gte": 10
        }
        
        # Сначала ищем в регионе RU
        try:
            r = requests.get(url, params=params, timeout=20)
            r.raise_for_status()
            potential_releases = [m for m in r.json().get("results", []) if m.get("poster_path")]
        except requests.exceptions.RequestException:
            potential_releases = []

        # Если в RU пусто, ищем в US
        if not potential_releases:
            params['region'] = 'US'
            try:
                r = requests.get(url, params=params, timeout=20)
                r.raise_for_status()
                potential_releases = [m for m in r.json().get("results", []) if m.get("poster_path")]
            except requests.exceptions.RequestException:
                potential_releases = []
        
        # Если нашли релизы за этот день, обрабатываем и возвращаем их
        if potential_releases:
            print(f"[INFO] Found next digital releases on {target_date_str}")
            top_releases = potential_releases[:limit]
            enriched_releases = []
            
            details_tasks = [asyncio.to_thread(_get_movie_details_blocking, movie['id']) for movie in top_releases]
            all_details = await asyncio.gather(*details_tasks)

            for idx, movie in enumerate(top_releases):
                details = all_details[idx]
                enriched_movie = {
                    **movie,
                    "overview": details.get("overview", movie.get("overview")),
                    "watch_status": _get_watch_status_string(details.get("watch/providers", {})),
                    "trailer_url": _parse_trailer(details.get("videos", {})),
                    "poster_url": f"https://image.tmdb.org/t/p/w780{movie['poster_path']}"
                }
                enriched_releases.append(enriched_movie)
            
            return enriched_releases, target_date

    return [], None


def _get_historical_premieres_blocking(year: int, month_day: str, limit=3):
    """
    Получает топ-N исторических премьер за определенную дату в указанном году.
    Используется для команды /year.
    """
    target_date = f"{year}-{month_day}"
    url = "https://api.themoviedb.org/3/discover/movie"
    params = {"api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc", "include_adult": "false", "primary_release_date.gte": target_date, "primary_release_date.lte": target_date}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return [m for m in r.json().get("results", []) if m.get("poster_path")][:limit]

def _get_random_movie_blocking(with_genres: str = None, without_genres: str = None, with_keywords: str = None, without_keywords: str = None):
    """Более гибкая функция для поиска случайного фильма."""
    discover_url = "https://api.themoviedb.org/3/discover/movie"
    params = {
        "api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc",
        "include_adult": "false", "vote_average.gte": 7.0, "vote_count.gte": 100,
        "primary_release_date.gte": "1985-01-01", "primary_release_date.lte": "2025-12-31",
        "page": 1
    }
    if with_genres: params["with_genres"] = with_genres
    if without_genres: params["without_genres"] = without_genres
    if with_keywords: params["with_keywords"] = with_keywords
    if without_keywords: params["without_keywords"] = without_keywords

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


async def _enrich_movie_data(movie: dict) -> dict:
    """
    Обогащает данные фильма, переводя описание, добавляя статус просмотра,
    URL трейлера и URL постера.
    Используется для /year и /random команд.
    """
    details = await asyncio.to_thread(_get_movie_details_blocking, movie['id'])
    overview_ru = await asyncio.to_thread(translate_text_blocking, movie.get("overview", ""))
    await asyncio.sleep(0.4) # Задержка для обхода лимитов переводчика
    return {
        **movie,
        "overview": overview_ru,
        "watch_status": _get_watch_status_string(details.get("watch/providers", {})),
        "trailer_url": _parse_trailer(details.get("videos", {})),
        "poster_url": f"https://image.tmdb.org/t/p/w780{movie['poster_path']}"
    }

# --- ФОРМАТИРОВАНИЕ И ПАГИНАЦИЯ ---

async def format_movie_message(movie_data: dict, genres_map: dict, title_prefix: str, is_paginated: bool = False, current_index: int = 0, total_count: int = 1, list_id: str = "", reroll_data: str = None):
    """Форматирует данные фильма в сообщение Telegram с поддержкой пагинации."""
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
    if is_paginated:
        nav_buttons = []
        if current_index > 0: nav_buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"page_{list_id}_{current_index - 1}"))
        nav_buttons.append(InlineKeyboardButton(f"[{current_index + 1}/{total_count}]", callback_data="noop"))
        if current_index < total_count - 1: nav_buttons.append(InlineKeyboardButton("➡️ Вперед", callback_data=f"page_{list_id}_{current_index + 1}"))
        keyboard.append(nav_buttons)
    
    action_buttons = []
    if reroll_data: action_buttons.append(InlineKeyboardButton("🔄 Повторить", callback_data=reroll_data))
    if trailer_url: action_buttons.append(InlineKeyboardButton("🎬 Смотреть трейлер", url=trailer_url))
    if action_buttons: keyboard.append(action_buttons)
    
    return text, poster_url, InlineKeyboardMarkup(keyboard) if keyboard else None

# --- КОМАНДЫ И ОБРАБОТЧИКИ ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает команду /start, подписывает чат на рассылку и выводит помощь."""
    chat_id = update.effective_chat.id
    chat_ids = context.bot_data.setdefault("chat_ids", set())
    msg = (
        "✅ Бот готов к работе!\n\n"
        "Я буду ежедневно в 14:00 по МСК присылать сюда анонсы *цифровых релизов*.\n\n"
        "**Доступные команды:**\n"
        "• `/releases` — показать *цифровые релизы* на сегодня.\n"
        "• `/next` — показать ближайшие цифровые релизы.\n"
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
    """Обрабатывает команду /help и выводит список команд."""
    await update.message.reply_text(
        "**Список команд:**\n\n"
        "• `/releases` — показать *цифровые релизы* на сегодня.\n"
        "• `/next` — показать ближайшие цифровые релизы.\n"
        "• `/random` — выбрать случайный фильм по жанру.\n"
        "• `/year <год>` — показать топ-3 фильма, вышедших в этот день в прошлом.\n"
        "• `/start` — подписаться на рассылку.\n"
        "• `/stop` — отписаться от рассылки.",
        parse_mode=constants.ParseMode.MARKDOWN
    )

async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает команду /stop и отписывает чат от рассылки."""
    chat_id = update.effective_chat.id
    if chat_id in context.bot_data.setdefault("chat_ids", set()):
        context.bot_data["chat_ids"].remove(chat_id)
        await update.message.reply_text("❌ Этот чат отписан от рассылки.")
    else:
        await update.message.reply_text("Этот чат и так не был подписан.")

async def premieres_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает команду /releases, показывая топ-5 сегодняшних цифровых релизов.
    """
    await update.message.reply_text("🔍 Ищу и обрабатываю сегодняшние *цифровые релизы*...")
    try:
        base_movies = await _get_todays_top_digital_releases_blocking(limit=5)
        if not base_movies:
            await update.message.reply_text("🎬 Значимых *цифровых релизов* на сегодня не найдено.")
            return
        
        # Переводим описания для каждого фильма
        enriched_movies = []
        for movie in base_movies:
            movie_with_translated_overview = {
                **movie,
                "overview": await asyncio.to_thread(translate_text_blocking, movie.get("overview", ""))
            }
            enriched_movies.append(movie_with_translated_overview)
            await asyncio.sleep(0.4) # Задержка для обхода лимитов переводчика

        list_id = str(uuid.uuid4())
        context.bot_data.setdefault('movie_lists', {})[list_id] = enriched_movies
        text, poster, markup = await format_movie_message(enriched_movies[0], context.bot_data.get('genres', {}), "🎬 Сегодня выходит в цифре:", is_paginated=True, current_index=0, total_count=len(enriched_movies), list_id=list_id)
        await update.message.reply_photo(photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
    except Exception as e:
        print(f"[ERROR] premieres_command failed: {e}")
        await update.message.reply_text("Произошла ошибка при получении данных о цифровых релизах.")

async def next_releases_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает команду /next, показывая ближайшие будущие цифровые релизы.
    """
    await update.message.reply_text("🔍 Ищу ближайшие *цифровые релизы*...")
    try:
        base_movies, release_date = await _get_next_digital_releases_blocking(limit=5)
        
        if not base_movies or not release_date:
            await update.message.reply_text("🎬 Не удалось найти цифровые релизы в ближайшие 3 месяца.")
            return
        
        enriched_movies = []
        for movie in base_movies:
            movie_with_translated_overview = {
                **movie,
                "overview": await asyncio.to_thread(translate_text_blocking, movie.get("overview", ""))
            }
            enriched_movies.append(movie_with_translated_overview)
            await asyncio.sleep(0.4)

        list_id = str(uuid.uuid4())
        context.bot_data.setdefault('movie_lists', {})[list_id] = enriched_movies
        
        release_date_formatted = release_date.strftime('%d.%m.%Y')
        title_prefix = f"🎬 Ближайший релиз ({release_date_formatted}):"

        text, poster, markup = await format_movie_message(
            enriched_movies[0], 
            context.bot_data.get('genres', {}), 
            title_prefix, 
            is_paginated=True, 
            current_index=0, 
            total_count=len(enriched_movies), 
            list_id=list_id
        )
        await update.message.reply_photo(photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
    except Exception as e:
        print(f"[ERROR] next_releases_command failed: {e}")
        await update.message.reply_text("Произошла ошибка при поиске ближайших релизов.")


async def year_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Обрабатывает команду /year, показывая топ-3 фильма, вышедших в этот день в прошлом.
    """
    if not context.args:
        await update.message.reply_text("Укажите год после команды, например: `/year 1999`", parse_mode=constants.ParseMode.MARKDOWN)
        return
    try:
        year = int(context.args[0])
        if not (1970 <= year <= datetime.now().year): raise ValueError("Год вне диапазона")
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

async def pagination_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает нажатия кнопок пагинации."""
    query = update.callback_query
    await query.answer()
    try:
        _, list_id, new_index_str = query.data.split("_")
        new_index = int(new_index_str)
    except (ValueError, IndexError): return
    
    movies = context.bot_data.get('movie_lists', {}).get(list_id)
    if not movies or not (0 <= new_index < len(movies)):
        await query.edit_message_text("Ошибка: список устарел или недоступен. Запросите заново.")
        return
    
    release_date_str = movies[new_index].get('release_date', '????')
    
    try:
        release_date_obj = datetime.strptime(release_date_str, '%Y-%m-%d').date()
        today_date_obj = datetime.now(timezone.utc).date()
    except ValueError:
        release_date_obj = None
        today_date_obj = datetime.now(timezone.utc).date()

    if release_date_obj == today_date_obj:
        title_prefix = "🎬 Сегодня выходит в цифре:"
    elif release_date_obj and release_date_obj > today_date_obj:
        release_date_formatted = release_date_obj.strftime('%d.%m.%Y')
        title_prefix = f"🎬 Ближайший релиз ({release_date_formatted}):"
    else: # Прошлые даты для команды /year
        year_str = release_date_str[:4]
        title_prefix = f"🎞️ Релиз {year_str} года:"

    text, poster, markup = await format_movie_message(
        movies[new_index], context.bot_data.get('genres', {}), title_prefix, is_paginated=True, current_index=new_index, total_count=len(movies), list_id=list_id
    )
    try:
        media = InputMediaPhoto(media=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
        await query.edit_message_media(media=media, reply_markup=markup)
    except Exception as e:
        print(f"[WARN] Failed to edit message media: {e}")

async def random_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает команду /random, предлагая выбрать жанр для случайного фильма."""
    genres_by_name = context.bot_data.get('genres_by_name', {})
    if not genres_by_name:
        await update.message.reply_text("Жанры еще не загружены, попробуйте через минуту.")
        return

    target_genres = ["Боевик", "Комедия", "Ужасы", "Фантастика", "Триллер", "Драма", "Приключения", "Фэнтези", "Детектив", "Криминал"]
    keyboard = []
    row = [InlineKeyboardButton("Мультфильмы", callback_data="random_cartoon"), InlineKeyboardButton("Аниме", callback_data="random_anime")]
    keyboard.append(row)
    row = []
    for genre_name in target_genres:
        genre_id = genres_by_name.get(genre_name.lower())
        if genre_id:
            row.append(InlineKeyboardButton(genre_name, callback_data=f"random_genre_{genre_id}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
    if row: keyboard.append(row)
    await update.message.reply_text("Выберите категорию или жанр:", reply_markup=InlineKeyboardMarkup(keyboard))

async def process_random_request(query: Update.callback_query, context: ContextTypes.DEFAULT_TYPE):
    """Общая логика для поиска случайного фильма и обновления сообщения."""
    data = query.data
    random_type = data.split("_")[1]
    
    genres_map = context.bot_data.get('genres', {})
    animation_id = next((gid for gid, name in genres_map.items() if name == "Мультфильм"), "16")
    anime_keyword_id = "210024"

    params, search_query_text = {}, ""

    if random_type == "genre":
        genre_id = data.split("_")[2]
        params = {"with_genres": genre_id, "without_genres": animation_id}
        search_query_text = f"'{genres_map.get(int(genre_id))}'"
    elif random_type == "cartoon":
        params = {"with_genres": animation_id, "without_keywords": anime_keyword_id}
        search_query_text = "'Мультфильм'"
    elif random_type == "anime":
        params = {"with_genres": animation_id, "with_keywords": anime_keyword_id}
        search_query_text = "'Аниме'"
    
    await query.edit_message_text(f"🔍 Подбираю случайный фильм в категории {search_query_text}...")
    try:
        random_movie = await asyncio.to_thread(_get_random_movie_blocking, **params)
        if not random_movie:
            await query.edit_message_text("🤷‍♂️ К сожалению, не удалось найти подходящий фильм. Попробуйте другой раз.")
            return

        enriched_movie = await _enrich_movie_data(random_movie)
        text, poster, markup = await format_movie_message(enriched_movie, genres_map, "🎲 Случайный фильм:", reroll_data=data)
        
        media = InputMediaPhoto(media=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
        await query.edit_message_media(media=media, reply_markup=markup)
    except Exception as e:
        print(f"[ERROR] process_random_request failed: {e}")
        await query.edit_message_text("Произошла ошибка при поиске фильма.")

async def random_genre_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает ПЕРВЫЙ выбор жанра для случайного фильма."""
    query = update.callback_query
    await query.answer()
    await query.delete_message()    
    temp_message = await context.bot.send_message(query.message.chat_id, "🔍 Подбираю случайный фильм...")
    
    class FakeQuery:
        def __init__(self, msg, data):
            self.message = msg
            self.data = data
        async def edit_message_text(self, text):
            return await self.message.edit_text(text)
        async def edit_message_media(self, media, reply_markup):
            return await self.message.edit_media(media=media, reply_markup=reply_markup)

    await process_random_request(FakeQuery(temp_message, query.data), context)

async def reroll_random_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обрабатывает кнопку 'Повторить' для случайного фильма."""
    query = update.callback_query
    await query.answer()
    await process_random_request(query, context)


async def daily_check_job(context: ContextTypes.DEFAULT_TYPE):
    """
    Ежедневная задача: отправляет топ-5 цифровых релизов в 14:00 МСК
    каждому подписанному чату одним сообщением с пагинацией.
    """
    print(f"[{datetime.now().isoformat()}] Running daily check job for digital releases")
    chat_ids = context.bot_data.get("chat_ids", set())
    if not chat_ids: return
    try:
        base_movies = await _get_todays_top_digital_releases_blocking(limit=5)
        if not base_movies:
            for chat_id in list(chat_ids):
                await context.bot.send_message(chat_id, "🎬 Сегодня значимых цифровых релизов не найдено.")
            return

        enriched_movies = []
        for movie in base_movies:
            movie_with_translated_overview = {
                **movie,
                "overview": await asyncio.to_thread(translate_text_blocking, movie.get("overview", ""))
            }
            enriched_movies.append(movie_with_translated_overview)
            await asyncio.sleep(0.4)

        for chat_id in list(chat_ids):
            print(f"Sending daily digital releases to {chat_id}")
            list_id = str(uuid.uuid4())
            context.bot_data.setdefault('movie_lists', {})[list_id] = enriched_movies
            text, poster, markup = await format_movie_message(enriched_movies[0], context.bot_data.get('genres', {}), "🎬 Сегодня выходит в цифре:", is_paginated=True, current_index=0, total_count=len(enriched_movies), list_id=list_id)
            await context.bot.send_photo(chat_id, photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
            await asyncio.sleep(1)
    except Exception as e:
        print(f"[ERROR] Daily job for digital releases failed: {e}")

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

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("releases", premieres_command))
    application.add_handler(CommandHandler("premieres", premieres_command))
    application.add_handler(CommandHandler("next", next_releases_command))
    application.add_handler(CommandHandler("stop", stop_command))
    application.add_handler(CommandHandler("year", year_command))
    application.add_handler(CommandHandler("random", random_command))

    # Регистрируем обработчики кнопок
    application.add_handler(CallbackQueryHandler(pagination_handler, pattern="^page_"))
    application.add_handler(CallbackQueryHandler(random_genre_handler, pattern="^random_"))
    application.add_handler(CallbackQueryHandler(reroll_random_handler, pattern="^reroll_"))
    application.add_handler(CallbackQueryHandler(lambda u, c: u.callback_query.answer(), pattern="^noop$"))
    
    tz = ZoneInfo("Europe/Moscow")
    scheduled_time = time(hour=14, minute=0, tzinfo=tz)
    application.job_queue.run_daily(daily_check_job, scheduled_time, name="daily_movie_check")

    print("[INFO] Starting bot...")
    application.run_polling()

if __name__ == "__main__":
    main()

