#!/usr/bin/env python3
"""
PODOLOG BOT — бот записи к подологу
Екатерина Шлейфер

Установка зависимостей:
    pip install "python-telegram-bot[job-queue]"

Запуск:
    python podolog_bot.py

Данные мастера и токен вписаны прямо в код.
Токен можно вынести в переменную окружения BOT_TOKEN.
"""

import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta
from functools import wraps

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, ConversationHandler, MessageHandler, filters,
)

# ═══════════════════════════════════════════════════════════════════════════════
# НАСТРОЙКИ — вписывай сюда свои данные
# ═══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN    = os.getenv("BOT_TOKEN", "СЮДА_ТОКЕН")
ADMIN_ID     = int(os.getenv("ADMIN_ID", "223326752"))

MASTER_NAME  = "Екатерина Шлейфер"
MASTER_PHONE = "8 (920) 649 26-16"
MASTER_TG    = "@master"
MASTER_SINCE = "2018"
MASTER_RATING= "4.9"
MASTER_ABOUT = (
    "Профессиональный подолог с 2018 года.\n"
    "Более 200 довольных клиентов.\n"
    "Работаю аккуратно, без боли и стресса. 🌸"
)

# Рабочие дни: 0=пн, 1=вт, ..., 5=сб, 6=вс
WORK_DAYS  = [0, 1, 2, 3, 4, 5]   # пн–сб
WORK_START = 10                     # с 10:00
WORK_END   = 20                     # до 20:00
SLOT_MIN   = 60                     # минут на один слот

DAYS_AHEAD     = 14   # сколько дней вперёд показывать для записи
MAX_ACTIVE_APTS = 2   # лимит активных записей на одного клиента

DB_FILE = "podolog.db"

# Услуги: (название, длительность мин, цена руб, описание)
PROCEDURES = [
    ("Педикюр аппаратный",     60, 2800,
     "Профессиональная обработка стопы и пальцев, удаление натоптышей и огрубевшей кожи."),
    ("Гигиенический педикюр",  45, 2200,
     "Базовый уход: придание формы ногтям, лёгкая обработка кожи стопы."),
    ("Лечение вросшего ногтя", 90, 3500,
     "Безболезненное устранение проблемы. Облегчение уже за 1 сеанс."),
    ("Протезирование ногтя",   90, 4500,
     "Восстановление эстетического вида ногтевой пластины гелем."),
    ("Ортониксия (скобы)",     60, 5200,
     "Коррекция формы ногтя специальными системами — титановая нить, скоба."),
    ("Лечение грибка (микоз)", 60, 3200,
     "Комплексная зачистка + подбор терапевтического ухода."),
]

# ═══════════════════════════════════════════════════════════════════════════════
# СОСТОЯНИЯ ДИАЛОГА
# ═══════════════════════════════════════════════════════════════════════════════

(
    S_MAIN,
    S_PROC, S_DATE, S_TIME,
    S_NAME, S_PHONE, S_CONFIRM,
    S_MY_APTS,
    S_ADM_MAIN,
    S_ADM_SCHED_DATE,
    S_ADM_BLOCK_DATE, S_ADM_BLOCK_TIME,
    S_ADM_UNBLOCK,
    S_ADM_CANCEL,
    S_ADM_ADD_DATE, S_ADM_ADD_TIME, S_ADM_ADD_PROC,
    S_ADM_ADD_NAME, S_ADM_ADD_PHONE,
) = range(19)

# ═══════════════════════════════════════════════════════════════════════════════
# ЛОГИРОВАНИЕ
# ═══════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("podolog.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════════════
# БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════════════════════

_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_FILE, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _lock, _conn() as db:
        db.executescript("""
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
            CREATE TABLE IF NOT EXISTS dayoff (
                date TEXT PRIMARY KEY,
                reason TEXT DEFAULT 'Выходной'
            );
        """)


# ── Слоты ────────────────────────────────────────────────────────────────────

def _booked(date: str) -> set:
    with _lock, _conn() as db:
        rows = db.execute(
            "SELECT time FROM appointments WHERE date=? AND status!='cancelled'", (date,)
        ).fetchall()
    return {r["time"] for r in rows}


def _blocked(date: str) -> set:
    with _lock, _conn() as db:
        rows = db.execute("SELECT time FROM blocked WHERE date=?", (date,)).fetchall()
    return {r["time"] for r in rows}


def free_slots(date: str) -> list[str]:
    if is_dayoff(date):
        return []
    taken = _booked(date) | _blocked(date)
    result, t = [], WORK_START * 60
    while t + SLOT_MIN <= WORK_END * 60:
        hh, mm = divmod(t, 60)
        s = f"{hh:02d}:{mm:02d}"
        if s not in taken:
            result.append(s)
        t += SLOT_MIN
    return result


def all_slots_status(date: str) -> list[tuple[str, str]]:
    """Возвращает [(время, статус)] где статус: free/booked/blocked."""
    booked  = _booked(date)
    blocked = _blocked(date)
    result, t = [], WORK_START * 60
    while t + SLOT_MIN <= WORK_END * 60:
        hh, mm = divmod(t, 60)
        s = f"{hh:02d}:{mm:02d}"
        st = "booked" if s in booked else ("blocked" if s in blocked else "free")
        result.append((s, st))
        t += SLOT_MIN
    return result


# ── Выходные дни ─────────────────────────────────────────────────────────────

def is_dayoff(date: str) -> bool:
    with _lock, _conn() as db:
        return db.execute(
            "SELECT 1 FROM dayoff WHERE date=?", (date,)
        ).fetchone() is not None


def toggle_dayoff(date: str) -> str:
    """'added' или 'removed'."""
    with _lock, _conn() as db:
        if db.execute("SELECT 1 FROM dayoff WHERE date=?", (date,)).fetchone():
            db.execute("DELETE FROM dayoff WHERE date=?", (date,))
            return "removed"
        else:
            db.execute("INSERT INTO dayoff(date) VALUES(?)", (date,))
            return "added"


# ── Блокировка слотов ─────────────────────────────────────────────────────────

def block_slot(date: str, time: str):
    with _lock, _conn() as db:
        db.execute("INSERT OR IGNORE INTO blocked(date,time) VALUES(?,?)", (date, time))


def unblock_slot(date: str, time: str):
    with _lock, _conn() as db:
        db.execute("DELETE FROM blocked WHERE date=? AND time=?", (date, time))


def all_blocked() -> list[dict]:
    today = _today()
    with _lock, _conn() as db:
        rows = db.execute(
            "SELECT date, time FROM blocked WHERE date>=? ORDER BY date,time", (today,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Записи ───────────────────────────────────────────────────────────────────

def add_apt(date, time, name, phone, uid, proc, dur, price) -> str:
    apt_id = f"{date}_{time.replace(':','')}_{uid}"
    with _lock, _conn() as db:
        # Проверка что слот ещё свободен
        if db.execute(
            "SELECT 1 FROM appointments WHERE date=? AND time=? AND status='confirmed'",
            (date, time)
        ).fetchone():
            raise ValueError("Этот слот уже занят — пожалуйста выберите другое время.")
        db.execute(
            "INSERT OR REPLACE INTO appointments "
            "(id,date,time,name,phone,user_id,procedure,duration,price,status,created) "
            "VALUES(?,?,?,?,?,?,?,?,?,'confirmed',?)",
            (apt_id, date, time, name, phone, uid, proc, dur, price,
             datetime.now().strftime("%Y-%m-%d %H:%M"))
        )
    return apt_id


def cancel_apt(apt_id: str) -> dict | None:
    with _lock, _conn() as db:
        row = db.execute(
            "SELECT * FROM appointments WHERE id=? AND status='confirmed'", (apt_id,)
        ).fetchone()
        if row:
            db.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (apt_id,))
            return dict(row)
    return None


def get_apt(apt_id: str) -> dict | None:
    with _lock, _conn() as db:
        row = db.execute(
            "SELECT * FROM appointments WHERE id=?", (apt_id,)
        ).fetchone()
    return dict(row) if row else None


def user_apts(uid: int) -> list[dict]:
    with _lock, _conn() as db:
        rows = db.execute(
            "SELECT * FROM appointments WHERE user_id=? AND status='confirmed' "
            "AND date>=? ORDER BY date,time",
            (uid, _today())
        ).fetchall()
    return [dict(r) for r in rows]


def count_active(uid: int) -> int:
    with _lock, _conn() as db:
        return db.execute(
            "SELECT COUNT(*) FROM appointments WHERE user_id=? "
            "AND status='confirmed' AND date>=?",
            (uid, _today())
        ).fetchone()[0]


def day_apts(date: str) -> list[dict]:
    with _lock, _conn() as db:
        rows = db.execute(
            "SELECT * FROM appointments WHERE date=? AND status='confirmed' ORDER BY time",
            (date,)
        ).fetchall()
    return [dict(r) for r in rows]


def prev_contact(uid: int) -> tuple[str, str] | None:
    """Возвращает (имя, телефон) из последней записи пользователя."""
    with _lock, _conn() as db:
        row = db.execute(
            "SELECT name, phone FROM appointments WHERE user_id=? ORDER BY created DESC LIMIT 1",
            (uid,)
        ).fetchone()
    return (row["name"], row["phone"]) if row else None


# ═══════════════════════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════════════════════

MONTHS    = ["","января","февраля","марта","апреля","мая","июня",
             "июля","августа","сентября","октября","ноября","декабря"]
WEEKDAYS  = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def fmt_date(d: str) -> str:
    dt = datetime.strptime(d, "%Y-%m-%d")
    return f"{WEEKDAYS[dt.weekday()]}, {dt.day} {MONTHS[dt.month]}"


def available_dates() -> list[str]:
    today = datetime.now()
    # Если рабочий день уже заканчивается — начинаем с завтра
    if today.hour >= WORK_END:
        today += timedelta(days=1)
    dates, d = [], today
    while len(dates) < DAYS_AHEAD * 2:   # берём с запасом
        if d.weekday() in WORK_DAYS and not is_dayoff(d.strftime("%Y-%m-%d")):
            ds = d.strftime("%Y-%m-%d")
            if free_slots(ds):            # только если есть свободные слоты
                dates.append(ds)
                if len(dates) == DAYS_AHEAD:
                    break
        d += timedelta(days=1)
    return dates


def validate_phone(p: str) -> bool:
    return 7 <= len(re.sub(r"\D", "", p)) <= 15


def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *a, **kw):
        uid = update.effective_user.id
        if uid != ADMIN_ID:
            if update.callback_query:
                await update.callback_query.answer("⛔ Нет доступа.", show_alert=True)
            else:
                await update.message.reply_text("⛔ Нет доступа.")
            return S_MAIN
        return await func(update, ctx, *a, **kw)
    return wrapper


# ═══════════════════════════════════════════════════════════════════════════════
# КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════════════════════

def kb_main(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("📅 Записаться",     callback_data="book")],
        [InlineKeyboardButton("📋 Мои записи",     callback_data="my_apts")],
        [InlineKeyboardButton("💆 Услуги и цены",  callback_data="prices")],
        [InlineKeyboardButton("👩‍⚕️ О мастере",   callback_data="about")],
        [InlineKeyboardButton("📞 Контакты",        callback_data="contact")],
    ]
    if uid == ADMIN_ID:
        rows.append([InlineKeyboardButton("🔐 Панель мастера", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


def kb_back(cb: str = "back_main") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=cb)]])


def kb_procs() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(
        f"{name}  •  {price:,} ₽  •  {dur} мин".replace(",", " "),
        callback_data=f"proc_{i}"
    )] for i, (name, dur, price, _) in enumerate(PROCEDURES)]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def kb_dates(dates: list[str]) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for d in dates:
        dt = datetime.strptime(d, "%Y-%m-%d")
        label = f"{dt.day} {MONTHS[dt.month][:3]} {WEEKDAYS[dt.weekday()]}"
        row.append(InlineKeyboardButton(label, callback_data=f"date_{d}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="book")])
    return InlineKeyboardMarkup(rows)


def kb_times(slots: list[str]) -> InlineKeyboardMarkup:
    rows = []
    row = []
    for s in slots:
        row.append(InlineKeyboardButton(f"🕐 {s}", callback_data=f"time_{s}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back_date")])
    return InlineKeyboardMarkup(rows)


def kb_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_yes"),
         InlineKeyboardButton("❌ Отмена",      callback_data="back_main")],
    ])


def kb_my_apts(apts: list[dict]) -> InlineKeyboardMarkup:
    rows = []
    for a in apts:
        rows.append([
            InlineKeyboardButton(
                f"❌  {fmt_date(a['date'])} {a['time']}",
                callback_data=f"cancel_{a['id']}"
            )
        ])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)


def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Расписание на день",   callback_data="adm_sched")],
        [InlineKeyboardButton("📆 Расписание на неделю", callback_data="adm_week")],
        [InlineKeyboardButton("➕ Добавить запись",      callback_data="adm_add")],
        [InlineKeyboardButton("🚫 Заблокировать слот",   callback_data="adm_block")],
        [InlineKeyboardButton("🔓 Разблокировать слот",  callback_data="adm_unblock")],
        [InlineKeyboardButton("📵 Выходной день",        callback_data="adm_dayoff")],
        [InlineKeyboardButton("❌ Отменить запись",       callback_data="adm_cancel")],
        [InlineKeyboardButton("◀️ Главное меню",         callback_data="back_main")],
    ])


def kb_adm_dates(days: int = 14, action_prefix: str = "adm_date") -> InlineKeyboardMarkup:
    """Датапикер для администратора — показывает все дни включая занятые."""
    today = datetime.now()
    rows, row = [], []
    for i in range(days):
        d = today + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        off = "🚫" if is_dayoff(ds) else ""
        label = f"{d.day} {MONTHS[d.month][:3]} {WEEKDAYS[d.weekday()]} {off}".strip()
        row.append(InlineKeyboardButton(label, callback_data=f"{action_prefix}_{ds}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
    return InlineKeyboardMarkup(rows)


# ═══════════════════════════════════════════════════════════════════════════════
# НАПОМИНАНИЯ
# ═══════════════════════════════════════════════════════════════════════════════

def schedule_reminders(app, apt_id: str, uid: int, date: str, time: str):
    if not app.job_queue:
        return
    try:
        apt_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        for hours, label in [(24, "завтра"), (2, "через 2 часа")]:
            fire = apt_dt - timedelta(hours=hours)
            if fire > datetime.now():
                app.job_queue.run_once(
                    _send_reminder,
                    when=fire,
                    data={"apt_id": apt_id, "uid": uid, "label": label},
                    name=f"rem_{apt_id}_{hours}",
                )
    except Exception as e:
        log.warning("Не удалось запланировать напоминание: %s", e)


def cancel_reminders(app, apt_id: str):
    if not app.job_queue:
        return
    for h in (24, 2):
        for job in app.job_queue.get_jobs_by_name(f"rem_{apt_id}_{h}"):
            job.schedule_removal()


async def _send_reminder(ctx: ContextTypes.DEFAULT_TYPE):
    data  = ctx.job.data
    apt   = get_apt(data["apt_id"])
    if not apt or apt["status"] != "confirmed":
        return
    try:
        await ctx.bot.send_message(
            data["uid"],
            f"🔔 *Напоминание о записи*\n\n"
            f"📅 {fmt_date(apt['date'])} в {apt['time']} ({data['label']})\n"
            f"💆 {apt['procedure']}\n\n"
            f"Адрес и вопросы: {MASTER_PHONE}",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.warning("Не удалось отправить напоминание: %s", e)


# ═══════════════════════════════════════════════════════════════════════════════
# КЛИЕНТСКИЕ ОБРАБОТЧИКИ
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    user = update.effective_user
    await update.message.reply_text(
        f"👋 Привет, *{user.first_name}*!\n\n"
        f"Я помогу записаться к подологу *{MASTER_NAME}*.\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=kb_main(user.id),
    )
    return S_MAIN


async def cb_back_main(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data.clear()
    await q.edit_message_text(
        "Главное меню — выберите действие:",
        reply_markup=kb_main(update.effective_user.id),
    )
    return S_MAIN


async def cb_prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    lines = ["💆 *Услуги и цены:*\n"]
    for name, dur, price, desc in PROCEDURES:
        lines.append(
            f"*{name}*\n"
            f"⏱ {dur} мин  •  💰 {price:,} ₽\n".replace(",", " ") +
            f"_{desc}_\n"
        )
    await q.edit_message_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=kb_back("back_main"),
    )
    return S_MAIN


async def cb_about(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        f"👩‍⚕️ *{MASTER_NAME}*\n\n"
        f"{MASTER_ABOUT}\n\n"
        f"⭐ Рейтинг: {MASTER_RATING}\n"
        f"📅 Принимаю с {MASTER_SINCE} года",
        parse_mode="Markdown",
        reply_markup=kb_back("back_main"),
    )
    return S_MAIN


async def cb_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text(
        f"📞 *Контакты мастера:*\n\n"
        f"Телефон: {MASTER_PHONE}\n"
        f"Telegram: {MASTER_TG}\n\n"
        f"Режим работы: пн–сб, {WORK_START}:00–{WORK_END}:00",
        parse_mode="Markdown",
        reply_markup=kb_back("back_main"),
    )
    return S_MAIN


# ── Запись ────────────────────────────────────────────────────────────────────

async def cb_book(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if count_active(update.effective_user.id) >= MAX_ACTIVE_APTS:
        await q.answer(
            f"У вас уже {MAX_ACTIVE_APTS} активных записи.\n"
            "Отмените одну, чтобы записаться снова.",
            show_alert=True,
        )
        return S_MAIN
    await q.edit_message_text(
        "💆 *Выберите процедуру:*",
        parse_mode="Markdown",
        reply_markup=kb_procs(),
    )
    return S_PROC


async def cb_proc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    idx = int(q.data.split("_")[1])
    name, dur, price, desc = PROCEDURES[idx]
    ctx.user_data.update(proc=name, dur=dur, price=price)

    dates = available_dates()
    if not dates:
        await q.edit_message_text(
            "😔 К сожалению, свободных дат пока нет.\n"
            "Загляните позже или свяжитесь с мастером напрямую.",
            reply_markup=kb_back("back_main"),
        )
        return S_MAIN

    await q.edit_message_text(
        f"✅ *{name}*\n_{desc}_\n\n📅 Выберите удобную дату:",
        parse_mode="Markdown",
        reply_markup=kb_dates(dates),
    )
    return S_DATE


async def cb_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    date = q.data[5:]   # "date_2026-06-10" → "2026-06-10"
    ctx.user_data["date"] = date
    slots = free_slots(date)
    if not slots:
        await q.answer("На этот день нет свободного времени.", show_alert=True)
        return S_DATE
    await q.edit_message_text(
        f"📅 *{fmt_date(date)}*\n\n🕐 Выберите время:",
        parse_mode="Markdown",
        reply_markup=kb_times(slots),
    )
    return S_TIME


async def cb_back_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Кнопка Назад со страницы выбора времени — возвращает к выбору даты."""
    q = update.callback_query
    await q.answer()
    dates = available_dates()
    await q.edit_message_text(
        f"💆 *{ctx.user_data.get('proc', 'Процедура')}*\n\n📅 Выберите дату:",
        parse_mode="Markdown",
        reply_markup=kb_dates(dates),
    )
    return S_DATE


async def cb_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["time"] = q.data[5:]  # "time_10:00" → "10:00"

    # Если клиент уже записывался — предзаполняем имя и телефон
    prev = prev_contact(update.effective_user.id)
    if prev:
        ctx.user_data["name"]  = prev[0]
        ctx.user_data["phone"] = prev[1]
        return await _show_confirm(q, ctx)

    await q.edit_message_text("📝 Введите ваше *имя и фамилию:*", parse_mode="Markdown")
    return S_NAME


async def msg_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("Пожалуйста, введите настоящее имя.")
        return S_NAME
    ctx.user_data["name"] = name
    await update.message.reply_text("📱 Введите ваш *номер телефона:*", parse_mode="Markdown")
    return S_PHONE


async def msg_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not validate_phone(phone):
        await update.message.reply_text(
            "❌ Неверный формат. Введите номер, например: *+7 900 123-45-67*",
            parse_mode="Markdown",
        )
        return S_PHONE
    ctx.user_data["phone"] = phone
    return await _show_confirm(update, ctx)


async def _show_confirm(src, ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.user_data
    text = (
        "📋 *Подтвердите запись:*\n\n"
        f"💆 {d['proc']}\n"
        f"📅 {fmt_date(d['date'])} в {d['time']}\n"
        f"⏱ {d['dur']} мин  •  💰 {d['price']:,} ₽\n\n".replace(",", " ") +
        f"👤 {d['name']}\n"
        f"📱 {d['phone']}"
    )
    if hasattr(src, "edit_message_text"):
        await src.edit_message_text(text, parse_mode="Markdown", reply_markup=kb_confirm())
    else:
        await src.message.reply_text(text, parse_mode="Markdown", reply_markup=kb_confirm())
    return S_CONFIRM


async def cb_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d   = ctx.user_data
    uid = update.effective_user.id
    try:
        apt_id = add_apt(
            d["date"], d["time"], d["name"], d["phone"], uid,
            d["proc"], d["dur"], d["price"],
        )
    except ValueError as e:
        await q.edit_message_text(str(e), reply_markup=kb_main(uid))
        return S_MAIN

    schedule_reminders(ctx.application, apt_id, uid, d["date"], d["time"])
    ctx.user_data.clear()

    # Уведомление мастеру
    try:
        await ctx.bot.send_message(
            ADMIN_ID,
            f"🔔 *Новая запись!*\n\n"
            f"👤 {d['name']}  📱 {d['phone']}\n"
            f"💆 {d['proc']}\n"
            f"📅 {fmt_date(d['date'])} в {d['time']}\n"
            f"💰 {d['price']:,} ₽".replace(",", " "),
            parse_mode="Markdown",
        )
    except Exception:
        pass

    await q.edit_message_text(
        f"🎉 *Вы записаны!*\n\n"
        f"💆 {d['proc']}\n"
        f"📅 {fmt_date(d['date'])} в {d['time']}\n\n"
        "Я напомню вам за сутки и за 2 часа до визита. 🔔\n\n"
        f"Вопросы: {MASTER_PHONE}",
        parse_mode="Markdown",
        reply_markup=kb_back("back_main"),
    )
    return S_MAIN


# ── Мои записи ───────────────────────────────────────────────────────────────

async def cb_my_apts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    apts = user_apts(update.effective_user.id)
    if not apts:
        await q.edit_message_text(
            "У вас нет предстоящих записей.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("📅 Записаться", callback_data="book")],
                [InlineKeyboardButton("◀️ Назад",      callback_data="back_main")],
            ]),
        )
        return S_MAIN

    lines = ["📋 *Ваши предстоящие записи:*\n"]
    for a in apts:
        lines.append(f"• {fmt_date(a['date'])} в {a['time']} — {a['procedure']}")

    await q.edit_message_text(
        "\n".join(lines) + "\n\nЧтобы отменить — нажмите кнопку ниже:",
        parse_mode="Markdown",
        reply_markup=kb_my_apts(apts),
    )
    return S_MAIN


async def cb_cancel_apt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    apt_id = q.data[7:]   # "cancel_..." → "..."
    apt = cancel_apt(apt_id)
    if apt:
        cancel_reminders(ctx.application, apt_id)
        try:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"⚠️ *Клиент отменил запись*\n\n"
                f"👤 {apt['name']}  📱 {apt['phone']}\n"
                f"💆 {apt['procedure']}\n"
                f"📅 {fmt_date(apt['date'])} в {apt['time']}",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        await q.edit_message_text(
            f"✅ Запись на {fmt_date(apt['date'])} в {apt['time']} отменена.",
            reply_markup=kb_back("back_main"),
        )
    else:
        await q.answer("Запись не найдена или уже отменена.", show_alert=True)
    return S_MAIN


# ═══════════════════════════════════════════════════════════════════════════════
# АДМИНИСТРАТИВНЫЕ ОБРАБОТЧИКИ
# ═══════════════════════════════════════════════════════════════════════════════

@admin_only
async def cb_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("🔐 *Панель мастера:*", parse_mode="Markdown",
                               reply_markup=kb_admin())
    return S_ADM_MAIN


@admin_only
async def adm_sched(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("📅 Выберите день:", reply_markup=kb_adm_dates(14, "adm_sched_d"))
    return S_ADM_SCHED_DATE


@admin_only
async def adm_sched_day(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    date = q.data[len("adm_sched_d_"):]
    apts = day_apts(date)
    free = free_slots(date)
    off  = is_dayoff(date)

    lines = [f"📅 *{fmt_date(date)}*\n"]
    if off:
        lines.append("🚫 Выходной день\n")
    if apts:
        total = 0
        for a in apts:
            lines.append(f"🕐 {a['time']}  {a['name']}  {a['phone']}\n"
                         f"   {a['procedure']} — {a['price']:,} ₽".replace(",", " "))
            total += a["price"]
        lines.append(f"\n💰 Итого: {total:,} ₽  •  записей: {len(apts)}".replace(",", " "))
    else:
        lines.append("_Записей нет._")
    lines.append(f"\n🟢 Свободных слотов: {len(free)}")

    await q.edit_message_text("\n".join(lines), parse_mode="Markdown",
                               reply_markup=kb_back("adm_sched"))
    return S_ADM_MAIN


@admin_only
async def adm_week(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    today = datetime.now()
    lines = ["📆 *Расписание на 7 дней:*\n"]
    for i in range(7):
        d  = (today + timedelta(days=i)).strftime("%Y-%m-%d")
        ap = day_apts(d)
        fr = free_slots(d)
        lines.append(f"*{fmt_date(d)}*")
        for a in ap:
            lines.append(f"  🕐 {a['time']} {a['name']} — {a['procedure']}")
        if not ap:
            lines.append("  _(пусто)_")
        lines.append(f"  🟢 Свободно: {len(fr)}\n")
    await q.edit_message_text("\n".join(lines), parse_mode="Markdown",
                               reply_markup=kb_back("admin"))
    return S_ADM_MAIN


# ── Добавить запись вручную ───────────────────────────────────────────────────

@admin_only
async def adm_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["adm_mode"] = "add"
    await q.edit_message_text("📅 Выберите дату:", reply_markup=kb_adm_dates(14, "adm_add_d"))
    return S_ADM_ADD_DATE


@admin_only
async def adm_add_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    date = q.data[len("adm_add_d_"):]
    ctx.user_data["date"] = date
    slots = free_slots(date)
    if not slots:
        await q.answer("На этот день нет свободных слотов.", show_alert=True)
        return S_ADM_ADD_DATE
    rows = []
    row  = []
    for s in slots:
        row.append(InlineKeyboardButton(s, callback_data=f"adm_add_t_{s}"))
        if len(row) == 4:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_add")])
    await q.edit_message_text(f"🕐 Выберите время на {fmt_date(date)}:",
                               reply_markup=InlineKeyboardMarkup(rows))
    return S_ADM_ADD_TIME


@admin_only
async def adm_add_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["time"] = q.data[len("adm_add_t_"):]
    await q.edit_message_text("💆 Выберите процедуру:", reply_markup=kb_procs())
    return S_ADM_ADD_PROC


@admin_only
async def adm_add_proc(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    idx = int(q.data.split("_")[1])
    name, dur, price, _ = PROCEDURES[idx]
    ctx.user_data.update(proc=name, dur=dur, price=price)
    await q.edit_message_text("👤 Введите имя клиента:")
    return S_ADM_ADD_NAME


async def adm_add_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("📱 Введите телефон клиента:")
    return S_ADM_ADD_PHONE


async def adm_add_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    if not validate_phone(phone):
        await update.message.reply_text("❌ Неверный формат телефона. Введите ещё раз:")
        return S_ADM_ADD_PHONE
    ctx.user_data["phone"] = phone
    d = ctx.user_data
    try:
        add_apt(d["date"], d["time"], d["name"], d["phone"],
                ADMIN_ID, d["proc"], d["dur"], d["price"])
    except ValueError as e:
        await update.message.reply_text(str(e), reply_markup=kb_admin())
        return S_ADM_MAIN
    await update.message.reply_text(
        f"✅ *Запись создана:*\n\n"
        f"👤 {d['name']}  📱 {d['phone']}\n"
        f"💆 {d['proc']}\n"
        f"📅 {fmt_date(d['date'])} в {d['time']}",
        parse_mode="Markdown",
        reply_markup=kb_admin(),
    )
    ctx.user_data.clear()
    return S_ADM_MAIN


# ── Блокировка слота ──────────────────────────────────────────────────────────

@admin_only
async def adm_block(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("🚫 Выберите дату для блокировки слота:",
                               reply_markup=kb_adm_dates(14, "adm_blk_d"))
    return S_ADM_BLOCK_DATE


@admin_only
async def adm_blk_date(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    date = q.data[len("adm_blk_d_"):]
    ctx.user_data["blk_date"] = date
    statuses = all_slots_status(date)
    rows = []
    for t, s in statuses:
        emoji = "🔴" if s == "booked" else ("🟡" if s == "blocked" else "🟢")
        rows.append([InlineKeyboardButton(
            f"{emoji} {t}",
            callback_data=f"adm_blk_t_{t}" if s == "free" else "adm_noop"
        )])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_block")])
    await q.edit_message_text(
        f"*{fmt_date(date)}*\n🟢 свободен  🟡 заблокирован  🔴 занят\n\nНажмите на свободный слот:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return S_ADM_BLOCK_TIME


@admin_only
async def adm_blk_time(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    time = q.data[len("adm_blk_t_"):]
    date = ctx.user_data.get("blk_date", "")
    block_slot(date, time)
    await q.edit_message_text(
        f"🚫 Слот {fmt_date(date)} в {time} заблокирован.",
        reply_markup=kb_admin(),
    )
    return S_ADM_MAIN


# ── Разблокировка слота ───────────────────────────────────────────────────────

@admin_only
async def adm_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    blocked = all_blocked()
    if not blocked:
        await q.answer("Нет заблокированных слотов.", show_alert=True)
        return S_ADM_MAIN
    rows = [[InlineKeyboardButton(
        f"🔓 {fmt_date(b['date'])} в {b['time']}",
        callback_data=f"adm_unblk_{b['date']}|{b['time']}"
    )] for b in blocked]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
    await q.edit_message_text("🔓 Выберите слот для разблокировки:",
                               reply_markup=InlineKeyboardMarkup(rows))
    return S_ADM_UNBLOCK


@admin_only
async def adm_do_unblock(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    # callback_data = "adm_unblk_2026-06-10|10:00"
    payload      = q.data[len("adm_unblk_"):]
    date, time   = payload.split("|", 1)
    unblock_slot(date, time)
    await q.edit_message_text(f"✅ Слот {fmt_date(date)} в {time} разблокирован.",
                               reply_markup=kb_admin())
    return S_ADM_MAIN


# ── Выходной день ─────────────────────────────────────────────────────────────

@admin_only
async def adm_dayoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("📵 Выберите день:",
                               reply_markup=kb_adm_dates(30, "adm_off_d"))
    return S_ADM_MAIN


@admin_only
async def adm_do_dayoff(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    date   = q.data[len("adm_off_d_"):]
    result = toggle_dayoff(date)
    msg    = (f"🚫 {fmt_date(date)} — добавлен как выходной."
              if result == "added"
              else f"✅ {fmt_date(date)} — убран из выходных.")
    await q.edit_message_text(msg, reply_markup=kb_admin())
    return S_ADM_MAIN


# ── Отмена записи (admin) ─────────────────────────────────────────────────────

@admin_only
async def adm_cancel_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    today = datetime.now()
    apts  = []
    for i in range(30):
        d = (today + timedelta(days=i)).strftime("%Y-%m-%d")
        apts.extend(day_apts(d))
    if not apts:
        await q.answer("Нет активных записей.", show_alert=True)
        return S_ADM_MAIN
    rows = [[InlineKeyboardButton(
        f"{a['date']} {a['time']} — {a['name']}",
        callback_data=f"adm_cncl_{a['id']}"
    )] for a in apts]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
    await q.edit_message_text("❌ Выберите запись для отмены:",
                               reply_markup=InlineKeyboardMarkup(rows))
    return S_ADM_CANCEL


@admin_only
async def adm_do_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    apt_id = q.data[len("adm_cncl_"):]
    apt    = cancel_apt(apt_id)
    if apt:
        cancel_reminders(ctx.application, apt_id)
        try:
            await ctx.bot.send_message(
                apt["user_id"],
                f"⚠️ *Ваша запись отменена мастером*\n\n"
                f"📅 {fmt_date(apt['date'])} в {apt['time']}\n"
                f"💆 {apt['procedure']}\n\n"
                "Свяжитесь с мастером для переноса:\n"
                f"{MASTER_PHONE}",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        await q.edit_message_text(
            f"✅ Запись {apt['name']} на {fmt_date(apt['date'])} отменена.\nКлиент уведомлён.",
            reply_markup=kb_admin(),
        )
    else:
        await q.answer("Запись не найдена.", show_alert=True)
    return S_ADM_MAIN


async def adm_noop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer("Этот слот нельзя заблокировать.", show_alert=True)


# ═══════════════════════════════════════════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    init_db()

    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        allow_reentry=True,
        per_message=False,
        states={
            S_MAIN: [
                CallbackQueryHandler(cb_book,       pattern="^book$"),
                CallbackQueryHandler(cb_my_apts,    pattern="^my_apts$"),
                CallbackQueryHandler(cb_prices,     pattern="^prices$"),
                CallbackQueryHandler(cb_about,      pattern="^about$"),
                CallbackQueryHandler(cb_contact,    pattern="^contact$"),
                CallbackQueryHandler(cb_admin,      pattern="^admin$"),
                CallbackQueryHandler(cb_cancel_apt, pattern="^cancel_"),
                CallbackQueryHandler(cb_back_main,  pattern="^back_main$"),
                # Админ: выходные и расписание доступны прямо из главного меню
                CallbackQueryHandler(adm_do_dayoff, pattern="^adm_off_d_"),
                CallbackQueryHandler(adm_sched_day, pattern="^adm_sched_d_"),
            ],
            S_PROC: [
                CallbackQueryHandler(cb_proc,      pattern="^proc_"),
                CallbackQueryHandler(cb_back_main, pattern="^back_main$"),
            ],
            S_DATE: [
                CallbackQueryHandler(cb_date,      pattern="^date_"),
                CallbackQueryHandler(cb_book,      pattern="^book$"),
                CallbackQueryHandler(cb_back_main, pattern="^back_main$"),
            ],
            S_TIME: [
                CallbackQueryHandler(cb_time,       pattern="^time_"),
                CallbackQueryHandler(cb_back_date,  pattern="^back_date$"),
                CallbackQueryHandler(cb_back_main,  pattern="^back_main$"),
            ],
            S_NAME:    [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_name)],
            S_PHONE:   [MessageHandler(filters.TEXT & ~filters.COMMAND, msg_phone)],
            S_CONFIRM: [
                CallbackQueryHandler(cb_confirm,   pattern="^confirm_yes$"),
                CallbackQueryHandler(cb_back_main, pattern="^back_main$"),
            ],
            S_ADM_MAIN: [
                CallbackQueryHandler(cb_admin,        pattern="^admin$"),
                CallbackQueryHandler(adm_sched,       pattern="^adm_sched$"),
                CallbackQueryHandler(adm_sched_day,   pattern="^adm_sched_d_"),
                CallbackQueryHandler(adm_week,        pattern="^adm_week$"),
                CallbackQueryHandler(adm_add,         pattern="^adm_add$"),
                CallbackQueryHandler(adm_block,       pattern="^adm_block$"),
                CallbackQueryHandler(adm_unblock,     pattern="^adm_unblock$"),
                CallbackQueryHandler(adm_do_unblock,  pattern="^adm_unblk_"),
                CallbackQueryHandler(adm_dayoff,      pattern="^adm_dayoff$"),
                CallbackQueryHandler(adm_do_dayoff,   pattern="^adm_off_d_"),
                CallbackQueryHandler(adm_cancel_menu, pattern="^adm_cancel$"),
                CallbackQueryHandler(adm_do_cancel,   pattern="^adm_cncl_"),
                CallbackQueryHandler(adm_noop,        pattern="^adm_noop$"),
                CallbackQueryHandler(cb_back_main,    pattern="^back_main$"),
            ],
            S_ADM_SCHED_DATE: [
                CallbackQueryHandler(adm_sched_day, pattern="^adm_sched_d_"),
                CallbackQueryHandler(adm_sched,     pattern="^adm_sched$"),
                CallbackQueryHandler(cb_admin,      pattern="^admin$"),
            ],
            S_ADM_BLOCK_DATE: [
                CallbackQueryHandler(adm_blk_date,  pattern="^adm_blk_d_"),
                CallbackQueryHandler(adm_block,     pattern="^adm_block$"),
                CallbackQueryHandler(cb_admin,      pattern="^admin$"),
            ],
            S_ADM_BLOCK_TIME: [
                CallbackQueryHandler(adm_blk_time,  pattern="^adm_blk_t_"),
                CallbackQueryHandler(adm_block,     pattern="^adm_block$"),
                CallbackQueryHandler(adm_noop,      pattern="^adm_noop$"),
            ],
            S_ADM_UNBLOCK: [
                CallbackQueryHandler(adm_do_unblock, pattern="^adm_unblk_"),
                CallbackQueryHandler(cb_admin,       pattern="^admin$"),
            ],
            S_ADM_CANCEL: [
                CallbackQueryHandler(adm_do_cancel,   pattern="^adm_cncl_"),
                CallbackQueryHandler(cb_admin,        pattern="^admin$"),
            ],
            S_ADM_ADD_DATE: [
                CallbackQueryHandler(adm_add_date, pattern="^adm_add_d_"),
                CallbackQueryHandler(cb_admin,     pattern="^admin$"),
            ],
            S_ADM_ADD_TIME: [
                CallbackQueryHandler(adm_add_time, pattern="^adm_add_t_"),
                CallbackQueryHandler(adm_add,      pattern="^adm_add$"),
            ],
            S_ADM_ADD_PROC: [
                CallbackQueryHandler(adm_add_proc, pattern="^proc_"),
            ],
            S_ADM_ADD_NAME:  [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_name)],
            S_ADM_ADD_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, adm_add_phone)],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
    )

    app.add_handler(conv)

    if app.job_queue:
        log.info("Напоминания активны ✅")
    else:
        log.warning(
            "JobQueue недоступен — напоминания выключены. "
            "Установите: pip install \"python-telegram-bot[job-queue]\""
        )

    log.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
