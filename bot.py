import asyncio
import logging
import asyncpg
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ChatMemberUpdated
from aiohttp import web, ClientSession
from rollypay import RollyPayClient
from aiorocket2 import xRocketClient
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

XROCKET_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhcHBJZCI6IjMwNzc2NSIsImp0aSI6ImFwcDozMDc3NjU6YWMzYjUyODgtYWI3MS00MzUyLTgzNWEtYjRmNjVjZTMyNGQwIiwiaWF0IjoxNzkzNzE1MjA4fQ.nBCxGBwKmyaKjt4cW_jrtLBlagXxGQbvgD5c14yT2TI"
XROCKET_WEBHOOK_SECRET = "74524a9673bb8d1f18dd00eaf22eecd7daf75fbfefd664d1389cefaead0a6673"

DATABASE_URL = "postgresql://postgres.qrfqdihhoqvlaqkuyols:Koloskov2006@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"

GO6_REF_LINK = "https://t.me/Go6online_bot?start=verify_XE732NNY"

MIN_TRC20_USDT = 10.0
MIN_BEP20_USDT = 50.0

ADMIN_FEE_PER_USDT = 1.0
ADMIN_ID = 7653934214

DEFAULT_PAYMENT_SYSTEM_FEE = 9.0
DEFAULT_ADMIN_DEPOSIT_FEE = 4.0

CHAT_ID = -1001234567890

# ==================== ЮРИДИЧЕСКИЕ ССЫЛКИ ====================
OFFER_URL = "https://telegra.ph/PUBLICHNAYA-OFERTA-08-12-15"
PRIVACY_URL = "https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-08-12-99"
SUPPORT_USERNAME = "@[твой_юзернейм]"  # ⚠️ ЗАМЕНИ

# 🔐 СЕКРЕТНЫЙ КОД ПОПОЛНЕНИЯ
DEFAULT_DEPOSIT_CODE = "CHANGE_ME_2025"   # ⚠️ ЗАМЕНИ на свой код
DEPOSIT_UNLOCK_SECONDS = 1800             # 30 минут разблокировка

CATEGORY_EMOJI = {
    "Игры": "🎮",
    "Подписки": "📱",
    "AI-сервисы": "🤖",
    "Софт": "💻",
    "Обучение": "📚",
    "Другое": "📦",
}

DEMO_PRODUCTS = [
    ("Игры", "Steam ключ 500₽", "Пополнение кошелька Steam на 500 рублей", 550.0, "Ожидайте выдачи ключа, напишет администратор"),
    ("Игры", "Steam ключ 1000₽", "Пополнение кошелька Steam на 1000 рублей", 1050.0, "Ожидайте выдачи ключа, напишет администратор"),
    ("Подписки", "Spotify Premium 1 мес", "Подписка Spotify Premium на 1 месяц", 350.0, "Ожидайте выдачи, напишет администратор"),
    ("Подписки", "Netflix 1 мес", "Подписка Netflix на 1 месяц", 700.0, "Ожидайте выдачи, напишет администратор"),
    ("AI-сервисы", "ChatGPT Plus 1 мес", "Подписка ChatGPT Plus на 1 месяц", 1500.0, "Ожидайте выдачи, напишет администратор"),
]

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

rolly_client = None
rocket_client = None

# 🔐 Глобальные переменные для кода
pending_deposit_code = {}   # user_id -> True (ждём ввод кода)
deposit_unlocked = {}       # user_id -> timestamp до которого пополнение разрешено


def calculate_network_fee(network: str, amount_usdt: float) -> float:
    if network == 'TRC20': return 3.0
    elif network == 'BEP20': return (amount_usdt * 0.01) + 2.0
    return 0.0


_base_rate = 0.0
_last_rate_update = 0


async def fetch_base_rate():
    global _base_rate, _last_rate_update
    if time.time() - _last_rate_update < 60 and _base_rate > 0:
        return _base_rate
    try:
        url = "https://api.rapira.net/open/market/rates"
        async with ClientSession() as session:
            async with session.get(url, timeout=10) as resp:
                data = await resp.json()
                rate_data = next((item for item in data.get('data', []) if item.get('symbol') == 'USDT/RUB'), None)
                if rate_data:
                    base = float(rate_data.get('close') or rate_data.get('askPrice') or 0)
                    if base > 0:
                        _base_rate = base
                        _last_rate_update = time.time()
                        return _base_rate
    except Exception as e:
        logging.error(f"Ошибка курса: {e}")
    return _base_rate if _base_rate > 0 else 85.0


async def get_current_markup():
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = 'markup'")
        return float(row['value']) if row else 2.0


async def get_ps_fee() -> float:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = 'ps_fee'")
        return float(row['value']) if row else DEFAULT_PAYMENT_SYSTEM_FEE


async def get_admin_deposit_fee() -> float:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = 'admin_deposit_fee'")
        return float(row['value']) if row else DEFAULT_ADMIN_DEPOSIT_FEE


# 🔐 Получить код из БД
async def get_deposit_code() -> str:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = 'deposit_code'")
        return row['value'] if row else DEFAULT_DEPOSIT_CODE


# 🔐 Проверить разблокировку
def is_deposit_unlocked(user_id: int) -> bool:
    ts = deposit_unlocked.get(user_id, 0)
    return ts > time.time()


async def get_final_rate():
    return await fetch_base_rate() + await get_current_markup()


db_pool: asyncpg.Pool = None


async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, ssl="require", statement_cache_size=0)
    async with db_pool.acquire() as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, balance REAL DEFAULT 0, last_active TIMESTAMPTZ DEFAULT NOW());''')
        await conn.execute('''ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMPTZ DEFAULT NOW();''')
        await conn.execute('''ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;''')
        await conn.execute('''ALTER TABLE users ADD COLUMN IF NOT EXISTS verified_go6 BOOLEAN DEFAULT FALSE;''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS orders (id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount REAL, payment_id TEXT UNIQUE, status TEXT, order_type TEXT DEFAULT 'deposit', comment TEXT, message_id BIGINT, created_at TIMESTAMPTZ DEFAULT NOW());''')
        await conn.execute('''ALTER TABLE orders ADD COLUMN IF NOT EXISTS comment TEXT;''')
        await conn.execute('''ALTER TABLE orders ADD COLUMN IF NOT EXISTS message_id BIGINT;''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS allowed_users (user_id BIGINT PRIMARY KEY);''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS withdrawals (id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount_usdt REAL, amount_rub REAL, address TEXT, status TEXT DEFAULT 'pending', network TEXT DEFAULT 'BEP20', created_at TIMESTAMPTZ DEFAULT NOW());''')
        await conn.execute('''ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS network TEXT DEFAULT 'BEP20';''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS products (
            id BIGSERIAL PRIMARY KEY, category TEXT NOT NULL, name TEXT NOT NULL,
            description TEXT, price REAL NOT NULL, delivery_content TEXT,
            stock INTEGER DEFAULT -1, is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT NOW()
        );''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS purchases (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT, product_id BIGINT,
            product_name TEXT, price REAL, status TEXT DEFAULT 'completed',
            created_at TIMESTAMPTZ DEFAULT NOW()
        );''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);''')
        await conn.execute("INSERT INTO settings (key, value) VALUES ('bot_status', 'active') ON CONFLICT (key) DO NOTHING;")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('markup', '2.0') ON CONFLICT (key) DO NOTHING;")
        await conn.execute(f"INSERT INTO settings (key, value) VALUES ('ps_fee', '{DEFAULT_PAYMENT_SYSTEM_FEE}') ON CONFLICT (key) DO NOTHING;")
        await conn.execute(f"INSERT INTO settings (key, value) VALUES ('admin_deposit_fee', '{DEFAULT_ADMIN_DEPOSIT_FEE}') ON CONFLICT (key) DO NOTHING;")
        # 🔐 Дефолтный код
        await conn.execute(f"INSERT INTO settings (key, value) VALUES ('deposit_code', '{DEFAULT_DEPOSIT_CODE}') ON CONFLICT (key) DO NOTHING;")
        await conn.execute("INSERT INTO allowed_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", ADMIN_ID)
        await conn.execute("INSERT INTO users (user_id, last_active, balance) VALUES ($1, NOW(), 0) ON CONFLICT (user_id) DO NOTHING", ADMIN_ID)
        existing = await conn.fetchval("SELECT COUNT(*) FROM products")
        if existing == 0:
            for cat, name, desc, price, delivery in DEMO_PRODUCTS:
                await conn.execute("INSERT INTO products (category, name, description, price, delivery_content) VALUES ($1,$2,$3,$4,$5)",
                                   cat, name, desc, price, delivery)
            logging.info(f"Добавлено {len(DEMO_PRODUCTS)} демо-товаров")
    logging.info("БД готова!")


async def is_user_allowed(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM allowed_users WHERE user_id = $1", user_id)
        return row is not None


async def get_bot_status() -> str:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = 'bot_status'")
        return row['value'] if row else 'active'


def format_last_seen(last_active: datetime):
    if not last_active: return "Неизвестно"
    if last_active.tzinfo is None: last_active = last_active.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - last_active).total_seconds()
    if seconds < 60: return "🟢 В сети"
    elif seconds < 3600: return f"🟡 {int(seconds // 60)} мин. назад"
    elif seconds < 86400: return f"🟠 {int(seconds // 3600)} ч. назад"
    else: return f"🔴 {int(seconds // 86400)} дн. назад"


# ==================== MIDDLEWARE ====================
@dp.message.middleware()
async def track_activity_middleware(handler, event: types.Message, data):
    if event.from_user:
        async with db_pool.acquire() as conn:
            await conn.execute('''
                INSERT INTO users (user_id, last_active, username) VALUES ($1, NOW(), $2)
                ON CONFLICT (user_id) DO UPDATE SET last_active = NOW(), username = $2
            ''', event.from_user.id, event.from_user.username)
    return await handler(event, data)


@dp.message.middleware()
async def ignore_forwarded_middleware(handler, event: types.Message, data):
    if event.forward_origin: return
    return await handler(event, data)


@dp.message.middleware()
@dp.callback_query.middleware()
async def maintenance_middleware(handler, event, data):
    if event.from_user and event.from_user.id == ADMIN_ID:
        return await handler(event, data)
    status = await get_bot_status()
    if status == 'stopped':
        if isinstance(event, types.Message):
            await event.answer("🛠 **Бот временно на техобслуживании.**", parse_mode="Markdown")
        elif isinstance(event, types.CallbackQuery):
            await event.answer("🛠 Бот на техобслуживании.", show_alert=True)
        return
    return await handler(event, data)


@dp.errors()
async def errors_handler(update: types.Update, exception: Exception):
    logging.error(f"Update {update} caused error {exception}")
    return True


# ==================== ЧАТ ====================
@dp.chat_member()
async def on_chat_member_updated(event: ChatMemberUpdated):
    if event.chat.id != CHAT_ID: return
    user_id = event.new_chat_member.user.id
    if event.new_chat_member.status in ("left", "kicked"):
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("SELECT 1 FROM allowed_users WHERE user_id = $1", user_id)
            if user:
                await conn.execute("DELETE FROM allowed_users WHERE user_id = $1", user_id)
                try: await bot.send_message(ADMIN_ID, f"⚠️ `{user_id}` вышел из чата. Доступ закрыт.", parse_mode="Markdown")
                except: pass


# ==================== УПРАВЛЕНИЕ БОТОМ ====================
@dp.message(Command("stop_bot"))
async def cmd_stop_bot(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE settings SET value = 'stopped' WHERE key = 'bot_status'")
    await message.answer("⏸ Бот остановлен.")


@dp.message(Command("start_bot"))
async def cmd_start_bot(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE settings SET value = 'active' WHERE key = 'bot_status'")
    await message.answer("▶️ Бот включён.")


@dp.message(Command("setmarkup"))
async def cmd_setmarkup(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("`/setmarkup 2`")
    try:
        markup = float(command.args.replace(',', '.'))
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE settings SET value = $1 WHERE key = 'markup'", str(markup))
        await message.answer(f"✅ Наценка: **+{markup} ₽**", parse_mode="Markdown")
    except ValueError:
        await message.answer("❌ Введите число.")


@dp.message(Command("setfee"))
async def cmd_setfee(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    ps_fee = await get_ps_fee()
    admin_fee = await get_admin_deposit_fee()
    user_gets = 100 - ps_fee - admin_fee
    if not command.args:
        return await message.answer(
            f"⚙️ **Проценты:**\n🏦 ПС: **{ps_fee}%**\n👑 Твой: **{admin_fee}%**\n👤 Юзеру: **{user_gets:.2f}%**\n\n"
            f"`/setfee ps 12` | `/setfee me 5` | `/setfee 5`", parse_mode="Markdown")
    args = command.args.split()
    if len(args) == 2 and args[0].lower() in ('ps', 'me'):
        target = args[0].lower()
        try: percent = float(args[1].replace(',', '.'))
        except ValueError: return await message.answer("❌ Введите число.")
        if percent < 0: return await message.answer("❌ Отрицательный?")
        other = admin_fee if target == 'ps' else ps_fee
        if percent + other >= 100: return await message.answer(f"❌ Сумма ≥ 100%")
        key = 'ps_fee' if target == 'ps' else 'admin_deposit_fee'
        async with db_pool.acquire() as conn:
            await conn.execute(f"UPDATE settings SET value = $1 WHERE key = '{key}'", str(percent))
        return await message.answer(f"✅ **{percent}%**", parse_mode="Markdown")
    if len(args) == 1:
        try: percent = float(args[0].replace(',', '.'))
        except ValueError: return await message.answer("❌ Введите число.")
        if percent < 0 or percent + ps_fee >= 100:
            return await message.answer(f"❌ Некорректно (ПС: {ps_fee}%)")
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE settings SET value = $1 WHERE key = 'admin_deposit_fee'", str(percent))
        return await message.answer(f"✅ Твой: **{percent}%**", parse_mode="Markdown")
    await message.answer("❌ `/setfee` — справка.")


# 🔐 Управление секретным кодом
@dp.message(Command("setcode"))
async def cmd_setcode(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args:
        current = await get_deposit_code()
        return await message.answer(
            f"🔐 **Текущий код пополнения:** `{current}`\n\n"
            f"Сменить: `/setcode новый_код`\n"
            f"Пример: `/setcode MySecret2025`\n\n"
            f"Длительность разблокировки: **{DEPOSIT_UNLOCK_SECONDS // 60} мин**",
            parse_mode="Markdown")
    new_code = command.args.strip()
    if len(new_code) < 4:
        return await message.answer("❌ Код должен быть минимум 4 символа.")
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE settings SET value = $1 WHERE key = 'deposit_code'", new_code)
    deposit_unlocked.clear()
    pending_deposit_code.clear()
    await message.answer(f"✅ **Новый код:** `{new_code}`\n\nВсе разблокировки сброшены.", parse_mode="Markdown")


@dp.message(Command("lockdeposit"))
async def cmd_lockdeposit(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    deposit_unlocked.clear()
    pending_deposit_code.clear()
    await message.answer("🔒 Все разблокировки пополнения сброшены.")


# ==================== БЕЛЫЙ СПИСОК ====================
@dp.message(Command("allow"))
async def cmd_allow(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("`/allow 123456789`")
    try:
        uid = int(command.args.strip())
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO allowed_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", uid)
            await conn.execute("INSERT INTO users (user_id, last_active) VALUES ($1, NOW()) ON CONFLICT DO NOTHING", uid)
        await message.answer(f"✅ `{uid}` добавлен.")
    except ValueError: await message.answer("❌ ID цифрами.")


@dp.message(Command("deny"))
async def cmd_deny(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("`/deny 123456789`")
    try:
        uid = int(command.args.strip())
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM allowed_users WHERE user_id = $1", uid)
        await message.answer(f"🚫 `{uid}` заблокирован.")
    except ValueError: await message.answer("❌ ID цифрами.")


# ==================== БАЛАНС ====================
@dp.message(Command("add"))
async def cmd_add(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    try:
        uid, amount = int(command.args.split()[0]), float(command.args.split()[1])
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, uid)
        await message.answer(f"✅ +{amount} ₽ → `{uid}`")
    except: pass


@dp.message(Command("deduct"))
async def cmd_deduct(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    try:
        uid, amount = int(command.args.split()[0]), float(command.args.split()[1])
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", amount, uid)
        await message.answer(f"✅ Списано {amount} ₽")
        try: await bot.send_message(uid, f"⚠️ Списано {amount:.2f} ₽")
        except: pass
    except: pass


@dp.message(Command("quiet_deduct"))
async def cmd_quiet_deduct(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    try:
        uid, amount = int(command.args.split()[0]), float(command.args.split()[1])
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", amount, uid)
        await message.answer(f"✅ Списано {amount} ₽ (тихо)")
    except: pass


# ==================== ПОПОЛНЕНИЕ / ВЫВОД ====================
@dp.message(Command("usdt"))
async def cmd_deposit_crypto(message: types.Message, command: CommandObject):
    global rocket_client
    if not await is_user_allowed(message.from_user.id): return
    # 🔐 Проверка разблокировки
    if message.from_user.id != ADMIN_ID and not is_deposit_unlocked(message.from_user.id):
        return await message.answer("🔐 Для пополнения нужен секретный код.\nНажмите «💰 Пополнить баланс» в меню.")
    if not command.args: return await message.answer("`/usdt 10`")
    try:
        amount_usdt = float(command.args.replace(',', '.'))
        if amount_usdt < 1: raise ValueError
    except: return await message.answer("Ошибка! Минимум 1 USDT.")
    try:
        async with db_pool.acquire() as conn:
            check_id = await conn.fetchval(
                "INSERT INTO orders (user_id, amount, status, order_type) VALUES ($1, $2, 'pending', 'crypto') RETURNING id",
                message.from_user.id, amount_usdt)
        invoice = await rocket_client.create_invoice(
            currency="USDT", amount=amount_usdt,
            description=f"Чек #{check_id}", payload=f"{message.from_user.id}_{check_id}", expired_in=3600)
        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Оплатить в xRocket", url=invoice.link)
        sent_msg = await message.answer(f"💵 **Чек #{check_id} на {amount_usdt} USDT**", reply_markup=builder.as_markup(), parse_mode="Markdown")
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE orders SET payment_id = $1, message_id = $2 WHERE id = $3", invoice.invoice_id, sent_msg.message_id, check_id)
    except Exception as e:
        logging.error(f"xRocket Error: {e}")
        await message.answer("❌ Ошибка создания счёта.")


@dp.message(Command("new"))
async def cmd_new(message: types.Message, command: CommandObject):
    global rolly_client
    if not await is_user_allowed(message.from_user.id): return
    # 🔐 Проверка разблокировки
    if message.from_user.id != ADMIN_ID and not is_deposit_unlocked(message.from_user.id):
        return await message.answer("🔐 Для пополнения нужен секретный код.\nНажмите «💰 Пополнить баланс» в меню.")
    if not command.args: return await message.answer("`/new 500`")
    parts = command.args.split(maxsplit=1)
    try: amount = float(parts[0].replace(',', '.'))
    except: return await message.answer("Ошибка!")
    comment = parts[1] if len(parts) > 1 else None
    try:
        async with db_pool.acquire() as conn:
            check_id = await conn.fetchval(
                "INSERT INTO orders (user_id, amount, status, comment) VALUES ($1, $2, 'pending', $3) RETURNING id",
                message.from_user.id, amount, comment)
        desc_text = f"Чек #{check_id}"
        if comment: desc_text += f" | {comment}"
        payment = rolly_client.payments.create(
            amount=str(amount), order_id=f"dep_{message.from_user.id}_{check_id}",
            terminal_id=TERMINAL_ID, description=desc_text,
            customer_id=str(message.from_user.id), redirect_url="https://t.me")
        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Перейти к оплате", url=payment['pay_url'])
        msg_text = f"**Счёт: {amount:.2f} ₽**"
        if comment: msg_text += f"\n📝 {comment}"
        sent_msg = await message.answer(f"{msg_text}\n🟡 Ожидает оплату...", reply_markup=builder.as_markup(), parse_mode="Markdown")
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE orders SET payment_id = $1, message_id = $2 WHERE id = $3", payment['payment_id'], sent_msg.message_id, check_id)
    except Exception as e:
        logging.error(f"RollyPay Error: {e}")
        await message.answer("Ошибка создания платежа.")


@dp.message(Command("withdraw"))
async def cmd_withdraw(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id): return
    if not command.args or len(command.args.split()) < 3:
        return await message.answer("`/withdraw [сумма] [BEP20|TRC20] [адрес]`")
    try:
        args = command.args.split()
        amount_usdt = float(args[0].replace(',', '.'))
        network = args[1].upper().strip()
        address = args[2].strip()
        if network not in ['BEP20', 'TRC20']: return await message.answer("❌ Неверная сеть.")
        if network == 'TRC20' and amount_usdt < MIN_TRC20_USDT: return await message.answer(f"❌ Мин: {MIN_TRC20_USDT}")
        if network == 'BEP20' and amount_usdt < MIN_BEP20_USDT: return await message.answer(f"❌ Мин: {MIN_BEP20_USDT}")
    except: return await message.answer("Ошибка!")
    fee = calculate_network_fee(network, amount_usdt)
    total_usdt = amount_usdt + fee
    rate = await get_final_rate()
    total_rub = total_usdt * rate
    profit_rub = amount_usdt * ADMIN_FEE_PER_USDT
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", message.from_user.id)
        bal = row['balance'] if row and row['balance'] else 0.0
        if bal < total_rub: return await message.answer("❌ Недостаточно средств.")
        today_wths = await conn.fetchval(
            "SELECT COALESCE(SUM(amount_rub), 0) FROM withdrawals WHERE user_id = $1 AND status IN ('pending','processing','completed') AND created_at >= CURRENT_DATE",
            message.from_user.id)
        max_limit = bal / 2.0
        if today_wths + total_rub > max_limit:
            return await message.answer(f"❌ Лимит: 50% баланса/сутки.\nБаланс: {bal:.2f}₽\nЛимит: {max_limit:.2f}₽\nВыведено: {today_wths:.2f}₽")
        await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", total_rub, message.from_user.id)
        await conn.execute("INSERT INTO withdrawals (user_id, amount_usdt, amount_rub, address, network) VALUES ($1,$2,$3,$4,$5)",
                          message.from_user.id, amount_usdt, total_rub, address, network)
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", profit_rub, ADMIN_ID)
        w_id = (await conn.fetchrow("SELECT lastval()"))[0]
    await message.answer(f"✅ **Заявка #{w_id}**\n{network} | {amount_usdt:.2f} USDT\nСписано: {total_rub:.2f} ₽")
    builder = InlineKeyboardBuilder()
    builder.button(text="🟢 В работу", callback_data=f"admin_take_{w_id}")
    builder.button(text="🔴 Отклонить", callback_data=f"admin_reject_{w_id}")
    await bot.send_message(ADMIN_ID, f"💸 **ЗАЯВКА #{w_id}**\nID: `{message.from_user.id}`\n{network}\n{amount_usdt} USDT\n`{address}`\n+{profit_rub} ₽", reply_markup=builder.as_markup())


# ==================== ВЕРИФИКАЦИЯ ====================
@dp.message(F.photo)
async def handle_verification_photo(message: types.Message):
    if await is_user_allowed(message.from_user.id): return
    user = message.from_user
    async with db_pool.acquire() as conn:
        await conn.execute('''INSERT INTO users (user_id, last_active, username) VALUES ($1, NOW(), $2) ON CONFLICT (user_id) DO UPDATE SET last_active = NOW(), username = $2''', user.id, user.username)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Одобрить", callback_data=f"approve_access_{user.id}")
    builder.button(text="❌ Отклонить", callback_data=f"reject_access_{user.id}")
    try:
        await bot.send_photo(chat_id=ADMIN_ID, photo=message.photo[-1].file_id,
            caption=f"🔔 **ЗАЯВКА**\n👤 {user.full_name}\n🔗 @{user.username}\n🆔 `{user.id}`",
            reply_markup=builder.as_markup(), parse_mode="Markdown")
        await message.answer("⏳ Ожидайте проверки.")
    except: pass


@dp.callback_query(F.data.startswith("approve_access_"))
async def process_approve(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    tid = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO allowed_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", tid)
        await conn.execute("UPDATE users SET verified_go6 = TRUE WHERE user_id = $1", tid)
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ ОДОБРЕНО", reply_markup=None)
    try: await bot.send_message(tid, "✅ Доступ одобрен! Напишите /start")
    except: pass


@dp.callback_query(F.data.startswith("reject_access_"))
async def process_reject(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    tid = int(callback.data.split("_")[2])
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ ОТКЛОНЕНО", reply_markup=None)
    try: await bot.send_message(tid, "❌ В доступе отказано.")
    except: pass


# ==================== 🔐 ВВОД СЕКРЕТНОГО КОДА ====================
# ВАЖНО: этот хендлер должен быть зарегистрирован ДО остальных текстовых,
# иначе /start и другие команды будут перехвачены. Фильтр ~F.text.startswith("/") это гарантирует.
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_deposit_code_input(message: types.Message):
    user_id = message.from_user.id
    # Если не ждём код от этого юзера — молча игнорируем
    if user_id not in pending_deposit_code:
        return
    code = message.text.strip()
    real_code = await get_deposit_code()

    if code == real_code:
        # Успех — разблокируем на 30 минут
        pending_deposit_code.pop(user_id, None)
        deposit_unlocked[user_id] = time.time() + DEPOSIT_UNLOCK_SECONDS
        rate = await get_final_rate()
        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Банковская карта", callback_data="shop_dep_card")
        builder.button(text="🪙 USDT (xRocket)", callback_data="shop_dep_usdt")
        builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="shop_main"))
        await message.answer(
            f"✅ **Код принят!**\n\nДоступ к пополнению открыт на **{DEPOSIT_UNLOCK_SECONDS // 60} минут**.\n\n"
            f"Курс: 1 USDT = {rate:.2f} ₽\n\nВыберите способ:",
            reply_markup=builder.as_markup(), parse_mode="Markdown")
    else:
        # Неверный код — не убираем из pending, пусть пробует ещё
        await message.answer("❌ **Неверный код.** Попробуйте снова.")
        try:
            await bot.send_message(ADMIN_ID, f"⚠️ Неверный код от `{user_id}`: `{code[:50]}`", parse_mode="Markdown")
        except: pass


# ==================== ГЛАВНОЕ МЕНЮ ====================
async def build_main_menu():
    rate = await get_final_rate()
    builder = InlineKeyboardBuilder()
    builder.button(text="🛒 Каталог товаров", callback_data="shop_catalog")
    builder.button(text="💰 Пополнить баланс", callback_data="shop_deposit")
    builder.button(text="👤 Профиль", callback_data="shop_profile")
    builder.row(
        InlineKeyboardButton(text="📄 Публичная оферта", url=OFFER_URL),
        InlineKeyboardButton(text="🔒 Политика конфиденциальности", url=PRIVACY_URL)
    )
    builder.adjust(1, 1, 1, 2)
    text = (
        f"👋 **Добро пожаловать!**\n\n"
        f"Курс пополнения: **1 USDT = {rate:.2f} ₽**\n\n"
        f"Выберите действие:"
    )
    return text, builder.as_markup()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not await is_user_allowed(message.from_user.id):
        return await message.answer(f"👋 Для доступа:\n1️⃣ Регистрация на GO6: {GO6_REF_LINK}\n2️⃣ Укажите ментора.\n3️⃣ Отправьте скрин сюда.")
    text, kb = await build_main_menu()
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@dp.callback_query(F.data == "shop_main")
async def cb_shop_main(callback: CallbackQuery):
    text, kb = await build_main_menu()
    await callback.message.edit_text(text, reply_markup=kb, parse_mode="Markdown")


@dp.callback_query(F.data == "shop_catalog")
async def cb_shop_catalog(callback: CallbackQuery):
    async with db_pool.acquire() as conn:
        cats = await conn.fetch("SELECT DISTINCT category FROM products WHERE is_active = TRUE ORDER BY category")
    if not cats:
        return await callback.answer("Каталог пуст", show_alert=True)
    builder = InlineKeyboardBuilder()
    for c in cats:
        emoji = CATEGORY_EMOJI.get(c['category'], '📦')
        builder.button(text=f"{emoji} {c['category']}", callback_data=f"shop_cat_{c['category']}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="shop_main"))
    await callback.message.edit_text("🛒 **Каталог**\n\nВыберите категорию:", reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("shop_cat_"))
async def cb_shop_category(callback: CallbackQuery):
    category = callback.data.replace("shop_cat_", "")
    async with db_pool.acquire() as conn:
        products = await conn.fetch("SELECT id, name, price FROM products WHERE category = $1 AND is_active = TRUE ORDER BY id", category)
    if not products:
        return await callback.answer("Нет товаров", show_alert=True)
    emoji = CATEGORY_EMOJI.get(category, '📦')
    builder = InlineKeyboardBuilder()
    for p in products:
        builder.button(text=f"{p['name']} — {p['price']:.0f} ₽", callback_data=f"shop_view_{p['id']}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text="🔙 К каталогу", callback_data="shop_catalog"))
    await callback.message.edit_text(f"{emoji} **{category}**\n\nВыберите:", reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("shop_view_"))
async def cb_shop_view(callback: CallbackQuery):
    pid = int(callback.data.replace("shop_view_", ""))
    async with db_pool.acquire() as conn:
        product = await conn.fetchrow("SELECT * FROM products WHERE id = $1 AND is_active = TRUE", pid)
    if not product:
        return await callback.answer("Недоступен", show_alert=True)
    text = f"📦 **{product['name']}**\n\n{product['description'] or '_Без описания_'}\n\n💰 Цена: **{product['price']:.0f} ₽**"
    if product['stock'] > 0: text += f"\n📊 Осталось: {product['stock']} шт."
    builder = InlineKeyboardBuilder()
    builder.button(text=f"💳 Купить за {product['price']:.0f} ₽", callback_data=f"shop_buy_{pid}")
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data=f"shop_cat_{product['category']}"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("shop_buy_"))
async def cb_shop_buy(callback: CallbackQuery):
    pid = int(callback.data.replace("shop_buy_", ""))
    user_id = callback.from_user.id
    async with db_pool.acquire() as conn:
        product = await conn.fetchrow("SELECT * FROM products WHERE id = $1 AND is_active = TRUE", pid)
        if not product: return await callback.answer("Недоступен", show_alert=True)
        if product['stock'] == 0: return await callback.answer("Распродан", show_alert=True)
        user = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
        bal = user['balance'] if user and user['balance'] else 0.0
        if bal < product['price']:
            return await callback.answer(f"❌ Недостаточно!\nНужно: {product['price']:.0f}₽\nУ вас: {bal:.2f}₽", show_alert=True)
        await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", product['price'], user_id)
        await conn.execute("INSERT INTO purchases (user_id, product_id, product_name, price) VALUES ($1,$2,$3,$4)",
                          user_id, pid, product['name'], product['price'])
        if product['stock'] > 0:
            await conn.execute("UPDATE products SET stock = stock - 1 WHERE id = $1", pid)
            await conn.execute("UPDATE products SET is_active = FALSE WHERE id = $1 AND stock = 0", pid)
    delivery = product['delivery_content'] or "Свяжитесь с администратором."
    await callback.message.answer(
        f"✅ **Покупка успешна!**\n\n📦 {product['name']}\n💰 {product['price']:.0f} ₽\n\n🎁 **Ваш товар:**\n{delivery}",
        parse_mode="Markdown")
    try:
        uname = f"@{callback.from_user.username}" if callback.from_user.username else f"ID:{user_id}"
        await bot.send_message(ADMIN_ID, f"🛒 **Покупка!**\n👤 {uname}\n📦 {product['name']}\n💰 {product['price']:.0f} ₽", parse_mode="Markdown")
    except: pass
    await callback.answer("✅ Оформлено!")


# 🔐 Пополнение теперь требует код
@dp.callback_query(F.data == "shop_deposit")
async def cb_shop_deposit(callback: CallbackQuery):
    user_id = callback.from_user.id
    # Админ или разблокированный — пускаем сразу
    if user_id == ADMIN_ID or is_deposit_unlocked(user_id):
        rate = await get_final_rate()
        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Банковская карта", callback_data="shop_dep_card")
        builder.button(text="🪙 USDT (xRocket)", callback_data="shop_dep_usdt")
        builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="shop_main"))
        await callback.message.edit_text(
            f"💰 **Пополнение**\n\nКурс: 1 USDT = {rate:.2f} ₽\n\nВыберите способ:",
            reply_markup=builder.as_markup(), parse_mode="Markdown")
        return
    # Иначе — ждём код
    pending_deposit_code[user_id] = True
    await callback.message.edit_text(
        "🔐 **Пополнение закрыто**\n\n"
        "Для получения доступа к пополнению отправьте **секретный код** одним сообщением.\n\n"
        "Если у вас нет кода — обратитесь в поддержку.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="shop_main")]]),
        parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data == "shop_dep_card")
async def cb_shop_dep_card(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID and not is_deposit_unlocked(callback.from_user.id):
        return await callback.answer("🔐 Введите код через «Пополнить баланс»", show_alert=True)
    await callback.message.answer("💳 Сумма в рублях:\n`/new 500`", parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data == "shop_dep_usdt")
async def cb_shop_dep_usdt(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID and not is_deposit_unlocked(callback.from_user.id):
        return await callback.answer("🔐 Введите код через «Пополнить баланс»", show_alert=True)
    await callback.message.answer("🪙 Сумма в USDT:\n`/usdt 10`", parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data == "shop_profile")
async def cb_shop_profile(callback: CallbackQuery):
    rate = await get_final_rate()
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", callback.from_user.id)
        bal = user['balance'] if user and user['balance'] else 0.0
        count = await conn.fetchval("SELECT COUNT(*) FROM purchases WHERE user_id = $1", callback.from_user.id)
    text = (
        f"👤 **Профиль**\n\n"
        f"🆔 `{callback.from_user.id}`\n"
        f"💰 Баланс: **{bal:.2f} ₽** (~{bal / rate:.2f} USDT)\n"
        f"🛒 Покупок: **{count}**")
    builder = InlineKeyboardBuilder()
    builder.button(text="📜 История покупок", callback_data="shop_history")
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="shop_main"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data == "shop_history")
async def cb_shop_history(callback: CallbackQuery):
    async with db_pool.acquire() as conn:
        purchases = await conn.fetch("SELECT product_name, price, created_at FROM purchases WHERE user_id = $1 ORDER BY created_at DESC LIMIT 10", callback.from_user.id)
    if not purchases:
        text = "📜 Пока нет покупок."
    else:
        text = "📜 **Последние покупки:**\n\n"
        for p in purchases:
            text += f"• {p['product_name']} — {p['price']:.0f} ₽ ({p['created_at'].strftime('%d.%m.%Y')})\n"
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="shop_profile"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


# ==================== ЮРИДИЧЕСКИЕ КОМАНДЫ ====================
@dp.message(Command("offer"))
async def cmd_offer(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="📄 Открыть публичную оферту", url=OFFER_URL)
    await message.answer("📄 **Публичная оферта**\n\nНажмите на кнопку ниже:",
        reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.message(Command("privacy"))
async def cmd_privacy(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="🔒 Открыть политику конфиденциальности", url=PRIVACY_URL)
    await message.answer("🔒 **Политика конфиденциальности**\n\nНажмите на кнопку ниже:",
        reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    builder = InlineKeyboardBuilder()
    builder.button(text="📄 Оферта", url=OFFER_URL)
    builder.button(text="🔒 Политика", url=PRIVACY_URL)
    await message.answer(
        "📚 **Помощь**\n\n"
        "🛒 /start — магазин\n"
        "💰 /balance — баланс\n"
        "📄 /offer — публичная оферта\n"
        "🔒 /privacy — политика конфиденциальности\n\n"
        f"Поддержка: {SUPPORT_USERNAME}",
        reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    if not await is_user_allowed(message.from_user.id): return
    rate = await get_final_rate()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", message.from_user.id)
        bal = row['balance'] if row and row['balance'] else 0.0
    await message.answer(f"💰 **Баланс:** {bal:.2f} ₽\n({bal / rate:.2f} USDT)", parse_mode="Markdown")


# ==================== УПРАВЛЕНИЕ ТОВАРАМИ ====================
@dp.message(Command("addproduct"))
async def cmd_addproduct(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args:
        return await message.answer(
            "**Формат:**\n`/addproduct Категория | Название | Описание | Цена | Что выдаём`\n\n"
            "**Пример:**\n`/addproduct Игры | Steam 500 | Пополнение | 550 | Код: XXXX`",
            parse_mode="Markdown")
    parts = [p.strip() for p in command.args.split("|")]
    if len(parts) < 4:
        return await message.answer("❌ Нужно: Категория | Название | Описание | Цена [| Выдача]")
    category, name, desc, price_str = parts[0], parts[1], parts[2], parts[3]
    delivery = parts[4] if len(parts) > 4 else None
    try: price = float(price_str.replace(',', '.'))
    except: return await message.answer("❌ Неверная цена")
    async with db_pool.acquire() as conn:
        pid = await conn.fetchval("INSERT INTO products (category, name, description, price, delivery_content) VALUES ($1,$2,$3,$4,$5) RETURNING id",
                                  category, name, desc, price, delivery)
    await message.answer(f"✅ Товар **#{pid}** добавлен в «{category}»", parse_mode="Markdown")


@dp.message(Command("products"))
async def cmd_products(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    async with db_pool.acquire() as conn:
        prods = await conn.fetch("SELECT id, category, name, price, stock, is_active FROM products ORDER BY category, id")
    if not prods: return await message.answer("Нет товаров. Добавь /addproduct")
    text = "📦 **Все товары:**\n\n"
    for p in prods:
        status = "✅" if p['is_active'] else "🚫"
        stock = f" | stock: {p['stock']}" if p['stock'] > 0 else ""
        text += f"{status} `#{p['id']}` {p['category']} → {p['name']} — {p['price']:.0f}₽{stock}\n"
    await message.answer(text, parse_mode="Markdown")


@dp.message(Command("delproduct"))
async def cmd_delproduct(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("`/delproduct 5`")
    try: pid = int(command.args.strip())
    except: return await message.answer("❌ ID цифрой")
    async with db_pool.acquire() as conn:
        result = await conn.execute("DELETE FROM products WHERE id = $1", pid)
    if result == "DELETE 0": return await message.answer(f"❌ Не найден")
    await message.answer(f"✅ Товар #{pid} удалён")


# ==================== АДМИН-ПАНЕЛЬ ====================
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    builder = InlineKeyboardBuilder()
    builder.button(text="📈 Статистика", callback_data="admin_stats")
    builder.button(text="👥 Пользователи", callback_data="admin_users_0")
    builder.button(text="🛡️ GO6", callback_data="admin_verified_0")
    builder.button(text="📦 Товары", callback_data="admin_products_0")
    builder.button(text="📋 Заказы", callback_data="admin_purchases_0")
    await message.answer("👑 **Админ-панель:**", reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    rate = await get_final_rate()
    markup = await get_current_markup()
    admin_fee = await get_admin_deposit_fee()
    ps_fee = await get_ps_fee()
    async with db_pool.acquire() as conn:
        u = await conn.fetchval("SELECT COUNT(*) FROM allowed_users") or 0
        d = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid'") or 0.0
        w = await conn.fetchval("SELECT COALESCE(SUM(amount_usdt),0) FROM withdrawals WHERE status='completed'") or 0.0
        p = await conn.fetchval("SELECT COALESCE(SUM(amount_usdt * $1),0) FROM withdrawals WHERE status!='cancelled'", ADMIN_FEE_PER_USDT) or 0.0
        pur = await conn.fetchval("SELECT COUNT(*) FROM purchases") or 0
        pur_sum = await conn.fetchval("SELECT COALESCE(SUM(price),0) FROM purchases") or 0.0
        active_w = await conn.fetchval("SELECT COUNT(*) FROM withdrawals WHERE status IN ('pending','processing')") or 0
        admin_bal = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1", ADMIN_ID) or 0.0
        prod_count = await conn.fetchval("SELECT COUNT(*) FROM products WHERE is_active = TRUE") or 0
    text = (
        f"📊 **СТАТИСТИКА**\n\n"
        f"👥 Пользователей: **{u}**\n"
        f"💰 Пополнено: **{d:.2f} ₽**\n"
        f"💸 Выведено: **{w:.2f} USDT**\n"
        f"⏳ Активных заявок: **{active_w}**\n\n"
        f"🛒 **Магазин:**\n"
        f"📦 Товаров: **{prod_count}**\n"
        f"🎁 Покупок: **{pur}** на **{pur_sum:.2f} ₽**\n\n"
        f"👑 **ВАШ ДОХОД:**\n"
        f"📈 С выводов: **{p:.2f} ₽**\n"
        f"📥 С пополнений ({admin_fee}%): **{d * admin_fee / 100:.2f} ₽**\n"
        f"💳 Ваш баланс: **{admin_bal:.2f} ₽**\n\n"
        f"💱 Курс: **Рапира + {markup} = {rate:.2f} ₽**\n"
        f"📥 ПС: {ps_fee}% | Твой: {admin_fee}%")
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_main")]]), parse_mode="Markdown")


@dp.callback_query(F.data == "admin_main")
async def cb_admin_main(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    builder = InlineKeyboardBuilder()
    builder.button(text="📈 Статистика", callback_data="admin_stats")
    builder.button(text="👥 Пользователи", callback_data="admin_users_0")
    builder.button(text="🛡️ GO6", callback_data="admin_verified_0")
    builder.button(text="📦 Товары", callback_data="admin_products_0")
    builder.button(text="📋 Заказы", callback_data="admin_purchases_0")
    await callback.message.edit_text("👑 **Админ-панель:**", reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("admin_products_"))
async def cb_admin_products(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    try: page = int(callback.data.split("_")[2])
    except: page = 0
    limit, offset = 8, page * 8
    async with db_pool.acquire() as conn:
        prods = await conn.fetch("SELECT id, category, name, price, is_active FROM products ORDER BY id LIMIT $1 OFFSET $2", limit, offset)
        total = await conn.fetchval("SELECT COUNT(*) FROM products") or 0
    keyboard = []
    for p in prods:
        emoji = "✅" if p['is_active'] else "🚫"
        keyboard.append([InlineKeyboardButton(text=f"{emoji} #{p['id']} {p['name']} — {p['price']:.0f}₽", callback_data=f"admin_prod_toggle_{p['id']}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_products_{page-1}"))
    if offset + limit < total: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_products_{page+1}"))
    if nav: keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="➕ Добавить (команда)", callback_data="admin_prod_help")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_main")])
    await callback.message.edit_text(f"📦 **Товары** (Стр. {page+1}):\n_Нажмите — вкл/выкл_", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("admin_prod_toggle_"))
async def cb_admin_prod_toggle(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    pid = int(callback.data.split("_")[3])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE products SET is_active = NOT is_active WHERE id = $1", pid)
        row = await conn.fetchrow("SELECT is_active FROM products WHERE id = $1", pid)
    status = "включён" if row['is_active'] else "выключен"
    await callback.answer(f"✅ #{pid} {status}")
    await cb_admin_products(callback)


@dp.callback_query(F.data == "admin_prod_help")
async def cb_admin_prod_help(callback: CallbackQuery):
    await callback.message.answer(
        "**Добавить:**\n`/addproduct Категория | Название | Описание | Цена | Что выдаём`\n\n"
        "**Удалить:** `/delproduct ID`\n**Список:** `/products`",
        parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_purchases_"))
async def cb_admin_purchases(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    try: page = int(callback.data.split("_")[2])
    except: page = 0
    limit, offset = 8, page * 8
    async with db_pool.acquire() as conn:
        purchases = await conn.fetch("SELECT product_name, price, user_id, created_at FROM purchases ORDER BY created_at DESC LIMIT $1 OFFSET $2", limit, offset)
        total = await conn.fetchval("SELECT COUNT(*) FROM purchases") or 0
    text = f"📋 **Заказы** (Стр. {page+1}):\n\n"
    if not purchases: text += "Нет покупок."
    for p in purchases:
        text += f"• {p['product_name']} — {p['price']:.0f}₽ | `{p['user_id']}`\n"
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_purchases_{page-1}"))
    if offset + limit < total: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_purchases_{page+1}"))
    nav.append(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_main"))
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[nav]), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("admin_users_"))
async def cb_admin_users(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    try: page = int(callback.data.split("_")[2])
    except: page = 0
    limit, offset = 5, page * 5
    async with db_pool.acquire() as conn:
        users = await conn.fetch("""
            SELECT u.user_id, u.username, u.balance, u.last_active,
                   (SELECT COALESCE(SUM(amount),0) FROM orders WHERE user_id=u.user_id AND status='paid' AND created_at>=CURRENT_DATE) as dep_today
            FROM users u JOIN allowed_users a ON u.user_id=a.user_id
            ORDER BY u.last_active DESC LIMIT $1 OFFSET $2""", limit, offset)
        total = await conn.fetchval("SELECT COUNT(*) FROM allowed_users") or 0
    keyboard = []
    for u in users:
        bal = u['balance'] or 0.0
        dep = u['dep_today'] or 0.0
        name = f"@{u['username']}" if u['username'] else f"ID:{u['user_id']}"
        keyboard.append([InlineKeyboardButton(text=f"👤 {name} | {bal:.0f}₽ | 📥{dep:.0f}₽", callback_data=f"admin_user_view_{u['user_id']}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_users_{page-1}"))
    if offset + limit < total: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_users_{page+1}"))
    if nav: keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_main")])
    await callback.message.edit_text(f"👥 **Пользователи** (Стр. {page+1}):", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("admin_verified_"))
async def cb_admin_verified(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    try: page = int(callback.data.split("_")[2])
    except: page = 0
    limit, offset = 5, page * 5
    async with db_pool.acquire() as conn:
        users = await conn.fetch("SELECT user_id, username, balance, last_active FROM users WHERE verified_go6 = TRUE ORDER BY last_active DESC LIMIT $1 OFFSET $2", limit, offset)
        total = await conn.fetchval("SELECT COUNT(*) FROM users WHERE verified_go6 = TRUE") or 0
    keyboard = []
    for u in users:
        bal = u['balance'] or 0.0
        name = f"@{u['username']}" if u['username'] else f"ID:{u['user_id']}"
        keyboard.append([InlineKeyboardButton(text=f"🛡️ {name} | {bal:.0f}₽", callback_data=f"admin_user_view_{u['user_id']}")])
    nav = []
    if page > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_verified_{page-1}"))
    if offset + limit < total: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_verified_{page+1}"))
    if nav: keyboard.append(nav)
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_main")])
    await callback.message.edit_text(f"🛡️ **GO6** (Стр. {page+1}):", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("admin_user_view_"))
async def cb_admin_user_view(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    tid = int(callback.data.split("_")[3])
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT username, balance, last_active, verified_go6 FROM users WHERE user_id = $1", tid)
        if not user: return await callback.answer("Не найден", show_alert=True)
        total_in = await conn.fetchval("SELECT COALESCE(SUM(amount),0) FROM orders WHERE user_id=$1 AND status='paid'", tid) or 0.0
        total_out = await conn.fetchval("SELECT COALESCE(SUM(amount_usdt),0) FROM withdrawals WHERE user_id=$1 AND status='completed'", tid) or 0.0
        pur_count = await conn.fetchval("SELECT COUNT(*) FROM purchases WHERE user_id=$1", tid) or 0
        pur_sum = await conn.fetchval("SELECT COALESCE(SUM(price),0) FROM purchases WHERE user_id=$1", tid) or 0.0
    bal = user['balance'] or 0.0
    name = f"@{user['username']}" if user['username'] else "Скрыт"
    v = "✅" if user['verified_go6'] else "❌"
    text = (
        f"👤 **Досье** `{tid}`\n\n"
        f"🔗 {name}\n🛡️ Верификация: **{v}**\n"
        f"🕒 {format_last_seen(user['last_active'])}\n"
        f"💰 Баланс: **{bal:.2f} ₽**\n\n"
        f"📥 Пополнил: **{total_in:.2f} ₽**\n"
        f"📤 Вывел: **{total_out:.2f} USDT**\n"
        f"🛒 Покупок: **{pur_count}** на **{pur_sum:.2f} ₽**")
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="📝 Списать", callback_data=f"admin_deduct_info_{tid}"), InlineKeyboardButton(text="🚫 Блок", callback_data=f"admin_block_{tid}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_users_0"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")


@dp.callback_query(F.data.startswith("admin_deduct_info_"))
async def cb_deduct_info(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    tid = int(callback.data.split("_")[3])
    await callback.message.answer(
        f"🔔 `/deduct {tid} 500` — с уведомлением\n🔕 `/quiet_deduct {tid} 500` — без",
        parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data.startswith("admin_block_"))
async def cb_admin_block(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    tid = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM allowed_users WHERE user_id = $1", tid)
    await callback.message.edit_text(f"🚫 `{tid}` заблокирован.", parse_mode="Markdown")


@dp.callback_query(F.data.startswith("admin_take_"))
async def process_admin_take(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    w_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE withdrawals SET status='processing' WHERE id=$1", w_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Выполнено", callback_data=f"admin_done_{w_id}")
    builder.button(text="🔴 Отклонить", callback_data=f"admin_reject_{w_id}")
    await callback.message.edit_reply_markup(reply_markup=builder.as_markup())


@dp.callback_query(F.data.startswith("admin_reject_"))
async def process_admin_reject(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    w_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT user_id, amount_rub, amount_usdt FROM withdrawals WHERE id=$1", w_id)
            await conn.execute("UPDATE withdrawals SET status='rejected' WHERE id=$1", w_id)
            await conn.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", row['amount_rub'], row['user_id'])
            await conn.execute("UPDATE users SET balance=balance-$1 WHERE user_id=$2", row['amount_usdt'] * ADMIN_FEE_PER_USDT, ADMIN_ID)
    await callback.message.edit_text(f"🔴 #{w_id} отклонена.")


@dp.callback_query(F.data.startswith("admin_done_"))
async def process_admin_done(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    w_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE withdrawals SET status='completed' WHERE id=$1", w_id)
    await callback.message.edit_text(f"✅ #{w_id} выполнена.")


# ==================== ВЕБХУКИ ====================
async def handle_rollypay_webhook(request: web.Request):
    try:
        raw_body = await request.read()
        sig = request.headers.get("X-Signature")
        ts = request.headers.get("X-Timestamp")
        msg = f"{ts}.{raw_body.decode('utf-8')}"
        expected = hmac.new(SIGNING_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected): return web.Response(text="Invalid", status=403)
        data = json.loads(raw_body)
        if data.get("status") == "paid":
            payment_id = data.get("payment_id")
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "UPDATE orders SET status='paid' WHERE payment_id=$1 AND status='pending' RETURNING id, user_id, amount, comment, message_id",
                    payment_id)
                if row:
                    check_id, user_id, amount = row['id'], row['user_id'], row['amount']
                    comment, msg_id = row['comment'], row['message_id']
                    user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
                    username = f"@{user_row['username']}" if user_row and user_row['username'] else f"ID:{user_id}"
                    admin_fee_pct = await get_admin_deposit_fee()
                    ps_fee_pct = await get_ps_fee()
                    fee_ps = amount * (ps_fee_pct / 100.0)
                    fee_admin = amount * (admin_fee_pct / 100.0)
                    net = amount - fee_ps - fee_admin
                    await conn.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", net, user_id)
                    await conn.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", fee_admin, ADMIN_ID)
                    reply = "Оплата произведена"
                    if msg_id:
                        try: await bot.send_message(user_id, reply, reply_to_message_id=msg_id, allow_sending_without_reply=True)
                        except:
                            try: await bot.send_message(user_id, reply)
                            except: pass
                    else:
                        try: await bot.send_message(user_id, reply)
                        except: pass
                    admin_msg = f"🔔 Чек #{check_id}\n👤 {username}\n💵 {amount:.2f} ₽\n"
                    if comment: admin_msg += f"📝 {comment}\n"
                    admin_msg += f"\n🏦 ПС ({ps_fee_pct}%): {fee_ps:.2f} ₽\n👑 Тебе ({admin_fee_pct}%): {fee_admin:.2f} ₽\n👤 Юзеру: {net:.2f} ₽"
                    try: await bot.send_message(ADMIN_ID, admin_msg, parse_mode="Markdown")
                    except: pass
        return web.Response(text="OK", status=200)
    except Exception as e:
        logging.error(f"Webhook Error: {e}")
        return web.Response(text="Error", status=500)


async def handle_xrocket_webhook(request: web.Request):
    try:
        raw_body = await request.read()
        sig = request.headers.get("Signature")
        sig_ts = request.headers.get("Signature-Timestamp")
        signed = f"{sig_ts}.{raw_body.decode('utf-8')}"
        expected = hmac.new(XROCKET_WEBHOOK_SECRET.encode(), signed.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected): return web.Response(text="Invalid", status=401)
        data = json.loads(raw_body)
        if data.get("type") == "invoice" and data.get("data", {}).get("status") == "paid":
            payload = data.get("data", {}).get("payload")
            amount_usdt = float(data.get("data", {}).get("amount", 0))
            if payload:
                try:
                    uid_str, check_str = payload.split("_")
                    user_id, check_id = int(uid_str), int(check_str)
                except:
                    user_id, check_id = int(payload), 0
                async with db_pool.acquire() as conn:
                    row = None
                    if check_id > 0:
                        row = await conn.fetchrow("UPDATE orders SET status='paid' WHERE id=$1 AND status='pending' RETURNING message_id", check_id)
                    if check_id > 0 and row is None: return web.Response(text="OK", status=200)
                    rate = await get_final_rate()
                    total_rub = amount_usdt * rate
                    user_row = await conn.fetchrow("SELECT username FROM users WHERE user_id=$1", user_id)
                    username = f"@{user_row['username']}" if user_row and user_row['username'] else f"ID:{user_id}"
                    admin_fee_pct = await get_admin_deposit_fee()
                    ps_fee_pct = await get_ps_fee()
                    fee_ps = total_rub * (ps_fee_pct / 100.0)
                    fee_admin = total_rub * (admin_fee_pct / 100.0)
                    net = total_rub - fee_ps - fee_admin
                    msg_id = row['message_id'] if row else None
                    await conn.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", net, user_id)
                    await conn.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", fee_admin, ADMIN_ID)
                reply = "Оплата произведена"
                if msg_id:
                    try: await bot.send_message(user_id, reply, reply_to_message_id=msg_id, allow_sending_without_reply=True)
                    except:
                        try: await bot.send_message(user_id, reply)
                        except: pass
                else:
                    try: await bot.send_message(user_id, reply)
                    except: pass
                admin_msg = f"🔔 Чек #{check_id} (xRocket)\n👤 {username}\n💵 {amount_usdt} USDT ({total_rub:.2f} ₽)\n\n🏦 ПС ({ps_fee_pct}%): {fee_ps:.2f} ₽\n👑 Тебе ({admin_fee_pct}%): {fee_admin:.2f} ₽\n👤 Юзеру: {net:.2f} ₽"
                try: await bot.send_message(ADMIN_ID, admin_msg, parse_mode="Markdown")
                except: pass
        return web.Response(text="OK", status=200)
    except Exception as e:
        logging.error(f"xRocket Error: {e}")
        return web.Response(text="Error", status=500)


async def main():
    global rolly_client, rocket_client
    await init_db()
    rolly_client = RollyPayClient(api_key=ROLLYPAY_API_KEY)
    rocket_client = xRocketClient(api_key=XROCKET_API_KEY)
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
