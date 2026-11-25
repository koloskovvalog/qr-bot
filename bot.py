import asyncio, logging, asyncpg, os, hmac, hashlib, json, time
from datetime import datetime, timezone
from typing import Callable, Dict, Any, Awaitable
from aiogram import Bot, Dispatcher, F, types, BaseMiddleware
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiogram.exceptions import TelegramRetryAfter
from aiohttp import web, ClientSession
from rollypay import RollyPayClient
from aiorocket2 import xRocketClient

# --- НАСТРОЙКИ ---
# Секреты задаются в Render -> Environment, а не хранятся в исходном коде.
def required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"Не задана обязательная переменная окружения: {name}")
    return value

BOT_TOKEN = required_env("BOT_TOKEN")
ROLLYPAY_API_KEY = required_env("aMkEd8uF3EY1PM5Zzki5oFfPVx7pnRUiuxYUJQLS5ts")
TERMINAL_ID = required_env("02485c32-7013-40f6-9c1f-00eb12f51d31")
SIGNING_SECRET = required_env("SQ7ZPKKj0l-_yFL_KsARgUj6FjuMJ51v4KFgbr53MAk")
XROCKET_API_KEY = required_env("XROCKET_API_KEY")
XROCKET_WEBHOOK_SECRET = required_env("XROCKET_WEBHOOK_SECRET")
DATABASE_URL = required_env("postgresql://postgres.qrfqdihhoqvlaqkuyols:Koloskov2006@aws-0-eu-west-1.pooler.supabase.com:5432/postgres")
WEBHOOK_PORT = int(os.environ.get("PORT", "10000"))
BOT_MODE = os.getenv("BOT_MODE", "polling").strip().lower()  # polling или webhook

MIN_TRC20_USDT = 10.0
MIN_BEP20_USDT = 50.0
ADMIN_FEE_PER_USDT = 1.0
ADMIN_ID = 7653934214
DEFAULT_PAYMENT_SYSTEM_FEE = 9.0
DEFAULT_ADMIN_DEPOSIT_FEE = 4.0

PROJECT_NAME = "PSD Escray"
PROJECT_DATE = "23.11.2024"
SUPPORT_USERNAME = "@FrezzyBrezzy1"

OFFER_URL = "https://telegra.ph/Polzovatelskoe-soglashenie-11-23-71"
PRIVACY_URL = "https://telegra.ph/Politika-konfidencialnosti-11-23-148"
REFUND_URL = "https://telegra.ph/Politika-vozvratov-11-23"
DEFAULT_VERIFICATION_CODE = "VERIFY_2025"

CATEGORY_EMOJI = {"Игры": "🎮", "Подписки": "📱", "AI-сервисы": "🤖", "Софт": "💻", "Обучение": "📚", "Другое": "📦"}

DEMO_PRODUCTS = [
    ("Игры", "Steam ключ 500₽", "Пополнение Steam на 500 рублей", 550.0, "Ожидайте выдачи, напишет админ"),
    ("Игры", "Steam ключ 1000₽", "Пополнение Steam на 1000 рублей", 1050.0, "Ожидайте выдачи, напишет админ"),
    ("Подписки", "Spotify Premium 1 мес", "Подписка Spotify Premium", 350.0, "Ожидайте выдачи"),
    ("Подписки", "Netflix 1 мес", "Подписка Netflix", 700.0, "Ожидайте выдачи"),
    ("AI-сервисы", "ChatGPT Plus 1 мес", "Подписка ChatGPT Plus", 1500.0, "Ожидайте выдачи"),
]

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
rolly_client = None
rocket_client = None
pending_code_input = {}
pending_amount_input = {}
admin_states = {}

# === АНТИ-ФЛУД МИДЛВАРЬ ===
class ThrottlingMiddleware(BaseMiddleware):
    def __init__(self, limit=0.3):
        self.limit = limit
        self.users = {}
    async def __call__(self, handler: Callable[[types.TelegramObject, Dict[str, Any]], Awaitable[Any]], event: types.TelegramObject, data: Dict[str, Any]) -> Any:
        user_id = event.from_user.id if hasattr(event, 'from_user') else None
        if user_id:
            now = time.time()
            if user_id in self.users and now - self.users[user_id] < self.limit:
                return
            self.users[user_id] = now
        return await handler(event, data)

dp.message.middleware(ThrottlingMiddleware(0.3))
dp.callback_query.middleware(ThrottlingMiddleware(0.3))

def calculate_network_fee(network: str, amount_usdt: float) -> float:
    if network == 'TRC20': return 3.0
    elif network == 'BEP20': return (amount_usdt * 0.01) + 2.0
    return 0.0

_base_rate = 0.0
_last_rate_update = 0

async def fetch_base_rate():
    global _base_rate, _last_rate_update
    if time.time() - _last_rate_update < 60 and _base_rate > 0: return _base_rate
    try:
        async with ClientSession() as s:
            async with s.get("https://api.rapira.net/open/market/rates", timeout=10) as r:
                data = await r.json()
                rd = next((i for i in data.get('data', []) if i.get('symbol') == 'USDT/RUB'), None)
                if rd:
                    b = float(rd.get('close') or rd.get('askPrice') or 0)
                    if b > 0:
                        _base_rate = b; _last_rate_update = time.time(); return _base_rate
    except Exception as e: logging.error(f"rate: {e}")
    return _base_rate if _base_rate > 0 else 85.0

async def get_setting(key, default):
    async with db_pool.acquire() as c:
        r = await c.fetchrow("SELECT value FROM settings WHERE key=$1", key)
        return r['value'] if r else default

async def get_current_markup(): return float(await get_setting('markup', '2.0'))
async def get_ps_fee(): return float(await get_setting('ps_fee', str(DEFAULT_PAYMENT_SYSTEM_FEE)))
async def get_admin_deposit_fee(): return float(await get_setting('admin_deposit_fee', str(DEFAULT_ADMIN_DEPOSIT_FEE)))
async def get_verification_code(): return await get_setting('verify_code', DEFAULT_VERIFICATION_CODE)
async def get_final_rate(): return await fetch_base_rate() + await get_current_markup()

async def is_verified(uid):
    async with db_pool.acquire() as c:
        r = await c.fetchrow("SELECT verified_go6 FROM users WHERE user_id=$1", uid)
        return bool(r and r['verified_go6'])

db_pool: asyncpg.Pool = None

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, ssl="require", statement_cache_size=0)
    async with db_pool.acquire() as c:
        await c.execute('''CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, balance REAL DEFAULT 0, last_active TIMESTAMPTZ DEFAULT NOW(), username TEXT, verified_go6 BOOLEAN DEFAULT FALSE);''')
        await c.execute('''ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMPTZ DEFAULT NOW();''')
        await c.execute('''ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;''')
        await c.execute('''ALTER TABLE users ADD COLUMN IF NOT EXISTS verified_go6 BOOLEAN DEFAULT FALSE;''')
        await c.execute('''CREATE TABLE IF NOT EXISTS orders (id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount REAL, payment_id TEXT UNIQUE, status TEXT, order_type TEXT DEFAULT 'deposit', comment TEXT, message_id BIGINT, created_at TIMESTAMPTZ DEFAULT NOW());''')
        await c.execute('''ALTER TABLE orders ADD COLUMN IF NOT EXISTS comment TEXT;''')
        await c.execute('''ALTER TABLE orders ADD COLUMN IF NOT EXISTS message_id BIGINT;''')
        await c.execute('''CREATE TABLE IF NOT EXISTS withdrawals (id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount_usdt REAL, amount_rub REAL, address TEXT, status TEXT DEFAULT 'pending', network TEXT DEFAULT 'BEP20', created_at TIMESTAMPTZ DEFAULT NOW());''')
        await c.execute('''ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS network TEXT DEFAULT 'BEP20';''')
        await c.execute('''CREATE TABLE IF NOT EXISTS products (id BIGSERIAL PRIMARY KEY, category TEXT, name TEXT, description TEXT, price REAL, delivery_content TEXT, stock INTEGER DEFAULT -1, is_active BOOLEAN DEFAULT TRUE, created_at TIMESTAMPTZ DEFAULT NOW());''')
        await c.execute('''CREATE TABLE IF NOT EXISTS purchases (id BIGSERIAL PRIMARY KEY, user_id BIGINT, product_id BIGINT, product_name TEXT, price REAL, status TEXT DEFAULT 'completed', created_at TIMESTAMPTZ DEFAULT NOW());''')
        await c.execute('''CREATE TABLE IF NOT EXISTS deposit_requests (id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount REAL, order_type TEXT, comment TEXT, status TEXT DEFAULT 'pending', admin_msg_id BIGINT, created_at TIMESTAMPTZ DEFAULT NOW());''')
        await c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);''')
        for k, v in [('bot_status','active'),('markup','2.0'),('ps_fee',str(DEFAULT_PAYMENT_SYSTEM_FEE)),('admin_deposit_fee',str(DEFAULT_ADMIN_DEPOSIT_FEE)),('verify_code',DEFAULT_VERIFICATION_CODE)]:
            await c.execute("INSERT INTO settings (key,value) VALUES ($1,$2) ON CONFLICT (key) DO NOTHING", k, v)
        await c.execute('''INSERT INTO users (user_id, last_active, balance, verified_go6) VALUES ($1, NOW(), 0, TRUE) ON CONFLICT (user_id) DO UPDATE SET verified_go6 = TRUE''', ADMIN_ID)
        if (await c.fetchval("SELECT COUNT(*) FROM products")) == 0:
            for cat, name, desc, price, dl in DEMO_PRODUCTS:
                await c.execute("INSERT INTO products (category,name,description,price,delivery_content) VALUES ($1,$2,$3,$4,$5)", cat, name, desc, price, dl)
    logging.info("DB ready")

async def get_bot_status(): return await get_setting('bot_status', 'active')

def fmt_seen(la):
    if not la: return "?"
    if la.tzinfo is None: la = la.replace(tzinfo=timezone.utc)
    s = (datetime.now(timezone.utc) - la).total_seconds()
    if s < 60: return "🟢 В сети"
    if s < 3600: return f"🟡 {int(s//60)} мин назад"
    if s < 86400: return f"🟠 {int(s//3600)} ч назад"
    return f"🔴 {int(s//86400)} дн назад"

# === MIDDLEWARE ===
@dp.message.middleware()
async def mw_track(handler, event: types.Message, data):
    if event.from_user:
        async with db_pool.acquire() as c:
            await c.execute('''INSERT INTO users (user_id, last_active, username) VALUES ($1, NOW(), $2) ON CONFLICT (user_id) DO UPDATE SET last_active=NOW(), username=$2''', event.from_user.id, event.from_user.username)
    return await handler(event, data)

@dp.message.middleware()
async def mw_fwd(handler, event: types.Message, data):
    if event.forward_origin: return
    return await handler(event, data)

@dp.message.middleware()
@dp.callback_query.middleware()
async def mw_maint(handler, event, data):
    if event.from_user and event.from_user.id == ADMIN_ID: return await handler(event, data)
    if (await get_bot_status()) == 'stopped':
        if isinstance(event, types.Message): await event.answer("🛠 Техобслуживание")
        elif isinstance(event, types.CallbackQuery): await event.answer("🛠 Техобслуживание", show_alert=True)
        return
    return await handler(event, data)

@dp.errors()
async def err_h(update, exception):
    logging.error(f"err: {exception}"); return True

# === ВВОД ТЕКСТА ===
@dp.message(F.text & ~F.text.startswith("/"))
async def h_text(m: types.Message):
    uid = m.from_user.id
    
    if uid == ADMIN_ID and uid in admin_states:
        state = admin_states[uid]
        action = state.get("action")
        
        if action == "create":
            step = state.get("step")
            if step == "category":
                state["category"] = m.text.strip()
                state["step"] = "name"
                return await m.answer("2️⃣ Введите **название** товара:", parse_mode="Markdown")
            elif step == "name":
                state["name"] = m.text.strip()
                state["step"] = "desc"
                return await m.answer("3️⃣ Введите **описание** (или `-` чтобы пропустить):", parse_mode="Markdown")
            elif step == "desc":
                state["desc"] = m.text.strip() if m.text.strip() != "-" else ""
                state["step"] = "price"
                return await m.answer("4️⃣ Введите **цену** в рублях (например `550`):", parse_mode="Markdown")
            elif step == "price":
                try:
                    price = float(m.text.replace(',', '.'))
                    state["price"] = price
                    state["step"] = "delivery"
                    return await m.answer("5️⃣ Что бот должен выдать юзеру после покупки?\nИли `-` если хотите, чтобы юзер обратился к вам.", parse_mode="Markdown")
                except:
                    return await m.answer("❌ Введите число.")
            elif step == "delivery":
                state["delivery"] = m.text.strip() if m.text.strip() != "-" else "Свяжитесь с админом для получения"
                async with db_pool.acquire() as c:
                    pid = await c.fetchval("INSERT INTO products (category,name,description,price,delivery_content) VALUES ($1,$2,$3,$4,$5) RETURNING id", 
                                           state["category"], state["name"], state["desc"], state["price"], state["delivery"])
                admin_states.pop(uid)
                return await m.answer(f"✅ **Товар успешно создан!**\nID: `{pid}`", parse_mode="Markdown")

        elif action == "edit_price":
            try:
                price = float(m.text.replace(',', '.'))
                async with db_pool.acquire() as c:
                    await c.execute("UPDATE products SET price=$1 WHERE id=$2", price, state["pid"])
                admin_states.pop(uid)
                return await m.answer(f"✅ Цена обновлена!")
            except:
                return await m.answer("❌ Введите число.")
                
        elif action == "edit_desc":
            async with db_pool.acquire() as c:
                await c.execute("UPDATE products SET description=$1 WHERE id=$2", m.text.strip(), state["pid"])
            admin_states.pop(uid)
            return await m.answer(f"✅ Описание обновлено!")

    if uid in pending_code_input:
        pending_code_input.pop(uid, None)
        code = m.text.strip()
        if code == await get_verification_code():
            async with db_pool.acquire() as c:
                await c.execute("INSERT INTO users (user_id, last_active, verified_go6) VALUES ($1,NOW(),TRUE) ON CONFLICT (user_id) DO UPDATE SET verified_go6=TRUE", uid)
            await m.answer("✅ Верификация пройдена! Пополнение без ограничений.")
            try:
                un = f"@{m.from_user.username}" if m.from_user.username else f"ID:{uid}"
                await bot.send_message(ADMIN_ID, f"✅ Верифицирован по коду: {un} `{uid}`", parse_mode="Markdown")
            except: pass
        else:
            await m.answer("❌ Неверный код")
            try: await bot.send_message(ADMIN_ID, f"⚠️ Неверный код от `{uid}`: `{code[:50]}`", parse_mode="Markdown")
            except: pass
        return
    
    if uid in pending_amount_input:
        info = pending_amount_input.pop(uid)
        typ = info['type']
        try:
            amount = float(m.text.replace(',', '.'))
            if amount <= 0: raise ValueError
        except:
            await m.answer("❌ Введите число"); return
        if typ == 'card':
            await process_deposit_request(m.from_user, amount, 'card', None)
        else:
            await process_deposit_request(m.from_user, amount, 'usdt', None)
        return

# === DEPOSIT REQUEST ===
async def process_deposit_request(user, amount, typ, comment):
    uid = user.id
    is_v = await is_verified(uid) or uid == ADMIN_ID
    
    if not is_v:
        rate = await get_final_rate()
        rub_amount = amount if typ == 'card' else amount * rate
        if rub_amount < 100:
            return await bot.send_message(uid, f"❌ Минимальная сумма для пополнения без верификации — **100 ₽** (или ~{100/rate:.2f} USDT).", parse_mode="Markdown")
        if rub_amount > 1000:
            return await bot.send_message(uid, f"❌ Максимальная сумма для пополнения без верификации — **1000 ₽** (или ~{1000/rate:.2f} USDT).", parse_mode="Markdown")

    if is_v:
        if typ == 'card':
            await create_card_invoice_by_id(uid, amount, comment)
        else:
            await create_usdt_invoice_by_id(uid, amount)
    else:
        async with db_pool.acquire() as c:
            rid = await c.fetchval("INSERT INTO deposit_requests (user_id, amount, order_type, comment) VALUES ($1,$2,$3,$4) RETURNING id", uid, amount, typ, comment)
        un = f"@{user.username}" if user.username else f"ID:{uid}"
        
        if typ == 'card':
            summ = f"{amount:.2f} ₽"
            way = "💳 Карта"
        else:
            rub_eq = amount * (await get_final_rate())
            summ = f"{amount:.2f} USDT (~{rub_eq:.2f} ₽)"
            way = "🪙 USDT"
            
        b = InlineKeyboardBuilder()
        b.button(text="✅ Одобрить", callback_data=f"dr_ok_{rid}")
        b.button(text="❌ Отклонить", callback_data=f"dr_no_{rid}")
        try:
            sent = await bot.send_message(ADMIN_ID, f"💰 **ЗАЯВКА НА ПОПОЛНЕНИЕ #{rid}**\n👤 {un}\n`{uid}`\nСпособ: {way}\nСумма: **{summ}**\n\n_Юзер не верифицирован_", reply_markup=b.as_markup(), parse_mode="Markdown")
            async with db_pool.acquire() as c: await c.execute("UPDATE deposit_requests SET admin_msg_id=$1 WHERE id=$2", sent.message_id, rid)
        except: pass
        await bot.send_message(uid, "⏳ Заявка на пополнение отправлена администратору. Ожидайте одобрения.")

@dp.callback_query(F.data.startswith("dr_ok_"))
async def cb_dr_ok(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    rid = int(cb.data.split("_")[2])
    async with db_pool.acquire() as c:
        req = await c.fetchrow("SELECT * FROM deposit_requests WHERE id=$1", rid)
        if not req or req['status'] != 'pending': return await cb.answer("Уже обработано", show_alert=True)
        await c.execute("UPDATE deposit_requests SET status='approved' WHERE id=$1", rid)
    uid = req['user_id']; amount = req['amount']; typ = req['order_type']
    if typ == 'card':
        await create_card_invoice_by_id(uid, amount, req['comment'])
    else:
        await create_usdt_invoice_by_id(uid, amount)
    await cb.message.edit_text(cb.message.text + "\n\n✅ **ОДОБРЕНО**", parse_mode="Markdown")
    await cb.answer("✅")

@dp.callback_query(F.data.startswith("dr_no_"))
async def cb_dr_no(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    rid = int(cb.data.split("_")[2])
    async with db_pool.acquire() as c:
        req = await c.fetchrow("SELECT * FROM deposit_requests WHERE id=$1", rid)
        if not req or req['status'] != 'pending': return await cb.answer("Уже обработано", show_alert=True)
        await c.execute("UPDATE deposit_requests SET status='rejected' WHERE id=$1", rid)
    try: await bot.send_message(req['user_id'], "❌ Заявка на пополнение отклонена.")
    except: pass
    await cb.message.edit_text(cb.message.text + "\n\n❌ **ОТКЛОНЕНО**", parse_mode="Markdown")
    await cb.answer("❌")

async def create_card_invoice_by_id(uid, amount, comment):
    try:
        async with db_pool.acquire() as c:
            cid = await c.fetchval("INSERT INTO orders (user_id, amount, status, comment) VALUES ($1,$2,'pending',$3) RETURNING id", uid, amount, comment)
        desc = f"Чек #{cid}"
        pay = rolly_client.payments.create(amount=str(amount), order_id=f"dep_{uid}_{cid}", terminal_id=TERMINAL_ID, description=desc, customer_id=str(uid), redirect_url="https://t.me")
        b = InlineKeyboardBuilder(); b.button(text="💳 Оплатить", url=pay['pay_url'])
        msg = await bot.send_message(uid, f"**Счёт: {amount:.2f} ₽**\n🟡 Ожидает оплату", reply_markup=b.as_markup(), parse_mode="Markdown")
        async with db_pool.acquire() as c:
            await c.execute("UPDATE orders SET payment_id=$1, message_id=$2 WHERE id=$3", pay['payment_id'], msg.message_id, cid)
    except Exception as e: logging.error(f"cardbyid: {e}")

async def create_usdt_invoice_by_id(uid, amount):
    try:
        async with db_pool.acquire() as c:
            cid = await c.fetchval("INSERT INTO orders (user_id, amount, status, order_type) VALUES ($1,$2,'pending','crypto') RETURNING id", uid, amount)
        inv = await rocket_client.create_invoice(currency="USDT", amount=amount, description=f"Чек #{cid}", payload=f"{uid}_{cid}", expired_in=3600)
        b = InlineKeyboardBuilder(); b.button(text="💳 xRocket", url=inv.link)
        msg = await bot.send_message(uid, f"💵 **Чек #{cid} на {amount} USDT**", reply_markup=b.as_markup(), parse_mode="Markdown")
        async with db_pool.acquire() as c:
            await c.execute("UPDATE orders SET payment_id=$1, message_id=$2 WHERE id=$3", inv.invoice_id, msg.message_id, cid)
    except Exception as e: logging.error(f"usdtbyid: {e}")

# === ADMIN COMMANDS ===
@dp.message(Command("stop_bot"))
async def c_stop(m: types.Message):
    if m.from_user.id != ADMIN_ID: return
    async with db_pool.acquire() as c: await c.execute("UPDATE settings SET value='stopped' WHERE key='bot_status'")
    await m.answer("⏸")

@dp.message(Command("start_bot"))
async def c_start_bot(m: types.Message):
    if m.from_user.id != ADMIN_ID: return
    async with db_pool.acquire() as c: await c.execute("UPDATE settings SET value='active' WHERE key='bot_status'")
    await m.answer("▶️")

@dp.message(Command("setmarkup"))
async def c_markup(m: types.Message, cmd: CommandObject):
    if m.from_user.id != ADMIN_ID: return
    if not cmd.args: return await m.answer("`/setmarkup 2`")
    try:
        v = float(cmd.args.replace(',','.'))
        async with db_pool.acquire() as c: await c.execute("UPDATE settings SET value=$1 WHERE key='markup'", str(v))
        await m.answer(f"✅ +{v}₽")
    except: await m.answer("❌")

@dp.message(Command("setfee"))
async def c_fee(m: types.Message, cmd: CommandObject):
    if m.from_user.id != ADMIN_ID: return
    ps, ad = await get_ps_fee(), await get_admin_deposit_fee()
    if not cmd.args: return await m.answer(f"ПС: {ps}% | Твой: {ad}% | Юзеру: {100-ps-ad:.2f}%\n`/setfee ps 12` `/setfee me 5`")
    a = cmd.args.split()
    if len(a) == 2 and a[0] in ('ps','me'):
        try: p = float(a[1].replace(',','.'))
        except: return await m.answer("❌")
        other = ad if a[0]=='ps' else ps
        if p < 0 or p + other >= 100: return await m.answer("❌")
        key = 'ps_fee' if a[0]=='ps' else 'admin_deposit_fee'
        async with db_pool.acquire() as c: await c.execute(f"UPDATE settings SET value=$1 WHERE key='{key}'", str(p))
        return await m.answer(f"✅ {p}%")
    if len(a) == 1:
        try: p = float(a[0].replace(',','.'))
        except: return await m.answer("❌")
        if p < 0 or p + ps >= 100: return await m.answer("❌")
        async with db_pool.acquire() as c: await c.execute("UPDATE settings SET value=$1 WHERE key='admin_deposit_fee'", str(p))
        return await m.answer(f"✅ {p}%")

@dp.message(Command("setverifycode"))
async def c_vc(m: types.Message, cmd: CommandObject):
    if m.from_user.id != ADMIN_ID: return
    if not cmd.args: return await m.answer(f"🔐 Код: `{await get_verification_code()}`\n`/setverifycode новый`")
    nc = cmd.args.strip()
    if len(nc) < 4: return await m.answer("❌ мин 4")
    async with db_pool.acquire() as c: await c.execute("UPDATE settings SET value=$1 WHERE key='verify_code'", nc)
    await m.answer(f"✅ `{nc}`")

@dp.message(Command("allow"))
async def c_allow(m: types.Message, cmd: CommandObject):
    if m.from_user.id != ADMIN_ID: return
    if not cmd.args: return await m.answer("`/allow ID`")
    try:
        uid = int(cmd.args.strip())
        async with db_pool.acquire() as c:
            await c.execute("INSERT INTO users (user_id, last_active, verified_go6) VALUES ($1,NOW(),TRUE) ON CONFLICT (user_id) DO UPDATE SET verified_go6=TRUE", uid)
        await m.answer(f"✅ {uid}")
        try: await bot.send_message(uid, "✅ Верифицированы админом")
        except: pass
    except: await m.answer("❌")

@dp.message(Command("deny"))
async def c_deny(m: types.Message, cmd: CommandObject):
    if m.from_user.id != ADMIN_ID: return
    if not cmd.args: return await m.answer("`/deny ID`")
    try:
        uid = int(cmd.args.strip())
        async with db_pool.acquire() as c: await c.execute("UPDATE users SET verified_go6=FALSE WHERE user_id=$1", uid)
        await m.answer(f"🚫 {uid}")
    except: await m.answer("❌")

@dp.message(Command("add"))
async def c_add(m: types.Message, cmd: CommandObject):
    if m.from_user.id != ADMIN_ID: return
    try:
        uid, amt = int(cmd.args.split()[0]), float(cmd.args.split()[1])
        async with db_pool.acquire() as c: await c.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", amt, uid)
        await m.answer(f"✅ +{amt}")
    except: pass

@dp.message(Command("deduct"))
async def c_ded(m: types.Message, cmd: CommandObject):
    if m.from_user.id != ADMIN_ID: return
    try:
        uid, amt = int(cmd.args.split()[0]), float(cmd.args.split()[1])
        async with db_pool.acquire() as c: await c.execute("UPDATE users SET balance=balance-$1 WHERE user_id=$2", amt, uid)
        await m.answer(f"✅ -{amt}")
        try: await bot.send_message(uid, f"⚠️ Списано {amt:.2f}")
        except: pass
    except: pass

@dp.message(Command("quiet_deduct"))
async def c_qded(m: types.Message, cmd: CommandObject):
    if m.from_user.id != ADMIN_ID: return
    try:
        uid, amt = int(cmd.args.split()[0]), float(cmd.args.split()[1])
        async with db_pool.acquire() as c: await c.execute("UPDATE users SET balance=balance-$1 WHERE user_id=$2", amt, uid)
        await m.answer(f"✅ -{amt} (тихо)")
    except: pass

# === USER COMMANDS ===
@dp.message(Command("new"))
async def c_new(m: types.Message, cmd: CommandObject):
    if not cmd.args: return await m.answer("`/new 500`")
    try: amount = float(cmd.args.split()[0].replace(',','.'))
    except: return await m.answer("❌")
    parts = cmd.args.split(maxsplit=1)
    comment = parts[1] if len(parts) > 1 else None
    await process_deposit_request(m.from_user, amount, 'card', comment)

@dp.message(Command("usdt"))
async def c_usdt(m: types.Message, cmd: CommandObject):
    if not cmd.args: return await m.answer("`/usdt 10`")
    try: amount = float(cmd.args.split()[0].replace(',','.'))
    except: return await m.answer("❌")
    if amount < 1: return await m.answer("❌ мин 1")
    await process_deposit_request(m.from_user, amount, 'usdt', None)

@dp.message(Command("withdraw"))
async def c_wd(m: types.Message, cmd: CommandObject):
    if not cmd.args or len(cmd.args.split()) < 3: return await m.answer("`/withdraw [сумма] [BEP20|TRC20] [адрес]`")
    try:
        a = cmd.args.split()
        amount_usdt = float(a[0].replace(',','.'))
        network = a[1].upper()
        address = a[2]
        if network not in ['BEP20','TRC20']: return await m.answer("❌ сеть")
        if network == 'TRC20' and amount_usdt < MIN_TRC20_USDT: return await m.answer(f"❌ мин {MIN_TRC20_USDT}")
        if network == 'BEP20' and amount_usdt < MIN_BEP20_USDT: return await m.answer(f"❌ мин {MIN_BEP20_USDT}")
    except: return await m.answer("❌")
    fee = calculate_network_fee(network, amount_usdt)
    total_rub = (amount_usdt + fee) * (await get_final_rate())
    profit = amount_usdt * ADMIN_FEE_PER_USDT
    async with db_pool.acquire() as c:
        r = await c.fetchrow("SELECT balance FROM users WHERE user_id=$1", m.from_user.id)
        bal = r['balance'] if r and r['balance'] else 0.0
        if bal < total_rub: return await m.answer("❌ мало средств")
        tw = await c.fetchval("SELECT COALESCE(SUM(amount_rub),0) FROM withdrawals WHERE user_id=$1 AND status IN ('pending','processing','completed') AND created_at>=CURRENT_DATE", m.from_user.id)
        lim = bal / 2.0
        if tw + total_rub > lim: return await m.answer(f"❌ Лимит 50%: {lim:.2f}₽, уже {tw:.2f}₽")
        await c.execute("UPDATE users SET balance=balance-$1 WHERE user_id=$2", total_rub, m.from_user.id)
        await c.execute("INSERT INTO withdrawals (user_id, amount_usdt, amount_rub, address, network) VALUES ($1,$2,$3,$4,$5)", m.from_user.id, amount_usdt, total_rub, address, network)
        await c.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", profit, ADMIN_ID)
        wid = (await c.fetchrow("SELECT lastval()"))[0]
    await m.answer(f"✅ Заявка #{wid}\n{network} | {amount_usdt} USDT\nСписано {total_rub:.2f}₽")
    b = InlineKeyboardBuilder()
    b.button(text="🟢 В работу", callback_data=f"wt_take_{wid}")
    b.button(text="🔴 Отклонить", callback_data=f"wt_no_{wid}")
    await bot.send_message(ADMIN_ID, f"💸 Заявка #{wid}\n`{m.from_user.id}`\n{network}\n{amount_usdt} USDT\n`{address}`\n+{profit}₽", reply_markup=b.as_markup())

@dp.message(Command("balance"))
async def c_bal(m: types.Message):
    rate = await get_final_rate()
    async with db_pool.acquire() as c:
        r = await c.fetchrow("SELECT balance FROM users WHERE user_id=$1", m.from_user.id)
        b = r['balance'] if r and r['balance'] else 0.0
    await m.answer(f"💰 **{b:.2f} ₽** ({b/rate:.2f} USDT)", parse_mode="Markdown")

@dp.message(Command("verify"))
async def c_verify(m: types.Message):
    if await is_verified(m.from_user.id): return await m.answer("✅ Уже верифицированы")
    pending_code_input[m.from_user.id] = True
    await m.answer("🔐 Введите код верификации одним сообщением")

@dp.message(Command("offer"))
async def c_off(m: types.Message):
    b = InlineKeyboardBuilder(); b.button(text="📄 Открыть", url=OFFER_URL)
    await m.answer(f"📄 **Пользовательское соглашение**\n\nПроект: {PROJECT_NAME}\nДата: {PROJECT_DATE}\nПоддержка: {SUPPORT_USERNAME}", reply_markup=b.as_markup(), parse_mode="Markdown")

@dp.message(Command("privacy"))
async def c_priv(m: types.Message):
    b = InlineKeyboardBuilder(); b.button(text="🔒 Открыть", url=PRIVACY_URL)
    await m.answer(f"🔒 **Политика конфиденциальности**\n\nПроект: {PROJECT_NAME}\nДата: {PROJECT_DATE}\nПоддержка: {SUPPORT_USERNAME}", reply_markup=b.as_markup(), parse_mode="Markdown")

@dp.message(Command("refund"))
async def c_refund(m: types.Message):
    b = InlineKeyboardBuilder(); b.button(text="🔄 Открыть", url=REFUND_URL)
    await m.answer(f"🔄 **Политика возвратов**\n\nПроект: {PROJECT_NAME}\nДата: {PROJECT_DATE}\nПоддержка: {SUPPORT_USERNAME}", reply_markup=b.as_markup(), parse_mode="Markdown")

@dp.message(Command("help"))
async def c_help(m: types.Message):
    await send_help(m.chat.id)

# === HELP FUNCTION (общая для команды и кнопки) ===
async def send_help(chat_id, edit_msg=None):
    is_admin = chat_id == ADMIN_ID
    text = (
        f"📚 **Справка по командам**\n\n"
        f"🛒 /start — открыть магазин\n"
        f"💰 /balance — проверить баланс\n"
        f"📥 /new [сумма] — пополнить рубли (пример: `/new 500`)\n"
        f"💵 /usdt [сумма] — пополнить USDT (пример: `/usdt 10`)\n"
        f"💸 /withdraw [сумма] [сеть] [адрес] — вывод USDT\n"
        f"🔐 /verify — ввести код верификации\n"
        f"📄 /offer — пользовательское соглашение\n"
        f"🔒 /privacy — политика конфиденциальности\n"
        f"🔄 /refund — политика возвратов\n"
    )
    if is_admin:
        text += (
            f"\n\n👑 **КОМАНДЫ АДМИНИСТРАТОРА:**\n"
            f"🔧 /admin — админ-панель\n"
            f"⏸ /stop_bot — остановить бота\n"
            f"▶️ /start_bot — запустить бота\n"
            f"💱 /setmarkup [число] — наценка к курсу\n"
            f"📊 /setfee — показать проценты\n"
            f"📊 /setfee ps [%] — комиссия ПС\n"
            f"📊 /setfee me [%] — твой процент\n"
            f"🔐 /setverifycode — код верификации\n"
            f"✅ /allow [ID] — верифицировать юзера\n"
            f"🚫 /deny [ID] — снять верификацию\n"
            f"➕ /add [ID] [сумма] — начислить\n"
            f"➖ /deduct [ID] [сумма] — списать\n"
            f"➖ /quiet_deduct [ID] [сумма] — списать тихо\n"
        )
    
    b = InlineKeyboardBuilder()
    if is_admin:
        b.row(InlineKeyboardButton(text="👑 Открыть админ-панель", callback_data="a_main"))
    b.row(InlineKeyboardButton(text="🔙 Назад в меню", callback_data="shop_main"))
    
    if edit_msg:
        try:
            await edit_msg.edit_text(text, reply_markup=b.as_markup(), parse_mode="Markdown")
            return
        except: pass
    await bot.send_message(chat_id, text, reply_markup=b.as_markup(), parse_mode="Markdown")

# === START / MENU ===
async def build_main_menu(uid):
    rate = await get_final_rate()
    v = await is_verified(uid)
    b = InlineKeyboardBuilder()
    b.button(text="🛒 Каталог товаров", callback_data="shop_catalog")
    b.button(text="💰 Пополнить баланс", callback_data="shop_deposit")
    b.button(text="👤 Профиль", callback_data="shop_profile")
    if not v: b.button(text="🔐 Ввести код верификации", callback_data="verify_enter")
    
    b.row(InlineKeyboardButton(text="📄 Соглашение", url=OFFER_URL), InlineKeyboardButton(text="🔒 Политика", url=PRIVACY_URL))
    b.row(InlineKeyboardButton(text="🔄 Политика возвратов", url=REFUND_URL))
    b.row(InlineKeyboardButton(text="📚 Все команды", callback_data="help_menu"))
    
    if v: b.adjust(1,1,1,2,1,1)
    else: b.adjust(1,1,1,1,2,1,1)
    status = "✅ Верифицирован" if v else "❌ Не верифицирован"
    return f"👋 **Добро пожаловать в {PROJECT_NAME}!**\n\nСтатус: **{status}**\nКурс: **1 USDT = {rate:.2f} ₽**\n\nВыберите действие:", b.as_markup()

@dp.message(Command("start"))
async def c_start(m: types.Message):
    t, k = await build_main_menu(m.from_user.id)
    await m.answer(t, reply_markup=k, parse_mode="Markdown")

@dp.callback_query(F.data == "help_menu")
async def cb_help_menu(cb: CallbackQuery):
    await send_help(cb.from_user.id, edit_msg=cb.message)
    await cb.answer()

@dp.callback_query(F.data == "shop_main")
async def cb_main(cb: CallbackQuery):
    t, k = await build_main_menu(cb.from_user.id)
    await cb.message.edit_text(t, reply_markup=k, parse_mode="Markdown")

@dp.callback_query(F.data == "verify_enter")
async def cb_ve(cb: CallbackQuery):
    if await is_verified(cb.from_user.id): return await cb.answer("Уже верифицированы", show_alert=True)
    pending_code_input[cb.from_user.id] = True
    await cb.message.answer("🔐 Введите код верификации одним сообщением")
    await cb.answer()

@dp.callback_query(F.data == "shop_catalog")
async def cb_cat(cb: CallbackQuery):
    async with db_pool.acquire() as c:
        cats = await c.fetch("SELECT DISTINCT category FROM products WHERE is_active=TRUE ORDER BY category")
    if not cats: return await cb.answer("Пусто", show_alert=True)
    b = InlineKeyboardBuilder()
    for x in cats: b.button(text=f"{CATEGORY_EMOJI.get(x['category'],'📦')} {x['category']}", callback_data=f"shop_cat_{x['category']}")
    b.adjust(1); b.row(InlineKeyboardButton(text="🔙", callback_data="shop_main"))
    await cb.message.edit_text("🛒 **Каталог**", reply_markup=b.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("shop_cat_"))
async def cb_sc(cb: CallbackQuery):
    cat = cb.data.replace("shop_cat_","")
    async with db_pool.acquire() as c:
        ps = await c.fetch("SELECT id,name,price FROM products WHERE category=$1 AND is_active=TRUE ORDER BY id", cat)
    if not ps: return await cb.answer("Нет товаров", show_alert=True)
    b = InlineKeyboardBuilder()
    for p in ps: b.button(text=f"{p['name']} — {p['price']:.0f}₽", callback_data=f"shop_view_{p['id']}")
    b.adjust(1); b.row(InlineKeyboardButton(text="🔙", callback_data="shop_catalog"))
    await cb.message.edit_text(f"{CATEGORY_EMOJI.get(cat,'📦')} **{cat}**", reply_markup=b.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("shop_view_"))
async def cb_sv(cb: CallbackQuery):
    pid = int(cb.data.replace("shop_view_",""))
    async with db_pool.acquire() as c: p = await c.fetchrow("SELECT * FROM products WHERE id=$1 AND is_active=TRUE", pid)
    if not p: return await cb.answer("Нет", show_alert=True)
    t = f"📦 **{p['name']}**\n\n{p['description'] or '_'}\n\n💰 **{p['price']:.0f}₽**"
    if p['stock'] > 0: t += f"\n📊 {p['stock']} шт"
    b = InlineKeyboardBuilder()
    b.button(text=f"💳 Купить", callback_data=f"shop_buy_{pid}")
    b.row(InlineKeyboardButton(text="🔙", callback_data=f"shop_cat_{p['category']}"))
    await cb.message.edit_text(t, reply_markup=b.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("shop_buy_"))
async def cb_buy(cb: CallbackQuery):
    pid = int(cb.data.replace("shop_buy_",""))
    uid = cb.from_user.id
    async with db_pool.acquire() as c:
        p = await c.fetchrow("SELECT * FROM products WHERE id=$1 AND is_active=TRUE", pid)
        if not p: return await cb.answer("Нет", show_alert=True)
        if p['stock'] == 0: return await cb.answer("Распродан", show_alert=True)
        u = await c.fetchrow("SELECT balance FROM users WHERE user_id=$1", uid)
        bal = u['balance'] if u and u['balance'] else 0.0
        if bal < p['price']: return await cb.answer(f"❌ Нужно {p['price']:.0f}₽, у вас {bal:.2f}₽", show_alert=True)
        await c.execute("UPDATE users SET balance=balance-$1 WHERE user_id=$2", p['price'], uid)
        await c.execute("INSERT INTO purchases (user_id, product_id, product_name, price) VALUES ($1,$2,$3,$4)", uid, pid, p['name'], p['price'])
        if p['stock'] > 0:
            await c.execute("UPDATE products SET stock=stock-1 WHERE id=$1", pid)
            await c.execute("UPDATE products SET is_active=FALSE WHERE id=$1 AND stock=0", pid)
    await cb.message.answer(f"✅ **Куплено!**\n📦 {p['name']}\n💰 {p['price']:.0f}₽\n\n🎁 {p['delivery_content'] or 'Свяжитесь с админом'}", parse_mode="Markdown")
    try:
        un = f"@{cb.from_user.username}" if cb.from_user.username else f"ID:{uid}"
        await bot.send_message(ADMIN_ID, f"🛒 Покупка\n{un}\n{p['name']} — {p['price']:.0f}₽", parse_mode="Markdown")
    except: pass
    await cb.answer("✅")

@dp.callback_query(F.data == "shop_deposit")
async def cb_dep(cb: CallbackQuery):
    rate = await get_final_rate()
    b = InlineKeyboardBuilder()
    b.button(text="💳 Банковская карта", callback_data="dep_card")
    b.button(text="🪙 USDT (xRocket)", callback_data="dep_usdt")
    b.row(InlineKeyboardButton(text="🔙", callback_data="shop_main"))
    v = await is_verified(cb.from_user.id)
    note = "" if v else f"\n\n_⚠️ Требуется одобрение админа._\n_Лимит без верификации: **100 - 1000 ₽**._"
    await cb.message.edit_text(f"💰 **Пополнение**\n\nКурс: 1 USDT = {rate:.2f} ₽{note}", reply_markup=b.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "dep_card")
async def cb_dc(cb: CallbackQuery):
    pending_amount_input[cb.from_user.id] = {"type": "card"}
    await cb.message.answer("💳 Введите сумму в рублях (например: `500`)", parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "dep_usdt")
async def cb_du(cb: CallbackQuery):
    pending_amount_input[cb.from_user.id] = {"type": "usdt"}
    await cb.message.answer("🪙 Введите сумму в USDT (например: `10`)", parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data == "shop_profile")
async def cb_prof(cb: CallbackQuery):
    rate = await get_final_rate()
    async with db_pool.acquire() as c:
        u = await c.fetchrow("SELECT balance, verified_go6 FROM users WHERE user_id=$1", cb.from_user.id)
        bal = u['balance'] if u and u['balance'] else 0.0
        v = "✅ Верифицирован" if u and u['verified_go6'] else "❌ Не верифицирован"
        cnt = await c.fetchval("SELECT COUNT(*) FROM purchases WHERE user_id=$1", cb.from_user.id)
    b = InlineKeyboardBuilder()
    b.button(text="📜 История покупок", callback_data="shop_history")
    b.row(InlineKeyboardButton(text="🔙", callback_data="shop_main"))
    await cb.message.edit_text(f"👤 **Профиль**\n\n🆔 `{cb.from_user.id}`\n{v}\n💰 **{bal:.2f}₽** (~{bal/rate:.2f} USDT)\n🛒 Покупок: **{cnt}**", reply_markup=b.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "shop_history")
async def cb_hist(cb: CallbackQuery):
    async with db_pool.acquire() as c:
        ps = await c.fetch("SELECT product_name, price, created_at FROM purchases WHERE user_id=$1 ORDER BY created_at DESC LIMIT 10", cb.from_user.id)
    t = "📜 **История:**\n\n" + ("Нет покупок" if not ps else "\n".join(f"• {p['product_name']} — {p['price']:.0f}₽ ({p['created_at'].strftime('%d.%m.%Y')})" for p in ps))
    b = InlineKeyboardBuilder(); b.row(InlineKeyboardButton(text="🔙", callback_data="shop_profile"))
    await cb.message.edit_text(t, reply_markup=b.as_markup(), parse_mode="Markdown")

# === ADMIN PANEL ===
@dp.message(Command("admin"))
async def c_admin(m: types.Message):
    if m.from_user.id != ADMIN_ID: return
    b = InlineKeyboardBuilder()
    b.button(text="📈 Статистика", callback_data="a_stats")
    b.button(text="👥 Юзеры", callback_data="a_users_0")
    b.button(text="📦 Товары", callback_data="a_products_0")
    b.button(text="📋 Заказы", callback_data="a_purchases_0")
    b.button(text="📚 Все команды", callback_data="help_menu")
    await m.answer("👑 **Админ:**", reply_markup=b.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data == "a_stats")
async def cb_as(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    rate = await get_final_rate(); mk = await get_current_markup()
    af = await get_admin_deposit_fee(); pf = await get_ps_fee()
    async with db_pool.acquire() as c:
        u = await c.fetchval("SELECT COUNT(*) FROM users WHERE verified_go6=TRUE") or 0
        d = await c.fetchval("SELECT COALESCE(SUM(amount),0) FROM orders WHERE status='paid'") or 0.0
        w = await c.fetchval("SELECT COALESCE(SUM(amount_usdt),0) FROM withdrawals WHERE status='completed'") or 0.0
        p = await c.fetchval("SELECT COALESCE(SUM(amount_usdt*$1),0) FROM withdrawals WHERE status!='cancelled'", ADMIN_FEE_PER_USDT) or 0.0
        pur = await c.fetchval("SELECT COUNT(*) FROM purchases") or 0
        ps = await c.fetchval("SELECT COALESCE(SUM(price),0) FROM purchases") or 0.0
        dr = await c.fetchval("SELECT COUNT(*) FROM deposit_requests WHERE status='pending'") or 0
        ab = await c.fetchval("SELECT balance FROM users WHERE user_id=$1", ADMIN_ID) or 0.0
    t = f"📊 **СТАТИСТИКА**\n\n👥 Вериф: **{u}**\n💰 Пополнено: **{d:.2f}₽**\n💸 Выведено: **{w:.2f} USDT**\n⏳ Заявок пополнения: **{dr}**\n\n🛒 Покупок: **{pur}** на **{ps:.2f}₽**\n\n👑 Доход:\n📈 С выводов: **{p:.2f}₽**\n📥 С пополнений ({af}%): **{d*af/100:.2f}₽**\n💳 Баланс: **{ab:.2f}₽**\n\n💱 Курс: Рапира+{mk}={rate:.2f}₽\n📥 ПС {pf}% | Твой {af}%"
    await cb.message.edit_text(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙", callback_data="a_main")]]), parse_mode="Markdown")

@dp.callback_query(F.data == "a_main")
async def cb_am(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    b = InlineKeyboardBuilder()
    b.button(text="📈 Статистика", callback_data="a_stats")
    b.button(text="👥 Юзеры", callback_data="a_users_0")
    b.button(text="📦 Товары", callback_data="a_products_0")
    b.button(text="📋 Заказы", callback_data="a_purchases_0")
    b.button(text="📚 Все команды", callback_data="help_menu")
    await cb.message.edit_text("👑 **Админ:**", reply_markup=b.as_markup(), parse_mode="Markdown")

# --- УПРАВЛЕНИЕ ТОВАРАМИ ---
@dp.callback_query(F.data.startswith("a_products_"))
async def cb_ap(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    pg = int(cb.data.split("_")[2]); lim, off = 8, pg*8
    async with db_pool.acquire() as c:
        ps = await c.fetch("SELECT id,name,price,is_active FROM products ORDER BY id LIMIT $1 OFFSET $2", lim, off)
        tot = await c.fetchval("SELECT COUNT(*) FROM products") or 0
    
    kb = []
    for p in ps:
        emoji = "✅" if p['is_active'] else "🚫"
        kb.append([InlineKeyboardButton(text=f"{emoji} #{p['id']} {p['name']} — {p['price']:.0f}₽", callback_data=f"a_pv_{p['id']}")])
    
    nav = []
    if pg > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"a_products_{pg-1}"))
    if off+lim < tot: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"a_products_{pg+1}"))
    if nav: kb.append(nav)
    
    kb.append([InlineKeyboardButton(text="➕ Создать товар", callback_data="a_prod_create")])
    kb.append([InlineKeyboardButton(text="🔙", callback_data="a_main")])
    
    await cb.message.edit_text(f"📦 **Управление товарами** (Стр.{pg+1}):", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("a_pv_"))
async def cb_apv(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    pid = int(cb.data.split("_")[2])
    async with db_pool.acquire() as c: p = await c.fetchrow("SELECT * FROM products WHERE id=$1", pid)
    if not p: return await cb.answer("Товар не найден", show_alert=True)
    
    t = f"📦 **{p['name']}**\n\n📁 Категория: {p['category']}\n💰 Цена: {p['price']:.0f}₽\n📝 Описание: {p['description'] or '-'}\n🎁 Выдача: {p['delivery_content'] or '-'}\nСтатус: {'✅ Вкл' if p['is_active'] else '🚫 Выкл'}"
    
    b = InlineKeyboardBuilder()
    b.button(text="✅ Выключить" if p['is_active'] else "🚫 Включить", callback_data=f"a_pt_{pid}")
    b.button(text="💰 Изменить цену", callback_data=f"a_edit_price_{pid}")
    b.button(text="📝 Изменить описание", callback_data=f"a_edit_desc_{pid}")
    b.button(text="🗑 Удалить", callback_data=f"a_del_prod_{pid}")
    b.row(InlineKeyboardButton(text="🔙 К товарам", callback_data="a_products_0"))
    
    await cb.message.edit_text(t, reply_markup=b.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("a_pt_"))
async def cb_apt(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    pid = int(cb.data.split("_")[2])
    async with db_pool.acquire() as c:
        await c.execute("UPDATE products SET is_active=NOT is_active WHERE id=$1", pid)
    await cb.answer("✅ Статус изменен")
    await cb_apv(cb)

@dp.callback_query(F.data == "a_prod_create")
async def cb_apc(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    admin_states[cb.from_user.id] = {"action": "create", "step": "category"}
    await cb.message.answer("1️⃣ Введите **категорию** товара (например: `Игры`, `Подписки`, `VPN`):", parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data.startswith("a_edit_price_"))
async def cb_aep(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    pid = int(cb.data.split("_")[3])
    admin_states[cb.from_user.id] = {"action": "edit_price", "pid": pid}
    await cb.message.answer(f"Введите **новую цену** в рублях для товара #{pid}:", parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data.startswith("a_edit_desc_"))
async def cb_aed(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    pid = int(cb.data.split("_")[3])
    admin_states[cb.from_user.id] = {"action": "edit_desc", "pid": pid}
    await cb.message.answer(f"Введите **новое описание** для товара #{pid} (можно `-`, чтобы очистить):", parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data.startswith("a_del_prod_"))
async def cb_adp(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    pid = int(cb.data.split("_")[3])
    async with db_pool.acquire() as c:
        await c.execute("DELETE FROM products WHERE id=$1", pid)
    await cb.answer("🗑 Товар удален")
    await cb_ap(cb)

# --- ЗАКАЗЫ И ЮЗЕРЫ ---
@dp.callback_query(F.data.startswith("a_purchases_"))
async def cb_apur(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    pg = int(cb.data.split("_")[2]); lim, off = 8, pg*8
    async with db_pool.acquire() as c:
        ps = await c.fetch("SELECT product_name, price, user_id, created_at FROM purchases ORDER BY created_at DESC LIMIT $1 OFFSET $2", lim, off)
        tot = await c.fetchval("SELECT COUNT(*) FROM purchases") or 0
    t = f"📋 **Заказы** (Стр.{pg+1}):\n\n" + ("Нет\n" if not ps else "\n".join(f"• {p['product_name']} — {p['price']:.0f}₽ | `{p['user_id']}`\n" for p in ps))
    nav = []
    if pg > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"a_purchases_{pg-1}"))
    if off+lim < tot: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"a_purchases_{pg+1}"))
    nav.append(InlineKeyboardButton(text="🔙", callback_data="a_main"))
    await cb.message.edit_text(t, reply_markup=InlineKeyboardMarkup(inline_keyboard=[nav]), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("a_users_"))
async def cb_au(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    pg = int(cb.data.split("_")[2]); lim, off = 5, pg*5
    async with db_pool.acquire() as c:
        us = await c.fetch("SELECT user_id, username, balance, verified_go6 FROM users ORDER BY last_active DESC LIMIT $1 OFFSET $2", lim, off)
        tot = await c.fetchval("SELECT COUNT(*) FROM users") or 0
    kb = []
    for u in us:
        bal = u['balance'] or 0.0
        nm = f"@{u['username']}" if u['username'] else f"ID:{u['user_id']}"
        v = "✅" if u['verified_go6'] else "❌"
        kb.append([InlineKeyboardButton(text=f"{v} {nm} | {bal:.0f}₽", callback_data=f"a_uv_{u['user_id']}")])
    nav = []
    if pg > 0: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"a_users_{pg-1}"))
    if off+lim < tot: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"a_users_{pg+1}"))
    if nav: kb.append(nav)
    kb.append([InlineKeyboardButton(text="🔙", callback_data="a_main")])
    await cb.message.edit_text(f"👥 Юзеры (Стр.{pg+1})", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@dp.callback_query(F.data.startswith("a_uv_"))
async def cb_auv(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    tid = int(cb.data.split("_")[2])
    async with db_pool.acquire() as c:
        u = await c.fetchrow("SELECT * FROM users WHERE user_id=$1", tid)
        if not u: return await cb.answer("Нет", show_alert=True)
        ti = await c.fetchval("SELECT COALESCE(SUM(amount),0) FROM orders WHERE user_id=$1 AND status='paid'", tid) or 0.0
        pc = await c.fetchval("SELECT COUNT(*) FROM purchases WHERE user_id=$1", tid) or 0
    bal = u['balance'] or 0.0
    nm = f"@{u['username']}" if u['username'] else "?"
    v = "✅" if u['verified_go6'] else "❌"
    t = f"👤 **{tid}**\n{nm}\nВерификация: {v}\n🕒 {fmt_seen(u['last_active'])}\n💰 {bal:.2f}₽\n📥 Пополнил: {ti:.2f}₽\n🛒 {pc} покупок"
    b = InlineKeyboardBuilder()
    b.row(InlineKeyboardButton(text="✅ Вериф" if not u['verified_go6'] else "❌ Де-вериф", callback_data=f"a_vt_{tid}"))
    b.row(InlineKeyboardButton(text="📝 Списать", callback_data=f"a_di_{tid}"), InlineKeyboardButton(text="🔙", callback_data="a_users_0"))
    await cb.message.edit_text(t, reply_markup=b.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("a_vt_"))
async def cb_avt(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    tid = int(cb.data.split("_")[2])
    async with db_pool.acquire() as c:
        cur = await c.fetchrow("SELECT verified_go6 FROM users WHERE user_id=$1", tid)
        new = not (cur and cur['verified_go6'])
        await c.execute("UPDATE users SET verified_go6=$1 WHERE user_id=$2", new, tid)
    await cb.answer("✅" if new else "🚫")
    await cb_auv(cb)

@dp.callback_query(F.data.startswith("a_di_"))
async def cb_adi(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    tid = int(cb.data.split("_")[2])
    await cb.message.answer(f"`/deduct {tid} 500` — с уведомлением\n`/quiet_deduct {tid} 500` — без", parse_mode="Markdown")
    await cb.answer()

@dp.callback_query(F.data.startswith("wt_take_"))
async def cb_wtt(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    wid = int(cb.data.split("_")[2])
    async with db_pool.acquire() as c: await c.execute("UPDATE withdrawals SET status='processing' WHERE id=$1", wid)
    b = InlineKeyboardBuilder()
    b.button(text="✅ Выполнено", callback_data=f"wt_done_{wid}")
    b.button(text="🔴 Отклонить", callback_data=f"wt_no_{wid}")
    await cb.message.edit_reply_markup(reply_markup=b.as_markup())

@dp.callback_query(F.data.startswith("wt_no_"))
async def cb_wtn(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    wid = int(cb.data.split("_")[2])
    async with db_pool.acquire() as c:
        async with c.transaction():
            r = await c.fetchrow("SELECT user_id, amount_rub, amount_usdt FROM withdrawals WHERE id=$1", wid)
            await c.execute("UPDATE withdrawals SET status='rejected' WHERE id=$1", wid)
            await c.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", r['amount_rub'], r['user_id'])
            await c.execute("UPDATE users SET balance=balance-$1 WHERE user_id=$2", r['amount_usdt']*ADMIN_FEE_PER_USDT, ADMIN_ID)
    await cb.message.edit_text(f"🔴 #{wid} отклонена")

@dp.callback_query(F.data.startswith("wt_done_"))
async def cb_wtd(cb: CallbackQuery):
    if cb.from_user.id != ADMIN_ID: return
    wid = int(cb.data.split("_")[2])
    async with db_pool.acquire() as c: await c.execute("UPDATE withdrawals SET status='completed' WHERE id=$1", wid)
    await cb.message.edit_text(f"✅ #{wid} выполнена")

# === WEBHOOKS ===
async def wh_rolly(request: web.Request):
    try:
        raw = await request.read()
        sig = request.headers.get("X-Signature"); ts = request.headers.get("X-Timestamp")
        exp = hmac.new(SIGNING_SECRET.encode(), f"{ts}.{raw.decode()}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, exp): return web.Response(text="bad", status=403)
        data = json.loads(raw)
        if data.get("status") == "paid":
            async with db_pool.acquire() as c:
                row = await c.fetchrow("UPDATE orders SET status='paid' WHERE payment_id=$1 AND status='pending' RETURNING id,user_id,amount,comment,message_id", data.get("payment_id"))
                if row:
                    cid, uid, amt = row['id'], row['user_id'], row['amount']
                    ur = await c.fetchrow("SELECT username FROM users WHERE user_id=$1", uid)
                    un = f"@{ur['username']}" if ur and ur['username'] else f"ID:{uid}"
                    af = await get_admin_deposit_fee(); pf = await get_ps_fee()
                    fps = amt*(pf/100); fad = amt*(af/100); net = amt-fps-fad
                    await c.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", net, uid)
                    await c.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", fad, ADMIN_ID)
                    if row['message_id']:
                        try: await bot.send_message(uid, "Оплата произведена", reply_to_message_id=row['message_id'], allow_sending_without_reply=True)
                        except:
                            try: await bot.send_message(uid, "Оплата произведена")
                            except: pass
                    try: await bot.send_message(ADMIN_ID, f"🔔 Чек #{cid}\n{un}\n💵 {amt:.2f}₽\n🏦 ПС {fps:.2f}₽\n👑 Тебе {fad:.2f}₽\n👤 Юзеру {net:.2f}₽", parse_mode="Markdown")
                    except: pass
        return web.Response(text="OK")
    except Exception as e:
        logging.error(f"wh: {e}"); return web.Response(text="err", status=500)

async def wh_xr(request: web.Request):
    try:
        raw = await request.read()
        sig = request.headers.get("Signature"); sts = request.headers.get("Signature-Timestamp")
        exp = hmac.new(XROCKET_WEBHOOK_SECRET.encode(), f"{sts}.{raw.decode()}".encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, exp): return web.Response(text="bad", status=401)
        data = json.loads(raw)
        if data.get("type") == "invoice" and data.get("data",{}).get("status") == "paid":
            payload = data["data"].get("payload")
            amt_usdt = float(data["data"].get("amount", 0))
            if payload:
                try: uid, cid = map(int, payload.split("_"))
                except: uid, cid = int(payload), 0
                async with db_pool.acquire() as c:
                    row = None
                    if cid > 0: row = await c.fetchrow("UPDATE orders SET status='paid' WHERE id=$1 AND status='pending' RETURNING message_id", cid)
                    if cid > 0 and row is None: return web.Response(text="OK")
                    rate = await get_final_rate(); rub = amt_usdt*rate
                    ur = await c.fetchrow("SELECT username FROM users WHERE user_id=$1", uid)
                    un = f"@{ur['username']}" if ur and ur['username'] else f"ID:{uid}"
                    af = await get_admin_deposit_fee(); pf = await get_ps_fee()
                    fps = rub*(pf/100); fad = rub*(af/100); net = rub-fps-fad
                    await c.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", net, uid)
                    await c.execute("UPDATE users SET balance=balance+$1 WHERE user_id=$2", fad, ADMIN_ID)
                    mid = row['message_id'] if row else None
                if mid:
                    try: await bot.send_message(uid, "Оплата произведена", reply_to_message_id=mid, allow_sending_without_reply=True)
                    except:
                        try: await bot.send_message(uid, "Оплата произведена")
                        except: pass
                else:
                    try: await bot.send_message(uid, "Оплата произведена")
                    except: pass
                try: await bot.send_message(ADMIN_ID, f"🔔 Чек #{cid} (xR)\n{un}\n💵 {amt_usdt} USDT ({rub:.2f}₽)\n🏦 {fps:.2f}₽\n👑 {fad:.2f}₽\n👤 {net:.2f}₽", parse_mode="Markdown")
                except: pass
        return web.Response(text="OK")
    except Exception as e:
        logging.error(f"whxr: {e}"); return web.Response(text="err", status=500)

# === 

async def health_check(request: web.Request):
    return web.Response(text="OK", status=200)


async def run_polling():
    """Запускает polling и переживает временные ошибки Telegram API."""
    delay = 5
    while True:
        try:
            await bot.delete_webhook(drop_pending_updates=False)
            logging.info("Запускаю Telegram polling")
            await dp.start_polling(
                bot,
                handle_signals=False,
                close_bot_session=False,
                drop_pending_updates=False,
            )
            return
        except TelegramRetryAfter as exc:
            delay = max(float(exc.retry_after), 1.0) + 1.0
            logging.warning("Telegram ограничил запросы; повтор через %.1f сек.", delay)
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("Ошибка Telegram polling; повтор через %s сек.", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)


async def main():
    global rolly_client, rocket_client

    runner = None
    try:
        await init_db()
        rolly_client = RollyPayClient(api_key=ROLLYPAY_API_KEY)
        rocket_client = xRocketClient(api_key=XROCKET_API_KEY)

        app = web.Application()
        app.router.add_get("/", health_check, allow_head=True)
        app.router.add_get("/health", health_check)
        app.router.add_post("/rollypay-webhook", wh_rolly)
        app.router.add_post("/xrocket-webhook", wh_xr)

        render_url = os.getenv("RENDER_EXTERNAL_URL")
        port = int(os.getenv("PORT", str(WEBHOOK_PORT)))
        runner = web.AppRunner(app)

        # Telegram webhook включается только при явном BOT_MODE=webhook.
        # По умолчанию используется polling: это обходит ситуацию, когда
        # Telegram показывает установленный webhook, но не присылает POST.
        if BOT_MODE == "webhook":
            if not render_url:
                raise RuntimeError("BOT_MODE=webhook требует RENDER_EXTERNAL_URL")
            webhook_path = "/telegram-webhook"
            webhook_url = f"{render_url.rstrip('/')}{webhook_path}"
            webhook_secret = os.getenv("WEBHOOK_SECRET")
            if not webhook_secret:
                raise RuntimeError("Для BOT_MODE=webhook задайте WEBHOOK_SECRET в Render")

            SimpleRequestHandler(
                dispatcher=dp,
                bot=bot,
                secret_token=webhook_secret,
                handle_in_background=False,
            ).register(app, path=webhook_path)

        await runner.setup()
        site = web.TCPSite(runner, host="0.0.0.0", port=port)
        await site.start()
        logging.info("HTTP-сервер запущен на порту %s; режим Telegram: %s", port, BOT_MODE)

        if BOT_MODE == "webhook":
            await bot.set_webhook(
                url=webhook_url,
                secret_token=webhook_secret,
                allowed_updates=dp.resolve_used_update_types(),
                drop_pending_updates=False,
            )
            info = await bot.get_webhook_info()
            logging.info(
                "Telegram webhook: url=%s pending=%s last_error=%s",
                info.url,
                info.pending_update_count,
                info.last_error_message,
            )
            await asyncio.Event().wait()
        else:
            # Важно: polling и webhook одного токена одновременно работать не могут.
            await run_polling()

    except Exception:
        logging.exception("Критическая ошибка запуска приложения")
        raise
    finally:
        if runner is not None:
            await runner.cleanup()
        try:
            await bot.session.close()
        except Exception:
            logging.exception("Ошибка закрытия Telegram-сессии")
        if db_pool is not None:
            await db_pool.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s"
    )

    asyncio.run(main())
