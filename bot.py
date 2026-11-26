
import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qsl

import asyncpg
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application


# ============================================================
# CONFIG
# ============================================================

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("qr_bot_pro")

BOT_TOKEN = os.environ["BOT_TOKEN"]
DATABASE_URL = os.environ["DATABASE_URL"]
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_PATH = "/telegram-webhook"

ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip()
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

db: asyncpg.Pool | None = None


# ============================================================
# DATABASE
# ============================================================

async def init_db():
    global db

    db = await asyncpg.create_pool(
        DATABASE_URL,
        min_size=1,
        max_size=5,
        ssl="require",
    )

    async with db.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS products (
                id BIGSERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                price_minor BIGINT NOT NULL CHECK (price_minor > 0),
                currency TEXT NOT NULL DEFAULT 'RUB',
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS orders (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                username TEXT,
                product_id BIGINT REFERENCES products(id),
                product_title TEXT NOT NULL,
                amount_minor BIGINT NOT NULL,
                currency TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id BIGSERIAL PRIMARY KEY,
                actor_id BIGINT,
                action TEXT NOT NULL,
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
        """)

    logger.info("Database initialized")


# ============================================================
# HELPERS
# ============================================================

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def format_price(amount_minor: int, currency: str) -> str:
    return f"{amount_minor / 100:.2f} {currency}"


def price_to_minor(value) -> int:
    try:
        amount = Decimal(str(value).replace(",", "."))
        minor = int(amount * 100)
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError("Некорректная цена")

    if minor <= 0:
        raise ValueError("Цена должна быть больше нуля")

    return minor


async def write_audit(actor_id: int, action: str, details: dict):
    async with db.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO audit_log(actor_id, action, details)
            VALUES($1, $2, $3::jsonb)
            """,
            actor_id,
            action,
            json.dumps(details, ensure_ascii=False),
        )


# ============================================================
# TELEGRAM MINI APP AUTH
# ============================================================

def verify_telegram_user(request: web.Request) -> dict:
    """
    Проверяет подпись Telegram Web App initData.
    Нельзя доверять user_id, переданному браузером отдельно.
    """

    raw_data = request.headers.get("X-Telegram-Init-Data", "")

    if not raw_data:
        raise web.HTTPUnauthorized(
            text="Откройте приложение через Telegram"
        )

    values = dict(parse_qsl(raw_data, keep_blank_values=True))
    received_hash = values.pop("hash", None)

    if not received_hash:
        raise web.HTTPUnauthorized(text="Подпись Telegram отсутствует")

    data_check_string = "\n".join(
        f"{key}={value}"
        for key, value in sorted(values.items())
    )

    secret_key = hmac.new(
        key=b"WebAppData",
        msg=BOT_TOKEN.encode(),
        digestmod=hashlib.sha256,
    ).digest()

    calculated_hash = hmac.new(
        key=secret_key,
        msg=data_check_string.encode(),
        digestmod=hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise web.HTTPUnauthorized(text="Неверная подпись Telegram")

    try:
        auth_date = int(values.get("auth_date", "0"))

        if time.time() - auth_date > 86400:
            raise ValueError("Срок авторизации истёк")

        if auth_date > time.time() + 60:
            raise ValueError("Некорректное время авторизации")

        user = json.loads(values["user"])

        if not isinstance(user.get("id"), int):
            raise ValueError("Некорректный пользователь")

        return user

    except (ValueError, KeyError, json.JSONDecodeError):
        raise web.HTTPUnauthorized(text="Сессия Telegram недействительна")


def require_admin(request: web.Request) -> dict:
    user = verify_telegram_user(request)

    if not is_admin(user["id"]):
        raise web.HTTPForbidden(text="Доступ запрещён")

    return user


# ============================================================
# HTTP MIDDLEWARE
# ============================================================

@web.middleware
async def error_middleware(request, handler):
    try:
        return await handler(request)

    except web.HTTPException:
        raise

    except Exception:
        logger.exception("Unhandled HTTP error")
        return web.json_response(
            {"error": "Внутренняя ошибка сервера"},
            status=500,
        )


# ============================================================
# PUBLIC API
# ============================================================

async def health(request):
    return web.json_response({
        "ok": True,
        "service": "QR BOT PRO",
    })


async def get_products(request):
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, title, description, price_minor, currency
            FROM products
            WHERE active = TRUE
            ORDER BY id DESC
        """)

    return web.json_response({
        "items": [dict(row) for row in rows]
    })


async def create_order(request):
    user = verify_telegram_user(request)

    try:
        data = await request.json()
        product_id = int(data.get("product_id"))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Некорректные данные заказа")

    async with db.acquire() as conn:
        async with conn.transaction():
            product = await conn.fetchrow(
                """
                SELECT *
                FROM products
                WHERE id = $1 AND active = TRUE
                FOR SHARE
                """,
                product_id,
            )

            if not product:
                raise web.HTTPNotFound(text="Товар не найден")

            order_id = await conn.fetchval(
                """
                INSERT INTO orders(
                    user_id,
                    username,
                    product_id,
                    product_title,
                    amount_minor,
                    currency
                )
                VALUES($1, $2, $3, $4, $5, $6)
                RETURNING id
                """,
                user["id"],
                user.get("username"),
                product["id"],
                product["title"],
                product["price_minor"],
                product["currency"],
            )

            await conn.execute(
                """
                INSERT INTO audit_log(actor_id, action, details)
                VALUES($1, $2, $3::jsonb)
                """,
                user["id"],
                "order_created",
                json.dumps({
                    "order_id": order_id,
                    "product_id": product_id,
                }),
            )

    # Уведомляем администраторов
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                (
                    f"🛒 Новый заказ #{order_id}\n\n"
                    f"Товар: {product['title']}\n"
                    f"Сумма: {format_price(product['price_minor'], product['currency'])}\n"
                    f"Покупатель: {user.get('first_name', '')}\n"
                    f"Username: @{user.get('username') or 'не указан'}\n"
                    f"Telegram ID: {user['id']}"
                ),
            )
        except Exception:
            logger.exception("Could not notify admin %s", admin_id)

    return web.json_response({
        "ok": True,
        "order_id": order_id,
        "status": "new",
    })


async def get_my_orders(request):
    user = verify_telegram_user(request)

    async with db.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                id,
                product_title,
                amount_minor,
                currency,
                status,
                created_at
            FROM orders
            WHERE user_id = $1
            ORDER BY id DESC
            LIMIT 50
            """,
            user["id"],
        )

    items = []

    for row in rows:
        item = dict(row)
        item["created_at"] = row["created_at"].isoformat()
        items.append(item)

    return web.json_response({"items": items})


# ============================================================
# ADMIN API
# ============================================================

async def admin_add_product(request):
    user = require_admin(request)

    try:
        data = await request.json()

        title = str(data.get("title", "")).strip()
        description = str(data.get("description", "")).strip()
        currency = str(data.get("currency", "RUB")).upper()
        price_minor = price_to_minor(data.get("price"))

    except (ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Проверьте данные товара")

    if not title or len(title) > 120:
        raise web.HTTPBadRequest(text="Название товара обязательно")

    if len(description) > 1500:
        raise web.HTTPBadRequest(text="Слишком длинное описание")

    if currency not in {"RUB", "EUR", "USD"}:
        raise web.HTTPBadRequest(text="Недопустимая валюта")

    async with db.acquire() as conn:
        product_id = await conn.fetchval(
            """
            INSERT INTO products(title, description, price_minor, currency)
            VALUES($1, $2, $3, $4)
            RETURNING id
            """,
            title,
            description,
            price_minor,
            currency,
        )

    await write_audit(
        user["id"],
        "product_created",
        {"product_id": product_id},
    )

    return web.json_response({
        "ok": True,
        "product_id": product_id,
    })


async def admin_toggle_product(request):
    user = require_admin(request)

    try:
        product_id = int(request.match_info["product_id"])
    except ValueError:
        raise web.HTTPBadRequest(text="Некорректный ID товара")

    async with db.acquire() as conn:
        active = await conn.fetchval(
            """
            UPDATE products
            SET active = NOT active
            WHERE id = $1
            RETURNING active
            """,
            product_id,
        )

    if active is None:
        raise web.HTTPNotFound(text="Товар не найден")

    await write_audit(
        user["id"],
        "product_toggled",
        {"product_id": product_id, "active": active},
    )

    return web.json_response({
        "ok": True,
        "active": active,
    })


async def admin_get_orders(request):
    require_admin(request)

    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT
                id,
                user_id,
                username,
                product_title,
                amount_minor,
                currency,
                status,
                created_at
            FROM orders
            ORDER BY id DESC
            LIMIT 100
        """)

    items = []

    for row in rows:
        item = dict(row)
        item["created_at"] = row["created_at"].isoformat()
        items.append(item)

    return web.json_response({"items": items})


async def admin_set_order_status(request):
    user = require_admin(request)

    try:
        order_id = int(request.match_info["order_id"])
        data = await request.json()
        status = str(data.get("status", ""))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Некорректные данные")

    allowed_statuses = {
        "new",
        "invoice_sent",
        "paid",
        "delivered",
        "cancelled",
        "refunded",
    }

    if status not in allowed_statuses:
        raise web.HTTPBadRequest(text="Недопустимый статус")

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE orders
            SET status = $1, updated_at = NOW()
            WHERE id = $2
            RETURNING user_id
            """,
            status,
            order_id,
        )

    if not row:
        raise web.HTTPNotFound(text="Заказ не найден")

    await write_audit(
        user["id"],
        "order_status_changed",
        {"order_id": order_id, "status": status},
    )

    try:
        await bot.send_message(
            row["user_id"],
            f"Обновлён статус вашего заказа #{order_id}: {status}",
        )
    except Exception:
        logger.exception("Could not notify customer")

    return web.json_response({"ok": True})


# ============================================================
# TELEGRAM COMMANDS
# ============================================================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not PUBLIC_BASE_URL:
        await message.answer(
            "QR BOT PRO запущен, но адрес Mini App ещё не настроен."
        )
        return

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[[
            InlineKeyboardButton(
                text="🛍 Открыть магазин",
                web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/"),
            )
        ]]
    )

    await message.answer(
        "Добро пожаловать в QR BOT PRO!\n\n"
        "Здесь вы можете посмотреть каталог цифровых товаров и услуг, "
        "оформить заказ и отслеживать его статус.\n\n"
        "Для начала нажмите кнопку ниже.",
        reply_markup=keyboard,
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "QR BOT PRO — магазин цифровых товаров и услуг.\n\n"
        "/start — открыть магазин\n"
        "/help — помощь\n\n"
        "Если у вас есть вопрос по заказу, обратитесь к администратору."
    )


@dp.message(Command("addproduct"))
async def cmd_add_product(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    raw = message.text.partition(" ")[2]
    parts = [part.strip() for part in raw.split("|")]

    if len(parts) != 4:
        await message.answer(
            "Формат команды:\n"
            "/addproduct Название | Описание | Цена | RUB\n\n"
            "Пример:\n"
            "/addproduct Дизайн баннера | PNG-файл | 990 | RUB"
        )
        return

    title, description, raw_price, currency = parts

    try:
        price_minor = price_to_minor(raw_price)
    except ValueError:
        await message.answer("Укажите корректную цену.")
        return

    currency = currency.upper()

    if not title or currency not in {"RUB", "EUR", "USD"}:
        await message.answer("Проверьте название и валюту.")
        return

    async with db.acquire() as conn:
        product_id = await conn.fetchval(
            """
            INSERT INTO products(title, description, price_minor, currency)
            VALUES($1, $2, $3, $4)
            RETURNING id
            """,
            title,
            description,
            price_minor,
            currency,
        )

    await write_audit(
        message.from_user.id,
        "product_created",
        {"product_id": product_id},
    )

    await message.answer(f"✅ Товар добавлен. ID: {product_id}")


@dp.message(Command("orders"))
async def cmd_orders(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, user_id, product_title, amount_minor, currency, status
            FROM orders
            ORDER BY id DESC
            LIMIT 30
        """)

    if not rows:
        await message.answer("Заказов пока нет.")
        return

    result = []

    for row in rows:
        result.append(
            f"#{row['id']} · {row['product_title']} · "
            f"{format_price(row['amount_minor'], row['currency'])} · "
            f"{row['status']} · user {row['user_id']}"
        )

    await message.answer("\n".join(result))


@dp.message(Command("setstatus"))
async def cmd_set_status(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=2)

    if len(parts) != 3:
        await message.answer(
            "Формат:\n"
            "/setstatus ID paid\n\n"
            "Статусы: invoice_sent, paid, delivered, cancelled, refunded"
        )
        return

    try:
        order_id = int(parts[1])
    except ValueError:
        await message.answer("ID заказа должен быть числом.")
        return

    status = parts[2].strip()

    allowed = {
        "invoice_sent",
        "paid",
        "delivered",
        "cancelled",
        "refunded",
    }

    if status not in allowed:
        await message.answer("Недопустимый статус.")
        return

    async with db.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE orders
            SET status = $1, updated_at = NOW()
            WHERE id = $2
            RETURNING user_id
            """,
            status,
            order_id,
        )

    if not row:
        await message.answer("Заказ не найден.")
        return

    await write_audit(
        message.from_user.id,
        "order_status_changed",
        {"order_id": order_id, "status": status},
    )

    try:
        await bot.send_message(
            row["user_id"],
            f"Статус вашего заказа #{order_id}: {status}",
        )
    except Exception:
        logger.exception("Could not notify customer")

    await message.answer(f"✅ Статус заказа #{order_id} изменён: {status}")


# ============================================================
# APPLICATION
# ============================================================

async def on_startup(app: web.Application):
    await init_db()

    if not PUBLIC_BASE_URL:
        raise RuntimeError("Не задан PUBLIC_BASE_URL")

    if not WEBHOOK_SECRET:
        raise RuntimeError("Не задан WEBHOOK_SECRET")

    await bot.set_webhook(
        url=f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(),
    )

    logger.info("QR BOT PRO started")


async def on_cleanup(app: web.Application):
    if db is not None:
        await db.close()

    await bot.session.close()


def create_app():
    app = web.Application(middlewares=[error_middleware])

    # Public endpoints
    app.router.add_get("/health", health)
    app.router.add_get("/api/products", get_products)
    app.router.add_post("/api/orders", create_order)
    app.router.add_get("/api/orders", get_my_orders)

    # Admin endpoints
    app.router.add_post("/api/admin/products", admin_add_product)
    app.router.add_post(
        "/api/admin/products/{product_id}/toggle",
        admin_toggle_product,
    )
    app.router.add_get("/api/admin/orders", admin_get_orders)
    app.router.add_post(
        "/api/admin/orders/{order_id}/status",
        admin_set_order_status,
    )

    # Telegram webhook
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    ).register(app, path=WEBHOOK_PATH)

    setup_application(app, dp, bot=bot)

    # Static Mini App
    app.router.add_static("/", path="static", show_index=True)

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app


async def main():
    runner = web.AppRunner(create_app())
    await runner.setup()

    site = web.TCPSite(
        runner,
        host="0.0.0.0",
        port=PORT,
    )

    await site.start()

    logger.info("HTTP server listening on port %s", PORT)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
