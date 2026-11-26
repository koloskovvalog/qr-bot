import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import secrets
from decimal import Decimal, InvalidOperation
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
        skin_images = {}
        catalog_cases = []
        fallback_case_image = None
    async with db.acquire() as conn:
        async with conn.transaction():
            for catalog_case in [
                item for item in catalog_cases
                if "case" in item["name"].lower()
            ][:5]:
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
                await conn.execute("""
                    INSERT INTO case_items(case_id, name, rarity, weight, image_url, value_minor)
                    SELECT $1, ci.name, ci.rarity, ci.weight, ci.image_url, ci.value_minor
                    FROM case_items ci
                    WHERE ci.case_id = (
                        SELECT id FROM cases WHERE title='Премиум кейс' ORDER BY id LIMIT 1
                    )
                    AND NOT EXISTS (
                        SELECT 1 FROM case_items existing WHERE existing.case_id=$1
                    )
                """, catalog_case_id)
        cases = await conn.fetch("""
            SELECT id, title, description, price_minor, currency, image_url
            FROM cases
            WHERE active = TRUE
            ORDER BY id
        """)
        items = await conn.fetch("""
            SELECT ci.case_id, ci.id, ci.name, ci.rarity, ci.weight,
                   ci.image_url, ci.value_minor
            FROM case_items ci
            JOIN cases c ON c.id = ci.case_id
            WHERE c.active = TRUE
            ORDER BY ci.case_id, ci.weight DESC
        """)

    grouped = {}
    for item in items:
        grouped.setdefault(item["case_id"], []).append({
            "id": item["id"], "name": item["name"],
            "rarity": item["rarity"], "weight": item["weight"],
            "image_url": item["image_url"] or skin_images.get(item["name"]), "value_minor": item["value_minor"],
        })
    result = []
    for case in cases:
        contents = grouped.get(case["id"], [])
        total = sum(x["weight"] for x in contents)
        result.append({
            "id": case["id"], "title": case["title"],
            "description": case["description"],
            "price_minor": case["price_minor"],
            "currency": case["currency"],
            "image_url": case["image_url"] or fallback_case_image,
            "demo_only": True,
            "items": [{
                **item,
                "chance_percent": round(item["weight"] * 100 / total, 2) if total else 0
            } for item in contents],
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
                SELECT id, name, rarity, weight, image_url, value_minor
                FROM case_items WHERE case_id = $1 ORDER BY id
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
            if not selected["image_url"]:
                cached_skins = CS2_CATALOG_CACHE.get("skins", [])
                selected["image_url"] = next(
                    (item["image_url"] for item in cached_skins
                     if item["name"] == selected["name"]),
                    None,
                )
            await conn.execute("""
                INSERT INTO user_inventory(
                    user_id, item_type, item_name, rarity, image_url,
                    value_minor, source
                )
                VALUES($1, 'skin', $2, $3, $4, $5, 'case')
            """, user["id"], selected["name"], selected["rarity"],
                 selected["image_url"], selected["value_minor"])

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
        "ok": True, "opening_id": opening_id, "case_title": case["title"],
        "item": {
            "id": selected["id"], "name": selected["name"],
            "rarity": selected["rarity"],
            "chance_percent": round(selected["weight"] * 100 / total, 2),
        },
        "demo_only": True,
        "message": "Виртуальный предмет симуляции; не является предметом Steam."
    })


async def get_inventory(request):
    user = verify_telegram_user(request)
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, item_type, item_name, rarity, image_url,
                   value_minor, source, created_at
            FROM user_inventory
            WHERE user_id = $1 AND consumed_at IS NULL
            ORDER BY id DESC LIMIT 100
        """, user["id"])
    return web.json_response({"items": [{
        "id": row["id"], "item_type": row["item_type"],
        "item_name": row["item_name"], "rarity": row["rarity"],
        "image_url": row["image_url"], "value_minor": row["value_minor"],
        "source": row["source"], "created_at": row["created_at"].isoformat()
    } for row in rows]})

# ============================================================
# CS2 PUBLIC CATALOG AND VIRTUAL UPGRADER
# ============================================================

CS2_CATALOG_URLS = {
    "skins": "https://raw.githubusercontent.com/ByMykel/CSGO-API/main/public/api/en/skins.json",
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
            item = {
                "id": str(entry.get("id") or name),
                "name": name,
                "rarity": rarity_name,
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

async def get_cs2_catalog(request):
    kind = request.query.get("kind", "skins")
    items = await load_cs2_catalog(kind)
    try:
        limit = min(max(int(request.query.get("limit", "120")), 1), 200)
    except ValueError:
        limit = 120
    return web.json_response({
        "items": items[:limit],
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
                SELECT id, item_type, item_name, rarity, value_minor
                FROM user_inventory
                WHERE id=$1 AND user_id=$2 AND item_type=$3 AND consumed_at IS NULL
                FOR UPDATE
            """, source_id, user["id"], item_type)
            if not source:
                raise web.HTTPNotFound(text="Предмет уже использован или не найден")
            source_value = int(source["value_minor"])
            target_value = int(target["value_minor"])
            if target_value <= source_value:
                raise web.HTTPBadRequest(text="Выберите предмет дороже исходного")
            chance = min(90.0, max(1.0, round(source_value / target_value * 75, 2)))
            roll = secrets.randbelow(10000)
            success = roll < int(chance * 100)
            await conn.execute("""
                UPDATE user_inventory SET consumed_at=NOW() WHERE id=$1
            """, source_id)
            if success:
                await conn.execute("""
                    INSERT INTO user_inventory(
                        user_id, item_type, item_name, rarity, image_url,
                        value_minor, source
                    )
                    VALUES($1, $2, $3, $4, $5, $6, 'upgrade')
                """, user["id"], item_type, target["name"], target["rarity"],
                     target["image_url"], target_value)
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
        "chance_percent": chance, "source_name": source["item_name"],
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
    app.router.add_post("/api/orders/bulk", create_bulk_orders)
    app.router.add_get("/api/orders", get_my_orders)
    app.router.add_get("/api/cases", get_cases)
    app.router.add_post("/api/cases/{case_id}/open", open_case)
    app.router.add_get("/api/inventory", get_inventory)
    app.router.add_get("/api/cs2/catalog", get_cs2_catalog)
    app.router.add_post("/api/cases/{case_id}/claim", claim_demo_case)
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
