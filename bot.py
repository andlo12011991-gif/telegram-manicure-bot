import os
import json
import asyncio
from dataclasses import dataclass
from datetime import datetime, date, time, timedelta
from typing import Optional, Dict, Any, List, Tuple

import aiosqlite
from dotenv import load_dotenv
from aiogram.types import FSInputFile, InputMediaPhoto
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
    InputMediaPhoto,
)

from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMINS_RAW = os.getenv("ADMINS", "")
ADMINS: List[int] = [int(x.strip()) for x in ADMINS_RAW.split(",") if x.strip().isdigit()]

DB_PATH = "bookings.db"

# ================== НАСТРОЙКИ МАСТЕРА ==================
SALON_NAME = "Маникюр у Ольги • Studio"
CONTACTS_TEXT = "📞 Телефон: +7 950 542-64-60\n💬 Telegram: @sun888sun"
PORTFOLIO_TEXT = "📸 Портфолио: https://instagram.com/yourprofile"

# Рабочие дни/часы
WORK_START = time(10, 0)
WORK_END = time(20, 0)

# Шаг слотов (мин)
SLOT_STEP_MIN = 30

# Услуги (название -> длительность в минутах, цена текстом)
SERVICES = {
    "Маникюр": [
        ("Аппаратный маникюр + укрепление ногтевой пластины", 120, "2400 ₽"),
        ("Маникюр комбинированный (без покрытия)", 60, "1500 ₽"),
        ("Ремонт ногтя", 15, "300 ₽"),
        ("Снятие гель-лака на руках", 15, "300 ₽"),
    ],
    "Педикюр": [
        ("Аппаратный педикюр", 90, "2200 ₽"),
        ("Педикюр аппаратный + покрытие гель лаком", 120, "2700 ₽"),
        ("Снятие гель-лака на ногах", 15, "300 ₽"),
    ],
    "Наращивание ногтей": [
        ("Наращивание ногтей акрил гелем", 150, "3200 ₽"),
        ("Наращивание ногтей френч", 150, "3500 ₽"),
    ]
}

# Сколько дней показываем в календаре
DAYS_AHEAD = 7


# ================== UI ==================
def main_kb() -> ReplyKeyboardMarkup:
   return ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📅 Записаться")],
        [KeyboardButton(text="💅 Услуги и цены"), KeyboardButton(text="🕒 Время работы")],
        [KeyboardButton(text="📸 Портфолио"), KeyboardButton(text="📞 Контакты")],
    ],
    resize_keyboard=True
)
def reply_kb(client_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✍️ Ответить", callback_data=f"reply:{client_id}")]
    ])

def cancel_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена ответа")]],
        resize_keyboard=True
    )

def cancel_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="❌ Отмена")]],
        resize_keyboard=True
    )
def removal_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да", callback_data="rm:yes"),
            InlineKeyboardButton(text="❌ Нет", callback_data="rm:no"),
        ]
    ])
def services_kb():
    rows = []
    for category in SERVICES.keys():
        rows.append([
            InlineKeyboardButton(
                text=category,
                callback_data=f"cat:{category}"
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:main")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
def category_services_kb(category: str):
    rows = []
    for idx, (name, minutes, price) in enumerate(SERVICES[category]):
        rows.append([
            InlineKeyboardButton(
                text=f"{name} • {price} • {minutes} мин",
                callback_data=f"svc:{category}:{idx}"
            )
        ])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back:categories")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
def days_kb() -> InlineKeyboardMarkup:
    rows = []
    today = date.today()

    weekdays = {
        0: "Пн",
        1: "Вт",
        2: "Ср",
        3: "Чт",
        4: "Пт",
        5: "Сб",
        6: "Вс",
    }

    for i in range(DAYS_AHEAD):
        d = today + timedelta(days=i)
        label = f"{weekdays[d.weekday()]} {d.strftime('%d.%m')}"
        rows.append([
            InlineKeyboardButton(
                text=label,
                callback_data=f"day:{d.isoformat()}"
            )
        ])

    rows.append([
        InlineKeyboardButton(
            text="⬅️ Назад",
            callback_data="back:services"
        )
    ])

    return InlineKeyboardMarkup(inline_keyboard=rows)
def times_kb(day_iso: str, times: List[str]) -> InlineKeyboardMarkup:
    rows = []
    # 2 кнопки в ряд
    row = []
    for t in times:
        row.append(InlineKeyboardButton(text=t, callback_data=f"time:{day_iso}:{t}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data=f"back:days")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ================== FSM ==================
class BookingFlow(StatesGroup):
    phone = State()
    name = State()
    removal = State()   # <-- НОВОЕ состояние (снятие да/нет)
    comment = State()
class AdminReplyFlow(StatesGroup):
    waiting_message = State()


# ================== DB ==================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            tg_user_id INTEGER NOT NULL,
            tg_username TEXT,
            customer_name TEXT,
            phone TEXT,
            service_name TEXT NOT NULL,
            duration_min INTEGER NOT NULL,
            price_text TEXT NOT NULL,
            day_iso TEXT NOT NULL,
            time_hhmm TEXT NOT NULL,
            comment TEXT,
            status TEXT NOT NULL, -- pending/confirmed/canceled
            admin_note TEXT
        )
        """)
        # Уникальный слот: нельзя 2 записи на одно время в один день (даже pending/confirmed)
        await db.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS ux_slot
        ON bookings(day_iso, time_hhmm)
        """)
        await db.commit()

async def create_pending_booking(
    user_id: int,
    username: Optional[str],
    customer_name: str,
    phone: str,
    service_name: str,
    duration_min: int,
    price_text: str,
    day_iso: str,
    time_hhmm: str,
    comment: str
) -> int:
    now = datetime.now().isoformat(timespec="seconds")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO bookings (
                created_at, tg_user_id, tg_username, customer_name, phone,
                service_name, duration_min, price_text,
                day_iso, time_hhmm, comment, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (
            now, user_id, username or "", customer_name, phone,
            service_name, duration_min, price_text,
            day_iso, time_hhmm, comment
        ))
        await db.commit()
        return cur.lastrowid

async def get_booked_times(day_iso: str) -> List[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT time_hhmm FROM bookings
            WHERE day_iso = ? AND status IN ('pending','confirmed')
        """, (day_iso,))
        rows = await cur.fetchall()
        return [r[0] for r in rows]

async def set_status(booking_id: int, status: str, admin_note: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE bookings SET status=?, admin_note=?
            WHERE id=?
        """, (status, admin_note, booking_id))
        await db.commit()

async def get_booking(booking_id: int) -> Optional[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, created_at, tg_user_id, tg_username, customer_name, phone,
                   service_name, duration_min, price_text, day_iso, time_hhmm, comment, status
            FROM bookings WHERE id=?
        """, (booking_id,))
        r = await cur.fetchone()
        if not r:
            return None
        keys = ["id","created_at","tg_user_id","tg_username","customer_name","phone",
                "service_name","duration_min","price_text","day_iso","time_hhmm","comment","status"]
        return dict(zip(keys, r))

def format_booking_admin(b: Dict[str, Any]) -> str:
    uname = f"@{b['tg_username']}" if b.get("tg_username") else "(нет username)"
    return (
        f"💅 *Запись #{b['id']}* ({b['status']})\n"
        f"👤 {b.get('customer_name') or '—'} {uname}\n"
        f"🆔 TG ID: `{b['tg_user_id']}`\n\n"
        f"*Услуга:* {b['service_name']} • {b['price_text']} • {b['duration_min']} мин\n"
        f"*Дата/время:* {b['day_iso']} {b['time_hhmm']}\n"
        f"*Телефон:* {b.get('phone') or '—'}\n"
        f"*Комментарий:* {b.get('comment') or '—'}\n"
    )

def admin_actions_kb(booking_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"adm:ok:{booking_id}"),
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"adm:no:{booking_id}")
        ]
    ])


# ================== SLOT LOGIC ==================
def generate_day_slots() -> List[str]:
    # генерируем HH:MM с шагом SLOT_STEP_MIN
    slots = []
    dt = datetime.combine(date.today(), WORK_START)
    end_dt = datetime.combine(date.today(), WORK_END)
    step = timedelta(minutes=SLOT_STEP_MIN)
    while dt < end_dt:
        slots.append(dt.strftime("%H:%M"))
        dt += step
    return slots

async def get_free_times_for_day(day_iso: str) -> List[str]:
    all_slots = generate_day_slots()
    booked = set(await get_booked_times(day_iso))
    return [t for t in all_slots if t not in booked]


# ================== BOT ==================
dp = Dispatcher()

@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer(
        f"Привет! 👋 Я бот записи на {SALON_NAME}\n"
        "Можно посмотреть цены и записаться онлайн 👇",
        reply_markup=main_kb()
    )

@dp.message(Command("testadmin"))
async def test_admin(m: Message, bot: Bot):
    for admin_id in ADMINS:
        await bot.send_message(admin_id, "✅ Тест: сообщение админу дошло!")
    await m.answer("Отправил тест админу.")

@dp.message(F.text == "💅 Услуги и цены")
async def prices(m: Message):
    lines = ["💅 *Услуги и цены*"]

    for category, items in SERVICES.items():
        lines.append(f"\n*{category}*")
        for name, minutes, price in items:
            lines.append(f"• {name} — {price} ({minutes} мин)")

    lines.append("\n📅 Для записи нажмите «Записаться».")
    await m.answer("\n".join(lines), parse_mode="Markdown")

@dp.message(F.text == "🕒 Время работы")
async def work_time(m: Message):
    await m.answer(
        "🕒 *Время работы:*\n\n"
        "Пн – Вс: 10:00 – 20:00\n\n"
        "Работаем без выходных 💅",
        parse_mode="Markdown"
    )

@dp.message(F.text == "📞 Контакты")
async def contacts(m: Message):
    await m.answer(CONTACTS_TEXT)

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORTFOLIO_DIR = os.path.join(BASE_DIR, "portfolio")


@dp.message(F.text == "📸 Портфолио")
async def portfolio(m: Message):
    media = []

    for i in range(1, 10):  # максимум 9 фото
        file_path = os.path.join(PORTFOLIO_DIR, f"{i}.jpg")

        if not os.path.exists(file_path):
            break

        if i == 1:
            media.append(
                InputMediaPhoto(
                    media=FSInputFile(file_path),
                    caption="📸 Примеры работ 💅"
                )
            )
        else:
            media.append(
                InputMediaPhoto(media=FSInputFile(file_path))
            )

    if not media:
        await m.answer("❌ Фото не найдены.\nПроверь папку portfolio.")
        return

    await m.answer_media_group(media)

@dp.message(F.text == "📅 Записаться")
async def book_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Выберите услугу 👇", reply_markup=cancel_kb())
    await m.answer("💅 Услуги:", reply_markup=services_kb())

@dp.message(F.text == "❌ Отмена")
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("Ок, отменено. Меню 👇", reply_markup=main_kb())

@dp.callback_query(BookingFlow.removal, F.data.startswith("rm:"))
async def pick_removal(cb: CallbackQuery, state: FSMContext):
    choice = cb.data.split(":", 1)[1]  # yes / no
    removal_value = "Да" if choice == "yes" else "Нет"

    await state.update_data(removal=removal_value)

    await state.set_state(BookingFlow.comment)

    await cb.message.answer("✍️ Напишите комментарий:")
    await cb.answer()
@dp.callback_query(F.data == "back:main")
async def back_main(cb: CallbackQuery):
    await cb.message.answer("Меню 👇", reply_markup=main_kb())
    await cb.answer()
@dp.callback_query(F.data.startswith("cat:"))
async def pick_category(cb: CallbackQuery):
    category = cb.data.split(":", 1)[1]
    await cb.message.edit_text(
        f"Выберите услугу в категории: {category}",
        reply_markup=category_services_kb(category)
    )
    await cb.answer()
@dp.callback_query(F.data == "back:services")
async def back_services(cb: CallbackQuery):
    await cb.message.edit_text("💅 Услуги:", reply_markup=services_kb())
    await cb.answer()

@dp.callback_query(F.data == "back:days")
async def back_days(cb: CallbackQuery):
    await cb.message.edit_text("📅 Выберите день:", reply_markup=days_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("svc:"))
async def pick_service(cb: CallbackQuery, state: FSMContext):
    _, category, idx = cb.data.split(":")
    idx = int(idx)

    name, minutes, price = SERVICES[category][idx]

    await state.update_data(
        service_name=name,
        duration_min=minutes,
        price_text=price
    )

    await cb.message.edit_text("📅 Выберите день:", reply_markup=days_kb())
    await cb.answer()

@dp.callback_query(F.data.startswith("day:"))
async def pick_day(cb: CallbackQuery, state: FSMContext):
    day_iso = cb.data.split(":", 1)[1]
    free = await get_free_times_for_day(day_iso)
    if not free:
        await cb.answer("На этот день нет свободных слотов 😔", show_alert=True)
        return
    await state.update_data(day_iso=day_iso)
    await cb.message.edit_text("🕒 Выберите время:", reply_markup=times_kb(day_iso, free))
    await cb.answer()

@dp.callback_query(F.data.startswith("time:"))
async def pick_time(cb: CallbackQuery, state: FSMContext):
    parts = cb.data.split(":")
    day_iso = parts[1]
    hhmm = f"{parts[2]}:{parts[3]}"
    data = await state.get_data()
    # Проверим занятость ещё раз (на случай гонки)
    booked = set(await get_booked_times(day_iso))
    if hhmm in booked:
        await cb.answer("Этот слот уже заняли. Выберите другое время.", show_alert=True)
        free = await get_free_times_for_day(day_iso)
        await cb.message.edit_text("🕒 Выберите время:", reply_markup=times_kb(day_iso, free))
        return

    await state.update_data(time_hhmm=hhmm)
    await state.set_state(BookingFlow.phone)
    await cb.message.answer("📞 Введите телефон (можно в свободном формате):", reply_markup=cancel_kb())
    await cb.answer()

@dp.message(BookingFlow.phone)
async def step_phone(m: Message, state: FSMContext):
    await state.update_data(phone=m.text.strip())
    await state.set_state(BookingFlow.name)
    await m.answer("👤 Как вас зовут?")

@dp.message(BookingFlow.name)
async def step_name(m: Message, state: FSMContext):
    await state.update_data(customer_name=m.text.strip())
    await state.set_state(BookingFlow.removal)
    await m.answer("Снятие нужно?", reply_markup=removal_kb())

@dp.message(BookingFlow.comment)
async def step_comment(m: Message, state: FSMContext, bot: Bot):
    await state.update_data(comment=m.text.strip())
    data = await state.get_data()
    removal = data.get("removal", "—")

    # повторная проверка слота перед записью в БД
    day_iso = data["day_iso"]
    hhmm = data["time_hhmm"]
    booked = set(await get_booked_times(day_iso))
    if hhmm in booked:
        await m.answer("Упс, это время уже заняли 😔 Нажмите «📅 Записаться» и выберите другое время.", reply_markup=main_kb())
        await state.clear()
        return

    try:
        booking_id = await create_pending_booking(
            user_id=m.from_user.id,
            username=m.from_user.username,
            customer_name=data["customer_name"],
            phone=data["phone"],
            service_name=data["service_name"],
            duration_min=int(data["duration_min"]),
            price_text=data["price_text"],
            day_iso=day_iso,
            time_hhmm=hhmm,
            comment=data["comment"]
        )
    except Exception:
        # если сработал UNIQUE (кто-то успел раньше)
        await m.answer("Это время только что заняли. Выберите другое 👇", reply_markup=main_kb())
        await state.clear()
        return

    await state.clear()

    # Клиенту
    await m.answer(
        "✅ Заявка на запись отправлена!\n"
        f"Услуга: {data['service_name']} ({data['price_text']})\n"
        f"Дата/время: {day_iso} {hhmm}\n\n"
        "Я напишу вам, когда мастер подтвердит запись.",
        reply_markup=main_kb()
    )

    # Админу
    b = await get_booking(booking_id)
    if b:
        text = format_booking_admin(b)
        for admin_id in ADMINS:
            try:
                await bot.send_message(admin_id, text, parse_mode="Markdown", reply_markup=admin_actions_kb(booking_id))
            except Exception:
                pass

@dp.callback_query(F.data.startswith("adm:ok:"))
async def admin_confirm(cb: CallbackQuery, bot: Bot):
    booking_id = int(cb.data.split(":")[2])
    b = await get_booking(booking_id)
    if not b:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    await set_status(booking_id, "confirmed")
    b2 = await get_booking(booking_id)

    # Админу обновим текст
    await cb.message.edit_text(format_booking_admin(b2), parse_mode="Markdown")
    await cb.answer("Подтверждено ✅")

    # Клиенту
    try:
        await bot.send_message(
            b["tg_user_id"],
            "✅ Ваша запись подтверждена!\n"
            f"💅 {b['service_name']} ({b['price_text']})\n"
            f"📅 {b['day_iso']} {b['time_hhmm']}\n\n"
            "Если нужно перенести — напишите сюда."
        )
    except Exception:
        pass
@dp.callback_query(BookingFlow.removal, F.data.startswith("rm:"))
async def pick_removal(cb: CallbackQuery, state: FSMContext):
    choice = cb.data.split(":", 1)[1]   # yes / no
    removal_value = "Да" if choice == "yes" else "Нет"

    await state.update_data(removal=removal_value)

    await state.set_state(BookingFlow.comment)

    await cb.message.answer(
        "Комментарий:\n"
        "• Длина / форма\n"
        "• Пожелания по дизайну\n\n"
        "Если ничего нет — напишите 'нет'"
    )
    await cb.answer()
@dp.callback_query(F.data.startswith("reply:"))
async def start_reply(cb: CallbackQuery, state: FSMContext):
    # Только админам
    if cb.from_user.id not in ADMINS:
        await cb.answer("Нет доступа", show_alert=True)
        return

    client_id = int(cb.data.split(":", 1)[1])

    await state.set_state(AdminReplyFlow.waiting_message)
    await state.update_data(reply_to=client_id)

    await cb.message.answer(
        f"✍️ Напишите ответ клиенту (ID: {client_id}).\n"
        "Можно текст или фото.\n\n"
        "Чтобы отменить — нажмите «❌ Отмена ответа».",
        reply_markup=cancel_reply_kb()
    )
    await cb.answer()
@dp.callback_query(F.data.startswith("adm:no:"))
async def admin_cancel(cb: CallbackQuery, bot: Bot):
    booking_id = int(cb.data.split(":")[2])
    b = await get_booking(booking_id)
    if not b:
        await cb.answer("Запись не найдена", show_alert=True)
        return
    await set_status(booking_id, "canceled")
    b2 = await get_booking(booking_id)

    await cb.message.edit_text(format_booking_admin(b2), parse_mode="Markdown")
    await cb.answer("Отменено ❌")

    try:
        await bot.send_message(
            b["tg_user_id"],
            "❌ Запись отменена мастером.\n"
            "Нажмите «📅 Записаться», чтобы выбрать другое время."
        )
    except Exception:
        pass
@dp.message(F.text == "❌ Отмена ответа")
async def cancel_admin_reply(m: Message, state: FSMContext):
    if m.from_user.id not in ADMINS:
        return

    if await state.get_state() == AdminReplyFlow.waiting_message:
        await state.clear()
        await m.answer("Ок, отменил ответ. Меню 👇", reply_markup=main_kb())
@dp.message(AdminReplyFlow.waiting_message)
async def send_admin_reply(m: Message, state: FSMContext, bot: Bot):
    if m.from_user.id not in ADMINS:
        return

    data = await state.get_data()
    client_id = int(data.get("reply_to"))

    try:
        if m.text:
            await bot.send_message(client_id, "💬 Сообщение от мастера:\n\n" + m.text)
        else:
            # если фото/видео/файл — пересылаем как есть
            await bot.forward_message(client_id, m.chat.id, m.message_id)

        await m.answer("✅ Ответ отправлен клиенту.", reply_markup=main_kb())
    except Exception as e:
        await m.answer(f"❌ Не смог отправить клиенту. Причина: {e}", reply_markup=main_kb())

    await state.clear()
@dp.message()
async def forward_to_admin(m: Message, bot: Bot):
    u = m.from_user
    if not u:
        return

    # Если пишет админ — не пересылаем самому себе
    if u.id in ADMINS:
        return

    uname = f"@{u.username}" if u.username else "(нет username)"
    header = (
        "💬 Сообщение от клиента\n"
        f"👤 {u.full_name} {uname}\n"
        f"🆔 `{u.id}`\n"
    )

    for admin_id in ADMINS:
        try:
            # отправляем админу "шапку" + кнопку Ответить
            if m.text:
                await bot.send_message(
                    admin_id,
                    header + "\n\n" + m.text,
                    parse_mode="Markdown",
                    reply_markup=reply_kb(u.id),
                )
            else:
                await bot.send_message(
                    admin_id,
                    header,
                    parse_mode="Markdown",
                    reply_markup=reply_kb(u.id),
                )
                await bot.forward_message(admin_id, m.chat.id, m.message_id)
        except Exception:
            pass

    await m.answer("✅ Сообщение получено. Мы скоро ответим.")


async def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN пуст. Проверь .env")

    await init_db()

    session = AiohttpSession(timeout=120)
    bot = Bot(BOT_TOKEN, session=session)

    await dp.start_polling(bot)


from aiogram.exceptions import TelegramNetworkError
import time

if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except TelegramNetworkError:
            print("Сеть/Telegram timeout. Повтор через 5 секунд...")
            time.sleep(5)
        except KeyboardInterrupt:
            print("Остановлено (Ctrl+C)")
            break