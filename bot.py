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
from aiorocket2 import xRocketClient
import os
import hmac
import hashlib
import json
import time
import random

# --- НАСТРОЙКИ ---
BOT_TOKEN = "9037852041:AAGlmd1cTt0Mbc9CM6t-kYJimE86h2cRiEo"
ROLLYPAY_API_KEY = "aMkEd8uF3EY1PM5Zzki5oFfPVx7pnRUiuxYUJQLS5ts"
TERMINAL_ID = "02485c32-7013-40f6-9c1f-00eb12f51d31"
SIGNING_SECRET = "SQ7ZPKKj0l-_yFL_KsARgUj6FjuMJ51v4KFgbr53MAk"
WEBHOOK_PORT = int(os.environ.get("PORT", 8080))

# --- XROCKET PAY API (ВАШИ КЛЮЧИ) ---
XROCKET_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhcHBJZCI6IjMwNzc2NSIsImp0aSI6ImFwcDozMDc3NjU6YWMzYjUyODgtYWI3MS00MzUyLTgzNWEtYjRmNjVjZTMyNGQwIiwiaWF0IjoxNzkzNzE1MjA4fQ.nBCxGBwKmyaKjt4cW_jrtLBlagXxGQbvgD5c14yT2TI"
XROCKET_WEBHOOK_SECRET = "74524a9673bb8d1f18dd00eaf22eecd7daf75fbfefd664d1389cefaead0a6673"
rocket_client = xRocketClient(api_key=XROCKET_API_KEY)

DATABASE_URL = "postgresql://postgres.qrfqdihhoqvlaqkuyols:Koloskov2006@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"

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

# --- ХРАНИЛИЩЕ АКТИВНЫХ ИГР ---
active_games = {}

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
        await conn.execute('''ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMPTZ DEFAULT NOW();''')
        await conn.execute('''ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;''')
        await conn.execute('''ALTER TABLE users ADD COLUMN IF NOT EXISTS verified_go6 BOOLEAN DEFAULT FALSE;''')
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount REAL,
                payment_id TEXT UNIQUE, status TEXT, order_type TEXT DEFAULT 'deposit',
                comment TEXT, created_at TIMESTAMPTZ DEFAULT NOW()
            );
        ''')
        await conn.execute('''ALTER TABLE orders ADD COLUMN IF NOT EXISTS comment TEXT;''')
        
        await conn.execute('''CREATE TABLE IF NOT EXISTS allowed_users (user_id BIGINT PRIMARY KEY);''')
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS withdrawals (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount_usdt REAL,
                amount_rub REAL, address TEXT, status TEXT DEFAULT 'pending',
                network TEXT DEFAULT 'BEP20', created_at TIMESTAMPTZ DEFAULT NOW()
            );
        ''')
        await conn.execute('''ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS network TEXT DEFAULT 'BEP20';''')
        
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

@dp.message.middleware()
async def track_activity_middleware(handler, event: types.Message, data):
    if event.from_user:
        username = event.from_user.username
        async with db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO users (user_id, last_active, username) VALUES ($1, NOW(), $2)
                ON CONFLICT (user_id) DO UPDATE SET last_active = NOW(), username = $2
            ''', event.from_user.id, username)
    return await handler(event, data)

# --- ИГРА "КРАШ" ---
@dp.message(Command("crash"))
async def cmd_crash(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id): return
    user_id = message.from_user.id
    if user_id in active_games: return await message.answer("❌ Вы уже играете!")
    if not command.args: return await message.answer("Укажите ставку: `/crash 1`")
    try:
        bet_usdt = float(command.args.replace(',', '.'))
        if bet_usdt <= 0: raise ValueError
    except: return await message.answer("Ошибка! Неверная сумма.")

    current_rate = await get_usdt_rate()
    bet_rub = bet_usdt * current_rate

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
        if not row or row['balance'] < bet_rub:
            return await message.answer(f"❌ Недостаточно средств! Нужно: {bet_rub:.2f} ₽")
        await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", bet_rub, user_id)

    r = random.uniform(0.01, 1.0)
    crash_point = max(1.0, 0.90 / r)
    if crash_point < 1.1: crash_point = 1.1

    active_games[user_id] = {'multiplier': 1.0, 'bet_usdt': bet_usdt, 'rate': current_rate, 'crash_point': crash_point}
    
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 ЗАБРАТЬ (1.0x)", callback_data=f"crash_cashout_{user_id}")
    msg = await message.answer(f"🚀 **Игра началась!**\nСтавка: {bet_usdt} USDT\n\nМножитель: **1.0x**", reply_markup=builder.as_markup(), parse_mode="Markdown")
    asyncio.create_task(run_crash_game(msg.chat.id, msg.message_id, user_id))

async def run_crash_game(chat_id: int, message_id: int, user_id: int):
    if user_id not in active_games: return
    game = active_games[user_id]
    crash_point = game['crash_point']
    try:
        while True:
            await asyncio.sleep(1.0)
            if user_id not in active_games: break
            game['multiplier'] += 0.1
            current_mult = game['multiplier']
            
            if current_mult >= crash_point:
                active_games.pop(user_id)
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"💥 **КРАШ!** Множитель лопнул на **{current_mult:.1f}x**.\nВы проиграли {game['bet_usdt']} USDT.", parse_mode="Markdown")
                break
            
            win_usdt = game['bet_usdt'] * current_mult
            win_rub = win_usdt * game['rate']
            builder = InlineKeyboardBuilder()
            builder.button(text=f"💰 ЗАБРАТЬ ({win_usdt:.2f} USDT)", callback_data=f"crash_cashout_{user_id}")
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"🚀 **Множитель: {current_mult:.1f}x**\nВаш выигрыш: {win_usdt:.2f} USDT ({win_rub:.2f} ₽)", reply_markup=builder.as_markup(), parse_mode="Markdown")
            except: pass
    except Exception as e: logging.error(f"Ошибка в игре: {e}")

@dp.callback_query(F.data.startswith("crash_cashout_"))
async def cb_crash_cashout(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[2])
    if callback.from_user.id != user_id: return await callback.answer("Это не ваша игра!", show_alert=True)
    if user_id in active_games:
        game = active_games.pop(user_id)
        multiplier = game['multiplier']
        win_rub = game['bet_usdt'] * multiplier * game['rate']
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", win_rub, user_id)
        await callback.message.edit_text(f"✅ **Вы забрали выигрыш!**\nМножитель: {multiplier:.1f}x\nВыигрыш: +{win_rub:.2f} ₽", parse_mode="Markdown")
        await callback.answer("Выигрыш зачислен!")
    else: await callback.answer("Игра уже завершена.", show_alert=True)

# --- ПОПОЛНЕНИЕ ЧЕРЕЗ XROCKET ---
@dp.message(Command("usdt"))
async def cmd_deposit_crypto(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id): return
    if not command.args: return await message.answer("Укажите сумму в USDT: `/usdt 10`")
    try:
        amount_usdt = float(command.args.replace(',', '.'))
        if amount_usdt < 1: raise ValueError
    except: return await message.answer("Ошибка! Минимальная сумма 1 USDT.")

    try:
        invoice = await rocket_client.create_invoice(
            currency="USDT",
            amount=amount_usdt,
            description="Пополнение баланса в боте",
            payload=str(message.from_user.id),
            expired_in=3600
        )
        
        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Оплатить в xRocket", url=invoice.link)
        
        await message.answer(
            f"💵 **Счет на {amount_usdt} USDT**\n\nНажмите на кнопку ниже, чтобы оплатить через @xRocket.",
            reply_markup=builder.as_markup()
        )
    except Exception as e:
        logging.error(f"xRocket Error: {e}")
        await message.answer("❌ Ошибка создания счета. Попробуйте позже.")

# --- ОБРАБОТКА ВЕРИФИКАЦИИ GO6 ---
@dp.message(F.photo)
async def handle_verification_photo(message: types.Message):
    if await is_user_allowed(message.from_user.id): return
    user = message.from_user
    async with db_pool.acquire() as conn:
        await conn.execute('''INSERT INTO users (user_id, last_active, username) VALUES ($1, NOW(), $2) ON CONFLICT (user_id) DO UPDATE SET last_active = NOW(), username = $2''', user.id, user.username)
    admin_builder = InlineKeyboardBuilder()
    admin_builder.button(text="✅ Одобрить", callback_data=f"approve_access_{user.id}")
    admin_builder.button(text="❌ Отклонить", callback_data=f"reject_access_{user.id}")
    try:
        await bot.send_photo(chat_id=ADMIN_ID, photo=message.photo[-1].file_id, caption=f"🔔 **ЗАЯВКА**\n👤 {user.full_name}\n🔗 @{user.username}\n🆔 `{user.id}`", reply_markup=admin_builder.as_markup(), parse_mode="Markdown")
        await message.answer("⏳ Ожидайте проверки.")
    except: pass

@dp.callback_query(F.data.startswith("approve_access_"))
async def process_approve_access(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO allowed_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", target_id)
        await conn.execute("UPDATE users SET verified_go6 = TRUE WHERE user_id = $1", target_id)
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ **ОДОБРЕНО**", reply_markup=None)
    try: await bot.send_message(target_id, "✅ Доступ одобрен! Напишите /start")
    except: pass

@dp.callback_query(F.data.startswith("reject_access_"))
async def process_reject_access(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[2])
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ **ОТКЛОНЕНО**", reply_markup=None)
    try: await bot.send_message(target_id, "❌ В доступе отказано.")
    except: pass

# --- КОМАНДЫ ПОЛЬЗОВАТЕЛЯ ---
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not await is_user_allowed(message.from_user.id):
        return await message.answer(f"👋 Для доступа:\n1️⃣ Регистрация на GO6: {GO6_REF_LINK}\n2️⃣ Укажите ментора.\n3️⃣ Отправьте скрин сюда.")
    current_rate = await get_usdt_rate()
    await message.answer(f"Привет! Курс: **1 USDT = {current_rate:.2f} ₽**\n\n💰 /balance — баланс\n📥 /new [сумма] — пополнить (Рубли)\n💵 /usdt [сумма] — пополнить USDT (xRocket)\n💸 /withdraw — вывод\n🎮 /crash [сумма_USDT] — играть")

@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    if not await is_user_allowed(message.from_user.id): return
    current_rate = await get_usdt_rate()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", message.from_user.id)
        bal = row['balance'] if row and row['balance'] is not None else 0.0
    await message.answer(f"💰 **Баланс:** {bal:.2f} ₽\n({bal / current_rate:.2f} USDT)", parse_mode="Markdown")

@dp.message(Command("new"))
async def cmd_new(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id): return
    if not command.args: return await message.answer("Укажите сумму: `/new 500`")
    parts = command.args.split(maxsplit=1)
    try: amount = float(parts[0].replace(',', '.'))
    except: return await message.answer("Ошибка! Неверная сумма.")
    comment = parts[1] if len(parts) > 1 else None
    try:
        payment = rolly_client.payments.create(amount=str(amount), order_id=f"dep_{message.from_user.id}_{int(time.time())}", terminal_id=TERMINAL_ID, description=f"Пополнение", customer_id=str(message.from_user.id), redirect_url="https://t.me")
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO orders (user_id, amount, payment_id, status, comment) VALUES ($1, $2, $3, $4, $5)", message.from_user.id, amount, payment['payment_id'], "pending", comment)
        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Перейти к оплате", url=payment['pay_url'])
        await message.answer(f"**Счет: {amount:.2f} ₽**\n🟡 Ожидает оплату...", reply_markup=builder.as_markup())
    except Exception as e: await message.answer("Ошибка создания платежа.")

@dp.message(Command("withdraw"))
async def cmd_withdraw(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id): return
    if not command.args or len(command.args.split()) < 3: return await message.answer("`/withdraw [сумма] [сеть] [адрес]`")
    try:
        args = command.args.split()
        amount_usdt = float(args[0].replace(',', '.'))
        network = args[1].upper().strip()
        address = args[2].strip()
        if network not in ['BEP20', 'TRC20']: return await message.answer("❌ Неверная сеть.")
        if network == 'TRC20' and amount_usdt < MIN_TRC20_USDT: return await message.answer(f"❌ Мин. TRC20: {MIN_TRC20_USDT} USDT")
        if network == 'BEP20' and amount_usdt < MIN_BEP20_USDT: return await message.answer(f"❌ Мин. BEP20: {MIN_BEP20_USDT} USDT")
    except: return await message.answer("Ошибка!")

    fee = calculate_network_fee(network, amount_usdt)
    total_usdt = amount_usdt + fee
    rate = await get_usdt_rate()
    total_rub = total_usdt * rate
    profit_rub = amount_usdt * ADMIN_FEE_PER_USDT

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", message.from_user.id)
        if not row or row['balance'] < total_rub: return await message.answer("❌ Недостаточно средств.")
        await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", total_rub, message.from_user.id)
        await conn.execute("INSERT INTO withdrawals (user_id, amount_usdt, amount_rub, address, network) VALUES ($1, $2, $3, $4, $5)", message.from_user.id, amount_usdt, total_rub, address, network)
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", profit_rub, ADMIN_ID)
        w_id = (await conn.fetchrow("SELECT lastval()"))[0]

    await message.answer(f"✅ **Заявка #{w_id}**\nСеть: {network}\nК получению: {amount_usdt:.2f} USDT\nСписано: {total_rub:.2f} ₽")
    builder = InlineKeyboardBuilder()
    builder.button(text="🟢 В работу", callback_data=f"admin_take_{w_id}")
    builder.button(text="🔴 Отклонить", callback_data=f"admin_reject_{w_id}")
    await bot.send_message(ADMIN_ID, f"💸 **ЗАЯВКА #{w_id}**\nID: `{message.from_user.id}`\nСеть: {network}\nUSDT: {amount_usdt}\nАдрес: `{address}`\nПрибыль: +{profit_rub} ₽", reply_markup=builder.as_markup())

# --- АДМИН ПАНЕЛЬ ---
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    builder = InlineKeyboardBuilder()
    builder.button(text="📈 Статистика", callback_data="admin_stats")
    builder.button(text="👥 Пользователи", callback_data="admin_users_0")
    await message.answer("👑 **Админ-панель:**", reply_markup=builder.as_markup())

@dp.message(Command("add"))
async def cmd_add(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    try:
        target_id, amount = int(command.args.split()[0]), float(command.args.split()[1])
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, target_id)
        await message.answer(f"✅ Начислено {amount} ₽.")
    except: pass

@dp.message(Command("deduct"))
async def cmd_deduct(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    try:
        target_id, amount = int(command.args.split()[0]), float(command.args.split()[1])
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", amount, target_id)
        await message.answer(f"✅ Списано {amount} ₽.")
    except: pass

@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    async with db_pool.acquire() as conn:
        u = await conn.fetchval("SELECT COUNT(*) FROM allowed_users")
        d = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status = 'paid'")
        w = await conn.fetchval("SELECT COALESCE(SUM(amount_usdt), 0) FROM withdrawals WHERE status = 'completed'")
        p = await conn.fetchval("SELECT COALESCE(SUM(amount_usdt * $1), 0) FROM withdrawals WHERE status != 'cancelled'", ADMIN_FEE_PER_USDT)
    text = f"👥 Юзеров: **{u}**\n💰 Пополнено: **{d:.2f} ₽**\n💸 Выведено: **{w:.2f} USDT**\n📈 Прибыль с выводов: **{p:.2f} ₽**"
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_main")]]))

@dp.callback_query(F.data == "admin_main")
async def cb_admin_main(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    builder = InlineKeyboardBuilder()
    builder.button(text="📈 Статистика", callback_data="admin_stats")
    builder.button(text="👥 Пользователи", callback_data="admin_users_0")
    await callback.message.edit_text("👑 **Админ-панель:**", reply_markup=builder.as_markup())

# --- ВЕБХУКИ ---
async def handle_rollypay_webhook(request: web.Request):
    try:
        raw_body = await request.read()
        sig = request.headers.get("X-Signature")
        ts = request.headers.get("X-Timestamp")
        if not sig or not ts: return web.Response(text="Missing", status=400)
        msg = f"{ts}.{raw_body.decode('utf-8')}"
        expected = hmac.new(SIGNING_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected): return web.Response(text="Invalid", status=403)
        data = json.loads(raw_body)
        if data.get("status") == "paid":
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow("SELECT user_id, amount FROM orders WHERE payment_id = $1 AND status = 'pending'", data.get("payment_id"))
                if row:
                    await conn.execute("UPDATE orders SET status = 'paid' WHERE payment_id = $1", data.get("payment_id"))
                    await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", row['amount'], row['user_id'])
                    try: await bot.send_message(row['user_id'], f"🟢 Пополнено на {row['amount']} ₽")
                    except: pass
        return web.Response(text="OK", status=200)
    except: return web.Response(text="Error", status=500)

async def handle_xrocket_webhook(request: web.Request):
    try:
        raw_body = await request.read()
        sig = request.headers.get("Signature")
        sig_ver = request.headers.get("Signature-Version")
        sig_ts = request.headers.get("Signature-Timestamp")
        
        if not sig or not sig_ver or not sig_ts:
            return web.Response(text="Missing signature headers", status=400)
        if sig_ver != "v1":
            return web.Response(text="Unsupported signature version", status=400)

        signed_string = f"{sig_ts}.{raw_body.decode('utf-8')}"
        expected_sig = hmac.new(XROCKET_WEBHOOK_SECRET.encode(), signed_string.encode(), hashlib.sha256).hexdigest()
        
        if not hmac.compare_digest(sig, expected_sig):
            return web.Response(text="Invalid signature", status=401)

        data = json.loads(raw_body)
        if data.get("type") == "invoice":
            invoice_data = data.get("data", {})
            status = invoice_data.get("status")
            if status == "paid":
                payload = invoice_data.get("payload")
                amount_usdt = float(invoice_data.get("amount", 0))
                
                if payload:
                    user_id = int(payload)
                    rate = await get_usdt_rate()
                    amount_rub = amount_usdt * rate
                    
                    async with db_pool.acquire() as conn:
                        await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount_rub, user_id)
                    
                    try:
                        await bot.send_message(user_id, f"🟢 **Баланс пополнен на {amount_usdt} USDT!**\n+{amount_rub:.2f} ₽", parse_mode="Markdown")
                    except: pass
        return web.Response(text="OK", status=200)
    except Exception as e:
        logging.error(f"xRocket webhook error: {e}")
        return web.Response(text="Error", status=500)

# --- ЗАПУСК ---
async def main():
    await init_db()
    app = web.Application()
    app.router.add_post("/rollypay-webhook", handle_rollypay_webhook)
    app.router.add_post("/xrocket-webhook", handle_xrocket_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", WEBHOOK_PORT)
    await site.start()
    logging.info(f"Бот запущен на порту {WEBHOOK_PORT}")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
