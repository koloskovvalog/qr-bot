import asyncio
import logging
import asyncpg
from datetime import datetime, timezone
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, ChatMemberUpdated
from aiohttp import web, ClientSession
from aiorocket2 import xRocketClient
import os
import hmac
import hashlib
import json
import time
import random

# --- НАСТРОЙКИ ---
BOT_TOKEN = "9037852041:AAGlmd1cTt0Mbc9CM6t-kYJimE86h2cRiEo"

# --- COINSO НАСТРОЙКИ ---
COINSO_PROJECT_ID = 510249038
COINSO_SECRET_KEY = "79b0c859ec27a097a9663fa2193f8ea6"
COINSO_API_URL = "https://api.coinso.io/payment/create" 

# --- XROCKET PAY API ---
XROCKET_API_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJhcHBJZCI6IjMwNzc2NSIsImp0aSI6ImFwcDozMDc3NjU6YWMzYjUyODgtYWI3MS00MzUyLTgzNWEtYjRmNjVjZTMyNGQwIiwiaWF0IjoxNzkzNzE1MjA4fQ.nBCxGBwKmyaKjt4cW_jrtLBlagXxGQbvgD5c14yT2TI"
XROCKET_WEBHOOK_SECRET = "74524a9673bb8d1f18dd00eaf22eecd7daf75fbfefd664d1389cefaead0a6673"

DATABASE_URL = "postgresql://postgres.qrfqdihhoqvlaqkuyols:Koloskov2006@aws-0-eu-west-1.pooler.supabase.com:5432/postgres"

GO6_REF_LINK = "https://t.me/Go6online_bot?start=verify_XE732NNY" 

MIN_TRC20_USDT = 10.0
MIN_BEP20_USDT = 50.0

ADMIN_FEE_PER_USDT = 1.0
ADMIN_ID = 7653934214

# ID ВАШЕГО ЧАТА (замените на свой, если еще не сделали)
CHAT_ID = -1001234567890 

logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

rocket_client = None
active_games = {}
db_pool: asyncpg.Pool = None

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
        if row:
            return float(row['value'])
        return 2.0

async def get_global_fee():
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = 'global_fee'")
        if row:
            return float(row['value'])
        return 0.0

async def get_final_rate():
    base = await fetch_base_rate()
    markup = await get_current_markup()
    return base + markup

async def init_db():
    global db_pool
    db_pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, ssl="require", statement_cache_size=0)
    async with db_pool.acquire() as conn:
        await conn.execute('''CREATE TABLE IF NOT EXISTS users (user_id BIGINT PRIMARY KEY, balance REAL DEFAULT 0, last_active TIMESTAMPTZ DEFAULT NOW());''')
        await conn.execute('''ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active TIMESTAMPTZ DEFAULT NOW();''')
        await conn.execute('''ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT;''')
        await conn.execute('''ALTER TABLE users ADD COLUMN IF NOT EXISTS verified_go6 BOOLEAN DEFAULT FALSE;''')
        await conn.execute('''ALTER TABLE users ADD COLUMN IF NOT EXISTS custom_fee REAL DEFAULT NULL;''')
        
        await conn.execute('''CREATE TABLE IF NOT EXISTS orders (id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount REAL, payment_id TEXT UNIQUE, status TEXT, order_type TEXT DEFAULT 'deposit', comment TEXT, message_id BIGINT, created_at TIMESTAMPTZ DEFAULT NOW());''')
        await conn.execute('''ALTER TABLE orders ADD COLUMN IF NOT EXISTS comment TEXT;''')
        await conn.execute('''ALTER TABLE orders ADD COLUMN IF NOT EXISTS message_id BIGINT;''')
        
        await conn.execute('''CREATE TABLE IF NOT EXISTS allowed_users (user_id BIGINT PRIMARY KEY);''')
        
        await conn.execute('''CREATE TABLE IF NOT EXISTS withdrawals (id BIGSERIAL PRIMARY KEY, user_id BIGINT, amount_usdt REAL, amount_rub REAL, address TEXT, status TEXT DEFAULT 'pending', network TEXT DEFAULT 'BEP20', created_at TIMESTAMPTZ DEFAULT NOW());''')
        await conn.execute('''ALTER TABLE withdrawals ADD COLUMN IF NOT EXISTS network TEXT DEFAULT 'BEP20';''')
        
        await conn.execute('''
            CREATE TABLE IF NOT EXISTS game_bets (
                id BIGSERIAL PRIMARY KEY, user_id BIGINT, bet_usdt REAL, bet_rub REAL,
                win_usdt REAL DEFAULT 0, multiplier REAL DEFAULT 0, status TEXT, created_at TIMESTAMPTZ DEFAULT NOW()
            );
        ''')
        
        await conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);''')
        await conn.execute("INSERT INTO settings (key, value) VALUES ('bot_status', 'active') ON CONFLICT (key) DO NOTHING;")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('markup', '2.0') ON CONFLICT (key) DO NOTHING;")
        await conn.execute("INSERT INTO settings (key, value) VALUES ('global_fee', '0.0') ON CONFLICT (key) DO NOTHING;")
        
        await conn.execute("INSERT INTO allowed_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", ADMIN_ID)
        await conn.execute("INSERT INTO users (user_id, last_active, balance) VALUES ($1, NOW(), 0) ON CONFLICT (user_id) DO NOTHING", ADMIN_ID)
    logging.info("БД Supabase готова!")

async def is_user_allowed(user_id: int) -> bool:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM allowed_users WHERE user_id = $1", user_id)
        return row is not None

async def get_bot_status() -> str:
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT value FROM settings WHERE key = 'bot_status'")
        return row['value'] if row else 'active'

def format_last_seen(last_active: datetime):
    if not last_active: return "Неизвестно"
    if last_active.tzinfo is None: last_active = last_active.replace(tzinfo=timezone.utc)
    seconds = (datetime.now(timezone.utc) - last_active).total_seconds()
    if seconds < 60: return "🟢 Только что (В сети)"
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
    if event.forward_origin:
        return
    return await handler(event, data)

@dp.message.middleware()
@dp.callback_query.middleware()
async def maintenance_middleware(handler, event, data):
    if event.from_user and event.from_user.id == ADMIN_ID:
        return await handler(event, data)
    
    status = await get_bot_status()
    if status == 'stopped':
        if isinstance(event, types.Message):
            await event.answer("🛠 **Бот временно на техническом обслуживании.**\n\nПожалуйста, зайдите позже. Приносим извинения за неудобства!", parse_mode="Markdown")
        elif isinstance(event, types.CallbackQuery):
            await event.answer("🛠 Бот на техобслуживании. Зайдите позже.", show_alert=True)
        return
    
    return await handler(event, data)

@dp.errors()
async def errors_handler(update: types.Update, exception: Exception):
    logging.error(f"Update {update} caused error {exception}")
    if update.callback_query:
        try:
            await update.callback_query.message.answer(f"❌ **Системная ошибка:**\n`{exception}`", parse_mode="Markdown")
            await update.callback_query.answer("Ошибка сервера")
        except: pass
    return True

# ==================== ОТСЛЕЖИВАНИЕ ВЫХОДА ИЗ ЧАТА ====================
@dp.chat_member()
async def on_chat_member_updated(event: ChatMemberUpdated):
    if event.chat.id != CHAT_ID:
        return
        
    user_id = event.new_chat_member.user.id
    
    if event.new_chat_member.status in ("left", "kicked"):
        async with db_pool.acquire() as conn:
            user = await conn.fetchrow("SELECT 1 FROM allowed_users WHERE user_id = $1", user_id)
            if user:
                await conn.execute("DELETE FROM allowed_users WHERE user_id = $1", user_id)
                try:
                    await bot.send_message(ADMIN_ID, f"⚠️ Пользователь `{user_id}` вышел/удален из чата.\nДоступ к боту автоматически закрыт.", parse_mode="Markdown")
                except:
                    pass

# ==================== УПРАВЛЕНИЕ БОТОМ И НАСТРОЙКАМИ ====================
@dp.message(Command("stop_bot"))
async def cmd_stop_bot(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE settings SET value = 'stopped' WHERE key = 'bot_status'")
    await message.answer("⏸ **Бот остановлен.** Пользователи видят сообщение о техобслуживании.")

@dp.message(Command("start_bot"))
async def cmd_start_bot(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE settings SET value = 'active' WHERE key = 'bot_status'")
    await message.answer("▶️ **Бот снова включен.** Пользователи могут пользоваться ботом.")

@dp.message(Command("setmarkup"))
async def cmd_setmarkup(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args:
        return await message.answer("Используйте: `/setmarkup [сумма в рублях]`\nПример: `/setmarkup 2` или `/setmarkup 1.5`")
    
    try:
        markup = float(command.args.replace(',', '.'))
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE settings SET value = $1 WHERE key = 'markup'", str(markup))
        await message.answer(f"✅ Наценка к курсу Рапиры установлена: **+{markup} ₽**", parse_mode="Markdown")
    except ValueError:
        await message.answer("❌ Ошибка! Введите число (например, 2 или 2.5).")

@dp.message(Command("setfee"))
async def cmd_setfee(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args:
        return await message.answer("Используйте:\n`/setfee [процент]` (общая для всех)\n`/setfee [ID] [процент]` (для конкретного юзера)")
    
    try:
        args = command.args.split()
        
        if len(args) == 1:
            percent = float(args[0].replace(',', '.'))
            async with db_pool.acquire() as conn:
                await conn.execute("UPDATE settings SET value = $1 WHERE key = 'global_fee'", str(percent))
            await message.answer(f"✅ **Общая комиссия для всех** установлена: **{percent}%**", parse_mode="Markdown")
            
        elif len(args) == 2:
            target_id = int(args[0])
            percent = float(args[1].replace(',', '.'))
            
            async with db_pool.acquire() as conn:
                user = await conn.fetchrow("SELECT 1 FROM users WHERE user_id = $1", target_id)
                if not user:
                    return await message.answer("❌ Пользователь не найден в базе.")
                
                await conn.execute("UPDATE users SET custom_fee = $1 WHERE user_id = $2", percent, target_id)
                
            await message.answer(f"✅ Для пользователя `{target_id}` установлена **индивидуальная** комиссия: **{percent}%**", parse_mode="Markdown")
        else:
            await message.answer("❌ Неверный формат.")
    except ValueError:
        await message.answer("❌ Ошибка! Проверьте правильность ввода.")

# ==================== БЕЛЫЙ СПИСОК ====================
@dp.message(Command("allow"))
async def cmd_allow(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: 
        return await message.answer("Укажите ID: `/allow 123456789`")
    
    try:
        new_user_id = int(command.args.strip())
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO allowed_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", new_user_id)
            await conn.execute("INSERT INTO users (user_id, last_active) VALUES ($1, NOW()) ON CONFLICT DO NOTHING", new_user_id)
        await message.answer(f"✅ Пользователь `{new_user_id}` добавлен в белый список.")
    except ValueError: 
        await message.answer("❌ Ошибка! ID должен состоять только из цифр.")
    except Exception as e:
        await message.answer(f"❌ Ошибка базы: {e}")

@dp.message(Command("deny"))
async def cmd_deny(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args: 
        return await message.answer("Укажите ID: `/deny 123456789`")
    
    try:
        target_id = int(command.args.strip())
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM allowed_users WHERE user_id = $1", target_id)
        await message.answer(f"🚫 Пользователь `{target_id}` заблокирован.")
    except ValueError: 
        await message.answer("❌ Ошибка! ID должен состоять только из цифр.")

# ==================== РУЧНОЕ СПИСАНИЕ ====================
@dp.message(Command("deduct"))
async def cmd_deduct(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args or len(command.args.split()) < 2:
        return await message.answer("Используйте: `/deduct [ID] [Сумма]`")
    
    try:
        target_id, amount = int(command.args.split()[0]), float(command.args.split()[1])
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", amount, target_id)
        await message.answer(f"✅ Списано **{amount:.2f} ₽** (с уведомлением).", parse_mode="Markdown")
        try: 
            await bot.send_message(target_id, f"⚠️ **Администратор списал с вашего баланса {amount:.2f} ₽.**", parse_mode="Markdown")
        except: pass
    except: pass

@dp.message(Command("quiet_deduct"))
async def cmd_quiet_deduct(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    if not command.args or len(command.args.split()) < 2:
        return await message.answer("Используйте: `/quiet_deduct [ID] [Сумма]`")
    
    try:
        target_id, amount = int(command.args.split()[0]), float(command.args.split()[1])
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", amount, target_id)
        await message.answer(f"✅ Списано **{amount:.2f} ₽** (без уведомления).", parse_mode="Markdown")
    except: pass

# ==================== ИГРА КРАШ ====================
@dp.message(Command("crash"))
async def cmd_crash(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id): return
    user_id = message.from_user.id
    if user_id in active_games: return await message.answer("❌ Вы уже играете!")
    if not command.args: return await message.answer("Укажите ставку: `/crash 1`")
    try:
        bet_usdt = float(command.args.replace(',', '.'))
        if bet_usdt <= 0: raise ValueError
    except: return await message.answer("Ошибка! Неверная сумма.")

    rate = await get_final_rate()
    bet_rub = bet_usdt * rate

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
        if not row or row['balance'] < bet_rub: return await message.answer(f"❌ Недостаточно средств! Нужно: {bet_rub:.2f} ₽")
        await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", bet_rub, user_id)
        bet_id = await conn.fetchval("INSERT INTO game_bets (user_id, bet_usdt, bet_rub, status) VALUES ($1, $2, $3, 'active') RETURNING id", user_id, bet_usdt, bet_rub)

    r = random.uniform(0.01, 1.0)
    crash_point = max(1.1, 0.90 / r)

    active_games[user_id] = {'multiplier': 1.0, 'bet_usdt': bet_usdt, 'rate': rate, 'crash_point': crash_point, 'bet_id': bet_id}
    
    builder = InlineKeyboardBuilder()
    builder.button(text="💰 ЗАБРАТЬ (1.0x)", callback_data=f"crash_cashout_{user_id}")
    msg = await message.answer(f"🚀 **Игра началась!**\nСтавка: {bet_usdt} USDT\n\nМножитель: **1.0x**", reply_markup=builder.as_markup(), parse_mode="Markdown")
    asyncio.create_task(run_crash_game(msg.chat.id, msg.message_id, user_id))

async def run_crash_game(chat_id: int, message_id: int, user_id: int):
    if user_id not in active_games: return
    game = active_games[user_id]
    try:
        while True:
            await asyncio.sleep(1.0)
            if user_id not in active_games: break
            game['multiplier'] += 0.1
            current_mult = game['multiplier']
            
            if current_mult >= game['crash_point']:
                active_games.pop(user_id)
                async with db_pool.acquire() as conn:
                    await conn.execute("UPDATE game_bets SET status = 'lost', multiplier = $1 WHERE id = $2", current_mult, game['bet_id'])
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"💥 **КРАШ!** Множитель лопнул на **{current_mult:.1f}x**.\nВы проиграли {game['bet_usdt']} USDT.", parse_mode="Markdown")
                break
            
            win_usdt = game['bet_usdt'] * current_mult
            win_rub = win_usdt * game['rate']
            builder = InlineKeyboardBuilder()
            builder.button(text=f"💰 ЗАБРАТЬ ({win_usdt:.2f} USDT)", callback_data=f"crash_cashout_{user_id}")
            try:
                await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=f"🚀 **Множитель: {current_mult:.1f}x**\nВаш выигрыш: {win_usdt:.2f} USDT ({win_rub:.2f} ₽)", reply_markup=builder.as_markup(), parse_mode="Markdown")
            except: pass
    except Exception as e: logging.error(f"Ошибка в игре: {e}")

@dp.callback_query(F.data.startswith("crash_cashout_"))
async def cb_crash_cashout(callback: CallbackQuery):
    user_id = int(callback.data.split("_")[2])
    if callback.from_user.id != user_id: return await callback.answer("Это не ваша игра!", show_alert=True)
    if user_id in active_games:
        game = active_games.pop(user_id)
        multiplier = game['multiplier']
        win_usdt = game['bet_usdt'] * multiplier
        win_rub = win_usdt * game['rate']
        
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", win_rub, user_id)
            await conn.execute("UPDATE game_bets SET status = 'won', win_usdt = $1, multiplier = $2 WHERE id = $3", win_usdt, multiplier, game['bet_id'])
            
        await callback.message.edit_text(f"✅ **Вы забрали выигрыш!**\nМножитель: {multiplier:.1f}x\nВыигрыш: +{win_rub:.2f} ₽ ({win_usdt:.2f} USDT)", parse_mode="Markdown")
        await callback.answer("Выигрыш зачислен!")
    else: await callback.answer("Игра уже завершена.", show_alert=True)

# ==================== ПОПОЛНЕНИЕ ЧЕРЕЗ COINSO ====================
@dp.message(Command("new"))
async def cmd_new(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id): return
    if not command.args: return await message.answer("Укажите сумму: `/new 500`")
    
    parts = command.args.split(maxsplit=1)
    try: amount = float(parts[0].replace(',', '.'))
    except: return await message.answer("Ошибка! Неверная сумма.")
    
    comment = parts[1] if len(parts) > 1 else None

    try:
        async with db_pool.acquire() as conn:
            check_id = await conn.fetchval(
                "INSERT INTO orders (user_id, amount, status, comment) VALUES ($1, $2, 'pending', $3) RETURNING id",
                message.from_user.id, amount, comment
            )
            
        desc_text = f"Чек #{check_id}"
        if comment: 
            desc_text += f" | {comment}"

        headers = {
            "Authorization": f"Bearer {COINSO_SECRET_KEY}",
            "Content-Type": "application/json"
        }
        payload = {
            "project_id": COINSO_PROJECT_ID,
            "amount": amount,
            "description": desc_text,
            "custom": str(check_id), 
            "client_telegram_id": str(message.from_user.id),
            "success_url": "https://t.me/QR1Checkbot"
        }

        async with ClientSession() as session:
            async with session.post(COINSO_API_URL, json=payload, headers=headers) as resp:
                resp_data = await resp.json()
                
                if resp.status == 200 and resp_data.get("success"):
                    payment_url = resp_data.get("payment_url")
                    invoice_id = resp_data.get("invoice_id")
                    
                    async with db_pool.acquire() as conn:
                        await conn.execute("UPDATE orders SET payment_id = $1 WHERE id = $2", invoice_id, check_id)
                        
                    builder = InlineKeyboardBuilder()
                    builder.button(text="💳 Перейти к оплате", url=payment_url)
                    
                    msg_text = f"**Счет: {amount:.2f} ₽**"
                    if comment: 
                        msg_text += f"\n📝 Название: {comment}"
                        
                    sent_msg = await message.answer(f"{msg_text}\n🟡 Ожидает оплату...", reply_markup=builder.as_markup(), parse_mode="Markdown")
                    
                    async with db_pool.acquire() as conn:
                        await conn.execute("UPDATE orders SET message_id = $1 WHERE id = $2", sent_msg.message_id, check_id)
                else:
                    logging.error(f"Coinso Error: {resp_data}")
                    await message.answer(f"❌ Ошибка создания счета: {resp_data.get('message', 'Неизвестная ошибка')}")
                    
    except Exception as e: 
        logging.error(f"Coinso Request Error: {e}")
        await message.answer("Ошибка создания платежа. Попробуйте позже.")

# ==================== ВЕРИФИКАЦИЯ ====================
@dp.message(F.photo)
async def handle_verification_photo(message: types.Message):
    if await is_user_allowed(message.from_user.id): return
    user = message.from_user
    async with db_pool.acquire() as conn:
        await conn.execute('''INSERT INTO users (user_id, last_active, username) VALUES ($1, NOW(), $2) ON CONFLICT (user_id) DO UPDATE SET last_active = NOW(), username = $2''', user.id, user.username)
    admin_builder = InlineKeyboardBuilder()
    admin_builder.button(text="✅ Одобрить", callback_data=f"approve_access_{user.id}")
    admin_builder.button(text="❌ Отклонить", callback_data=f"reject_access_{user.id}")
    try:
        await bot.send_photo(chat_id=ADMIN_ID, photo=message.photo[-1].file_id, caption=f"🔔 **ЗАЯВКА**\n👤 {user.full_name}\n🔗 @{user.username}\n🆔 `{user.id}`", reply_markup=admin_builder.as_markup(), parse_mode="Markdown")
        await message.answer("⏳ Ожидайте проверки.")
    except: pass

@dp.callback_query(F.data.startswith("approve_access_"))
async def process_approve_access(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO allowed_users (user_id) VALUES ($1) ON CONFLICT DO NOTHING", target_id)
        await conn.execute("UPDATE users SET verified_go6 = TRUE WHERE user_id = $1", target_id)
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n✅ **ОДОБРЕНО**", reply_markup=None)
    try: await bot.send_message(target_id, "✅ Доступ одобрен! Напишите /start")
    except: pass

@dp.callback_query(F.data.startswith("reject_access_"))
async def process_reject_access(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[2])
    await callback.message.edit_caption(caption=callback.message.caption + "\n\n❌ **ОТКЛОНЕНО**", reply_markup=None)
    try: await bot.send_message(target_id, "❌ В доступе отказано.")
    except: pass

# ==================== КОМАНДЫ ПОЛЬЗОВАТЕЛЯ ====================
@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not await is_user_allowed(message.from_user.id):
        return await message.answer(f"👋 Для доступа:\n1️⃣ Регистрация на GO6: {GO6_REF_LINK}\n2️⃣ Укажите ментора.\n3️⃣ Отправьте скрин сюда.")
    rate = await get_final_rate()
    await message.answer(f"Привет! Текущий курс: **1 USDT = {rate:.2f} ₽**\n\n💰 /balance — баланс\n📥 /new [сумма] — пополнить (Рубли)\n💸 /withdraw — вывод\n🎮 /crash [сумма_USDT] — играть", parse_mode="Markdown")

@dp.message(Command("balance"))
async def cmd_balance(message: types.Message):
    if not await is_user_allowed(message.from_user.id): return
    rate = await get_final_rate()
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", message.from_user.id)
        bal = row['balance'] if row and row['balance'] is not None else 0.0
    await message.answer(f"💰 **Баланс:** {bal:.2f} ₽\n({bal / rate:.2f} USDT)", parse_mode="Markdown")

# ==================== ВЫВОД СРЕДСТВ ====================
@dp.message(Command("withdraw"))
async def cmd_withdraw(message: types.Message, command: CommandObject):
    if not await is_user_allowed(message.from_user.id): return
    if not command.args or len(command.args.split()) < 3: return await message.answer("`/withdraw [сумма] [сеть] [адрес]`")
    try:
        args = command.args.split()
        amount_usdt = float(args[0].replace(',', '.'))
        network = args[1].upper().strip()
        address = args[2].strip()
        if network not in ['BEP20', 'TRC20']: return await message.answer("❌ Неверная сеть.")
        if network == 'TRC20' and amount_usdt < MIN_TRC20_USDT: return await message.answer(f"❌ Мин. TRC20: {MIN_TRC20_USDT} USDT")
        if network == 'BEP20' and amount_usdt < MIN_BEP20_USDT: return await message.answer(f"❌ Мин. BEP20: {MIN_BEP20_USDT} USDT")
    except: return await message.answer("Ошибка!")

    fee = calculate_network_fee(network, amount_usdt)
    total_usdt = amount_usdt + fee
    rate = await get_final_rate()
    total_rub = total_usdt * rate
    profit_rub = amount_usdt * ADMIN_FEE_PER_USDT

    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", message.from_user.id)
        current_bal = row['balance'] if row and row['balance'] is not None else 0.0
        
        if current_bal < total_rub: 
            return await message.answer("❌ Недостаточно средств.")
            
        today_wths = await conn.fetchval(
            "SELECT COALESCE(SUM(amount_rub), 0) FROM withdrawals WHERE user_id = $1 AND status IN ('pending', 'processing', 'completed') AND created_at >= CURRENT_DATE",
            message.from_user.id
        )
        
        max_limit_today = current_bal / 2.0
        
        if today_wths + total_rub > max_limit_today:
            return await message.answer(
                f"❌ **Превышен дневной лимит на вывод!**\n\n"
                f"Правило: не более 50% от текущего баланса в сутки.\n"
                f"Ваш баланс: **{current_bal:.2f} ₽**\n"
                f"Лимит на сегодня: **{max_limit_today:.2f} ₽**\n"
                f"Вы уже вывели сегодня: **{today_wths:.2f} ₽**\n"
                f"Сейчас пытаетесь вывести: **{total_rub:.2f} ₽**",
                parse_mode="Markdown"
            )

        await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", total_rub, message.from_user.id)
        
        w_id = await conn.fetchval("INSERT INTO withdrawals (user_id, amount_usdt, amount_rub, address, network) VALUES ($1, $2, $3, $4, $5) RETURNING id", message.from_user.id, amount_usdt, total_rub, address, network)
        
        await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", profit_rub, ADMIN_ID)

    await message.answer(f"✅ **Заявка #{w_id}**\nСеть: {network}\nК получению: {amount_usdt:.2f} USDT\nСписано: {total_rub:.2f} ₽")
    builder = InlineKeyboardBuilder()
    builder.button(text="🟢 В работу", callback_data=f"admin_take_{w_id}")
    builder.button(text="🔴 Отклонить", callback_data=f"admin_reject_{w_id}")
    await bot.send_message(ADMIN_ID, f"💸 **ЗАЯВКА #{w_id}**\nID: `{message.from_user.id}`\nСеть: {network}\nUSDT: {amount_usdt}\nАдрес: `{address}`\nПрибыль: +{profit_rub} ₽", reply_markup=builder.as_markup())

# ==================== АДМИН-ПАНЕЛЬ ====================
@dp.message(Command("admin"))
async def cmd_admin(message: types.Message):
    if message.from_user.id != ADMIN_ID: return
    builder = InlineKeyboardBuilder()
    builder.button(text="📈 Статистика", callback_data="admin_stats")
    builder.button(text="👥 Все пользователи", callback_data="admin_users_0")
    builder.button(text="🛡️ Верифицированные (GO6)", callback_data="admin_verified_0")
    await message.answer("👑 **Админ-панель:**", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.message(Command("add"))
async def cmd_add(message: types.Message, command: CommandObject):
    if message.from_user.id != ADMIN_ID: return
    try:
        target_id, amount = int(command.args.split()[0]), float(command.args.split()[1])
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", amount, target_id)
        await message.answer(f"✅ Начислено {amount} ₽.")
    except: pass

@dp.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    
    rate = await get_final_rate()
    markup = await get_current_markup()
    global_fee = await get_global_fee()
    
    async with db_pool.acquire() as conn:
        u = await conn.fetchval("SELECT COUNT(*) FROM allowed_users") or 0
        v = await conn.fetchval("SELECT COUNT(*) FROM users WHERE verified_go6 = TRUE") or 0
        d = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status = 'paid'") or 0.0
        w = await conn.fetchval("SELECT COALESCE(SUM(amount_usdt), 0) FROM withdrawals WHERE status = 'completed'") or 0.0
        p = await conn.fetchval("SELECT COALESCE(SUM(amount_usdt * $1), 0) FROM withdrawals WHERE status != 'cancelled'", ADMIN_FEE_PER_USDT) or 0.0
        user_balances = await conn.fetchval("SELECT COALESCE(SUM(balance), 0) FROM users WHERE user_id != $1", ADMIN_ID) or 0.0
        
        today_deposits = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM orders WHERE status = 'paid' AND created_at >= CURRENT_DATE") or 0.0
        today_bets = await conn.fetchval("SELECT COALESCE(SUM(bet_usdt), 0) FROM game_bets WHERE created_at >= CURRENT_DATE") or 0.0
        active_w = await conn.fetchval('SELECT COUNT(*) FROM withdrawals WHERE status = \'pending\' OR status = \'processing\'') or 0
        
        admin_balance = await conn.fetchval("SELECT balance FROM users WHERE user_id = $1", ADMIN_ID) or 0.0
        
    text = (f"📊 **ОБЩАЯ СТАТИСТИКА**\n\n"
            f"👥 Всего пользователей: **{u}**\n"
            f"🛡️ Прошли GO6: **{v}**\n"
            f"💰 Пополнено: **{d:.2f} ₽**\n"
            f"💸 Выведено: **{w:.2f} USDT**\n"
            f"⏳ Активных заявок: **{active_w}**\n\n"
            f"💼 Баланс юзеров: **{user_balances:.2f} ₽**\n\n"
            f"📅 **ЗА СЕГОДНЯ:**\n"
            f"📥 Пополнений: **{today_deposits:.2f} ₽**\n"
            f"🎰 Оборот игры: **{today_bets:.2f} USDT**\n\n"
            f"👑 **ВАШ ДОХОД:**\n"
            f"📈 С выводов (+{ADMIN_FEE_PER_USDT}₽/USDT): **{p:.2f} ₽**\n"
            f"💳 Ваш баланс: **{admin_balance:.2f} ₽**\n\n"
            f"💱 **Наценка к Рапире: +{markup} ₽**\n"
            f"💰 **Итоговый курс: {rate:.2f} ₽**\n"
            f"📥 **Общая комиссия: {global_fee}%**")
            
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_main")]]))

@dp.callback_query(F.data == "admin_main")
async def cb_admin_main(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    builder = InlineKeyboardBuilder()
    builder.button(text="📈 Статистика", callback_data="admin_stats")
    builder.button(text="👥 Все пользователи", callback_data="admin_users_0")
    builder.button(text="🛡️ Верифицированные (GO6)", callback_data="admin_verified_0")
    await callback.message.edit_text("👑 **Админ-панель:**", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("admin_users_"))
async def cb_admin_users(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    try: page = int(callback.data.split("_")[2])
    except: page = 0
    limit, offset = 5, page * 5
    
    async with db_pool.acquire() as conn:
        users = await conn.fetch("""
            SELECT u.user_id, u.username, u.balance, u.last_active,
                   (SELECT COALESCE(SUM(amount), 0) FROM orders WHERE user_id = u.user_id AND status = 'paid' AND created_at >= CURRENT_DATE) as dep_today
            FROM users u 
            JOIN allowed_users a ON u.user_id = a.user_id 
            ORDER BY u.last_active DESC LIMIT $1 OFFSET $2
        """, limit, offset)
        total_users = await conn.fetchval("SELECT COUNT(*) FROM allowed_users") or 0
    
    keyboard = []
    for u in users:
        bal = u['balance'] if u['balance'] is not None else 0.0
        dep_today = u['dep_today'] if u['dep_today'] is not None else 0.0
        display_name = f"@{u['username']}" if u['username'] else f"ID:{u['user_id']}"
        keyboard.append([InlineKeyboardButton(text=f"👤 {display_name} | {bal:.0f} ₽ | 📥 {dep_today:.0f} ₽", callback_data=f"admin_user_view_{u['user_id']}")])
    
    nav_row = []
    if page > 0: nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_users_{page-1}"))
    if offset + limit < total_users: nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"admin_users_{page+1}"))
    if nav_row: keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton(text="🔙 В меню", callback_data="admin_main")])
    
    await callback.message.edit_text(f"👥 **Пользователи** (Стр. {page+1}):\n📥 *цифра в конце — пополнения за сегодня*", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("admin_verified_"))
async def cb_admin_verified(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    try: page = int(callback.data.split("_")[2])
    except: page = 0
    limit, offset = 5, page * 5
    
    async with db_pool.acquire() as conn:
        users = await conn.fetch("""
            SELECT u.user_id, u.username, u.balance, u.last_active,
                   (SELECT COALESCE(SUM(amount), 0) FROM orders WHERE user_id = u.user_id AND status = 'paid' AND created_at >= CURRENT_DATE) as dep_today
            FROM users u 
            WHERE u.verified_go6 = TRUE
            ORDER BY u.last_active DESC LIMIT $1 OFFSET $2
        """, limit, offset)
        total_verified = await conn.fetchval("SELECT COUNT(*) FROM users WHERE verified_go6 = TRUE") or 0
        
    keyboard = []
    for u in users:
        bal = u['balance'] if u['balance'] is not None else 0.0
        dep_today = u['dep_today'] if u['dep_today'] is not None else 0.0
        display_name = f"@{u['username']}" if u['username'] else f"ID:{u['user_id']}"
        keyboard.append([InlineKeyboardButton(text=f"🛡️ {display_name} | {bal:.0f} ₽ | 📥 {dep_today:.0f} ₽", callback_data=f"admin_user_view_{u['user_id']}")])
        
    nav_row = []
    if page > 0: nav_row.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_verified_{page-1}"))
    if offset + limit < total_verified: nav_row.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"admin_verified_{page+1}"))
    if nav_row: keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton(text="🔙 В меню", callback_data="admin_main")])
    
    await callback.message.edit_text(f"🛡️ **GO6 Верификация** (Стр. {page+1}):\n📥 *цифра в конце — пополнения за сегодня*", reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("admin_user_view_"))
async def cb_admin_user_view(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[3])
    
    async with db_pool.acquire() as conn:
        user = await conn.fetchrow("SELECT username, balance, last_active, verified_go6, custom_fee FROM users WHERE user_id = $1", target_id)
        if not user: return await callback.answer("Пользователь не найден.", show_alert=True)
        
        total_in = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM orders WHERE user_id = $1 AND status = 'paid'", target_id) or 0.0
        total_out = await conn.fetchval("SELECT COALESCE(SUM(amount_usdt), 0) FROM withdrawals WHERE user_id = $1 AND status = 'completed'", target_id) or 0.0
        active_w = await conn.fetchval("SELECT COUNT(*) FROM withdrawals WHERE user_id = $1 AND status IN ('pending', 'processing')", target_id) or 0
        
        today_dep = await conn.fetchval("SELECT COALESCE(SUM(amount), 0) FROM orders WHERE user_id = $1 AND status = 'paid' AND created_at >= CURRENT_DATE", target_id) or 0.0
        today_bets = await conn.fetchval("SELECT COALESCE(SUM(bet_usdt), 0) FROM game_bets WHERE user_id = $1 AND created_at >= CURRENT_DATE", target_id) or 0.0
        today_wths = await conn.fetchval("SELECT COALESCE(SUM(amount_usdt), 0) FROM withdrawals WHERE user_id = $1 AND status = 'completed' AND created_at >= CURRENT_DATE", target_id) or 0.0
        
    global_fee = await get_global_fee()
    bal = user['balance'] if user['balance'] is not None else 0.0
    display_name = f"@{user['username']}" if user['username'] else "Скрыт"
    v_status = "✅ Да" if user['verified_go6'] else "❌ Нет"
    c_fee = user['custom_fee']
    fee_text = f"{c_fee}%" if c_fee is not None else f"Глобальная ({global_fee}%)"
    
    text = (f"👤 **ДОСЬЕ:** `{target_id}`\n\n"
            f"🔗 Юзернейм: **{display_name}**\n"
            f"🛡️ Верификация: **{v_status}**\n"
            f"📊 Комиссия: **{fee_text}**\n"
            f"🕒 Статус: {format_last_seen(user['last_active'])}\n"
            f"💰 Баланс: **{bal:.2f} ₽**\n\n"
            f"📅 **ЗА СЕГОДНЯ:**\n"
            f"📥 Пополнил: **{today_dep:.2f} ₽**\n"
            f"🎰 Игра (оборот): **{today_bets:.2f} USDT**\n"
            f"📤 Вывел: **{today_wths:.2f} USDT**\n\n"
            f"📊 **ЗА ВСЁ ВРЕМЯ:**\n"
            f"📥 Пополнил: **{total_in:.2f} ₽**\n"
            f"📤 Вывел: **{total_out:.2f} USDT**\n"
            f"⏳ Активных заявок: **{active_w}**")
            
    builder = InlineKeyboardBuilder()
    builder.button(text="📥 История пополнений", callback_data=f"admin_user_orders_{target_id}_0")
    builder.button(text="💸 История выводов", callback_data=f"admin_user_wths_{target_id}_0")
    builder.row(InlineKeyboardButton(text="📝 Списать", callback_data=f"admin_deduct_info_{target_id}"), InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"admin_block_{target_id}"))
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin_users_0"))
    await callback.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("admin_deduct_info_"))
async def cb_admin_deduct_info(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[3])
    text = (
        f"Чтобы списать средства, используйте одну из команд:\n\n"
        f"🔔 **С уведомлением:**\n`/deduct {target_id} 500`\n\n"
        f"🔕 **Без уведомления:**\n`/quiet_deduct {target_id} 500`"
    )
    await callback.message.answer(text, parse_mode="Markdown")
    await callback.answer()

@dp.callback_query(F.data.startswith("admin_block_"))
async def cb_admin_block(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    target_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM allowed_users WHERE user_id = $1", target_id)
    await callback.message.edit_text(f"🚫 Пользователь `{target_id}` заблокирован.", parse_mode="Markdown")

@dp.callback_query(F.data.startswith("admin_user_orders_"))
async def cb_admin_user_orders(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    parts = callback.data.split("_")
    target_id, page = int(parts[3]), int(parts[4])
    limit, offset = 5, page * 5
    async with db_pool.acquire() as conn:
        orders = await conn.fetch("SELECT id, amount, status, comment, created_at FROM orders WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3", target_id, limit, offset)
        total = await conn.fetchval("SELECT COUNT(*) FROM orders WHERE user_id = $1", target_id) or 0
        
    text = f"📥 **Пополнения** `{target_id}`:\n\n"
    if not orders: text += "Нет пополнений."
    
    keyboard = []
    for o in orders:
        emoji = "🟢" if o['status'] == 'paid' else "🟡"
        cmt = f"({o['comment']})" if o['comment'] else ""
        text += f"{emoji} Чек #{o['id']} | {o['amount']:.2f} ₽ {cmt}\n"
        
    nav_row = []
    if page > 0: nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_user_orders_{target_id}_{page-1}"))
    if offset + limit < total: nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_user_orders_{target_id}_{page+1}"))
    if nav_row: keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"admin_user_view_{target_id}")])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data.startswith("admin_user_wths_"))
async def cb_admin_user_wths(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    parts = callback.data.split("_")
    target_id, page = int(parts[3]), int(parts[4])
    limit, offset = 5, page * 5
    async with db_pool.acquire() as conn:
        wths = await conn.fetch("SELECT id, amount_usdt, status, network, created_at FROM withdrawals WHERE user_id = $1 ORDER BY created_at DESC LIMIT $2 OFFSET $3", target_id, limit, offset)
        total = await conn.fetchval("SELECT COUNT(*) FROM withdrawals WHERE user_id = $1", target_id) or 0
        
    text = f"💸 **Выводы** `{target_id}`:\n\n"
    if not wths: text += "Нет заявок."
    
    keyboard = []
    for w in wths:
        emoji = "✅" if w['status'] == 'completed' else "🟡" if w['status'] == 'pending' else "⏳" if w['status'] == 'processing' else "🔴"
        text += f"{emoji} #{w['id']} | {w['amount_usdt']:.2f} USDT ({w['network']})\n"
        if w['status'] in ['pending', 'processing']: 
            keyboard.append([InlineKeyboardButton(text=f"Управлять #{w['id']}", callback_data=f"manage_wth_{w['id']}")])
            
    nav_row = []
    if page > 0: nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"admin_user_wths_{target_id}_{page-1}"))
    if offset + limit < total: nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"admin_user_wths_{target_id}_{page+1}"))
    if nav_row: keyboard.append(nav_row)
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"admin_user_view_{target_id}")])
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))

@dp.callback_query(F.data.startswith("manage_wth_"))
async def cb_manage_wth(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    w_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT user_id, amount_usdt, address, network, status FROM withdrawals WHERE id = $1", w_id)
        if row['status'] == 'pending':
            await conn.execute("UPDATE withdrawals SET status = 'processing' WHERE id = $1", w_id)
            row = await conn.fetchrow("SELECT user_id, amount_usdt, address, network, status FROM withdrawals WHERE id = $1", w_id)
            
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Выполнено", callback_data=f"admin_done_{w_id}")
    builder.button(text="🔴 Отклонить", callback_data=f"admin_reject_{w_id}")
    await callback.message.edit_text(f"⚙️ **Заявка #{w_id}**\nID: `{row['user_id']}`\nСеть: {row['network']}\nUSDT: {row['amount_usdt']}\nАдрес: `{row['address']}`", reply_markup=builder.as_markup(), parse_mode="Markdown")

@dp.callback_query(F.data.startswith("admin_take_"))
async def process_admin_take(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    w_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE withdrawals SET status = 'processing' WHERE id = $1", w_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Выполнено", callback_data=f"admin_done_{w_id}")
    builder.button(text="🔴 Отклонить", callback_data=f"admin_reject_{w_id}")
    await callback.message.edit_reply_markup(reply_markup=builder.as_markup())

@dp.callback_query(F.data.startswith("admin_reject_"))
async def process_admin_reject(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    w_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow("SELECT user_id, amount_rub, amount_usdt FROM withdrawals WHERE id = $1", w_id)
            await conn.execute("UPDATE withdrawals SET status = 'rejected' WHERE id = $1", w_id)
            await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", row['amount_rub'], row['user_id'])
            await conn.execute("UPDATE users SET balance = balance - $1 WHERE user_id = $2", row['amount_usdt'] * ADMIN_FEE_PER_USDT, ADMIN_ID)
    await callback.message.edit_text(f"🔴 Заявка #{w_id} отклонена. Деньги возвращены.")

@dp.callback_query(F.data.startswith("admin_done_"))
async def process_admin_done(callback: CallbackQuery):
    if callback.from_user.id != ADMIN_ID: return
    w_id = int(callback.data.split("_")[2])
    async with db_pool.acquire() as conn:
        await conn.execute("UPDATE withdrawals SET status = 'completed' WHERE id = $1", w_id)
    await callback.message.edit_text(f"✅ Заявка #{w_id} успешно выполнена.")

# ==================== ВЕБХУКИ ====================
async def handle_coinso_webhook(request: web.Request):
    try:
        raw_body = await request.read()
        signature = request.headers.get("X-Signature")
        
        if not signature:
            return web.Response(text="Missing signature", status=400)

        expected_signature = hmac.new(
            COINSO_SECRET_KEY.encode('utf-8'),
            raw_body,
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature, expected_signature):
            logging.warning("Неверная подпись вебхука Coinso!")
            return web.Response(text="Invalid signature", status=403)

        data = json.loads(raw_body)
        
        if data.get("event") == "payment.success":
            if data.get("test_mode"):
                logging.info("Тестовый платеж Coinso получен и проигнорирован.")
                return web.Response(text="OK", status=200)

            check_id = int(data.get("custom")) 
            amount = float(data.get("amount"))
            
            async with db_pool.acquire() as conn:
                row = await conn.fetchrow(
                    "UPDATE orders SET status = 'paid' WHERE id = $1 AND status = 'pending' RETURNING user_id, comment, message_id",
                    check_id
                )
                
                if row:
                    user_id = row['user_id']
                    comment = row['comment']
                    msg_id = row['message_id']
                    
                    user_row = await conn.fetchrow("SELECT username, custom_fee FROM users WHERE user_id = $1", user_id)
                    custom_fee = user_row['custom_fee'] if user_row else None
                    username = f"@{user_row['username']}" if user_row and user_row['username'] else f"ID:{user_id}"
                    
                    global_fee = await get_global_fee()
                    actual_fee_percent = custom_fee if custom_fee is not None else global_fee
                    
                    fee = amount * (actual_fee_percent / 100.0)
                    net_amount = amount - fee
                    
                    await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", net_amount, user_id)
                    if fee > 0:
                        await conn.execute("UPDATE users SET balance = balance + $1 WHERE user_id = $2", fee, ADMIN_ID)
                    
                    reply_text = f"Счёт {amount:.2f} ₽\n"
                    if comment: reply_text += f"Название {comment}\n"
                    reply_text += "Оплата произведена"
                    
                    if msg_id:
                        try:
                            await bot.send_message(user_id, reply_text, reply_to_message_id=msg_id, allow_sending_without_reply=True)
                        except Exception as e:
                            logging.error(f"Failed to reply: {e}")
                            try: await bot.send_message(user_id, reply_text)
                            except: pass
                    else:
                        try: await bot.send_message(user_id, reply_text)
                        except: pass

                    admin_msg = (
                        f"🔔 Оплачен Чек #{check_id} (Coinso)!\n"
                        f"👤 Клиент: {username}\n"
                        f"💵 Сумма: {amount:.2f} ₽\n"
                    )
                    if comment: 
                        admin_msg += f"📝 Название: {comment}\n"
                    if fee > 0:
                        admin_msg += f"💰 Ваш процент ({actual_fee_percent}%): {fee:.2f} ₽"
                    
                    try: await bot.send_message(ADMIN_ID, admin_msg)
                    except Exception as e:
                        logging.error(f"Admin msg error: {e}")
                        
        return web.Response(text="OK", status=200)
    except Exception as e: 
        logging.error(f"Coinso Webhook Error: {e}")
        return web.Response(text="Error", status=500)

# ==================== ЗАПУСК ====================
async def handle_root(request):
    # Этот обработчик нужен, чтобы Render видел, что порт открыт и сервер жив
    return web.Response(text="Bot is running successfully!")

async def main():
    global rocket_client
    await init_db()
    
    rocket_client = xRocketClient(api_key=XROCKET_API_KEY)
    
    app = web.Application()
    app.router.add_get("/", handle_root) # Health Check для Render
    app.router.add_post("/coinso-webhook", handle_coinso_webhook)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    # <-- ИСПОЛЬЗУЕМ СИСТЕМНУЮ ПЕРЕМЕННУЮ PORT (Render выдает её сам)
    port = int(os.getenv("PORT", 10000))
    
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"Веб-сервер запущен на 0.0.0.0:{port}")
    
    try:
        logging.info("Бот запущен в режиме Polling")
        await dp.start_polling(bot)
    finally:
        logging.info("Завершение работы бота...")
        await bot.session.close()
        await runner.cleanup()
        if db_pool:
            await db_pool.close()
        logging.info("Бот успешно остановлен.")

if __name__ == "__main__":
    asyncio.run(main())
