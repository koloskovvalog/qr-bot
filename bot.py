import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import secrets
import re
from decimal import Decimal, InvalidOperation
from datetime import datetime, timezone
from urllib.parse import parse_qsl

import asyncpg
import aiohttp
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
APISKINS_API_KEY = os.getenv("APISKINS_API_KEY", "").strip()
USD_RUB_RATE = None
USD_RUB_RATE_AT = 0
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
CS2_CATALOG_CACHE = {}
CS2_CATALOG_CACHE_AT = {}
CS2_PRICE_QUEUE = asyncio.Queue()
CS2_PRICE_QUEUED = set()
CS2_PRICE_WORKER_TASK = None


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
        async with conn.transaction():
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS products (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    price_minor BIGINT NOT NULL DEFAULT 100,
                    currency TEXT NOT NULL DEFAULT 'RUB',
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS orders (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    username TEXT,
                    product_id BIGINT REFERENCES products(id),
                    product_title TEXT NOT NULL DEFAULT '',
                    amount_minor BIGINT NOT NULL DEFAULT 100,
                    currency TEXT NOT NULL DEFAULT 'RUB',
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

                ALTER TABLE products
                    ADD COLUMN IF NOT EXISTS title TEXT NOT NULL DEFAULT '',
                    ADD COLUMN IF NOT EXISTS description TEXT NOT NULL DEFAULT '',
                    ADD COLUMN IF NOT EXISTS price_minor BIGINT NOT NULL DEFAULT 100,
                    ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'RUB',
                    ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE,
                    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

                ALTER TABLE orders
    ADD COLUMN IF NOT EXISTS product_id BIGINT,
    ADD COLUMN IF NOT EXISTS username TEXT,
    ADD COLUMN IF NOT EXISTS product_title TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS amount_minor BIGINT NOT NULL DEFAULT 100,
    ADD COLUMN IF NOT EXISTS currency TEXT NOT NULL DEFAULT 'RUB',
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'new',
    ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
    
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM information_schema.columns
                        WHERE table_name = 'products'
                        AND column_name = 'name'
                    ) THEN
                        EXECUTE '
                            UPDATE products
                            SET title = name
                            WHERE title = ''''
                        ';
                    END IF;
                END $$;

                UPDATE orders o
                SET product_title = p.title
                FROM products p
                WHERE o.product_id = p.id
                  AND o.product_title = '';

                CREATE TABLE IF NOT EXISTS payment_providers (
                    code TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    mode TEXT NOT NULL DEFAULT 'demo',
                    enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    public_config JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS payment_transactions (
                    id BIGSERIAL PRIMARY KEY,
                    order_id BIGINT NOT NULL REFERENCES orders(id),
                    user_id BIGINT NOT NULL,
                    provider_code TEXT NOT NULL REFERENCES payment_providers(code),
                    amount_minor BIGINT NOT NULL,
                    currency TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'demo_pending',
                    external_reference TEXT NOT NULL UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS customer_profiles (
                    user_id BIGINT PRIMARY KEY,
                    referral_code TEXT NOT NULL UNIQUE,
                    referred_by BIGINT,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS loyalty_ledger (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    order_id BIGINT REFERENCES orders(id),
                    points_delta INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE UNIQUE INDEX IF NOT EXISTS loyalty_order_reward_unique
                    ON loyalty_ledger(user_id, order_id, reason)
                    WHERE order_id IS NOT NULL;

                CREATE TABLE IF NOT EXISTS wallet_accounts (
                    user_id BIGINT PRIMARY KEY,
                    balance_minor BIGINT NOT NULL DEFAULT 0 CHECK (balance_minor >= 0),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS wallet_ledger (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    inventory_id BIGINT NOT NULL UNIQUE,
                    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
                    currency TEXT NOT NULL DEFAULT 'RUB',
                    reason TEXT NOT NULL DEFAULT 'virtual_skin_sale',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_wallet_ledger_user
                    ON wallet_ledger(user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS cs2_steam_prices (
                    market_hash_name TEXT PRIMARY KEY,
                    lowest_price TEXT,
                    median_price TEXT,
                    volume TEXT,
                    currency TEXT NOT NULL DEFAULT 'RUB',
                    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                ALTER TABLE cs2_steam_prices
                    ADD COLUMN IF NOT EXISTS price_source TEXT NOT NULL DEFAULT 'steam';
                CREATE INDEX IF NOT EXISTS idx_cs2_steam_prices_fetched
                    ON cs2_steam_prices(fetched_at);

                INSERT INTO payment_providers(code, title, mode, enabled, public_config)
                VALUES
                    ('demo', 'QR BOT PRO Demo Gateway', 'demo', TRUE, '{"description":"Тестовый провайдер без списания средств"}'::jsonb),
                    ('antilopay', 'Antilopay', 'unconfigured', FALSE, '{"description":"Адаптер будет активирован после получения официальной документации и тестовых реквизитов"}'::jsonb)
                ON CONFLICT (code) DO NOTHING;

                CREATE TABLE IF NOT EXISTS cases (
                    id BIGSERIAL PRIMARY KEY,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    price_minor BIGINT NOT NULL DEFAULT 0,
                    currency TEXT NOT NULL DEFAULT 'RUB',
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                ALTER TABLE cases ADD COLUMN IF NOT EXISTS image_url TEXT;

                CREATE TABLE IF NOT EXISTS case_items (
                    id BIGSERIAL PRIMARY KEY,
                    case_id BIGINT NOT NULL REFERENCES cases(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    rarity TEXT NOT NULL DEFAULT 'Обычный',
                    weight INTEGER NOT NULL CHECK (weight > 0),
                    image_url TEXT,
                    value_minor BIGINT NOT NULL DEFAULT 1000,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS case_openings (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    case_id BIGINT NOT NULL REFERENCES cases(id),
                    item_id BIGINT NOT NULL REFERENCES case_items(id),
                    item_name TEXT NOT NULL,
                    rarity TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                ALTER TABLE case_items ADD COLUMN IF NOT EXISTS image_url TEXT;
                ALTER TABLE case_items ADD COLUMN IF NOT EXISTS value_minor BIGINT NOT NULL DEFAULT 1000;
                ALTER TABLE case_items ADD COLUMN IF NOT EXISTS market_hash_name TEXT;
                ALTER TABLE case_items ADD COLUMN IF NOT EXISTS wear TEXT;
                ALTER TABLE case_items ADD COLUMN IF NOT EXISTS stattrak BOOLEAN NOT NULL DEFAULT FALSE;
                ALTER TABLE case_items ADD COLUMN IF NOT EXISTS souvenir BOOLEAN NOT NULL DEFAULT FALSE;
                ALTER TABLE case_items ADD COLUMN IF NOT EXISTS phase TEXT;
                ALTER TABLE case_items ADD COLUMN IF NOT EXISTS is_active BOOLEAN NOT NULL DEFAULT TRUE;
                CREATE UNIQUE INDEX IF NOT EXISTS case_items_market_hash_unique
                    ON case_items(case_id, market_hash_name)
                    WHERE market_hash_name IS NOT NULL;

                CREATE TABLE IF NOT EXISTS user_inventory (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    item_type TEXT NOT NULL DEFAULT 'skin',
                    item_name TEXT NOT NULL,
                    rarity TEXT NOT NULL DEFAULT 'Обычный',
                    image_url TEXT,
                    value_minor BIGINT NOT NULL DEFAULT 1000,
                    source TEXT NOT NULL DEFAULT 'case',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    consumed_at TIMESTAMPTZ
                );
                ALTER TABLE user_inventory ADD COLUMN IF NOT EXISTS market_hash_name TEXT;
                ALTER TABLE user_inventory ADD COLUMN IF NOT EXISTS wear TEXT;
                ALTER TABLE user_inventory ADD COLUMN IF NOT EXISTS stattrak BOOLEAN NOT NULL DEFAULT FALSE;
                ALTER TABLE user_inventory ADD COLUMN IF NOT EXISTS souvenir BOOLEAN NOT NULL DEFAULT FALSE;
                ALTER TABLE user_inventory ADD COLUMN IF NOT EXISTS phase TEXT;
                CREATE INDEX IF NOT EXISTS idx_user_inventory_active
                    ON user_inventory(user_id, item_type, created_at DESC)
                    WHERE consumed_at IS NULL;

                CREATE TABLE IF NOT EXISTS upgrade_attempts (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    item_type TEXT NOT NULL DEFAULT 'skin',
                    source_inventory_id BIGINT NOT NULL,
                    source_name TEXT NOT NULL,
                    target_name TEXT NOT NULL,
                    target_image_url TEXT,
                    source_value_minor BIGINT NOT NULL,
                    target_value_minor BIGINT NOT NULL,
                    chance_percent NUMERIC(6,2) NOT NULL,
                    roll_value INTEGER NOT NULL,
                    success BOOLEAN NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS demo_case_claims (
                    user_id BIGINT NOT NULL,
                    claim_date DATE NOT NULL DEFAULT CURRENT_DATE,
                    PRIMARY KEY(user_id, claim_date)
                );

                INSERT INTO user_inventory(user_id, item_type, item_name, rarity, value_minor, source, created_at)
                SELECT co.user_id, 'skin', co.item_name, co.rarity,
                    CASE
                      WHEN lower(co.rarity) LIKE '%тайн%' OR lower(co.rarity) LIKE '%covert%' THEN 80000
                      WHEN lower(co.rarity) LIKE '%засекр%' OR lower(co.rarity) LIKE '%classified%' THEN 30000
                      WHEN lower(co.rarity) LIKE '%запрещ%' OR lower(co.rarity) LIKE '%restricted%' THEN 12000
                      ELSE 3000
                    END,
                    'case', co.created_at
                FROM case_openings co
                WHERE NOT EXISTS (
                    SELECT 1 FROM user_inventory ui
                    WHERE ui.user_id = co.user_id AND ui.item_name = co.item_name
                      AND ui.created_at = co.created_at
                );

                INSERT INTO cases(title, description, price_minor, currency, active)
                SELECT 'Премиум кейс', 'Демонстрационное открытие CS2 — только виртуальные предметы', 0, 'RUB', TRUE
                WHERE NOT EXISTS (SELECT 1 FROM cases);

                INSERT INTO case_items(case_id, name, rarity, weight)
                SELECT c.id, v.name, v.rarity, v.weight
                FROM cases c
                CROSS JOIN (VALUES
                    ('AK-47 | Slate', 'Армейское качество', 6000),
                    ('AWP | Atheris', 'Запрещённое', 2500),
                    ('USP-S | Cortex', 'Засекреченное', 1200),
                    ('Karambit | Doppler', 'Тайное', 300)
                ) AS v(name, rarity, weight)
                WHERE c.title = 'Премиум кейс'
                  AND NOT EXISTS (SELECT 1 FROM case_items ci WHERE ci.case_id = c.id);

                UPDATE case_items SET value_minor = CASE
                    WHEN lower(rarity) LIKE '%тайн%' OR lower(rarity) LIKE '%covert%' THEN 80000
                    WHEN lower(rarity) LIKE '%засекр%' OR lower(rarity) LIKE '%classified%' THEN 30000
                    WHEN lower(rarity) LIKE '%запрещ%' OR lower(rarity) LIKE '%restricted%' THEN 12000
                    ELSE 5000
                END
                WHERE value_minor = 1000;
            """)

    logger.info("Database initialized and migrated")


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


async def get_cases(request):
    try:
        catalog_skins = await load_cs2_catalog("skins")
        skin_images = {item["name"]: item["image_url"] for item in catalog_skins}
        catalog_cases = await load_cs2_catalog("cases")
        fallback_case_image = next(
            (item["image_url"] for item in catalog_cases if "case" in item["name"].lower()),
            catalog_cases[0]["image_url"] if catalog_cases else None,
        )
    except Exception:
        catalog_skins = []
        skin_images = {}
        catalog_cases = []
        fallback_case_image = None
    skins_by_crate = {}
    skins_by_id = {}
    for skin in catalog_skins:
        crate_keys = set(skin.get("crate_ids", []))
        crate_keys.update(skin.get("crate_names", []))
        for crate_key in crate_keys:
            skins_by_crate.setdefault(str(crate_key).strip().casefold(), []).append(skin)
        for skin_key in {skin.get("id"), skin.get("skin_id")}:
            if skin_key:
                skins_by_id.setdefault(str(skin_key).strip().casefold(), []).append(skin)

    async with db.acquire() as conn:
        async with conn.transaction():
            # Import a broad set of real CS2 cases from the public catalog.
            # Existing case rows and opening history are never deleted or rewritten.
            for catalog_case in [
                item for item in catalog_cases
                if "case" in item["name"].lower()
            ][:24]:
                catalog_case_id = await conn.fetchval("""
                    INSERT INTO cases(title, description, price_minor, currency, active, image_url)
                    SELECT $1, 'Демонстрационный кейс CS2 — виртуальные предметы',
                           0, 'RUB', TRUE, $2
                    WHERE NOT EXISTS (SELECT 1 FROM cases WHERE title=$1)
                    RETURNING id
                """, catalog_case["name"], catalog_case["image_url"])
                if not catalog_case_id:
                    catalog_case_id = await conn.fetchval(
                        "SELECT id FROM cases WHERE title=$1 ORDER BY id LIMIT 1",
                        catalog_case["name"],
                    )
                await conn.execute("""
                    UPDATE cases SET image_url=COALESCE(image_url, $2)
                    WHERE id=$1
                """, catalog_case_id, catalog_case["image_url"])

                # Match source crate relationships by stable ID and display name.
                # Earlier builds copied Premium items into crates with missing relationships;
                # deactivate those incorrect rows without deleting opening history.
                case_keys = {
                    str(catalog_case.get("id") or "").strip().casefold(),
                    str(catalog_case.get("name") or "").strip().casefold(),
                }
                crate_skins_by_key = [
                    skins_by_crate.get(key, []) for key in case_keys if key
                ]
                crate_skins_map = {}
                for group in crate_skins_by_key:
                    for skin in group:
                        crate_skins_map[skin["market_hash_name"]] = skin
                # crates.json is the authoritative membership source: its contains
                # and contains_rare entries reference base skin IDs. Expand each
                # base item into all wear/StatTrak/Souvenir variants in skins_not_grouped.
                for contained_id in catalog_case.get("contains_ids", []):
                    for skin in skins_by_id.get(str(contained_id).strip().casefold(), []):
                        crate_skins_map[skin["market_hash_name"]] = skin
                crate_skins = list(crate_skins_map.values())
                if catalog_case["name"].casefold() != "премиум кейс":
                    await conn.execute("""
                        UPDATE case_items ci
                        SET is_active=FALSE
                        WHERE ci.case_id=$1
                          AND ci.market_hash_name IS NULL
                          AND EXISTS (
                              SELECT 1 FROM cases premium
                              JOIN case_items pi ON pi.case_id=premium.id
                              WHERE premium.title='Премиум кейс'
                                AND pi.name=ci.name
                          )
                    """, catalog_case_id)
                already_seeded = await conn.fetchval("""
                    SELECT EXISTS(
                        SELECT 1 FROM case_items
                        WHERE case_id=$1 AND market_hash_name IS NOT NULL
                    )
                """, catalog_case_id)
                if crate_skins and not already_seeded:
                    market_names = [skin["market_hash_name"] for skin in crate_skins]
                    cached_rows = await conn.fetch("""
                        SELECT market_hash_name, lowest_price
                        FROM cs2_steam_prices
                        WHERE market_hash_name = ANY($1::text[])
                          AND price_source <> 'steam'
                    """, market_names)
                    cached_prices = {
                        row["market_hash_name"]: steam_price_to_minor(row["lowest_price"])
                        for row in cached_rows
                    }
                    records = []
                    for skin in crate_skins:
                        value_minor = cached_prices.get(skin["market_hash_name"]) or int(skin["value_minor"])
                        records.append((
                            catalog_case_id, skin["name"], skin["rarity"],
                            cs2_case_weight(skin["rarity"]), skin["image_url"],
                            value_minor, skin["market_hash_name"], skin["wear"],
                            skin["stattrak"], skin["souvenir"], skin["phase"],
                        ))
                    await conn.executemany("""
                        INSERT INTO case_items(
                            case_id, name, rarity, weight, image_url, value_minor,
                            market_hash_name, wear, stattrak, souvenir, phase
                        )
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                        ON CONFLICT (case_id, market_hash_name)
                        WHERE market_hash_name IS NOT NULL DO NOTHING
                    """, records)
                elif not crate_skins:
                    # Never borrow Premium contents: an empty source mapping must stay empty
                    # until the real crate-to-skin relationship is resolved.
                    logger.warning(
                        "No source skin membership found for CS2 crate %s (%s)",
                        catalog_case.get("name"), catalog_case.get("id")
                    )
        cases = await conn.fetch("""
            SELECT id, title, description, price_minor, currency, image_url
            FROM cases
            WHERE active = TRUE
            ORDER BY id
        """)
        items = await conn.fetch("""
            SELECT ci.case_id, ci.id, ci.name, ci.rarity, ci.weight,
                   ci.image_url, ci.value_minor, ci.market_hash_name, ci.wear,
                   ci.stattrak, ci.souvenir, ci.phase,
                   p.lowest_price, p.fetched_at
            FROM case_items ci
            JOIN cases c ON c.id = ci.case_id
            LEFT JOIN cs2_steam_prices p ON p.market_hash_name=ci.market_hash_name
                AND p.price_source <> 'steam'
            WHERE c.active = TRUE AND ci.is_active = TRUE
            ORDER BY ci.case_id, ci.weight DESC
        """)
    # Gradually warm Steam RUB prices for every skin in the active cases.
    # The worker remains throttled (~one request per 3.2s); the queue is bounded.
    # Only queue prices that are missing or older than 12 hours. Repeated
    # visits to the Cases tab must not enqueue already-fresh marketplace quotes.
    now_utc = datetime.now(timezone.utc)
    queue_cs2_price_refresh([
        {"market_hash_name": item["market_hash_name"]}
        for item in items
        if item["market_hash_name"] and (
            not item["fetched_at"] or
            (now_utc - item["fetched_at"]).total_seconds() > 43200
        )
    ])

    grouped = {}
    for item in items:
        fresh = bool(item["fetched_at"] and
                     (datetime.now(timezone.utc) - item["fetched_at"]).total_seconds() <= 43200)
        market_minor = steam_price_to_minor(item["lowest_price"]) if fresh else None
        grouped.setdefault(item["case_id"], []).append({
            "id": item["id"], "name": item["name"],
            "rarity": item["rarity"], "weight": item["weight"],
            "image_url": item["image_url"] or skin_images.get(item["name"]),
            "value_minor": item["value_minor"],
            "market_price_minor": market_minor,
            "market_price": item["lowest_price"] if fresh else None,
            "market_hash_name": item["market_hash_name"],
            "wear": item["wear"], "stattrak": item["stattrak"],
            "souvenir": item["souvenir"], "phase": item["phase"],
        })
    result = []
    # Keep the initial Mini App payload small: the full weighted pool stays in
    # PostgreSQL and open_case() still draws from every active item. The client
    # only needs a compact preview pool for the reel and the visible item list.
    preview_limit = 48
    for case in cases:
        contents = grouped.get(case["id"], [])
        total = sum(x["weight"] for x in contents)
        visible_contents = contents[:preview_limit]
        result.append({
            "id": case["id"], "title": case["title"],
            "description": case["description"],
            "price_minor": case["price_minor"],
            "currency": case["currency"],
            "image_url": case["image_url"] or fallback_case_image,
            "demo_only": True,
            "items_total": len(contents),
            "items": [{
                **item,
                "chance_percent": round(item["weight"] * 100 / total, 2) if total else 0
            } for item in visible_contents],
        })
    return web.json_response({"items": result})

async def open_case(request):
    user = verify_telegram_user(request)
    try:
        case_id = int(request.match_info["case_id"])
    except ValueError:
        raise web.HTTPBadRequest(text="Некорректный ID кейса")

    async with db.acquire() as conn:
        async with conn.transaction():
            case = await conn.fetchrow("""
                SELECT id, title, price_minor, active
                FROM cases WHERE id = $1 FOR SHARE
            """, case_id)
            if not case or not case["active"]:
                raise web.HTTPNotFound(text="Кейс не найден")
            if case["price_minor"] != 0:
                raise web.HTTPForbidden(text="Этот кейс пока недоступен для открытия")
            contents = await conn.fetch("""
                SELECT id, name, rarity, weight, image_url, value_minor,
                       market_hash_name, wear, stattrak, souvenir, phase
                FROM case_items WHERE case_id = $1 AND is_active = TRUE ORDER BY id
            """, case_id)
            total = sum(row["weight"] for row in contents)
            if total <= 0:
                raise web.HTTPConflict(text="В кейсе пока нет предметов")
            roll = secrets.randbelow(total)
            cursor = 0
            selected = contents[-1]
            for item in contents:
                cursor += item["weight"]
                if roll < cursor:
                    selected = item
                    break
            selected = dict(selected)
            if not selected["image_url"]:
                cached_skins = CS2_CATALOG_CACHE.get("skins", [])
                selected["image_url"] = next(
                    (item["image_url"] for item in cached_skins
                     if item["market_hash_name"] == selected.get("market_hash_name")
                     or item["name"].casefold() == selected["name"].casefold()),
                    None,
                )
            market_minor = None
            if selected.get("market_hash_name"):
                price_row = await conn.fetchrow("""
                    SELECT lowest_price, fetched_at FROM cs2_steam_prices
                    WHERE market_hash_name=$1 AND price_source <> 'steam'
                """, selected["market_hash_name"])
                fresh = bool(price_row and
                    (datetime.now(timezone.utc) - price_row["fetched_at"]).total_seconds() <= 43200)
                market_minor = steam_price_to_minor(price_row["lowest_price"]) if fresh else None
                if market_minor:
                    selected["value_minor"] = market_minor
                else:
                    queue_cs2_price_refresh([{"market_hash_name": selected["market_hash_name"]}])
            inventory_id = await conn.fetchval("""
                INSERT INTO user_inventory(
                    user_id, item_type, item_name, rarity, image_url,
                    value_minor, source, market_hash_name, wear,
                    stattrak, souvenir, phase
                )
                VALUES($1, 'skin', $2, $3, $4, $5, 'case', $6, $7, $8, $9, $10)
                RETURNING id
            """, user["id"], selected["name"], selected["rarity"],
                 selected["image_url"], selected["value_minor"],
                 selected.get("market_hash_name"), selected.get("wear"),
                 selected.get("stattrak", False), selected.get("souvenir", False),
                 selected.get("phase"))

            opening_id = await conn.fetchval("""
                INSERT INTO case_openings(user_id, case_id, item_id, item_name, rarity)
                VALUES($1, $2, $3, $4, $5) RETURNING id
            """, user["id"], case_id, selected["id"], selected["name"], selected["rarity"])
            await conn.execute("""
                INSERT INTO audit_log(actor_id, action, details)
                VALUES($1, 'case_opened', $2::jsonb)
            """, user["id"], json.dumps({
                "opening_id": opening_id, "case_id": case_id,
                "item_id": selected["id"], "roll": roll, "total_weight": total
            }))

    return web.json_response({
        "ok": True, "opening_id": opening_id, "case_id": case_id, "case_title": case["title"],
        "item": {
            "id": selected["id"], "name": selected["name"],
            "rarity": selected["rarity"],
            "wear": selected.get("wear"),
            "stattrak": selected.get("stattrak", False),
            "souvenir": selected.get("souvenir", False),
            "phase": selected.get("phase"),
            "image_url": selected.get("image_url"),
            "value_minor": selected["value_minor"],
            "market_price_minor": market_minor,
            "sell_value_minor": (market_minor * 90 // 100) if market_minor else None,
            "inventory_id": inventory_id,
            "chance_percent": round(selected["weight"] * 100 / total, 2),
        },
        "demo_only": True,
        "message": "Виртуальный предмет симуляции; не является предметом Steam."
    })



async def get_wallet(request):
    user = verify_telegram_user(request)
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO wallet_accounts(user_id) VALUES($1)
            ON CONFLICT (user_id) DO NOTHING
        """, user["id"])
        row = await conn.fetchrow("""
            SELECT balance_minor, updated_at FROM wallet_accounts WHERE user_id=$1
        """, user["id"])
        ledger = await conn.fetch("""
            SELECT amount_minor, reason, created_at FROM wallet_ledger
            WHERE user_id=$1 ORDER BY id DESC LIMIT 20
        """, user["id"])
    return web.json_response({
        "balance_minor": row["balance_minor"], "currency": "RUB",
        "virtual_only": True, "updated_at": row["updated_at"].isoformat(),
        "ledger": [{
            "amount_minor": item["amount_minor"], "reason": item["reason"],
            "created_at": item["created_at"].isoformat()
        } for item in ledger]
    })


async def sell_inventory_item(request):
    user = verify_telegram_user(request)
    try:
        inventory_id = int(request.match_info["inventory_id"])
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="Некорректный ID предмета")

    async with db.acquire() as conn:
        async with conn.transaction():
            item = await conn.fetchrow("""
                SELECT id, item_type, item_name, market_hash_name
                FROM user_inventory
                WHERE id=$1 AND user_id=$2 AND consumed_at IS NULL
                FOR UPDATE
            """, inventory_id, user["id"])
            if not item:
                raise web.HTTPNotFound(text="Предмет уже продан, использован или не найден")
            if item["item_type"] != "skin" or not item["market_hash_name"]:
                raise web.HTTPBadRequest(text="Этот тип предмета нельзя продать за виртуальные рубли")

            price = await conn.fetchrow("""
                SELECT lowest_price, fetched_at FROM cs2_steam_prices
                WHERE market_hash_name=$1 AND price_source <> 'steam'
            """, item["market_hash_name"])
            fresh = bool(price and
                         (datetime.now(timezone.utc) - price["fetched_at"]).total_seconds() <= 43200)
            market_minor = steam_price_to_minor(price["lowest_price"]) if fresh else None
            if not market_minor:
                queue_cs2_price_refresh([{"market_hash_name": item["market_hash_name"]}])
                raise web.HTTPConflict(text="Цена скина обновляется с Lis-Skins / Market.CSGO. Попробуйте продать предмет чуть позже.")

            # Demo marketplace credit: 90% of the fresh Steam lowest listing.
            sale_minor = market_minor * 90 // 100
            if sale_minor <= 0:
                raise web.HTTPConflict(text="Не удалось рассчитать цену продажи")
            await conn.execute("""
                INSERT INTO wallet_accounts(user_id) VALUES($1)
                ON CONFLICT (user_id) DO NOTHING
            """, user["id"])
            await conn.execute("""
                UPDATE user_inventory SET consumed_at=NOW() WHERE id=$1
            """, inventory_id)
            await conn.execute("""
                INSERT INTO wallet_ledger(user_id, inventory_id, amount_minor, currency, reason)
                VALUES($1,$2,$3,'RUB','virtual_skin_sale')
            """, user["id"], inventory_id, sale_minor)
            wallet = await conn.fetchrow("""
                UPDATE wallet_accounts
                SET balance_minor=balance_minor+$2, updated_at=NOW()
                WHERE user_id=$1 RETURNING balance_minor
            """, user["id"], sale_minor)

    return web.json_response({
        "ok": True, "inventory_id": inventory_id,
        "item_name": item["item_name"], "credited_minor": sale_minor,
        "balance_minor": wallet["balance_minor"], "currency": "RUB",
        "virtual_only": True,
        "message": "Предмет обменян на виртуальные рубли. Вывод средств отключён."
    })


async def get_inventory(request):
    user = verify_telegram_user(request)
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT i.id, i.item_type, i.item_name, i.rarity, i.image_url,
                   i.value_minor, i.source, i.created_at, i.market_hash_name,
                   i.wear, i.stattrak, i.souvenir, i.phase,
                   p.lowest_price, p.fetched_at
            FROM user_inventory i
            LEFT JOIN cs2_steam_prices p ON p.market_hash_name=i.market_hash_name
                AND p.price_source <> 'steam'
            WHERE i.user_id = $1 AND i.consumed_at IS NULL
            ORDER BY i.id DESC LIMIT 100
        """, user["id"])
    items = []
    for row in rows:
        fresh = bool(row["fetched_at"] and
                     (datetime.now(timezone.utc) - row["fetched_at"]).total_seconds() <= 43200)
        market_minor = steam_price_to_minor(row["lowest_price"]) if fresh else None
        if row["market_hash_name"] and not fresh:
            queue_cs2_price_refresh([{"market_hash_name": row["market_hash_name"]}])
        items.append({
            "id": row["id"], "item_type": row["item_type"],
            "item_name": row["item_name"], "rarity": row["rarity"],
            "image_url": row["image_url"],
            "value_minor": market_minor or row["value_minor"],
            "market_price_minor": market_minor,
            "sell_value_minor": (market_minor * 90 // 100) if market_minor else None,
            "price_updated_at": row["fetched_at"].isoformat() if fresh else None,
            "source": row["source"], "created_at": row["created_at"].isoformat(),
            "market_hash_name": row["market_hash_name"], "wear": row["wear"],
            "stattrak": row["stattrak"], "souvenir": row["souvenir"],
            "phase": row["phase"]
        })
    return web.json_response({"items": items})

# ============================================================
# CS2 PUBLIC CATALOG AND VIRTUAL UPGRADER
# ============================================================

CS2_CATALOG_URLS = {
    "skins": "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins_not_grouped.json",
    "cases": "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/crates.json",
}

def cs2_virtual_value(rarity) -> int:
    label = str((rarity or {}).get("name", rarity or "")).lower() if isinstance(rarity, dict) else str(rarity or "").lower()
    if any(x in label for x in ("extraordinary", "covert", "тайное", "контрабанд")):
        return 80000
    if any(x in label for x in ("classified", "засекреч")):
        return 30000
    if any(x in label for x in ("restricted", "запрещ")):
        return 12000
    if any(x in label for x in ("mil-spec", "армейское", "армейск")):
        return 5000
    if any(x in label for x in ("industrial", "промышлен")):
        return 2500
    return 1500


def cs2_case_weight(rarity) -> int:
    label = str(rarity or "").lower()
    if any(x in label for x in ("covert", "тайное", "extraordinary")):
        return 2
    if any(x in label for x in ("classified", "засекреч")):
        return 8
    if any(x in label for x in ("restricted", "запрещ")):
        return 24
    if any(x in label for x in ("mil-spec", "армейск")):
        return 72
    if any(x in label for x in ("industrial", "промышлен")):
        return 150
    return 220

async def load_cs2_catalog(kind: str):
    if kind not in CS2_CATALOG_URLS:
        raise web.HTTPBadRequest(text="Неизвестный тип каталога")
    now = time.time()
    if kind in CS2_CATALOG_CACHE and now - CS2_CATALOG_CACHE_AT.get(kind, 0) < 21600:
        return CS2_CATALOG_CACHE[kind]
    timeout = aiohttp.ClientTimeout(total=18)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(CS2_CATALOG_URLS[kind], headers={"User-Agent": "QR-BOT-PRO/1.0"}) as response:
                if response.status != 200:
                    raise RuntimeError(f"catalog upstream returned {response.status}")
                raw = await response.json(content_type=None)
        if not isinstance(raw, list):
            raise RuntimeError("catalog payload is not a list")
        normalized = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            name = str(entry.get("name") or "").strip()
            image_url = str(entry.get("image") or "").strip()
            if not name or not image_url.startswith("https://"):
                continue
            rarity = entry.get("rarity") or {}
            rarity_name = rarity.get("name", "Неизвестно") if isinstance(rarity, dict) else str(rarity)
            wear = entry.get("wear") or {}
            wear_name = wear.get("name", "") if isinstance(wear, dict) else str(wear or "")
            weapon = entry.get("weapon") or {}
            weapon_name = weapon.get("name", "") if isinstance(weapon, dict) else str(weapon or "")
            market_hash_name = str(entry.get("market_hash_name") or name).strip()
            item = {
                "id": str(entry.get("id") or market_hash_name),
                "skin_id": str(entry.get("skin_id") or entry.get("id") or market_hash_name),
                "name": market_hash_name if kind == "skins" else name,
                "base_name": name,
                "contains_ids": [
                    str(item.get("id")) for item in (
                        list(entry.get("contains") or []) + list(entry.get("contains_rare") or [])
                    )
                    if isinstance(item, dict) and item.get("id") is not None
                ] if kind == "cases" else [],
                "market_hash_name": market_hash_name,
                "weapon": weapon_name,
                "wear": wear_name,
                "rarity": rarity_name,
                "stattrak": bool(entry.get("stattrak", False)),
                "souvenir": bool(entry.get("souvenir", False)),
                "phase": str(entry.get("phase") or ""),
                "float_min": entry.get("min_float"),
                "float_max": entry.get("max_float"),
                "crate_ids": [
                    str(crate.get("id")) for crate in (entry.get("crates") or [])
                    if isinstance(crate, dict) and crate.get("id") is not None
                ],
                "crate_names": [
                    str(crate.get("name")).strip().casefold()
                    for crate in (entry.get("crates") or [])
                    if isinstance(crate, dict) and crate.get("name")
                ],
                "image_url": image_url,
                "value_minor": cs2_virtual_value(rarity),
                "item_type": "skin" if kind == "skins" else "case",
            }
            normalized.append(item)
        normalized.sort(key=lambda x: (x["value_minor"], x["name"]))
        CS2_CATALOG_CACHE[kind] = normalized
        CS2_CATALOG_CACHE_AT[kind] = now
        return normalized
    except web.HTTPException:
        raise
    except Exception:
        logger.exception("Could not load CS2 %s catalog", kind)
        if kind in CS2_CATALOG_CACHE:
            return CS2_CATALOG_CACHE[kind]
        raise web.HTTPServiceUnavailable(text="Каталог CS2 временно недоступен")



def steam_price_to_minor(value):
    """Parse a localized Steam RUB price into kopecks without guessing missing values."""
    if not value:
        return None
    raw = re.sub(r"[^0-9,\.]", "", str(value))
    if not raw:
        return None
    if "," in raw and "." in raw:
        decimal_sep = "," if raw.rfind(",") > raw.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        raw = raw.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif "," in raw:
        raw = raw.replace(",", ".")
    try:
        amount = Decimal(raw)
        if amount <= 0:
            return None
        return int(amount * 100)
    except (InvalidOperation, ValueError):
        return None


async def get_usd_rub_rate(session):
    """Fetch and cache the current USD/RUB reference rate for marketplace quotes."""
    global USD_RUB_RATE, USD_RUB_RATE_AT
    if USD_RUB_RATE and time.time() - USD_RUB_RATE_AT < 3600:
        return USD_RUB_RATE
    async with session.get("https://open.er-api.com/v6/latest/USD") as response:
        if response.status != 200:
            raise RuntimeError(f"FX service returned HTTP {response.status}")
        payload = await response.json(content_type=None)
    rate = (payload.get("rates") or {}).get("RUB")
    if not rate or float(rate) <= 0:
        raise RuntimeError("USD/RUB rate is missing")
    USD_RUB_RATE = Decimal(str(rate))
    USD_RUB_RATE_AT = time.time()
    return USD_RUB_RATE


async def cs2_steam_price_worker():
    """Fetch Lis-Skins first, then Market.CSGO as a per-item fallback."""
    timeout = aiohttp.ClientTimeout(total=25)
    headers = {"Accept": "application/json"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        while True:
            first = await CS2_PRICE_QUEUE.get()
            batch = [first]
            while len(batch) < 100:
                try:
                    batch.append(CS2_PRICE_QUEUE.get_nowait())
                except asyncio.QueueEmpty:
                    break
            retry_later = False
            try:
                if not APISKINS_API_KEY:
                    logger.warning("APISKINS_API_KEY is not configured; marketplace price refresh is paused")
                else:
                    payload = {
                        "market_hash_names": batch,
                        "providers": ["lis_skins", "market_csgo", "cs_float", "skinport", "dmarket", "waxpeer"],
                    }
                    async with session.post(
                        "https://api.apiskins.com/v1/offers/batch",
                        json=payload,
                        headers={**headers, "X-API-KEY": APISKINS_API_KEY},
                    ) as response:
                        if response.status == 429:
                            logger.warning("API Skins rate limited; retrying marketplace batch in 60 seconds")
                            retry_later = True
                        elif response.status == 200:
                            offers_payload = await response.json(content_type=None)
                            rate = await get_usd_rub_rate(session)
                            updates = []
                            for item in offers_payload if isinstance(offers_payload, list) else []:
                                by_provider = {
                                    offer.get("provider"): offer.get("price")
                                    for offer in item.get("offers", [])
                                    if isinstance(offer, dict)
                                }
                                provider_priority = (
                                    "lis_skins", "market_csgo", "cs_float",
                                    "skinport", "dmarket", "waxpeer"
                                )
                                provider = next(
                                    (name for name in provider_priority if by_provider.get(name) is not None),
                                    None,
                                )
                                usd_price = by_provider.get(provider) if provider else None
                                if not usd_price:
                                    continue
                                try:
                                    rub_price = (Decimal(str(usd_price)) * rate).quantize(Decimal("0.01"))
                                    if rub_price > 0:
                                        updates.append((item["market_hash_name"], f"{rub_price} ₽", provider))
                                except (InvalidOperation, KeyError, TypeError):
                                    continue
                            if updates:
                                async with db.acquire() as conn:
                                    await conn.executemany("""
                                        INSERT INTO cs2_steam_prices(
                                            market_hash_name, lowest_price, median_price,
                                            volume, currency, fetched_at, price_source
                                        )
                                        VALUES($1, $2, NULL, NULL, 'RUB', NOW(), $3)
                                        ON CONFLICT (market_hash_name) DO UPDATE SET
                                            lowest_price=EXCLUDED.lowest_price,
                                            median_price=NULL,
                                            volume=NULL,
                                            currency='RUB',
                                            fetched_at=NOW(),
                                            price_source=EXCLUDED.price_source
                                    """, updates)
                            logger.info(
                                "Marketplace prices refreshed: %s requested, %s updated",
                                len(batch), len(updates)
                            )
                        else:
                            body = (await response.text())[:400]
                            logger.warning("API Skins price request returned HTTP %s: %s", response.status, body)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Marketplace price refresh failed for batch of %s items", len(batch), exc_info=True)
            finally:
                for market_hash_name in batch:
                    CS2_PRICE_QUEUED.discard(market_hash_name)
                    CS2_PRICE_QUEUE.task_done()
                if retry_later:
                    await asyncio.sleep(60)
                    for market_hash_name in batch:
                        if market_hash_name not in CS2_PRICE_QUEUED:
                            CS2_PRICE_QUEUED.add(market_hash_name)
                            await CS2_PRICE_QUEUE.put(market_hash_name)
            await asyncio.sleep(0.25)


def queue_cs2_price_refresh(items):
    """Queue a bounded batch of missing/stale prices; duplicates are coalesced."""
    for item in items:
        if CS2_PRICE_QUEUE.qsize() >= 1000:
            break
        market_name = item.get("market_hash_name")
        if market_name and market_name not in CS2_PRICE_QUEUED:
            CS2_PRICE_QUEUED.add(market_name)
            CS2_PRICE_QUEUE.put_nowait(market_name)


async def get_cs2_skins(request):
    catalog = await load_cs2_catalog("skins")
    query = request.query.get("q", "").strip().casefold()[:80]
    wear_filter = request.query.get("wear", "").strip().casefold()
    rarity_filter = request.query.get("rarity", "").strip().casefold()
    phase_filter = request.query.get("phase", "").strip().casefold()
    stattrak_only = request.query.get("stattrak") == "1"
    souvenir_only = request.query.get("souvenir") == "1"
    # The public skin catalog is intentionally bounded to the requested
    # market-price range. Defaults include both endpoints: 2 RUB .. 100,000 RUB.
    min_price_minor = steam_price_to_minor(request.query.get("min_price") or "2")
    max_price_minor = steam_price_to_minor(request.query.get("max_price") or "100000")
    min_price_minor = max(200, min_price_minor or 200)
    max_price_minor = min(10000000, max_price_minor or 10000000)
    if min_price_minor > max_price_minor:
        raise web.HTTPBadRequest(text="Минимальная цена не может быть выше максимальной")
    sort = request.query.get("sort", "name")
    try:
        page = max(0, min(int(request.query.get("page", "0")), 10000))
        limit = max(1, min(int(request.query.get("limit", "24")), 40))
    except ValueError:
        page, limit = 0, 48

    # Search-first UX: never dump the full 10k+ catalogue into the scrolling UI.
    if len(query) < 2:
        return web.json_response({
            "items": [], "total": len(catalog), "matched": 0,
            "page": 0, "limit": 24, "has_more": False,
            "search_required": True, "price_currency": "RUB",
            "price_source": "Lis-Skins / Market.CSGO",
        })
    terms = [term for term in query.split() if term]
    items = [
        item for item in catalog
        if all(term in (
            item["name"] + " " + item.get("base_name", "") + " " +
            item.get("weapon", "") + " " + item.get("rarity", "") + " " +
            item.get("wear", "") + " " + item.get("phase", "") +
            (" StatTrak" if item.get("stattrak") else "") +
            (" Souvenir" if item.get("souvenir") else "")
        ).casefold() for term in terms)
    ]
    if wear_filter:
        items = [x for x in items if x.get("wear", "").casefold() == wear_filter]
    if rarity_filter:
        items = [x for x in items if x.get("rarity", "").casefold() == rarity_filter]
    if phase_filter:
        items = [x for x in items if x.get("phase", "").casefold() == phase_filter]
    if stattrak_only:
        items = [x for x in items if x.get("stattrak")]
    if souvenir_only:
        items = [x for x in items if x.get("souvenir")]

    all_prices = {}
    price_pending_count = 0
    if items and (
        sort in {"price_asc", "price_desc"}
        or min_price_minor is not None
        or max_price_minor is not None
    ):
        all_names = [x["market_hash_name"] for x in items]
        async with db.acquire() as conn:
            all_price_rows = await conn.fetch("""
                SELECT market_hash_name, lowest_price, fetched_at
                FROM cs2_steam_prices
                WHERE market_hash_name = ANY($1::text[])
                  AND price_source <> 'steam'
            """, all_names)
        now_utc = datetime.now(timezone.utc)
        all_prices = {
            row["market_hash_name"]: steam_price_to_minor(row["lowest_price"])
            for row in all_price_rows
            if (now_utc - row["fetched_at"]).total_seconds() <= 43200
            and steam_price_to_minor(row["lowest_price"]) is not None
        }
        # If the user applies a price filter, request prices for the next
        # 100 matching skins in one API Skins batch. Never fabricate a price.
        missing_price_items = [
            x for x in items if x["market_hash_name"] not in all_prices
        ]
        price_pending_count = len(missing_price_items)
        queue_cs2_price_refresh(missing_price_items[:100])

    if min_price_minor is not None or max_price_minor is not None:
        items = [
            x for x in items
            if all_prices.get(x["market_hash_name"]) is not None
            and (min_price_minor is None or all_prices[x["market_hash_name"]] >= min_price_minor)
            and (max_price_minor is None or all_prices[x["market_hash_name"]] <= max_price_minor)
        ]

    if sort in {"price_asc", "price_desc"}:
        items.sort(
            key=lambda x: (
                all_prices.get(x["market_hash_name"]) is None,
                -(all_prices.get(x["market_hash_name"]) or 0)
                if sort == "price_desc"
                else (all_prices.get(x["market_hash_name"]) or 0),
            )
        )
    elif sort == "rarity":
        items.sort(key=lambda x: x.get("rarity", ""))
    else:
        items.sort(key=lambda x: x.get("name", "").casefold())

    total = len(items)
    start = page * limit
    page_items = items[start:start + limit]
    names = [x["market_hash_name"] for x in page_items]

    async with db.acquire() as conn:
        price_rows = await conn.fetch("""
            SELECT market_hash_name, lowest_price, median_price, volume, fetched_at
            FROM cs2_steam_prices
            WHERE market_hash_name = ANY($1::text[])
              AND price_source <> 'steam'
        """, names) if names else []
    prices = {row["market_hash_name"]: row for row in price_rows}

    enriched = []
    stale = []
    now = time.time()
    for source in page_items:
        item = dict(source)
        price = prices.get(item["market_hash_name"])
        if price:
            item["steam_lowest_price"] = price["lowest_price"]
            item["steam_median_price"] = price["median_price"]
            item["steam_volume"] = price["volume"]
            item["steam_fetched_at"] = price["fetched_at"].isoformat()
            is_stale = (now - price["fetched_at"].timestamp()) > 43200
            item["market_price_minor"] = None if is_stale else steam_price_to_minor(price["lowest_price"])
            if item["market_price_minor"]:
                item["value_minor"] = item["market_price_minor"]
        else:
            item["steam_lowest_price"] = None
            item["steam_median_price"] = None
            item["steam_volume"] = None
            item["steam_fetched_at"] = None
            item["market_price_minor"] = None
            is_stale = True
        if is_stale:
            stale.append(source)
        enriched.append(item)

    # Refresh a small batch asynchronously; cached values remain visible immediately.
    queue_cs2_price_refresh(stale[:8])
    return web.json_response({
        "items": enriched,
        "total": total,
        "matched": total,
        "page": page,
        "limit": limit,
        "has_more": start + len(page_items) < total,
        "price_source": "Lis-Skins (primary) / Market.CSGO (fallback)",
        "price_currency": "RUB",
        "price_cache_hours": 12,
        "price_range_minor": {"min": 200, "max": 10000000},
        "price_pending_count": price_pending_count,
        "demo_only": True,
    })


async def get_cs2_catalog(request):
    kind = request.query.get("kind", "skins")
    items = await load_cs2_catalog(kind)
    try:
        limit = min(max(int(request.query.get("limit", "120")), 1), 200)
    except ValueError:
        limit = 120
    selected = items[-limit:]
    if kind == "skins" and selected:
        names = [x.get("market_hash_name") for x in selected if x.get("market_hash_name")]
        async with db.acquire() as conn:
            rows = await conn.fetch("""
                SELECT market_hash_name, lowest_price, median_price, volume, fetched_at
                FROM cs2_steam_prices WHERE market_hash_name = ANY($1::text[])
                    AND price_source <> 'steam'
            """, names) if names else []
        price_map = {r["market_hash_name"]: r for r in rows}
        for item in selected:
            price = price_map.get(item.get("market_hash_name"))
            if price:
                item["steam_lowest_price"] = price["lowest_price"]
                item["steam_median_price"] = price["median_price"]
                item["steam_volume"] = price["volume"]
                market_minor = steam_price_to_minor(price["lowest_price"])
                if market_minor:
                    item["value_minor"] = market_minor
    return web.json_response({
        "items": selected,
        "source": "ByMykel/CSGO-API",
        "demo_only": True,
    })

async def claim_demo_case(request):
    user = verify_telegram_user(request)
    try:
        case_id = int(request.match_info["case_id"])
    except ValueError:
        raise web.HTTPBadRequest(text="Некорректный ID кейса")
    try:
        catalog_cases = await load_cs2_catalog("cases")
        fallback_image = next(
            (item["image_url"] for item in catalog_cases if "case" in item["name"].lower()),
            catalog_cases[0]["image_url"] if catalog_cases else None,
        )
    except Exception:
        fallback_image = None
    async with db.acquire() as conn:
        async with conn.transaction():
            case = await conn.fetchrow(
                "SELECT id, title, image_url FROM cases WHERE id=$1 AND active=TRUE",
                case_id,
            )
            if not case:
                raise web.HTTPNotFound(text="Кейс не найден")
            claimed = await conn.fetchval("""
                INSERT INTO demo_case_claims(user_id, claim_date)
                VALUES($1, CURRENT_DATE)
                ON CONFLICT DO NOTHING
                RETURNING user_id
            """, user["id"])
            if not claimed:
                raise web.HTTPConflict(text="Бесплатный тестовый кейс уже получен сегодня")
            inventory_id = await conn.fetchval("""
                INSERT INTO user_inventory(
                    user_id, item_type, item_name, rarity, image_url,
                    value_minor, source
                )
                VALUES($1, 'case', $2, 'Тестовый кейс', $3, 1000, 'daily_demo')
                RETURNING id
            """, user["id"], case["title"], case["image_url"])
    return web.json_response({"ok": True, "inventory_id": inventory_id, "message": "Тестовый кейс добавлен в инвентарь"})

async def suggest_upgrade_target(request):
    user = verify_telegram_user(request)
    try:
        data = await request.json()
        source_id = int(data.get("source_inventory_id"))
        item_type = str(data.get("item_type", "skin"))
        mode = str(data.get("mode", "multiplier"))
        raw_value = float(data.get("value"))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Передайте корректный режим и значение")
    if item_type != "skin":
        raise web.HTTPBadRequest(text="Автоподбор по X и процентам доступен для скинов")
    if mode not in {"multiplier", "chance"}:
        raise web.HTTPBadRequest(text="Неизвестный режим подбора")
    if mode == "multiplier" and not 1.01 <= raw_value <= 500:
        raise web.HTTPBadRequest(text="Множитель должен быть от 1.01x до 500x")
    if mode == "chance" and not 0.1 <= raw_value < 75:
        raise web.HTTPBadRequest(text="Шанс должен быть от 0.1% до 75%")
    catalog = await load_cs2_catalog("skins")
    async with db.acquire() as conn:
        source = await conn.fetchrow("""
            SELECT id, item_type, item_name, value_minor, market_hash_name
            FROM user_inventory
            WHERE id=$1 AND user_id=$2 AND item_type='skin' AND consumed_at IS NULL
            FOR SHARE
        """, source_id, user["id"])
        if not source:
            raise web.HTTPNotFound(text="Исходный скин не найден в инвентаре")
        # Older case drops and inventory rows may predate market_hash_name.
        # Resolve them against the canonical CS2 catalog using the exact item label,
        # then persist the match additively so the user does not need to re-open a case.
        source_market_name = source["market_hash_name"]
        if not source_market_name:
            source_label = str(source["item_name"] or "").strip().casefold()
            matches = [
                x for x in catalog
                if str(x.get("name") or "").strip().casefold() == source_label
                or str(x.get("base_name") or "").strip().casefold() == source_label
            ]
            if matches:
                exact = [x for x in matches if str(x.get("name") or "").strip().casefold() == source_label]
                candidates = exact or matches
                candidate_names = [x.get("market_hash_name") for x in candidates if x.get("market_hash_name")]
                cached_match = await conn.fetchval("""
                    SELECT market_hash_name FROM cs2_steam_prices
                    WHERE market_hash_name = ANY($1::text[]) AND price_source <> 'steam'
                    ORDER BY fetched_at DESC LIMIT 1
                """, candidate_names) if candidate_names else None
                source_market_name = cached_match or (candidate_names[0] if candidate_names else None)
                if source_market_name:
                    await conn.execute(
                        "UPDATE user_inventory SET market_hash_name=$1 WHERE id=$2 AND user_id=$3 AND market_hash_name IS NULL",
                        source_market_name, source_id, user["id"]
                    )
        source_price = await conn.fetchrow("""
            SELECT lowest_price, fetched_at FROM cs2_steam_prices
            WHERE market_hash_name=$1 AND price_source <> 'steam'
        """, source_market_name) if source_market_name else None
        fresh_source = bool(source_price and
            (datetime.now(timezone.utc)-source_price["fetched_at"]).total_seconds() <= 43200)
        source_value = steam_price_to_minor(source_price["lowest_price"]) if fresh_source else None
        if not source_value:
            if not source_market_name:
                raise web.HTTPBadRequest(text="Не удалось однозначно сопоставить этот предмет с каталогом CS2. Выберите другой скин.")
            if not APISKINS_API_KEY:
                raise web.HTTPServiceUnavailable(text="Цены временно недоступны: не настроен ключ API Skins на сервере.")
            queue_cs2_price_refresh([{"market_hash_name": source_market_name}])
            return web.json_response({
                "pending": True,
                "stage": "source_price",
                "message": "Получаем актуальную цену исходного скина. Это может занять несколько секунд."
            }, status=202)
        desired = source_value * raw_value if mode == "multiplier" else source_value * 75.0 / raw_value
        names = [x.get("market_hash_name") for x in catalog if x.get("market_hash_name")]
        rows = await conn.fetch("""
            SELECT market_hash_name, lowest_price, fetched_at
            FROM cs2_steam_prices
            WHERE market_hash_name = ANY($1::text[])
              AND price_source <> 'steam'
        """, names)
    prices = {}
    stale_names = set()
    now = datetime.now(timezone.utc)
    for row in rows:
        minor = steam_price_to_minor(row["lowest_price"])
        fresh = (now-row["fetched_at"]).total_seconds() <= 43200
        if fresh and minor and 200 <= minor <= 10000000 and minor > source_value:
            prices[row["market_hash_name"]] = minor
        elif not fresh:
            stale_names.add(row["market_hash_name"])
    candidates = [x for x in catalog if x.get("market_hash_name") in prices]
    if not candidates:
        if not APISKINS_API_KEY:
            raise web.HTTPServiceUnavailable(text="Цены каталога недоступны: проверьте APISKINS_API_KEY в настройках Render.")
        # Queue a useful sample around the requested price instead of the first
        # eight alphabetic entries; one batch can contain up to 100 names.
        ranked = sorted(
            (x for x in catalog if x.get("market_hash_name")),
            key=lambda x: abs(cs2_virtual_value({"name": x.get("rarity", "")}) - desired) / max(desired, 1)
        )
        queue_cs2_price_refresh(ranked[:100])
        return web.json_response({
            "pending": True,
            "stage": "target_prices",
            "message": "Подбираем предметы по актуальным ценам. Подождите несколько секунд."
        }, status=202)
    candidates.sort(key=lambda x: abs(prices[x["market_hash_name"]]-desired)/max(desired,1))
    chosen = dict(candidates[0])
    chosen["market_price_minor"] = prices[chosen["market_hash_name"]]
    chosen["value_minor"] = chosen["market_price_minor"]
    chosen["suggested_multiplier"] = round(chosen["market_price_minor"]/source_value, 2)
    chosen["suggested_chance_percent"] = round(source_value/chosen["market_price_minor"]*75, 2)
    return web.json_response({"item": chosen, "source_value_minor": source_value,
        "desired_target_minor": round(desired), "mode": mode, "value": raw_value,
        "fresh_price_count": len(prices), "demo_only": True})


async def create_upgrade_attempt(request):
    user = verify_telegram_user(request)
    try:
        data = await request.json()
        source_id = int(data.get("source_inventory_id"))
        item_type = str(data.get("item_type", "skin"))
        target_id = str(data.get("target_id", ""))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Некорректные данные апгрейда")
    if item_type not in {"skin", "case"} or not target_id:
        raise web.HTTPBadRequest(text="Некорректный тип или цель апгрейда")
    catalog = await load_cs2_catalog("skins" if item_type == "skin" else "cases")
    target = next((x for x in catalog if x["id"] == target_id), None)
    if not target:
        raise web.HTTPNotFound(text="Целевой предмет не найден в каталоге")
    async with db.acquire() as conn:
        async with conn.transaction():
            source = await conn.fetchrow("""
                SELECT id, item_type, item_name, rarity, value_minor, market_hash_name
                FROM user_inventory
                WHERE id=$1 AND user_id=$2 AND item_type=$3 AND consumed_at IS NULL
                FOR UPDATE
            """, source_id, user["id"], item_type)
            if not source:
                raise web.HTTPNotFound(text="Предмет уже использован или не найден")
            source_value = int(source["value_minor"])
            target_value = int(target["value_minor"])
            if item_type == "skin":
                source_market_name = source["market_hash_name"]
                if not source_market_name:
                    source_label = str(source["item_name"] or "").strip().casefold()
                    matches = [
                        x for x in catalog
                        if str(x.get("name") or "").strip().casefold() == source_label
                        or str(x.get("base_name") or "").strip().casefold() == source_label
                    ]
                    if matches:
                        exact = [x for x in matches if str(x.get("name") or "").strip().casefold() == source_label]
                        candidates = exact or matches
                        candidate_names = [x.get("market_hash_name") for x in candidates if x.get("market_hash_name")]
                        cached_match = await conn.fetchval("""
                            SELECT market_hash_name FROM cs2_steam_prices
                            WHERE market_hash_name = ANY($1::text[]) AND price_source <> 'steam'
                            ORDER BY fetched_at DESC LIMIT 1
                        """, candidate_names) if candidate_names else None
                        source_market_name = cached_match or (candidate_names[0] if candidate_names else None)
                        if source_market_name:
                            await conn.execute(
                                "UPDATE user_inventory SET market_hash_name=$1 WHERE id=$2 AND user_id=$3 AND market_hash_name IS NULL",
                                source_market_name, source_id, user["id"]
                            )
                if not source_market_name:
                    raise web.HTTPBadRequest(text="Не удалось сопоставить исходный скин с каталогом CS2. Выберите другой предмет.")
                source_price = await conn.fetchrow("""
                    SELECT lowest_price, fetched_at FROM cs2_steam_prices
                    WHERE market_hash_name=$1 AND price_source <> 'steam'
                """, source_market_name)
                source_fresh = bool(source_price and
                    (datetime.now(timezone.utc) - source_price["fetched_at"]).total_seconds() <= 43200)
                source_market_minor = steam_price_to_minor(source_price["lowest_price"]) if source_fresh else None
                if not source_market_minor:
                    queue_cs2_price_refresh([{"market_hash_name": source_market_name}])
                    return web.json_response({"pending": True, "message": "Обновляем цену исходного скина. Повторите через несколько секунд."}, status=202)
                source_value = source_market_minor
                target_price = await conn.fetchrow("""
                    SELECT lowest_price, fetched_at FROM cs2_steam_prices
                    WHERE market_hash_name=$1 AND price_source <> 'steam'
                """, target["market_hash_name"])
                target_fresh = bool(target_price and
                    (datetime.now(timezone.utc) - target_price["fetched_at"]).total_seconds() <= 43200)
                target_market_minor = steam_price_to_minor(target_price["lowest_price"]) if target_fresh else None
                if not target_market_minor:
                    queue_cs2_price_refresh([{"market_hash_name": target["market_hash_name"]}])
                    return web.json_response({"pending": True, "message": "Обновляем цену целевого скина. Повторите через несколько секунд."}, status=202)
                if target_market_minor < 200 or target_market_minor > 10000000:
                    raise web.HTTPBadRequest(text="Для апгрейда доступны цели стоимостью от 2 до 100 000 ₽ по Lis-Skins / Market.CSGO.")
                target_value = target_market_minor
                target["value_minor"] = target_market_minor
                target["market_price_minor"] = target_market_minor
            elif target.get("market_hash_name"):
                market_price = await conn.fetchval("""
                    SELECT lowest_price FROM cs2_steam_prices
                    WHERE market_hash_name=$1 AND price_source <> 'steam'
                """, target["market_hash_name"])
                market_minor = steam_price_to_minor(market_price)
                if market_minor:
                    target_value = market_minor
            if target_value <= source_value:
                raise web.HTTPBadRequest(text="Выберите предмет дороже исходного")
            chance = min(75.0, max(0.01, round(source_value / target_value * 75, 2)))
            roll = secrets.randbelow(10000)
            success = roll < int(chance * 100)
            await conn.execute("""
                UPDATE user_inventory SET consumed_at=NOW() WHERE id=$1
            """, source_id)
            if success:
                await conn.execute("""
                    INSERT INTO user_inventory(
                        user_id, item_type, item_name, rarity, image_url,
                        value_minor, source, market_hash_name, wear,
                        stattrak, souvenir, phase
                    )
                    VALUES($1, $2, $3, $4, $5, $6, 'upgrade', $7, $8, $9, $10, $11)
                """, user["id"], item_type, target["name"], target["rarity"],
                     target["image_url"], target_value,
                     target.get("market_hash_name"), target.get("wear"),
                     target.get("stattrak", False), target.get("souvenir", False),
                     target.get("phase"))
            attempt_id = await conn.fetchval("""
                INSERT INTO upgrade_attempts(
                    user_id, item_type, source_inventory_id, source_name,
                    target_name, target_image_url, source_value_minor,
                    target_value_minor, chance_percent, roll_value, success
                )
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                RETURNING id
            """, user["id"], item_type, source_id, source["item_name"],
                 target["name"], target["image_url"], source_value,
                 target_value, chance, roll, success)
    return web.json_response({
        "ok": True, "attempt_id": attempt_id, "success": success,
        "chance_percent": chance, "roll_percent": roll / 100.0, "source_name": source["item_name"],
        "target": target, "demo_only": True,
        "message": "Успех! Предмет добавлен в инвентарь." if success else "Неудача. Виртуальный предмет ставки списан.",
    })

async def get_upgrade_history(request):
    user = verify_telegram_user(request)
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, item_type, source_name, target_name, source_value_minor,
                   target_value_minor, chance_percent, success, created_at
            FROM upgrade_attempts
            WHERE user_id=$1 ORDER BY id DESC LIMIT 50
        """, user["id"])
    return web.json_response({"items": [{
        "id": row["id"], "item_type": row["item_type"],
        "source_name": row["source_name"], "target_name": row["target_name"],
        "source_value_minor": row["source_value_minor"],
        "target_value_minor": row["target_value_minor"],
        "chance_percent": float(row["chance_percent"]),
        "success": row["success"], "created_at": row["created_at"].isoformat(),
    } for row in rows]})


async def create_bulk_orders(request):
    user = verify_telegram_user(request)
    try:
        data = await request.json()
        raw_ids = data.get("product_ids")
        if not isinstance(raw_ids, list) or not raw_ids or len(raw_ids) > 20:
            raise ValueError()
        product_ids = [int(value) for value in raw_ids]
    except (ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Передайте от 1 до 20 корректных product_ids")

    created = []
    async with db.acquire() as conn:
        async with conn.transaction():
            for product_id in product_ids:
                product = await conn.fetchrow("""
                    SELECT id, title, price_minor, currency
                    FROM products
                    WHERE id = $1 AND active = TRUE
                    FOR SHARE
                """, product_id)
                if not product:
                    raise web.HTTPNotFound(text=f"Товар {product_id} не найден или отключён")
                order_id = await conn.fetchval("""
                    INSERT INTO orders(user_id, username, product_id, product_title, amount_minor, currency)
                    VALUES($1, $2, $3, $4, $5, $6)
                    RETURNING id
                """, user["id"], user.get("username"), product["id"],
                     product["title"], product["price_minor"], product["currency"])
                created.append({
                    "order_id": order_id, "title": product["title"],
                    "amount_minor": product["price_minor"], "currency": product["currency"]
                })
                await conn.execute("""
                    INSERT INTO audit_log(actor_id, action, details)
                    VALUES($1, 'order_created', $2::jsonb)
                """, user["id"], json.dumps({
                    "order_id": order_id, "product_id": product_id, "source": "cart"
                }))

    summary = "\n".join(
        f"#{x['order_id']} — {x['title']} ({format_price(x['amount_minor'], x['currency'])})"
        for x in created
    )
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🛒 Новые заказы из корзины ({len(created)})\n"
                f"Покупатель: {user.get('first_name', '')} (@{user.get('username') or 'не указан'})\n"
                f"Telegram ID: {user['id']}\n\n{summary}"
            )
        except Exception:
            logger.exception("Could not notify admin %s", admin_id)

    return web.json_response({"ok": True, "orders": created, "count": len(created)})


async def ensure_customer_profile(conn, user_id: int):
    row = await conn.fetchrow(
        "SELECT user_id, referral_code, referred_by FROM customer_profiles WHERE user_id=$1",
        user_id,
    )
    if row:
        return row
    for _ in range(5):
        code = secrets.token_hex(4).upper()
        await conn.execute("""
            INSERT INTO customer_profiles(user_id, referral_code)
            VALUES($1, $2) ON CONFLICT DO NOTHING
        """, user_id, code)
        row = await conn.fetchrow(
            "SELECT user_id, referral_code, referred_by FROM customer_profiles WHERE user_id=$1",
            user_id,
        )
        if row:
            return row
    raise web.HTTPConflict(text="Не удалось создать профиль. Повторите попытку.")


async def get_my_profile(request):
    user = verify_telegram_user(request)
    async with db.acquire() as conn:
        async with conn.transaction():
            profile = await ensure_customer_profile(conn, user["id"])
        stats = await conn.fetchrow("""
            SELECT COUNT(*)::INTEGER AS orders_count,
                   COUNT(*) FILTER (WHERE status IN ('paid','delivered'))::INTEGER AS paid_orders_count
            FROM orders WHERE user_id=$1
        """, user["id"])
        points = await conn.fetchval(
            "SELECT COALESCE(SUM(points_delta),0)::INTEGER FROM loyalty_ledger WHERE user_id=$1",
            user["id"],
        )
        referrals = await conn.fetchval(
            "SELECT COUNT(*)::INTEGER FROM customer_profiles WHERE referred_by=$1",
            user["id"],
        )
        ledger = await conn.fetch("""
            SELECT points_delta, reason, created_at FROM loyalty_ledger
            WHERE user_id=$1 ORDER BY id DESC LIMIT 30
        """, user["id"])
        case_drops = await conn.fetch("""
            SELECT co.id, co.item_name, co.rarity, co.created_at,
                   c.title AS case_title, ci.image_url, ci.value_minor
            FROM case_openings co
            JOIN cases c ON c.id=co.case_id
            LEFT JOIN case_items ci ON ci.id=co.item_id
            WHERE co.user_id=$1
            ORDER BY co.id DESC LIMIT 50
        """, user["id"])
    return web.json_response({
        "user_id": user["id"], "referral_code": profile["referral_code"],
        "has_referrer": profile["referred_by"] is not None,
        "points": points, "orders_count": stats["orders_count"],
        "paid_orders_count": stats["paid_orders_count"], "referrals_count": referrals,
        "ledger": [{"points_delta": r["points_delta"], "reason": r["reason"],
                    "created_at": r["created_at"].isoformat()} for r in ledger],
        "case_drops": [{
            "id": r["id"], "item_name": r["item_name"], "rarity": r["rarity"],
            "case_title": r["case_title"], "image_url": r["image_url"],
            "value_minor": r["value_minor"],
            "created_at": r["created_at"].isoformat()
        } for r in case_drops],
        "points_rule": "1 балл за каждые 100 ₽ подтверждённого заказа."
    })


async def apply_referral_code(request):
    user = verify_telegram_user(request)
    try:
        data = await request.json()
        code = str(data.get("code", "")).strip().upper()
    except (ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Некорректные данные")
    if not code or len(code) > 32:
        raise web.HTTPBadRequest(text="Введите корректный реферальный код")
    async with db.acquire() as conn:
        async with conn.transaction():
            profile = await ensure_customer_profile(conn, user["id"])
            if profile["referred_by"] is not None:
                raise web.HTTPConflict(text="Реферальный код уже применён")
            orders = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE user_id=$1", user["id"])
            if orders:
                raise web.HTTPConflict(text="Код можно применить только до первого заказа")
            referrer = await conn.fetchrow(
                "SELECT user_id FROM customer_profiles WHERE referral_code=$1", code
            )
            if not referrer:
                raise web.HTTPNotFound(text="Реферальный код не найден")
            if referrer["user_id"] == user["id"]:
                raise web.HTTPBadRequest(text="Нельзя применить собственный код")
            await conn.execute("""
                UPDATE customer_profiles SET referred_by=$1, updated_at=NOW()
                WHERE user_id=$2 AND referred_by IS NULL
            """, referrer["user_id"], user["id"])
            await conn.execute("""
                INSERT INTO audit_log(actor_id, action, details)
                VALUES($1, 'referral_code_applied', $2::jsonb)
            """, user["id"], json.dumps({"referrer_id": referrer["user_id"]}))
    return web.json_response({"ok": True, "message": "Код применён. Бонусы начислятся после подтверждённой оплаты."})


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
        async with conn.transaction():
            current = await conn.fetchrow("""
                SELECT user_id, amount_minor, currency, status
                FROM orders WHERE id=$1 FOR UPDATE
            """, order_id)
            if not current:
                raise web.HTTPNotFound(text="Заказ не найден")
            row = await conn.fetchrow("""
                UPDATE orders SET status=$1, updated_at=NOW()
                WHERE id=$2 RETURNING user_id
            """, status, order_id)

            # Баллы начисляются только при первом ручном переводе заказа в paid.
            if status == "paid" and current["status"] != "paid":
                await ensure_customer_profile(conn, current["user_id"])
                if current["currency"] == "RUB":
                    points = int(current["amount_minor"]) // 10000
                    if points > 0:
                        await conn.execute("""
                            INSERT INTO loyalty_ledger(user_id, order_id, points_delta, reason)
                            VALUES($1, $2, $3, 'purchase_reward')
                            ON CONFLICT DO NOTHING
                        """, current["user_id"], order_id, points)
                referred_by = await conn.fetchval(
                    "SELECT referred_by FROM customer_profiles WHERE user_id=$1",
                    current["user_id"],
                )
                if referred_by:
                    paid_count = await conn.fetchval("""
                        SELECT COUNT(*) FROM orders
                        WHERE user_id=$1 AND status IN ('paid','delivered')
                    """, current["user_id"])
                    if paid_count == 1:
                        await conn.execute("""
                            INSERT INTO loyalty_ledger(user_id, order_id, points_delta, reason)
                            VALUES($1, $2, 50, 'referral_reward')
                            ON CONFLICT DO NOTHING
                        """, current["user_id"], order_id)
                        await conn.execute("""
                            INSERT INTO loyalty_ledger(user_id, order_id, points_delta, reason)
                            VALUES($1, $2, 100, 'referral_reward')
                            ON CONFLICT DO NOTHING
                        """, referred_by, order_id)

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
# DEMO PAYMENT PROVIDER MANAGEMENT
# ============================================================

async def admin_get_payment_providers(request):
    require_admin(request)
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT code, title, mode, enabled, public_config, updated_at
            FROM payment_providers ORDER BY code
        """)
        transactions = await conn.fetchval(
            "SELECT COUNT(*) FROM payment_transactions"
        )
    return web.json_response({
        "items": [{
            "code": row["code"], "title": row["title"],
            "mode": row["mode"], "enabled": row["enabled"],
            "public_config": (json.loads(row["public_config"]) if isinstance(row["public_config"], str) else (row["public_config"] or {})),
            "updated_at": row["updated_at"].isoformat()
        } for row in rows],
        "transaction_count": transactions,
        "real_payments_enabled": False
    })


async def admin_configure_payment_provider(request):
    user = require_admin(request)
    code = request.match_info["provider_code"]
    if code not in {"demo", "antilopay"}:
        raise web.HTTPNotFound(text="Провайдер не найден")
    try:
        data = await request.json()
        enabled = bool(data.get("enabled", False))
        merchant_id = str(data.get("merchant_id", "")).strip()[:100]
        api_url = str(data.get("api_url", "")).strip()[:300]
    except (TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Некорректные данные")
    # No secret keys are accepted or persisted in this demo configuration.
    if code == "antilopay":
        if api_url and not api_url.startswith("https://"):
            raise web.HTTPBadRequest(text="API URL должен начинаться с https://")
        async with db.acquire() as conn:
            await conn.execute("""
                UPDATE payment_providers
                SET enabled = FALSE, mode = 'unconfigured',
                    public_config = jsonb_build_object(
                        'description', 'Демо-профиль. Реальное подключение ещё не выполнено.',
                        'merchant_id', $1,
                        'api_url', $2,
                        'credentials_saved', FALSE
                    ),
                    updated_at = NOW()
                WHERE code = 'antilopay'
            """, merchant_id, api_url)
        await write_audit(user["id"], "payment_provider_demo_profile_saved", {
            "provider": code, "merchant_id_present": bool(merchant_id),
            "api_url_present": bool(api_url), "secrets_saved": False
        })
        return web.json_response({
            "ok": True, "provider": code, "enabled": False,
            "mode": "unconfigured",
            "message": "Демо-профиль сохранён. API-ключ не запрашивался; реальная оплата не подключена."
        })
    async with db.acquire() as conn:
        await conn.execute("""
            UPDATE payment_providers
            SET enabled = $1, mode = CASE WHEN $1 THEN 'demo' ELSE 'unconfigured' END,
                updated_at = NOW()
            WHERE code = 'demo'
        """, enabled)
    await write_audit(user["id"], "payment_provider_configured", {
        "provider": code, "enabled": enabled, "mode": "demo"
    })
    return web.json_response({"ok": True, "provider": code,
                              "enabled": enabled, "mode": "demo"})


async def create_demo_payment(request):
    user = verify_telegram_user(request)
    try:
        data = await request.json()
        order_id = int(data.get("order_id"))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Некорректный ID заказа")
    async with db.acquire() as conn:
        async with conn.transaction():
            provider = await conn.fetchrow("""
                SELECT code FROM payment_providers
                WHERE code = 'demo' AND enabled = TRUE AND mode = 'demo'
            """)
            if not provider:
                raise web.HTTPServiceUnavailable(text="Демо-платежи временно отключены")
            order = await conn.fetchrow("""
                SELECT id, user_id, amount_minor, currency, status
                FROM orders WHERE id = $1 FOR UPDATE
            """, order_id)
            if not order or order["user_id"] != user["id"]:
                raise web.HTTPNotFound(text="Заказ не найден")
            if order["status"] in {"paid", "delivered", "refunded", "cancelled"}:
                raise web.HTTPConflict(text="Для этого заказа нельзя создать платёж")
            reference = "QRDEMO-" + secrets.token_hex(8).upper()
            tx_id = await conn.fetchval("""
                INSERT INTO payment_transactions(
                    order_id, user_id, provider_code, amount_minor, currency,
                    status, external_reference
                ) VALUES($1, $2, 'demo', $3, $4, 'demo_pending', $5)
                RETURNING id
            """, order_id, user["id"], order["amount_minor"], order["currency"], reference)
            await conn.execute("""
                INSERT INTO audit_log(actor_id, action, details)
                VALUES($1, 'demo_payment_created', $2::jsonb)
            """, user["id"], json.dumps({"transaction_id": tx_id, "order_id": order_id}))
    return web.json_response({
        "ok": True, "transaction_id": tx_id, "order_id": order_id,
        "status": "demo_pending", "reference": reference,
        "amount_minor": order["amount_minor"], "currency": order["currency"],
        "demo_only": True,
        "message": "Тестовая операция. Деньги не списываются, заказ не считается оплаченным."
    })


async def get_my_demo_payments(request):
    user = verify_telegram_user(request)
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, order_id, amount_minor, currency, status,
                   external_reference, created_at
            FROM payment_transactions
            WHERE user_id = $1 ORDER BY id DESC LIMIT 50
        """, user["id"])
    return web.json_response({"items": [{
        "id": r["id"], "order_id": r["order_id"],
        "amount_minor": r["amount_minor"], "currency": r["currency"],
        "status": r["status"], "reference": r["external_reference"],
        "created_at": r["created_at"].isoformat()
    } for r in rows], "demo_only": True})


async def admin_simulate_demo_payment(request):
    user = require_admin(request)
    try:
        tx_id = int(request.match_info["transaction_id"])
        data = await request.json()
        outcome = str(data.get("outcome", "success"))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Некорректные данные")
    if outcome not in {"success", "failed", "cancelled"}:
        raise web.HTTPBadRequest(text="Недопустимый результат")
    status = "demo_" + outcome
    async with db.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE payment_transactions
            SET status = $1, updated_at = NOW()
            WHERE id = $2 AND provider_code = 'demo' AND status = 'demo_pending'
            RETURNING order_id
        """, status, tx_id)
    if not row:
        raise web.HTTPNotFound(text="Ожидающая демо-транзакция не найдена")
    await write_audit(user["id"], "demo_payment_simulated", {
        "transaction_id": tx_id, "outcome": outcome,
        "note": "Order status intentionally unchanged"
    })
    return web.json_response({
        "ok": True, "status": status, "order_status_changed": False,
        "message": "Демо-результат сохранён. Статус заказа не изменён."
    })


async def admin_get_payment_transactions(request):
    require_admin(request)
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, order_id, user_id, provider_code, amount_minor,
                   currency, status, external_reference, created_at
            FROM payment_transactions ORDER BY id DESC LIMIT 100
        """)
    return web.json_response({"items": [{
        "id": r["id"], "order_id": r["order_id"], "user_id": r["user_id"],
        "provider": r["provider_code"], "amount_minor": r["amount_minor"],
        "currency": r["currency"], "status": r["status"],
        "reference": r["external_reference"],
        "created_at": r["created_at"].isoformat()
    } for r in rows]})


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
        "оформить заказ, отслеживать его статус и открыть демо-кейсы CS2.\n\n"
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

    global CS2_PRICE_WORKER_TASK
    if CS2_PRICE_WORKER_TASK is None or CS2_PRICE_WORKER_TASK.done():
        CS2_PRICE_WORKER_TASK = asyncio.create_task(cs2_steam_price_worker())
    logger.info("QR BOT PRO started")


async def on_cleanup(app: web.Application):
    global CS2_PRICE_WORKER_TASK
    if CS2_PRICE_WORKER_TASK is not None:
        CS2_PRICE_WORKER_TASK.cancel()
        try:
            await CS2_PRICE_WORKER_TASK
        except asyncio.CancelledError:
            pass
        CS2_PRICE_WORKER_TASK = None
    if db is not None:
        await db.close()

    await bot.session.close()


def create_app():
    app = web.Application(middlewares=[error_middleware])

    # Public endpoints
    app.router.add_get("/health", health)
    app.router.add_get("/api/products", get_products)
    app.router.add_post("/api/orders", create_order)
    app.router.add_post("/api/orders/bulk", create_bulk_orders)
    app.router.add_get("/api/orders", get_my_orders)
    app.router.add_get("/api/profile", get_my_profile)
    app.router.add_post("/api/referrals/apply", apply_referral_code)
    app.router.add_get("/api/cases", get_cases)
    app.router.add_post("/api/cases/{case_id}/open", open_case)
    app.router.add_get("/api/inventory", get_inventory)
    app.router.add_get("/api/wallet", get_wallet)
    app.router.add_post("/api/inventory/{inventory_id}/sell", sell_inventory_item)

    # Demo payment endpoints
    app.router.add_post("/api/payments/demo/create", create_demo_payment)
    app.router.add_get("/api/payments", get_my_demo_payments)

    # Payment administration endpoints
    app.router.add_get("/api/admin/payment-providers", admin_get_payment_providers)
    app.router.add_post(
        "/api/admin/payment-providers/{provider_code}",
        admin_configure_payment_provider,
    )
    app.router.add_get("/api/admin/payment-transactions", admin_get_payment_transactions)
    app.router.add_post(
        "/api/admin/payment-transactions/{transaction_id}/simulate",
        admin_simulate_demo_payment,
    )
    app.router.add_get("/api/cs2/catalog", get_cs2_catalog)
    app.router.add_get("/api/cs2/skins", get_cs2_skins)
    app.router.add_post("/api/cases/{case_id}/claim", claim_demo_case)
    app.router.add_post("/api/upgrades/suggest", suggest_upgrade_target)
    app.router.add_post("/api/upgrades/attempt", create_upgrade_attempt)
    app.router.add_get("/api/upgrades/history", get_upgrade_history)

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
    async def index_handler(request):
        return web.FileResponse("static/index.html")

    app.router.add_get("/", index_handler)
    app.router.add_static("/", path="static", show_index=False)

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
