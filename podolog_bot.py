#!/usr/bin/env python3
"""
PODOLOG BOT — финальная версия.
Состояния хранятся в user_data. Один роутер обрабатывает все кнопки.
Установка: pip install "python-telegram-bot[job-queue]"
"""

import logging
import os
import re
import sqlite3
import threading
from datetime import datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, PicklePersistence, filters,
)

# ═══════════════════════════════════════════════════════════════════════════════
# НАСТРОЙКИ
# ═══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN    = os.getenv("BOT_TOKEN", "Сюда токен")
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

WORK_DAYS  = [0, 1, 2, 3, 4, 5]   # пн–сб
WORK_START = 10
WORK_END   = 20
SLOT_MIN   = 60
DAYS_AHEAD = 14
MAX_ACTIVE = 2
DB_FILE    = "podolog.db"

PROCEDURES = [
    ("Педикюр аппаратный",     60, 2800,
     "Профессиональная обработка стопы и пальцев, удаление натоптышей."),
    ("Гигиенический педикюр",  45, 2200,
     "Базовый уход: придание формы ногтям, лёгкая обработка кожи стопы."),
    ("Лечение вросшего ногтя", 90, 3500,
     "Безболезненное устранение проблемы. Облегчение уже за 1 сеанс."),
    ("Протезирование ногтя",   90, 4500,
     "Восстановление эстетического вида ногтевой пластины гелем."),
    ("Ортониксия (скобы)",     60, 5200,
     "Коррекция формы ногтя — титановая нить, скоба."),
    ("Лечение грибка (микоз)", 60, 3200,
     "Комплексная зачистка + подбор терапевтического ухода."),
]

# Состояния (хранятся в user_data["state"])
ST_MAIN      = "main"
ST_PROC      = "proc"
ST_DATE      = "date"
ST_TIME      = "time"
ST_NAME      = "name"
ST_PHONE     = "phone"
ST_CONFIRM   = "confirm"
ST_ADM       = "adm"
ST_ADM_ADD_NAME  = "adm_add_name"
ST_ADM_ADD_PHONE = "adm_add_phone"

MONTHS   = ["","января","февраля","марта","апреля","мая","июня",
            "июля","августа","сентября","октября","ноября","декабря"]
WEEKDAYS = ["Пн","Вт","Ср","Чт","Пт","Сб","Вс"]

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("podolog.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger(__name__)
_lock = threading.Lock()

# ═══════════════════════════════════════════════════════════════════════════════
# БД
# ═══════════════════════════════════════════════════════════════════════════════

def db():
    c = sqlite3.connect(DB_FILE, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with _lock, db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS appointments (
                id TEXT PRIMARY KEY, date TEXT, time TEXT,
                name TEXT, phone TEXT, user_id INTEGER,
                procedure TEXT, duration INTEGER, price INTEGER,
                status TEXT DEFAULT 'confirmed', created TEXT
            );
            CREATE TABLE IF NOT EXISTS blocked (
                date TEXT, time TEXT, PRIMARY KEY(date,time)
            );
            CREATE TABLE IF NOT EXISTS dayoff (
                date TEXT PRIMARY KEY, reason TEXT DEFAULT 'Выходной'
            );
        """)

def today():
    return datetime.now().strftime("%Y-%m-%d")

def fmt_date(d):
    dt = datetime.strptime(d, "%Y-%m-%d")
    return f"{WEEKDAYS[dt.weekday()]}, {dt.day} {MONTHS[dt.month]}"

def is_dayoff(date):
    with _lock, db() as c:
        return c.execute("SELECT 1 FROM dayoff WHERE date=?", (date,)).fetchone() is not None

def toggle_dayoff(date):
    with _lock, db() as c:
        if c.execute("SELECT 1 FROM dayoff WHERE date=?", (date,)).fetchone():
            c.execute("DELETE FROM dayoff WHERE date=?", (date,))
            return "removed"
        c.execute("INSERT INTO dayoff(date) VALUES(?)", (date,))
        return "added"

def booked(date):
    with _lock, db() as c:
        return {r["time"] for r in c.execute(
            "SELECT time FROM appointments WHERE date=? AND status!='cancelled'", (date,)
        )}

def blocked(date):
    with _lock, db() as c:
        return {r["time"] for r in c.execute(
            "SELECT time FROM blocked WHERE date=?", (date,)
        )}

def free_slots(date):
    if is_dayoff(date):
        return []
    taken = booked(date) | blocked(date)
    slots, t = [], WORK_START * 60
    while t + SLOT_MIN <= WORK_END * 60:
        hh, mm = divmod(t, 60)
        s = f"{hh:02d}:{mm:02d}"
        if s not in taken:
            slots.append(s)
        t += SLOT_MIN
    return slots

def all_slots_status(date):
    b, bl = booked(date), blocked(date)
    result, t = [], WORK_START * 60
    while t + SLOT_MIN <= WORK_END * 60:
        hh, mm = divmod(t, 60)
        s = f"{hh:02d}:{mm:02d}"
        st = "booked" if s in b else ("blocked" if s in bl else "free")
        result.append((s, st))
        t += SLOT_MIN
    return result

def available_dates():
    now = datetime.now()
    if now.hour >= WORK_END:
        now += timedelta(days=1)
    dates, d = [], now
    while len(dates) < DAYS_AHEAD * 3:
        if d.weekday() in WORK_DAYS:
            ds = d.strftime("%Y-%m-%d")
            if not is_dayoff(ds) and free_slots(ds):
                dates.append(ds)
                if len(dates) == DAYS_AHEAD:
                    break
        d += timedelta(days=1)
    return dates

def add_apt(date, time, name, phone, uid, proc, dur, price):
    apt_id = f"{date}_{time.replace(':','')}_{uid}"
    with _lock, db() as c:
        if c.execute(
            "SELECT 1 FROM appointments WHERE date=? AND time=? AND status='confirmed'",
            (date, time)
        ).fetchone():
            raise ValueError("Этот слот уже занят — выберите другое время.")
        c.execute(
            "INSERT OR REPLACE INTO appointments "
            "(id,date,time,name,phone,user_id,procedure,duration,price,status,created) "
            "VALUES(?,?,?,?,?,?,?,?,?,'confirmed',?)",
            (apt_id, date, time, name, phone, uid, proc, dur, price,
             datetime.now().strftime("%Y-%m-%d %H:%M"))
        )
    return apt_id

def cancel_apt(apt_id):
    with _lock, db() as c:
        row = c.execute(
            "SELECT * FROM appointments WHERE id=? AND status='confirmed'", (apt_id,)
        ).fetchone()
        if row:
            c.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (apt_id,))
            return dict(row)
    return None

def get_apt(apt_id):
    with _lock, db() as c:
        row = c.execute("SELECT * FROM appointments WHERE id=?", (apt_id,)).fetchone()
    return dict(row) if row else None

def user_apts(uid):
    with _lock, db() as c:
        rows = c.execute(
            "SELECT * FROM appointments WHERE user_id=? AND status='confirmed' "
            "AND date>=? ORDER BY date,time", (uid, today())
        ).fetchall()
    return [dict(r) for r in rows]

def count_active(uid):
    with _lock, db() as c:
        return c.execute(
            "SELECT COUNT(*) FROM appointments WHERE user_id=? "
            "AND status='confirmed' AND date>=?", (uid, today())
        ).fetchone()[0]

def day_apts(date):
    with _lock, db() as c:
        rows = c.execute(
            "SELECT * FROM appointments WHERE date=? AND status='confirmed' ORDER BY time",
            (date,)
        ).fetchall()
    return [dict(r) for r in rows]

def prev_contact(uid):
    with _lock, db() as c:
        row = c.execute(
            "SELECT name, phone FROM appointments WHERE user_id=? "
            "ORDER BY created DESC LIMIT 1", (uid,)
        ).fetchone()
    return (row["name"], row["phone"]) if row else None

def all_blocked_slots():
    with _lock, db() as c:
        rows = c.execute(
            "SELECT date, time FROM blocked WHERE date>=? ORDER BY date,time", (today(),)
        ).fetchall()
    return [dict(r) for r in rows]

def block_slot(date, time):
    with _lock, db() as c:
        c.execute("INSERT OR IGNORE INTO blocked(date,time) VALUES(?,?)", (date, time))

def unblock_slot(date, time):
    with _lock, db() as c:
        c.execute("DELETE FROM blocked WHERE date=? AND time=?", (date, time))

# ═══════════════════════════════════════════════════════════════════════════════
# НАПОМИНАНИЯ
# ═══════════════════════════════════════════════════════════════════════════════

def schedule_reminders(app, apt_id, uid, date, time):
    if not app.job_queue:
        return
    try:
        apt_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        for hours, label in [(24, "завтра"), (2, "через 2 часа")]:
            fire = apt_dt - timedelta(hours=hours)
            if fire > datetime.now():
                app.job_queue.run_once(
                    _remind, when=fire,
                    data={"apt_id": apt_id, "uid": uid, "label": label},
                    name=f"rem_{apt_id}_{hours}",
                )
    except Exception as e:
        log.warning("Ошибка планирования напоминания: %s", e)

def cancel_reminders(app, apt_id):
    if not app.job_queue:
        return
    for h in (24, 2):
        for j in app.job_queue.get_jobs_by_name(f"rem_{apt_id}_{h}"):
            j.schedule_removal()

async def _remind(ctx: ContextTypes.DEFAULT_TYPE):
    d = ctx.job.data
    apt = get_apt(d["apt_id"])
    if not apt or apt["status"] != "confirmed":
        return
    try:
        await ctx.bot.send_message(
            d["uid"],
            f"🔔 *Напоминание!*\n\n"
            f"Запись *{d['label']}*:\n"
            f"📅 {fmt_date(apt['date'])} в {apt['time']}\n"
            f"💆 {apt['procedure']}\n\n"
            f"Вопросы: {MASTER_PHONE}",
            parse_mode="Markdown",
        )
    except Exception as e:
        log.warning("Ошибка отправки напоминания: %s", e)

# ═══════════════════════════════════════════════════════════════════════════════
# КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════════════════════

def kb_main(uid):
    rows = [
        [InlineKeyboardButton("📅 Записаться",    callback_data="book")],
        [InlineKeyboardButton("📋 Мои записи",    callback_data="my_apts")],
        [InlineKeyboardButton("💆 Услуги и цены", callback_data="prices")],
        [InlineKeyboardButton("👩‍⚕️ О мастере",  callback_data="about")],
        [InlineKeyboardButton("📞 Контакты",       callback_data="contact")],
    ]
    if uid == ADMIN_ID:
        rows.append([InlineKeyboardButton("🔐 Панель мастера", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

def kb_back(cb="main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=cb)]])

def kb_procs():
    rows = [[InlineKeyboardButton(
        f"{name}  •  {price:,} ₽  •  {dur} мин".replace(",", " "),
        callback_data=f"proc_{i}"
    )] for i, (name, dur, price, _) in enumerate(PROCEDURES)]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="main")])
    return InlineKeyboardMarkup(rows)

def kb_dates(dates):
    rows, row = [], []
    for d in dates:
        dt = datetime.strptime(d, "%Y-%m-%d")
        label = f"{dt.day} {MONTHS[dt.month][:3]} {WEEKDAYS[dt.weekday()]}"
        row.append(InlineKeyboardButton(label, callback_data=f"date_{d}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="book")])
    return InlineKeyboardMarkup(rows)

def kb_times(slots):
    rows, row = [], []
    for s in slots:
        row.append(InlineKeyboardButton(f"🕐 {s}", callback_data=f"time_{s}"))
        if len(row) == 4:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back_date")])
    return InlineKeyboardMarkup(rows)

def kb_confirm():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_yes"),
        InlineKeyboardButton("❌ Отмена",      callback_data="main"),
    ]])

def kb_my_apts(apts):
    rows = [[InlineKeyboardButton(
        f"❌  {fmt_date(a['date'])} {a['time']} — {a['procedure'][:20]}",
        callback_data=f"cancel_{a['id']}"
    )] for a in apts]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="main")])
    return InlineKeyboardMarkup(rows)

def kb_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Расписание на день",   callback_data="adm_sched")],
        [InlineKeyboardButton("📆 Расписание на неделю", callback_data="adm_week")],
        [InlineKeyboardButton("➕ Добавить запись",      callback_data="adm_add")],
        [InlineKeyboardButton("🚫 Заблокировать слот",   callback_data="adm_block")],
        [InlineKeyboardButton("🔓 Разблокировать слот",  callback_data="adm_unblock")],
        [InlineKeyboardButton("📵 Выходной день",        callback_data="adm_dayoff")],
        [InlineKeyboardButton("❌ Отменить запись",       callback_data="adm_cancel")],
        [InlineKeyboardButton("◀️ Главное меню",         callback_data="main")],
    ])

def kb_adm_dates(days=14, prefix="adm_d"):
    now = datetime.now()
    rows, row = [], []
    for i in range(days):
        d = (now + timedelta(days=i)).strftime("%Y-%m-%d")
        dt = now + timedelta(days=i)
        off = "🚫" if is_dayoff(d) else ""
        label = f"{dt.day} {MONTHS[dt.month][:3]} {WEEKDAYS[dt.weekday()]} {off}".strip()
        row.append(InlineKeyboardButton(label, callback_data=f"{prefix}_{d}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

# ═══════════════════════════════════════════════════════════════════════════════
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════════════════════

def validate_phone(p):
    return 7 <= len(re.sub(r"\D", "", p)) <= 15

def set_state(ctx, state):
    ctx.user_data["state"] = state

def get_state(ctx):
    return ctx.user_data.get("state", ST_MAIN)

async def show_main(update, ctx, text="Главное меню:"):
    uid = update.effective_user.id
    set_state(ctx, ST_MAIN)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=kb_main(uid))
    else:
        await update.message.reply_text(text, reply_markup=kb_main(uid))

def confirm_text(d):
    return (
        "📋 *Подтвердите запись:*\n\n"
        f"💆 {d['proc']}\n"
        f"📅 {fmt_date(d['date'])} в {d['time']}\n"
        f"⏱ {d['dur']} мин  •  💰 {d['price']:,} ₽\n\n".replace(",", " ") +
        f"👤 {d['name']}\n"
        f"📱 {d['phone']}"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИКИ КОМАНД
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    set_state(ctx, ST_MAIN)
    await update.message.reply_text(
        f"👋 Привет, *{update.effective_user.first_name}*!\n\n"
        f"Я помогу записаться к подологу *{MASTER_NAME}*.\n"
        "Выберите действие:",
        parse_mode="Markdown",
        reply_markup=kb_main(update.effective_user.id),
    )

async def cmd_book(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if count_active(uid) >= MAX_ACTIVE:
        await update.message.reply_text(
            f"⚠️ У вас уже {MAX_ACTIVE} активных записи. Отмените одну.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("Мои записи", callback_data="my_apts"),
            ]]))
        return
    set_state(ctx, ST_PROC)
    await update.message.reply_text(
        "💆 *Выберите процедуру:*", parse_mode="Markdown", reply_markup=kb_procs())

async def cmd_my_apts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    apts = user_apts(uid)
    if not apts:
        await update.message.reply_text("У вас нет предстоящих записей.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📅 Записаться", callback_data="book"),
            ]]))
        return
    lines = ["📋 *Ваши записи:*\n"]
    for a in apts:
        lines.append(f"• {fmt_date(a['date'])} в {a['time']} — {a['procedure']}")
    await update.message.reply_text(
        "\n".join(lines) + "\n\nНажмите для отмены:",
        parse_mode="Markdown", reply_markup=kb_my_apts(apts))

async def cmd_prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = ["💆 *Услуги и цены:*\n"]
    for name, dur, price, desc in PROCEDURES:
        lines.append(
            f"*{name}*\n"
            f"⏱ {dur} мин  •  💰 {price:,} ₽\n".replace(",", " ") +
            f"_{desc}_\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown",
                                     reply_markup=kb_back("main"))

async def cmd_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📞 *Контакты:*\n\nТелефон: {MASTER_PHONE}\nTelegram: {MASTER_TG}\n\n"
        f"Режим работы: пн–сб, {WORK_START}:00–{WORK_END}:00",
        parse_mode="Markdown", reply_markup=kb_back("main"))

async def cmd_about(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👩‍⚕️ *{MASTER_NAME}*\n\n{MASTER_ABOUT}\n\n"
        f"⭐ Рейтинг: {MASTER_RATING}\n"
        f"📅 Принимает с {MASTER_SINCE} года",
        parse_mode="Markdown", reply_markup=kb_back("main"))

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет доступа.")
        return
    set_state(ctx, ST_ADM)
    await update.message.reply_text(
        "🔐 *Панель мастера:*", parse_mode="Markdown", reply_markup=kb_admin())

# ═══════════════════════════════════════════════════════════════════════════════
# ГЛАВНЫЙ РОУТЕР КНОПОК
# ═══════════════════════════════════════════════════════════════════════════════

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await q.answer()
    data = q.data
    uid  = update.effective_user.id
    log.info("КНОПКА: data='%s' uid=%s state='%s'", data, uid, get_state(ctx))

    # ── Главное меню ──────────────────────────────────────────────────────────
    if data == "main":
        ctx.user_data.clear()
        set_state(ctx, ST_MAIN)
        await q.edit_message_text("Главное меню:", reply_markup=kb_main(uid))
        return

    if data == "prices":
        lines = ["💆 *Услуги и цены:*\n"]
        for name, dur, price, desc in PROCEDURES:
            lines.append(
                f"*{name}*\n"
                f"⏱ {dur} мин  •  💰 {price:,} ₽\n".replace(",", " ") +
                f"_{desc}_\n"
            )
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                   reply_markup=kb_back("main"))
        return

    if data == "about":
        await q.edit_message_text(
            f"👩‍⚕️ *{MASTER_NAME}*\n\n{MASTER_ABOUT}\n\n"
            f"⭐ Рейтинг: {MASTER_RATING}\n"
            f"📅 Принимает с {MASTER_SINCE} года",
            parse_mode="Markdown", reply_markup=kb_back("main"))
        return

    if data == "contact":
        await q.edit_message_text(
            f"📞 *Контакты:*\n\nТелефон: {MASTER_PHONE}\nTelegram: {MASTER_TG}\n\n"
            f"Режим работы: пн–сб, {WORK_START}:00–{WORK_END}:00",
            parse_mode="Markdown", reply_markup=kb_back("main"))
        return

    # ── Мои записи ────────────────────────────────────────────────────────────
    if data == "my_apts":
        apts = user_apts(uid)
        if not apts:
            await q.edit_message_text(
                "У вас нет предстоящих записей.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📅 Записаться", callback_data="book")],
                    [InlineKeyboardButton("◀️ Назад",      callback_data="main")],
                ]))
            return
        lines = ["📋 *Ваши записи:*\n"]
        for a in apts:
            lines.append(f"• {fmt_date(a['date'])} в {a['time']} — {a['procedure']}")
        await q.edit_message_text(
            "\n".join(lines) + "\n\nНажмите для отмены:",
            parse_mode="Markdown", reply_markup=kb_my_apts(apts))
        return

    if data.startswith("cancel_"):
        apt_id = data[7:]
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
                    parse_mode="Markdown")
            except Exception:
                pass
            await q.edit_message_text(
                f"✅ Запись на {fmt_date(apt['date'])} в {apt['time']} отменена.",
                reply_markup=kb_back("main"))
        else:
            await q.edit_message_text("Запись не найдена.", reply_markup=kb_back("main"))
        return

    # ── Запись: выбор процедуры ───────────────────────────────────────────────
    if data == "book":
        if count_active(uid) >= MAX_ACTIVE:
            await q.edit_message_text(
                f"⚠️ У вас уже {MAX_ACTIVE} активных записи.\n"
                "Отмените одну, чтобы записаться снова.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("Мои записи", callback_data="my_apts")],
                    [InlineKeyboardButton("◀️ Назад",   callback_data="main")],
                ]))
            return
        set_state(ctx, ST_PROC)
        await q.edit_message_text("💆 *Выберите процедуру:*",
                                   parse_mode="Markdown", reply_markup=kb_procs())
        return

    if data.startswith("proc_"):
        idx = int(data[5:])
        name, dur, price, desc = PROCEDURES[idx]
        ctx.user_data.update(proc=name, dur=dur, price=price)
        dates = available_dates()
        if not dates:
            await q.edit_message_text(
                "😔 Свободных дат нет. Загляните позже.",
                reply_markup=kb_back("main"))
            return
        set_state(ctx, ST_DATE)
        await q.edit_message_text(
            f"✅ *{name}*\n_{desc}_\n\n📅 Выберите дату:",
            parse_mode="Markdown", reply_markup=kb_dates(dates))
        return

    if data == "back_date":
        dates = available_dates()
        set_state(ctx, ST_DATE)
        proc_name = ctx.user_data.get("proc", "Процедура")
        await q.edit_message_text(
            f"💆 *{proc_name}*\n\n📅 Выберите дату:",
            parse_mode="Markdown", reply_markup=kb_dates(dates))
        return

    if data.startswith("date_"):
        date = data[5:]
        ctx.user_data["date"] = date
        slots = free_slots(date)
        if not slots:
            await q.edit_message_text(
                "На этот день нет свободных слотов.",
                reply_markup=kb_dates(available_dates()))
            return
        set_state(ctx, ST_TIME)
        await q.edit_message_text(
            f"📅 *{fmt_date(date)}*\n\n🕐 Выберите время:",
            parse_mode="Markdown", reply_markup=kb_times(slots))
        return

    if data.startswith("time_"):
        time = data[5:]
        ctx.user_data["time"] = time
        prev = prev_contact(uid)
        if prev:
            ctx.user_data["name"]  = prev[0]
            ctx.user_data["phone"] = prev[1]
            set_state(ctx, ST_CONFIRM)
            await q.edit_message_text(
                confirm_text(ctx.user_data),
                parse_mode="Markdown", reply_markup=kb_confirm())
        else:
            set_state(ctx, ST_NAME)
            await q.edit_message_text(
                "📝 Введите ваше *имя и фамилию:*", parse_mode="Markdown")
        return

    if data == "confirm_yes":
        d = ctx.user_data
        if not d.get("proc"):
            await q.edit_message_text(
                "⚠️ Сессия устарела. Начните запись заново.",
                reply_markup=kb_main(uid))
            return
        # Сохраняем в локальные переменные ДО очистки
        proc  = d["proc"];  date  = d["date"]
        time  = d["time"];  name  = d["name"]
        phone = d["phone"]; price = d["price"]
        dur   = d["dur"]
        try:
            apt_id = add_apt(date, time, name, phone, uid, proc, dur, price)
        except ValueError as e:
            await q.edit_message_text(str(e), reply_markup=kb_main(uid))
            return
        schedule_reminders(ctx.application, apt_id, uid, date, time)
        ctx.user_data.clear()
        set_state(ctx, ST_MAIN)
        try:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"🔔 *Новая запись!*\n\n"
                f"👤 {name}  📱 {phone}\n"
                f"💆 {proc}\n"
                f"📅 {fmt_date(date)} в {time}\n"
                f"💰 {price:,} ₽".replace(",", " "),
                parse_mode="Markdown")
        except Exception:
            pass
        await q.edit_message_text(
            f"🎉 *Вы записаны!*\n\n"
            f"💆 {proc}\n"
            f"📅 {fmt_date(date)} в {time}\n\n"
            "Напомню за сутки и за 2 часа. 🔔\n\n"
            f"Вопросы: {MASTER_PHONE}",
            parse_mode="Markdown", reply_markup=kb_back("main"))
        return

    # ── Панель администратора ─────────────────────────────────────────────────
    if uid != ADMIN_ID and data.startswith("adm"):
        await q.answer("⛔ Нет доступа.", show_alert=True)
        return

    if data == "admin":
        set_state(ctx, ST_ADM)
        await q.edit_message_text("🔐 *Панель мастера:*",
                                   parse_mode="Markdown", reply_markup=kb_admin())
        return

    if data == "adm_sched":
        ctx.user_data["adm_action"] = "sched"
        await q.edit_message_text("📅 Выберите день:", reply_markup=kb_adm_dates(14, "adm_d"))
        return

    if data == "adm_week":
        now = datetime.now()
        lines = ["📆 *Расписание на 7 дней:*\n"]
        for i in range(7):
            d = (now + timedelta(days=i)).strftime("%Y-%m-%d")
            apts = day_apts(d)
            fr = free_slots(d)
            lines.append(f"*{fmt_date(d)}*")
            for a in apts:
                lines.append(f"  🕐 {a['time']} {a['name']} — {a['procedure']}")
            if not apts:
                lines.append("  _(пусто)_")
            lines.append(f"  🟢 Свободно: {len(fr)}\n")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                   reply_markup=kb_back("admin"))
        return

    if data == "adm_block":
        ctx.user_data["adm_action"] = "block"
        await q.edit_message_text("🚫 Выберите дату:", reply_markup=kb_adm_dates(14, "adm_d"))
        return

    if data == "adm_unblock":
        bl = all_blocked_slots()
        if not bl:
            await q.edit_message_text("Нет заблокированных слотов.",
                                       reply_markup=kb_back("admin"))
            return
        rows = [[InlineKeyboardButton(
            f"🔓 {fmt_date(b['date'])} в {b['time']}",
            callback_data=f"adm_unblk_{b['date']}|{b['time']}"
        )] for b in bl]
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
        await q.edit_message_text("🔓 Выберите слот:",
                                   reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("adm_unblk_"):
        payload = data[len("adm_unblk_"):]
        d, t = payload.split("|", 1)
        unblock_slot(d, t)
        await q.edit_message_text(f"✅ Слот {fmt_date(d)} в {t} разблокирован.",
                                   reply_markup=kb_admin())
        return

    if data == "adm_dayoff":
        ctx.user_data["adm_action"] = "dayoff"
        await q.edit_message_text("📵 Выберите день:", reply_markup=kb_adm_dates(30, "adm_d"))
        return

    if data == "adm_add":
        ctx.user_data["adm_action"] = "add"
        await q.edit_message_text("➕ Выберите дату:", reply_markup=kb_adm_dates(14, "adm_d"))
        return

    if data == "adm_cancel":
        now = datetime.now()
        apts = []
        for i in range(30):
            apts.extend(day_apts((now + timedelta(days=i)).strftime("%Y-%m-%d")))
        if not apts:
            await q.edit_message_text("Нет активных записей.", reply_markup=kb_back("admin"))
            return
        rows = [[InlineKeyboardButton(
            f"{a['date']} {a['time']} — {a['name']}",
            callback_data=f"adm_cncl_{a['id']}"
        )] for a in apts]
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
        await q.edit_message_text("❌ Выберите запись:",
                                   reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("adm_cncl_"):
        apt_id = data[len("adm_cncl_"):]
        apt = cancel_apt(apt_id)
        if apt:
            cancel_reminders(ctx.application, apt_id)
            try:
                await ctx.bot.send_message(
                    apt["user_id"],
                    f"⚠️ *Ваша запись отменена мастером*\n\n"
                    f"📅 {fmt_date(apt['date'])} в {apt['time']}\n"
                    f"💆 {apt['procedure']}\n\n"
                    f"Свяжитесь: {MASTER_PHONE}",
                    parse_mode="Markdown")
            except Exception:
                pass
            await q.edit_message_text(
                f"✅ Запись {apt['name']} отменена. Клиент уведомлён.",
                reply_markup=kb_admin())
        return

    # Выбор даты в панели администратора
    if data.startswith("adm_d_"):
        date = data[len("adm_d_"):]
        action = ctx.user_data.get("adm_action", "sched")

        if action == "sched":
            apts = day_apts(date)
            off  = is_dayoff(date)
            lines = [f"📅 *{fmt_date(date)}*\n"]
            if off:
                lines.append("🚫 Выходной день\n")
            if apts:
                total = sum(a["price"] for a in apts)
                for a in apts:
                    lines.append(f"🕐 {a['time']}  {a['name']}  {a['phone']}\n"
                                 f"   {a['procedure']} — {a['price']:,} ₽".replace(",", " "))
                lines.append(f"\n💰 Итого: {total:,} ₽  •  {len(apts)} записей".replace(",", " "))
            else:
                lines.append("_Записей нет._")
            lines.append(f"\n🟢 Свободных слотов: {len(free_slots(date))}")
            await q.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                       reply_markup=kb_back("admin"))
            return

        if action == "dayoff":
            result = toggle_dayoff(date)
            msg = (f"🚫 {fmt_date(date)} — добавлен как выходной."
                   if result == "added"
                   else f"✅ {fmt_date(date)} — убран из выходных.")
            await q.edit_message_text(msg, reply_markup=kb_admin())
            return

        if action == "block":
            ctx.user_data["adm_date"] = date
            statuses = all_slots_status(date)
            rows = []
            for t, s in statuses:
                e = "🔴" if s == "booked" else ("🟡" if s == "blocked" else "🟢")
                cb = f"adm_blk_{date}|{t}" if s == "free" else "adm_noop"
                rows.append([InlineKeyboardButton(f"{e} {t}", callback_data=cb)])
            rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
            await q.edit_message_text(
                f"*{fmt_date(date)}*\n🟢 свободен  🟡 заблокирован  🔴 занят",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
            return

        if action == "add":
            ctx.user_data["adm_date"] = date
            slots = free_slots(date)
            if not slots:
                await q.edit_message_text("Нет свободных слотов.",
                                           reply_markup=kb_back("admin"))
                return
            rows, row = [], []
            for s in slots:
                row.append(InlineKeyboardButton(s, callback_data=f"adm_addt_{date}|{s}"))
                if len(row) == 4:
                    rows.append(row); row = []
            if row:
                rows.append(row)
            rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
            await q.edit_message_text(f"🕐 Выберите время на {fmt_date(date)}:",
                                       reply_markup=InlineKeyboardMarkup(rows))
            return

    if data.startswith("adm_blk_"):
        payload = data[len("adm_blk_"):]
        date, time = payload.split("|", 1)
        block_slot(date, time)
        await q.edit_message_text(f"🚫 Слот {fmt_date(date)} в {time} заблокирован.",
                                   reply_markup=kb_admin())
        return

    if data == "adm_noop":
        return

    if data.startswith("adm_addt_"):
        payload = data[len("adm_addt_"):]
        date, time = payload.split("|", 1)
        ctx.user_data["adm_date"] = date
        ctx.user_data["adm_time"] = time
        await q.edit_message_text("💆 Выберите процедуру:", reply_markup=kb_procs())
        ctx.user_data["adm_action"] = "add_proc"
        return

    if data.startswith("proc_") and ctx.user_data.get("adm_action") == "add_proc":
        idx = int(data[5:])
        name, dur, price, _ = PROCEDURES[idx]
        ctx.user_data.update(adm_proc=name, adm_dur=dur, adm_price=price)
        set_state(ctx, ST_ADM_ADD_NAME)
        await q.edit_message_text("👤 Введите имя клиента:")
        return

# ═══════════════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИК ТЕКСТОВЫХ СООБЩЕНИЙ
# ═══════════════════════════════════════════════════════════════════════════════

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text  = update.message.text.strip()
    state = get_state(ctx)
    uid   = update.effective_user.id

    # Ввод имени при записи
    if state == ST_NAME:
        if len(text) < 2:
            await update.message.reply_text("Пожалуйста, введите настоящее имя.")
            return
        ctx.user_data["name"] = text
        set_state(ctx, ST_PHONE)
        await update.message.reply_text(
            "📱 Введите ваш *номер телефона:*", parse_mode="Markdown")
        return

    # Ввод телефона при записи
    if state == ST_PHONE:
        if not validate_phone(text):
            await update.message.reply_text(
                "❌ Неверный формат. Пример: *+7 900 123-45-67*", parse_mode="Markdown")
            return
        ctx.user_data["phone"] = text
        set_state(ctx, ST_CONFIRM)
        await update.message.reply_text(
            confirm_text(ctx.user_data),
            parse_mode="Markdown", reply_markup=kb_confirm())
        return

    # Ввод имени клиента администратором
    if state == ST_ADM_ADD_NAME:
        ctx.user_data["adm_name"] = text
        set_state(ctx, ST_ADM_ADD_PHONE)
        await update.message.reply_text("📱 Введите телефон клиента:")
        return

    # Ввод телефона клиента администратором
    if state == ST_ADM_ADD_PHONE:
        if not validate_phone(text):
            await update.message.reply_text("❌ Неверный формат. Введите ещё раз:")
            return
        d = ctx.user_data
        try:
            add_apt(d["adm_date"], d["adm_time"], d["adm_name"], text,
                    ADMIN_ID, d["adm_proc"], d["adm_dur"], d["adm_price"])
        except ValueError as e:
            await update.message.reply_text(str(e), reply_markup=kb_admin())
            set_state(ctx, ST_ADM)
            return
        await update.message.reply_text(
            f"✅ *Запись создана:*\n\n"
            f"👤 {d['adm_name']}  📱 {text}\n"
            f"💆 {d['adm_proc']}\n"
            f"📅 {fmt_date(d['adm_date'])} в {d['adm_time']}",
            parse_mode="Markdown", reply_markup=kb_admin())
        set_state(ctx, ST_ADM)
        return

    # Любое другое сообщение — показываем главное меню
    await update.message.reply_text(
        "Используйте кнопки меню 👇",
        reply_markup=kb_main(uid))

# ═══════════════════════════════════════════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    init_db()

    persistence = PicklePersistence(filepath="podolog_data")
    app = Application.builder().token(BOT_TOKEN).persistence(persistence).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("book",    cmd_book))
    app.add_handler(CommandHandler("my_apts", cmd_my_apts))
    app.add_handler(CommandHandler("prices",  cmd_prices))
    app.add_handler(CommandHandler("contact", cmd_contact))
    app.add_handler(CommandHandler("about",   cmd_about))
    app.add_handler(CommandHandler("admin",   cmd_admin))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    if app.job_queue:
        log.info("Напоминания активны ✅")
    else:
        log.warning('Напоминания выключены. pip install "python-telegram-bot[job-queue]"')

    log.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
