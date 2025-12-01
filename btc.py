import hashlib
import os
import ecdsa
import base58
import requests
import json
import time
import asyncio
import sys
import aiosqlite

from aiogram import Bot, Dispatcher, Router, types, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.client.default import DefaultBotProperties
from requests.exceptions import RequestException, HTTPError

# --- КОНСТАНТЫ И НАСТРОЙКИ ---
TOKEN = '8528170692:AAH7RI_MXEkt1Qq9NIhTePaR_nc4RS6eh40'
CURVE = ecdsa.SECP256k1
DATABASE_NAME = 'btc_adress.db'
MIN_DELAY_SECONDS = 0.2 

# --- FSM STATES ---

class SearchStates(StatesGroup):
    idle = State()
    searching = State()

# --- КРИПТОГРАФИЧЕСКИЕ ФУНКЦИИ ---

def double_sha256(data):
    return hashlib.sha256(hashlib.sha256(data).digest()).digest()

def hash160(data):
    sha256_hash = hashlib.sha256(data).digest()
    return hashlib.new('ripemd160', sha256_hash).digest()

def generate_key_and_address():
    """Генерирует HEX, WIF и сжатый BTC-адрес формата P2SH-SegWit (начинается с '3')."""
    private_key_bytes = os.urandom(32)
    
    # 1. Генерация Public Key (Сжатый формат)
    sk = ecdsa.SigningKey.from_string(private_key_bytes, curve=CURVE)
    vk = sk.verifying_key
    public_key_bytes = b'\x02' + vk.to_string()[:32]
    
    # 2. Вычисление SegWit Script Hash для адреса P2SH-SegWit
    pubkey_hash = hash160(public_key_bytes)
    # Witness Script: OP_0 [20-byte-pubkey-hash]
    witness_script = b'\x00\x14' + pubkey_hash
    # HASH160(Witness Script)
    script_hash = hash160(witness_script)
    
    # 3. Кодирование Base58Check с префиксом P2SH (0x05)
    P2SH_VERSION = b'\x05' 
    versioned_hash = P2SH_VERSION + script_hash
    checksum = double_sha256(versioned_hash)[:4]
    bitcoin_address = base58.b58encode(versioned_hash + checksum).decode('utf-8')

    # 4. Генерация WIF-ключа (Сжатый формат: начинается с K или L)
    extended_key = b'\x80' + private_key_bytes + b'\x01' 
    checksum_wif = double_sha256(extended_key)[:4]
    wif_key = base58.b58encode(extended_key + checksum_wif).decode('utf-8')

    return {
        "Private_Key_Hex": private_key_bytes.hex(),
        "WIF_Key": wif_key,
        "Bitcoin_Address": bitcoin_address
    }

def check_balance_sync(address):
    """Синхронная функция для проверки баланса."""
    API_URL = f"https://blockchain.info/balance?active={address}"

    while True:
        try:
            response = requests.get(API_URL, timeout=10) 
            response.raise_for_status() 
            data = response.json()

            balance_satoshi = data.get(address, {}).get('final_balance', 0)
            balance_btc = balance_satoshi / 100_000_000.0
            return balance_btc

        except json.JSONDecodeError:
            return 0.0 
        except HTTPError as e:
            if response.status_code in [429, 500]:
                time.sleep(60)
                continue 
            else:
                return 0.0
        except RequestException:
            return 0.0

# --- ФУНКЦИИ БАЗЫ ДАННЫХ ASYNCSQLITE ---

async def setup_database():
    """Создает таблицу, включая поля для HEX и WIF ключей."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS generated_addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                address TEXT NOT NULL UNIQUE,
                private_key TEXT,
                wif_key TEXT, 
                balance REAL NOT NULL,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        await db.commit()
        
async def get_initial_count():
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute('SELECT COUNT(*) FROM generated_addresses') as cursor:
            count = (await cursor.fetchone())[0]
            return count

async def save_address_async(address_data, balance):
    """Сохраняет адрес, HEX и WIF."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        await db.execute('''
            INSERT OR REPLACE INTO generated_addresses 
            (address, private_key, wif_key, balance) 
            VALUES (?, ?, ?, ?)
        ''', (address_data['Bitcoin_Address'], 
              address_data['Private_Key_Hex'],
              address_data['WIF_Key'],
              balance))
        await db.commit()

async def get_all_addresses_async():
    """Получает все данные для отображения в списке."""
    async with aiosqlite.connect(DATABASE_NAME) as db:
        async with db.execute('SELECT address, private_key, wif_key, balance FROM generated_addresses ORDER BY timestamp DESC') as cursor:
            results = await cursor.fetchall()
            return results

# --- ОСНОВНАЯ ЛОГИКА БОТА ---

SEARCH_TASKS = {}
router = Router()

def get_keyboard(is_searching: bool) -> InlineKeyboardMarkup:
    if is_searching:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⏸️ СТОП ПОИСК", callback_data='stop_search')]
        ])
    else:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔍 ПОИСК", callback_data='start_search')],
            [InlineKeyboardButton(text="📋 СПИСОК АДРЕСОВ", callback_data='list_addresses')],
            [InlineKeyboardButton(text="💰 БАЛАНС (всего)", callback_data='total_balance')]
        ])

async def get_main_message_text(user_id: int, state: FSMContext, status_override: str = None) -> str:
    data = await state.get_data()
    is_searching = data.get('is_searching', False)
    
    if is_searching and not status_override:
        status = data.get('search_progress', "Поиск запущен...")
    elif status_override:
        status = status_override
    else:
        status = data.get('status_message', 'Ожидание команды...')

    message = (
        "**Crypto Search Bot v1.0**\n"
        "-------------------------------------\n"
        f"**Статус:** {status}\n"
        "-------------------------------------\n"
        "Нажмите 'Поиск', чтобы начать сканирование Bitcoin-адресов."
    )
    return message

async def send_main_message(bot: Bot, chat_id: int, state: FSMContext, status_override: str = None) -> None:
    data = await state.get_data()
    message_id = data.get('main_message_id')
    is_searching = data.get('is_searching', False)
    
    message_text = await get_main_message_text(chat_id, state, status_override)
    keyboard = get_keyboard(is_searching)
    
    try:
        if message_id:
            await bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=message_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=message_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
            await state.update_data(main_message_id=msg.message_id)
            
    except Exception:
        try:
            msg = await bot.send_message(
                chat_id=chat_id,
                text=message_text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN
            )
            await state.update_data(main_message_id=msg.message_id)
        except Exception:
            pass

# --- АСИНХРОННАЯ ЗАДАЧА ПОИСКА ---

async def search_runner(bot: Bot, chat_id: int, state: FSMContext):
    initial_count = await get_initial_count()
    await state.update_data(initial_count=initial_count)
    counter = initial_count
    start_time = time.time()
    loop = asyncio.get_running_loop()
    
    try:
        while True:
            counter += 1
            
            key_data = generate_key_and_address()
            generated_address = key_data['Bitcoin_Address']

            balance = await loop.run_in_executor(None, check_balance_sync, generated_address)
            
            await save_address_async(key_data, balance)
            
            attempts_since_start = counter - initial_count
            elapsed_time = time.time() - start_time
            rate = attempts_since_start / elapsed_time if elapsed_time > 0 else 0
            
            progress_msg = (
                f"**ПОИСК АКТИВЕН**\n"
                f"Попыток: {attempts_since_start:,} / {counter:,}\n"
                f"Скорость: {rate:.2f} addr/s\n\n"
                f"**Адрес:** `{generated_address}`\n"
                f"**WIF:** `{key_data['WIF_Key']}`\n"
                f"Баланс: {balance:.8f} BTC"
            )
            await state.update_data(search_progress=progress_msg)
            
            if counter % 10 == 0:
                 await send_main_message(bot, chat_id, state)

            if balance > 0:
                success_message = (
                    "🎉 **УСПЕХ! АДРЕС С БАЛАНСОМ НАЙДЕН!** 🎉\n"
                    f"**Адрес:** `{generated_address}`\n"
                    f"**Приватный Ключ (WIF):** `{key_data['WIF_Key']}`\n"
                    f"**Приватный Ключ (HEX):** `{key_data['Private_Key_Hex']}`\n"
                    f"**Баланс:** {balance:.8f} BTC\n"
                    f"Данные сохранены в базе."
                )
                await bot.send_message(chat_id=chat_id, text=success_message, parse_mode=ParseMode.MARKDOWN)
                break
                
            await asyncio.sleep(MIN_DELAY_SECONDS) 

    except asyncio.CancelledError:
        await state.update_data(status_message=f"Поиск остановлен. Попыток: {counter:,}.", is_searching=False)
    except Exception as e:
        error_msg = f"❌ Критическая ошибка в задаче поиска: {e}"
        await state.update_data(status_message=error_msg, is_searching=False)
        await bot.send_message(chat_id=chat_id, text=error_msg)
    finally:
        if chat_id in SEARCH_TASKS:
            del SEARCH_TASKS[chat_id]
        await state.set_state(SearchStates.idle)
        await send_main_message(bot, chat_id, state)


# --- ОБРАБОТЧИКИ AIO GRAM ---

@router.message(CommandStart())
async def command_start_handler(message: types.Message, state: FSMContext) -> None:
    await state.set_state(SearchStates.idle)
    await state.set_data({}) 
    await send_main_message(message.bot, message.chat.id, state, status_override="Ожидание команды...")

@router.callback_query(F.data == 'start_search', SearchStates.idle)
async def start_search_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Запускаю поиск...")
    chat_id = callback.message.chat.id

    await state.update_data(is_searching=True, search_progress="Запуск задачи поиска...")
    await state.set_state(SearchStates.searching)

    task = asyncio.create_task(search_runner(callback.bot, chat_id, state))
    SEARCH_TASKS[chat_id] = task

    await send_main_message(callback.bot, chat_id, state)

@router.callback_query(F.data == 'stop_search', SearchStates.searching)
async def stop_search_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Остановка поиска...")
    chat_id = callback.message.chat.id
    
    if chat_id in SEARCH_TASKS:
        SEARCH_TASKS[chat_id].cancel()
    
    await state.update_data(is_searching=False, status_message="Останавливаю поиск...")
    await send_main_message(callback.bot, chat_id, state)

@router.callback_query(F.data == 'list_addresses')
async def list_addresses_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Получаю список...")
    chat_id = callback.message.chat.id

    addresses = await get_all_addresses_async()
    
    if not addresses:
        message = "База данных пуста. Запустите поиск!"
    else:
        # 0: address, 1: private_key (HEX), 2: wif_key, 3: balance
        list_output = "\n".join([
            f"`{addr}`\n`WIF: {wif_key[:10]}...`\n**{bal:.8f} BTC**\n" 
            for addr, hex_key, wif_key, bal in addresses[:5]
        ])
        message = (
            f"**📋 Последние {min(5, len(addresses))} Адресов из {len(addresses)}**\n"
            "-------------------------------------\n"
            f"{list_output}"
            "\n*Для полного списка см. файл btc_adress.db*"
        )
    await callback.message.answer(message, parse_mode=ParseMode.MARKDOWN)
    await send_main_message(callback.bot, chat_id, state)

@router.callback_query(F.data == 'total_balance')
async def total_balance_callback(callback: types.CallbackQuery, state: FSMContext) -> None:
    await callback.answer("Считаю баланс...")
    chat_id = callback.message.chat.id
    
    addresses = await get_all_addresses_async()
    total_balance = sum(bal for _, _, _, bal in addresses)
    
    message = (
        "**💰 Общий Баланс**\n"
        "-------------------------------------\n"
        f"Обнаружено {len(addresses):,} адресов.\n"
        f"**Суммарный баланс:** {total_balance:.8f} BTC"
    )
    await callback.message.answer(message, parse_mode=ParseMode.MARKDOWN)
    await send_main_message(callback.bot, chat_id, state)


async def main() -> None:
    if TOKEN == 'YOUR_TELEGRAM_BOT_TOKEN':
        print("❌ ОШИБКА: Пожалуйста, замените 'YOUR_TELEGRAM_BOT_TOKEN' на ваш реальный токен.")
        sys.exit(1)
        
    # Важно: если ранее вы запускали код и получили ошибку,
    # УДАЛИТЕ файл 'btc_adress.db' перед запуском, чтобы создать его с новой структурой.
    await setup_database()
    print(f"База данных '{DATABASE_NAME}' готова. Бот запущен.")
    
    # Исправленная инициализация Bot для aiogram 3.7+
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.MARKDOWN))
    
    dp = Dispatcher()
    dp.include_router(router)

    await dp.start_polling(bot)

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен вручную.")
