import asyncio
import logging
import aiosqlite
from datetime import datetime
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiohttp import web
from rollypay import RollyPayClient

# --- НАСТРОЙКИ ---
BOT_TOKEN = "9037852041:AAGlmd1cTt0Mbc9CM6t-kYJimE86h2cRiEo"
ROLLYPAY_API_KEY = "aMkEd8uF3EY1PM5Zzki5oFfPVx7pnRUiuxYUJQLS5ts"
TERMINAL_ID = "02485c32-7013-40f6-9c1f-00eb12f51d31"
SIGNING_SECRET = "SQ7ZPKKj0l-_yFL_KsARgUj6FjuMJ51v4KFgbr53MAk"
WEBHOOK_PORT = 8080

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
rolly_client = RollyPayClient(api_key=ROLLYPAY_API_KEY)


# --- БАЗА ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect("orders.db") as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                amount REAL,
                payment_id TEXT UNIQUE,
                status TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.commit()

# --- КОМАНДЫ ---

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "Привет! Я бот для создания счетов на оплату.\n\n"
        "Используй команду `/new [сумма]`, чтобы создать заказ.\n"
        "Пример: `/new 100`"
    )

@dp.message(Command("new"))
async def cmd_new(message: types.Message, command: CommandObject):
    if not command.args:
        await message.answer("Пожалуйста, укажите сумму после команды /new.\nПример: `/new 500`")
        return

    try:
        amount = float(command.args.replace(',', '.'))
        if amount <= 0:
            raise ValueError
    except ValueError:
        await message.answer("Ошибка! Сумма должна быть положительным числом.\nПример: `/new 500`")
        return

    user_id = message.from_user.id
    order_id_str = f"order_{user_id}_{int(datetime.now().timestamp())}"

    try:
        payment = rolly_client.payments.create(
            amount=str(amount),
            order_id=order_id_str,
            terminal_id=TERMINAL_ID,
            description=f"Оплата заказа на сумму {amount:.2f} ₽",
            customer_id=str(user_id),
            redirect_url=f"https://t.me/{(await bot.get_me()).username}"
        )
        
        payment_url = payment['pay_url']
        payment_id = payment['payment_id']

        async with aiosqlite.connect("orders.db") as db:
            await db.execute(
                "INSERT INTO orders (user_id, amount, payment_id, status) VALUES (?, ?, ?, ?)",
                (user_id, amount, payment_id, "pending")
            )
            await db.commit()
            db_order_id = (await (await db.execute("SELECT last_insert_rowid()")).fetchone())[0]

        builder = InlineKeyboardBuilder()
        builder.button(text="💳 Перейти к оплате", url=payment_url)
        
        await message.answer(
            f"**Заказ #{db_order_id}**\n\n"
            f"Сумма: {amount:.2f} ₽\n\n"
            f"🟡 **Ожидает оплату...**",
            reply_markup=builder.as_markup(),
            parse_mode="Markdown",
            disable_web_page_preview=True
        )

    except Exception as e:
        logging.error(f"Ошибка RollyPay: {e}")
        await message.answer("Не удалось создать платеж. Попробуйте позже.")

# --- ВЕБХУКИ ---
import hmac
import hashlib
import json

async def handle_rollypay_webhook(request: web.Request):
    try:
        # Читаем сырые байты тела запроса
        raw_body = await request.read()
        signature = request.headers.get("X-Signature")
        timestamp = request.headers.get("X-Timestamp")

        if not signature or not timestamp:
            logging.warning("Вебхук без подписи или метки времени")
            return web.Response(text="Missing signature", status=400)

        # Проверка подлинности подписи RollyPay
        message = f"{timestamp}.{raw_body.decode('utf-8')}"
        expected_signature = hmac.new(
            SIGNING_SECRET.encode('utf-8'),
            message.encode('utf-8'),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature, expected_signature):
            logging.warning("Неверная подпись вебхука RollyPay!")
            return web.Response(text="Invalid signature", status=403)

        # Парсим JSON и проверяем тип события
        data = json.loads(raw_body)
        logging.info(f"Получен вебхук RollyPay: {data}")

        if data.get("event_type") == "payment.paid" or data.get("status") == "paid":
            payment_id = data.get("payment_id")
            
            async with aiosqlite.connect("orders.db") as db:
                async with db.execute(
                    "SELECT id, user_id, amount FROM orders WHERE payment_id = ? AND status = 'pending'", 
                    (payment_id,)
                ) as cursor:
                    row = await cursor.fetchone()
                    if row:
                        db_id, user_id, amount = row
                        await db.execute("UPDATE orders SET status = 'paid' WHERE id = ?", (db_id,))
                        await db.commit()
                        
                        try:
                            await bot.send_message(
                                user_id,
                                f"🟢 **Заказ #{db_id}** успешно оплачен!\nСумма: {amount:.2f} ₽",
                                parse_mode="Markdown"
                            )
                        except Exception as e:
                            logging.error(f"Не удалось отправить сообщение: {e}")

        # RollyPay требует ответ 2xx в течение 10 секунд
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
