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

# СТРОКА ПОДКЛЮЧЕНИЯ К SUPABASE
DATABASE_URL = "postgresql://postgres.qrfqdihhoqvlaqkuyols:Koloskov2006@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"

# КУРС USDT (сколько рублей за 1 USDT)
USDT_RATE = 90.0

# ВАШ TELEGRAM ID (Уведомления о выводах и оплатах)
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
        # Белый список
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS allowed_users (
                user_id BIGINT PRIMARY KEY
            );
        ''')
        # НОВАЯ ТАБЛИЦА: Заявки на вывод
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS withdrawals (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT,
                amount_usdt REAL,
                amount_rub REAL,
                address TEXT,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        ''')
        # Автоматически добавляем админа
        await conn.execute("INSERT INTO allowed_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", ADMIN_ID)
        
    logging.info("Подключение к Supabase успешно! Таблицы готовы.")

# --- ПРОВЕРКА ДОСТУПА ---
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
        "Привет! Я бот для пополнения баланса и вывода средств.\n\n"
        "💰 **/balance** — проверить баланс (в USDT)\n"
        "📥 **/new [сумма]** — пополнить баланс (в рублях)\n"
        "💸 **/withdraw [сумма_USDT] [адрес_BEP20]** — вывести средства\n\n"
        "Пример вывода: `/withdraw 10 0x1234567890abcdef...`"
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

# --- ВЫВОД СРЕДСТВ ---
@dp.message(Command("withdraw"))
async def cmd_withdraw(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id):
        return

    if not command.args or len(command.args.split()) < 2:
        await message.answer("Ошибка! Укажите сумму в USDT и адрес BEP20.\nПример: `/withdraw 10 0x123abc...`")
        return

    try:
        args = command.args.split()
        amount_usdt = float(args[0].replace(',', '.'))
        address = args[1].strip()
        
        if amount_usdt <= 0:
            raise ValueError
        if not address.startswith("0x") or len(address) < 40:
            await message.answer("Ошибка! Некорректный адрес BEP20. Адрес должен начинаться с `0x`.")
            return
    except ValueError:
        await message.answer("Ошибка! Сумма должна быть положительным числом.")
        return

    amount_rub_needed = amount_usdt * USDT_RATE
    user_id = message.from_user.id

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            # Получаем текущий баланс
            row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
            current_balance = row['balance'] if row else 0.0

            if current_balance < amount_rub_needed:
                await message.answer(f"❌ Недостаточно средств!\nДля вывода {amount_usdt} USDT нужно {amount_rub_needed:.2f} ₽.\nВаш баланс: {current_balance:.2f} ₽")
                return

            # Списываем баланс
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", amount_rub_needed, user_id)
            
            # Создаем заявку на вывод
            await conn.execute(
                "INSERT INTO withdrawals (user_id, amount_usdt, amount_rub, address) VALUES ($1, $2, $3, $4)",
                user_id, amount_usdt, amount_rub_needed, address
            )
            w_id = (await conn.fetchrow("SELECT lastval()"))[0]

    # Сообщение пользователю
    await message.answer(
        f"✅ **Заявка на вывод #{w_id} создана!**\n\n"
        f"Сумма: {amount_usdt:.2f} USDT\n"
        f"Адрес: `{address}`\n"
        f"Списано: {amount_rub_needed:.2f} ₽\n\n"
        f"⏳ Ожидайте обработки. Обычно это занимает до 24 часов.",
        parse_mode="Markdown"
    )

    # Уведомление Админу
    admin_msg = (
        f"💸 **ЗАЯВКА НА ВЫВОД #{w_id}**\n\n"
        f"👤 Пользователь ID: `{user_id}`\n"
        f"💵 Сумма: {amount_usdt:.2f} USDT ({amount_rub_needed:.2f} ₽)\n"
        f"🏦 Адрес BEP20: `{address}`\n\n"
        f"⚠️ *Не забудьте отправить USDT вручную через панель RollyPay!*"
    )
    try:
        await bot.send_message(ADMIN_ID, admin_msg, parse_mode="Markdown")
    except Exception as e:
        logging.error(f"Не удалось отправить админу: {e}")

# --- ПОПОЛНЕНИЕ (NEW / DEPOSIT) ---
async def create_deposit_invoice(message: types.Message, amount: float):
    user_id = message.from_user.id
    order_id_str = f"deposit_{user_id}_{int(datetime.now().timestamp())}"

    try:
        payment = rolly_client.payments.create(
            amount=str(amount), order_id=order_id_str, terminal_id=TERMINAL_ID,
            description=f"Пополнение баланса на {amount:.2f} ₽", customer_id=str(user_id),
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
            f"**Счет на пополнение: {amount:.2f} ₽**\n\n🟡 **Ожидает оплату...**",
            reply_markup=builder.as_markup(), parse_mode="Markdown", disable_web_page_preview=True
        )
    except Exception as e:
        logging.error(f"Ошибка RollyPay: {e}")
        await message.answer("Не удалось создать платеж.")

@dp.message(Command("deposit"))
async def cmd_deposit(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id): return
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
    if not await is_user_allowed(message.from_user.id): return
    if not command.args:
        await message.answer("Укажите сумму: `/new 500`")
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
        expected_signature = hmac.new(SIGNING_SECRET.encode('utf-8'), message.encode('utf-8'), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(signature, expected_signature):
            return web.Response(text="Invalid signature", status=403)

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
                        
                        # Уведомления
                        try:
                            await bot.send_message(user_id, f"🟢 **Баланс пополнен!**\nЗачислено: {amount:.2f} ₽", parse_mode="Markdown")
                        except Exception: pass
                        try:
                            await bot.send_message(ADMIN_ID, f"🔔 **Новое пополнение!**\n👤 ID: `{user_id}`\n💵 Сумма: {amount:.2f} ₽\n🧾 Заказ: #{db_id}", parse_mode="Markdown")
                        except Exception: pass

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
