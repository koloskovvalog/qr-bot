import asyncio
import logging
import asyncpg
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web
from rollypay import RollyPayClient
import os
import hmac
import hashlib
import json

# --- НАСТРОЙКИ ---
BOT_TOKEN = "9037852041:AAGlmd1cTt0Mbc9CM6t-kYJimE86h2cRiEo"
ROLLYPAY_API_KEY = "aMkEd8uF3EY1PM5Zzki5oFfPVx7pnRUiuxYUJQLS5ts"
TERMINAL_ID = "02485c32-7013-40f6-9c1f-00eb12f51d31"
SIGNING_SECRET = "SQ7ZPKKj0l-_yFL_KsARgUj6FjuMJ51v4KFgbr53MAk"
WEBHOOK_PORT = int(os.environ.get("PORT", 8080))

# СТРОКА ПОДКЛЮЧЕНИЯ К SUPABASE (С вашим паролем)
DATABASE_URL = "postgresql://postgres.qrfqdihhoqvlaqkuyols:Koloskov2006@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"

# КУРС USDT (сколько рублей за 1 USDT)
USDT_RATE = 90.0

# ВАШ TELEGRAM ID (Сюда будут приходить уведомления)
ADMIN_ID = 7653934214

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
rolly_client = RollyPayClient(api_key=ROLLYPAY_API_KEY)

# --- БАЗА ДАННЫХ ---
db_pool: asyncpg.Pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(
        DATABASE_URL, 
        min_size=1, 
        max_size=5, 
        ssl="require", 
        statement_cache_size=0 
    )
    
    async with db_pool.acquire() as conn:
        # Таблица балансов
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                balance REAL DEFAULT 0
            );
        ''')
        # Таблица заказов
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT,
                amount REAL,
                payment_id TEXT UNIQUE,
                status TEXT,
                order_type TEXT DEFAULT 'deposit',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        ''')
        # НОВАЯ ТАБЛИЦА: Белый список
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id BIGINT PRIMARY KEY
            );
        ''')
        # Автоматически добавляем админа в базу, чтобы он всегда имел доступ
        await conn.execute("INSERT INTO allowed_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", ADMIN_ID)
        
    logging.info("Подключение к Supabase успешно! Таблицы готовы.")

# --- ФУНКЦИЯ ПРОВЕРКИ ДОСТУПА ---
async def is_user_allowed(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM allowed_users WHERE user_id = $1", user_id)
        return row is not None

# --- КОМАНДЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not await is_user_allowed(message.from_user.id):
        return
    
    await message.answer(
        "Привет! Я бот для пополнения баланса.\n\n"
        "💰 **/balance** — проверить баланс (в USDT)\n"
        "📥 **/new [сумма]** — создать счет на пополнение\n"
        "📥 **/deposit [сумма]** — альтернативная команда для пополнения"
    )

@dp.message(Command("allow"))
async def cmd_allow(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID:
        return
    if not command.args:
        await message.answer("Укажите ID: `/allow 123456789`")
        return
    try:
        new_user_id = int(command.args.strip())
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO allowed_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", new_user_id)
        await message.answer(f"✅ Пользователь `{new_user_id}` добавлен в белый список навсегда.")
    except ValueError:
        await message.answer("Ошибка! ID должен состоять только из цифр.")

@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    if not await is_user_allowed(message.from_user.id):
        return
    
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", message.from_user.id)
        balance_rub = row['balance'] if row else 0.0

    balance_usdt = balance_rub / USDT_RATE
    await message.answer(
        f"💰 **Ваш баланс:**\n"
        f"{balance_rub:.2f} ₽\n"
        f"({balance_usdt:.2f} USDT по курсу {USDT_RATE} ₽/USDT)",
        parse_mode="Markdown"
    )

# Единая функция для создания счета на пополнение
async def create_deposit_invoice(message: types.Message, amount: float):
    user_id = message.from_user.id
    order_id_str = f"deposit_{user_id}_{int(datetime.now().timestamp())}"

    try:
        payment = rolly_client.payments.create(
            amount=str(amount), 
            order_id=order_id_str, 
            terminal_id=TERMINAL_ID,
            description=f"Пополнение баланса на {amount:.2f} ₽", 
            customer_id=str(user_id),
            redirect_url=f"https://t.me/{(await bot.get_me()).username}"
        )
        
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO orders (user_id, amount, payment_id, status, order_type) VALUES ($1, $2, $3, $4, $5)",
                user_id, amount, payment['payment_id'], "pending", "deposit"
            )

        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Перейти к оплате", url=payment['pay_url'])
        
        await message.answer(
            f"**Счет на пополнение: {amount:.2f} ₽**\n\n"
            f"🟡 **Ожидает оплату...**",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )
    except Exception as e:
        logging.error(f"Ошибка RollyPay: {e}")
        await message.answer("Не удалось создать платеж.")

@dp.message(Command("deposit"))
async def cmd_deposit(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id):
        return
    if not command.args:
        await message.answer("Укажите сумму: `/deposit 500`")
        return
    try:
        amount = float(command.args.replace(',', '.'))
        if amount <= 0: raise ValueError
    except ValueError:
        await message.answer("Ошибка! Сумма должна быть положительным числом.")
        return
    await create_deposit_invoice(message, amount)

@dp.message(Command("new"))
async def cmd_new(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id):
        return
    if not command.args:
        await message.answer("Пожалуйста, укажите сумму после команды /new.\nПример: `/new 500`")
        return
    try:
        amount = float(command.args.replace(',', '.'))
        if amount <= 0: raise ValueError
    except ValueError:
        await message.answer("Ошибка! Сумма должна быть положительным числом.")
        return
    await create_deposit_invoice(message, amount)

# --- ВЕБХУКИ ---

async def handle_rollypay_webhook(request: web.Request):
    try:
        raw_body = await request.read()
        signature = request.headers.get("X-Signature")
        timestamp = request.headers.get("X-Timestamp")

        if not signature or not timestamp:
            return web.Response(text="Missing signature", status=400)

        message = f"{timestamp}.{raw_body.decode('utf-8')}"
        expected_signature = hmac.new(
            SIGNING_SECRET.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature, expected_signature):
            return web.Response(text="Invalid signature", status=403)

        data = json.loads(raw_body)
        
        if data.get("event_type") == "payment.paid" or data.get("status") == "paid":
            payment_id = data.get("payment_id")
            
            async with db_pool.acquire() as conn:
                async with conn.transaction():
                    row = await conn.fetchrow(
                        "SELECT id, user_id, amount FROM orders WHERE payment_id = $1 AND status = 'pending'", 
                        payment_id
                    )
                    if row:
                        db_id = row['id']
                        user_id = row['user_id']
                        amount = row['amount']
                        
                        # Обновляем статус заказа
                        await conn.execute("UPDATE orders SET status = 'paid' WHERE id = $1", db_id)
                        
                        # Начисляем баланс
                        await conn.execute("""
                            INSERT INTO users (user_id, balance) VALUES ($1, $2)
                            ON CONFLICT (user_id) DO UPDATE SET balance = users.balance + EXCLUDED.balance
                        """, user_id, amount)
                        
                        # 1. Уведомление пользователю
                        user_msg = f"🟢 **Баланс пополнен!**\nЗачислено: {amount:.2f} ₽"
                        try:
                            await bot.send_message(user_id, user_msg, parse_mode="Markdown")
                        except Exception as e:
                            logging.error(f"Не удалось отправить пользователю: {e}")

                        # 2. УВЕДОМЛЕНИЕ АДМИНУ
                        admin_msg = (
                            f"🔔 **Новое пополнение!**\n\n"
                            f"👤 Пользователь ID: `{user_id}`\n"
                            f"💵 Сумма: {amount:.2f} ₽\n"
                            f"🧾 Номер заказа: #{db_id}"
                        )
                        try:
                            await bot.send_message(ADMIN_ID, admin_msg, parse_mode="Markdown")
                        except Exception as e:
                            logging.error(f"Не удалось отправить админу: {e}")

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
    logging.info(f"Веб-сервер запущен на порту {WEBHOOK_PORT}")

    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
