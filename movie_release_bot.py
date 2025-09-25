#!/usr/bin/env python3
"""
Movie release Telegram bot with all features including pagination and random movie selection.
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
    params = {"api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc", "include_adult": "false", "with_original_language": "en|es|fr|de|it", "primary_release_date.gte": today_str, "primary_release_date.lte": today_str}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return [m for m in r.json().get("results", []) if m.get("poster_path")][:limit]

def _get_historical_premieres_blocking(year: int, month_day: str, limit=3):
    target_date = f"{year}-{month_day}"
    url = "https://api.themoviedb.org/3/discover/movie"
    params = {"api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc", "include_adult": "false", "primary_release_date.gte": target_date, "primary_release_date.lte": target_date}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return [m for m in r.json().get("results", []) if m.get("poster_path")][:limit]

def _get_random_movie_blocking(genre_id: int):
    discover_url = "https://api.themoviedb.org/3/discover/movie"
    params = {"api_key": TMDB_API_KEY, "language": "en-US", "sort_by": "popularity.desc", "include_adult": "false", "with_genres": str(genre_id), "vote_average.gte": 7.0, "vote_count.gte": 100, "primary_release_date.gte": "1985-01-01", "primary_release_date.lte": "2025-12-31", "page": 1}
    r = requests.get(discover_url, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()
    total_pages = data.get("total_pages", 1)
    random_page = random.randint(1, min(total_pages, 500))
    params["page"] = random_page
    r = requests.get(discover_url, params=params, timeout=20)
    r.raise_for_status()
    results = [m for m in r.json().get("results", []) if m.get("poster_path")]
    return random.choice(results) if results else None

def _get_movie_details_blocking(movie_id: int):
    url = f"https://api.themoviedb.org/3/movie/{movie_id}"
    params = {"api_key": TMDB_API_KEY, "append_to_response": "videos,watch/providers"}
    r = requests.get(url, params=params, timeout=20)
    r.raise_for_status()
    return r.json()

def _parse_watch_providers(providers_data: dict) -> str:
    results = providers_data.get("results", {}).get("RU", providers_data.get("results", {}).get("US"))
    if not results: return "🍿 Только в кинотеатрах"
    flatrate, buy = results.get("flatrate"), results.get("buy")
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

async def _enrich_movie_data(movie: dict) -> dict:
    details = await asyncio.to_thread(_get_movie_details_blocking, movie['id'])
    overview_ru = await asyncio.to_thread(translate_text_blocking, movie.get("overview", ""))
    await asyncio.sleep(0.4)
    return {**movie, "overview": overview_ru, "watch_status": _parse_watch_providers(details.get("watch/providers", {})), "trailer_url": _parse_trailer(details.get("videos", {})), "poster_url": f"https://image.tmdb.org/t/p/w780{movie['poster_path']}"}

# --- ФОРМАТИРОВАНИЕ И ПАГИНАЦИЯ ---

async def format_movie_for_pagination(movie_data: dict, genres_map: dict, current_index: int, total_count: int, list_id: str, title_prefix: str):
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
        text, poster, markup = await format_movie_for_pagination(enriched_movies[0], context.bot_data.get('genres', {}), 0, len(enriched_movies), list_id, "🎬 Сегодня выходит:")
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
        text, poster, markup = await format_movie_for_pagination(enriched_movies[0], context.bot_data.get('genres', {}), 0, len(enriched_movies), list_id, f"🎞️ Релиз {year} года:")
        await update.message.reply_photo(photo=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN, reply_markup=markup)
    except Exception as e:
        print(f"[ERROR] year_command failed: {e}")
        await update.message.reply_text("Произошла ошибка при поиске по году.")

async def random_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправляет кнопки для выбора жанра случайного фильма."""
    genres = context.bot_data.get('genres', {})
    if not genres:
        await update.message.reply_text("Жанры еще не загружены, попробуйте через минуту.")
        return

    # Исправлено: Динамически берем первые 6 жанров из загруженного списка
    keyboard = []
    row = []
    for genre_id, genre_name in list(genres.items())[:6]:
        row.append(InlineKeyboardButton(genre_name, callback_data=f"random_{genre_id}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)

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
        text, poster, markup = await format_movie_for_pagination(enriched_movie, context.bot_data.get('genres', {}), 0, 1, "random", "🎲 Случайный фильм:")
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
    # Проверяем, есть ли дата релиза и отличается ли год от текущего
    release_year = movies[new_index].get('release_date', '????')[:4]
    if release_year.isdigit() and int(release_year) < datetime.now().year:
        title_prefix = f"🎞️ Релиз {release_year} года:"

    text, poster, markup = await format_movie_for_pagination(
        movies[new_index], context.bot_data.get('genres', {}), new_index, len(movies), list_id, title_prefix
    )
    try:
        media = InputMediaPhoto(media=poster, caption=text, parse_mode=constants.ParseMode.MARKDOWN)
        await query.edit_message_media(media=media, reply_markup=markup)
    except Exception as e:
        print(f"[WARN] Failed to edit message media: {e}")

async def daily_check_job(context: ContextTypes.DEFAULT_TYPE):
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
                # В ежедневной рассылке пагинация не нужна, отправляем как отдельные карточки
                text, poster, markup = await format_movie_for_pagination(movie, context.bot_data.get('genres', {}), 0, 1, "daily", "🎬 Сегодня выходит:")
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
