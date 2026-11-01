import asyncio
import logging
import asyncpg
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
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

DATABASE_URL = "postgresql://postgres.qrfqdihhoqvlaqkuyols:Koloskov2006@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"

USDT_RATE = 90.0
MIN_WITHDRAW_USDT = 50.0
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
        DATABASE_URL, min_size=1, max_size=5, ssl="require", statement_cache_size=0 
    )
    
    async with db_pool.acquire() as conn:
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY, balance REAL DEFAULT 0
            );
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
        # Таблица заявок (статусы: pending, processing, completed, cancelled, rejected)
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS withdrawals (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount_usdt REAL,
                amount_rub REAL, address TEXT, status TEXT DEFAULT 'pending',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
        ''')
        await conn.execute("INSERT INTO allowed_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", ADMIN_ID)
    logging.info("Подключение к Supabase успешно!")

async def is_user_allowed(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM allowed_users WHERE user_id = $1", user_id)
        return row is not None

# --- КОМАНДЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not await is_user_allowed(message.from_user.id): return
    await message.answer(
        "Привет! Я бот для пополнения и вывода средств.\n\n"
        "💰 **/balance** — баланс\n"
        "📥 **/new [сумма]** — пополнить (в рублях)\n"
        f"💸 **/withdraw [сумма] [адрес]** — вывод (от {MIN_WITHDRAW_USDT} USDT)\n\n"
        "Пример: `/withdraw 50 0x1234567890abcdef...`"
    )

@dp.message(Command("allow"))
async def cmd_allow(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: return await message.answer("Укажите ID: `/allow 123456789`")
    try:
        new_user_id = int(command.args.strip())
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO allowed_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", new_user_id)
        await message.answer(f"✅ Пользователь `{new_user_id}` добавлен.")
    except ValueError: await message.answer("Ошибка в ID.")

@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    if not await is_user_allowed(message.from_user.id): return
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", message.from_user.id)
        bal = row['balance'] if row else 0.0
    await message.answer(f"💰 **Баланс:** {bal:.2f} ₽\n({bal / USDT_RATE:.2f} USDT)", parse_mode="Markdown")

# --- ВЫВОД СРЕДСТВ ---
@dp.message(Command("withdraw"))
async def cmd_withdraw(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id): return
    if not command.args or len(command.args.split()) < 2:
        return await message.answer(f"Укажите сумму (от {MIN_WITHDRAW_USDT} USDT) и адрес.\nПример: `/withdraw 50 0x123...`")

    try:
        args = command.args.split()
        amount_usdt = float(args[0].replace(',', '.'))
        address = args[1].strip()
        if amount_usdt < MIN_WITHDRAW_USDT:
            return await message.answer(f"❌ Минимум: **{MIN_WITHDRAW_USDT} USDT**.", parse_mode="Markdown")
        if not address.startswith("0x") or len(address) < 40:
            return await message.answer("❌ Неверный адрес BEP20 (должен начинаться с `0x`).")
    except ValueError:
        return await message.answer("Ошибка! Неверная сумма.")

    amount_rub = amount_usdt * USDT_RATE
    user_id = message.from_user.id

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
            bal = row['balance'] if row else 0.0
            if bal < amount_rub:
                return await message.answer(f"❌ Недостаточно средств!\nНужно: {amount_rub:.2f} ₽\nУ вас: {bal:.2f} ₽")

            # Списываем баланс
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", amount_rub, user_id)
            
            # Создаем заявку
            await conn.execute(
                "INSERT INTO withdrawals (user_id, amount_usdt, amount_rub, address) VALUES ($1, $2, $3, $4)",
                user_id, amount_usdt, amount_rub, address
            )
            w_id = (await conn.fetchrow("SELECT lastval()"))[0]

    # Кнопка отмены для пользователя
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отменить заявку", callback_data=f"user_cancel_{w_id}")

    await message.answer(
        f"✅ **Заявка #{w_id} создана!**\n\n"
        f"Сумма: {amount_usdt:.2f} USDT\n"
        f"Адрес: `{address}`\n"
        f"Списано: {amount_rub:.2f} ₽\n\n"
        f"⏳ Ожидайте. Вы можете отменить заявку, пока она не взята в работу.",
        reply_markup=builder.as_markup(), parse_mode="Markdown"
    )

    # Кнопки для админа
    admin_builder = InlineKeyboardBuilder()
    admin_builder.button(text="🟢 Взять в работу", callback_data=f"admin_take_{w_id}")
    admin_builder.button(text="🔴 Отклонить (возврат)", callback_data=f"admin_reject_{w_id}")

    await bot.send_message(
        ADMIN_ID,
        f"💸 **НОВАЯ ЗАЯВКА НА ВЫВОД #{w_id}**\n\n"
        f"👤 ID: `{user_id}`\n"
        f"💵 Сумма: {amount_usdt:.2f} USDT ({amount_rub:.2f} ₽)\n"
        f"🏦 Адрес: `{address}`",
        reply_markup=admin_builder.as_markup(), parse_mode="Markdown"
    )

# --- ОБРАБОТКА КНОПОК ВЫВОДА ---

@dp.callback_query(F.data.startswith("user_cancel_"))
async def process_user_cancel(callback: types.CallbackQuery):
    w_id = int(callback.data.split("_")[2])
    user_id = callback.from_user.id

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT amount_rub, status FROM withdrawals WHERE id = $1 AND user_id = $2", w_id, user_id)
            if not row:
                return await callback.answer("Заявка не найдена.", show_alert=True)
            
            if row['status'] != 'pending':
                return await callback.answer("Заявка уже в обработке или выполнена, отмена невозможна.", show_alert=True)

            # Возвращаем деньги
            await conn.execute("UPDATE withdrawals SET status = 'cancelled' WHERE id = $1", w_id)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", row['amount_rub'], user_id)

    await callback.message.edit_text(f"❌ **Заявка #{w_id} отменена.**\nДеньги возвращены на баланс.", parse_mode="Markdown")
    await callback.answer("Заявка отменена, средства возвращены.")

    # Уведомляем админа об отмене
    try:
        await bot.send_message(ADMIN_ID, f"⚠️ Пользователь `{user_id}` отменил заявку на вывод #{w_id}.", parse_mode="Markdown")
    except Exception: pass

@dp.callback_query(F.data.startswith("admin_take_"))
async def process_admin_take(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer("Нет доступа.")
    w_id = int(callback.data.split("_")[2])

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, amount_usdt, address, status FROM withdrawals WHERE id = $1", w_id)
        if not row or row['status'] != 'pending':
            return await callback.answer("Заявка уже обработана или отменена.", show_alert=True)
        
        await conn.execute("UPDATE withdrawals SET status = 'processing' WHERE id = $1", w_id)

    # Меняем кнопки у админа
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Выполнено (Отправил)", callback_data=f"admin_done_{w_id}")
    await callback.message.edit_reply_markup(reply_markup=builder.as_markup())

    # Уведомляем пользователя
    try:
        await bot.send_message(row['user_id'], f"⏳ **Заявка #{w_id} взята в работу.**\nОжидайте поступления средств.", parse_mode="Markdown")
    except Exception: pass
    await callback.answer("Заявка взята в работу.")

@dp.callback_query(F.data.startswith("admin_reject_"))
async def process_admin_reject(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer("Нет доступа.")
    w_id = int(callback.data.split("_")[2])

    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT user_id, amount_rub, status FROM withdrawals WHERE id = $1", w_id)
            if not row or row['status'] not in ['pending', 'processing']:
                return await callback.answer("Заявка уже выполнена или отменена.", show_alert=True)
            
            # Отклоняем и возвращаем деньги
            await conn.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = $1", w_id)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", row['amount_rub'], row['user_id'])

    await callback.message.edit_text(f"🔴 Заявка #{w_id} отклонена. Деньги возвращены пользователю.")
    try:
        await bot.send_message(row['user_id'], f"🔴 **Заявка #{w_id} отклонена администратором.**\nСредства возвращены на ваш баланс.", parse_mode="Markdown")
    except Exception: pass
    await callback.answer("Заявка отклонена.")

@dp.callback_query(F.data.startswith("admin_done_"))
async def process_admin_done(callback: types.CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return await callback.answer("Нет доступа.")
    w_id = int(callback.data.split("_")[2])

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, amount_usdt, address, status FROM withdrawals WHERE id = $1", w_id)
        if not row or row['status'] != 'processing':
            return await callback.answer("Ошибка статуса.", show_alert=True)
        
        await conn.execute("UPDATE withdrawals SET status = 'completed' WHERE id = $1", w_id)

    await callback.message.edit_text(f"✅ Заявка #{w_id} успешно выполнена.")
    try:
        await bot.send_message(row['user_id'], f"🟢 **Выплата по заявке #{w_id} отправлена!**\nСумма: {row['amount_usdt']:.2f} USDT", parse_mode="Markdown")
    except Exception: pass
    await callback.answer("Заявка выполнена.")

# --- ПОПОЛНЕНИЕ ---
async def create_deposit_invoice(message: types.Message, amount: float):
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

@dp.message(Command("deposit"))
@dp.message(Command("new"))
async def cmd_deposit_new(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id): return
    if not command.args: return await message.answer("Укажите сумму: `/deposit 500`")
    try:
        amount = float(command.args.replace(',', '.'))
        if amount <= 0: raise ValueError
    except ValueError: return await message.answer("Ошибка! Неверная сумма.")
    await create_deposit_invoice(message, amount)

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
