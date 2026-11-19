import asyncio
import base64
import logging
import secrets
import time
from decimal import Decimal
from urllib.parse import quote

import aiohttp
import aiosqlite

from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    LabeledPrice,
    PreCheckoutQuery,
)

# ============================================================
# НАСТРОЙКИ (МЕНЯЙ ТОЛЬКО ЭТИ ДВЕ СТРОКИ!)
# ============================================================

# Вставь токен из BotFather
BOT_TOKEN = "ТВОЙ_ТОКЕН_ЗДЕСЬ"

# Твой Telegram ID (числом)
ADMIN_ID = 8932848253 

# ============================================================
# (ОСТАЛЬНОЕ ТРОГАТЬ НЕ НУЖНО)
# ============================================================

# Бот сам создаст уникальную базу данных для каждого токена
DB_PATH = f"shop_{BOT_TOKEN.split(':')[0]}.db"

TON_API_URL = "https://toncenter.com/api/v2"
TON_API_V3_URL = "https://toncenter.com/api/v3"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger(__name__)

# ============================================================
# БАЗА ДАННЫХ И ДЕФОЛТНЫЕ ТЕКСТЫ
# ============================================================

async def db_connect():
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db

async def init_db():
    db = await db_connect()
    try:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS payments (
            invoice_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, amount_nano INTEGER NOT NULL, 
            comment TEXT NOT NULL UNIQUE, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, 
            status TEXT NOT NULL DEFAULT 'pending', tx_hash TEXT, item_name TEXT
        );
        CREATE TABLE IF NOT EXISTS stars_payments (
            payment_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at INTEGER NOT NULL, telegram_charge_id TEXT UNIQUE
        );
        """)
        # ВСЕ ТЕКСТЫ ВЫНЕСЕНЫ СЮДА (МЕНЯЮТСЯ В АДМИНКЕ)
        defaults = {
            "shop_text": "<b>🔥 Эксклюзивный товар</b>\n\nЭтот товар изменит вашу жизнь. Красивое описание, которое можно менять в админке.\n\nВыберите способ оплаты:",
            "shop_photo": "",
            "ton_address": "UQ...", # Вписать свой
            "price_ton": "1",
            "price_stars": "100",
            
            # Тексты кнопок
            "btn_buy_ton": "💎 Купить за {price} TON",
            "btn_buy_stars": "⭐ Купить за {price} Stars",
            
            # Тексты оплат
            "text_ton_invoice": "💎 <b>Оплата TON</b>\n\nСумма: <b>{price} TON</b>\nАдрес: <code>{address}</code>\nКомментарий (обязательно): <code>{comment}</code>\n\nПосле перевода нажмите «Я оплатил».",
            "text_stars_desc": "Оплата через Telegram Stars для доступа к товару.",
            
            # Тексты успеха
            "text_success_ton": "✅ <b>Оплата TON подтверждена!</b>\nСпасибо за покупку.",
            "text_success_stars": "✅ <b>Оплата Stars прошла успешно!</b>\nСпасибо за покупку."
        }
        for k, v in defaults.items():
            await db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)", (k, v))
        await db.commit()
    finally:
        await db.close()

async def get_setting(key, default=""):
    db = await db_connect()
    try:
        cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = await cur.fetchone()
        return row["value"] if row else default
    finally:
        await db.close()

async def set_setting(key, value):
    db = await db_connect()
    try:
        await db.execute("INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        await db.commit()
    finally:
        await db.close()

# ============================================================
# ИНИЦИАЛИЗАЦИЯ БОТА
# ============================================================

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())

class AdminState(StatesGroup):
    waiting_for_text = State()
    waiting_for_photo = State()
    waiting_for_ton = State()
    waiting_for_stars = State()
    waiting_for_address = State()

# ============================================================
# ОТПРАВКА ТОВАРА (ВИТРИНА)
# ============================================================

async def send_shop_item(chat_id, edit_message_id=None):
    try:
        text = await get_setting("shop_text")
        photo = await get_setting("shop_photo")
        price_ton = await get_setting("price_ton")
        price_stars = await get_setting("price_stars")
        
        btn_ton_text = (await get_setting("btn_buy_ton")).format(price=price_ton)
        btn_stars_text = (await get_setting("btn_buy_stars")).format(price=price_stars)
        
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=btn_ton_text, style="success", callback_data="buy_ton")],
            [InlineKeyboardButton(text=btn_stars_text, style="primary", callback_data="buy_stars")]
        ])

        if photo:
            try:
                if edit_message_id:
                    await bot.delete_message(chat_id, edit_message_id)
                await bot.send_photo(chat_id=chat_id, photo=photo, caption=text, reply_markup=kb)
                return
            except Exception as e:
                logger.error(f"Photo send error: {e}")
                
        if edit_message_id:
            await bot.edit_message_text(chat_id=chat_id, message_id=edit_message_id, text=text, reply_markup=kb)
        else:
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
    except Exception as e:
        logger.error(f"Error formatting shop text: {e}")
        await bot.send_message(chat_id, "Ошибка в тексте витрины. Проверьте админку.")

# ============================================================
# ПОЛЬЗОВАТЕЛЬСКАЯ ЧАСТЬ
# ============================================================

@dp.message(F.text == "/start")
async def start_cmd(message: Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("👋 Привет, Админ! Используй /admin для настройки витрины.")
    await send_shop_item(message.chat.id)

# ============================================================
# ОПЛАТА STARS
# ============================================================

@dp.callback_query(F.data == "buy_stars")
async def buy_stars(callback: CallbackQuery):
    await callback.answer()
    stars = int(await get_setting("price_stars", "100"))
    desc = await get_setting("text_stars_desc", "Оплата")
    
    if stars <= 0:
        return await callback.message.answer("❌ Цена в Stars не настроена.")
        
    try:
        await bot.send_invoice(
            chat_id=callback.from_user.id,
            title="Покупка товара",
            description=desc,
            payload=f"SHOP_STARS:{callback.from_user.id}:{stars}",
            currency="XTR",
            prices=[LabeledPrice(label="Товар", amount=stars)]
        )
    except Exception as e:
        await callback.message.answer(f"Ошибка создания счета: {e}")

@dp.pre_checkout_query()
async def pre_checkout(query: PreCheckoutQuery):
    await query.answer(ok=True)

@dp.message(F.successful_payment)
async def success_stars(message: Message):
    payment = message.successful_payment
    if payment.currency != "XTR": return
    
    db = await db_connect()
    try:
        await db.execute(
            "INSERT OR IGNORE INTO stars_payments(payment_id, user_id, created_at, telegram_charge_id) VALUES(?,?,?,?)",
            (payment.telegram_payment_charge_id, message.from_user.id, int(time.time()), payment.telegram_payment_charge_id)
        )
        await db.commit()
    finally:
        await db.close()
        
    success_text = await get_setting("text_success_stars", "✅ Оплата прошла!")
    await message.answer(success_text)
    
    try:
        await bot.send_message(ADMIN_ID, f"💰 <b>Новая оплата Stars!</b>\nЮзер: {message.from_user.username or message.from_user.id}\nСумма: {payment.total_amount} ⭐")
    except: pass

# ============================================================
# ОПЛАТА TON (УПРОЩЕННАЯ)
# ============================================================

def ton_to_nano(value): return int(Decimal(str(value)) * Decimal(1_000_000_000))
def format_ton(value): return format(Decimal(str(value)).normalize(), "f")

async def get_ton_transactions(address):
    headers = {"Accept": "application/json"}
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            async with session.get(f"{TON_API_V3_URL}/transactions", params={"account": address, "limit": 20}, headers=headers) as r:
                data = await r.json(content_type=None)
                if r.status == 200: return data.get("transactions", [])
        except: pass
        async with session.get(f"{TON_API_URL}/getTransactions", params={"address": address, "limit": 20}, headers=headers) as r:
            data = await r.json(content_type=None)
            if data.get("ok"): return data.get("result", [])
    return []

def decode_comment(in_msg):
    msg_data = in_msg.get("msg_data") or {}
    if isinstance(msg_data, dict) and (raw := msg_data.get("text")):
        try: return base64.b64decode(raw).decode("utf-8").strip()
        except: pass
    content = in_msg.get("message_content") or {}
    if isinstance(content, dict) and (raw := content.get("body")):
        try: 
            decoded = base64.b64decode(raw).decode("utf-8", errors="ignore").strip("\x00 \t\r\n")
            if "SHOP-" in decoded: return decoded[decoded.find("SHOP-"):].split("\x00", 1)[0].strip()
        except: pass
    return ""

@dp.callback_query(F.data == "buy_ton")
async def buy_ton(callback: CallbackQuery):
    await callback.answer()
    address = (await get_setting("ton_address")).strip()
    if not address or len(address) < 20:
        return await callback.message.answer("❌ TON-кошелек не настроен в админке.")
        
    price = Decimal(await get_setting("price_ton", "1"))
    invoice_id = secrets.token_hex(4).upper()
    comment = f"SHOP-{callback.from_user.id}-{invoice_id}"
    amount_nano = ton_to_nano(price)
    
    db = await db_connect()
    try:
        await db.execute(
            "INSERT INTO payments(invoice_id, user_id, amount_nano, comment, created_at, expires_at, item_name) VALUES(?,?,?,?,?,?,?)",
            (invoice_id, callback.from_user.id, amount_nano, comment, int(time.time()), int(time.time()) + 1800, "Товар")
        )
        await db.commit()
    finally: await db.close()

    pay_url = f"https://app.tonkeeper.com/transfer/{address}?amount={amount_nano}&text={quote(comment)}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 Открыть Tonkeeper", url=pay_url)],
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data=f"check_ton:{invoice_id}")]
    ])
    
    try:
        invoice_text = (await get_setting("text_ton_invoice")).format(
            price=format_ton(price), 
            address=address, 
            comment=comment
        )
    except Exception as e:
        invoice_text = f"Ошибка в тексте счета TON. Проверьте админку.\n{e}"

    await callback.message.answer(invoice_text, reply_markup=kb)

@dp.callback_query(F.data.startswith("check_ton:"))
async def check_ton(callback: CallbackQuery):
    invoice_id = callback.data.split(":")[1]
    await callback.answer("Проверяем блокчейн...")
    
    db = await db_connect()
    try:
        cur = await db.execute("SELECT * FROM payments WHERE invoice_id=? AND status='pending'", (invoice_id,))
        payment = await cur.fetchone()
    finally: await db.close()

    if not payment:
        return await callback.message.edit_text("❌ Счет не найден или уже оплачен.")

    address = (await get_setting("ton_address")).strip()
    try: txs = await get_ton_transactions(address)
    except: return await callback.message.answer("⚠️ Ошибка сети TON. Попробуйте позже.")

    for tx in txs:
        in_msg = tx.get("in_msg", {})
        if int(in_msg.get("value", 0)) == int(payment["amount_nano"]):
            if decode_comment(in_msg) == payment["comment"]:
                # Успех!
                db = await db_connect()
                try:
                    await db.execute("UPDATE payments SET status='paid' WHERE invoice_id=?", (invoice_id,))
                    await db.commit()
                finally: await db.close()
                
                success_text = await get_setting("text_success_ton", "✅ Оплата TON прошла!")
                await callback.message.edit_text(success_text)
                
                try: await bot.send_message(ADMIN_ID, f"💰 <b>Новая оплата TON!</b>\nЮзер: {callback.from_user.username or callback.from_user.id}\nСумма: {format_ton(payment['amount_nano'] / 1_000_000_000)} TON")
                except: pass
                return

    await callback.message.answer("⏳ Платеж не найден. Подождите пару минут и проверьте снова.")

# ============================================================
# АДМИНКА
# ============================================================

def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🖼 Картинка", style="primary", callback_data="adm_photo")],
        [InlineKeyboardButton(text="📝 Редактировать тексты", style="primary", callback_data="adm_texts_menu")],
        [InlineKeyboardButton(text="💎 Кошелек TON", style="primary", callback_data="adm_address")],
        [InlineKeyboardButton(text="💰 Цены", style="primary", callback_data="adm_prices")],
        [InlineKeyboardButton(text="👀 Посмотреть витрину", style="success", callback_data="adm_preview")]
    ])

def texts_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Текст витрины", callback_data="edit_txt:shop_text")],
        [InlineKeyboardButton(text="Кнопка TON", callback_data="edit_txt:btn_buy_ton")],
        [InlineKeyboardButton(text="Кнопка Stars", callback_data="edit_txt:btn_buy_stars")],
        [InlineKeyboardButton(text="Счет TON", callback_data="edit_txt:text_ton_invoice")],
        [InlineKeyboardButton(text="Счет Stars", callback_data="edit_txt:text_stars_desc")],
        [InlineKeyboardButton(text="Успех TON", callback_data="edit_txt:text_success_ton")],
        [InlineKeyboardButton(text="Успех Stars", callback_data="edit_txt:text_success_stars")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")]
    ])

def prices_menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Изменить цену TON", callback_data="adm_ton")],
        [InlineKeyboardButton(text="Изменить цену Stars", callback_data="adm_stars")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="adm_back")]
    ])

@dp.message(F.text == "/admin")
async def admin_panel(message: Message):
    if message.from_user.id != ADMIN_ID: return
    await message.answer("<b>⚙️ Админ-панель магазина</b>\n\nВыберите, что хотите изменить:", reply_markup=admin_kb())

@dp.callback_query(F.data == "adm_back")
async def adm_back(callback: CallbackQuery):
    await callback.message.edit_text("<b>⚙️ Админ-панель</b>", reply_markup=admin_kb())

@dp.callback_query(F.data == "adm_texts_menu")
async def adm_texts_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "<b>📝 Редактирование текстов</b>\n\n"
        "Вы можете использовать переменные в фигурных скобках:\n"
        "• В кнопках: <code>{price}</code>\n"
        "• В счете TON: <code>{price}</code>, <code>{address}</code>, <code>{comment}</code>", 
        reply_markup=texts_menu_kb()
    )

@dp.callback_query(F.data.startswith("edit_txt:"))
async def prompt_edit_text(callback: CallbackQuery, state: FSMContext):
    if callback.from_user.id != ADMIN_ID: return
    key = callback.data.split(":")[1]
    await state.set_state(AdminState.waiting_for_text)
    await state.update_data(edit_key=key)
    
    current = await get_setting(key)
    await callback.message.answer(f"Текущий текст:\n\n<code>{current}</code>\n\nОтправьте новый текст (можно с HTML):")
    await callback.answer()

@dp.message(AdminState.waiting_for_text)
async def process_edit_text(message: Message, state: FSMContext):
    data = await state.get_data()
    key = data.get("edit_key")
    await set_setting(key, message.html_text)
    await state.clear()
    await message.answer("✅ Текст обновлен!\nНажмите /admin -> Посмотреть витрину.", reply_markup=admin_kb())

# --- Остальные настройки ---

@dp.callback_query(F.data == "adm_prices")
async def adm_prices(callback: CallbackQuery):
    await callback.message.edit_text("<b>💰 Изменение цен</b>", reply_markup=prices_menu_kb())

@dp.callback_query(F.data == "adm_address")
async def adm_address(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_address)
    await callback.message.answer("Отправьте адрес вашего TON-кошелька (начинается на UQ или EQ):")
    await callback.answer()

@dp.message(AdminState.waiting_for_address)
async def set_address(message: Message, state: FSMContext):
    if len(message.text) < 40:
        return await message.answer("❌ Неверный формат адреса.")
    await set_setting("ton_address", message.text.strip())
    await state.clear()
    await message.answer("✅ Кошелек обновлен!", reply_markup=admin_kb())

@dp.callback_query(F.data == "adm_preview")
async def adm_preview(callback: CallbackQuery):
    await send_shop_item(callback.from_user.id)
    await callback.answer("Витрина отправлена ниже")

@dp.callback_query(F.data == "adm_photo")
async def adm_photo(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_photo)
    await callback.message.answer("📸 Отправьте новую фотографию для витрины (или /cancel):")
    await callback.answer()

@dp.message(AdminState.waiting_for_photo, F.photo)
async def set_photo(message: Message, state: FSMContext):
    await set_setting("shop_photo", message.photo[-1].file_id)
    await state.clear()
    await message.answer("✅ Картинка обновлена!", reply_markup=admin_kb())

@dp.callback_query(F.data.in_({"adm_ton", "adm_stars"}))
async def adm_price_prompt(callback: CallbackQuery, state: FSMContext):
    if callback.data == "adm_ton":
        await state.set_state(AdminState.waiting_for_ton)
        await callback.message.answer("💎 Введите новую цену в TON (например: 1.5):")
    else:
        await state.set_state(AdminState.waiting_for_stars)
        await callback.message.answer("⭐ Введите новую цену в Stars (например: 100):")
    await callback.answer()

@dp.message(AdminState.waiting_for_ton)
async def set_ton_price(message: Message, state: FSMContext):
    try:
        price = Decimal(message.text.replace(",", "."))
        if price <= 0: raise ValueError
        await set_setting("price_ton", format_ton(price))
        await state.clear()
        await message.answer(f"✅ Цена TON изменена на {format_ton(price)}", reply_markup=admin_kb())
    except:
        await message.answer("❌ Введите число (например 1.5).")

@dp.message(AdminState.waiting_for_stars)
async def set_stars_price(message: Message, state: FSMContext):
    try:
        price = int(message.text)
        if price <= 0: raise ValueError
        await set_setting("price_stars", price)
        await state.clear()
        await message.answer(f"✅ Цена Stars изменена на {price} ⭐", reply_markup=admin_kb())
    except:
        await message.answer("❌ Введите целое число.")

@dp.message(F.text == "/cancel")
async def cancel_state(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=admin_kb())

# ============================================================
# ЗАПУСК
# ============================================================

async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    logger.info("SHOP BOT STARTED!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())