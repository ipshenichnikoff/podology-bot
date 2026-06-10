#!/usr/bin/env python3
"""
PODOLOGY BOT — бот для записи клиентов подолога
Запуск: python bot.py

Зависимости:
    pip install python-telegram-bot python-dotenv

Переменные окружения (файл .env):
    BOT_TOKEN=...
    ADMIN_ID=...
    MASTER_NAME=Имя мастера
    MASTER_USERNAME=@username
    MASTER_PHONE=+7 999 000-00-00
"""

import sqlite3
db = sqlite3.connect("appointments.db")
import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from functools import wraps

from dotenv import load_dotenv
from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Update,
    InputMediaPhoto,
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters,
)

# ── Настройки ──────────────────────────────────────────────────────────────────
load_dotenv()
BOT_TOKEN        = os.getenv("BOT_TOKEN", "8827220812:AAG7FxZR778sSDX9a_BBicW7Datw-7s7Mdg")
ADMIN_ID         = int(os.getenv("ADMIN_ID", "223326752"))
MASTER_NAME      = os.getenv("MASTER_NAME", "Мастер")
MASTER_USERNAME  = os.getenv("MASTER_USERNAME", "@master")
MASTER_PHONE     = os.getenv("MASTER_PHONE", "")
# Фото мастера для онбординга — file_id из Telegram или None
MASTER_PHOTO_ID  = os.getenv("MASTER_PHOTO_ID", None)

if not BOT_TOKEN or not ADMIN_ID:
    raise RuntimeError("Установите BOT_TOKEN и ADMIN_ID в файле .env")

WORK_START      = 10
WORK_END        = 20
SLOT_MIN        = 60
MAX_ACTIVE_APTS = 2
DB_FILE         = "appointments.db"

# ── Состояния диалога ──────────────────────────────────────────────────────────
(
    ONBOARDING,
    CLIENT_MENU, CHOOSE_PROC, CHOOSE_DATE, CHOOSE_TIME, CONFIRM_BOOK,
    ENTER_NAME, ENTER_PHONE,
    RESCHEDULE_DATE, RESCHEDULE_TIME,
    ADMIN_MENU, ADMIN_DATE,
    ADMIN_ADD_DATE, ADMIN_ADD_TIME, ADMIN_ADD_PROC, ADMIN_ADD_NAME, ADMIN_ADD_PHONE,
    ADMIN_BLOCK_DATE, ADMIN_BLOCK_TIME,
) = range(19)

# ── Процедуры ──────────────────────────────────────────────────────────────────
PROCEDURES = [
    ("Педикюр аппаратный",      60, 2800),
    ("Гигиенический педикюр",   45, 2200),
    ("Лечение вросшего ногтя",  90, 3500),
    ("Протезирование ногтя",    90, 4500),
    ("Ортониксия (скоба/нить)", 60, 5200),
    ("Лечение грибка",          60, 3200),
]

MONTHS = ["","января","февраля","марта","апреля","мая","июня",
          "июля","августа","сентября","октября","ноября","декабря"]
DAYS   = ["Понедельник","Вторник","Среда","Четверг","Пятница","Суббота","Воскресенье"]
SHORT_DAYS = ["пн","вт","ср","чт","пт","сб","вс"]

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()],
)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# База данных (SQLite)
# ══════════════════════════════════════════════════════════════════════════════

_db_lock = threading.Lock()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with _db_lock, get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS appointments (
                id        TEXT PRIMARY KEY,
                date      TEXT NOT NULL,
                time      TEXT NOT NULL,
                name      TEXT NOT NULL,
                phone     TEXT NOT NULL,
                user_id   INTEGER NOT NULL,
                procedure TEXT NOT NULL,
                duration  INTEGER NOT NULL,
                price     INTEGER NOT NULL,
                status    TEXT NOT NULL DEFAULT 'confirmed',
                created   TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS blocked (
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                PRIMARY KEY (date, time)
            );
            CREATE TABLE IF NOT EXISTS users (
                user_id    INTEGER PRIMARY KEY,
                first_name TEXT,
                seen_onboarding INTEGER DEFAULT 0
            );
        """)


# ── Пользователи ──────────────────────────────────────────────────────────────

def db_has_seen_onboarding(user_id: int) -> bool:
    with _db_lock, get_conn() as conn:
        row = conn.execute(
            "SELECT seen_onboarding FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
    return bool(row and row["seen_onboarding"])


def db_mark_onboarding(user_id: int, first_name: str = ""):
    with _db_lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO users (user_id, first_name, seen_onboarding) VALUES (?,?,1) "
            "ON CONFLICT(user_id) DO UPDATE SET seen_onboarding=1",
            (user_id, first_name)
        )


# ── Записи ────────────────────────────────────────────────────────────────────

def db_get_booked_slots(date_str: str) -> list:
    with _db_lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT time FROM appointments WHERE date=? AND status!='cancelled'",
            (date_str,)
        ).fetchall()
    return [r["time"] for r in rows]


def db_get_blocked_slots(date_str: str) -> list:
    with _db_lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT time FROM blocked WHERE date=?", (date_str,)
        ).fetchall()
    return [r["time"] for r in rows]


def db_get_free_slots(date_str: str) -> list:
    booked  = db_get_booked_slots(date_str)
    blocked = db_get_blocked_slots(date_str)
    taken   = set(booked + blocked)
    slots, t = [], WORK_START * 60
    while t + SLOT_MIN <= WORK_END * 60:
        hh, mm = divmod(t, 60)
        s = f"{hh:02d}:{mm:02d}"
        if s not in taken:
            slots.append(s)
        t += SLOT_MIN
    return slots


def db_get_all_slots(date_str: str) -> list:
    """Все слоты дня с пометкой занятости."""
    booked  = set(db_get_booked_slots(date_str))
    blocked = set(db_get_blocked_slots(date_str))
    slots, t = [], WORK_START * 60
    while t + SLOT_MIN <= WORK_END * 60:
        hh, mm = divmod(t, 60)
        s = f"{hh:02d}:{mm:02d}"
        status = "free"
        if s in booked:
            status = "booked"
        elif s in blocked:
            status = "blocked"
        slots.append((s, status))
        t += SLOT_MIN
    return slots


def db_count_active(user_id: int) -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    with _db_lock, get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM appointments "
            "WHERE user_id=? AND status='confirmed' AND date>=?",
            (user_id, today)
        ).fetchone()
    return row["cnt"]


def db_add_appointment(date, time, name, phone, user_id,
                       procedure, duration, price) -> str:
    apt_id = f"{date}-{time.replace(':','-')}-{user_id}"
    with _db_lock, get_conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO appointments "
            "(id,date,time,name,phone,user_id,procedure,duration,price,status,created) "
            "VALUES (?,?,?,?,?,?,?,?,?,'confirmed',?)",
            (apt_id, date, time, name, phone, user_id,
             procedure, duration, price,
             datetime.now().strftime("%Y-%m-%d %H:%M"))
        )
    return apt_id


def db_cancel_appointment(apt_id: str) -> dict | None:
    with _db_lock, get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM appointments WHERE id=? AND status='confirmed'", (apt_id,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE appointments SET status='cancelled' WHERE id=?", (apt_id,)
            )
            return dict(row)
    return None


def db_reschedule(apt_id: str, new_date: str, new_time: str) -> bool:
    uid    = apt_id.split("-")[-1]
    new_id = f"{new_date}-{new_time.replace(':','-')}-{uid}"
    with _db_lock, get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM appointments WHERE id=?", (apt_id,)
        ).fetchone()
        if not row:
            return False
        conn.execute(
            "UPDATE appointments SET id=?,date=?,time=?,status='confirmed' WHERE id=?",
            (new_id, new_date, new_time, apt_id)
        )
    return True


def db_get_day_schedule(date_str: str) -> list:
    with _db_lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM appointments WHERE date=? AND status!='cancelled' ORDER BY time",
            (date_str,)
        ).fetchall()
    return [dict(r) for r in rows]


def db_get_user_appointments(user_id: int) -> list:
    today = datetime.now().strftime("%Y-%m-%d")
    with _db_lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM appointments WHERE user_id=? AND status='confirmed' "
            "AND date>=? ORDER BY date, time",
            (user_id, today)
        ).fetchall()
    return [dict(r) for r in rows]


def db_get_appointment(apt_id: str) -> dict | None:
    with _db_lock, get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM appointments WHERE id=?", (apt_id,)
        ).fetchone()
    return dict(row) if row else None


def db_block_slot(date: str, time: str) -> bool:
    try:
        with _db_lock, get_conn() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO blocked (date,time) VALUES (?,?)", (date, time)
            )
        return True
    except Exception:
        return False


def db_unblock_slot(date: str, time: str) -> bool:
    with _db_lock, get_conn() as conn:
        cur = conn.execute(
            "DELETE FROM blocked WHERE date=? AND time=?", (date, time)
        )
    return cur.rowcount > 0


def db_get_blocked_all() -> list:
    today = datetime.now().strftime("%Y-%m-%d")
    with _db_lock, get_conn() as conn:
        rows = conn.execute(
            "SELECT date, time FROM blocked WHERE date>=? ORDER BY date, time",
            (today,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Статистика ────────────────────────────────────────────────────────────────

def db_stats(period_days: int = 30) -> dict:
    since = (datetime.now() - timedelta(days=period_days)).strftime("%Y-%m-%d")
    with _db_lock, get_conn() as conn:
        total = conn.execute(
            "SELECT COUNT(*) AS cnt FROM appointments WHERE date>=? AND status='confirmed'",
            (since,)
        ).fetchone()["cnt"]

        revenue = conn.execute(
            "SELECT COALESCE(SUM(price),0) AS s FROM appointments "
            "WHERE date>=? AND status='confirmed'",
            (since,)
        ).fetchone()["s"]

        cancelled = conn.execute(
            "SELECT COUNT(*) AS cnt FROM appointments WHERE date>=? AND status='cancelled'",
            (since,)
        ).fetchone()["cnt"]

        top_proc = conn.execute(
            "SELECT procedure, COUNT(*) AS cnt FROM appointments "
            "WHERE date>=? AND status='confirmed' "
            "GROUP BY procedure ORDER BY cnt DESC LIMIT 1",
            (since,)
        ).fetchone()

        unique_clients = conn.execute(
            "SELECT COUNT(DISTINCT user_id) AS cnt FROM appointments "
            "WHERE date>=? AND status='confirmed'",
            (since,)
        ).fetchone()["cnt"]

        upcoming = conn.execute(
            "SELECT COUNT(*) AS cnt FROM appointments "
            "WHERE date>=? AND status='confirmed'",
            (datetime.now().strftime("%Y-%m-%d"),)
        ).fetchone()["cnt"]

    cancel_rate = round(cancelled / (total + cancelled) * 100) if (total + cancelled) else 0
    return {
        "total": total,
        "revenue": revenue,
        "cancelled": cancelled,
        "cancel_rate": cancel_rate,
        "top_proc": dict(top_proc) if top_proc else None,
        "unique_clients": unique_clients,
        "upcoming": upcoming,
        "period_days": period_days,
    }


# ══════════════════════════════════════════════════════════════════════════════
# Вспомогательные функции UI
# ══════════════════════════════════════════════════════════════════════════════

def fmt_date(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{DAYS[d.weekday()]}, {d.day} {MONTHS[d.month]}"


def fmt_date_short(date_str: str) -> str:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{SHORT_DAYS[d.weekday()]} {d.day} {MONTHS[d.month]}"


def next_n_days(n: int = 14) -> list:
    days, d = [], datetime.now()
    if d.hour >= WORK_END:
        d += timedelta(days=1)
    while len(days) < n:
        if d.weekday() < 6:
            days.append(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
    return days


def validate_phone(phone: str) -> bool:
    digits = re.sub(r"\D", "", phone)
    return 7 <= len(digits) <= 15


def step_header(step: int, total: int, title: str) -> str:
    """Прогресс-бар: шаг N из M."""
    filled = "●" * step
    empty  = "○" * (total - step)
    return f"{filled}{empty}  Шаг {step} из {total}: {title}\n\n"


# ── Клавиатуры ────────────────────────────────────────────────────────────────

def proc_keyboard(back_cb: str = "back_main") -> InlineKeyboardMarkup:
    rows = []
    for i, (name, dur, price) in enumerate(PROCEDURES):
        dur_label  = f"⏱ {dur} мин"
        price_label = f"💰 {price:,} ₽"
        rows.append([InlineKeyboardButton(
            f"{name}  ·  {dur_label}  ·  {price_label}",
            callback_data=f"proc_{i}"
        )])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


def dates_keyboard(slots_map: dict, back_cb: str = "back_main") -> InlineKeyboardMarkup:
    rows = []
    for date, free in slots_map.items():
        d_obj = datetime.strptime(date, "%Y-%m-%d")
        short = f"{SHORT_DAYS[d_obj.weekday()]}, {d_obj.day} {MONTHS[d_obj.month]}"
        if not free:
            rows.append([InlineKeyboardButton(f"🚫  {short} — занято", callback_data="noop")])
        else:
            rows.append([InlineKeyboardButton(
                f"📅  {short} — {len(free)} свободных окна",
                callback_data=f"date_{date}"
            )])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


def times_keyboard_grid(all_slots: list, back_cb: str = "back_date") -> InlineKeyboardMarkup:
    """
    Сетка слотов: свободные — кликабельны, занятые — серые (noop).
    all_slots: [(time_str, status), ...] где status in {'free','booked','blocked'}
    """
    rows = []
    row  = []
    for time_str, status in all_slots:
        if status == "free":
            btn = InlineKeyboardButton(f"✅ {time_str}", callback_data=f"time_{time_str}")
        else:
            btn = InlineKeyboardButton(f"✖ {time_str}", callback_data="noop")
        row.append(btn)
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data=back_cb)])
    return InlineKeyboardMarkup(rows)


def main_menu_kb(is_admin: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📅 Записаться на приём",  callback_data="book")],
        [InlineKeyboardButton("📋 Мои записи",           callback_data="my_apts")],
        [InlineKeyboardButton("ℹ️ Услуги и цены",        callback_data="prices")],
        [InlineKeyboardButton("📞 Связаться с мастером", callback_data="contact")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton("🔐 Панель мастера", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Расписание на день",     callback_data="adm_schedule")],
        [InlineKeyboardButton("📆 Расписание на неделю",   callback_data="adm_week")],
        [InlineKeyboardButton("➕ Добавить запись",        callback_data="adm_add")],
        [InlineKeyboardButton("🚫 Заблокировать слот",     callback_data="adm_block")],
        [InlineKeyboardButton("🔓 Разблокировать слот",    callback_data="adm_unblock")],
        [InlineKeyboardButton("❌ Отменить запись",         callback_data="adm_cancel")],
        [InlineKeyboardButton("📊 Статистика",             callback_data="adm_stats")],
        [InlineKeyboardButton("◀️ Главное меню",           callback_data="back_main")],
    ])


# ══════════════════════════════════════════════════════════════════════════════
# Декоратор admin_only
# ══════════════════════════════════════════════════════════════════════════════

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id != ADMIN_ID:
            if update.callback_query:
                await update.callback_query.answer("⛔ Доступ запрещён", show_alert=True)
            return CLIENT_MENU
        return await func(update, ctx, *args, **kwargs)
    return wrapper


# ══════════════════════════════════════════════════════════════════════════════
# Напоминания и пост-визитное сообщение
# ══════════════════════════════════════════════════════════════════════════════

async def send_reminder(context: ContextTypes.DEFAULT_TYPE):
    job     = context.job
    apt_id  = job.data["apt_id"]
    user_id = job.data["user_id"]
    hours   = job.data["hours"]
    apt = db_get_appointment(apt_id)
    if not apt or apt["status"] != "confirmed":
        return
    label = "Завтра" if hours == 24 else "Сегодня"
    try:
        await context.bot.send_message(
            user_id,
            f"🔔 *Напоминание о записи*\n\n"
            f"📅 {label} — {fmt_date(apt['date'])} в *{apt['time']}*\n"
            f"💆 {apt['procedure']}\n"
            f"⏱ {apt['duration']} мин · 💰 {apt['price']:,} ₽\n\n"
            "_Если планы изменились — отмените запись заранее, нажав «Мои записи»._",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.warning("Напоминание не отправлено %s: %s", apt_id, e)


async def send_post_visit(context: ContextTypes.DEFAULT_TYPE):
    job     = context.job
    apt     = db_get_appointment(job.data["apt_id"])
    user_id = job.data["user_id"]
    if not apt:
        return
    try:
        await context.bot.send_message(
            user_id,
            f"😊 *Как прошёл приём?*\n\n"
            f"Надеемся, вам всё понравилось!\n"
            f"Если хотите записаться снова — нажмите кнопку ниже.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📅 Записаться снова", callback_data="book")
            ]]),
        )
    except Exception as e:
        log.warning("Пост-визит не отправлен %s: %s", job.data["apt_id"], e)


def schedule_reminders(app, apt_id: str, user_id: int, date_str: str,
                        time_str: str, duration: int = SLOT_MIN):
    try:
        apt_dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        for hours in (24, 1):
            fire_at = apt_dt - timedelta(hours=hours)
            if fire_at > datetime.now():
                app.job_queue.run_once(
                    send_reminder,
                    when=fire_at,
                    data={"apt_id": apt_id, "user_id": user_id, "hours": hours},
                    name=f"reminder_{apt_id}_{hours}h",
                )
        # Пост-визит — через 1 ч после окончания приёма
        end_dt   = apt_dt + timedelta(minutes=duration)
        visit_at = end_dt + timedelta(hours=1)
        if visit_at > datetime.now():
            app.job_queue.run_once(
                send_post_visit,
                when=visit_at,
                data={"apt_id": apt_id, "user_id": user_id},
                name=f"postvisit_{apt_id}",
            )
    except Exception as e:
        log.warning("Не удалось запланировать задачи: %s", e)


def cancel_reminders(app, apt_id: str):
    for suffix in ("24h", "1h"):
        for job in app.job_queue.get_jobs_by_name(f"reminder_{apt_id}_{suffix}"):
            job.schedule_removal()
    for job in app.job_queue.get_jobs_by_name(f"postvisit_{apt_id}"):
        job.schedule_removal()


# ══════════════════════════════════════════════════════════════════════════════
# КЛИЕНТСКИЕ хендлеры
# ══════════════════════════════════════════════════════════════════════════════

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    user     = update.effective_user
    is_admin = user.id == ADMIN_ID

    # Повторный /start — сразу меню
    if db_has_seen_onboarding(user.id) or is_admin:
        await update.message.reply_text(
            f"👋 С возвращением, *{user.first_name}*!\n\nЧем могу помочь?",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(is_admin),
        )
        return CLIENT_MENU

    # Онбординг — первый визит
    intro = (
        f"👋 Привет, *{user.first_name}*!\n\n"
        f"Я помогу вам записаться к подологу *{MASTER_NAME}*.\n\n"
        f"🏥 Принимаю по записи пн–сб, 10:00–20:00\n"
        f"📍 Адрес уточните у мастера\n\n"
        f"Запись занимает меньше минуты 👇"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("📅 Записаться", callback_data="onboard_book"),
        InlineKeyboardButton("ℹ️ Услуги и цены", callback_data="onboard_prices"),
    ]])
    if MASTER_PHOTO_ID:
        await update.message.reply_photo(
            photo=MASTER_PHOTO_ID, caption=intro,
            parse_mode="Markdown", reply_markup=kb,
        )
    else:
        await update.message.reply_text(intro, parse_mode="Markdown", reply_markup=kb)

    return ONBOARDING


async def onboard_done(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Любое нажатие на онбординге — запомнить и перейти дальше."""
    q = update.callback_query; await q.answer()
    user = update.effective_user
    db_mark_onboarding(user.id, user.first_name or "")

    if q.data == "onboard_prices":
        return await _show_prices(q)
    # onboard_book или что угодно → к записи
    return await _start_booking(q, ctx, user)


async def _show_prices(q):
    text = "💆 *Услуги и цены:*\n\n"
    for name, dur, price in PROCEDURES:
        text += f"• *{name}*\n  ⏱ {dur} мин · 💰 {price:,} ₽\n\n"
    await q.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("📅 Записаться", callback_data="book"),
            InlineKeyboardButton("◀️ Меню",       callback_data="back_main"),
        ]]),
    )
    return CLIENT_MENU


async def _start_booking(q_or_update, ctx, user):
    if db_count_active(user.id) >= MAX_ACTIVE_APTS:
        if hasattr(q_or_update, 'answer'):
            await q_or_update.answer(
                f"У вас уже {MAX_ACTIVE_APTS} активные записи. "
                "Отмените одну, чтобы записаться снова.",
                show_alert=True,
            )
        return CLIENT_MENU
    text = step_header(1, 3, "выберите процедуру") + "Что вас беспокоит?"
    if hasattr(q_or_update, 'edit_message_text'):
        await q_or_update.edit_message_text(
            text, parse_mode="Markdown", reply_markup=proc_keyboard(back_cb="back_main")
        )
    else:
        await q_or_update.reply_text(
            text, parse_mode="Markdown", reply_markup=proc_keyboard(back_cb="back_main")
        )
    return CHOOSE_PROC


async def prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    return await _show_prices(q)


async def contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    lines = [f"Напишите напрямую: {MASTER_USERNAME}"]
    if MASTER_PHONE:
        lines.append(f"Телефон: {MASTER_PHONE}")
    lines.append("\n_Приём пн–сб, 10:00–20:00_")
    await q.edit_message_text(
        f"📞 *Контакты мастера {MASTER_NAME}:*\n\n" + "\n".join(lines),
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Назад", callback_data="back_main")
        ]]),
    )
    return CLIENT_MENU


async def book_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    return await _start_booking(q, ctx, update.effective_user)


async def proc_chosen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    idx = int(q.data.split("_")[1])
    name, dur, price = PROCEDURES[idx]
    ctx.user_data.update(proc_name=name, proc_dur=dur, proc_price=price)

    days      = next_n_days(14)
    slots_map = {d: db_get_free_slots(d) for d in days}
    text = (
        step_header(2, 3, "выберите дату") +
        f"✅ *{name}* · {dur} мин · {price:,} ₽\n\n"
        "Выберите удобный день:"
    )
    await q.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=dates_keyboard(slots_map, back_cb="book"),
    )
    return CHOOSE_DATE


async def date_chosen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    date = q.data[len("date_"):]
    ctx.user_data["date"] = date
    all_slots = db_get_all_slots(date)
    free_count = sum(1 for _, s in all_slots if s == "free")
    if not free_count:
        await q.answer("На этот день нет свободного времени", show_alert=True)
        return CHOOSE_DATE
    text = (
        step_header(2, 3, "выберите время") +
        f"📅 *{fmt_date(date)}*\n\n"
        "✅ свободно  ·  ✖ занято"
    )
    await q.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=times_keyboard_grid(all_slots, back_cb="book"),
    )
    return CHOOSE_TIME


async def time_chosen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["time"] = q.data[len("time_"):]
    if ctx.user_data.get("client_name"):
        return await _show_confirm(update, ctx)
    text = (
        step_header(3, 3, "ваши данные") +
        f"📅 {fmt_date_short(ctx.user_data['date'])} в *{ctx.user_data['time']}*\n\n"
        "Как вас зовут? (Имя и фамилия)"
    )
    await q.edit_message_text(text, parse_mode="Markdown")
    return ENTER_NAME


async def enter_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["client_name"] = update.message.text.strip()
    await update.message.reply_text(
        "📱 Введите номер телефона для связи:"
    )
    return ENTER_PHONE


async def enter_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not validate_phone(phone):
        await update.message.reply_text(
            "❌ Похоже, номер введён неверно.\n"
            "Попробуйте ещё раз, например: *+7 900 123-45-67*",
            parse_mode="Markdown",
        )
        return ENTER_PHONE
    ctx.user_data["client_phone"] = phone
    return await _show_confirm(update, ctx)


async def _show_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.user_data
    end_time = _calc_end_time(d["time"], d["proc_dur"])
    text = (
        "✅ *Подтвердите запись*\n\n"
        f"┌ 💆 *{d['proc_name']}*\n"
        f"├ 📅 {fmt_date(d['date'])}\n"
        f"├ 🕐 {d['time']} — {end_time}\n"
        f"├ ⏱ {d['proc_dur']} мин\n"
        f"├ 💰 {d['proc_price']:,} ₽\n"
        f"├ 👤 {d['client_name']}\n"
        f"└ 📱 {d['client_phone']}"
    )
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Записаться", callback_data="confirm_yes")],
        [InlineKeyboardButton("✏️ Изменить",  callback_data="book"),
         InlineKeyboardButton("✖ Отмена",    callback_data="back_main")],
    ])
    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    else:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    return CONFIRM_BOOK


def _calc_end_time(start: str, duration: int) -> str:
    h, m = map(int, start.split(":"))
    total = h * 60 + m + duration
    return f"{total // 60:02d}:{total % 60:02d}"


async def confirm_booking(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    d   = ctx.user_data
    uid = update.effective_user.id
    apt_id = db_add_appointment(
        d["date"], d["time"],
        d["client_name"], d["client_phone"], uid,
        d["proc_name"], d["proc_dur"], d["proc_price"],
    )
    schedule_reminders(ctx.application, apt_id, uid, d["date"], d["time"], d["proc_dur"])

    try:
        await ctx.bot.send_message(
            ADMIN_ID,
            f"🔔 *Новая запись!*\n\n"
            f"👤 {d['client_name']} · {d['client_phone']}\n"
            f"💆 {d['proc_name']}\n"
            f"📅 {fmt_date(d['date'])} в {d['time']}\n"
            f"💰 {d['proc_price']:,} ₽\n"
            f"🆔 `{apt_id}`",
            parse_mode="Markdown",
        )
    except Exception:
        pass

    await q.edit_message_text(
        f"🎉 *Вы записаны!*\n\n"
        f"💆 {d['proc_name']}\n"
        f"📅 {fmt_date(d['date'])} в {d['time']} — {_calc_end_time(d['time'], d['proc_dur'])}\n"
        f"💰 {d['proc_price']:,} ₽\n\n"
        "🔔 Напомню накануне и за час до приёма.\n"
        "_До встречи!_ 👋",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Главное меню", callback_data="back_main")
        ]]),
    )
    return CLIENT_MENU


# ── Мои записи ────────────────────────────────────────────────────────────────

async def my_apts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    apts = db_get_user_appointments(update.effective_user.id)
    if not apts:
        await q.edit_message_text(
            "📋 *Мои записи*\n\nУ вас пока нет предстоящих записей.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 Записаться", callback_data="book")],
                [InlineKeyboardButton("◀️ Назад",      callback_data="back_main")],
            ]),
        )
        return CLIENT_MENU

    now   = datetime.now()
    today = now.strftime("%Y-%m-%d")

    text = "📋 *Ваши записи:*\n\n"
    rows = []
    for a in apts:
        apt_dt    = datetime.strptime(f"{a['date']} {a['time']}", "%Y-%m-%d %H:%M")
        is_today  = a["date"] == today
        is_soon   = 0 < (apt_dt - now).total_seconds() < 3600 * 3
        badge = ""
        if is_soon:
            badge = " 🔴 скоро"
        elif is_today:
            badge = " 🟡 сегодня"

        end_time = _calc_end_time(a["time"], a["duration"])
        text += (
            f"📅 *{fmt_date_short(a['date'])}*{badge}\n"
            f"🕐 {a['time']} — {end_time} · {a['procedure']}\n"
            f"💰 {a['price']:,} ₽\n\n"
        )
        rows.append([
            InlineKeyboardButton(
                f"🔄 {fmt_date_short(a['date'])} {a['time']}",
                callback_data=f"reschedule_{a['id']}"
            ),
            InlineKeyboardButton("❌", callback_data=f"cancel_{a['id']}"),
        ])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back_main")])
    await q.edit_message_text(
        text, parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)
    )
    return CLIENT_MENU


async def cancel_apt_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    apt_id = q.data[len("cancel_"):]
    apt = db_cancel_appointment(apt_id)
    if apt:
        cancel_reminders(ctx.application, apt_id)
        try:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"⚠️ *Клиент отменил запись*\n\n"
                f"👤 {apt['name']} · {apt['phone']}\n"
                f"💆 {apt['procedure']}\n"
                f"📅 {fmt_date(apt['date'])} в {apt['time']}",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        await q.edit_message_text(
            "✅ Запись отменена.\n\n_Если хотите записаться снова — нажмите «Записаться»._",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 Записаться", callback_data="book")],
                [InlineKeyboardButton("◀️ Меню",       callback_data="back_main")],
            ]),
        )
    else:
        await q.answer("Не удалось отменить запись.", show_alert=True)
    return CLIENT_MENU


# ── Перенос записи ────────────────────────────────────────────────────────────

async def reschedule_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    apt_id = q.data[len("reschedule_"):]
    ctx.user_data["reschedule_id"] = apt_id
    days      = next_n_days(14)
    slots_map = {d: db_get_free_slots(d) for d in days}
    await q.edit_message_text(
        "🔄 *Перенос записи*\n\nВыберите новую дату:",
        parse_mode="Markdown",
        reply_markup=dates_keyboard(slots_map, back_cb="my_apts"),
    )
    return RESCHEDULE_DATE


async def reschedule_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    date = q.data[len("date_"):]
    ctx.user_data["reschedule_date"] = date
    all_slots = db_get_all_slots(date)
    if not any(s == "free" for _, s in all_slots):
        await q.answer("На этот день нет свободного времени", show_alert=True)
        return RESCHEDULE_DATE
    await q.edit_message_text(
        f"🔄 *Перенос записи*\n\n📅 *{fmt_date(date)}*\n\nВыберите время:",
        parse_mode="Markdown",
        reply_markup=times_keyboard_grid(all_slots, back_cb="my_apts"),
    )
    return RESCHEDULE_TIME


async def reschedule_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    new_time = q.data[len("time_"):]
    apt_id   = ctx.user_data["reschedule_id"]
    new_date = ctx.user_data["reschedule_date"]
    uid      = update.effective_user.id

    cancel_reminders(ctx.application, apt_id)

    if db_reschedule(apt_id, new_date, new_time):
        uid_str    = apt_id.split("-")[-1]
        new_apt_id = f"{new_date}-{new_time.replace(':','-')}-{uid_str}"
        apt = db_get_appointment(new_apt_id)
        dur = apt["duration"] if apt else SLOT_MIN
        schedule_reminders(ctx.application, new_apt_id, uid, new_date, new_time, dur)
        try:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"🔄 *Перенос записи*\n\n"
                f"👤 {apt['name']} · {apt['phone']}\n"
                f"💆 {apt['procedure']}\n"
                f"📅 {fmt_date(new_date)} в {new_time}",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        await q.edit_message_text(
            f"✅ *Запись перенесена!*\n\n"
            f"📅 {fmt_date(new_date)} в {new_time}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Мои записи", callback_data="my_apts")
            ]]),
        )
    else:
        await q.answer("Не удалось перенести запись.", show_alert=True)
    return CLIENT_MENU


# ── Навигация ─────────────────────────────────────────────────────────────────

async def back_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    is_admin = update.effective_user.id == ADMIN_ID
    await q.edit_message_text(
        "Выберите действие:", reply_markup=main_menu_kb(is_admin)
    )
    return CLIENT_MENU


async def noop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()


# ══════════════════════════════════════════════════════════════════════════════
# ADMIN хендлеры
# ══════════════════════════════════════════════════════════════════════════════

@admin_only
async def admin_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    await q.edit_message_text(
        "🔐 *Панель мастера:*", parse_mode="Markdown",
        reply_markup=admin_menu_kb(),
    )
    return ADMIN_MENU


@admin_only
async def adm_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    days = next_n_days(7)
    rows = []
    for d in days:
        apts  = db_get_day_schedule(d)
        free  = db_get_free_slots(d)
        busy  = len(apts)
        d_obj = datetime.strptime(d, "%Y-%m-%d")
        label = f"{SHORT_DAYS[d_obj.weekday()]} {d_obj.day} {MONTHS[d_obj.month]}"
        status = f"  ·  {busy} записей, {len(free)} свободно" if busy else f"  ·  {len(free)} свободно"
        rows.append([InlineKeyboardButton(f"📅 {label}{status}", callback_data=f"adm_day_{d}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
    await q.edit_message_text("Выберите день:", reply_markup=InlineKeyboardMarkup(rows))
    return ADMIN_DATE


@admin_only
async def adm_day_chosen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    date = q.data[len("adm_day_"):]
    apts = db_get_day_schedule(date)
    free = db_get_free_slots(date)

    text = f"📅 *{fmt_date(date)}*\n\n"
    if apts:
        for a in apts:
            end = _calc_end_time(a["time"], a["duration"])
            text += (
                f"🕐 *{a['time']}–{end}*  {a['name']}\n"
                f"   📱 {a['phone']}\n"
                f"   💆 {a['procedure']} · {a['price']:,} ₽\n\n"
            )
    else:
        text += "_Записей нет._\n\n"
    text += f"🟢 Свободно: {len(free)} слотов"

    await q.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Назад", callback_data="adm_schedule")
        ]]),
    )
    return ADMIN_MENU


@admin_only
async def adm_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    days = next_n_days(7)
    text = "📆 *Расписание на 7 дней:*\n\n"
    total_apts = 0
    for d in days:
        apts = db_get_day_schedule(d)
        free = db_get_free_slots(d)
        d_obj = datetime.strptime(d, "%Y-%m-%d")
        label = f"{DAYS[d_obj.weekday()]}, {d_obj.day} {MONTHS[d_obj.month]}"
        text += f"*{label}*\n"
        for a in apts:
            text += f"  🕐 {a['time']} — {a['name']} · {a['procedure']}\n"
            total_apts += 1
        if not apts:
            text += "  _(пусто)_\n"
        text += f"  🟢 Свободно: {len(free)}\n\n"
    text += f"_Итого за неделю: {total_apts} записей_"

    await q.edit_message_text(
        text, parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Назад", callback_data="admin")
        ]]),
    )
    return ADMIN_MENU


# ── Статистика ────────────────────────────────────────────────────────────────

@admin_only
async def adm_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    period = ctx.user_data.get("stats_period", 30)
    s = db_stats(period)

    top = f"{s['top_proc']['procedure']} ({s['top_proc']['cnt']} раз)" \
        if s["top_proc"] else "нет данных"

    text = (
        f"📊 *Статистика за {period} дней*\n\n"
        f"📅 Записей выполнено: *{s['total']}*\n"
        f"👥 Уникальных клиентов: *{s['unique_clients']}*\n"
        f"💰 Выручка: *{s['revenue']:,} ₽*\n"
        f"❌ Отмен: {s['cancelled']} ({s['cancel_rate']}%)\n"
        f"🏆 Топ процедура: {top}\n"
        f"🗓 Предстоящих записей: *{s['upcoming']}*"
    )
    kb = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("7 дней",  callback_data="stats_7"),
            InlineKeyboardButton("30 дней", callback_data="stats_30"),
            InlineKeyboardButton("90 дней", callback_data="stats_90"),
        ],
        [InlineKeyboardButton("◀️ Панель мастера", callback_data="admin")],
    ])
    await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
    return ADMIN_MENU


@admin_only
async def adm_stats_period(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    period = int(q.data.split("_")[1])
    ctx.user_data["stats_period"] = period
    return await adm_stats(update, ctx)


# ── Отмена записи (админ) ─────────────────────────────────────────────────────

@admin_only
async def adm_cancel_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    days, all_apts = next_n_days(14), []
    for d in days:
        all_apts.extend(db_get_day_schedule(d))
    if not all_apts:
        await q.answer("Нет активных записей", show_alert=True)
        return ADMIN_MENU
    rows = [[InlineKeyboardButton(
        f"{fmt_date_short(a['date'])} {a['time']} · {a['name']}",
        callback_data=f"adm_cancel_{a['id']}"
    )] for a in all_apts]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
    await q.edit_message_text(
        "Выберите запись для отмены:", reply_markup=InlineKeyboardMarkup(rows)
    )
    return ADMIN_MENU


@admin_only
async def adm_do_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    apt_id = q.data[len("adm_cancel_"):]
    apt = db_cancel_appointment(apt_id)
    if apt:
        cancel_reminders(ctx.application, apt_id)
        try:
            await ctx.bot.send_message(
                apt["user_id"],
                f"⚠️ *Ваша запись отменена мастером*\n\n"
                f"📅 {fmt_date(apt['date'])} в {apt['time']}\n"
                f"💆 {apt['procedure']}\n\n"
                "_Свяжитесь с мастером для переноса._",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        await q.edit_message_text(
            f"✅ Запись отменена.\n👤 Клиент {apt['name']} уведомлён.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("◀️ Назад", callback_data="admin")
            ]]),
        )
    else:
        await q.answer("Не удалось отменить", show_alert=True)
    return ADMIN_MENU


# ── Добавить запись вручную ───────────────────────────────────────────────────

@admin_only
async def adm_add_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["adm_adding"] = True
    days      = next_n_days(14)
    slots_map = {d: db_get_free_slots(d) for d in days}
    await q.edit_message_text(
        "➕ *Новая запись*\n\nШаг 1: выберите дату",
        parse_mode="Markdown",
        reply_markup=dates_keyboard(slots_map, back_cb="admin"),
    )
    return ADMIN_ADD_DATE


@admin_only
async def adm_add_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    date = q.data[len("date_"):]
    ctx.user_data["date"] = date
    all_slots = db_get_all_slots(date)
    await q.edit_message_text(
        f"➕ *Новая запись*\n\n📅 {fmt_date(date)}\nШаг 2: выберите время",
        parse_mode="Markdown",
        reply_markup=times_keyboard_grid(all_slots, back_cb="adm_add"),
    )
    return ADMIN_ADD_TIME


@admin_only
async def adm_add_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    ctx.user_data["time"] = q.data[len("time_"):]
    await q.edit_message_text(
        "➕ *Новая запись*\n\nШаг 3: выберите процедуру",
        parse_mode="Markdown",
        reply_markup=proc_keyboard(back_cb="adm_add"),
    )
    return ADMIN_ADD_PROC


@admin_only
async def adm_add_proc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    idx = int(q.data.split("_")[1])
    name, dur, price = PROCEDURES[idx]
    ctx.user_data.update(proc_name=name, proc_dur=dur, proc_price=price)
    await q.edit_message_text(
        f"➕ *Новая запись*\n\n"
        f"📅 {fmt_date(ctx.user_data['date'])} в {ctx.user_data['time']}\n"
        f"💆 {name}\n\n"
        "Введите имя клиента:",
        parse_mode="Markdown",
    )
    return ADMIN_ADD_NAME


async def adm_add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["client_name"] = update.message.text.strip()
    await update.message.reply_text("📱 Введите телефон клиента:")
    return ADMIN_ADD_PHONE


async def adm_add_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not validate_phone(phone):
        await update.message.reply_text(
            "❌ Неверный формат. Введите телефон ещё раз:"
        )
        return ADMIN_ADD_PHONE
    ctx.user_data["client_phone"] = phone
    d = ctx.user_data
    apt_id = db_add_appointment(
        d["date"], d["time"],
        d["client_name"], d["client_phone"],
        ADMIN_ID,
        d["proc_name"], d["proc_dur"], d["proc_price"],
    )
    ctx.user_data.pop("adm_adding", None)
    await update.message.reply_text(
        f"✅ *Запись создана:*\n\n"
        f"👤 {d['client_name']} · {d['client_phone']}\n"
        f"💆 {d['proc_name']}\n"
        f"📅 {fmt_date(d['date'])} в {d['time']}\n"
        f"💰 {d['proc_price']:,} ₽",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Панель мастера", callback_data="admin")
        ]]),
    )
    return ADMIN_MENU


# ── Блокировка слотов ─────────────────────────────────────────────────────────

@admin_only
async def adm_block_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    days      = next_n_days(14)
    slots_map = {d: db_get_free_slots(d) for d in days}
    await q.edit_message_text(
        "🚫 *Блокировка слота*\n\nВыберите дату:",
        parse_mode="Markdown",
        reply_markup=dates_keyboard(slots_map, back_cb="admin"),
    )
    return ADMIN_BLOCK_DATE


@admin_only
async def adm_block_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    date = q.data[len("date_"):]
    ctx.user_data["block_date"] = date
    slots = db_get_free_slots(date)
    if not slots:
        await q.answer("На этот день нет свободных слотов", show_alert=True)
        return ADMIN_BLOCK_DATE
    await q.edit_message_text(
        f"🚫 *{fmt_date(date)}*\n\nВыберите время для блокировки:",
        parse_mode="Markdown",
        reply_markup=times_keyboard_grid(
            [(s, "free") for s in slots], back_cb="adm_block"
        ),
    )
    return ADMIN_BLOCK_TIME


@admin_only
async def adm_block_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    time = q.data[len("time_"):]
    date = ctx.user_data["block_date"]
    db_block_slot(date, time)
    await q.edit_message_text(
        f"🚫 {fmt_date(date)} в {time} — заблокировано.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Панель мастера", callback_data="admin")
        ]]),
    )
    return ADMIN_MENU


# ── Разблокировка слотов ──────────────────────────────────────────────────────

@admin_only
async def adm_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    blocked = db_get_blocked_all()
    if not blocked:
        await q.answer("Нет заблокированных слотов", show_alert=True)
        return ADMIN_MENU
    rows = [[InlineKeyboardButton(
        f"🔓 {fmt_date_short(b['date'])} {b['time']}",
        callback_data=f"adm_unblock_{b['date']}_{b['time']}"
    )] for b in blocked]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
    await q.edit_message_text(
        "🔓 Выберите слот для разблокировки:",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return ADMIN_MENU


@admin_only
async def adm_do_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query; await q.answer()
    parts = q.data.split("_")
    date, time = parts[2], parts[3]
    db_unblock_slot(date, time)
    await q.edit_message_text(
        f"✅ {fmt_date(date)} в {time} — разблокировано.",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ Панель мастера", callback_data="admin")
        ]]),
    )
    return ADMIN_MENU


# ══════════════════════════════════════════════════════════════════════════════
# Запуск
# ══════════════════════════════════════════════════════════════════════════════

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        conversation_timeout=600,
        states={
            ONBOARDING: [
                CallbackQueryHandler(onboard_done, pattern="^onboard_"),
            ],
            CLIENT_MENU: [
                CallbackQueryHandler(book_start,       pattern="^book$"),
                CallbackQueryHandler(my_apts,          pattern="^my_apts$"),
                CallbackQueryHandler(prices,           pattern="^prices$"),
                CallbackQueryHandler(contact,          pattern="^contact$"),
                CallbackQueryHandler(admin_menu,       pattern="^admin$"),
                CallbackQueryHandler(cancel_apt_cb,    pattern="^cancel_"),
                CallbackQueryHandler(reschedule_start, pattern="^reschedule_"),
                CallbackQueryHandler(back_main,        pattern="^back_main$"),
                CallbackQueryHandler(noop,             pattern="^noop$"),
            ],
            CHOOSE_PROC: [
                CallbackQueryHandler(proc_chosen, pattern="^proc_"),
                CallbackQueryHandler(back_main,   pattern="^back_main$"),
            ],
            CHOOSE_DATE: [
                CallbackQueryHandler(date_chosen, pattern="^date_"),
                CallbackQueryHandler(book_start,  pattern="^book$"),
                CallbackQueryHandler(back_main,   pattern="^back_main$"),
                CallbackQueryHandler(noop,        pattern="^noop$"),
            ],
            CHOOSE_TIME: [
                CallbackQueryHandler(time_chosen, pattern="^time_"),
                CallbackQueryHandler(book_start,  pattern="^book$"),
                CallbackQueryHandler(back_main,   pattern="^back_main$"),
                CallbackQueryHandler(noop,        pattern="^noop$"),
            ],
            CONFIRM_BOOK: [
                CallbackQueryHandler(confirm_booking, pattern="^confirm_yes$"),
                CallbackQueryHandler(book_start,      pattern="^book$"),
                CallbackQueryHandler(back_main,       pattern="^back_main$"),
            ],
            ENTER_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_name)],
            ENTER_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, enter_phone)],
            RESCHEDULE_DATE: [
                CallbackQueryHandler(reschedule_date, pattern="^date_"),
                CallbackQueryHandler(my_apts,         pattern="^my_apts$"),
                CallbackQueryHandler(noop,            pattern="^noop$"),
            ],
            RESCHEDULE_TIME: [
                CallbackQueryHandler(reschedule_time, pattern="^time_"),
                CallbackQueryHandler(my_apts,         pattern="^my_apts$"),
                CallbackQueryHandler(noop,            pattern="^noop$"),
            ],
            ADMIN_MENU: [
                CallbackQueryHandler(adm_schedule,     pattern="^adm_schedule$"),
                CallbackQueryHandler(adm_week,         pattern="^adm_week$"),
                CallbackQueryHandler(adm_add_start,    pattern="^adm_add$"),
                CallbackQueryHandler(adm_block_start,  pattern="^adm_block$"),
                CallbackQueryHandler(adm_unblock,      pattern="^adm_unblock$"),
                CallbackQueryHandler(adm_do_unblock,   pattern="^adm_unblock_"),
                CallbackQueryHandler(adm_cancel_menu,  pattern="^adm_cancel$"),
                CallbackQueryHandler(adm_do_cancel,    pattern="^adm_cancel_"),
                CallbackQueryHandler(adm_day_chosen,   pattern="^adm_day_"),
                CallbackQueryHandler(adm_stats,        pattern="^adm_stats$"),
                CallbackQueryHandler(adm_stats_period, pattern="^stats_"),
                CallbackQueryHandler(admin_menu,       pattern="^admin$"),
                CallbackQueryHandler(back_main,        pattern="^back_main$"),
            ],
            ADMIN_DATE: [
                CallbackQueryHandler(adm_day_chosen, pattern="^adm_day_"),
                CallbackQueryHandler(admin_menu,     pattern="^admin$"),
            ],
            ADMIN_ADD_DATE: [
                CallbackQueryHandler(adm_add_date, pattern="^date_"),
                CallbackQueryHandler(admin_menu,   pattern="^admin$"),
                CallbackQueryHandler(noop,         pattern="^noop$"),
            ],
            ADMIN_ADD_TIME: [
                CallbackQueryHandler(adm_add_time,  pattern="^time_"),
                CallbackQueryHandler(adm_add_start, pattern="^adm_add$"),
                CallbackQueryHandler(noop,          pattern="^noop$"),
            ],
            ADMIN_ADD_PROC: [
                CallbackQueryHandler(adm_add_proc,  pattern="^proc_"),
                CallbackQueryHandler(adm_add_start, pattern="^adm_add$"),
            ],
            ADMIN_ADD_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_name)],
            ADMIN_ADD_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_phone)],
            ADMIN_BLOCK_DATE: [
                CallbackQueryHandler(adm_block_date,  pattern="^date_"),
                CallbackQueryHandler(adm_block_start, pattern="^adm_block$"),
                CallbackQueryHandler(admin_menu,      pattern="^admin$"),
                CallbackQueryHandler(noop,            pattern="^noop$"),
            ],
            ADMIN_BLOCK_TIME: [
                CallbackQueryHandler(adm_block_time,  pattern="^time_"),
                CallbackQueryHandler(adm_block_start, pattern="^adm_block$"),
                CallbackQueryHandler(noop,            pattern="^noop$"),
            ],
        },
        fallbacks=[CommandHandler("start", start)],
        per_message=False,
    )

    app.add_handler(conv)
    log.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
