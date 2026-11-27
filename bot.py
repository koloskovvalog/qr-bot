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
from datetime import datetime, timezone, timedelta
from urllib.parse import parse_qsl, urlencode

import asyncpg
import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, MenuButtonWebApp
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
CASES_BOT_TOKEN = os.getenv("CASES_BOT_TOKEN", "").strip()
DATABASE_URL = os.environ["DATABASE_URL"]
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
CASES_APP_URL = os.getenv("CASES_APP_URL", f"{PUBLIC_BASE_URL}/cases").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
CASES_WEBHOOK_SECRET = os.getenv("CASES_WEBHOOK_SECRET", "").strip()
APISKINS_API_KEY = os.getenv("APISKINS_API_KEY", "").strip()
CS2SELL_API_KEY = os.getenv("CS2SELL_API_KEY", "").strip()
TELEBOTLY_PAY_KEY = os.getenv("TELEBOTLY_PAY_KEY", "").strip()
TELEBOTLY_WEBHOOK_SECRET = os.getenv("TELEBOTLY_WEBHOOK_SECRET", "").strip()
TELEBOTLY_API_BASE = "https://api.telebotly.app/v1"
CS2SELL_WEBHOOK_SECRET = os.getenv("CS2SELL_WEBHOOK_SECRET", "").strip()
SKINSNODE_CLIENT_ID = os.getenv("SKINSNODE_CLIENT_ID", "").strip()
SKINSNODE_CLIENT_SECRET = os.getenv("SKINSNODE_CLIENT_SECRET", "").strip()
CS2SELL_PARTNER_DEPOSITS_CONFIRMED = os.getenv("CS2SELL_PARTNER_DEPOSITS_CONFIRMED", "").lower() == "true"
SKINSNODE_DEPOSITS_CONFIRMED = os.getenv("SKINSNODE_DEPOSITS_CONFIRMED", "").lower() == "true"
SKINSNODE_WITHDRAWALS_CONFIRMED = os.getenv("SKINSNODE_WITHDRAWALS_CONFIRMED", "").lower() == "true"
APISKINS_COOLDOWN_UNTIL = 0
USD_RUB_RATE = None
USD_RUB_RATE_AT = 0
CS2_PRICE_USABLE_TTL_SECONDS = 7 * 24 * 60 * 60  # last known quote remains usable for 7 days
CS2_PRICE_REFRESH_TTL_SECONDS = 12 * 60 * 60  # refresh in background after 12 hours
PORT = int(os.getenv("PORT", "10000"))
WEBHOOK_PATH = "/telegram-webhook"
CASES_WEBHOOK_PATH = "/telegram-webhook-cases"

ADMIN_IDS = {
    int(value.strip())
    for value in os.getenv("ADMIN_IDS", "").split(",")
    if value.strip()
}

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
cases_bot = Bot(token=CASES_BOT_TOKEN) if CASES_BOT_TOKEN else None
cases_dp = Dispatcher()

db: asyncpg.Pool | None = None
CS2_CATALOG_CACHE = {}
CS2_CATALOG_CACHE_AT = {}
CASES_RESPONSE_CACHE = None
CASES_RESPONSE_CACHE_AT = 0
CASES_RESPONSE_CACHE_TTL = 30
CASES_RESPONSE_LOCK = asyncio.Lock()
CS2_PRICE_QUEUE = asyncio.Queue()
CS2_PRICE_QUEUED = set()
CS2_PRICE_WORKER_TASK = None
CS2_PRICE_WORKER_TASK_2 = None
CS2_PRICE_WARMUP_TASK = None


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



                CREATE TABLE IF NOT EXISTS steam_identities (
                    user_id BIGINT PRIMARY KEY,
                    steam_id64 TEXT NOT NULL UNIQUE,
                    trade_url TEXT,
                    verified_at TIMESTAMPTZ,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );

                CREATE TABLE IF NOT EXISTS steam_auth_states (
                    state TEXT PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    consumed_at TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS idx_steam_auth_states_created
                    ON steam_auth_states(created_at);

                CREATE TABLE IF NOT EXISTS cs2_skin_deposits (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    provider TEXT NOT NULL,
                    external_id TEXT NOT NULL UNIQUE,
                    provider_deposit_id TEXT,
                    steam_id64 TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'created',
                    amount_usd_cents BIGINT,
                    amount_rub_minor BIGINT,
                    provider_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_cs2_skin_deposits_user
                    ON cs2_skin_deposits(user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS cs2_skin_withdrawals (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    provider TEXT NOT NULL,
                    external_id TEXT NOT NULL UNIQUE,
                    provider_order_id TEXT,
                    steam_id64 TEXT NOT NULL,
                    market_hash_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'created',
                    amount_usd_cents BIGINT,
                    amount_rub_minor BIGINT,
                    provider_payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_cs2_skin_withdrawals_user
                    ON cs2_skin_withdrawals(user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS cs2_provider_webhook_events (
                    provider TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload JSONB NOT NULL,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    processed_at TIMESTAMPTZ,
                    PRIMARY KEY(provider, event_id)
                );

                CREATE TABLE IF NOT EXISTS wallet_external_ledger (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    operation_id TEXT NOT NULL UNIQUE,
                    delta_minor BIGINT NOT NULL CHECK (delta_minor <> 0),
                    currency TEXT NOT NULL DEFAULT 'RUB',
                    reason TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_wallet_external_ledger_user
                    ON wallet_external_ledger(user_id, created_at DESC);

                -- Separate cash balance: never mix real deposits with legacy demo credits.
                CREATE TABLE IF NOT EXISTS cash_wallet_accounts (
                    user_id BIGINT PRIMARY KEY,
                    balance_minor BIGINT NOT NULL DEFAULT 0 CHECK (balance_minor >= 0),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS cash_wallet_ledger (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    operation_id TEXT NOT NULL UNIQUE,
                    amount_minor BIGINT NOT NULL CHECK (amount_minor > 0),
                    currency TEXT NOT NULL DEFAULT 'RUB',
                    reason TEXT NOT NULL DEFAULT 'telebotly_topup',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_cash_wallet_ledger_user
                    ON cash_wallet_ledger(user_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS wallet_topups (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    amount_minor BIGINT NOT NULL CHECK (amount_minor >= 1000),
                    currency TEXT NOT NULL DEFAULT 'RUB',
                    status TEXT NOT NULL DEFAULT 'creating',
                    provider_payment_id TEXT UNIQUE,
                    payment_url TEXT,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    livemode BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    credited_at TIMESTAMPTZ
                );
                CREATE INDEX IF NOT EXISTS idx_wallet_topups_user
                    ON wallet_topups(user_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS telebotly_webhook_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
                    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    processed_at TIMESTAMPTZ
                );

                CREATE TABLE IF NOT EXISTS admin_wallet_adjustments (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    admin_id BIGINT NOT NULL,
                    delta_minor BIGINT NOT NULL,
                    balance_after_minor BIGINT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_admin_wallet_adjustments_user
                    ON admin_wallet_adjustments(user_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS cs2_steam_prices (
                    market_hash_name TEXT PRIMARY KEY,
                    lowest_price TEXT,
                    median_price TEXT,
                    volume TEXT,
                    currency TEXT NOT NULL DEFAULT 'RUB',
                    fetched_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                ALTER TABLE cs2_steam_prices
                    ADD COLUMN IF NOT EXISTS price_source TEXT NOT NULL DEFAULT 'steam',
                    ADD COLUMN IF NOT EXISTS image_url TEXT;
                CREATE INDEX IF NOT EXISTS idx_cs2_steam_prices_fetched
                    ON cs2_steam_prices(fetched_at);

                -- Persistent canonical CS2 catalog; additive and independent from user data.
                CREATE TABLE IF NOT EXISTS cs2_skin_catalog (
                    market_hash_name TEXT PRIMARY KEY,
                    base_name TEXT NOT NULL DEFAULT '',
                    weapon TEXT NOT NULL DEFAULT '',
                    wear TEXT NOT NULL DEFAULT '',
                    rarity TEXT NOT NULL DEFAULT '',
                    phase TEXT NOT NULL DEFAULT '',
                    stattrak BOOLEAN NOT NULL DEFAULT FALSE,
                    souvenir BOOLEAN NOT NULL DEFAULT FALSE,
                    image_url TEXT,
                    value_minor BIGINT NOT NULL DEFAULT 0,
                    catalog_updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_cs2_skin_catalog_weapon
                    ON cs2_skin_catalog(weapon);
                CREATE INDEX IF NOT EXISTS idx_cs2_skin_catalog_rarity
                    ON cs2_skin_catalog(rarity);
                CREATE INDEX IF NOT EXISTS idx_cs2_skin_catalog_wear
                    ON cs2_skin_catalog(wear);

                -- Records successful and empty quote lookups, preventing endless retries
                -- for skins that currently have no marketplace listing.
                CREATE TABLE IF NOT EXISTS cs2_price_checks (
                    market_hash_name TEXT PRIMARY KEY,
                    last_checked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    has_quote BOOLEAN NOT NULL DEFAULT FALSE,
                    price_source TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_cs2_price_checks_checked
                    ON cs2_price_checks(last_checked_at);

                UPDATE payment_providers
                SET enabled=FALSE, mode='disabled'
                WHERE code='demo';

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
                ALTER TABLE cases ADD COLUMN IF NOT EXISTS reference_price_minor BIGINT NOT NULL DEFAULT 0;

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

                CREATE TABLE IF NOT EXISTS creator_cases (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    image_url TEXT,
                    active BOOLEAN NOT NULL DEFAULT TRUE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_creator_cases_active
                    ON creator_cases(active, created_at DESC);
                CREATE TABLE IF NOT EXISTS creator_case_items (
                    id BIGSERIAL PRIMARY KEY,
                    creator_case_id BIGINT NOT NULL REFERENCES creator_cases(id) ON DELETE CASCADE,
                    market_hash_name TEXT NOT NULL,
                    item_name TEXT NOT NULL,
                    rarity TEXT NOT NULL DEFAULT 'Обычный',
                    chance_bp INTEGER NOT NULL CHECK (chance_bp BETWEEN 1 AND 10000),
                    image_url TEXT,
                    value_minor BIGINT NOT NULL DEFAULT 0,
                    wear TEXT,
                    stattrak BOOLEAN NOT NULL DEFAULT FALSE,
                    souvenir BOOLEAN NOT NULL DEFAULT FALSE,
                    phase TEXT,
                    UNIQUE(creator_case_id, market_hash_name)
                );
                CREATE TABLE IF NOT EXISTS creator_case_openings (
                    id BIGSERIAL PRIMARY KEY,
                    creator_case_id BIGINT NOT NULL REFERENCES creator_cases(id),
                    user_id BIGINT NOT NULL,
                    item_id BIGINT NOT NULL REFERENCES creator_case_items(id),
                    item_name TEXT NOT NULL,
                    rarity TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_creator_case_openings_created
                    ON creator_case_openings(created_at DESC);
                CREATE TABLE IF NOT EXISTS creator_case_favorites (
                    user_id BIGINT NOT NULL,
                    creator_case_id BIGINT NOT NULL REFERENCES creator_cases(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY(user_id, creator_case_id)
                );
                CREATE TABLE IF NOT EXISTS user_inventory_favorites (
                    user_id BIGINT NOT NULL,
                    inventory_id BIGINT NOT NULL REFERENCES user_inventory(id) ON DELETE CASCADE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY(user_id, inventory_id)
                );
                CREATE TABLE IF NOT EXISTS user_reward_points (
                    user_id BIGINT PRIMARY KEY,
                    points BIGINT NOT NULL DEFAULT 0,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS weekly_reward_claims (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    points INTEGER NOT NULL DEFAULT 100,
                    claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                CREATE INDEX IF NOT EXISTS idx_weekly_reward_claims_user
                    ON weekly_reward_claims(user_id, claimed_at DESC);

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

    store_mode = request.headers.get("X-Store-Mode", "digital").strip().lower()
    if store_mode not in {"digital", "cases"}:
        raise web.HTTPBadRequest(text="Неизвестный магазин")
    auth_token = CASES_BOT_TOKEN if store_mode == "cases" else BOT_TOKEN
    if not auth_token:
        raise web.HTTPServiceUnavailable(text="Telegram-бот кейсов ещё не подключён")

    secret_key = hmac.new(
        key=b"WebAppData",
        msg=auth_token.encode(),
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



async def admin_get_cs2_provider_status(request):
    """Read-only connectivity check for candidate CS2 skin providers."""
    require_admin(request)
    timeout = aiohttp.ClientTimeout(total=12)
    result = {
        "providers": {
            "cs2sell": {
                "name": "CS2Sell",
                "credentials_configured": bool(CS2SELL_API_KEY),
                "partner_deposits_confirmed": CS2SELL_PARTNER_DEPOSITS_CONFIRMED,
                "connected": False,
                "scopes": [],
                "deposit_ready": False,
                "withdrawal_ready": False,
                "message": "Нужен API key с нужными scopes и подтверждённый доступ к partner deposits.",
            },
            "skinsnode": {
                "name": "SkinsNode",
                "credentials_configured": bool(SKINSNODE_CLIENT_ID and SKINSNODE_CLIENT_SECRET),
                "deposits_confirmed": SKINSNODE_DEPOSITS_CONFIRMED,
                "withdrawals_confirmed": SKINSNODE_WITHDRAWALS_CONFIRMED,
                "connected": False,
                "deposit_ready": False,
                "withdrawal_ready": False,
                "message": "Нужны client credentials и включённые поставщиком функции deposits/withdrawals.",
            },
        }
    }

    async with aiohttp.ClientSession(timeout=timeout) as session:
        cs2sell = result["providers"]["cs2sell"]
        if CS2SELL_API_KEY:
            try:
                async with session.get(
                    "https://api.cs2sell.com/v1/public/me",
                    headers={"Authorization": f"Bearer {CS2SELL_API_KEY}"},
                ) as response:
                    body = await response.json(content_type=None)
                    if response.status == 200:
                        scopes = body.get("scopes", []) if isinstance(body, dict) else []
                        cs2sell["connected"] = True
                        cs2sell["scopes"] = scopes
                        cs2sell["deposit_ready"] = (
                            CS2SELL_PARTNER_DEPOSITS_CONFIRMED
                            and "deposits:read" in scopes
                            and "deposits:write" in scopes
                        )
                        cs2sell["withdrawal_ready"] = (
                            "orders:read" in scopes and "orders:write" in scopes
                        )
                        cs2sell["message"] = "API key проверен. Доступность partner deposits требует отдельного подтверждения CS2Sell."
                    else:
                        cs2sell["message"] = f"Проверка API не пройдена (HTTP {response.status})."
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                logger.warning("CS2Sell provider connectivity check failed", exc_info=True)
                cs2sell["message"] = "Не удалось связаться с CS2Sell API."

        skinsnode = result["providers"]["skinsnode"]
        if SKINSNODE_CLIENT_ID and SKINSNODE_CLIENT_SECRET:
            try:
                async with session.get(
                    "https://api.skinsnode.com/v1/wallet",
                    headers={
                        "x-client-id": SKINSNODE_CLIENT_ID,
                        "x-client-secret": SKINSNODE_CLIENT_SECRET,
                    },
                ) as response:
                    body = await response.json(content_type=None)
                    if response.status == 200:
                        skinsnode["connected"] = True
                        skinsnode["deposit_ready"] = SKINSNODE_DEPOSITS_CONFIRMED
                        skinsnode["withdrawal_ready"] = SKINSNODE_WITHDRAWALS_CONFIRMED
                        # Do not expose provider balances or account identifiers in this status endpoint.
                        skinsnode["message"] = "Credentials проверены. Коммерческий доступ к операциям должен быть подтверждён при onboarding."
                    else:
                        skinsnode["message"] = f"Проверка API не пройдена (HTTP {response.status})."
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                logger.warning("SkinsNode provider connectivity check failed", exc_info=True)
                skinsnode["message"] = "Не удалось связаться с SkinsNode API."

    return web.json_response(result)




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


async def build_cases_response(request):
    try:
        catalog_skins = await load_cs2_catalog("skins")
        skin_images = {}
        for catalog_skin in catalog_skins:
            for image_key in {
                catalog_skin.get("name"), catalog_skin.get("base_name"),
                catalog_skin.get("market_hash_name")
            }:
                if image_key:
                    skin_images[str(image_key).strip().casefold()] = catalog_skin["image_url"]
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
        # Replace generic imported crates on the public shelf with original
        # ShirpDrop themed demo cases. Old case rows/items and opening history stay intact.
        standard_case_titles = [
            item["name"] for item in catalog_cases
            if "case" in str(item.get("name", "")).casefold()
        ]
        # Previous generic weapon-by-weapon shelf is retired. Preserve its rows and history.
        legacy_shirpdrop_case_titles = [
            "АК-47: КАЛИБР", "AWP: ОДИН ВЫСТРЕЛ", "M4: ТАКТИЧЕСКИЙ ОТРЯД",
            "ПИСТОЛЕТНЫЙ КАЛИБР", "НОЖЕВОЙ: КОЛЛЕКЦИЯ КЛИНКОВ",
            "ПЕРЧАТОЧНЫЙ: BLACK GOLD", "НЕОНОВАЯ НОЧЬ", "ТАЙНЫЙ УРОВЕНЬ",
            "ЗОЛОТОЙ ЧАС",
        ]
        themed_case_specs = [
            (
                "GTA VI: VICE CITY",
                "Вайс-Сити уже ждёт: неон, скорость, яркие цвета и редкие ножи. Авторская подборка ShirpDrop по мотивам GTA VI.",
                lambda x: any(k in str(x.get("name", "")).casefold() for k in (
                    "ultra violet", "vice", "doppler", "player two", "neon rider",
                    "neon revolution", "berries and cherries", "marble fade", "fade",
                    "decimator", "sugar rush", "crime scene", "momentum", "ocean drive",
                    "the oligarch", "wasteland princess", "full throttle", "starcade",
                    "frontside misty", "crimson web", "phosphor", "target acquired",
                    "amber fade", "nightwish", "neo-noir"
                )),
            ),
            (
                "CYBERPUNK 2077",
                "Ночной город, неоновые улицы и футуристичные раскраски. Подборка ярких технологичных скинов.",
                lambda x: any(k in str(x.get("name", "")).casefold() for k in (
                    "neon", "neo-noir", "printstream", "cyber", "mecha industries",
                    "phosphor", "decimator", "nightwish", "ocean drive", "hyper beast",
                    "pulse", "momentum", "asiimov", "blue phosphor"
                )),
            ),
            (
                "МИНЬОНЫ: ЖЁЛТЫЙ ХАОС",
                "Жёлтая коллекция с солнечными, золотыми и контрастными раскрасками.",
                lambda x: any(k in str(x.get("name", "")).casefold() for k in (
                    "banana", "yellow", "bulldozer", "tiger tooth", "saffron",
                    "sand dune", "nuclear", "hazard", "sundown", "gold"
                )),
            ),
            (
                "МАЛИБУ: BEACH PARTY",
                "Розовый закат, бирюзовый океан и летний вайб. Яркие скины и редкие градиенты.",
                lambda x: any(k in str(x.get("name", "")).casefold() for k in (
                    "vice", "fade", "doppler", "marble fade", "ocean drive",
                    "berries and cherries", "neon rider", "neo-noir", "pink",
                    "sugar rush", "vogue", "starlight protector"
                )),
            ),
            (
                "МОРФЕЙ: СОН НАЯВУ",
                "Фиолетовые и синие оттенки, космические эффекты и загадочные раскраски.",
                lambda x: any(k in str(x.get("name", "")).casefold() for k in (
                    "doppler", "sapphire", "blue", "night", "nightmare",
                    "cobalt", "lunar", "phosphor", "nebula", "purple",
                    "blue steel", "ultraviolet"
                )),
            ),
            (
                "КРАСНЫЙ КОД",
                "Красная тревога: кровавые узоры, огонь и агрессивные красные раскраски.",
                lambda x: any(k in str(x.get("name", "")).casefold() for k in (
                    "crimson", "bloodsport", "redline", "slaughter", "cardiac",
                    "hot rod", "ruby", "the empress", "燃", "sang"
                )),
            ),
            (
                "БОГИ И ЧУДОВИЩА",
                "Мифы, древние боги и легендарные существа. Подборка редких тематических скинов.",
                lambda x: any(k in str(x.get("name", "")).casefold() for k in (
                    "medusa", "poseidon", "hades", "zeus", "hydra", "cerberus",
                    "dragon lore", "eye of horus", "anubis", "mjolnir", "gungnir",
                    "icarus", "myth", "gods"
                )),
            ),
            (
                "ОПЕРАЦИЯ: ПОСЛЕДНИЙ РУБЕЖ",
                "Постапокалипсис, опасные зоны и выживание. Контрастные военные и токсичные расцветки.",
                lambda x: any(k in str(x.get("name", "")).casefold() for k in (
                    "wasteland", "containment", "toxic", "radiation", "fallout",
                    "nuclear", "hazard", "jungle", "survival", "outbreak",
                    "scavenger", "green"
                )),
            ),
            (
                "НОЖЕВОЙ РЕЙД",
                "Отдельная охота за ножами и перчатками: в пуле только ножевые и перчаточные предметы.",
                lambda x: any(k in str(x.get("weapon", "")).casefold() for k in (
                    "knife", "karambit", "bayonet", "daggers", "talon", "stiletto",
                    "kukri", "ursus", "navaja", "nomad", "paracord", "skeleton",
                    "survival", "falchion", "bowie", "flip", "gut", "gloves"
                )),
            ),
            (
                "ЗОЛОТОЙ БИЛЕТ",
                "Золотые покрытия, градиенты и драгоценные фазы. Премиальная тематическая подборка.",
                lambda x: any(k in str(x.get("name", "")).casefold() for k in (
                    "fade", "doppler", "tiger tooth", "marble fade", "gamma doppler",
                    "gold", "lore", "ruby", "sapphire", "emerald", "black pearl",
                    "case hardened", "the prince", "medusa"
                )),
            ),
        ]
        async with conn.transaction():
            await conn.execute("""
                UPDATE cases SET active=FALSE
                WHERE title = ANY($1::text[])
                   OR title = ANY($2::text[])
                   OR title='Премиум кейс'
            """, standard_case_titles, legacy_shirpdrop_case_titles)
            for title, description, predicate in themed_case_specs:
                selected = [
                    skin for skin in catalog_skins
                    if skin.get("market_hash_name") and predicate(skin)
                ]
                unique = {skin["market_hash_name"]: skin for skin in selected}
                selected = list(unique.values())
                selected.sort(key=lambda x: (
                    cs2_case_weight(x.get("rarity")),
                    -int(x.get("value_minor") or 0),
                    str(x.get("name") or "")
                ))
                selected = selected[:160]
                if len(selected) < 8:
                    logger.warning("Skipping themed case %s: only %s matching catalog items", title, len(selected))
                    continue
                cover_url = selected[0].get("image_url")
                case_id = await conn.fetchval(
                    "SELECT id FROM cases WHERE title=$1 ORDER BY id LIMIT 1",
                    title
                )
                if case_id:
                    await conn.execute("""
                        UPDATE cases SET description=$2, price_minor=0, currency='RUB',
                            image_url=COALESCE(image_url, $3), active=TRUE
                        WHERE id=$1
                    """, case_id, description + " Демо-режим: виртуальные предметы, без денежных ставок.", cover_url)
                else:
                    case_id = await conn.fetchval("""
                        INSERT INTO cases(title, description, price_minor, currency, image_url, active)
                        VALUES($1,$2,0,'RUB',$3,TRUE) RETURNING id
                    """, title, description + " Демо-режим: виртуальные предметы, без денежных ставок.", cover_url)
                has_items = await conn.fetchval(
                    "SELECT EXISTS(SELECT 1 FROM case_items WHERE case_id=$1)",
                    case_id
                )
                if not has_items:
                    records = []
                    for skin in selected:
                        try:
                            value_minor = int(skin.get("value_minor") or 0)
                        except (TypeError, ValueError):
                            value_minor = 0
                        records.append((
                            case_id, skin.get("name") or skin["market_hash_name"],
                            skin.get("rarity") or "Неизвестно",
                            cs2_case_weight(skin.get("rarity")),
                            skin.get("image_url"), value_minor,
                            skin["market_hash_name"], skin.get("wear") or "",
                            bool(skin.get("stattrak")), bool(skin.get("souvenir")),
                            skin.get("phase") or ""
                        ))
                    await conn.executemany("""
                        INSERT INTO case_items(
                            case_id,name,rarity,weight,image_url,value_minor,
                            market_hash_name,wear,stattrak,souvenir,phase
                        )
                        VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                        ON CONFLICT (case_id, market_hash_name)
                        WHERE market_hash_name IS NOT NULL DO NOTHING
                    """, records)

        # Public Caser price points are stored separately from the playable price.
        # The app remains demo-only: no money is charged and no paid opening is enabled.
        caser_case_prices = {
            "snakebite case": 18000,
            "revolution case": 20000,
            "clutch case": 21000,
            "recoil case": 21900,
            "fracture case": 23500,
            "falchion case": 24000,
            "danger zone case": 26000,
            "gallery case": 29500,
            "prisma case": 32000,
            "prisma 2 case": 33300,
            "horizon case": 33500,
            "dreams nightmares case": 35000,
            "dreams and nightmares case": 35000,
            "operation riptide case": 58000,
            "kilowatt case": 35000,
            "cs20 case": 38000,
            "shadow case": 39000,
            "shattered web case": 45000,
            "operation phoenix weapon case": 55000,
            "revolver case": 65000,
            "spectrum 2 case": 69000,
            "operation wildfire case": 69000,
            "spectrum case": 80000,
            "operation broken fang case": 105000,
            "operation hydra case": 300000,
        }
        caser_style_specs = [
            ("MAJOR", 2900, lambda x: "mil-spec" in str(x.get("rarity") or "").casefold()),
            ("CLEAVER", 2900, lambda x: "mil-spec" in str(x.get("rarity") or "").casefold() and 0 < int(x.get("value_minor") or 0) <= 10000),
            ("BLOWBACK", 3500, lambda x: "mil-spec" in str(x.get("rarity") or "").casefold() or ("restricted" in str(x.get("rarity") or "").casefold() and int(x.get("value_minor") or 0) <= 20000)),
            ("GENESIS", 8900, lambda x: any(k in str(x.get("rarity") or "").casefold() for k in ("restricted", "classified"))),
            ("SIGMA 2", 5500, lambda x: "mil-spec" in str(x.get("rarity") or "").casefold() and int(x.get("value_minor") or 0) <= 25000),
            ("SIGMA", 5900, lambda x: "restricted" in str(x.get("rarity") or "").casefold()),
            ("ECLIPSE", 7900, lambda x: any(k in str(x.get("rarity") or "").casefold() for k in ("mil-spec", "restricted"))),
            ("MIL-SPEC", 3900, lambda x: "mil-spec" in str(x.get("rarity") or "").casefold()),
            ("RESTRICTED", 15900, lambda x: "restricted" in str(x.get("rarity") or "").casefold()),
            ("CLASSIFIED", 64900, lambda x: "classified" in str(x.get("rarity") or "").casefold()),
            ("COVERT", 589900, lambda x: "covert" in str(x.get("rarity") or "").casefold()),
            ("KNIFE", 1249900, lambda x: any(k in str(x.get("weapon") or x.get("name") or "").casefold() for k in ("knife", "karambit", "bayonet", "daggers", "talon", "stiletto", "kukri", "ursus", "navaja", "nomad", "paracord", "skeleton", "survival", "falchion", "bowie", "flip", "gut", "gloves"))),
            ("PREMIUM 1", 499900, lambda x: any(k in str(x.get("rarity") or "").casefold() for k in ("classified", "covert"))),
        ]
        for title, reference_price, predicate in caser_style_specs:
            selected = [skin for skin in catalog_skins if skin.get("market_hash_name") and predicate(skin)]
            unique = {skin["market_hash_name"]: skin for skin in selected}
            selected = list(unique.values())
            selected.sort(key=lambda x: (int(x.get("value_minor") or 0), str(x.get("name") or "")))
            selected = selected[:160]
            if len(selected) < 8:
                logger.info("Skipping Caser-inspired demo case %s: only %s matching catalog items", title, len(selected))
                continue
            cover_url = selected[0].get("image_url")
            case_id = await conn.fetchval("SELECT id FROM cases WHERE title=$1 ORDER BY id LIMIT 1", title)
            description = "Авторская подборка ShirpDrop по мотивам публичных категорий кейсов. Ориентир цены взят из открытого каталога Caser; открытие бесплатное и демо."
            if case_id:
                await conn.execute("""
                    UPDATE cases SET description=$2, price_minor=0, reference_price_minor=$3,
                        currency='RUB', image_url=COALESCE(image_url,$4), active=TRUE
                    WHERE id=$1
                """, case_id, description, reference_price, cover_url)
            else:
                case_id = await conn.fetchval("""
                    INSERT INTO cases(title,description,price_minor,reference_price_minor,currency,image_url,active)
                    VALUES($1,$2,0,$3,'RUB',$4,TRUE) RETURNING id
                """, title, description, reference_price, cover_url)
            has_items = await conn.fetchval("SELECT EXISTS(SELECT 1 FROM case_items WHERE case_id=$1)", case_id)
            if not has_items:
                records = []
                for skin in selected:
                    records.append((
                        case_id, skin.get("name") or skin["market_hash_name"],
                        skin.get("rarity") or "Неизвестно", cs2_case_weight(skin.get("rarity")),
                        skin.get("image_url"), int(skin.get("value_minor") or 0),
                        skin["market_hash_name"], skin.get("wear") or "",
                        bool(skin.get("stattrak")), bool(skin.get("souvenir")), skin.get("phase") or ""
                    ))
                await conn.executemany("""
                    INSERT INTO case_items(case_id,name,rarity,weight,image_url,value_minor,market_hash_name,wear,stattrak,souvenir,phase)
                    VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
                    ON CONFLICT (case_id, market_hash_name) WHERE market_hash_name IS NOT NULL DO NOTHING
                """, records)

        for catalog_case in catalog_cases:
            normalized_title = re.sub(r"[^a-z0-9]+", " ", str(catalog_case.get("name") or "").casefold()).strip()
            reference_price = caser_case_prices.get(normalized_title)
            if not reference_price:
                continue
            existing_case = await conn.fetchrow("""
                SELECT c.id, EXISTS(
                    SELECT 1 FROM case_items ci
                    WHERE ci.case_id=c.id AND ci.is_active=TRUE
                ) AS has_items
                FROM cases c WHERE c.title=$1 ORDER BY c.id LIMIT 1
            """, catalog_case["name"])
            if existing_case and existing_case["has_items"]:
                await conn.execute("""
                    UPDATE cases
                    SET active=TRUE, reference_price_minor=$2
                    WHERE id=$1
                """, existing_case["id"], reference_price)

        cases = await conn.fetch("""
            SELECT id, title, description, price_minor, reference_price_minor, currency, image_url
            FROM cases
            WHERE active = TRUE
            ORDER BY id
        """)
        items = await conn.fetch("""
            SELECT ci.case_id, ci.id, ci.name, ci.rarity, ci.weight,
                   ci.image_url, ci.value_minor, ci.market_hash_name, ci.wear,
                   ci.stattrak, ci.souvenir, ci.phase,
                   p.lowest_price, p.fetched_at, p.image_url AS price_image_url
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
                     (datetime.now(timezone.utc) - item["fetched_at"]).total_seconds() <= CS2_PRICE_USABLE_TTL_SECONDS)
        market_minor = steam_price_to_minor(item["lowest_price"]) if fresh else None
        grouped.setdefault(item["case_id"], []).append({
            # Only fields consumed by the Mini App's case preview and reel.
            # Keep the full weighted item pool in PostgreSQL for server-side rolls.
            "name": item["name"],
            "rarity": item["rarity"],
            "weight": item["weight"],
            "image_url": item["image_url"] or item.get("price_image_url") or
                         skin_images.get(str(item["name"] or "").strip().casefold()) or
                         skin_images.get(str(item["market_hash_name"] or "").strip().casefold()),
            "market_price_minor": market_minor,
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
            "reference_price_minor": case["reference_price_minor"],
            "estimated_return_minor": (sum(int(x["weight"]) * int(x.get("market_price_minor") or x.get("value_minor") or 0) * 90 for x in contents) // (total * 100)) if total else 0,
            "estimated_rtp_percent": round((sum(int(x["weight"]) * int(x.get("market_price_minor") or x.get("value_minor") or 0) * 90 for x in contents) / (total * 100)) * 100 / case["reference_price_minor"], 2) if total and case["reference_price_minor"] else None,
            "currency": case["currency"],
            "image_url": case["image_url"] or fallback_case_image,
            "demo_only": False,
            "catalog_only": True,
            "opening_enabled": False,
            "items_total": len(contents),
            "items": [{
                **item,
                "chance_percent": round(item["weight"] * 100 / total, 2) if total else 0
            } for item in visible_contents],
        })
    return {"items": result}


async def get_cases(request):
    global CASES_RESPONSE_CACHE, CASES_RESPONSE_CACHE_AT
    now = time.monotonic()
    if CASES_RESPONSE_CACHE is not None and now - CASES_RESPONSE_CACHE_AT < CASES_RESPONSE_CACHE_TTL:
        return web.json_response(CASES_RESPONSE_CACHE, headers={"Cache-Control": "private, max-age=15"})
    async with CASES_RESPONSE_LOCK:
        now = time.monotonic()
        if CASES_RESPONSE_CACHE is not None and now - CASES_RESPONSE_CACHE_AT < CASES_RESPONSE_CACHE_TTL:
            return web.json_response(CASES_RESPONSE_CACHE, headers={"Cache-Control": "private, max-age=15"})
        payload = await build_cases_response(request)
        CASES_RESPONSE_CACHE = payload
        CASES_RESPONSE_CACHE_AT = time.monotonic()
        return web.json_response(payload, headers={"Cache-Control": "private, max-age=15"})


async def open_case(request):
    user = verify_telegram_user(request)
    try:
        case_id = int(request.match_info["case_id"])
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="Некорректный ID кейса")

    try:
        payload = await request.json()
    except Exception:
        payload = {}
    try:
        count = int(payload.get("count", 1)) if isinstance(payload, dict) else 1
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="Некорректное количество кейсов")
    if count < 1 or count > 5:
        raise web.HTTPBadRequest(text="За одно открытие можно выбрать от 1 до 5 кейсов")

    opened_items = []
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

            for _ in range(count):
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
                    selected_name = str(selected.get("name") or "").strip().casefold()
                    selected_market = str(selected.get("market_hash_name") or "").strip().casefold()
                    selected["image_url"] = next(
                        (item["image_url"] for item in cached_skins
                         if item["market_hash_name"].casefold() == selected_market
                         or item["name"].casefold() == selected_name
                         or item.get("base_name", "").casefold() == selected_name),
                        None,
                    )

                market_minor = None
                if selected.get("market_hash_name"):
                    price_row = await conn.fetchrow("""
                        SELECT lowest_price, fetched_at FROM cs2_steam_prices
                        WHERE market_hash_name=$1 AND price_source <> 'steam'
                    """, selected["market_hash_name"])
                    fresh = bool(price_row and
                        (datetime.now(timezone.utc) - price_row["fetched_at"]).total_seconds() <= CS2_PRICE_USABLE_TTL_SECONDS)
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
                    "item_id": selected["id"], "roll": roll,
                    "total_weight": total, "batch_count": count
                }))
                opened_items.append({
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
                    "opening_id": opening_id,
                    "chance_percent": round(selected["weight"] * 100 / total, 2),
                })

    return web.json_response({
        "ok": True, "opening_id": opened_items[0]["opening_id"],
        "case_id": case_id, "case_title": case["title"],
        "count": count, "items": opened_items, "item": opened_items[0],
        "demo_only": True,
        "message": "Виртуальные предметы симуляции; не являются предметами Steam."
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




async def get_cash_wallet(request):
    user = verify_telegram_user(request)
    async with db.acquire() as conn:
        await conn.execute("""
            INSERT INTO cash_wallet_accounts(user_id) VALUES($1)
            ON CONFLICT (user_id) DO NOTHING
        """, user["id"])
        account = await conn.fetchrow("""
            SELECT balance_minor, updated_at FROM cash_wallet_accounts WHERE user_id=$1
        """, user["id"])
        rows = await conn.fetch("""
            SELECT amount_minor, reason, created_at FROM cash_wallet_ledger
            WHERE user_id=$1 ORDER BY id DESC LIMIT 20
        """, user["id"])
        topups = await conn.fetch("""
            SELECT id, amount_minor, status, created_at, credited_at
            FROM wallet_topups WHERE user_id=$1 ORDER BY id DESC LIMIT 10
        """, user["id"])
    return web.json_response({
        "balance_minor": account["balance_minor"], "currency": "RUB",
        "updated_at": account["updated_at"].isoformat(),
        "ledger": [{
            "amount_minor": row["amount_minor"], "reason": row["reason"],
            "created_at": row["created_at"].isoformat()
        } for row in rows],
        "topups": [{
            "id": row["id"], "amount_minor": row["amount_minor"],
            "status": row["status"], "created_at": row["created_at"].isoformat(),
            "credited_at": row["credited_at"].isoformat() if row["credited_at"] else None
        } for row in topups]
    })


async def create_telebotly_topup(request):
    user = verify_telegram_user(request)
    if not TELEBOTLY_PAY_KEY:
        raise web.HTTPServiceUnavailable(text="TELEBOTLY_PAY_KEY не задан в Render")
    # TeleBotly Pay project keys may be issued with tbp_* or the newer
    # account API tbk_* prefix. Do not reject locally by prefix: let /project
    # validate the credential and its permissions.
    if not TELEBOTLY_WEBHOOK_SECRET or not TELEBOTLY_WEBHOOK_SECRET.startswith("whsec_"):
        raise web.HTTPServiceUnavailable(text="TELEBOTLY_WEBHOOK_SECRET отсутствует или имеет неверный формат (ожидается whsec_)")
    try:
        data = await request.json()
        amount_minor = int(data.get("amount_minor"))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Укажи сумму пополнения")
    if amount_minor < 1000 or amount_minor > 10000000:
        raise web.HTTPBadRequest(text="Сумма пополнения должна быть от 10 до 100 000 ₽")

    idempotency_key = "shirpd-topup-" + secrets.token_urlsafe(24)
    async with db.acquire() as conn:
        topup_id = await conn.fetchval("""
            INSERT INTO wallet_topups(user_id, amount_minor, idempotency_key)
            VALUES($1, $2, $3) RETURNING id
        """, user["id"], amount_minor, idempotency_key)

    amount = f"{Decimal(amount_minor) / Decimal(100):.2f}"
    payload = {
        "amount": amount,
        "currency": "RUB",
        "methods": ["sbp"],
        "description": f"Пополнение баланса ShirpDrop #{topup_id}",
        "order_id": str(topup_id),
        "customer": {"id": str(user["id"]), "telegram_id": user["id"]},
        "metadata": {
            "wallet_topup_id": str(topup_id),
            "user_id": str(user["id"]),
            "purpose": "cash_wallet_topup"
        }
    }
    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{TELEBOTLY_API_BASE}/project",
                headers={"Authorization": f"Bearer {TELEBOTLY_PAY_KEY}"}
            ) as project_response:
                project_result = await project_response.json(content_type=None)
                if project_response.status != 200:
                    err = project_result.get("error") if isinstance(project_result, dict) else None
                    if not isinstance(err, dict):
                        err = {}
                    logger.warning(
                        "TeleBotly project check failed: status=%s code=%s message=%s request_id=%s",
                        project_response.status, err.get("code"), err.get("message"), err.get("request_id")
                    )
                    raise web.HTTPBadGateway(
                        text=f"TeleBotly не подтвердил Pay API Key (HTTP {project_response.status}, {err.get('code') or 'ошибка ключа'})"
                    )
                project = project_result.get("data") or project_result
                is_live_key = TELEBOTLY_PAY_KEY.startswith(("tbp_live_", "tbk_live_"))
                is_live_project = project.get("livemode") is True
                if (is_live_key or is_live_project) and not project.get("accepts_live_payments"):
                    raise web.HTTPServiceUnavailable(text="TeleBotly пока не разрешил проекту принимать реальные платежи")
            async with session.post(
                f"{TELEBOTLY_API_BASE}/payments",
                json=payload,
                headers={
                    "Authorization": f"Bearer {TELEBOTLY_PAY_KEY}",
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotency_key
                }
            ) as response:
                result = await response.json(content_type=None)
                if response.status not in {200, 201}:
                    err = result.get("error") if isinstance(result, dict) else None
                    if not isinstance(err, dict):
                        err = {}
                    logger.warning(
                        "TeleBotly create payment rejected: status=%s code=%s message=%s request_id=%s",
                        response.status, err.get("code"), err.get("message"), err.get("request_id")
                    )
                    raise web.HTTPBadGateway(
                        text=f"TeleBotly отклонил создание платежа (HTTP {response.status}, {err.get('code') or 'ошибка API'}). Проверь статус проекта и доступность СБП."
                    )
    except web.HTTPException:
        async with db.acquire() as conn:
            await conn.execute("UPDATE wallet_topups SET status='failed', updated_at=NOW() WHERE id=$1", topup_id)
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        async with db.acquire() as conn:
            await conn.execute("UPDATE wallet_topups SET status='failed', updated_at=NOW() WHERE id=$1", topup_id)
        raise web.HTTPBadGateway(text="Не удалось связаться с TeleBotly Pay")

    payment = result.get("data") or result.get("payment") or {}
    payment_id = payment.get("id")
    payment_url = payment.get("url")
    if not payment_id or not payment_url or not str(payment_url).startswith("https://pay.telebotly.app/") or payment.get("status") != "pending":
        async with db.acquire() as conn:
            await conn.execute("UPDATE wallet_topups SET status='failed', updated_at=NOW() WHERE id=$1", topup_id)
        raise web.HTTPBadGateway(text="TeleBotly вернул неполные данные платежа")
    returned_minor = payment.get("amount_minor")
    if returned_minor is not None and int(returned_minor) != amount_minor:
        raise web.HTTPBadGateway(text="Сумма платежа в ответе TeleBotly не совпала")
    livemode = bool(payment.get("livemode", False))
    async with db.acquire() as conn:
        await conn.execute("""
            UPDATE wallet_topups SET status='pending', provider_payment_id=$2,
                payment_url=$3, livemode=$4, updated_at=NOW()
            WHERE id=$1
        """, topup_id, str(payment_id), str(payment_url), livemode)
    return web.json_response({
        "ok": True, "topup_id": topup_id, "payment_url": payment_url,
        "amount_minor": amount_minor, "currency": "RUB",
        "livemode": livemode, "status": "pending"
    })


def verify_telebotly_signature(secret: str, headers, body: bytes) -> bool:
    if not secret or not secret.startswith("whsec_"):
        return False
    event_id = headers.get("webhook-id", "")
    timestamp = headers.get("webhook-timestamp", "")
    signatures = headers.get("webhook-signature", "")
    try:
        if not event_id or not timestamp or abs(time.time() - int(timestamp)) > 300:
            return False
        key = __import__("base64").b64decode(secret.removeprefix("whsec_"), validate=True)
        expected = hmac.new(
            key, f"{event_id}.{timestamp}.".encode("utf-8") + body, hashlib.sha256
        ).digest()
        for part in signatures.split():
            if not part.startswith("v1,"):
                continue
            supplied = __import__("base64").b64decode(part[3:], validate=True)
            if hmac.compare_digest(expected, supplied):
                return True
    except (ValueError, TypeError):
        return False
    return False


async def telebotly_webhook(request):
    if not TELEBOTLY_WEBHOOK_SECRET:
        raise web.HTTPServiceUnavailable(text="Webhook secret is not configured")
    raw_body = await request.read()
    if not verify_telebotly_signature(TELEBOTLY_WEBHOOK_SECRET, request.headers, raw_body):
        raise web.HTTPUnauthorized(text="Invalid webhook signature")
    try:
        event = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Invalid event JSON")
    event_id = str(event.get("id") or request.headers.get("webhook-id") or "")
    event_type = str(event.get("type") or "")
    if not event_id:
        raise web.HTTPBadRequest(text="Missing event ID")
    if event_type == "test.ping":
        return web.json_response({"ok": True, "received": True})
    payment = (event.get("data") or {}).get("payment") or {}
    if event_type != "payment.paid":
        async with db.acquire() as conn:
            await conn.execute("""
                INSERT INTO telebotly_webhook_events(event_id, event_type, payload, processed_at)
                VALUES($1, $2, $3::jsonb, NOW()) ON CONFLICT(event_id) DO NOTHING
            """, event_id, event_type, json.dumps(event))
        return web.json_response({"ok": True, "ignored": True})

    try:
        topup_id = int(payment.get("order_id"))
        payment_minor = int(payment.get("amount_minor"))
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="Payment is missing order or amount")
    if payment.get("status") != "paid" or payment.get("currency") != "RUB":
        raise web.HTTPBadRequest(text="Payment is not confirmed in RUB")
    if not payment.get("id"):
        raise web.HTTPBadRequest(text="Payment ID is missing")

    async with db.acquire() as conn:
        async with conn.transaction():
            inserted = await conn.fetchval("""
                INSERT INTO telebotly_webhook_events(event_id, event_type, payload)
                VALUES($1, $2, $3::jsonb)
                ON CONFLICT(event_id) DO NOTHING RETURNING event_id
            """, event_id, event_type, json.dumps(event))
            if not inserted:
                return web.json_response({"ok": True, "duplicate": True})
            topup = await conn.fetchrow("""
                SELECT id, user_id, amount_minor, currency, status,
                       provider_payment_id, livemode
                FROM wallet_topups WHERE id=$1 FOR UPDATE
            """, topup_id)
            if not topup or topup["provider_payment_id"] != str(payment["id"]):
                raise web.HTTPBadRequest(text="Top-up reference does not match")
            if topup["amount_minor"] != payment_minor or topup["currency"] != payment["currency"]:
                raise web.HTTPBadRequest(text="Payment amount does not match top-up")
            if bool(event.get("livemode", False)) != bool(topup["livemode"]):
                raise web.HTTPBadRequest(text="Payment mode does not match top-up")
            if topup["status"] == "credited":
                await conn.execute("""
                    UPDATE telebotly_webhook_events SET processed_at=NOW() WHERE event_id=$1
                """, event_id)
                return web.json_response({"ok": True, "already_credited": True})
            await conn.execute("""
                INSERT INTO cash_wallet_accounts(user_id) VALUES($1)
                ON CONFLICT(user_id) DO NOTHING
            """, topup["user_id"])
            ledger_id = await conn.fetchval("""
                INSERT INTO cash_wallet_ledger(user_id, operation_id, amount_minor, currency, reason)
                VALUES($1, $2, $3, 'RUB', 'telebotly_topup')
                ON CONFLICT(operation_id) DO NOTHING RETURNING id
            """, topup["user_id"], "telebotly:" + str(payment["id"]), payment_minor)
            if ledger_id:
                await conn.execute("""
                    UPDATE cash_wallet_accounts SET balance_minor=balance_minor+$2, updated_at=NOW()
                    WHERE user_id=$1
                """, topup["user_id"], payment_minor)
            await conn.execute("""
                UPDATE wallet_topups SET status='credited', credited_at=NOW(), updated_at=NOW()
                WHERE id=$1
            """, topup_id)
            await conn.execute("""
                UPDATE telebotly_webhook_events SET processed_at=NOW() WHERE event_id=$1
            """, event_id)
    return web.json_response({"ok": True, "credited": True})



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
                   p.lowest_price, p.fetched_at,
                   (f.inventory_id IS NOT NULL) AS is_favorite
            FROM user_inventory i
            LEFT JOIN cs2_steam_prices p ON p.market_hash_name=i.market_hash_name
                AND p.price_source <> 'steam'
            LEFT JOIN user_inventory_favorites f
                ON f.inventory_id=i.id AND f.user_id=i.user_id
            WHERE i.user_id = $1 AND i.consumed_at IS NULL
            ORDER BY i.id DESC LIMIT 100
        """, user["id"])
    items = []
    for row in rows:
        age_seconds = (datetime.now(timezone.utc) - row["fetched_at"]).total_seconds() if row["fetched_at"] else float("inf")
        usable = age_seconds <= CS2_PRICE_USABLE_TTL_SECONDS
        refresh_due = age_seconds > CS2_PRICE_REFRESH_TTL_SECONDS
        market_minor = steam_price_to_minor(row["lowest_price"]) if usable else None
        if row["market_hash_name"] and refresh_due:
            queue_cs2_price_refresh([{"market_hash_name": row["market_hash_name"]}])
        items.append({
            "id": row["id"], "item_type": row["item_type"],
            "item_name": row["item_name"], "rarity": row["rarity"],
            "image_url": row["image_url"],
            "value_minor": market_minor or row["value_minor"],
            "market_price_minor": market_minor,
            "market_price_stale": bool(market_minor and refresh_due),
            "sell_value_minor": (market_minor * 90 // 100) if market_minor else None,
            "price_updated_at": row["fetched_at"].isoformat() if usable else None,
            "source": row["source"], "created_at": row["created_at"].isoformat(),
            "market_hash_name": row["market_hash_name"], "wear": row["wear"],
            "stattrak": row["stattrak"], "souvenir": row["souvenir"],
            "phase": row["phase"], "is_favorite": row["is_favorite"]
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

async def persist_cs2_skin_catalog(items):
    """Persist the complete normalized skin catalog in small additive batches."""
    if not items or db is None:
        return 0
    rows = [
        (
            item["market_hash_name"], item.get("base_name", ""),
            item.get("weapon", ""), item.get("wear", ""),
            item.get("rarity", ""), item.get("phase", ""),
            bool(item.get("stattrak")), bool(item.get("souvenir")),
            item.get("image_url"), int(item.get("value_minor") or 0),
        )
        for item in items if item.get("market_hash_name")
    ]
    async with db.acquire() as conn:
        for offset in range(0, len(rows), 500):
            await conn.executemany("""
                INSERT INTO cs2_skin_catalog(
                    market_hash_name, base_name, weapon, wear, rarity, phase,
                    stattrak, souvenir, image_url, value_minor, catalog_updated_at
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NOW())
                ON CONFLICT (market_hash_name) DO UPDATE SET
                    base_name=EXCLUDED.base_name,
                    weapon=EXCLUDED.weapon,
                    wear=EXCLUDED.wear,
                    rarity=EXCLUDED.rarity,
                    phase=EXCLUDED.phase,
                    stattrak=EXCLUDED.stattrak,
                    souvenir=EXCLUDED.souvenir,
                    image_url=COALESCE(EXCLUDED.image_url, cs2_skin_catalog.image_url),
                    value_minor=EXCLUDED.value_minor,
                    catalog_updated_at=NOW()
            """, rows[offset:offset + 500])
    logger.info("CS2 skin catalog persisted: %s variants", len(rows))
    return len(rows)


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
            if not name:
                continue
            if not image_url.startswith("https://"):
                image_url = None
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
        if kind == "skins":
            await persist_cs2_skin_catalog(normalized)
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


async def _save_market_price_updates(updates):
    if not updates:
        return 0
    async with db.acquire() as conn:
        await conn.executemany("""
            INSERT INTO cs2_steam_prices(
                market_hash_name, lowest_price, median_price,
                volume, currency, fetched_at, price_source, image_url
            )
            VALUES($1, $2, NULL, NULL, 'RUB', $3, $4, $5)
            ON CONFLICT (market_hash_name) DO UPDATE SET
                lowest_price=EXCLUDED.lowest_price,
                median_price=NULL,
                volume=NULL,
                currency='RUB',
                fetched_at=EXCLUDED.fetched_at,
                price_source=EXCLUDED.price_source,
                image_url=COALESCE(EXCLUDED.image_url, cs2_steam_prices.image_url)
        """, updates)
    return len(updates)


def _parse_market_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


async def _fetch_openskin_prices(session, batch):
    """Fetch up to 500 exact market names from OpenSkin's public batch endpoint."""
    try:
        async with session.post(
            "https://api.openskin.dev/v1/prices/batch",
            json={"items": batch},
            headers={"Accept": "application/json"},
        ) as response:
            if response.status != 200:
                body = (await response.text())[:250]
                logger.warning("OpenSkin batch returned HTTP %s: %s", response.status, body)
                return []
            payload = await response.json(content_type=None)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("OpenSkin price batch failed", exc_info=True)
        return []

    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    if not isinstance(data, dict) or not data:
        logger.info("OpenSkin returned no priced items for batch of %s", len(batch))
        return []

    now = datetime.now(timezone.utc)
    fx_rate = None
    updates = []
    for market_name, item in data.items():
        if not isinstance(item, dict):
            continue
        steam = item.get("steam") or {}
        lowest = item.get("lowest_ask") or {}
        # Use the cheapest currently listed ask across supported marketplaces,
        # like case sites that benchmark against tradable marketplace listings.
        # Fall back to Steam's own ask only when the aggregator has no floor quote.
        price = lowest.get("price")
        marketplace = str(lowest.get("marketplace") or "").strip().lower()
        provider = (
            "openskin_" + re.sub(r"[^a-z0-9_]+", "_", marketplace)[:32]
            if marketplace else "openskin_market"
        )
        if price is None:
            price = steam.get("ask")
            provider = "openskin_steam"
        if price is None:
            for marketplace in ("skinport", "csfloat", "buff", "youpin"):
                quote = item.get(marketplace) or {}
                if quote.get("ask") is not None:
                    price = quote["ask"]
                    provider = "openskin_" + marketplace
                    break
        try:
            usd_price = Decimal(str(price))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if usd_price <= 0:
            continue
        updated_at = _parse_market_timestamp(item.get("updated_at") or payload.get("updated_at"))
        if updated_at and (now - updated_at).total_seconds() > CS2_PRICE_USABLE_TTL_SECONDS:
            continue
        if updated_at and updated_at > now + timedelta(minutes=5):
            continue
        if fx_rate is None:
            try:
                fx_rate = await get_usd_rub_rate(session)
            except Exception:
                logger.warning("USD/RUB rate unavailable; OpenSkin quotes were not stored", exc_info=True)
                return []
        rub_price = (usd_price * fx_rate).quantize(Decimal("0.01"))
        if rub_price <= 0:
            continue
        # Keep the upstream observation time where supplied; this lets the
        # normal seven-day freshness check reject old market snapshots.
        fetched_at = updated_at or now
        image_url = str(item.get("icon_url") or item.get("image_url") or "").strip()
        if not image_url.startswith("https://"):
            image_url = None
        updates.append((market_name, f"{rub_price} ₽", fetched_at, provider, image_url))
    return updates


async def _fetch_apiskins_prices(session, batch):
    """Legacy paid-key provider fallback; a 429 is remembered instead of busy-looped."""
    global APISKINS_COOLDOWN_UNTIL
    if not APISKINS_API_KEY or time.time() < APISKINS_COOLDOWN_UNTIL:
        return []
    payload = {
        "market_hash_names": batch,
        "providers": ["lis_skins", "market_csgo", "cs_float", "skinport", "dmarket", "waxpeer"],
    }
    try:
        async with session.post(
            "https://api.apiskins.com/v1/offers/batch",
            json=payload,
            headers={"Accept": "application/json", "X-API-KEY": APISKINS_API_KEY},
        ) as response:
            if response.status == 429:
                APISKINS_COOLDOWN_UNTIL = time.time() + 1800
                logger.warning("API Skins rate limited; fallback disabled for 30 minutes")
                return []
            if response.status != 200:
                body = (await response.text())[:250]
                logger.warning("API Skins fallback returned HTTP %s: %s", response.status, body)
                return []
            offers_payload = await response.json(content_type=None)
        if not isinstance(offers_payload, list):
            return []
        rate = await get_usd_rub_rate(session)
        updates = []
        for item in offers_payload:
            if not isinstance(item, dict):
                continue
            by_provider = {
                offer.get("provider"): offer.get("price")
                for offer in item.get("offers", [])
                if isinstance(offer, dict)
            }
            provider = next(
                (name for name in ("lis_skins", "market_csgo", "cs_float", "skinport", "dmarket", "waxpeer")
                 if by_provider.get(name) is not None),
                None,
            )
            if not provider:
                continue
            try:
                rub_price = (Decimal(str(by_provider[provider])) * rate).quantize(Decimal("0.01"))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if rub_price > 0 and item.get("market_hash_name"):
                updates.append((item["market_hash_name"], f"{rub_price} ₽", datetime.now(timezone.utc), provider, None))
        return updates
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("API Skins fallback failed", exc_info=True)
        return []


async def cs2_steam_price_worker():
    """Refresh market quotes in one batch, preferring OpenSkin over quota-limited API Skins."""
    timeout = aiohttp.ClientTimeout(total=35, connect=10)
    headers = {"Accept": "application/json"}
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        while True:
            first = await CS2_PRICE_QUEUE.get()
            batch = [first]
            # Keep provider requests and in-memory batches small on Render Free (512 MiB).
            while len(batch) < 100:
                try:
                    batch.append(CS2_PRICE_QUEUE.get_nowait())
                except asyncio.QueueEmpty:
                    break
            try:
                updates = await _fetch_openskin_prices(session, batch)
                updated_names = {row[0] for row in updates}
                missing = [name for name in batch if name not in updated_names]
                if missing:
                    # Use API Skins only for gaps, never for items already priced
                    # by OpenSkin. A 429 is remembered and never stalls the queue.
                    updates.extend(await _fetch_apiskins_prices(session, missing))
                saved = await _save_market_price_updates(updates)
                updated_names = {row[0] for row in updates}
                async with db.acquire() as conn:
                    await conn.executemany("""
                        INSERT INTO cs2_price_checks(market_hash_name, last_checked_at, has_quote, price_source)
                        VALUES($1, NOW(), $2, $3)
                        ON CONFLICT (market_hash_name) DO UPDATE SET
                            last_checked_at=NOW(),
                            has_quote=EXCLUDED.has_quote,
                            price_source=EXCLUDED.price_source
                    """, [
                        (name, name in updated_names,
                         next((row[3] for row in updates if row[0] == name), None))
                        for name in batch
                    ])
                logger.info(
                    "CS2 market price batch completed: %s requested, %s updated (OpenSkin primary)",
                    len(batch), saved
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("CS2 market price worker failed for batch of %s", len(batch), exc_info=True)
            finally:
                for market_name in batch:
                    CS2_PRICE_QUEUED.discard(market_name)
                    CS2_PRICE_QUEUE.task_done()
            await asyncio.sleep(0.05)


def queue_cs2_price_refresh(items):
    """Queue a bounded batch of missing/stale prices; duplicates are coalesced."""
    for item in items:
        if CS2_PRICE_QUEUE.qsize() >= 1000:
            break
        market_name = item.get("market_hash_name")
        if market_name and market_name not in CS2_PRICE_QUEUED:
            CS2_PRICE_QUEUED.add(market_name)
            CS2_PRICE_QUEUE.put_nowait(market_name)


async def cs2_price_catalog_warmup():
    """Warm all CS2 skin quotes in the background without delaying app startup."""
    try:
        catalog = await load_cs2_catalog("skins")
        names = [item["market_hash_name"] for item in catalog if item.get("market_hash_name")]
        async with db.acquire() as conn:
            rows = await conn.fetch("""
                SELECT market_hash_name FROM cs2_price_checks
                WHERE last_checked_at > NOW() - INTERVAL '12 hours'
                UNION
                SELECT market_hash_name FROM cs2_steam_prices
                WHERE fetched_at > NOW() - INTERVAL '12 hours'
                  AND lowest_price IS NOT NULL
            """)
        fresh = {row["market_hash_name"] for row in rows}
        pending = [name for name in names if name not in fresh]
        logger.info(
            "CS2 catalog price warmup: %s total skins, %s already fresh, %s queued",
            len(names), len(fresh), len(pending)
        )
        for offset in range(0, len(pending), 500):
            # Leave room for user-triggered refreshes and keep the worker queue bounded.
            while CS2_PRICE_QUEUE.qsize() > 500:
                await asyncio.sleep(0.5)
            queue_cs2_price_refresh(
                {"market_hash_name": name} for name in pending[offset:offset + 500]
            )
            await asyncio.sleep(0.25)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning("CS2 catalog price warmup failed", exc_info=True)


async def get_cs2_skins(request):
    catalog = await load_cs2_catalog("skins")
    query = request.query.get("q", "").strip().casefold()[:80]
    wear_filter = request.query.get("wear", "").strip().casefold()
    rarity_filter = request.query.get("rarity", "").strip().casefold()
    phase_filter = request.query.get("phase", "").strip().casefold()
    stattrak_only = request.query.get("stattrak") == "1"
    souvenir_only = request.query.get("souvenir") == "1"
    # No implicit price filter: return every matching CS2 skin variant.
    # Price bounds are applied only when the user explicitly enters them.
    min_raw = request.query.get("min_price", "").strip()
    max_raw = request.query.get("max_price", "").strip()
    min_price_minor = steam_price_to_minor(min_raw) if min_raw else None
    max_price_minor = steam_price_to_minor(max_raw) if max_raw else None
    if min_price_minor is not None:
        min_price_minor = max(200, min_price_minor)
    if max_price_minor is not None:
        max_price_minor = min(10000000, max_price_minor)
    if min_price_minor is not None and max_price_minor is not None and min_price_minor > max_price_minor:
        raise web.HTTPBadRequest(text="Минимальная цена не может быть выше максимальной")
    sort = request.query.get("sort", "name")
    try:
        page = max(0, min(int(request.query.get("page", "0")), 10000))
        limit = max(1, min(int(request.query.get("limit", "24")), 40))
    except ValueError:
        page, limit = 0, 48

    # Search is optional: with an empty query the UI can browse the entire
    # paginated CS2 catalog; no giant response is sent to the client.
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
            if (now_utc - row["fetched_at"]).total_seconds() <= CS2_PRICE_USABLE_TTL_SECONDS
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
            SELECT market_hash_name, lowest_price, median_price, volume, fetched_at,
                   price_source, image_url
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
            item["market_price_source"] = price["price_source"]
            item["market_price_updated_at"] = price["fetched_at"].isoformat()
            if not item.get("image_url") and price["image_url"]:
                item["image_url"] = price["image_url"]
            age_seconds = now - price["fetched_at"].timestamp()
            is_stale = age_seconds > CS2_PRICE_REFRESH_TTL_SECONDS
            is_expired = age_seconds > CS2_PRICE_USABLE_TTL_SECONDS
            item["market_price_minor"] = None if is_expired else steam_price_to_minor(price["lowest_price"])
            item["market_price_stale"] = bool(item["market_price_minor"] and is_stale)
            if item["market_price_minor"]:
                item["value_minor"] = item["market_price_minor"]
        else:
            item["steam_lowest_price"] = None
            item["steam_median_price"] = None
            item["steam_volume"] = None
            item["steam_fetched_at"] = None
            item["market_price_source"] = None
            item["market_price_updated_at"] = None
            item["market_price_minor"] = None
            item["market_price_stale"] = False
            is_stale = True
        if is_stale:
            stale.append(source)
        enriched.append(item)

    # Warm the visible page in one batch so unpriced targets become selectable
    # without requiring the user to click a disabled card or change sort order.
    price_pending_count = sum(1 for item in enriched if not item.get("market_price_minor"))
    queue_cs2_price_refresh(stale[:40])
    return web.json_response({
        "items": enriched,
        "total": total,
        "matched": total,
        "page": page,
        "limit": limit,
        "has_more": start + len(page_items) < total,
        "price_source": "Steam via OpenSkin (primary) / marketplace fallback",
        "price_currency": "RUB",
        "price_cache_hours": 168,
        "price_refresh_after_hours": 12,
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
                SELECT market_hash_name, lowest_price, median_price, volume, fetched_at,
                   price_source, image_url
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
                item["market_price_source"] = price["price_source"]
                item["market_price_updated_at"] = price["fetched_at"].isoformat()
                if not item.get("image_url") and price["image_url"]:
                    item["image_url"] = price["image_url"]
                market_minor = steam_price_to_minor(price["lowest_price"])
                if market_minor:
                    item["market_price_minor"] = market_minor
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
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="Некорректный ID кейса")

    async with db.acquire() as conn:
        async with conn.transaction():
            case = await conn.fetchrow(
                "SELECT id, title FROM cases WHERE id=$1 AND active=TRUE",
                case_id,
            )
            if not case:
                raise web.HTTPNotFound(text="Кейс не найден")

            # A daily demo claim grants a virtual catalog skin for practicing
            # target selection. It is not a Steam item and has no cash value.
            item = await conn.fetchrow("""
                SELECT name, rarity, image_url, value_minor, market_hash_name,
                       wear, stattrak, souvenir, phase
                FROM case_items
                WHERE case_id=$1 AND is_active=TRUE
                  AND market_hash_name IS NOT NULL
                ORDER BY COALESCE(value_minor, 1000), id
                LIMIT 1
            """, case_id)
            if not item:
                raise web.HTTPConflict(text="В этом кейсе пока нет предмета для тестового апгрейда")

            claimed = await conn.fetchval("""
                INSERT INTO demo_case_claims(user_id, claim_date)
                VALUES($1, CURRENT_DATE)
                ON CONFLICT DO NOTHING
                RETURNING user_id
            """, user["id"])
            if not claimed:
                raise web.HTTPConflict(text="Бесплатный тестовый скин уже получен сегодня")

            inventory_id = await conn.fetchval("""
                INSERT INTO user_inventory(
                    user_id, item_type, item_name, rarity, image_url,
                    value_minor, source, market_hash_name, wear,
                    stattrak, souvenir, phase
                )
                VALUES($1, 'skin', $2, $3, $4, $5, 'daily_demo',
                       $6, $7, $8, $9, $10)
                RETURNING id
            """, user["id"], item["name"], item["rarity"] or "Обычный",
                item["image_url"], int(item["value_minor"] or 1000),
                item["market_hash_name"], item["wear"], bool(item["stattrak"]),
                bool(item["souvenir"]), item["phase"])

    return web.json_response({
        "ok": True,
        "inventory_id": inventory_id,
        "item_name": item["name"],
        "message": "Тестовый виртуальный скин добавлен в инвентарь",
        "demo_only": True,
    })

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
            # Legacy inventory rows can contain the base skin label while the
            # canonical catalog stores wear-specific market hash names.
            def normalize_skin_label(value):
                label = str(value or "").strip().casefold()
                label = label.replace("stattrak™ ", "").replace("stattrak ", "")
                label = label.replace("souvenir ", "")
                if label.endswith(")") and " (" in label:
                    label = label.rsplit(" (", 1)[0].strip()
                return " ".join(label.split())
            normalized_source = normalize_skin_label(source_label)
            matches = [
                x for x in catalog
                if normalize_skin_label(x.get("name")) == normalized_source
                or normalize_skin_label(x.get("base_name")) == normalized_source
                or normalize_skin_label(x.get("market_hash_name")) == normalized_source
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
            (datetime.now(timezone.utc)-source_price["fetched_at"]).total_seconds() <= CS2_PRICE_USABLE_TTL_SECONDS)
        source_value = steam_price_to_minor(source_price["lowest_price"]) if fresh_source else None
        if source_value and (datetime.now(timezone.utc)-source_price["fetched_at"]).total_seconds() > CS2_PRICE_REFRESH_TTL_SECONDS:
            queue_cs2_price_refresh([{"market_hash_name": source_market_name}])
        if not source_value:
            if not source_market_name:
                raise web.HTTPBadRequest(text="Не удалось однозначно сопоставить этот предмет с каталогом CS2. Выберите другой скин.")
            # OpenSkin is the primary public price source and does not require API Skins credentials.
            # API Skins remains an optional fallback inside the price worker.
            # Fetch the source quote and a useful target sample in the SAME
            # marketplace batch. Previously these were requested sequentially,
            # costing users two complete API round trips before an X2 target appeared.
            provisional_source = int(source["value_minor"] or 0)
            provisional_desired = (
                provisional_source * raw_value if mode == "multiplier"
                else provisional_source * 75.0 / raw_value
            ) if provisional_source > 0 else 0
            ranked_targets = sorted(
                (x for x in catalog if x.get("market_hash_name") and x.get("market_hash_name") != source_market_name),
                key=lambda x: abs(cs2_virtual_value({"name": x.get("rarity", "")}) - provisional_desired) / max(provisional_desired, 1)
            ) if provisional_desired else []
            queue_cs2_price_refresh(
                [{"market_hash_name": source_market_name}] +
                [{"market_hash_name": x["market_hash_name"]} for x in ranked_targets[:499]]
            )
            return web.json_response({
                "pending": True,
                "stage": "source_and_target_prices",
                "message": "Одновременно получаем цену ставки и подходящих целей."
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
        fresh = (now-row["fetched_at"]).total_seconds() <= CS2_PRICE_USABLE_TTL_SECONDS
        if fresh and minor and 200 <= minor <= 10000000 and minor > source_value:
            prices[row["market_hash_name"]] = minor
        elif not fresh:
            stale_names.add(row["market_hash_name"])
    if stale_names:
        queue_cs2_price_refresh([{"market_hash_name": name} for name in list(stale_names)[:100]])
    candidates = [x for x in catalog if x.get("market_hash_name") in prices]
    if not candidates:
        # Continue with the keyless OpenSkin queue; API Skins is only an optional fallback.
        # Queue a useful sample around the requested price instead of the first
        # eight alphabetic entries; one batch can contain up to 100 names.
        ranked = sorted(
            (x for x in catalog if x.get("market_hash_name")),
            key=lambda x: abs(cs2_virtual_value({"name": x.get("rarity", "")}) - desired) / max(desired, 1)
        )
        queue_cs2_price_refresh(ranked[:500])
        return web.json_response({
            "pending": True,
            "stage": "target_prices",
            "message": "Подбираем предметы по актуальным ценам. Подождите несколько секунд."
        }, status=202)
    candidates.sort(key=lambda x: abs(prices[x["market_hash_name"]]-desired)/max(desired,1))
    chosen = dict(candidates[0])
    chosen["market_price_minor"] = prices[chosen["market_hash_name"]]
    chosen["market_price_stale"] = any(
        row["market_hash_name"] == chosen["market_hash_name"]
        and (now - row["fetched_at"]).total_seconds() > CS2_PRICE_REFRESH_TTL_SECONDS
        for row in rows
    )
    chosen["value_minor"] = chosen["market_price_minor"]
    chosen["suggested_multiplier"] = round(chosen["market_price_minor"]/source_value, 2)
    chosen["suggested_chance_percent"] = round(source_value/chosen["market_price_minor"]*75, 2)
    return web.json_response({"item": chosen, "source_value_minor": source_value,
        "desired_target_minor": round(desired), "mode": mode, "value": raw_value,
        "fresh_price_count": len(prices), "preview_only": True,
        "demo_only": False})


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
                    (datetime.now(timezone.utc) - source_price["fetched_at"]).total_seconds() <= CS2_PRICE_USABLE_TTL_SECONDS)
                source_market_minor = steam_price_to_minor(source_price["lowest_price"]) if source_fresh else None
                if source_market_minor and (datetime.now(timezone.utc)-source_price["fetched_at"]).total_seconds() > CS2_PRICE_REFRESH_TTL_SECONDS:
                    queue_cs2_price_refresh([{"market_hash_name": source_market_name}])
                if not source_market_minor:
                    queue_cs2_price_refresh([{"market_hash_name": source_market_name}])
                    return web.json_response({"pending": True, "message": "Обновляем цену исходного скина. Повторите через несколько секунд."}, status=202)
                source_value = source_market_minor
                target_price = await conn.fetchrow("""
                    SELECT lowest_price, fetched_at FROM cs2_steam_prices
                    WHERE market_hash_name=$1 AND price_source <> 'steam'
                """, target["market_hash_name"])
                target_fresh = bool(target_price and
                    (datetime.now(timezone.utc) - target_price["fetched_at"]).total_seconds() <= CS2_PRICE_USABLE_TTL_SECONDS)
                target_market_minor = steam_price_to_minor(target_price["lowest_price"]) if target_fresh else None
                if target_market_minor and (datetime.now(timezone.utc)-target_price["fetched_at"]).total_seconds() > CS2_PRICE_REFRESH_TTL_SECONDS:
                    queue_cs2_price_refresh([{"market_hash_name": target["market_hash_name"]}])
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
        "target": target, "preview_only": True, "demo_only": False,
        "message": "Успех! Предмет добавлен в инвентарь." if success else "Неудача. Виртуальный предмет ставки списан.",
    })

async def get_live_drops(request):
    """Public, anonymized feed of real demo case openings and upgrader results."""
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT event_id, user_id, event_type, title, rarity, image_url,
                   value_minor, context, success, created_at
            FROM (
                SELECT
                    'case:' || co.id::text AS event_id,
                    co.user_id,
                    'case'::text AS event_type,
                    co.item_name AS title,
                    co.rarity,
                    ci.image_url,
                    COALESCE(ci.value_minor, 0)::BIGINT AS value_minor,
                    c.title AS context,
                    NULL::BOOLEAN AS success,
                    co.created_at
                FROM case_openings co
                JOIN cases c ON c.id = co.case_id
                LEFT JOIN case_items ci ON ci.id = co.item_id

                UNION ALL

                SELECT
                    'upgrade:' || ua.id::text AS event_id,
                    ua.user_id,
                    'upgrade'::text AS event_type,
                    ua.target_name AS title,
                    NULL::TEXT AS rarity,
                    ua.target_image_url AS image_url,
                    ua.target_value_minor AS value_minor,
                    ua.source_name AS context,
                    ua.success,
                    ua.created_at
                FROM upgrade_attempts ua

                UNION ALL

                SELECT
                    'creator_case:' || co.id::text AS event_id,
                    co.user_id,
                    'case'::text AS event_type,
                    co.item_name AS title,
                    co.rarity,
                    ci.image_url,
                    COALESCE(ci.value_minor, 0)::BIGINT AS value_minor,
                    c.title AS context,
                    NULL::BOOLEAN AS success,
                    co.created_at
                FROM creator_case_openings co
                JOIN creator_cases c ON c.id=co.creator_case_id
                LEFT JOIN creator_case_items ci ON ci.id=co.item_id
            ) events
            ORDER BY created_at DESC
            LIMIT 40
        """)
    items = []
    for row in rows:
        user_suffix = str(row["user_id"])[-3:].rjust(3, "0")
        items.append({
            "id": row["event_id"],
            "player": "Игрок • " + user_suffix,
            "type": row["event_type"],
            "title": row["title"],
            "rarity": row["rarity"],
            "image_url": row["image_url"],
            "value_minor": row["value_minor"],
            "context": row["context"],
            "success": row["success"],
            "created_at": row["created_at"].isoformat(),
            "demo_only": True,
        })
    return web.json_response({"items": items, "demo_only": True})

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


async def create_steam_auth_url(user_id: int) -> str:
    if not PUBLIC_BASE_URL:
        raise RuntimeError("PUBLIC_BASE_URL не настроен")
    state = secrets.token_urlsafe(32)
    callback_url = f"{PUBLIC_BASE_URL}/api/steam/callback?state={state}"
    params = {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": callback_url,
        "openid.realm": f"{PUBLIC_BASE_URL}/",
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }
    async with db.acquire() as conn:
        await conn.execute("""
            DELETE FROM steam_auth_states
            WHERE created_at < NOW() - INTERVAL '30 minutes'
               OR consumed_at IS NOT NULL
        """)
        await conn.execute("""
            INSERT INTO steam_auth_states(state,user_id)
            VALUES($1,$2)
        """, state, user_id)
    return "https://steamcommunity.com/openid/login?" + urlencode(params)


async def start_steam_auth(request):
    user = verify_telegram_user(request)
    auth_url = await create_steam_auth_url(user["id"])
    return web.json_response({"ok": True, "auth_url": auth_url})


async def steam_auth_callback(request):
    state = request.query.get("state", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,64}", state):
        raise web.HTTPBadRequest(text="Ссылка авторизации недействительна. Вернитесь в ShirpDrop и попробуйте ещё раз.")

    identity = request.query.get("openid.claimed_id", "")
    steam_match = re.fullmatch(r"https://steamcommunity.com/openid/id/(\d{17})", identity)
    if not steam_match:
        raise web.HTTPUnauthorized(text="Steam не вернул корректный SteamID64.")

    expected_return_to = f"{PUBLIC_BASE_URL}/api/steam/callback?state={state}"
    if request.query.get("openid.return_to") != expected_return_to:
        raise web.HTTPUnauthorized(text="Не совпадает адрес возврата Steam OpenID.")
    if request.query.get("openid.identity") != identity:
        raise web.HTTPUnauthorized(text="Steam identity не совпадает с claimed_id.")
    if request.query.get("openid.op_endpoint") != "https://steamcommunity.com/openid/login":
        raise web.HTTPUnauthorized(text="Неизвестный OpenID endpoint.")

    verification = {
        key: value for key, value in request.query.items()
        if key.startswith("openid.")
    }
    verification["openid.mode"] = "check_authentication"
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                "https://steamcommunity.com/openid/login",
                data=verification,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            ) as response:
                body = await response.text()
                if response.status != 200 or not re.search(r"(?m)^is_valid:true\s*$", body):
                    raise web.HTTPUnauthorized(text="Steam не подтвердил подлинность авторизации.")
    except web.HTTPException:
        raise
    except Exception:
        logger.exception("Steam OpenID verification failed")
        raise web.HTTPBadGateway(text="Не удалось проверить ответ Steam. Попробуйте позже.")

    steam_id64 = steam_match.group(1)
    async with db.acquire() as conn:
        async with conn.transaction():
            auth = await conn.fetchrow("""
                UPDATE steam_auth_states
                SET consumed_at=NOW()
                WHERE state=$1 AND consumed_at IS NULL
                  AND created_at > NOW() - INTERVAL '10 minutes'
                RETURNING user_id
            """, state)
            if not auth:
                raise web.HTTPUnauthorized(text="Ссылка авторизации истекла или уже использована.")
            try:
                await conn.execute("""
                    INSERT INTO steam_identities(user_id,steam_id64,verified_at)
                    VALUES($1,$2,NOW())
                    ON CONFLICT(user_id) DO UPDATE SET
                        steam_id64=EXCLUDED.steam_id64,
                        verified_at=NOW(),
                        updated_at=NOW()
                """, auth["user_id"], steam_id64)
            except asyncpg.UniqueViolationError:
                raise web.HTTPConflict(text="Этот Steam-аккаунт уже привязан к другому пользователю ShirpDrop.")
            await conn.execute("""
                INSERT INTO audit_log(actor_id,action,details)
                VALUES($1,'steam_account_linked',$2::jsonb)
            """, auth["user_id"], json.dumps({"steam_id64": steam_id64}))
    try:
        await bot.send_message(
            auth["user_id"],
            f"✅ <b>Steam успешно привязан!</b>\\n\\nSteamID64: <code>{steam_id64}</code>\\nТеперь аккаунт связан с вашим профилем ShirpDrop.",
            parse_mode="HTML",
        )
    except Exception:
        logger.exception("Could not notify Telegram user about Steam link")
    return web.Response(
        text="<html lang='ru'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Steam подключён</title><body style='background:#101010;color:#f5e7c2;font:16px sans-serif;text-align:center;padding:48px 20px'><h2>Steam успешно привязан</h2><p>Вернитесь в Telegram — бот уже отправил подтверждение.</p></body></html>",
        content_type="text/html",
    )


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
        steam = await conn.fetchrow("""
            SELECT steam_id64, verified_at FROM steam_identities WHERE user_id=$1
        """, user["id"])
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
        "steam": {"connected": bool(steam), "steam_id64": steam["steam_id64"] if steam else None, "verified_at": steam["verified_at"].isoformat() if steam and steam["verified_at"] else None},
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
        item["amount"] = int(item["amount_minor"] or 0) / 100
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


async def admin_get_dashboard(request):
    require_admin(request)
    async with db.acquire() as conn:
        totals = await conn.fetchrow("""
            SELECT COUNT(*)::BIGINT AS total_orders,
                   COUNT(*) FILTER (WHERE status IN ('new','invoice_sent'))::BIGINT AS pending_orders,
                   COUNT(*) FILTER (WHERE status IN ('paid','delivered'))::BIGINT AS paid_orders
            FROM orders
        """)
        turnover = await conn.fetch("""
            SELECT currency, COALESCE(SUM(amount_minor), 0)::BIGINT AS amount_minor
            FROM orders
            WHERE status IN ('paid','delivered')
            GROUP BY currency ORDER BY currency
        """)
    return web.json_response({
        "total_orders": totals["total_orders"],
        "pending_orders": totals["pending_orders"],
        "paid_orders": totals["paid_orders"],
        "turnover": [{"currency": r["currency"], "amount_minor": r["amount_minor"]} for r in turnover],
    })


async def admin_get_products(request):
    require_admin(request)
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT id, title, description, price_minor, currency, active, created_at
            FROM products ORDER BY id DESC LIMIT 200
        """)
    return web.json_response({"items": [{
        "id": r["id"], "title": r["title"], "description": r["description"],
        "price_minor": r["price_minor"], "currency": r["currency"],
        "active": r["active"], "created_at": r["created_at"].isoformat(),
    } for r in rows]})


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
# FULL ADMIN EDITOR: PRODUCTS, CASES, CASE CONTENTS AND USERS
# ============================================================

def admin_json_body(request):
    try:
        return request._cached_json if request.has_body else {}
    except Exception:
        return {}

async def admin_update_product(request):
    user = require_admin(request)
    try:
        product_id = int(request.match_info["product_id"])
        data = await request.json()
        title = str(data.get("title", "")).strip()
        description = str(data.get("description", "")).strip()
        currency = str(data.get("currency", "RUB")).upper()
        price_minor = price_to_minor(data.get("price"))
        active = bool(data.get("active", True))
    except (ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Проверьте название, цену и валюту")
    if not title or len(title) > 120 or len(description) > 1500:
        raise web.HTTPBadRequest(text="Название обязательно; описание до 1500 символов")
    if currency not in {"RUB", "EUR", "USD"}:
        raise web.HTTPBadRequest(text="Недопустимая валюта")
    async with db.acquire() as conn:
        row = await conn.fetchrow("""
            UPDATE products SET title=$1, description=$2, price_minor=$3,
                currency=$4, active=$5 WHERE id=$6 RETURNING id
        """, title, description, price_minor, currency, active, product_id)
    if not row:
        raise web.HTTPNotFound(text="Товар не найден")
    await write_audit(user["id"], "product_updated", {"product_id": product_id})
    return web.json_response({"ok": True})

async def admin_get_cases(request):
    require_admin(request)
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT c.id,c.title,c.description,c.price_minor,c.reference_price_minor,c.currency,c.active,
                   c.image_url,c.created_at,
                   COUNT(ci.id) FILTER (WHERE ci.is_active)::INTEGER AS active_items,
                   COUNT(ci.id)::INTEGER AS total_items
            FROM cases c LEFT JOIN case_items ci ON ci.case_id=c.id
            GROUP BY c.id ORDER BY c.id DESC
        """)
    return web.json_response({"items":[{
        "id":r["id"],"title":r["title"],"description":r["description"],
        "price_minor":r["price_minor"],"reference_price_minor":r["reference_price_minor"],"currency":r["currency"],
        "active":r["active"],"image_url":r["image_url"],
        "active_items":r["active_items"],"total_items":r["total_items"],
        "created_at":r["created_at"].isoformat()
    } for r in rows]})

async def admin_save_case(request):
    user = require_admin(request)
    try:
        data = await request.json()
        case_id = int(request.match_info["case_id"]) if request.match_info.get("case_id") else None
        title = str(data.get("title","")).strip()[:120]
        description = str(data.get("description","")).strip()[:1500]
        currency = str(data.get("currency","RUB")).upper()
        raw_price = Decimal(str(data.get("price","0")).replace(",","."))
        price_minor = int(raw_price * 100)
        raw_reference_price = Decimal(str(data.get("reference_price","0")).replace(",","."))
        reference_price_minor = int(raw_reference_price * 100)
        image_url = str(data.get("image_url","")).strip()[:1000] or None
        active = bool(data.get("active", True))
    except (ValueError, TypeError, InvalidOperation, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Проверьте данные кейса")
    if not title or currency not in {"RUB","EUR","USD"} or price_minor < 0 or reference_price_minor < 0:
        raise web.HTTPBadRequest(text="Название обязательно, цена не может быть отрицательной")
    if image_url and not image_url.startswith(("https://","http://")):
        raise web.HTTPBadRequest(text="Ссылка на изображение должна начинаться с http:// или https://")
    async with db.acquire() as conn:
        if case_id:
            row = await conn.fetchrow("""
                UPDATE cases SET title=$1,description=$2,price_minor=$3,
                    reference_price_minor=$4,currency=$5,image_url=$6,active=$7 WHERE id=$8 RETURNING id
            """,title,description,price_minor,reference_price_minor,currency,image_url,active,case_id)
            if not row: raise web.HTTPNotFound(text="Кейс не найден")
            saved_id=case_id
        else:
            saved_id = await conn.fetchval("""
                INSERT INTO cases(title,description,price_minor,reference_price_minor,currency,image_url,active)
                VALUES($1,$2,$3,$4,$5,$6,$7) RETURNING id
            """,title,description,price_minor,reference_price_minor,currency,image_url,active)
    await write_audit(user["id"],"case_saved",{"case_id":saved_id})
    return web.json_response({"ok":True,"case_id":saved_id})

async def admin_toggle_case(request):
    user=require_admin(request)
    try: case_id=int(request.match_info["case_id"])
    except ValueError: raise web.HTTPBadRequest(text="Некорректный ID кейса")
    async with db.acquire() as conn:
        active=await conn.fetchval("UPDATE cases SET active=NOT active WHERE id=$1 RETURNING active",case_id)
    if active is None: raise web.HTTPNotFound(text="Кейс не найден")
    await write_audit(user["id"],"case_toggled",{"case_id":case_id,"active":active})
    return web.json_response({"ok":True,"active":active})

async def admin_get_case_items(request):
    require_admin(request)
    try: case_id=int(request.match_info["case_id"])
    except ValueError: raise web.HTTPBadRequest(text="Некорректный ID кейса")
    async with db.acquire() as conn:
        rows=await conn.fetch("""
            SELECT id,case_id,name,rarity,weight,image_url,value_minor,
                   market_hash_name,wear,stattrak,souvenir,phase,is_active
            FROM case_items WHERE case_id=$1 ORDER BY id
        """,case_id)
    return web.json_response({"items":[dict(r) for r in rows]})

async def admin_save_case_item(request):
    user=require_admin(request)
    try:
        data=await request.json()
        case_id=int(request.match_info["case_id"])
        item_id=int(request.match_info["item_id"]) if request.match_info.get("item_id") else None
        name=str(data.get("name","")).strip()[:180]
        rarity=str(data.get("rarity","Обычный")).strip()[:80]
        weight=int(data.get("weight",1))
        value_minor=int(Decimal(str(data.get("price","0")).replace(",", "."))*100)
        image_url=str(data.get("image_url","")).strip()[:1000] or None
        market_hash_name=str(data.get("market_hash_name","")).strip()[:220] or None
        wear=str(data.get("wear","")).strip()[:80] or None
        phase=str(data.get("phase","")).strip()[:80] or None
        stattrak=bool(data.get("stattrak",False))
        souvenir=bool(data.get("souvenir",False))
        active=bool(data.get("active",True))
    except (ValueError,TypeError,InvalidOperation,json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Проверьте поля предмета")
    if not name or weight < 1 or weight > 1000000000 or value_minor < 0:
        raise web.HTTPBadRequest(text="Название обязательно; вес должен быть положительным")
    if image_url and not image_url.startswith(("https://","http://")):
        raise web.HTTPBadRequest(text="Ссылка на изображение должна начинаться с http:// или https://")
    async with db.acquire() as conn:
        exists=await conn.fetchval("SELECT id FROM cases WHERE id=$1",case_id)
        if not exists: raise web.HTTPNotFound(text="Кейс не найден")
        if item_id:
            row=await conn.fetchrow("""
                UPDATE case_items SET name=$1,rarity=$2,weight=$3,image_url=$4,
                    value_minor=$5,market_hash_name=$6,wear=$7,stattrak=$8,
                    souvenir=$9,phase=$10,is_active=$11
                WHERE id=$12 AND case_id=$13 RETURNING id
            """,name,rarity,weight,image_url,value_minor,market_hash_name,wear,
                stattrak,souvenir,phase,active,item_id,case_id)
            if not row: raise web.HTTPNotFound(text="Предмет кейса не найден")
            saved_id=item_id
        else:
            saved_id=await conn.fetchval("""
                INSERT INTO case_items(case_id,name,rarity,weight,image_url,value_minor,
                    market_hash_name,wear,stattrak,souvenir,phase,is_active)
                VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) RETURNING id
            """,case_id,name,rarity,weight,image_url,value_minor,market_hash_name,
                wear,stattrak,souvenir,phase,active)
    await write_audit(user["id"],"case_item_saved",{"case_id":case_id,"item_id":saved_id})
    return web.json_response({"ok":True,"item_id":saved_id})

async def admin_toggle_case_item(request):
    user=require_admin(request)
    try:
        case_id=int(request.match_info["case_id"]); item_id=int(request.match_info["item_id"])
    except ValueError: raise web.HTTPBadRequest(text="Некорректный ID")
    async with db.acquire() as conn:
        active=await conn.fetchval("""
            UPDATE case_items SET is_active=NOT is_active
            WHERE id=$1 AND case_id=$2 RETURNING is_active
        """,item_id,case_id)
    if active is None: raise web.HTTPNotFound(text="Предмет не найден")
    await write_audit(user["id"],"case_item_toggled",{"case_id":case_id,"item_id":item_id,"active":active})
    return web.json_response({"ok":True,"active":active})

async def admin_get_wallet_users(request):
    require_admin(request)
    q = str(request.query.get("q", "")).strip()[:100]
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            WITH known_users AS (
                SELECT user_id FROM wallet_accounts
                UNION SELECT user_id FROM orders
                UNION SELECT user_id FROM user_inventory
                UNION SELECT user_id FROM customer_profiles
            )
            SELECT u.user_id, COALESCE(w.balance_minor, 0)::BIGINT AS balance_minor,
                   COALESCE(w.updated_at, NOW()) AS updated_at,
                   (SELECT COUNT(*) FROM orders o WHERE o.user_id=u.user_id) AS orders_count
            FROM known_users u
            LEFT JOIN wallet_accounts w ON w.user_id=u.user_id
            WHERE ($1='' OR u.user_id::text ILIKE '%'||$1||'%')
            ORDER BY COALESCE(w.updated_at, NOW()) DESC
            LIMIT 100
        """, q)
    return web.json_response({"items":[{
        "user_id":r["user_id"],"balance_minor":r["balance_minor"],
        "orders_count":r["orders_count"],"updated_at":r["updated_at"].isoformat()
    } for r in rows]})


async def admin_adjust_wallet(request):
    admin=require_admin(request)
    try:
        user_id=int(request.match_info["user_id"])
        data=await request.json()
        delta=int(Decimal(str(data.get("amount","0")).replace(",", "."))*100)
        reason=str(data.get("reason","Ручная корректировка администратором")).strip()[:250]
    except (ValueError,TypeError,InvalidOperation,json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Укажите пользователя и сумму")
    if delta == 0 or abs(delta) > 100000000:
        raise web.HTTPBadRequest(text="Сумма должна быть ненулевой и не превышать 1 000 000 ₽")
    async with db.acquire() as conn:
        known_user = await conn.fetchval("""
            SELECT EXISTS(SELECT 1 FROM orders WHERE user_id=$1)
                OR EXISTS(SELECT 1 FROM user_inventory WHERE user_id=$1)
                OR EXISTS(SELECT 1 FROM customer_profiles WHERE user_id=$1)
                OR EXISTS(SELECT 1 FROM wallet_accounts WHERE user_id=$1)
        """, user_id)
        if not known_user:
            raise web.HTTPNotFound(text="Пользователь не найден")
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO wallet_accounts(user_id,balance_minor)
                VALUES($1,0) ON CONFLICT(user_id) DO NOTHING
            """,user_id)
            row=await conn.fetchrow("""
                UPDATE wallet_accounts SET balance_minor=balance_minor+$1,updated_at=NOW()
                WHERE user_id=$2 AND balance_minor+$1>=0
                RETURNING balance_minor
            """,delta,user_id)
            if not row: raise web.HTTPBadRequest(text="Недостаточно средств для списания")
            await conn.execute("""
                INSERT INTO admin_wallet_adjustments(user_id,admin_id,delta_minor,
                    balance_after_minor,reason) VALUES($1,$2,$3,$4,$5)
            """,user_id,admin["id"],delta,row["balance_minor"],reason)
            await conn.execute("""
                INSERT INTO audit_log(actor_id,action,details)
                VALUES($1,'wallet_manually_adjusted',$2::jsonb)
            """,admin["id"],json.dumps({"user_id":user_id,"delta_minor":delta,"balance_after_minor":row["balance_minor"],"reason":reason}))
    return web.json_response({"ok":True,"balance_minor":row["balance_minor"]})




# ============================================================
# FINANCIAL MOVEMENT REPORTS
# ============================================================

async def admin_finance_rows(request):
    require_admin(request)
    mode = request.headers.get("X-Store-Mode", "digital").strip().lower()
    if mode not in {"digital", "cases"}:
        raise web.HTTPBadRequest(text="Неизвестный магазин")
    date_from = request.query.get("from", "").strip()
    date_to = request.query.get("to", "").strip()
    user_q = request.query.get("user", "").strip()[:40]
    kind = request.query.get("type", "").strip()[:40]
    try:
        from_dt = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc) if date_from else None
        to_dt = datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc) + timedelta(days=1) if date_to else None
        user_id = int(user_q) if user_q else None
    except ValueError:
        raise web.HTTPBadRequest(text="Некорректный фильтр даты или Telegram ID")

    if mode == "digital":
        sql = """
            SELECT 'order'::text AS event_type, o.id::text AS ref,
                   o.user_id, COALESCE(o.username,'') AS username,
                   CASE WHEN o.status IN ('paid','delivered') THEN 'Покупка подтверждена'
                        WHEN o.status='refunded' THEN 'Возврат'
                        WHEN o.status='cancelled' THEN 'Заказ отменён'
                        ELSE 'Заказ создан — ожидает оплаты' END AS title,
                   CASE WHEN o.status='refunded' THEN -o.amount_minor
                        WHEN o.status IN ('paid','delivered') THEN o.amount_minor ELSE 0 END::bigint AS amount_minor,
                   o.currency, o.status AS status,
                   CASE WHEN o.status='refunded' THEN 'out'
                        WHEN o.status IN ('paid','delivered') THEN 'in' ELSE 'info' END AS direction,
                   o.updated_at AS created_at,
                   'Заказ #' || o.id || ': ' || o.product_title AS details,
                   FALSE AS demo
            FROM orders o
            WHERE ($1::timestamptz IS NULL OR o.updated_at >= $1)
              AND ($2::timestamptz IS NULL OR o.updated_at < $2)
              AND ($3::bigint IS NULL OR o.user_id=$3)
              AND ($4::text='' OR 'order'=$4)
            UNION ALL
            SELECT 'payment'::text, pt.id::text, pt.user_id, ''::text,
                   'Платёжная транзакция DEMO', 0::bigint, pt.currency,
                   pt.status, 'info', pt.updated_at,
                   'Транзакция #' || pt.id || ' · заказ #' || pt.order_id || ' · ' || pt.external_reference,
                   TRUE
            FROM payment_transactions pt
            WHERE ($1::timestamptz IS NULL OR pt.updated_at >= $1)
              AND ($2::timestamptz IS NULL OR pt.updated_at < $2)
              AND ($3::bigint IS NULL OR pt.user_id=$3)
              AND ($4::text='' OR 'payment'=$4)
            ORDER BY created_at DESC LIMIT 2000
        """
    else:
        sql = """
            SELECT 'skin_sale'::text AS event_type, wl.id::text AS ref,
                   wl.user_id, ''::text AS username,
                   'Продажа скина — виртуальное зачисление' AS title,
                   wl.amount_minor::bigint AS amount_minor, wl.currency,
                   'completed'::text AS status, 'in'::text AS direction,
                   wl.created_at, 'Инвентарь #' || wl.inventory_id AS details,
                   TRUE AS demo
            FROM wallet_ledger wl
            WHERE ($1::timestamptz IS NULL OR wl.created_at >= $1)
              AND ($2::timestamptz IS NULL OR wl.created_at < $2)
              AND ($3::bigint IS NULL OR wl.user_id=$3)
              AND ($4::text='' OR 'skin_sale'=$4)
            UNION ALL
            SELECT 'adjustment', aw.id::text, aw.user_id, '',
                   CASE WHEN aw.delta_minor>=0 THEN 'Ручное пополнение администратором'
                        ELSE 'Ручное списание администратором' END,
                   aw.delta_minor, 'RUB',
                   'completed', CASE WHEN aw.delta_minor>=0 THEN 'in' ELSE 'out' END,
                   aw.created_at,
                   'Администратор #' || aw.admin_id || ' · ' || aw.reason,
                   TRUE
            FROM admin_wallet_adjustments aw
            WHERE ($1::timestamptz IS NULL OR aw.created_at >= $1)
              AND ($2::timestamptz IS NULL OR aw.created_at < $2)
              AND ($3::bigint IS NULL OR aw.user_id=$3)
              AND ($4::text='' OR 'adjustment'=$4)
            ORDER BY created_at DESC LIMIT 2000
        """
    async with db.acquire() as conn:
        rows = await conn.fetch(sql, from_dt, to_dt, user_id, kind)
    items = []
    for row in rows:
        item = dict(row)
        item["created_at"] = row["created_at"].isoformat()
        items.append(item)
    return items


async def admin_get_finance(request):
    items = await admin_finance_rows(request)
    incoming = {}
    outgoing = {}
    for item in items:
        currency = item["currency"]
        if item["direction"] == "in":
            incoming[currency] = incoming.get(currency, 0) + item["amount_minor"]
        elif item["direction"] == "out":
            outgoing[currency] = outgoing.get(currency, 0) + abs(item["amount_minor"])
    return web.json_response({
        "items": items,
        "count": len(items),
        "incoming": incoming,
        "outgoing": outgoing,
        "store": request.headers.get("X-Store-Mode", "digital"),
        "note": "DEMO и виртуальные операции не являются реальными денежными платежами."
    })


async def admin_export_finance(request):
    items = await admin_finance_rows(request)
    fmt = request.query.get("format", "xlsx").lower()
    columns = [
        ("Дата и время", "created_at"), ("Тип", "title"),
        ("Telegram ID", "user_id"), ("Username", "username"),
        ("Сумма", "amount"),
        ("Валюта", "currency"), ("Направление", "direction"),
        ("Статус", "status"), ("Источник", "details"),
        ("DEMO / виртуально", "demo"), ("ID записи", "ref")
    ]
    if fmt in {"csv", "txt"}:
        import csv
        import io
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter=";" if fmt == "csv" else "\\t")
        writer.writerow([x[0] for x in columns])
        for item in items:
            writer.writerow([item.get(key, "") for _, key in columns])
        body = output.getvalue().encode("utf-8-sig")
        content_type = "text/csv; charset=utf-8" if fmt == "csv" else "text/plain; charset=utf-8"
        filename = "finance_report.csv" if fmt == "csv" else "finance_report.txt"
    elif fmt == "xlsx":
        try:
            from openpyxl import Workbook
            from openpyxl.styles import Font, PatternFill
            from openpyxl.utils import get_column_letter
        except ImportError:
            raise web.HTTPServiceUnavailable(text="Экспорт XLSX временно недоступен")
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Движение средств"
        sheet.append([x[0] for x in columns])
        for cell in sheet[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="262626")
        for item in items:
            sheet.append([item.get(key, "") for _, key in columns])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for col in sheet.columns:
            width = min(48, max(12, max(len(str(cell.value or "")) for cell in col) + 2))
            sheet.column_dimensions[get_column_letter(col[0].column)].width = width
        import io
        output = io.BytesIO()
        workbook.save(output)
        body = output.getvalue()
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        filename = "finance_report.xlsx"
    else:
        raise web.HTTPBadRequest(text="Формат должен быть xlsx, csv или txt")
    return web.Response(
        body=body, content_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


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

    # Give the administrator a private Telegram chat menu button for the admin panel.
    # The global menu button remains the public store for all customers.
    if is_admin(message.from_user.id):
        await bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonWebApp(
                text="Админ-панель",
                web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/admin"),
            ),
        )

    buttons = [[
        InlineKeyboardButton(
            text="🛍 Открыть магазин",
            web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/"),
        )
    ], [
        InlineKeyboardButton(
            text="📄 Пользовательское соглашение",
            url="https://telegra.ph/POLZOVATELSKOE-SOGLASHENIE-11-27-36",
        )
    ], [
        InlineKeyboardButton(
            text="🔒 Политика конфиденциальности",
            url="https://telegra.ph/Politika-konfidencialnosti-11-27-110",
        )
    ]]
    if is_admin(message.from_user.id):
        buttons.append([
            InlineKeyboardButton(
                text="⚙️ Админ-панель",
                web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/admin"),
            )
        ])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

    await message.answer(
        "Добро пожаловать в QR BOT PRO!\n\n"
        "Здесь вы можете посмотреть каталог цифровых товаров и услуг, оформить заказ, отслеживать его статус\n"
        "Для начала нажмите кнопку ниже",
        reply_markup=keyboard,
    )


@cases_dp.message(Command("start"))
async def cmd_cases_start(message: types.Message):
    if not cases_bot or not CASES_APP_URL:
        await message.answer("QR CASES ещё не настроен. Администратор подключит отдельного бота.")
        return

    if is_admin(message.from_user.id):
        await message.bot.set_chat_menu_button(
            chat_id=message.chat.id,
            menu_button=MenuButtonWebApp(
                text="Админ-панель CS2",
                web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/admin?store=cases"),
            ),
        )

    buttons = [[
        InlineKeyboardButton(
            text="🎁 Открыть CS2-кейсы",
            web_app=WebAppInfo(url=CASES_APP_URL),
        )
    ]]
    if is_admin(message.from_user.id):
        buttons.append([
            InlineKeyboardButton(
                text="⚙️ Управление кейсами",
                web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/admin?store=cases"),
            )
        ])

    await message.answer(
        "Добро пожаловать в QR CASES!\n\n"
        "Отдельный магазин CS2: кейсы, апгрейд, инвентарь, авторские кейсы и лента выпадений.\n"
        "Авторизация Steam и приватный доступ будут добавлены отдельным этапом.\n\n"
        "Нажмите кнопку ниже, чтобы открыть магазин.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@cases_dp.message(Command("help"))
async def cmd_cases_help(message: types.Message):
    await message.answer(
        "QR CASES — отдельный магазин CS2.\n"
        "/start — открыть кейсы\n"
        "/help — помощь"
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
# CASER-INSPIRED CREATOR CASES, FAVORITES AND WEEKLY REWARDS
# Demo-only: no real payments, cashout or Steam item transfers.
# ============================================================

async def get_creator_cases(request):
    user = verify_telegram_user(request)
    mine_only = request.query.get("mine") == "1"
    favorites_only = request.query.get("favorites") == "1"
    async with db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT c.id, c.user_id, c.title, c.description, c.image_url,
                   c.created_at,
                   COUNT(ci.id)::INTEGER AS items_count,
                   COALESCE(SUM(ci.value_minor * ci.chance_bp)::BIGINT / 10000, 0)::BIGINT AS estimated_value_minor,
                   EXISTS(SELECT 1 FROM creator_case_favorites f
                          WHERE f.creator_case_id=c.id AND f.user_id=$1) AS is_favorite
            FROM creator_cases c
            LEFT JOIN creator_case_items ci ON ci.creator_case_id=c.id
            WHERE c.active=TRUE
              AND ($2::BOOLEAN=FALSE OR c.user_id=$1)
              AND ($3::BOOLEAN=FALSE OR EXISTS(
                  SELECT 1 FROM creator_case_favorites f
                  WHERE f.creator_case_id=c.id AND f.user_id=$1
              ))
            GROUP BY c.id
            ORDER BY c.created_at DESC
            LIMIT 100
        """, user["id"], mine_only, favorites_only)
        items = []
        for row in rows:
            contents = await conn.fetch("""
                SELECT item_name, rarity, chance_bp, image_url, value_minor,
                       wear, stattrak, souvenir, phase
                FROM creator_case_items
                WHERE creator_case_id=$1
                ORDER BY chance_bp DESC, id
            """, row["id"])
            items.append({
                "id": row["id"], "title": row["title"],
                "description": row["description"], "image_url": row["image_url"],
                "creator": "Вы" if row["user_id"] == user["id"] else "Игрок",
                "is_mine": row["user_id"] == user["id"],
                "is_favorite": row["is_favorite"],
                "items_count": row["items_count"],
                "estimated_value_minor": row["estimated_value_minor"],
                "created_at": row["created_at"].isoformat(),
                "items": [{
                    "name": x["item_name"], "rarity": x["rarity"],
                    "chance_percent": round(x["chance_bp"] / 100, 2),
                    "image_url": x["image_url"], "value_minor": x["value_minor"],
                    "wear": x["wear"], "stattrak": x["stattrak"],
                    "souvenir": x["souvenir"], "phase": x["phase"]
                } for x in contents]
            })
    return web.json_response({"items": items, "demo_only": True})


async def create_creator_case(request):
    user = verify_telegram_user(request)
    try:
        data = await request.json()
        title = str(data.get("title", "")).strip()[:48]
        description = str(data.get("description", "")).strip()[:240]
        raw_items = data.get("items")
        if not title or not isinstance(raw_items, list) or not 2 <= len(raw_items) <= 30:
            raise ValueError()
        items = []
        seen = set()
        total_bp = 0
        for raw in raw_items:
            market_name = str(raw.get("market_hash_name", "")).strip()
            chance_bp = int(raw.get("chance_bp", 0))
            if not market_name or market_name in seen or not 1 <= chance_bp <= 10000:
                raise ValueError()
            seen.add(market_name)
            total_bp += chance_bp
            items.append((market_name, chance_bp))
        if total_bp != 10000:
            raise ValueError()
    except (ValueError, TypeError, json.JSONDecodeError, AttributeError):
        raise web.HTTPBadRequest(text="Укажите название, от 2 до 30 разных скинов и вероятности в сумме ровно 100%.")
    catalog = await load_cs2_catalog("skins")
    by_market = {x.get("market_hash_name"): x for x in catalog}
    if any(name not in by_market for name, _ in items):
        raise web.HTTPBadRequest(text="Один из скинов отсутствует в каталоге CS2. Обновите список и попробуйте снова.")
    async with db.acquire() as conn:
        async with conn.transaction():
            case_id = await conn.fetchval("""
                INSERT INTO creator_cases(user_id, title, description, image_url)
                VALUES($1,$2,$3,$4) RETURNING id
            """, user["id"], title, description,
                 by_market[items[0][0]].get("image_url"))
            names = [name for name, _ in items]
            prices = await conn.fetch("""
                SELECT market_hash_name, lowest_price, fetched_at
                FROM cs2_steam_prices
                WHERE market_hash_name=ANY($1::text[]) AND price_source <> 'steam'
            """, names)
            price_map = {
                x["market_hash_name"]: steam_price_to_minor(x["lowest_price"])
                for x in prices
                if (datetime.now(timezone.utc)-x["fetched_at"]).total_seconds() <= CS2_PRICE_USABLE_TTL_SECONDS
            }
            records = []
            for market_name, chance_bp in items:
                skin = by_market[market_name]
                records.append((
                    case_id, market_name, skin["name"], skin.get("rarity") or "Обычный",
                    chance_bp, skin.get("image_url"), price_map.get(market_name) or 0,
                    skin.get("wear"), bool(skin.get("stattrak")),
                    bool(skin.get("souvenir")), skin.get("phase")
                ))
            await conn.executemany("""
                INSERT INTO creator_case_items(
                    creator_case_id, market_hash_name, item_name, rarity, chance_bp,
                    image_url, value_minor, wear, stattrak, souvenir, phase
                ) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
            """, records)
            await conn.execute("""
                INSERT INTO audit_log(actor_id, action, details)
                VALUES($1,'creator_case_created',$2::jsonb)
            """, user["id"], json.dumps({"creator_case_id": case_id, "items": len(items)}))
    return web.json_response({
        "ok": True, "id": case_id,
        "message": "Авторский кейс создан в демо-режиме. Открытия не списывают деньги."
    })


async def toggle_creator_case_favorite(request):
    user = verify_telegram_user(request)
    case_id = int(request.match_info["case_id"])
    async with db.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT EXISTS(SELECT 1 FROM creator_cases WHERE id=$1 AND active=TRUE)",
            case_id
        )
        if not exists:
            raise web.HTTPNotFound(text="Авторский кейс не найден")
        removed = await conn.fetchval("""
            DELETE FROM creator_case_favorites
            WHERE user_id=$1 AND creator_case_id=$2
            RETURNING TRUE
        """, user["id"], case_id)
        if not removed:
            await conn.execute("""
                INSERT INTO creator_case_favorites(user_id, creator_case_id)
                VALUES($1,$2) ON CONFLICT DO NOTHING
            """, user["id"], case_id)
    return web.json_response({"ok": True, "is_favorite": not bool(removed)})


async def open_creator_case(request):
    user = verify_telegram_user(request)
    case_id = int(request.match_info["case_id"])
    try:
        data = await request.json()
        count = int(data.get("count", 1))
        if not 1 <= count <= 5:
            raise ValueError()
    except (ValueError, TypeError, json.JSONDecodeError):
        raise web.HTTPBadRequest(text="Можно открыть от 1 до 5 кейсов за раз.")
    results = []
    async with db.acquire() as conn:
        async with conn.transaction():
            case = await conn.fetchrow("""
                SELECT id, title FROM creator_cases
                WHERE id=$1 AND active=TRUE FOR SHARE
            """, case_id)
            if not case:
                raise web.HTTPNotFound(text="Авторский кейс не найден")
            contents = await conn.fetch("""
                SELECT id, market_hash_name, item_name, rarity, chance_bp,
                       image_url, value_minor, wear, stattrak, souvenir, phase
                FROM creator_case_items WHERE creator_case_id=$1
                ORDER BY id
            """, case_id)
            if not contents or sum(x["chance_bp"] for x in contents) != 10000:
                raise web.HTTPConflict(text="Состав кейса не прошёл проверку вероятностей.")
            for _ in range(count):
                roll = secrets.randbelow(10000)
                cursor = 0
                selected = contents[-1]
                for item in contents:
                    cursor += item["chance_bp"]
                    if roll < cursor:
                        selected = item
                        break
                inventory_id = await conn.fetchval("""
                    INSERT INTO user_inventory(
                        user_id,item_type,item_name,rarity,image_url,value_minor,source,
                        market_hash_name,wear,stattrak,souvenir,phase
                    ) VALUES($1,'skin',$2,$3,$4,$5,'creator_case',$6,$7,$8,$9,$10)
                    RETURNING id
                """, user["id"], selected["item_name"], selected["rarity"],
                     selected["image_url"], selected["value_minor"],
                     selected["market_hash_name"], selected["wear"],
                     selected["stattrak"], selected["souvenir"], selected["phase"])
                opening_id = await conn.fetchval("""
                    INSERT INTO creator_case_openings(
                        creator_case_id,user_id,item_id,item_name,rarity
                    ) VALUES($1,$2,$3,$4,$5) RETURNING id
                """, case_id, user["id"], selected["id"],
                     selected["item_name"], selected["rarity"])
                results.append({
                    "opening_id": opening_id, "inventory_id": inventory_id,
                    "item_name": selected["item_name"], "rarity": selected["rarity"],
                    "image_url": selected["image_url"],
                    "market_hash_name": selected["market_hash_name"],
                    "value_minor": selected["value_minor"],
                    "chance_percent": round(selected["chance_bp"]/100, 2)
                })
            await conn.execute("""
                INSERT INTO audit_log(actor_id,action,details)
                VALUES($1,'creator_case_opened',$2::jsonb)
            """, user["id"], json.dumps({"creator_case_id": case_id, "count": count}))
    return web.json_response({
        "ok": True, "case_title": case["title"], "items": results,
        "demo_only": True, "message": "Демо-открытие завершено. Предметы добавлены в виртуальный инвентарь."
    })


async def toggle_inventory_favorite(request):
    user = verify_telegram_user(request)
    try:
        inventory_id = int(request.match_info["inventory_id"])
    except (ValueError, TypeError):
        raise web.HTTPBadRequest(text="Некорректный предмет")
    async with db.acquire() as conn:
        owned = await conn.fetchval("""
            SELECT EXISTS(SELECT 1 FROM user_inventory
            WHERE id=$1 AND user_id=$2 AND consumed_at IS NULL)
        """, inventory_id, user["id"])
        if not owned:
            raise web.HTTPNotFound(text="Предмет не найден в инвентаре")
        removed = await conn.fetchval("""
            DELETE FROM user_inventory_favorites
            WHERE user_id=$1 AND inventory_id=$2 RETURNING TRUE
        """, user["id"], inventory_id)
        if not removed:
            await conn.execute("""
                INSERT INTO user_inventory_favorites(user_id,inventory_id)
                VALUES($1,$2) ON CONFLICT DO NOTHING
            """, user["id"], inventory_id)
    return web.json_response({"ok": True, "is_favorite": not bool(removed)})


async def get_rewards(request):
    user = verify_telegram_user(request)
    async with db.acquire() as conn:
        points = await conn.fetchval(
            "SELECT points FROM user_reward_points WHERE user_id=$1", user["id"]
        ) or 0
        last = await conn.fetchrow("""
            SELECT claimed_at, points FROM weekly_reward_claims
            WHERE user_id=$1 ORDER BY claimed_at DESC LIMIT 1
        """, user["id"])
    available = (not last or (datetime.now(timezone.utc)-last["claimed_at"]).total_seconds() >= 7*86400) and int(points) < 1000
    next_at = (last["claimed_at"] + timedelta(days=7)).isoformat() if last and not available and int(points) < 1000 else None
    return web.json_response({
        "points": int(points), "weekly_points": 100, "points_cap": 1000,
        "cap_reached": int(points) >= 1000,
        "available": available, "next_claim_at": next_at,
        "last_claim_at": last["claimed_at"].isoformat() if last else None,
        "demo_only": True
    })


async def claim_weekly_reward(request):
    user = verify_telegram_user(request)
    async with db.acquire() as conn:
        async with conn.transaction():
            await conn.execute("""
                INSERT INTO user_reward_points(user_id,points) VALUES($1,0)
                ON CONFLICT(user_id) DO NOTHING
            """, user["id"])
            await conn.fetchrow("""
                SELECT user_id FROM user_reward_points WHERE user_id=$1 FOR UPDATE
            """, user["id"])
            last = await conn.fetchrow("""
                SELECT claimed_at FROM weekly_reward_claims
                WHERE user_id=$1 ORDER BY claimed_at DESC LIMIT 1
            """, user["id"])
            if last and (datetime.now(timezone.utc)-last["claimed_at"]).total_seconds() < 7*86400:
                raise web.HTTPConflict(text="Еженедельная награда уже получена. Загляните позже.")
            current_points = await conn.fetchval("SELECT points FROM user_reward_points WHERE user_id=$1", user["id"]) or 0
            if int(current_points) >= 1000:
                raise web.HTTPConflict(text="Достигнут лимит демонстрационных бонусов — 1 000 баллов.")
            award_points = min(100, 1000 - int(current_points))
            await conn.execute("""
                INSERT INTO weekly_reward_claims(user_id,points) VALUES($1,$2)
            """, user["id"], award_points)
            points = await conn.fetchval("""
                UPDATE user_reward_points SET points=points+$2,updated_at=NOW()
                WHERE user_id=$1 RETURNING points
            """, user["id"], award_points)
            await conn.execute("""
                INSERT INTO audit_log(actor_id,action,details)
                VALUES($1,'weekly_demo_reward_claimed',$2::jsonb)
            """, user["id"], json.dumps({"points":award_points, "cap":1000}))
    return web.json_response({
        "ok": True, "points": int(points), "added_points": award_points,
        "message": f"Начислено {award_points} демонстрационных бонусных баллов. Лимит — 1 000 баллов; они не являются деньгами и не выводятся."
    })


# ============================================================
# APPLICATION
# ============================================================

async def on_startup(app: web.Application):
    await init_db()

    if not PUBLIC_BASE_URL:
        raise RuntimeError("Не задан PUBLIC_BASE_URL")

    if not WEBHOOK_SECRET:
        raise RuntimeError("Не задан WEBHOOK_SECRET")

    # The digital-products bot always opens its own storefront.
    bot_identity = await bot.get_me()
    logger.info("Telegram bot identity: @%s (id=%s)", bot_identity.username, bot_identity.id)
    await bot.set_chat_menu_button(
        menu_button=MenuButtonWebApp(
            text="Открыть магазин",
            web_app=WebAppInfo(url=f"{PUBLIC_BASE_URL}/"),
        )
    )

    await bot.set_webhook(
        url=f"{PUBLIC_BASE_URL}{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=dp.resolve_used_update_types(),
    )

    if cases_bot:
        cases_identity = await cases_bot.get_me()
        logger.info("CS2 bot identity: @%s (id=%s)", cases_identity.username, cases_identity.id)
        await cases_bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Открыть CS2",
                web_app=WebAppInfo(url=CASES_APP_URL),
            )
        )
        await cases_bot.set_webhook(
            url=f"{PUBLIC_BASE_URL}{CASES_WEBHOOK_PATH}",
            secret_token=CASES_WEBHOOK_SECRET or WEBHOOK_SECRET,
            allowed_updates=cases_dp.resolve_used_update_types(),
        )
    else:
        logger.warning("CASES_BOT_TOKEN is not set; QR CASES Mini App is deployed but its Telegram bot is not connected")

    # Render Free has a 512 MiB memory ceiling. Run one bounded worker and
    # avoid a full-catalog warmup at boot, which can spike memory during deploy.
    global CS2_PRICE_WORKER_TASK
    if CS2_PRICE_WORKER_TASK is None or CS2_PRICE_WORKER_TASK.done():
        CS2_PRICE_WORKER_TASK = asyncio.create_task(cs2_steam_price_worker())
    logger.info("QR BOT PRO started")


async def on_cleanup(app: web.Application):
    global CS2_PRICE_WORKER_TASK, CS2_PRICE_WORKER_TASK_2, CS2_PRICE_WARMUP_TASK
    for task in (CS2_PRICE_WARMUP_TASK, CS2_PRICE_WORKER_TASK, CS2_PRICE_WORKER_TASK_2):
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    CS2_PRICE_WARMUP_TASK = None
    CS2_PRICE_WORKER_TASK = None
    CS2_PRICE_WORKER_TASK_2 = None
    if db is not None:
        await db.close()

    await bot.session.close()
    if cases_bot:
        await cases_bot.session.close()


@web.middleware
async def store_boundary_middleware(request: web.Request, handler):
    if request.path.startswith("/api/"):
        mode = request.headers.get("X-Store-Mode", "digital").strip().lower()
        if mode not in {"digital", "cases"}:
            raise web.HTTPBadRequest(text="Неизвестный магазин")

        case_only = (
            "/api/cases", "/api/cs2", "/api/live-drops",
            "/api/creator-cases", "/api/inventory", "/api/upgrades",
            "/api/rewards", "/api/admin/cases", "/api/admin/cs2-providers",
        )
        digital_only = (
            "/api/products", "/api/orders", "/api/payments",
            "/api/referrals", "/api/admin/products", "/api/admin/orders",
            "/api/admin/payment-providers", "/api/admin/payment-transactions",
            "/api/admin/dashboard",
        )

        def matches(prefixes):
            return any(request.path == prefix or request.path.startswith(prefix + "/") for prefix in prefixes)

        disabled_demo = (
            "/api/live-drops", "/api/creator-cases", "/api/rewards",
            "/api/admin/cases", "/api/admin/cs2-providers",
        )
        if matches(disabled_demo):
            raise web.HTTPGone(text="Этот демонстрационный раздел ShirpDrop отключён.")
        # Case catalogue and upgrade target suggestions are read-only previews.
        # Opening cases and consuming inventory through an upgrade stay disabled
        # until a real item-delivery provider and approved payment flow exist.
        if request.path.startswith("/api/cases/") and not (
            request.method == "POST"
            and re.fullmatch(r"/api/cases/\\d+/claim", request.path)
        ):
            raise web.HTTPGone(text="Открытие кейсов пока недоступно: подключение реальной выдачи предметов ещё не завершено.")
        if request.path.startswith("/api/upgrades/") and not (
            request.path == "/api/upgrades/suggest" and request.method == "POST"
        ):
            raise web.HTTPGone(text="Реальные апгрейды пока отключены. Можно просматривать каталог и рассчитывать цель.")
        if request.path.startswith("/api/inventory/") and request.path.endswith("/sell"):
            raise web.HTTPGone(text="Продажа виртуальных предметов отключена. Денежные средства доступны только через подтверждённые платежи.")

        if mode == "digital" and matches(case_only):
            raise web.HTTPNotFound(text="Этот раздел доступен в магазине QR CASES")
        if mode == "cases" and matches(digital_only):
            raise web.HTTPNotFound(text="Этот раздел доступен в магазине цифровых товаров")

    return await handler(request)


def create_app():
    app = web.Application(middlewares=[error_middleware, store_boundary_middleware])

    # Public endpoints
    app.router.add_get("/health", health)
    app.router.add_get("/api/products", get_products)
    app.router.add_post("/api/orders", create_order)
    app.router.add_post("/api/orders/bulk", create_bulk_orders)
    app.router.add_get("/api/orders", get_my_orders)
    app.router.add_get("/api/profile", get_my_profile)
    app.router.add_post("/api/steam/connect", start_steam_auth)
    app.router.add_get("/api/steam/callback", steam_auth_callback)
    app.router.add_post("/api/referrals/apply", apply_referral_code)
    app.router.add_get("/api/cases", get_cases)
    app.router.add_post("/api/cases/{case_id}/claim", claim_demo_case)
    app.router.add_get("/api/live-drops", get_live_drops)
    app.router.add_get("/api/creator-cases", get_creator_cases)
    app.router.add_post("/api/creator-cases", create_creator_case)
    app.router.add_post("/api/creator-cases/{case_id}/favorite", toggle_creator_case_favorite)
    app.router.add_post("/api/inventory/{inventory_id}/favorite", toggle_inventory_favorite)
    app.router.add_get("/api/rewards", get_rewards)
    app.router.add_get("/api/inventory", get_inventory)
    # Read-only upgrade target discovery used by the Mini App auto-picker.
    app.router.add_post("/api/upgrades/suggest", suggest_upgrade_target)
    app.router.add_get("/api/wallet", get_wallet)
    app.router.add_get("/api/wallet/cash", get_cash_wallet)
    app.router.add_post("/api/wallet/topups", create_telebotly_topup)
    app.router.add_post("/api/webhooks/telebotly", telebotly_webhook)

    # Payment administration endpoints
    app.router.add_get("/api/admin/payment-providers", admin_get_payment_providers)
    app.router.add_post(
        "/api/admin/payment-providers/{provider_code}",
        admin_configure_payment_provider,
    )
    app.router.add_get("/api/admin/payment-transactions", admin_get_payment_transactions)
    app.router.add_get("/api/cs2/catalog", get_cs2_catalog)
    app.router.add_get("/api/cs2/skins", get_cs2_skins)

    # Admin endpoints
    app.router.add_put("/api/admin/products/{product_id}", admin_update_product)
    app.router.add_get("/api/admin/cases", admin_get_cases)
    app.router.add_get("/api/admin/cs2-providers", admin_get_cs2_provider_status)
    app.router.add_post("/api/admin/cases", admin_save_case)
    app.router.add_put("/api/admin/cases/{case_id}", admin_save_case)
    app.router.add_post("/api/admin/cases/{case_id}/toggle", admin_toggle_case)
    app.router.add_get("/api/admin/cases/{case_id}/items", admin_get_case_items)
    app.router.add_post("/api/admin/cases/{case_id}/items", admin_save_case_item)
    app.router.add_put("/api/admin/cases/{case_id}/items/{item_id}", admin_save_case_item)
    app.router.add_post("/api/admin/cases/{case_id}/items/{item_id}/toggle", admin_toggle_case_item)
    app.router.add_get("/api/admin/wallet-users", admin_get_wallet_users)
    app.router.add_post("/api/admin/wallet-users/{user_id}/adjust", admin_adjust_wallet)
    app.router.add_post("/api/admin/products", admin_add_product)
    app.router.add_post(
        "/api/admin/products/{product_id}/toggle",
        admin_toggle_product,
    )
    app.router.add_get("/api/admin/dashboard", admin_get_dashboard)
    app.router.add_get("/api/admin/finance", admin_get_finance)
    app.router.add_get("/api/admin/finance/export", admin_export_finance)
    app.router.add_get("/api/admin/products", admin_get_products)
    app.router.add_get("/api/admin/orders", admin_get_orders)
    app.router.add_post(
        "/api/admin/orders/{order_id}/status",
        admin_set_order_status,
    )

    # Telegram webhooks: one dispatcher and token per independent storefront.
    SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET,
    ).register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    if cases_bot:
        SimpleRequestHandler(
            dispatcher=cases_dp,
            bot=cases_bot,
            secret_token=CASES_WEBHOOK_SECRET or WEBHOOK_SECRET,
        ).register(app, path=CASES_WEBHOOK_PATH)

    # Static Mini Apps
    async def index_handler(request):
        return web.FileResponse("static/index.html")

    async def cases_page_handler(request):
        return web.FileResponse("static/cases.html")

    async def admin_page_handler(request):
        # Serve only the static shell here. Telegram initData is not attached to
        # the initial document navigation; every admin API request verifies it.
        return web.FileResponse("static/admin.html")

    app.router.add_get("/admin/finance", admin_page_handler)
    app.router.add_get("/admin", admin_page_handler)
    app.router.add_get("/admin/", admin_page_handler)
    app.router.add_get("/cases", cases_page_handler)
    app.router.add_get("/cases/", cases_page_handler)
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
