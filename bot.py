import asyncio
import logging
import asyncpg
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiohttp import web, ClientSession
from rollypay import RollyPayClient
import os
import hmac
import hashlib
import json
import time

# --- НАСТРОЙКИ ---
BOT_TOKEN = "9037852041:AAGlmd1cTt0Mbc9CM6t-kYJimE86h2cRiEo"
ROLLYPAY_API_KEY = "aMkEd8uF3EY1PM5Zzki5oFfPVx7pnRUiuxYUJQLS5ts"
TERMINAL_ID = "02485c32-7013-40f6-9c1f-00eb12f51d31"
SIGNING_SECRET = "SQ7ZPKKj0l-_yFL_KsARgUj6FjuMJ51v4KFgbr53MAk"
WEBHOOK_PORT = int(os.environ.get("PORT", 8080))

DATABASE_URL = "postgresql://postgres.qrfqdihhoqvlaqkuyols:Koloskov2006@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"

# --- ВАША РЕФЕРАЛЬНАЯ ССЫЛКА GO6 ---
GO6_REF_LINK = "https://t.me/Go6online_bot?start=verify_XE732NNY" 

MIN_TRC20_USDT = 10.0
MIN_BEP20_USDT = 50.0
MARKUP_RUB = 3.0
ADMIN_FEE_PER_USDT = 1.0
ADMIN_ID = 7653934214

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
rolly_client = RollyPayClient(api_key=ROLLYPAY_API_KEY)

# --- ФУНКЦИЯ РАСЧЕТА КОМИССИИ СЕТИ ---
def calculate_network_fee(network: str, amount_usdt: float) -> float:
    if network == 'TRC20': return 3.0
    elif network == 'BEP20': return (amount_usdt * 0.01) + 2.0
    return 0.0

# --- ДИНАМИЧЕСКИЙ КУРС ---
_cached_rate = 0.0
_last_rate_update = 0

async def get_usdt_rate():
    global _cached_rate, _last_rate_update
    if time.time() - _last_rate_update < 600 and _cached_rate > 0:
        return _cached_rate

    try:
        url = "https://api.rapira.net/open/market/rates"
        async with ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()
                rate_data = next((item for item in data.get('data', []) if item.get('symbol') == 'USDT/RUB'), None)
                if rate_data:
                    base_rate = float(rate_data.get('close') or rate_data.get('askPrice') or 0)
                    if base_rate > 0:
                        _cached_rate = base_rate + MARKUP_RUB
                        _last_rate_update = time.time()
                        return _cached_rate
    except Exception as e:
        logging.error(f"Ошибка получения курса с Rapira: {e}")
    return _cached_rate if _cached_rate > 0 else 95.0

# --- БАЗА ДАННЫХ ---
db_pool: asyncpg.Pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(
        DATABASE_URL, min_size=1, max_size=5, ssl="require", statement_cache_size=0 
    )
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY, balance REAL DEFAULT 0,
                last_active TIMESTAMPTZ DEFAULT NOW()
            );
        ''')
        await conn.execute('''
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMPTZ DEFAULT NOW();
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount REAL,
                payment_id TEXT UNIQUE, status TEXT, order_type TEXT DEFAULT 'deposit',
                comment TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        ''')
        await conn.execute('''
            ALTER TABLE orders ADD COLUMN IF NOT EXISTS comment TEXT;
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS allowed_users (user_id BIGINT PRIMARY KEY);
        ''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS withdrawals (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount_usdt REAL,
                amount_rub REAL, address TEXT, status TEXT DEFAULT 'pending',
                network TEXT DEFAULT 'BEP20',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        ''')
        await conn.execute('''
            ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS network TEXT DEFAULT 'BEP20';
        ''')
        await conn.execute("INSERT INTO allowed_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", ADMIN_ID)
        await conn.execute("INSERT INTO users (user_id, last_active) VALUES ($1, NOW()) ON CONFLICT DO NOTHING", ADMIN_ID)
    logging.info("Подключение к Supabase успешно!")

async def is_user_allowed(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM allowed_users WHERE user_id = $1", user_id)
        return row is not None

def format_last_seen(last_active: datetime):
    if not last_active: return "Неизвестно"
    if last_active.tzinfo is None: last_active = last_active.replace(tzinfo=timezone.utc)
    diff = datetime.now(timezone.utc) - last_active
    seconds = diff.total_seconds()
    
    if seconds < 60: return "🟢 Только что (В сети)"
    elif seconds < 3600: return f"🟡 {int(seconds // 60)} мин. назад"
    elif seconds < 86400: return f"🟠 {int(seconds // 3600)} ч. назад"
    else: return f"🔴 {int(seconds // 86400)} дн. назад"

# --- MIDDLEWARE ---
@dp.message.middleware()
async def track_activity_middleware(handler, event: types.Message, data):
    if event.from_user:
        async with db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO users (user_id, last_active) VALUES ($1, NOW())
                ON CONFLICT (user_id) DO UPDATE SET last_active = NOW()
            ''', event.from_user.id)
    return await handler(event, data)

# --- ОБРАБОТКА НЕАВТОРИЗОВАННЫХ ПОЛЬЗОВАТЕЛЕЙ (ВЕРИФИКАЦИЯ) ---

@dp.message(F.photo)
async def handle_verification_photo(message: types.Message):
    # Если пользователь уже одобрен, игнорируем фото
    if await is_user_allowed(message.from_user.id):
        return

    user = message.from_user
    admin_builder = InlineKeyboardBuilder()
    admin_builder.button(text="✅ Одобрить", callback_data=f"approve_access_{user.id}")
    admin_builder.button(text="❌ Отклонить", callback_data=f"reject_access_{user.id}")

    admin_text = (
        f"🔔 **НОВАЯ ЗАЯВКА НА ДОСТУП**\n\n"
        f"👤 Имя: {user.full_name}\n"
        f"🔗 Username: @{user.username if user.username else 'Нет'}\n"
        f"🆔 ID: `{user.id}`\n\n"
        f"Проверьте скриншот и примите решение."
    )

    try:
        # Отправляем фото админу
        await bot.send_photo(
            chat_id=ADMIN_ID,
            photo=message.photo[-1].file_id,
            caption=admin_text,
            reply_markup=admin_builder.as_markup(),
            parse_mode="Markdown"
        )
        await message.answer("⏳ Ваш скриншот отправлен на проверку администратору. Ожидайте.")
    except Exception as e:
        logging.error(f"Ошибка отправки фото админу: {e}")
        await message.answer("❌ Ошибка отправки. Попробуйте позже.")

# Обработка кнопок одобрения/отклонения от админа
@dp.callback_query(F.data.startswith("approve_access_"))
async def process_approve_access(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[2])

    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO allowed_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", target_id)
        await conn.execute("INSERT INTO users (user_id, last_active) VALUES ($1, NOW()) ON CONFLICT DO NOTHING", target_id)

    # Редактируем сообщение у админа
    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n✅ **ДОСТУП РАЗРЕШЕН**",
        reply_markup=None
    )
    
    # Уведомляем пользователя
    try:
        await bot.send_message(target_id, "✅ **Ваша заявка одобрена!**\nТеперь вы можете пользоваться ботом. Напишите /start")
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение: {e}")
    await callback.answer("Пользователь одобрен")

@dp.callback_query(F.data.startswith("reject_access_"))
async def process_reject_access(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[2])

    await callback.message.edit_caption(
        caption=callback.message.caption + "\n\n❌ **ДОСТУП ОТКЛОНЕН**",
        reply_markup=None
    )
    
    try:
        await bot.send_message(target_id, "❌ **В доступе отказано.**\nПожалуйста, убедитесь, что вы зарегистрировались по правильной ссылке и указали ментора.")
    except Exception as e:
        logging.error(f"Не удалось отправить сообщение: {e}")
    await callback.answer("Пользователь отклонен")

# --- КОМАНДЫ ПОЛЬЗОВАТЕЛЯ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    # Если пользователь не в белом списке, показываем инструкцию
    if not await is_user_allowed(message.from_user.id):
        await message.answer(
            f"👋 Привет! Для получения доступа к боту выполните следующие шаги:\n\n"
            f"1️⃣ Зарегистрируйтесь на **GO6** по моей ссылке:\n{GO6_REF_LINK}\n\n"
            f"2️⃣ Обязательно укажите меня как ментора.\n\n"
            f"3️⃣ Сделайте скриншот вашего личного кабинета (где видно, что вы под моим рефоводом) и **отправьте его сюда**."
        )
        return

    current_rate = await get_usdt_rate()
    await message.answer(
        f"Привет! Я бот для пополнения и вывода средств.\n\n"
        f"Текущий курс вывода: **1 USDT = {current_rate:.2f} ₽**\n\n"
        "💰 **/balance** — баланс\n"
        "📥 **/new [сумма] [название]** — пополнить\n"
        f"💸 **/withdraw [сумма] [сеть] [адрес]** — вывод"
    )

@dp.message(Command("allow"))
async def cmd_allow(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: 
        return await message.answer("Укажите ID: `/allow 123456789`")
    
    try:
        new_user_id = int(command.args.strip())
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO allowed_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", new_user_id)
            await conn.execute("INSERT INTO users (user_id, last_active) VALUES ($1, NOW()) ON CONFLICT DO NOTHING", new_user_id)
        await message.answer(f"✅ Пользователь `{new_user_id}` добавлен вручную.")
    except ValueError: 
        await message.answer("❌ Ошибка! ID должен состоять только из цифр.")

@dp.message(Command("deny"))
async def cmd_deny(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: 
        return await message.answer("Укажите ID: `/deny 123456789`")
    
    try:
        target_id = int(command.args.strip())
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM allowed_users WHERE user_id = $1", target_id)
        await message.answer(f"🚫 Пользователь `{target_id}` заблокирован.")
    except ValueError: 
        await message.answer("❌ Ошибка! ID должен состоять только из цифр.")

@dp.message(Command("deduct"))
async def cmd_deduct(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args or len(command.args.split()) < 2:
        return await message.answer("Используйте: `/deduct [ID] [Сумма]`")
    
    try:
        args = command.args.split()
        target_id = int(args[0])
        amount = float(args[1].replace(',', '.'))
        if amount <= 0: raise ValueError
    except ValueError:
        return await message.answer("❌ Ошибка формата.")

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", target_id)
            if not row: return await message.answer(f"❌ Пользователь `{target_id}` не найден.")
            current_bal = row['balance'] if row['balance'] is not None else 0.0
            if current_bal < amount:
                return await message.answer(f"❌ Недостаточно средств!\nЕго баланс: {current_bal:.2f} ₽")
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", amount, target_id)
    
    await message.answer(f"✅ Успешно списано **{amount:.2f} ₽**.", parse_mode="Markdown")
    try: await bot.send_message(target_id, f"⚠️ **Администратор списал с вашего баланса {amount:.2f} ₽.**", parse_mode="Markdown")
    except: pass

@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    if not await is_user_allowed(message.from_user.id): return
    current_rate = await get_usdt_rate()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", message.from_user.id)
        bal = row['balance'] if row and row['balance'] is not None else 0.0
    await message.answer(f"💰 **Баланс:** {bal:.2f} ₽\n({bal / current_rate:.2f} USDT)", parse_mode="Markdown")

# --- ПОПОЛНЕНИЕ С КОММЕНТАРИЕМ ---
@dp.message(Command("deposit"))
@dp.message(Command("new"))
async def cmd_deposit_new(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id): return
    if not command.args: return await message.answer("Укажите сумму: `/new 500 [Название]`")
    
    parts = command.args.split(maxsplit=1)
    try:
        amount = float(parts[0].replace(',', '.'))
        if amount <= 0: raise ValueError
    except ValueError: 
        return await message.answer("Ошибка! Неверная сумма.")
    
    comment = parts[1].strip() if len(parts) > 1 else None

    user_id = message.from_user.id
    order_id_str = f"deposit_{user_id}_{int(datetime.now().timestamp())}"
    
    desc_text = f"Пополнение на {amount:.2f} ₽"
    if comment: desc_text += f" | {comment}"

    try:
        payment = rolly_client.payments.create(
            amount=str(amount), order_id=order_id_str, terminal_id=TERMINAL_ID,
            description=desc_text, customer_id=str(user_id),
            redirect_url=f"https://t.me/{(await bot.get_me()).username}"
        )
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO orders (user_id, amount, payment_id, status, order_type, comment) VALUES ($1, $2, $3, $4, $5, $6)",
                user_id, amount, payment['payment_id'], "pending", "deposit", comment
            )
        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Перейти к оплате", url=payment['pay_url'])
        
        msg_text = f"**Счет: {amount:.2f} ₽**"
        if comment: msg_text = f"**Счет: {comment}**\nСумма: {amount:.2f} ₽"
            
        await message.answer(f"{msg_text}\n🟡 Ожидает оплату...", reply_markup=builder.as_markup(), parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Ошибка: {e}")
        await message.answer("Ошибка создания платежа.")

# --- ВЫВОД СРЕДСТВ ---
@dp.message(Command("withdraw"))
async def cmd_withdraw(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id): return
    
    if not command.args or len(command.args.split()) < 3:
        return await message.answer(f"Используйте: `/withdraw [сумма] [сеть] [адрес]`")

    try:
        args = command.args.split()
        amount_usdt = float(args[0].replace(',', '.'))
        network = args[1].upper().strip()
        address = args[2].strip()
        
        if network not in ['BEP20', 'TRC20']: return await message.answer("❌ Неверная сеть. Доступно `BEP20` или `TRC20`.")
        if network == 'TRC20' and amount_usdt < MIN_TRC20_USDT: return await message.answer(f"❌ Минимум для TRC20: **{MIN_TRC20_USDT} USDT**.", parse_mode="Markdown")
        if network == 'BEP20' and amount_usdt < MIN_BEP20_USDT: return await message.answer(f"❌ Минимум для BEP20: **{MIN_BEP20_USDT} USDT**.", parse_mode="Markdown")
        
        if network == 'BEP20' and (not address.startswith("0x") or len(address) < 40): return await message.answer("❌ Неверный адрес BEP20.")
        if network == 'TRC20' and (not address.startswith("T") or len(address) < 30): return await message.answer("❌ Неверный адрес TRC20.")
    except ValueError: return await message.answer("Ошибка! Неверная сумма.")

    network_fee_usdt = calculate_network_fee(network, amount_usdt)
    total_usdt_to_deduct = amount_usdt + network_fee_usdt
    
    current_rate = await get_usdt_rate()
    total_rub_to_deduct = total_usdt_to_deduct * current_rate
    admin_profit_rub = amount_usdt * ADMIN_FEE_PER_USDT
    user_id = message.from_user.id

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
            bal = row['balance'] if row and row['balance'] is not None else 0.0
            
            if bal < total_rub_to_deduct: 
                return await message.answer(f"❌ Недостаточно средств!\nК выводу: {amount_usdt} USDT\nКомиссия сети: {network_fee_usdt:.2f} USDT\nИтого: {total_rub_to_deduct:.2f} ₽\nУ вас: {bal:.2f} ₽")

            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", total_rub_to_deduct, user_id)
            await conn.execute("INSERT INTO withdrawals (user_id, amount_usdt, amount_rub, address, network) VALUES ($1, $2, $3, $4, $5)", user_id, amount_usdt, total_rub_to_deduct, address, network)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", admin_profit_rub, ADMIN_ID)
            w_id = (await conn.fetchrow("SELECT lastval()"))[0]

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отменить заявку", callback_data=f"user_cancel_{w_id}")
    await message.answer(f"✅ **Заявка #{w_id} создана!**\n\nСеть: **{network}**\nК получению: **{amount_usdt:.2f} USDT**\nСписано: **{total_rub_to_deduct:.2f} ₽**", reply_markup=builder.as_markup(), parse_mode="Markdown")

    admin_builder = InlineKeyboardBuilder()
    admin_builder.button(text="🟢 Взять в работу", callback_data=f"admin_take_{w_id}")
    admin_builder.button(text="🔴 Отклонить", callback_data=f"admin_reject_{w_id}")
    await bot.send_message(ADMIN_ID, f"💸 **ЗАЯВКА #{w_id}**\n👤 ID: `{user_id}`\n🌐 Сеть: **{network}**\n💵 К отправке: {amount_usdt:.2f} USDT\n🏦 Адрес: `{address}`\n📈 *Ваша прибыль: +{admin_profit_rub:.2f} ₽*", reply_markup=admin_builder.as_markup(), parse_mode="Markdown")

# --- АДМИН ПАНЕЛЬ ---
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    builder = InlineKeyboardBuilder()
    builder.button(text="📈 Общая статистика", callback_data="admin_stats")
    builder.button(text="👥 Список пользователей", callback_data="admin_users_0")
    await message.answer("👑 **Админ-панель**\nВыберите раздел:", reply_markup=builder.as_markup())

@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    current_rate = await get_usdt_rate()
    async with db_pool.acquire() as conn:
        users_count = await conn.fetchval("SELECT COUNT(*) FROM allowed_users")
        total_deposited = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status = 'paid'")
        total_withdrawn = await conn.fetchval("SELECT COALESCE(SUM(amount_usdt), 0) FROM withdrawals WHERE status = 'completed'")
        pending_withdrawals = await conn.fetchval("SELECT COUNT(*) FROM withdrawals WHERE status IN ('pending', 'processing')")
        total_user_balances = await conn.fetchval("SELECT COALESCE(SUM(balance), 0) FROM users WHERE user_id != $1", ADMIN_ID)
        total_profit = await conn.fetchval("SELECT COALESCE(SUM(amount_usdt * $1), 0) FROM withdrawals WHERE status != 'cancelled' AND status != 'rejected'", ADMIN_FEE_PER_USDT)
        admin_balance = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1", ADMIN_ID)

    text = (f"📊 **ОБЩАЯ СТАТИСТИКА**\n\n👥 Активных пользователей: **{users_count}**\n💰 Пополнено: **{total_deposited:.2f} ₽**\n💸 Выведено: **{total_withdrawn:.2f} USDT**\n⏳ Активных заявок: **{pending_withdrawals}**\n\n💼 Баланс юзеров: **{total_user_balances:.2f} ₽**\n\n👑 **ВАШ ДОХОД:**\n📈 Прибыль (+{ADMIN_FEE_PER_USDT}₽/USDT): **{total_profit:.2f} ₽**\n💳 Ваш баланс: **{admin_balance:.2f} ₽**")
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад в меню", callback_data="admin_main")
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_users_"))
async def cb_admin_users(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    try: page = int(callback.data.split("_")[2])
    except IndexError: page = 0
    limit, offset = 5, page * 5

    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT u.user_id, u.balance, u.last_active FROM users u JOIN allowed_users a ON u.user_id = a.user_id ORDER BY u.last_active DESC LIMIT $1 OFFSET $2", limit, offset)
        total_users = await conn.fetchval("SELECT COUNT(*) FROM allowed_users")

    keyboard = []
    for u in users:
        bal = u['balance'] if u['balance'] is not None else 0.0
        keyboard.append([InlineKeyboardButton(text=f"👤 ID: {u['user_id']} | {bal:.0f} ₽", callback_data=f"admin_user_view_{u['user_id']}")])
    
    nav_row = []
    if page > 0: nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_users_{page-1}"))
    if offset + limit < total_users: nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"admin_users_{page+1}"))
    if nav_row: keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton(text="🔙 В меню", callback_data="admin_main")])
    
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)
    total_pages = max(1, (total_users + limit - 1) // limit)
    await callback.message.edit_text(f"👥 **Список пользователей** (Стр. {page+1} из {total_pages}):", reply_markup=markup, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_user_view_"))
async def cb_admin_user_view(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[3])
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT balance, last_active FROM users WHERE user_id = $1", target_id)
        if not user: return await callback.answer("Пользователь не найден.", show_alert=True)
        total_in = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM orders WHERE user_id = $1 AND status = 'paid'", target_id)
        total_out = await conn.fetchval("SELECT COALESCE(SUM(amount_usdt), 0) FROM withdrawals WHERE user_id = $1 AND status = 'completed'", target_id)
        active_w = await conn.fetchval("SELECT COUNT(*) FROM withdrawals WHERE user_id = $1 AND status IN ('pending', 'processing')", target_id)

    bal = user['balance'] if user['balance'] is not None else 0.0
    text = (f"👤 **ДОСЬЕ:** `{target_id}`\n\n🕒 Статус: {format_last_seen(user['last_active'])}\n💰 Баланс: **{bal:.2f} ₽**\n\n📥 Всего пополнил: **{total_in:.2f} ₽**\n📤 Всего вывел: **{total_out:.2f} USDT**\n⏳ Активных заявок: **{active_w}**")
    builder = InlineKeyboardBuilder()
    builder.button(text="📥 История пополнений", callback_data=f"admin_user_orders_{target_id}_0")
    builder.button(text="💸 История выводов", callback_data=f"admin_user_wths_{target_id}_0")
    builder.row(InlineKeyboardButton(text="📝 Списать средства", callback_data=f"admin_deduct_info_{target_id}"), InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"admin_block_{target_id}"))
    builder.row(InlineKeyboardButton(text="🔙 К списку", callback_data="admin_users_0"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_deduct_info_"))
async def cb_admin_deduct_info(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[3])
    await callback.message.answer(f"Чтобы списать средства, отправьте:\n`/deduct {target_id} 500`", parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_block_"))
async def cb_admin_block(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM allowed_users WHERE user_id = $1", target_id)
    await callback.message.edit_text(f"🚫 Пользователь `{target_id}` заблокирован.", parse_mode="Markdown")
    await callback.answer("Заблокирован!")

@dp.callback_query(F.data.startswith("admin_user_orders_"))
async def cb_admin_user_orders(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    parts = callback.data.split("_")
    target_id, page = int(parts[3]), int(parts[4])
    limit, offset = 5, page * 5
    async with db_pool.acquire() as conn:
        orders = await conn.fetch("SELECT id, amount, status, comment, created_at FROM orders WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3", target_id, limit, offset)
        total_orders = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE user_id = $1", target_id)
    text = f"📥 **Пополнения пользователя** `{target_id}`:\n\n"
    if not orders: text += "Нет пополнений."
    builder = InlineKeyboardBuilder()
    for o in orders:
        status_emoji = "🟢" if o['status'] == 'paid' else "🟡"
        date_str = o['created_at'].strftime('%d.%m %H:%M')
        amt = o['amount'] if o['amount'] is not None else 0.0
        cmt = f"({o['comment']})" if o['comment'] else ""
        text += f"{status_emoji} #{o['id']} | {amt:.2f} ₽ {cmt} | {date_str}\n"
    nav_builder = InlineKeyboardBuilder()
    if page > 0: nav_builder.button(text="⬅️", callback_data=f"admin_user_orders_{target_id}_{page-1}")
    if offset + limit < total_orders: nav_builder.button(text="➡️", callback_data=f"admin_user_orders_{target_id}_{page+1}")
    builder.attach(nav_builder)
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data=f"admin_user_view_{target_id}"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_user_wths_"))
async def cb_admin_user_wths(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    parts = callback.data.split("_")
    target_id, page = int(parts[3]), int(parts[4])
    limit, offset = 5, page * 5
    async with db_pool.acquire() as conn:
        wths = await conn.fetch("SELECT id, amount_usdt, status, address, network, created_at FROM withdrawals WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3", target_id, limit, offset)
        total_wths = await conn.fetchval("SELECT COUNT(*) FROM withdrawals WHERE user_id = $1", target_id)
    text = f"💸 **Выводы пользователя** `{target_id}`:\n\n"
    if not wths: text += "Нет заявок."
    builder = InlineKeyboardBuilder()
    for w in wths:
        st = w['status']
        emoji = "✅" if st == 'completed' else "🟡" if st == 'pending' else "⏳" if st == 'processing' else "🔴" if st == 'rejected' else "❌"
        date_str = w['created_at'].strftime('%d.%m %H:%M')
        addr_short = f"{w['address'][:6]}...{w['address'][-4:]}" if w['address'] else "Нет адреса"
        amt = w['amount_usdt'] if w['amount_usdt'] is not None else 0.0
        net = w['network'] if w['network'] else 'BEP20'
        text += f"{emoji} #{w['id']} | {amt:.2f} USDT ({net})\n└ {addr_short} | {date_str}\n"
        if st in ['pending', 'processing']: builder.button(text=f"Управлять #{w['id']}", callback_data=f"manage_wth_{w['id']}")
    nav_builder = InlineKeyboardBuilder()
    if page > 0: nav_builder.button(text="⬅️", callback_data=f"admin_user_wths_{target_id}_{page-1}")
    if offset + limit < total_wths: nav_builder.button(text="➡️", callback_data=f"admin_user_wths_{target_id}_{page+1}")
    builder.adjust(1)
    builder.attach(nav_builder)
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data=f"admin_user_view_{target_id}"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("manage_wth_"))
async def cb_manage_wth(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    w_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, amount_usdt, address, network, status FROM withdrawals WHERE id = $1", w_id)
        if not row: return await callback.answer("Заявка не найдена.", show_alert=True)
        if row['status'] == 'pending':
            await conn.execute("UPDATE withdrawals SET status = 'processing' WHERE id = $1", w_id)
            row = await conn.fetchrow("SELECT user_id, amount_usdt, address, network, status FROM withdrawals WHERE id = $1", w_id)
        elif row['status'] == 'completed': return await callback.answer("Заявка уже выполнена.", show_alert=True)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Выполнено", callback_data=f"admin_done_{w_id}")
    builder.button(text="🔴 Отклонить", callback_data=f"admin_reject_{w_id}")
    net = row['network'] if row['network'] else 'BEP20'
    await callback.message.edit_text(f"⚙️ **Управление заявкой #{w_id}**\n\n👤 ID: `{row['user_id']}`\n🌐 Сеть: **{net}**\n💵 К отправке: {row['amount_usdt']:.2f} USDT\n🏦 Адрес: `{row['address']}`\n\nСтатус: **В работе**", reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data == "admin_main")
async def cb_admin_main(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    builder = InlineKeyboardBuilder()
    builder.button(text="📈 Общая статистика", callback_data="admin_stats")
    builder.button(text="👥 Список пользователей", callback_data="admin_users_0")
    await callback.message.edit_text("👑 **Админ-панель**\nВыберите раздел:", reply_markup=builder.as_markup())
    await callback.answer()

# --- ОБРАБОТКА ЗАЯВОК ---
@dp.callback_query(F.data.startswith("user_cancel_"))
async def process_user_cancel(callback: CallbackQuery):
    w_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT amount_rub, amount_usdt, status FROM withdrawals WHERE id = $1 AND user_id = $2", w_id, user_id)
            if not row: return await callback.answer("Заявка не найдена.", show_alert=True)
            if row['status'] != 'pending': return await callback.answer("Заявка уже в обработке.", show_alert=True)
            await conn.execute("UPDATE withdrawals SET status = 'cancelled' WHERE id = $1", w_id)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", row['amount_rub'], user_id)
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", row['amount_usdt'] * ADMIN_FEE_PER_USDT, ADMIN_ID)
    await callback.message.edit_text(f"❌ **Заявка #{w_id} отменена.**\nДеньги возвращены.", parse_mode="Markdown")
    await callback.answer("Отменено.")

@dp.callback_query(F.data.startswith("admin_take_"))
async def process_admin_take(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer("Нет доступа.")
    w_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, status FROM withdrawals WHERE id = $1", w_id)
        if not row or row['status'] != 'pending': return await callback.answer("Ошибка.", show_alert=True)
        await conn.execute("UPDATE withdrawals SET status = 'processing' WHERE id = $1", w_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Выполнено", callback_data=f"admin_done_{w_id}")
    builder.button(text="🔴 Отклонить", callback_data=f"admin_reject_{w_id}")
    await callback.message.edit_reply_markup(reply_markup=builder.as_markup())
    await callback.answer("В работе.")

@dp.callback_query(F.data.startswith("admin_reject_"))
async def process_admin_reject(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer("Нет доступа.")
    w_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT user_id, amount_rub, amount_usdt, status FROM withdrawals WHERE id = $1", w_id)
            if not row or row['status'] not in ['pending', 'processing']: return await callback.answer("Нельзя.", show_alert=True)
            await conn.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = $1", w_id)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", row['amount_rub'], row['user_id'])
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", row['amount_usdt'] * ADMIN_FEE_PER_USDT, ADMIN_ID)
    await callback.message.edit_text(f"🔴 Заявка #{w_id} отклонена. Деньги возвращены.")
    await callback.answer("Отклонено.")

@dp.callback_query(F.data.startswith("admin_done_"))
async def process_admin_done(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer("Нет доступа.")
    w_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, status FROM withdrawals WHERE id = $1", w_id)
        if not row or row['status'] != 'processing': return await callback.answer("Ошибка.", status=200)
        await conn.execute("UPDATE withdrawals SET status = 'completed' WHERE id = $1", w_id)
    await callback.message.edit_text(f"✅ Заявка #{w_id} успешно выполнена.")
    await callback.answer("Выполнено.")

# --- ВЕБХУКИ ---
async def handle_rollypay_webhook(request: web.Request):
    try:
        raw_body = await request.read()
        signature = request.headers.get("X-Signature")
        timestamp = request.headers.get("X-Timestamp")
        if not signature or not timestamp: return web.Response(text="Missing", status=400)

        message = f"{timestamp}.{raw_body.decode('utf-8')}"
        expected_signature = hmac.new(SIGNING_SECRET.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected_signature): return web.Response(text="Invalid", status=403)

        data = json.loads(raw_body)
        if data.get("event_type") == "payment.paid" or data.get("status") == "paid":
            payment_id = data.get("payment_id")
            async with db_pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow("SELECT id, user_id, amount, comment FROM orders WHERE payment_id = $1 AND status = 'pending'", payment_id)
                    if row:
                        db_id, user_id, amount, comment = row['id'], row['user_id'], row['amount'], row['comment']
                        await conn.execute("UPDATE orders SET status = 'paid' WHERE id = $1", db_id)
                        await conn.execute("INSERT INTO users (user_id, balance) VALUES ($1, $2) ON CONFLICT (user_id) DO UPDATE SET balance = users.balance + EXCLUDED.balance", user_id, amount)
                        
                        user_msg = f"🟢 **Баланс пополнен!**\n+{amount:.2f} ₽"
                        if comment: user_msg = f"**{comment}** {user_msg}"
                        try: await bot.send_message(user_id, user_msg, parse_mode="Markdown")
                        except: pass
                        
                        admin_msg = f"🔔 **Новое пополнение!**\n👤 ID: `{user_id}`\n💵 Сумма: {amount:.2f} ₽\n"
                        if comment: admin_msg += f"📝 Название: {comment}\n"
                        admin_msg += f"🧾 Заказ: #{db_id}"
                        try: await bot.send_message(ADMIN_ID, admin_msg, parse_mode="Markdown")
                        except: pass
        return web.Response(text="OK", status=200)
    except Exception as e:
        logging.error(f"Ошибка вебхука: {e}")
        return web.Response(text="Error", status=500)

# --- ЗАПУСК ---
async def main():
    await init_db()
    app = web.Application()
    app.router.add_post("/rollypay-webhook", handle_rollypay_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    logging.info(f"Бот запущен. Порт {WEBHOOK_PORT}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
