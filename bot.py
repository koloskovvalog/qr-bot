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

# --- НАСТРОЙКИ ---
BOT_TOKEN = "9037852041:AAGlmd1cTt0Mbc9CM6t-kYJimE86h2cRiEo"
ROLLYPAY_API_KEY = "aMkEd8uF3EY1PM5Zzki5oFfPVx7pnRUiuxYUJQLS5ts"
TERMINAL_ID = "02485c32-7013-40f6-9c1f-00eb12f51d31"
SIGNING_SECRET = "SQ7ZPKKj0l-_yFL_KsARgUj6FjuMJ51v4KFgbr53MAk"
WEBHOOK_PORT = int(os.environ.get("PORT", 8080))

XROCKET_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhcHBJZCI6IjMwNzc2NSIsImp0aSI6ImFwcDozMDc3NjU6YWMzYjUyODgtYWI3MS00MzUyLTgzNWEtYjRmNjVjZTMyNGQwIiwiaWF0IjoxNzkzNzE1MjA4fQ.nBCxGBwKmyaKjt4cW_jrtLBlagXxGQbvgD5c14yT2TI"
XROCKET_WEBHOOK_SECRET = "74524a9673bb8d1f18dd00eaf22eecd7daf75fbfefd664d1389cefaead0a6673"

DATABASE_URL = "postgresql://postgres.qrfqdihhoqvlaqkuyols:Koloskov2006@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"

MIN_TRC20_USDT = 10.0
MIN_BEP20_USDT = 50.0

ADMIN_FEE_PER_USDT = 1.0
ADMIN_ID = 7653934214

DEFAULT_PAYMENT_SYSTEM_FEE = 9.0
DEFAULT_ADMIN_DEPOSIT_FEE = 4.0

# ==================== ЮРИДИЧЕСКИЕ ССЫЛКИ ====================
OFFER_URL = "https://telegra.ph/PUBLICHNAYA-OFERTA-08-12-15"
PRIVACY_URL = "https://telegra.ph/POLITIKA-KONFIDENCIALNOSTI-08-12-99"
SUPPORT_USERNAME = "@[твой_юзернейм]"  # ⚠️ ЗАМЕНИ

# 🔐 КОД ВЕРИФИКАЦИИ (полный доступ)
DEFAULT_VERIFICATION_CODE = "VERIFY_2025"  # ⚠️ ЗАМЕНИ или смени через /setverifycode

CATEGORY_EMOJI = {
    "Игры": "🎮", "Подписки": "📱", "AI-сервисы": "🤖",
    "Софт": "💻", "Обучение": "📚", "Другое": "📦",
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

# 🔐 Ждём ввод кода от юзера
pending_code_input = {}


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


async def get_verification_code() -> str:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = 'verify_code'")
        return row['value'] if row else DEFAULT_VERIFICATION_CODE


async def is_verified(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT verified_go6 FROM users WHERE user_id = $1", user_id)
        return bool(row and row['verified_go6'])


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
        # 🔐 Заявки на пополнение от неверифицированных
        await conn.execute('''CREATE TABLE IF NOT EXISTS deposit_requests (
            id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount REAL, order_type TEXT,
            comment TEXT, status TEXT DEFAULT 'pending',
            admin_msg_id BIGINT, created_at TIMESTAMPTZ DEFAULT NOW()
        );''')
        await conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);''')
        await conn.execute("INSERT INTO settings (key, value) VALUES ('bot_status', 'active') ON CONFLICT (key) DO NOTHING;")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('markup', '2.0') ON CONFLICT (key) DO NOTHING;")
        await conn.execute(f"INSERT INTO settings (key, value) VALUES ('ps_fee', '{DEFAULT_PAYMENT_SYSTEM_FEE}') ON CONFLICT (key) DO NOTHING;")
        await conn.execute(f"INSERT INTO settings (key, value) VALUES ('admin_deposit_fee', '{DEFAULT_ADMIN_DEPOSIT_FEE}') ON CONFLICT (key) DO NOTHING;")
        await conn.execute(f"INSERT INTO settings (key, value) VALUES ('verify_code', '{DEFAULT_VERIFICATION_CODE}') ON CONFLICT (key) DO NOTHING;")
        # Админ по умолчанию верифицирован
        await conn.execute('''INSERT INTO users (user_id, last_active, balance, verified_go6) VALUES ($1, NOW(), 0, TRUE) ON CONFLICT (user_id) DO UPDATE SET verified_go6 = TRUE''', ADMIN_ID)
        existing = await conn.fetchval("SELECT COUNT(*) FROM products")
        if existing == 0:
            for cat, name, desc, price, delivery in DEMO_PRODUCTS:
                await conn.execute("INSERT INTO products (category, name, description, price, delivery_content) VALUES ($1,$2,$3,$4,$5)",
                                   cat, name, desc, price, delivery)
    logging.info("БД готова!")


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


# 🔐 Управление кодом верификации
@dp.message(Command("setverifycode"))
async def cmd_setverifycode(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args:
        current = await get_verification_code()
        return await message.answer(
            f"🔐 **Код верификации:** `{current}`\n\n"
            f"Сменить: `/setverifycode новый_код`\n"
            f"Пример: `/setverifycode MyCode2025`",
            parse_mode="Markdown")
    new_code = command.args.strip()
    if len(new_code) < 4:
        return await message.answer("❌ Минимум 4 символа.")
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE settings SET value = $1 WHERE key = 'verify_code'", new_code)
    await message.answer(f"✅ **Новый код верификации:** `{new_code}`", parse_mode="Markdown")


@dp.message(Command("allow"))
async def cmd_allow(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("`/allow 123456789`")
    try:
        uid = int(command.args.strip())
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET verified_go6 = TRUE WHERE user_id = $1", uid)
            await conn.execute("INSERT INTO users (user_id, last_active, verified_go6) VALUES ($1, NOW(), TRUE) ON CONFLICT (user_id) DO UPDATE SET verified_go6 = TRUE", uid)
        await message.answer(f"✅ `{uid}` верифицирован вручную.")
        try: await bot.send_message(uid, "✅ Вы верифицированы администратором!")
        except: pass
    except ValueError: await message.answer("❌ ID цифрами.")


@dp.message(Command("deny"))
async def cmd_deny(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("`/deny 123456789`")
    try:
        uid = int(command.args.strip())
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET verified_go6 = FALSE WHERE user_id = $1", uid)
        await message.answer(f"🚫 `{uid}` де-верифицирован.")
    except ValueError: await message.answer("❌ ID цифрами.")


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


# ==================== 🔐 ВВОД КОДА ВЕРИФИКАЦИИ ====================
@dp.message(F.text & ~F.text.startswith("/"))
async def handle_text_input(message: types.Message):
    user_id = message.from_user.id
    if user_id not in pending_code_input:
        return
    pending_code_input.pop(user_id, None)
    code = message.text.strip()
    real_code = await get_verification_code()

    if code == real_code:
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO users (user_id, last_active, verified_go6) VALUES ($1, NOW(), TRUE) ON CONFLICT (user_id) DO UPDATE SET verified_go6 = TRUE", user_id)
        await message.answer(
            "✅ **Верификация пройдена!**\n\n"
            "Теперь вы можете пополнять баланс без ограничений.",
            parse_mode="Markdown")
        try:
            uname = f"@{message.from_user.username}" if message.from_user.username else f"ID:{user_id}"
            await bot.send_message(ADMIN_ID, f"✅ **Верификация по коду**\n👤 {uname}\n`{user_id}`", parse_mode="Markdown")
        except: pass
    else:
        await message.answer("❌ **Неверный код.** Попробуйте снова или обратитесь в поддержку.")
        try:
            await bot.send_message(ADMIN_ID, f"⚠️ Неверный код от `{user_id}`: `{code[:50]}`", parse_mode="Markdown")
        except: pass


# ==================== ГЛАВНОЕ МЕНЮ ====================
async def build_main_menu(user_id: int):
    rate = await get_final_rate()
    verified = await is_verified(user_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="🛒 Каталог товаров", callback_data="shop_catalog")
    builder.button(text="💰 Пополнить баланс", callback_data="shop_deposit")
    builder.button(text="👤 Профиль", callback_data="shop_profile")
    if not verified:
        builder.button(text="🔐 Ввести код верификации", callback_data="verify_enter")
    builder.row(
        InlineKeyboardButton(text="📄 Публичная оферта", url=OFFER_URL),
        InlineKeyboardButton(text="🔒 Политика конфиденциальности", url=PRIVACY_URL)
    )
    builder.adjust(1, 1, 1, 1 if not verified else 0, 2)
    status = "✅ Верифицирован" if verified else "❌ Не верифицирован"
    text = (
        f"👋 **Добро пожаловать!**\n\n"
        f"Статус: **{status}**\n"
        f"Курс пополнения: **1 USDT = {rate:.2f} ₽**\n\n"
        f"Выберите действие:"
    )
    return text, builder.as_markup()


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    text, kb = await build_main_menu(message.from_user.id)
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


@dp.message(Command("verify"))
async def cmd_verify(message: types.Message):
    user_id = message.from_user.id
    if await is_verified(user_id):
        return await message.answer("✅ Вы уже верифицированы.")
    pending_code_input[user_id] = True
    await message.answer(
        "🔐 **Введите код верификации**\n\n"
        "Отправьте секретный код одним сообщением.",
        parse_mode="Markdown")


@dp.callback_query(F.data == "verify_enter")
async def cb_verify_enter(callback: CallbackQuery):
    user_id = callback.from_user.id
    if await is_verified(user_id):
        return await callback.answer("✅ Вы уже верифицированы.", show_alert=True)
    pending_code_input[user_id] = True
    await callback.message.answer(
        "🔐 **Введите код верификации**\n\n"
        "Отправьте секретный код одним сообщением.",
        parse_mode="Markdown")
    await callback.answer()


@dp.callback_query(F.data == "shop_main")
async def cb_shop_main(callback: CallbackQuery):
    text, kb = await build_main_menu(callback.from_user.id)
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
        bal = user['balance'] if user and
