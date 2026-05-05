import asyncio
import io
import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from aiohttp import web
from aiogram import Bot, Dispatcher, types, F
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import Command
from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, InputFile, InputMediaPhoto, InputMediaVideo, WebAppInfo

# Настройка логирования
logging.basicConfig(level=logging.INFO)

# --- КОНФИГУРАЦИЯ ---
API_TOKEN = '8782020081:AAEKxJY3oV4FCfArMhBiD3V4NFgPKhRaoF4'
MODERATOR_IDS = [1370071250, 1967790848]  # Оба модератора получают запросы
NEWS_CHANNEL_ID = -1003827545466  # Канал для публикации
DB_NAME = 'news_moderation.db'
WEB_APP_URL = 'http://localhost:8080'
WEB_APP_HOST = '0.0.0.0'
WEB_APP_PORT = 8080
WEB_APP_DIR = Path(__file__).parent / 'webapp'

# Инициализация бота
bot = Bot(token=API_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# --- БАЗА ДАННЫХ ---
def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS news (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                username TEXT,
                text TEXT,
                photo_ids TEXT,
                video_ids TEXT,
                latitude REAL,
                longitude REAL,
                status TEXT,
                timestamp DATETIME
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_requests (
                user_id INTEGER PRIMARY KEY,
                request_count INTEGER,
                last_request_time DATETIME
            )
        ''')
        conn.commit()

async def send_to_moderators(news_id: int, user_id: int, username: str, text: str, photo_ids: list, video_ids: list, lat: float | None, lon: float | None) -> tuple[list[str], list[str]]:
    """Отправляет новость модераторам с кнопками для модерации"""
    logging.info('send_to_moderators: news_id=%d, photos=%d, videos=%d, moderators=%d', 
                 news_id, len(photo_ids or []), len(video_ids or []), len(MODERATOR_IDS))
    
    if lat and lon:
        google_map_url = f"https://www.google.com/maps?q={lat},{lon}"
        apple_map_url = f"https://maps.apple.com/?q={lat},{lon}"
        map_links = f"\n\n<a href=\"{google_map_url}\">🗺️ Google Maps</a> | <a href=\"{apple_map_url}\">🍎 Apple Maps</a>"
        admin_text = f"📨 Новая новость\n👤 От: @{username}\nID: {user_id}{map_links}\n\n📝 {text}"
    else:
        admin_text = f"📨 Новая новость\n👤 От: @{username}\nID: {user_id}\n\n📝 {text}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='✅ Опубликовать', callback_data=f'approve_{news_id}')],
        [InlineKeyboardButton(text='✏️ Редактировать', callback_data=f'edit_{news_id}')],
        [InlineKeyboardButton(text='❌ Отклонить', callback_data=f'reject_{news_id}')]
    ])

    # photo_ids и video_ids уже содержат file_ids из Telegram
    for mod_id in MODERATOR_IDS:
        try:
            logging.info('send_to_moderators: Отправляю модератору %d', mod_id)
            if photo_ids:
                # Отправляем фото группой
                photo_media = [InputMediaPhoto(media=fid) for fid in photo_ids]
                photo_media[0].caption = admin_text
                photo_media[0].parse_mode = 'HTML'
                await bot.send_media_group(mod_id, photo_media)
                logging.info('send_to_moderators: Отправлены фото модератору %d', mod_id)
                await bot.send_message(mod_id, 'Кнопки для модерации:', reply_markup=kb)
            
            if video_ids:
                # Отправляем видео по одному
                for i, vid_id in enumerate(video_ids):
                    caption = admin_text if not photo_ids and i == 0 else None
                    await bot.send_video(mod_id, vid_id, caption=caption, 
                                        parse_mode='HTML' if caption else None)
                logging.info('send_to_moderators: Отправлены видео модератору %d', mod_id)
                if not photo_ids:
                    await bot.send_message(mod_id, 'Кнопки для модерации:', reply_markup=kb)
            
            if not photo_ids and not video_ids:
                await bot.send_message(mod_id, admin_text, reply_markup=kb, parse_mode='HTML')
                logging.info('send_to_moderators: Отправлено текстовое сообщение модератору %d', mod_id)
        except Exception as e:
            logging.warning('send_to_moderators: Ошибка отправки модератору %d: %s', mod_id, e)
            try:
                await bot.send_message(mod_id, f"Ошибка отправки: {e}")
            except Exception:
                pass

    return photo_ids, video_ids


@web.middleware
async def error_middleware(request, handler):
    try:
        # Обработка CORS OPTIONS запросов
        if request.method == 'OPTIONS':
            return web.Response(status=200, headers={
                'Access-Control-Allow-Origin': '*',
                'Access-Control-Allow-Methods': 'GET, POST, OPTIONS',
                'Access-Control-Allow-Headers': 'Content-Type',
            })
        
        response = await handler(request)
        # Добавляем CORS заголовки ко всем ответам
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return response
    except web.HTTPException as e:
        logging.warning('HTTP Exception: %s %s', e.status, e)
        raise
    except Exception as exc:
        logging.exception('Ошибка в обработчике webapp: %s', exc)
        return web.json_response({'ok': False, 'error': 'Внутренняя ошибка сервера'}, status=500)

async def webapp_index(request: web.Request) -> web.Response:
    return web.FileResponse(WEB_APP_DIR / 'index.html')

async def webapp_static(request: web.Request) -> web.Response:
    filename = request.match_info['filename']
    return web.FileResponse(WEB_APP_DIR / filename)

async def webapp_upload(request: web.Request) -> web.Response:
    """Обработчик загрузки файлов - загружает в Telegram и возвращает file_id"""
    logging.info('webapp_upload: Получен запрос %s %s', request.method, request.path)
    try:
        form = await request.post()
        file_field = form.get('file')
        
        if not file_field:
            logging.warning('webapp_upload: Файл не передан')
            return web.json_response({'ok': False, 'error': 'Файл не передан'}, status=400)
        
        content = file_field.file.read()
        if not content:
            logging.warning('webapp_upload: Файл пуст')
            return web.json_response({'ok': False, 'error': 'Файл пуст'}, status=400)
        
        content_type = getattr(file_field, 'content_type', '')
        filename = getattr(file_field, 'filename', 'file')
        
        logging.info('webapp_upload: Получен файл %s, size=%d, type=%s', filename, len(content), content_type)
        
        # Определяем тип файла
        is_photo = content_type.startswith('image/')
        is_video = content_type.startswith('video/')
        
        # Fallback на расширение файла
        if not (is_photo or is_video):
            ext = filename.split('.')[-1].lower() if '.' in filename else ''
            is_photo = ext in ('jpg', 'jpeg', 'png', 'webp', 'gif', 'heic')
            is_video = ext in ('mp4', 'mov', 'webm', 'avi', 'mkv')
        
        if not (is_photo or is_video):
            logging.warning('webapp_upload: Неподдерживаемый тип файла %s', filename)
            return web.json_response({'ok': False, 'error': 'Поддерживаются только фото и видео'}, status=400)
        
        file_obj = InputFile(io.BytesIO(content), filename=filename)
        
        try:
            if is_photo:
                logging.info('webapp_upload: Отправляю фото в Telegram')
                msg = await bot.send_photo(MODERATOR_IDS[0], file_obj)
                file_id = msg.photo[-1].file_id
            else:  # is_video
                logging.info('webapp_upload: Отправляю видео в Telegram')
                msg = await bot.send_video(MODERATOR_IDS[0], file_obj)
                file_id = msg.video.file_id
            
            logging.info('webapp_upload: Файл загружен успешно: file_id=%s, type=%s, filename=%s', 
                        file_id, 'photo' if is_photo else 'video', filename)
            
            return web.json_response({'ok': True, 'file_id': file_id, 'id': file_id})
        except Exception as e:
            logging.error('webapp_upload: Ошибка загрузки в Telegram: %s', e)
            return web.json_response({'ok': False, 'error': f'Ошибка загрузки: {str(e)}'}, status=500)
    
    except Exception as e:
        logging.error('webapp_upload: Внутренняя ошибка: %s', e, exc_info=True)
        return web.json_response({'ok': False, 'error': 'Внутренняя ошибка сервера'}, status=500)

async def webapp_submit(request: web.Request) -> web.Response:
    """Обработчик отправки новости из веб-приложения"""
    try:
        form = await request.post()
        user_id = form.get('user_id')
        username = form.get('username') or 'unknown'
        text = form.get('text', '').strip()
        lat = form.get('lat')
        lon = form.get('lon')
        
        logging.info('webapp_submit: Получена заявка от user_id=%s, username=%s', user_id, username)

        if not user_id:
            logging.warning('webapp_submit: user_id не указан')
            return web.json_response({'ok': False, 'error': 'Не удалось определить Telegram ID.'}, status=400)

        try:
            user_id_int = int(user_id)
        except ValueError:
            logging.warning('webapp_submit: Неверный user_id: %s', user_id)
            return web.json_response({'ok': False, 'error': 'Неверный Telegram ID.'}, status=400)

        # Получаем file_ids из формы
        photo_ids = form.getall('photo_ids')
        video_ids = form.getall('video_ids')
        
        # Фильтруем пустые значения
        photo_ids = [fid for fid in photo_ids if fid]
        video_ids = [fid for fid in video_ids if fid]

        lat_value = float(lat) if lat and lat.strip() else None
        lon_value = float(lon) if lon and lon.strip() else None

        if not (text or photo_ids or video_ids):
            logging.warning('webapp_submit: Нет контента')
            return web.json_response({'ok': False, 'error': 'Добавьте текст или медиа.'}, status=400)

        logging.info('webapp_submit: Контент - text=%d chars, photos=%d, videos=%d, lat=%s, lon=%s', 
                     len(text), len(photo_ids), len(video_ids), lat_value, lon_value)

        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute('''INSERT INTO news (user_id, username, text, photo_ids, video_ids, latitude, longitude, status, timestamp)
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                           (user_id_int, username, text, 
                            ','.join(photo_ids) if photo_ids else None,
                            ','.join(video_ids) if video_ids else None,
                            lat_value, lon_value, 'pending', datetime.now()))
            news_id = cursor.lastrowid
            conn.commit()
            logging.info('webapp_submit: Сохранено в БД с news_id=%d', news_id)
        
        # Отправляем модераторам
        await send_to_moderators(news_id, user_id_int, username, text, photo_ids, video_ids, lat_value, lon_value)
        logging.info('webapp_submit: Отправлено модераторам')

        try:
            await bot.send_message(user_id_int, '✅ Ваша новость принята и отправлена на модерацию.')
            logging.info('webapp_submit: Уведомление отправлено пользователю')
        except Exception as e:
            logging.error('webapp_submit: Ошибка отправки уведомления пользователю: %s', e)
        
        return web.json_response({'ok': True, 'message': 'Новость отправлена на модерацию.'})
    
    except Exception as e:
        logging.error('webapp_submit: Критическая ошибка: %s', e, exc_info=True)
        return web.json_response({'ok': False, 'error': 'Внутренняя ошибка сервера'}, status=500)


async def create_webapp_server() -> web.Application:
    app = web.Application(middlewares=[error_middleware])
    app.router.add_get('/', webapp_index)
    app.router.add_get('/static/{filename}', webapp_static)
    app.router.add_post('/submit', webapp_submit)
    app.router.add_post('/upload', webapp_upload)
    return app

# --- СОСТОЯНИЯ ---
class NewsSubmission(StatesGroup):
    waiting_for_content = State()  # Ожидание контента

# --- ХЕНДЛЕРЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    kb = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text='📩 Отправить новость')]],
        resize_keyboard=True
    )
    await message.answer(
        "Привет! Отправляйте новости с текстом, фото, видео или геопозицией.\n"
        "Нажмите кнопку ниже, чтобы начать.",
        reply_markup=kb
    )

    inline_kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='🌐 Открыть мини-приложение', web_app=WebAppInfo(url=WEB_APP_URL))]
    ])
    await message.answer("Или откройте мини-приложение Telegram:", reply_markup=inline_kb)

@dp.message(F.text == '📩 Отправить новость')
async def start_news_submission(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    
    # Проверка лимитов (кроме модераторов)
    if user_id not in MODERATOR_IDS:
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT request_count, last_request_time FROM user_requests WHERE user_id = ?', (user_id,))
            result = cursor.fetchone()
            now = datetime.now()

            if result:
                count, last_time_str = result
                last_time = datetime.strptime(last_time_str.split('.')[0], '%Y-%m-%d %H:%M:%S')
                if now - last_time < timedelta(minutes=10) and count >= 3:
                    await message.answer("⏳ Лимит: 3 заявки за 10 минут. Попробуйте позже.")
                    return
                elif now - last_time >= timedelta(minutes=10):
                    cursor.execute('UPDATE user_requests SET request_count = 1, last_request_time = ? WHERE user_id = ?', (now, user_id))
                else:
                    cursor.execute('UPDATE user_requests SET request_count = request_count + 1, last_request_time = ? WHERE user_id = ?', (now, user_id))
            else:
                cursor.execute('INSERT INTO user_requests VALUES (?, ?, ?)', (user_id, 1, now))
            conn.commit()

    # Клавиатура для отправки новости
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text='✅ Подтвердить и отправить')],
            [KeyboardButton(text='📍 Моя геолокация', request_location=True)],
            [KeyboardButton(text='❌ Отмена')]
        ],
        resize_keyboard=True
    )
    
    # Инициализируем состояние
    await state.set_data({
        'user_id': user_id,
        'username': message.from_user.username or message.from_user.full_name or "unknown",
        'text': '',
        'photo_ids': [],
        'video_ids': [],
        'lat': None,
        'lon': None
    })
    
    await message.answer(
        "📝 Отправьте контент новости:\n"
        "- Текст\n"
        "- Фото (можно несколько)\n"
        "- Видео\n"
        "- Геопозицию\n\n"
        "Когда всё добавите, нажмите '✅ Подтвердить и отправить'",
        reply_markup=kb
    )
    await state.set_state(NewsSubmission.waiting_for_content)

@dp.message(NewsSubmission.waiting_for_content)
async def process_content(message: types.Message, state: FSMContext):
    data = await state.get_data()
    
    # Обработка отмены
    if message.text == '❌ Отмена':
        await message.answer("❌ Отменено.")
        # Возвращаем обычную клавиатуру
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text='📩 Отправить новость')]],
            resize_keyboard=True
        )
        await message.answer("Нажмите кнопку, чтобы начать заново.", reply_markup=kb)
        await state.clear()
        return
    
    # Обработка подтверждения
    if message.text == '✅ Подтвердить и отправить':
        # Проверяем, есть ли хоть что-то
        if not (data.get('text') or data.get('photo_ids') or data.get('video_ids') or data.get('lat')):
            await message.answer("❌ Добавьте хотя бы текст, фото, видео или геопозицию.")
            return
        
        user_id = data['user_id']
        username = data['username']
        text = data['text']
        photo_ids = data.get('photo_ids', [])
        video_ids = data.get('video_ids', [])
        lat = data.get('lat')
        lon = data.get('lon')
        
        # Сохраняем в БД
        with sqlite3.connect(DB_NAME) as conn:
            cursor = conn.cursor()
            cursor.execute('''INSERT INTO news (user_id, username, text, photo_ids, video_ids, latitude, longitude, status, timestamp) 
                              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                           (user_id, username, text, ','.join(photo_ids) if photo_ids else None, 
                            ','.join(video_ids) if video_ids else None, lat, lon, 'pending', datetime.now()))
            news_id = cursor.lastrowid
            conn.commit()

        # Формируем сообщение для админа
        media_count = len(photo_ids) + len(video_ids)
        media_info = f"\n📎 Вложений: {media_count} (фото: {len(photo_ids)}, видео: {len(video_ids)})"
        
        # Ссылки на карты для админа в HTML формате
        if lat and lon:
            google_map_url = f"https://www.google.com/maps?q={lat},{lon}"
            apple_map_url = f"https://maps.apple.com/?q={lat},{lon}"
            map_links = f"\n\n<a href=\"{google_map_url}\">🗺️ Google Maps</a> | <a href=\"{apple_map_url}\">🍎 Apple Maps</a>"
            admin_text = f"📨 Новая новость\n👤 От: @{username}\nID: {user_id}{media_info}{map_links}\n\n📝 {text}"
        else:
            admin_text = f"📨 Новая новость\n👤 От: @{username}\nID: {user_id}{media_info}\n\n📝 {text}"
        
        # Кнопки для админа (без кнопок карт)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text='✅ Опубликовать', callback_data=f'approve_{news_id}')],
            [InlineKeyboardButton(text='✏️ Редактировать', callback_data=f'edit_{news_id}')],
            [InlineKeyboardButton(text='❌ Отклонить', callback_data=f'reject_{news_id}')]
        ])

        # Отправляем контент обоим модераторам
        for mod_id in MODERATOR_IDS:
            try:
                if photo_ids:
                    # Отправляем все фото альбомом
                    media = [types.InputMediaPhoto(media=pid, parse_mode='HTML') for pid in photo_ids]
                    if text:
                        media[0].caption = admin_text
                    await bot.send_media_group(mod_id, media)
                    await bot.send_message(mod_id, "Кнопки:", reply_markup=kb)
                elif video_ids:
                    # Отправляем видео
                    for vid in video_ids:
                        await bot.send_video(mod_id, vid, caption=admin_text if vid == video_ids[0] else None, reply_markup=kb if vid == video_ids[-1] else None, parse_mode='HTML')
                else:
                    await bot.send_message(mod_id, admin_text, reply_markup=kb, parse_mode='HTML')
            except Exception as e:
                await bot.send_message(mod_id, f"{admin_text}\n\nОшибка отправки медиа: {e}", reply_markup=kb, parse_mode='HTML')

        await message.answer("✅ Новость отправлена на модерацию!")
        
        # Возвращаем обычную клавиатуру
        kb = ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text='📩 Отправить новость')]],
            resize_keyboard=True
        )
        await message.answer("Нажмите кнопку, чтобы отправить ещё новость.", reply_markup=kb)
        await state.clear()
        return

    # Обработка геолокации
    if message.location:
        lat = message.location.latitude
        lon = message.location.longitude
        
        new_photo_ids = list(data.get('photo_ids', []))
        new_video_ids = list(data.get('video_ids', []))
        current_text = data.get('text', '')
        
        await state.update_data(
            text=current_text,
            photo_ids=new_photo_ids,
            video_ids=new_video_ids,
            lat=lat,
            lon=lon
        )
        
        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text='✅ Подтвердить и отправить')],
                [KeyboardButton(text='📍 Моя геолокация', request_location=True)],
                [KeyboardButton(text='❌ Отмена')]
            ],
            resize_keyboard=True
        )
        
        await message.answer(
            f"✅ Геолокация добавлена!\n\n📍 Координаты: {lat}, {lon}\n\n"
            "Продолжайте добавлять или нажмите '✅ Подтвердить и отправить'",
            reply_markup=kb
        )
        return

    # Накопление контента
    new_photo_ids = list(data.get('photo_ids', []))
    new_video_ids = list(data.get('video_ids', []))
    current_text = data.get('text', '')
    
    # Добавляем текст
    if message.text or message.caption:
        current_text = message.text or message.caption
    
    # Добавляем фото (только самое большое качество, без дубликатов)
    if message.photo:
        photo_id = message.photo[-1].file_id
        if photo_id not in new_photo_ids:
            new_photo_ids.append(photo_id)
    
    # Добавляем видео
    if message.video:
        new_video_ids.append(message.video.file_id)
    
    # Добавляем геопозицию
    lat = data.get('lat')
    lon = data.get('lon')
    if message.location:
        lat = message.location.latitude
        lon = message.location.longitude
    
    # Ограничение: макс 10 фото и 3 видео
    if len(new_photo_ids) > 10:
        await message.answer("⚠️ Максимум 10 фото. Остальные не будут сохранены.")
        new_photo_ids = new_photo_ids[:10]
    
    if len(new_video_ids) > 3:
        await message.answer("⚠️ Максимум 3 видео. Остальные не будут сохранены.")
        new_video_ids = new_video_ids[:3]
    
    # Сохраняем обновленные данные
    await state.update_data(
        text=current_text,
        photo_ids=new_photo_ids,
        video_ids=new_video_ids,
        lat=lat,
        lon=lon
    )
    
    # Показываем текущий статус
    status_parts = []
    if current_text:
        status_parts.append(f"📝 Текст: {len(current_text)} симв.")
    if new_photo_ids:
        status_parts.append(f"🖼️ Фото: {len(new_photo_ids)}")
    if new_video_ids:
        status_parts.append(f"🎥 Видео: {len(new_video_ids)}")
    if lat and lon:
        status_parts.append(f"📍 Геопозиция: есть")
    
    status_text = " | ".join(status_parts) if status_parts else "Пока ничего нет"
    
    # Обновляем клавиатуру с кнопками
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text='✅ Подтвердить и отправить')],
            [KeyboardButton(text='❌ Отмена')]
        ],
        resize_keyboard=True
    )
    
    await message.answer(
        f"✅ Добавлено!\n\n{status_text}\n\n"
        "Продолжайте добавлять или нажмите '✅ Подтвердить и отправить'",
        reply_markup=kb
    )

@dp.callback_query(F.data.startswith('approve_'))
async def approve_news(callback: types.CallbackQuery):
    news_id = callback.data.split('_')[1]
    moderator_id = callback.from_user.id
    
    # Проверяем, что модератор имеет право голосовать
    if moderator_id not in MODERATOR_IDS:
        await callback.answer("Вы не модератор", show_alert=True)
        return
    
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM news WHERE id = ?', (news_id,))
        n = cursor.fetchone()
    
    if not n:
        await callback.answer("❌ Новость не найдена", show_alert=True)
        return
    
    # Проверяем, не опубликована ли уже новость
    if n[8] == 'approved':
        await callback.answer("⚠️ Новость уже опубликована", show_alert=True)
        return
    
    # n[1]=user_id, n[2]=username, n[3]=text, n[4]=photo_ids, n[5]=video_ids, n[6]=lat, n[7]=lon
    # Формат: картинка – Согласно пользователю @username, текст
    source_text = f"Согласно пользователю @{n[2]}"
    final_text = f"{n[3]}"
    
    try:
        photo_ids = n[4].split(',') if n[4] else []
        video_ids = n[5].split(',') if n[5] else []
        
        # Ссылки на карты в HTML формате
        if n[6] and n[7]:
            google_map_url = f"https://www.google.com/maps?q={n[6]},{n[7]}"
            apple_map_url = f"https://maps.apple.com/?q={n[6]},{n[7]}"
            map_links = f"\n\n<a href=\"{google_map_url}\">🗺️ Google Maps</a> | <a href=\"{apple_map_url}\">🍎 Apple Maps</a>"
            final_text_with_maps = final_text + map_links
        else:
            final_text_with_maps = final_text
        
        if photo_ids:
            # Отправляем все фото альбомом с источником в подписи
            media = [types.InputMediaPhoto(media=pid, parse_mode='HTML') for pid in photo_ids if pid]
            if final_text_with_maps:
                media[0].caption = f"{source_text}\n\n{final_text_with_maps}"
            await bot.send_media_group(NEWS_CHANNEL_ID, media)
        elif video_ids:
            for i, vid in enumerate(video_ids):
                if vid:
                    await bot.send_video(NEWS_CHANNEL_ID, vid, caption=f"{source_text}\n\n{final_text_with_maps}" if i == 0 else None, parse_mode='HTML')
        else:
            # Только текст - добавляем источник в начало
            await bot.send_message(NEWS_CHANNEL_ID, f"{source_text}\n\n{final_text_with_maps}", parse_mode='HTML')
        
        # Уведомляем пользователя
        await bot.send_message(n[1], "🎉 Ваша новость опубликована!")
        
        # Обновляем статус
        with sqlite3.connect(DB_NAME) as conn:
            conn.execute('UPDATE news SET status = "approved" WHERE id = ?', (news_id,))
        
        # Уведомляем другого модератора, что новость опубликована
        for mod_id in MODERATOR_IDS:
            if mod_id != moderator_id:
                try:
                    await bot.send_message(mod_id, f"✅ Новость #{news_id} опубликована другим модератором!")
                except:
                    pass
        
        await callback.message.delete()
        await callback.answer("✅ Опубликовано!")
    except Exception as e:
        for mod_id in MODERATOR_IDS:
            try:
                await bot.send_message(mod_id, f"Ошибка публикации: {e}")
            except:
                pass
        await callback.answer("❌ Ошибка публикации", show_alert=True)

@dp.callback_query(F.data.startswith('reject_'))
async def reject_news(callback: types.CallbackQuery):
    news_id = callback.data.split('_')[1]
    moderator_id = callback.from_user.id
    
    # Проверяем, что модератор имеет право голосовать
    if moderator_id not in MODERATOR_IDS:
        await callback.answer("Вы не модератор", show_alert=True)
        return
    
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT user_id FROM news WHERE id = ?', (news_id,))
        result = cursor.fetchone()
        if result:
            uid = result[0]
            conn.execute('UPDATE news SET status = "rejected" WHERE id = ?', (news_id,))
            await bot.send_message(uid, "❌ Ваша новость отклонена.")
    
    # Уведомляем другого модератора
    for mod_id in MODERATOR_IDS:
        if mod_id != moderator_id:
            try:
                await bot.send_message(mod_id, f"❌ Новость #{news_id} отклонена другим модератором!")
            except:
                pass
    
    await callback.message.delete()
    await callback.answer("❌ Отклонено")

# --- РЕДАКТИРОВАНИЕ НОВОСТИ ---

class AdminEdit(StatesGroup):
    waiting_for_new_text = State()

@dp.callback_query(F.data.startswith('edit_'))
async def edit_news_start(callback: types.CallbackQuery, state: FSMContext):
    news_id = callback.data.split('_')[1]
    moderator_id = callback.from_user.id
    
    # Проверяем, что модератор имеет право редактировать
    if moderator_id not in MODERATOR_IDS:
        await callback.answer("Вы не модератор", show_alert=True)
        return
    
    await state.update_data(edit_id=news_id, msg_id=callback.message.message_id, moderator_id=moderator_id)
    await callback.message.answer("✏️ Введите новый текст новости:")
    await state.set_state(AdminEdit.waiting_for_new_text)
    await callback.answer()

@dp.message(AdminEdit.waiting_for_new_text)
async def edit_news_finish(message: types.Message, state: FSMContext):
    data = await state.get_data()
    new_text = message.text
    moderator_id = data.get('moderator_id')
    
    with sqlite3.connect(DB_NAME) as conn:
        cursor = conn.cursor()
        cursor.execute('UPDATE news SET text = ? WHERE id = ?', (new_text, data['edit_id']))
        cursor.execute('SELECT * FROM news WHERE id = ?', (data['edit_id'],))
        n = cursor.fetchone()
    
    # Обновляем сообщение у модератора со ссылками на карты в HTML формате
    if n[6] and n[7]:
        google_map_url = f"https://www.google.com/maps?q={n[6]},{n[7]}"
        apple_map_url = f"https://maps.apple.com/?q={n[6]},{n[7]}"
        map_links = f"\n\n<a href=\"{google_map_url}\">🗺️ Google Maps</a> | <a href=\"{apple_map_url}\">🍎 Apple Maps</a>"
        admin_text = f"📨 Новость (отредактировано)\nID: {n[0]}\n👤 От: @{n[2]}{map_links}\n\n📝 {n[3]}"
    else:
        admin_text = f"📨 Новость (отредактировано)\nID: {n[0]}\n👤 От: @{n[2]}\n\n📝 {n[3]}"
    
    # Кнопки для модератора
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text='✅ Опубликовать', callback_data=f'approve_{n[0]}')],
        [InlineKeyboardButton(text='✏️ Редактировать', callback_data=f'edit_{n[0]}')],
        [InlineKeyboardButton(text='❌ Отклонить', callback_data=f'reject_{n[0]}')]
    ])
    
    try:
        await bot.edit_message_caption(chat_id=moderator_id, message_id=data['msg_id'], caption=admin_text, reply_markup=kb, parse_mode='HTML')
    except:
        try:
            await bot.edit_message_text(chat_id=moderator_id, message_id=data['msg_id'], text=admin_text, reply_markup=kb, parse_mode='HTML')
        except:
            pass
    
    # Уведомляем другого модератора
    for mod_id in MODERATOR_IDS:
        if mod_id != moderator_id:
            try:
                await bot.send_message(mod_id, f"✏️ Новость #{n[0]} отредактирована другим модератором!")
            except:
                pass
    
    await message.answer("✅ Текст новости обновлён!")
    await state.clear()

# --- ЗАПУСК ---
async def main():
    init_db()
    webapp = await create_webapp_server()
    runner = web.AppRunner(webapp)
    await runner.setup()
    site = web.TCPSite(runner, WEB_APP_HOST, WEB_APP_PORT)
    await site.start()
    logging.info(f"Веб-приложение запущено: {WEB_APP_URL}")
    logging.info("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass