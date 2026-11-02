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

# --- ЛИМИТЫ И КОМИССИИ ---
MIN_TRC20_USDT = 10.0
MIN_BEP20_USDT = 50.0
TRC20_FEE_USDT = 3.0
ADMIN_ID = 7653934214

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
rolly_client = RollyPayClient(api_key=ROLLYPAY_API_KEY)

# --- ДИНАМИЧЕСКИЙ КУРС (RAPIRA + 3 РУБЛЯ) ---
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
                        _cached_rate = base_rate + 3.0
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
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
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

# --- КОМАНДЫ ПОЛЬЗОВАТЕЛЯ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not await is_user_allowed(message.from_user.id): return
    current_rate = await get_usdt_rate()
    await message.answer(
        f"Привет! Я бот для пополнения и вывода средств.\n\n"
        f"Текущий курс вывода: **1 USDT = {current_rate:.2f} ₽** (Rapira + 3₽)\n\n"
        "💰 **/balance** — баланс\n"
        "📥 **/new [сумма]** — пополнить (в рублях)\n"
        f"💸 **/withdraw [сумма] [сеть] [адрес]** — вывод\n\n"
        f"🌐 **Доступные сети:**\n"
        f"├ `BEP20` — без комиссии (от {MIN_BEP20_USDT} USDT)\n"
        f"└ `TRC20` — комиссия сети {TRC20_FEE_USDT} USDT (от {MIN_TRC20_USDT} USDT)\n\n"
        "Пример: `/withdraw 15 TRC20 TQ5N...`"
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
        await message.answer(f"✅ Пользователь `{new_user_id}` добавлен в белый список.")
    except ValueError: 
        await message.answer("❌ Ошибка! ID должен состоять только из цифр.")
    except Exception as e:
        await message.answer(f"❌ Ошибка базы: {e}")

@dp.message(Command("deny"))
async def cmd_deny(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: 
        return await message.answer("Укажите ID: `/deny 123456789`")
    
    try:
        target_id = int(command.args.strip())
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM allowed_users WHERE user_id = $1", target_id)
        await message.answer(f"🚫 Пользователь `{target_id}` удален из белого списка.")
    except ValueError: 
        await message.answer("❌ Ошибка! ID должен состоять только из цифр.")

@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    if not await is_user_allowed(message.from_user.id): return
    current_rate = await get_usdt_rate()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", message.from_user.id)
        bal = row['balance'] if row and row['balance'] is not None else 0.0
    await message.answer(f"💰 **Баланс:** {bal:.2f} ₽\n({bal / current_rate:.2f} USDT по курсу {current_rate:.2f})", parse_mode="Markdown")

@dp.message(Command("deposit"))
@dp.message(Command("new"))
async def cmd_deposit_new(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id): return
    if not command.args: return await message.answer("Укажите сумму: `/deposit 500`")
    try:
        amount = float(command.args.replace(',', '.'))
        if amount <= 0: raise ValueError
    except ValueError: return await message.answer("Ошибка! Неверная сумма.")

    user_id = message.from_user.id
    order_id_str = f"deposit_{user_id}_{int(datetime.now().timestamp())}"
    try:
        payment = rolly_client.payments.create(
            amount=str(amount), order_id=order_id_str, terminal_id=TERMINAL_ID,
            description=f"Пополнение на {amount:.2f} ₽", customer_id=str(user_id),
            redirect_url=f"https://t.me/{(await bot.get_me()).username}"
        )
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO orders (user_id, amount, payment_id, status, order_type) VALUES ($1, $2, $3, $4, $5)",
                user_id, amount, payment['payment_id'], "pending", "deposit"
            )
        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Перейти к оплате", url=payment['pay_url'])
        await message.answer(f"**Счет: {amount:.2f} ₽**\n🟡 Ожидает оплату...", reply_markup=builder.as_markup(), parse_mode="Markdown")
    except Exception as e:
        await message.answer("Ошибка создания платежа.")

# --- ВЫВОД СРЕДСТВ (РАЗНЫЕ ЛИМИТЫ) ---
@dp.message(Command("withdraw"))
async def cmd_withdraw(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id): return
    
    if not command.args or len(command.args.split()) < 3:
        return await message.answer(f"Ошибка формата!\nИспользуйте: `/withdraw [сумма] [сеть] [адрес]`\nПример: `/withdraw 15 TRC20 TQ5N...`")

    try:
        args = command.args.split()
        amount_usdt = float(args[0].replace(',', '.'))
        network = args[1].upper().strip()
        address = args[2].strip()
        
        # Проверка сети
        if network not in ['BEP20', 'TRC20']:
            return await message.answer("❌ Неверная сеть. Доступно только `BEP20` или `TRC20`.")
        
        # ИНДИВИДУАЛЬНАЯ ПРОВЕРКА ЛИМИТОВ
        if network == 'TRC20' and amount_usdt < MIN_TRC20_USDT:
            return await message.answer(f"❌ Минимальная сумма для сети **TRC20** — {MIN_TRC20_USDT} USDT.", parse_mode="Markdown")
        if network == 'BEP20' and amount_usdt < MIN_BEP20_USDT:
            return await message.answer(f"❌ Минимальная сумма для сети **BEP20** — {MIN_BEP20_USDT} USDT.", parse_mode="Markdown")
        
        # Проверка адреса
        if network == 'BEP20' and (not address.startswith("0x") or len(address) < 40):
            return await message.answer("❌ Неверный адрес для сети BEP20 (должен начинаться с `0x`).")
        if network == 'TRC20' and (not address.startswith("T") or len(address) < 30):
            return await message.answer("❌ Неверный адрес для сети TRC20 (должен начинаться с `T`).")
            
    except ValueError: 
        return await message.answer("Ошибка! Неверная сумма.")

    # Считаем комиссию
    fee_usdt = TRC20_FEE_USDT if network == 'TRC20' else 0.0
    total_usdt_to_deduct = amount_usdt + fee_usdt
    
    current_rate = await get_usdt_rate()
    total_rub_to_deduct = total_usdt_to_deduct * current_rate
    user_id = message.from_user.id

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
            bal = row['balance'] if row and row['balance'] is not None else 0.0
            
            if bal < total_rub_to_deduct: 
                return await message.answer(
                    f"❌ Недостаточно средств!\n"
                    f"К выводу: {amount_usdt} USDT\n"
                    f"Комиссия сети: {fee_usdt} USDT\n"
                    f"Итого спишется: {total_usdt_to_deduct:.2f} USDT ({total_rub_to_deduct:.2f} ₽)\n"
                    f"У вас: {bal:.2f} ₽"
                )

            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", total_rub_to_deduct, user_id)
            await conn.execute(
                "INSERT INTO withdrawals (user_id, amount_usdt, amount_rub, address, network) VALUES ($1, $2, $3, $4, $5)",
                user_id, amount_usdt, total_rub_to_deduct, address, network
            )
            w_id = (await conn.fetchrow("SELECT lastval()"))[0]

    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отменить заявку", callback_data=f"user_cancel_{w_id}")
    
    fee_text = f"(+{fee_usdt} USDT комиссия сети)" if fee_usdt > 0 else "(без комиссии)"
    await message.answer(
        f"✅ **Заявка #{w_id} создана!**\n\n"
        f"Сеть: **{network}**\n"
        f"К получению: **{amount_usdt:.2f} USDT** {fee_text}\n"
        f"Адрес: `{address}`\n"
        f"Списано с баланса: **{total_rub_to_deduct:.2f} ₽**", 
        reply_markup=builder.as_markup(), parse_mode="Markdown"
    )

    admin_builder = InlineKeyboardBuilder()
    admin_builder.button(text="🟢 Взять в работу", callback_data=f"admin_take_{w_id}")
    admin_builder.button(text="🔴 Отклонить", callback_data=f"admin_reject_{w_id}")
    
    admin_msg = (
        f"💸 **ЗАЯВКА #{w_id}**\n"
        f"👤 ID: `{user_id}`\n"
        f"🌐 Сеть: **{network}**\n"
        f"💵 К отправке: {amount_usdt:.2f} USDT\n"
        f"🏦 Адрес: `{address}`"
    )
    await bot.send_message(ADMIN_ID, admin_msg, reply_markup=admin_builder.as_markup(), parse_mode="Markdown")

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
        total_user_balances = await conn.fetchval("SELECT COALESCE(SUM(balance), 0) FROM users")

    text = (
        f"📊 **ОБЩАЯ СТАТИСТИКА**\n\n"
        f"👥 Активных пользователей: **{users_count}**\n"
        f"💰 Пополнено: **{total_deposited:.2f} ₽**\n"
        f"💸 Выведено: **{total_withdrawn:.2f} USDT**\n"
        f"⏳ Активных заявок: **{pending_withdrawals}**\n\n"
        f"💼 Баланс всех юзеров: **{total_user_balances:.2f} ₽** ({total_user_balances/current_rate:.2f} USDT)\n"
        f"💱 Текущий курс: **{current_rate:.2f} ₽** (Rapira +3₽)"
    )
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад в меню", callback_data="admin_main")
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_users_"))
async def cb_admin_users(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    try: page = int(callback.data.split("_")[2])
    except IndexError: page = 0
    
    limit = 5
    offset = page * limit

    async with db_pool.acquire() as conn:
        users = await conn.fetch("""
            SELECT u.user_id, u.balance, u.last_active 
            FROM users u
            JOIN allowed_users a ON u.user_id = a.user_id
            ORDER BY u.last_active DESC LIMIT $1 OFFSET $2
        """, limit, offset)
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

    await callback.message.edit_text(f"👥 **Список пользователей** (Страница {page+1} из {total_pages}):", reply_markup=markup, parse_mode="Markdown")
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
    text = (
        f"👤 **ДОСЬЕ:** `{target_id}`\n\n"
        f"🕒 Статус: {format_last_seen(user['last_active'])}\n"
        f"💰 Баланс: **{bal:.2f} ₽**\n\n"
        f"📥 Всего пополнил: **{total_in:.2f} ₽**\n"
        f"📤 Всего вывел: **{total_out:.2f} USDT**\n"
        f"⏳ Активных заявок на вывод: **{active_w}**"
    )

    builder = InlineKeyboardBuilder()
    builder.button(text="📥 История пополнений", callback_data=f"admin_user_orders_{target_id}_0")
    builder.button(text="💸 История выводов", callback_data=f"admin_user_wths_{target_id}_0")
    builder.row(
        InlineKeyboardButton(text="🚫 Заблокировать доступ", callback_data=f"admin_block_{target_id}"),
        InlineKeyboardButton(text="🔙 К списку", callback_data="admin_users_0")
    )
    
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_block_"))
async def cb_admin_block(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[2])

    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM allowed_users WHERE user_id = $1", target_id)
    
    await callback.message.edit_text(f"🚫 Пользователь `{target_id}` успешно заблокирован.\nБаланс и история сохранены.", parse_mode="Markdown")
    await callback.answer("Пользователь заблокирован!")

@dp.callback_query(F.data.startswith("admin_user_orders_"))
async def cb_admin_user_orders(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    parts = callback.data.split("_")
    target_id, page = int(parts[3]), int(parts[4])
    limit, offset = 5, page * 5

    async with db_pool.acquire() as conn:
        orders = await conn.fetch("SELECT id, amount, status, created_at FROM orders WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3", target_id, limit, offset)
        total_orders = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE user_id = $1", target_id)

    text = f"📥 **Пополнения пользователя** `{target_id}`:\n\n"
    if not orders: text += "Нет пополнений."

    builder = InlineKeyboardBuilder()
    for o in orders:
        status_emoji = "🟢" if o['status'] == 'paid' else "🟡"
        date_str = o['created_at'].strftime('%d.%m %H:%M')
        amt = o['amount'] if o['amount'] is not None else 0.0
        text += f"{status_emoji} #{o['id']} | {amt:.2f} ₽ | {date_str}\n"

    nav_builder = InlineKeyboardBuilder()
    if page > 0: nav_builder.button(text="⬅️", callback_data=f"admin_user_orders_{target_id}_{page-1}")
    if offset + limit < total_orders: nav_builder.button(text="➡️", callback_data=f"admin_user_orders_{target_id}_{page+1}")
    builder.attach(nav_builder)
    builder.row(InlineKeyboardButton(text="🔙 Назад к профилю", callback_data=f"admin_user_view_{target_id}"))

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
    if not wths: text += "Нет заявок на вывод."

    builder = InlineKeyboardBuilder()
    for w in wths:
        st = w['status']
        emoji = "✅" if st == 'completed' else "🟡" if st == 'pending' else "⏳" if st == 'processing' else "🔴" if st == 'rejected' else "❌"
        date_str = w['created_at'].strftime('%d.%m %H:%M')
        addr_short = f"{w['address'][:6]}...{w['address'][-4:]}" if w['address'] else "Нет адреса"
        amt = w['amount_usdt'] if w['amount_usdt'] is not None else 0.0
        net = w['network'] if w['network'] else 'BEP20'
        
        text += f"{emoji} #{w['id']} | {amt:.2f} USDT ({net})\n└ {addr_short} | {date_str}\n"
        
        if st in ['pending', 'processing']:
            builder.button(text=f"Управлять #{w['id']}", callback_data=f"manage_wth_{w['id']}")

    nav_builder = InlineKeyboardBuilder()
    if page > 0: nav_builder.button(text="⬅️", callback_data=f"admin_user_wths_{target_id}_{page-1}")
    if offset + limit < total_wths: nav_builder.button(text="➡️", callback_data=f"admin_user_wths_{target_id}_{page+1}")
    
    builder.adjust(1)
    builder.attach(nav_builder)
    builder.row(InlineKeyboardButton(text="🔙 Назад к профилю", callback_data=f"admin_user_view_{target_id}"))

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
        elif row['status'] == 'completed':
            return await callback.answer("Заявка уже выполнена.", show_alert=True)

    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Выполнено", callback_data=f"admin_done_{w_id}")
    builder.button(text="🔴 Отклонить", callback_data=f"admin_reject_{w_id}")

    net = row['network'] if row['network'] else 'BEP20'

    await callback.message.edit_text(
        f"⚙️ **Управление заявкой #{w_id}**\n\n"
        f"👤 ID: `{row['user_id']}`\n"
        f"🌐 Сеть: **{net}**\n"
        f"💵 К отправке: {row['amount_usdt']:.2f} USDT\n"
        f"🏦 Адрес: `{row['address']}`\n\n"
        f"Статус: **В работе**",
        reply_markup=builder.as_markup(), parse_mode="Markdown"
    )
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
            row = await conn.fetchrow("SELECT amount_rub, status FROM withdrawals WHERE id = $1 AND user_id = $2", w_id, user_id)
            if not row: return await callback.answer("Заявка не найдена.", show_alert=True)
            if row['status'] != 'pending': return await callback.answer("Заявка уже в обработке.", show_alert=True)
            await conn.execute("UPDATE withdrawals SET status = 'cancelled' WHERE id = $1", w_id)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", row['amount_rub'], user_id)
    await callback.message.edit_text(f"❌ **Заявка #{w_id} отменена.**\nДеньги (включая комиссию) возвращены.", parse_mode="Markdown")
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
            row = await conn.fetchrow("SELECT user_id, amount_rub, status FROM withdrawals WHERE id = $1", w_id)
            if not row or row['status'] not in ['pending', 'processing']: return await callback.answer("Нельзя.", show_alert=True)
            await conn.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = $1", w_id)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", row['amount_rub'], row['user_id'])
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
                    row = await conn.fetchrow("SELECT id, user_id, amount FROM orders WHERE payment_id = $1 AND status = 'pending'", payment_id)
                    if row:
                        db_id, user_id, amount = row['id'], row['user_id'], row['amount']
                        await conn.execute("UPDATE orders SET status = 'paid' WHERE id = $1", db_id)
                        await conn.execute("""
                            INSERT INTO users (user_id, balance) VALUES ($1, $2)
                            ON CONFLICT (user_id) DO UPDATE SET balance = users.balance + EXCLUDED.balance
                        """, user_id, amount)
                        try: await bot.send_message(user_id, f"🟢 **Баланс пополнен!**\n+{amount:.2f} ₽", parse_mode="Markdown")
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
