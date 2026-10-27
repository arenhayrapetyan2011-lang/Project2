import asyncio
import html
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.filters import Command
from aiogram.types import Message

# ========== НАСТРОЙКИ ==========
# ВАЖНО: этот токен засветился в переписке — отзови его в @BotFather (/revoke)
# и вставь новый ниже, или задай через переменную окружения BOT_TOKEN.
BOT_TOKEN = os.environ.get(
    "BOT_TOKEN",
    "8899279978:AAHwqcSWP1adPf1ZI5iyTnerYF8mFbGd1zk"
).strip()

ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "7464115048") or 0)

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", "8080"))
# ===============================

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "bookings.db"
TZ = ZoneInfo("Europe/Moscow")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

STATUS_NAMES = {
    "new": "Новая",
    "confirmed": "Подтверждена",
    "completed": "Завершена",
    "cancelled": "Отменена",
}


def connect_db():
    conn = sqlite3.connect(DB_PATH, timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db():
    conn = connect_db()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS bookings (
                id TEXT PRIMARY KEY,
                booking_number TEXT NOT NULL UNIQUE,
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                duration INTEGER NOT NULL,
                service_name TEXT NOT NULL,
                price INTEGER NOT NULL DEFAULT 0,
                first_name TEXT NOT NULL,
                last_name TEXT NOT NULL,
                phone TEXT NOT NULL,
                car_model TEXT NOT NULL DEFAULT '',
                comment TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'confirmed',
                created_at TEXT NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_bookings_date_time
            ON bookings(date, time)
        """)
    finally:
        conn.close()


def text_value(value, max_len=300):
    return str(value or "").strip()[:max_len]


def parse_booking(payload):
    if not isinstance(payload, dict):
        raise ValueError("Неверный формат данных брони.")

    booking = {
        "id": text_value(payload.get("id"), 100),
        "booking_number": text_value(payload.get("bookingNumber"), 80),
        "date": text_value(payload.get("date"), 10),
        "time": text_value(payload.get("time"), 5),
        "duration": int(payload.get("duration", 60)),
        "service_name": text_value(payload.get("serviceName"), 150),
        "price": int(round(float(payload.get("price", 0)))),
        "first_name": text_value(payload.get("firstName"), 100),
        "last_name": text_value(payload.get("lastName"), 100),
        "phone": text_value(payload.get("phone"), 40),
        "car_model": text_value(payload.get("carModel"), 100),
        "comment": text_value(payload.get("comment"), 300),
        "status": "confirmed",
        "created_at": text_value(payload.get("createdAt"), 50)
                       or datetime.now(TZ).isoformat(),
    }

    required = (
        "id", "booking_number", "date", "time",
        "service_name", "first_name", "last_name", "phone"
    )
    if not all(booking[key] for key in required):
        raise ValueError("Заполнены не все обязательные поля брони.")

    try:
        booking_date = datetime.strptime(booking["date"], "%Y-%m-%d").date()
        datetime.strptime(booking["time"], "%H:%M")
    except ValueError as exc:
        raise ValueError("Неверная дата или время брони.") from exc

    if booking_date < datetime.now(TZ).date():
        raise ValueError("Нельзя забронировать на прошедшую дату.")

    if not 5 <= booking["duration"] <= 1000:
        raise ValueError("Неверная длительность услуги.")

    if not 0 <= booking["price"] <= 100_000_000:
        raise ValueError("Неверная стоимость услуги.")

    digits = re.sub(r"\D", "", booking["phone"])
    if not 8 <= len(digits) <= 15:
        raise ValueError("Неверный номер телефона.")

    return booking


def minutes(time_text):
    hour, minute = map(int, time_text.split(":"))
    return hour * 60 + minute


def add_booking(booking):
    """Сохраняет бронь и не допускает пересечения интервалов."""
    conn = connect_db()
    try:
        conn.execute("BEGIN IMMEDIATE")

        existing = conn.execute(
            "SELECT * FROM bookings WHERE id = ?",
            (booking["id"],)
        ).fetchone()
        if existing:
            conn.execute("COMMIT")
            return dict(existing), False, False

        start = minutes(booking["time"])
        end = start + booking["duration"]

        same_day = conn.execute(
            "SELECT time, duration FROM bookings "
            "WHERE date = ? AND status != 'cancelled'",
            (booking["date"],)
        ).fetchall()

        for row in same_day:
            other_start = minutes(row["time"])
            other_end = other_start + int(row["duration"])
            if start < other_end and end > other_start:
                conn.execute("ROLLBACK")
                return None, False, True

        conn.execute("""
            INSERT INTO bookings (
                id, booking_number, date, time, duration,
                service_name, price, first_name, last_name,
                phone, car_model, comment, status, created_at, notified
            ) VALUES (
                :id, :booking_number, :date, :time, :duration,
                :service_name, :price, :first_name, :last_name,
                :phone, :car_model, :comment, :status, :created_at, 0
            )
        """, booking)

        conn.execute("COMMIT")

        saved = conn.execute(
            "SELECT * FROM bookings WHERE id = ?",
            (booking["id"],)
        ).fetchone()
        return dict(saved), True, False

    except sqlite3.IntegrityError:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        return None, False, True
    except Exception:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()


def mark_notified(booking_id):
    conn = connect_db()
    try:
        conn.execute(
            "UPDATE bookings SET notified = 1 WHERE id = ?",
            (booking_id,)
        )
    finally:
        conn.close()


def get_bookings(day=None):
    conn = connect_db()
    try:
        if day:
            rows = conn.execute(
                "SELECT * FROM bookings WHERE date = ? ORDER BY time ASC",
                (day,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM bookings ORDER BY date ASC, time ASC"
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def make_notification(b):
    safe = lambda value: html.escape(str(value or "—"))
    price = f"{int(b['price']):,}".replace(",", " ") + " ֏"
    return (
        "🆕 <b>Новая бронь</b>\n\n"
        f"<b>Номер:</b> {safe(b['booking_number'])}\n"
        f"<b>Дата:</b> {safe(b['date'])}\n"
        f"<b>Время:</b> {safe(b['time'])}\n"
        f"<b>Услуга:</b> {safe(b['service_name'])}\n"
        f"<b>Клиент:</b> {safe(b['first_name'])} {safe(b['last_name'])}\n"
        f"<b>Телефон:</b> {safe(b['phone'])}\n"
        f"<b>Автомобиль:</b> {safe(b['car_model'])}\n"
        f"<b>Цена:</b> {safe(price)}\n"
        f"<b>Комментарий:</b> {safe(b['comment'])}"
    )


async def notify_admin(booking):
    if not ADMIN_CHAT_ID:
        logging.warning(
            "ADMIN_CHAT_ID не настроен: бронь сохранена без уведомления."
        )
        return False
    try:
        await bot.send_message(
            ADMIN_CHAT_ID,
            make_notification(booking),
            parse_mode="HTML"
        )
        await asyncio.to_thread(mark_notified, booking["id"])
        return True
    except Exception:
        logging.exception("Не удалось отправить сообщение о брони в Telegram")
        return False


@web.middleware
async def cors_middleware(request, handler):
    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)

    response.headers["Access-Control-Allow-Origin"] = request.headers.get("Origin", "*")
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Vary"] = "Origin"
    return response


async def index_page(_request):
    path = BASE_DIR / "index.html"
    if not path.exists():
        return web.Response(text="Положи index.html рядом с bot.py", status=404)
    return web.FileResponse(path)


async def health(_request):
    return web.json_response({"ok": True})


async def create_booking(request):
    try:
        payload = await request.json()
        booking = parse_booking(payload)
    except (ValueError, TypeError, KeyError) as exc:
        return web.json_response({"ok": False, "error": str(exc)}, status=400)
    except Exception:
        logging.exception("Ошибка чтения запроса")
        return web.json_response(
            {"ok": False, "error": "Не удалось прочитать данные брони."},
            status=400
        )

    saved, inserted, conflict = await asyncio.to_thread(add_booking, booking)

    if conflict:
        return web.json_response(
            {"ok": False, "error": "Это время уже занято."},
            status=409
        )
    if saved is None:
        return web.json_response(
            {"ok": False, "error": "Не удалось сохранить бронь."},
            status=409
        )

    notification = True
    if not saved.get("notified") and ADMIN_CHAT_ID:
        notification = await notify_admin(saved)
    elif not ADMIN_CHAT_ID:
        notification = False

    return web.json_response({
        "ok": True,
        "notification": notification,
        "duplicate": not inserted
    }, status=201)


def is_admin(message: Message):
    return bool(ADMIN_CHAT_ID) and message.chat.id == ADMIN_CHAT_ID


async def require_admin(message: Message):
    if not ADMIN_CHAT_ID:
        await message.answer(
            "Сначала настрой ADMIN_CHAT_ID: напиши /id, "
            "вставь число в ADMIN_CHAT_ID и перезапусти бота."
        )
        return False
    if not is_admin(message):
        await message.answer("Нет доступа к списку броней.")
        return False
    return True


@dp.message(Command("start"))
async def start_command(message: Message):
    await message.answer(
        "Бот броней запущен.\n\n"
        "Команды:\n"
        "/today — брони на сегодня\n"
        "/tomorrow — брони на завтра\n"
        "/all — все брони\n"
        "/id — узнать свой Telegram chat ID\n"
        "/help — помощь"
    )


@dp.message(Command("id"))
async def id_command(message: Message):
    await message.answer(
        f"Твой Telegram chat ID: <code>{message.chat.id}</code>\n"
        "Вставь это число в ADMIN_CHAT_ID и перезапусти бота.",
        parse_mode="HTML"
    )


@dp.message(Command("help"))
async def help_command(message: Message):
    await start_command(message)


async def send_booking_list(message: Message, rows, heading):
    if not rows:
        await message.answer(f"{heading}\n\nБроней нет.")
        return

    lines = [f"<b>{html.escape(heading)}</b>"]

    for b in rows:
        escape = lambda value: html.escape(str(value or "—"))
        status = STATUS_NAMES.get(b["status"], b["status"])
        price = f"{int(b['price']):,}".replace(",", " ") + " ֏"

        lines.append(
            f"\n<b>{escape(b['date'])} · {escape(b['time'])}</b> — "
            f"{escape(b['booking_number'])}\n"
            f"{escape(b['first_name'])} {escape(b['last_name'])} · "
            f"{escape(b['phone'])}\n"
            f"{escape(b['service_name'])} · {escape(price)}\n"
            f"Авто: {escape(b['car_model'])} · Статус: {escape(status)}"
        )
        if b["comment"]:
            lines.append(f"Комментарий: {escape(b['comment'])}")

    chunk = ""
    for item in lines:
        candidate = item if not chunk else chunk + "\n" + item
        if len(candidate) > 3500 and chunk:
            await message.answer(chunk, parse_mode="HTML")
            chunk = item
        else:
            chunk = candidate
    if chunk:
        await message.answer(chunk, parse_mode="HTML")


@dp.message(Command("today"))
async def today_command(message: Message):
    if not await require_admin(message):
        return
    day = datetime.now(TZ).date().isoformat()
    rows = await asyncio.to_thread(get_bookings, day)
    await send_booking_list(message, rows, f"Брони на сегодня ({day})")


@dp.message(Command("tomorrow"))
async def tomorrow_command(message: Message):
    if not await require_admin(message):
        return
    day = (datetime.now(TZ).date() + timedelta(days=1)).isoformat()
    rows = await asyncio.to_thread(get_bookings, day)
    await send_booking_list(message, rows, f"Брони на завтра ({day})")


@dp.message(Command("all"))
async def all_command(message: Message):
    if not await require_admin(message):
        return
    rows = await asyncio.to_thread(get_bookings)
    await send_booking_list(message, rows, "Все брони")


async def main():
    if not BOT_TOKEN or ":" not in BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN не настроен. Задай переменную окружения BOT_TOKEN "
            "или впиши токен в код."
        )

    await asyncio.to_thread(init_db)

    app = web.Application(
        client_max_size=64 * 1024,
        middlewares=[cors_middleware]
    )
    app.router.add_get("/", index_page)
    app.router.add_get("/health", health)
    app.router.add_post("/api/bookings", create_booking)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()

    logging.info("HTTP-сервер запущен на http://%s:%s", HOST, PORT)
    logging.info("SQLite база броней: %s", DB_PATH)
    logging.info("Часовой пояс: %s", TZ)
    if not ADMIN_CHAT_ID:
        logging.warning(
            "ADMIN_CHAT_ID = 0 — уведомления в Telegram отключены. "
            "Напиши боту /id и вставь chat_id в ADMIN_CHAT_ID."
        )
    else:
        logging.info("Уведомления будут приходить на chat_id = %s", ADMIN_CHAT_ID)

    try:
        await dp.start_polling(bot)
    finally:
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass