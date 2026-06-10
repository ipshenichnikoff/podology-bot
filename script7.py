#!/usr/bin/env python3
"""
PODOLOGY BOT — Полная рабочая версия.
Клиент: запись, просмотр, отмена, повтор процедуры.
Админ: расписание, блокировка слотов, выходные дни, отмена записей,
       статистика, рассылка, поиск клиента, рабочие часы, бан/разбан.
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

# ─────────────────────────────────────────────────────────────────────────────
# НАСТРОЙКИ
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN       = os.getenv("BOT_TOKEN",  "8827220812:AAG7FxZR778sSDX9a_BBicW7Datw-7s7Mdg")
ADMIN_ID        = int(os.getenv("ADMIN_ID", "223326752"))

MASTER_NAME     = "Екатерина Шлейфер"
MASTER_USERNAME = "@master"
MASTER_PHONE    = "8 (920) 649 26-16"
MASTER_RATING   = "4.9"
MASTER_CLIENTS  = "200"
MASTER_SINCE    = "2018"

WORK_START_DEFAULT  = 10
WORK_END_DEFAULT    = 20
SLOT_MIN_DEFAULT    = 60
DB_FILE             = "appointments.db"
MAX_ACTIVE_APTS     = 2

PROCEDURES = [
    ("Педикюр аппаратный",     60, 2800, "✨ Профессиональная обработка стопы и пальцев, удаление натоптышей."),
    ("Гигиенический педикюр",  45, 2200, "🌿 Базовый уход: придание формы ногтям, легкая обработка кожи."),
    ("Лечение вросшего ногтя", 90, 3500, "🩺 Безболезненное устранение проблемы, облегчение боли за 1 сеанс."),
    ("Протезирование ногтя",   90, 4500, "💎 Восстановление эстетического вида ногтевой пластины гелем."),
    ("Ортониксия (скобы)",     60, 5200, "🛠 Коррекция формы ногтя специальными системами (титановая нить)."),
    ("Лечение грибка (микоз)", 60, 3200, "🔬 Комплексная зачистка + подбор терапевтического ухода."),
]

# ─────────────────────────────────────────────────────────────────────────────
# СОСТОЯНИЯ
# ─────────────────────────────────────────────────────────────────────────────
(
    ST_CLIENT_MENU,
    ST_CHOOSE_PROC,
    ST_CHOOSE_DATE,
    ST_CHOOSE_TIME,
    ST_ENTER_NAME,
    ST_ENTER_PHONE,
    ST_CONFIRM_BOOK,
    ST_ADM_MENU,
    ST_ADM_CHOOSE_DATE,
    ST_ADM_CHOOSE_SLOT,
    ST_ADM_CANCEL_APT,
    ST_ADM_STATS,
    ST_ADM_BROADCAST,
    ST_ADM_SEARCH,
    ST_ADM_WORKHOURS,
    ST_ADM_BAN,
) = range(16)

MONTHS     = ["","января","февраля","марта","апреля","мая","июня",
              "июля","августа","сентября","октября","ноября","декабря"]
SHORT_DAYS = ["пн","вт","ср","чт","пт","сб","вс"]

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger(__name__)
db_lock = threading.Lock()

# ─────────────────────────────────────────────────────────────────────────────
# БД
# ─────────────────────────────────────────────────────────────────────────────
def init_db():
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("""
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
                created   TEXT NOT NULL,
                note      TEXT DEFAULT ''
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS blocked_slots (
                date TEXT NOT NULL, time TEXT NOT NULL,
                PRIMARY KEY (date, time))""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS day_off (
                date TEXT PRIMARY KEY, reason TEXT DEFAULT 'Выходной')""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id         INTEGER PRIMARY KEY,
                first_name      TEXT,
                seen_onboarding INTEGER DEFAULT 0,
                last_procedure  TEXT DEFAULT NULL,
                is_banned       INTEGER DEFAULT 0)""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
        # Миграции для старых БД
        for table, col, defn in [
            ("users",        "is_banned", "INTEGER DEFAULT 0"),
            ("appointments", "note",      "TEXT DEFAULT ''"),
        ]:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
                log.info("Миграция: %s.%s добавлен", table, col)
            except sqlite3.OperationalError:
                pass
        conn.commit()

# ─── Настройки ───────────────────────────────────────────────────────────────
def get_setting(key, default):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT value FROM settings WHERE key=?", (key,))
        row = c.fetchone()
        return row[0] if row else str(default)

def set_setting(key, value):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                  "ON CONFLICT(key) DO UPDATE SET value=?",
                  (key, str(value), str(value)))
        conn.commit()

def work_start():  return int(get_setting("work_start",  WORK_START_DEFAULT))
def work_end():    return int(get_setting("work_end",    WORK_END_DEFAULT))
def slot_minutes():return int(get_setting("slot_minutes",SLOT_MIN_DEFAULT))

# ─── Пользователи ────────────────────────────────────────────────────────────
def has_seen_onboarding(uid):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT seen_onboarding FROM users WHERE user_id=?", (uid,))
        row = c.fetchone()
        return bool(row and row[0])

def mark_onboarding_seen(uid, first_name=""):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO users(user_id,first_name,seen_onboarding) VALUES(?,?,1) "
                  "ON CONFLICT(user_id) DO UPDATE SET seen_onboarding=1", (uid, first_name))
        conn.commit()

def set_last_procedure(uid, proc_name):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("INSERT INTO users(user_id,last_procedure) VALUES(?,?) "
                  "ON CONFLICT(user_id) DO UPDATE SET last_procedure=?",
                  (uid, proc_name, proc_name))
        conn.commit()

def get_last_procedure(uid):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT last_procedure FROM users WHERE user_id=?", (uid,))
        row = c.fetchone()
        return row[0] if row else None

def is_banned(uid):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT is_banned FROM users WHERE user_id=?", (uid,))
        row = c.fetchone()
        return bool(row and row[0])

def toggle_ban(uid):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT is_banned FROM users WHERE user_id=?", (uid,))
        row = c.fetchone()
        new_val = 0 if (row and row[0]) else 1
        c.execute("INSERT INTO users(user_id,is_banned) VALUES(?,?) "
                  "ON CONFLICT(user_id) DO UPDATE SET is_banned=?",
                  (uid, new_val, new_val))
        conn.commit()
        return "banned" if new_val else "unbanned"

def get_all_users():
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE is_banned=0")
        return [r[0] for r in c.fetchall()]

def search_clients(query):
    q = f"%{query.strip()}%"
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("""SELECT DISTINCT name, phone, user_id FROM appointments
                     WHERE (name LIKE ? OR phone LIKE ?) AND status='confirmed'
                     ORDER BY name LIMIT 20""", (q, q))
        return [dict(r) for r in c.fetchall()]

# ─── Слоты и записи ──────────────────────────────────────────────────────────
def is_day_off(date_str):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM day_off WHERE date=?", (date_str,))
        return c.fetchone() is not None

def toggle_day_off(date_str):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM day_off WHERE date=?", (date_str,))
        if c.fetchone():
            c.execute("DELETE FROM day_off WHERE date=?", (date_str,))
            conn.commit(); return "removed"
        else:
            c.execute("INSERT INTO day_off(date,reason) VALUES(?,'Выходной')", (date_str,))
            conn.commit(); return "added"

def get_booked_slots(date_str):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT time FROM appointments WHERE date=? AND status!='cancelled'", (date_str,))
        return [r[0] for r in c.fetchall()]

def get_blocked_slots(date_str):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT time FROM blocked_slots WHERE date=?", (date_str,))
        return [r[0] for r in c.fetchall()]

def get_free_slots(date_str):
    if is_day_off(date_str):
        return []
    taken = set(get_booked_slots(date_str) + get_blocked_slots(date_str))
    slots, cur, sm = [], work_start() * 60, slot_minutes()
    while cur + sm <= work_end() * 60:
        hh, mm = divmod(cur, 60)
        t = f"{hh:02d}:{mm:02d}"
        if t not in taken:
            slots.append(t)
        cur += sm
    return slots

def get_all_slots_status(date_str):
    booked  = set(get_booked_slots(date_str))
    blocked = set(get_blocked_slots(date_str))
    slots, cur, sm = [], work_start() * 60, slot_minutes()
    while cur + sm <= work_end() * 60:
        hh, mm = divmod(cur, 60)
        t = f"{hh:02d}:{mm:02d}"
        status = "booked" if t in booked else ("blocked" if t in blocked else "free")
        slots.append((t, status))
        cur += sm
    return slots

def count_active_appointments(uid):
    today = datetime.now().strftime("%Y-%m-%d")
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM appointments "
                  "WHERE user_id=? AND status='confirmed' AND date>=?", (uid, today))
        return c.fetchone()[0]

def add_appointment(date_str, time_str, name, phone, uid, proc_name, duration, price):
    apt_id = f"{date_str}-{time_str.replace(':','')}-{uid}"
    created = datetime.now().strftime("%Y-%m-%d %H:%M")
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM appointments "
                  "WHERE date=? AND time=? AND status='confirmed'", (date_str, time_str))
        if c.fetchone()[0] > 0:
            raise ValueError("Этот слот уже занят, выберите другое время.")
        c.execute("""INSERT OR REPLACE INTO appointments
            (id,date,time,name,phone,user_id,procedure,duration,price,status,created)
            VALUES(?,?,?,?,?,?,?,?,?,'confirmed',?)""",
            (apt_id, date_str, time_str, name, phone, uid, proc_name, duration, price, created))
        conn.commit()
    return apt_id

def cancel_appointment(apt_id):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM appointments WHERE id=? AND status='confirmed'", (apt_id,))
        row = c.fetchone()
        if row:
            res = dict(row)
            c.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (apt_id,))
            conn.commit()
            return res
    return None

def toggle_block_slot(date_str, time_str):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT 1 FROM blocked_slots WHERE date=? AND time=?", (date_str, time_str))
        if c.fetchone():
            c.execute("DELETE FROM blocked_slots WHERE date=? AND time=?", (date_str, time_str))
            conn.commit(); return "unblocked"
        else:
            c.execute("INSERT INTO blocked_slots(date,time) VALUES(?,?)", (date_str, time_str))
            conn.commit(); return "blocked"

def get_user_active_appointments(uid):
    today = datetime.now().strftime("%Y-%m-%d")
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM appointments "
                  "WHERE user_id=? AND status='confirmed' AND date>=? ORDER BY date,time",
                  (uid, today))
        return [dict(r) for r in c.fetchall()]

def get_day_appointments(date_str):
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute("SELECT * FROM appointments WHERE date=? AND status='confirmed' ORDER BY time",
                  (date_str,))
        return [dict(r) for r in c.fetchall()]

def get_stats(period):
    today = datetime.now().date()
    if period == "day":   start = today
    elif period == "week": start = today - timedelta(days=today.weekday())
    else:                  start = today.replace(day=1)
    s, e = start.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")
    with db_lock, sqlite3.connect(DB_FILE) as conn:
        c = conn.cursor()
        c.execute("SELECT COUNT(*), COALESCE(SUM(price),0) FROM appointments "
                  "WHERE status='confirmed' AND date BETWEEN ? AND ?", (s, e))
        cnt, rev = c.fetchone()
        c.execute("SELECT procedure, COUNT(*) cnt FROM appointments "
                  "WHERE status='confirmed' AND date BETWEEN ? AND ? "
                  "GROUP BY procedure ORDER BY cnt DESC LIMIT 3", (s, e))
        top = c.fetchall()
    return {"period": period, "start": s, "end": e, "count": cnt, "revenue": rev, "top": top}

# ─────────────────────────────────────────────────────────────────────────────
# ХЕЛПЕРЫ
# ─────────────────────────────────────────────────────────────────────────────
def fmt_date(date_str):
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return f"{d.day} {MONTHS[d.month]}, {SHORT_DAYS[d.weekday()]}"

def admin_only(func):
    @wraps(func)
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE, *a, **kw):
        uid = update.effective_user.id
        if uid != ADMIN_ID:
            if update.message:
                await update.message.reply_text("⛔ Нет доступа.")
            elif update.callback_query:
                await update.callback_query.answer("⛔ Нет доступа.", show_alert=True)
            return ConversationHandler.END
        return await func(update, ctx, *a, **kw)
    return wrapper

# ─────────────────────────────────────────────────────────────────────────────
# КЛАВИАТУРЫ
# ─────────────────────────────────────────────────────────────────────────────
def kb_main_menu(uid):
    last = get_last_procedure(uid)
    rows = []
    if last:
        short = last[:25] + "…" if len(last) > 25 else last
        rows.append([InlineKeyboardButton(f"🔄 Снова: {short}", callback_data="repeat_proc")])
    rows += [
        [InlineKeyboardButton("📋 Записаться",  callback_data="book"),
         InlineKeyboardButton("📅 Мои записи", callback_data="my_apts")],
        [InlineKeyboardButton("ℹ️ О мастере",   callback_data="about")],
    ]
    return InlineKeyboardMarkup(rows)

def kb_procedures():
    rows = []
    for i, (name, dur, price, _) in enumerate(PROCEDURES):
        rows.append([InlineKeyboardButton(
            f"{name} — {price} ₽ ({dur} мин)", callback_data=f"proc_{i}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)

def kb_date_picker_client(days_ahead=14):
    today = datetime.now().date()
    buttons, row = [], []
    for i in range(days_ahead):
        d = today + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        if is_day_off(ds):
            continue
        if not get_free_slots(ds):
            continue
        label = f"{d.day} {SHORT_DAYS[d.weekday()]}"
        row.append(InlineKeyboardButton(label, callback_data=f"cdate_{ds}"))
        if len(row) == 4:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    if not buttons:
        return None
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(buttons)

def kb_time_slots(date_str):
    slots = get_free_slots(date_str)
    if not slots:
        return None
    rows = [[InlineKeyboardButton(t, callback_data=f"ctime_{t}")] for t in slots]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="book")])
    return InlineKeyboardMarkup(rows)

def kb_confirm():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Подтвердить", callback_data="confirm_yes"),
         InlineKeyboardButton("❌ Отмена",      callback_data="confirm_no")],
    ])

def kb_my_apts(apts):
    rows = []
    for a in apts:
        label = f"{fmt_date(a['date'])} {a['time']} — {a['procedure'][:20]}"
        rows.append([InlineKeyboardButton(f"❌ Отменить: {label}",
                                          callback_data=f"cancel_apt_{a['id']}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="back_main")])
    return InlineKeyboardMarkup(rows)

def kb_admin_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Расписание",       callback_data="adm_schedule"),
         InlineKeyboardButton("🔒 Блокировать слот", callback_data="adm_block")],
        [InlineKeyboardButton("🚫 Выходной день",    callback_data="adm_dayoff"),
         InlineKeyboardButton("❌ Отменить запись",  callback_data="adm_cancel")],
        [InlineKeyboardButton("📊 Статистика",       callback_data="adm_stats"),
         InlineKeyboardButton("📢 Рассылка",         callback_data="adm_broadcast")],
        [InlineKeyboardButton("🔍 Найти клиента",    callback_data="adm_search"),
         InlineKeyboardButton("⏰ Рабочие часы",     callback_data="adm_workhours")],
        [InlineKeyboardButton("🚷 Бан/разбан",       callback_data="adm_ban")],
    ])

def kb_adm_date_picker(days_ahead=14):
    today = datetime.now().date()
    buttons, row = [], []
    for i in range(days_ahead):
        d = today + timedelta(days=i)
        ds = d.strftime("%Y-%m-%d")
        off = " 🚫" if is_day_off(ds) else ""
        row.append(InlineKeyboardButton(f"{d.day} {SHORT_DAYS[d.weekday()]}{off}",
                                        callback_data=f"adate_{ds}"))
        if len(row) == 4:
            buttons.append(row); row = []
    if row:
        buttons.append(row)
    buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_back")])
    return InlineKeyboardMarkup(buttons)

def kb_stats_periods():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("Сегодня", callback_data="stats_day"),
         InlineKeyboardButton("Неделя",  callback_data="stats_week"),
         InlineKeyboardButton("Месяц",   callback_data="stats_month")],
        [InlineKeyboardButton("◀️ Назад", callback_data="adm_back")],
    ])

# ─────────────────────────────────────────────────────────────────────────────
# КЛИЕНТСКИЕ ОБРАБОТЧИКИ
# ─────────────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if is_banned(user.id):
        await update.message.reply_text("⛔ Вы заблокированы.")
        return ConversationHandler.END

    if not has_seen_onboarding(user.id):
        mark_onboarding_seen(user.id, user.first_name or "")
        text = (f"Привет, {user.first_name or 'гость'}! 👋\n\n"
                f"Я бот мастера-подолога *{MASTER_NAME}*.\n"
                f"Опыт с {MASTER_SINCE} года · ⭐ {MASTER_RATING} · "
                f"{MASTER_CLIENTS}+ клиентов\n\n"
                "Записаться легко — выбери нужный пункт ниже.")
    else:
        text = f"С возвращением, {user.first_name or ''}! Чем могу помочь?"

    await update.message.reply_text(text, parse_mode="Markdown",
                                    reply_markup=kb_main_menu(user.id))
    return ST_CLIENT_MENU


async def client_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid  = q.from_user.id
    data = q.data

    # ── Назад в главное меню ─────────────────────────────────────────────────
    if data == "back_main":
        await q.edit_message_text("Главное меню:", reply_markup=kb_main_menu(uid))
        return ST_CLIENT_MENU

    # ── О мастере ────────────────────────────────────────────────────────────
    if data == "about":
        text = (f"👩‍⚕️ *{MASTER_NAME}*\n"
                f"📞 {MASTER_PHONE}\n"
                f"Telegram: {MASTER_USERNAME}\n\n"
                f"⭐ Рейтинг: {MASTER_RATING}\n"
                f"👥 Клиентов: {MASTER_CLIENTS}+\n"
                f"📅 Работает с {MASTER_SINCE} года")
        await q.edit_message_text(text, parse_mode="Markdown",
                                  reply_markup=InlineKeyboardMarkup([[
                                      InlineKeyboardButton("◀️ Назад", callback_data="back_main")
                                  ]]))
        return ST_CLIENT_MENU

    # ── Мои записи ───────────────────────────────────────────────────────────
    if data == "my_apts":
        apts = get_user_active_appointments(uid)
        if not apts:
            await q.edit_message_text("У вас нет предстоящих записей.",
                                      reply_markup=InlineKeyboardMarkup([[
                                          InlineKeyboardButton("◀️ Назад", callback_data="back_main")
                                      ]]))
            return ST_CLIENT_MENU
        lines = ["📅 *Ваши записи:*\n"]
        for a in apts:
            lines.append(f"• {fmt_date(a['date'])} в {a['time']}\n"
                         f"  {a['procedure']} — {a['price']} ₽")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown",
                                  reply_markup=kb_my_apts(apts))
        return ST_CLIENT_MENU

    # ── Отмена своей записи ──────────────────────────────────────────────────
    if data.startswith("cancel_apt_"):
        apt_id = data[len("cancel_apt_"):]
        apt = cancel_appointment(apt_id)
        if apt:
            await q.edit_message_text(
                f"✅ Запись на {fmt_date(apt['date'])} в {apt['time']} отменена.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("◀️ В меню", callback_data="back_main")
                ]]))
            # Уведомление администратора
            try:
                await ctx.bot.send_message(
                    ADMIN_ID,
                    f"❌ Клиент отменил запись:\n"
                    f"{apt['name']} · {apt['phone']}\n"
                    f"{fmt_date(apt['date'])} {apt['time']} · {apt['procedure']}")
            except Exception:
                pass
        else:
            await q.edit_message_text("Запись не найдена или уже отменена.",
                                      reply_markup=kb_main_menu(uid))
        return ST_CLIENT_MENU

    # ── Записаться (выбор процедуры) ─────────────────────────────────────────
    if data in ("book", "repeat_proc"):
        if count_active_appointments(uid) >= MAX_ACTIVE_APTS:
            await q.edit_message_text(
                f"⚠️ У вас уже {MAX_ACTIVE_APTS} активные записи. "
                "Сначала отмените одну из них.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("Мои записи", callback_data="my_apts"),
                    InlineKeyboardButton("◀️ Назад",   callback_data="back_main"),
                ]]))
            return ST_CLIENT_MENU

        if data == "repeat_proc":
            last = get_last_procedure(uid)
            if last:
                # Найти индекс процедуры
                for i, (name, *_) in enumerate(PROCEDURES):
                    if name == last:
                        ctx.user_data["proc_idx"] = i
                        ctx.user_data["proc_name"] = PROCEDURES[i][0]
                        ctx.user_data["proc_dur"]  = PROCEDURES[i][1]
                        ctx.user_data["proc_price"]= PROCEDURES[i][2]
                        kb = kb_date_picker_client()
                        if not kb:
                            await q.edit_message_text(
                                "😔 Свободных дат нет. Загляните позже.",
                                reply_markup=InlineKeyboardMarkup([[
                                    InlineKeyboardButton("◀️ Назад", callback_data="back_main")
                                ]]))
                            return ST_CLIENT_MENU
                        await q.edit_message_text("Выберите дату:", reply_markup=kb)
                        return ST_CHOOSE_DATE

        await q.edit_message_text("Выберите процедуру:", reply_markup=kb_procedures())
        return ST_CHOOSE_PROC

    # ── Выбор процедуры ──────────────────────────────────────────────────────
    if data.startswith("proc_"):
        idx = int(data[5:])
        name, dur, price, desc = PROCEDURES[idx]
        ctx.user_data["proc_idx"]  = idx
        ctx.user_data["proc_name"] = name
        ctx.user_data["proc_dur"]  = dur
        ctx.user_data["proc_price"]= price
        kb = kb_date_picker_client()
        if not kb:
            await q.edit_message_text("😔 Свободных дат нет. Загляните позже.",
                                      reply_markup=InlineKeyboardMarkup([[
                                          InlineKeyboardButton("◀️ Назад", callback_data="back_main")
                                      ]]))
            return ST_CLIENT_MENU
        await q.edit_message_text(
            f"*{name}*\n{desc}\n\n💰 {price} ₽ · ⏱ {dur} мин\n\nВыберите дату:",
            parse_mode="Markdown", reply_markup=kb)
        return ST_CHOOSE_DATE

    # ── Выбор даты ───────────────────────────────────────────────────────────
    if data.startswith("cdate_"):
        date_str = data[6:]
        ctx.user_data["date"] = date_str
        kb = kb_time_slots(date_str)
        if not kb:
            await q.edit_message_text("На эту дату нет свободных слотов.",
                                      reply_markup=kb_date_picker_client())
            return ST_CHOOSE_DATE
        await q.edit_message_text(
            f"📅 {fmt_date(date_str)}\nВыберите время:",
            reply_markup=kb)
        return ST_CHOOSE_TIME

    # ── Выбор времени ────────────────────────────────────────────────────────
    if data.startswith("ctime_"):
        time_str = data[6:]
        ctx.user_data["time"] = time_str
        # Проверяем что имя/телефон уже есть в БД (повторный пользователь)
        apts = get_user_active_appointments(uid)
        # Если есть предыдущие записи — имя/телефон уже знаем
        with db_lock, sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT name, phone FROM appointments WHERE user_id=? "
                      "ORDER BY created DESC LIMIT 1", (uid,))
            prev = c.fetchone()
        if prev:
            ctx.user_data["name"]  = prev["name"]
            ctx.user_data["phone"] = prev["phone"]
            await q.edit_message_text(
                f"*Подтверждение записи:*\n\n"
                f"📋 {ctx.user_data['proc_name']}\n"
                f"📅 {fmt_date(ctx.user_data['date'])} в {time_str}\n"
                f"💰 {ctx.user_data['proc_price']} ₽\n\n"
                f"👤 {prev['name']}\n📞 {prev['phone']}\n\n"
                "Всё верно?",
                parse_mode="Markdown", reply_markup=kb_confirm())
            return ST_CONFIRM_BOOK
        else:
            await q.edit_message_text("Введите ваше имя:")
            return ST_ENTER_NAME

    return ST_CLIENT_MENU


async def enter_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.message.text.strip()
    if len(name) < 2:
        await update.message.reply_text("Пожалуйста, введите корректное имя.")
        return ST_ENTER_NAME
    ctx.user_data["name"] = name
    await update.message.reply_text("Введите ваш номер телефона:")
    return ST_ENTER_PHONE


async def enter_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.text.strip()
    # Простая проверка: есть цифры
    if not re.search(r'\d{7,}', phone.replace(" ", "").replace("-", "")):
        await update.message.reply_text("Введите корректный номер телефона.")
        return ST_ENTER_PHONE
    ctx.user_data["phone"] = phone
    d = ctx.user_data
    await update.message.reply_text(
        f"*Подтверждение записи:*\n\n"
        f"📋 {d['proc_name']}\n"
        f"📅 {fmt_date(d['date'])} в {d['time']}\n"
        f"💰 {d['proc_price']} ₽\n\n"
        f"👤 {d['name']}\n📞 {d['phone']}\n\n"
        "Всё верно?",
        parse_mode="Markdown", reply_markup=kb_confirm())
    return ST_CONFIRM_BOOK


async def confirm_book(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "confirm_no":
        await q.edit_message_text("Запись отменена.", reply_markup=kb_main_menu(uid))
        return ST_CLIENT_MENU

    d = ctx.user_data
    try:
        apt_id = add_appointment(
            d["date"], d["time"], d["name"], d["phone"], uid,
            d["proc_name"], d["proc_dur"], d["proc_price"])
    except ValueError as e:
        await q.edit_message_text(str(e), reply_markup=kb_main_menu(uid))
        return ST_CLIENT_MENU

    set_last_procedure(uid, d["proc_name"])

    await q.edit_message_text(
        f"✅ *Запись подтверждена!*\n\n"
        f"📋 {d['proc_name']}\n"
        f"📅 {fmt_date(d['date'])} в {d['time']}\n"
        f"💰 {d['proc_price']} ₽\n\n"
        f"📞 Мастер: {MASTER_NAME}\n"
        f"Телефон: {MASTER_PHONE}\n\n"
        "До встречи! 😊",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("◀️ В меню", callback_data="back_main")
        ]]))

    # Уведомление администратора
    try:
        await ctx.bot.send_message(
            ADMIN_ID,
            f"🔔 *Новая запись!*\n\n"
            f"👤 {d['name']} · 📞 {d['phone']}\n"
            f"📋 {d['proc_name']}\n"
            f"📅 {fmt_date(d['date'])} в {d['time']}\n"
            f"💰 {d['proc_price']} ₽",
            parse_mode="Markdown")
    except Exception:
        pass

    return ST_CLIENT_MENU

# ─────────────────────────────────────────────────────────────────────────────
# АДМИНИСТРАТИВНЫЕ ОБРАБОТЧИКИ
# ─────────────────────────────────────────────────────────────────────────────
@admin_only
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🛠 Панель администратора",
                                    reply_markup=kb_admin_menu())
    return ST_ADM_MENU


async def adm_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data == "adm_back":
        await q.edit_message_text("🛠 Панель администратора", reply_markup=kb_admin_menu())
        return ST_ADM_MENU

    # ── Расписание ───────────────────────────────────────────────────────────
    if data == "adm_schedule":
        ctx.user_data["adm_action"] = "schedule"
        await q.edit_message_text("Выберите дату:", reply_markup=kb_adm_date_picker())
        return ST_ADM_CHOOSE_DATE

    # ── Блокировать слот ─────────────────────────────────────────────────────
    if data == "adm_block":
        ctx.user_data["adm_action"] = "block"
        await q.edit_message_text("Выберите дату:", reply_markup=kb_adm_date_picker())
        return ST_ADM_CHOOSE_DATE

    # ── Выходной день ────────────────────────────────────────────────────────
    if data == "adm_dayoff":
        ctx.user_data["adm_action"] = "dayoff"
        await q.edit_message_text("Выберите дату:", reply_markup=kb_adm_date_picker())
        return ST_ADM_CHOOSE_DATE

    # ── Отмена записи ────────────────────────────────────────────────────────
    if data == "adm_cancel":
        ctx.user_data["adm_action"] = "cancel"
        await q.edit_message_text("Выберите дату:", reply_markup=kb_adm_date_picker())
        return ST_ADM_CHOOSE_DATE

    # ── Статистика ───────────────────────────────────────────────────────────
    if data == "adm_stats":
        await q.edit_message_text("Выберите период:", reply_markup=kb_stats_periods())
        return ST_ADM_STATS

    if data.startswith("stats_"):
        period = data.split("_")[1]
        s = get_stats(period)
        label = {"day":"сегодня","week":"эту неделю","month":"этот месяц"}[period]
        lines = [f"📊 Статистика за {label}\n",
                 f"✅ Записей: {s['count']}",
                 f"💰 Выручка: {s['revenue']:,} ₽".replace(",", " ")]
        if s["top"]:
            lines.append("\n🏆 Топ услуги:")
            for proc, cnt in s["top"]:
                lines.append(f"  • {proc}: {cnt} раз")
        await q.edit_message_text("\n".join(lines),
                                  reply_markup=InlineKeyboardMarkup([[
                                      InlineKeyboardButton("◀️ Назад", callback_data="adm_stats")
                                  ]]))
        return ST_ADM_STATS

    # ── Рассылка ─────────────────────────────────────────────────────────────
    if data == "adm_broadcast":
        await q.edit_message_text("✉️ Введите текст рассылки:")
        return ST_ADM_BROADCAST

    # ── Поиск ────────────────────────────────────────────────────────────────
    if data == "adm_search":
        await q.edit_message_text("🔍 Введите имя или телефон клиента:")
        return ST_ADM_SEARCH

    # ── Рабочие часы ─────────────────────────────────────────────────────────
    if data == "adm_workhours":
        ws, we, sm = work_start(), work_end(), slot_minutes()
        await q.edit_message_text(
            f"⏰ Сейчас: {ws}:00 — {we}:00, слот {sm} мин.\n\n"
            "Введите новые параметры через пробел:\n"
            "<code>начало конец слот</code>\n"
            "Например: <code>9 19 45</code>",
            parse_mode="HTML")
        return ST_ADM_WORKHOURS

    # ── Бан/разбан ───────────────────────────────────────────────────────────
    if data == "adm_ban":
        await q.edit_message_text(
            "🚷 Введите Telegram ID пользователя:\n"
            "(найти через «Найти клиента»)")
        return ST_ADM_BAN

    # ── Выбор даты ───────────────────────────────────────────────────────────
    if data.startswith("adate_"):
        date_str = data[6:]
        action = ctx.user_data.get("adm_action", "")

        if action == "schedule":
            apts = get_day_appointments(date_str)
            day_off_flag = is_day_off(date_str)
            lines = [f"📅 Расписание на {fmt_date(date_str)}\n"]
            if day_off_flag:
                lines.append("🚫 Выходной день\n")
            if not apts:
                lines.append("Записей нет.")
            else:
                total = 0
                for a in apts:
                    note = f"\n   📝 {a['note']}" if a.get("note") else ""
                    lines.append(f"🕐 {a['time']}  {a['name']}  {a['phone']}\n"
                                 f"   {a['procedure']} — {a['price']} ₽{note}")
                    total += a["price"]
                lines.append(f"\nИтого: {len(apts)} зап. · {total} ₽")
            await q.edit_message_text("\n".join(lines),
                                      reply_markup=InlineKeyboardMarkup([[
                                          InlineKeyboardButton("◀️ Назад", callback_data="adm_back")
                                      ]]))
            return ST_ADM_MENU

        if action == "dayoff":
            result = toggle_day_off(date_str)
            msg = (f"🚫 {fmt_date(date_str)} — добавлен как выходной."
                   if result == "added"
                   else f"✅ {fmt_date(date_str)} — убран из выходных.")
            await q.edit_message_text(msg, reply_markup=kb_admin_menu())
            return ST_ADM_MENU

        if action == "block":
            ctx.user_data["adm_date"] = date_str
            slots = get_all_slots_status(date_str)
            buttons = []
            for t, s in slots:
                e = "🔴" if s == "booked" else ("🟡" if s == "blocked" else "🟢")
                buttons.append([InlineKeyboardButton(f"{e} {t}",
                                                     callback_data=f"blk_{date_str}_{t}")])
            buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_back")])
            await q.edit_message_text(
                f"Слоты на {fmt_date(date_str)}\n"
                "🟢 свободен  🟡 заблокирован  🔴 занят",
                reply_markup=InlineKeyboardMarkup(buttons))
            return ST_ADM_CHOOSE_SLOT

        if action == "cancel":
            apts = get_day_appointments(date_str)
            if not apts:
                await q.edit_message_text("На эту дату записей нет.",
                                          reply_markup=kb_admin_menu())
                return ST_ADM_MENU
            buttons = []
            for a in apts:
                label = f"{a['time']} — {a['name']} ({a['procedure'][:20]})"
                buttons.append([InlineKeyboardButton(label, callback_data=f"acncl_{a['id']}")])
            buttons.append([InlineKeyboardButton("◀️ Назад", callback_data="adm_back")])
            await q.edit_message_text(f"Записи на {fmt_date(date_str)}:",
                                      reply_markup=InlineKeyboardMarkup(buttons))
            return ST_ADM_CANCEL_APT

    # ── Переключение блокировки слота ────────────────────────────────────────
    if data.startswith("blk_"):
        _, date_str, time_str = data.split("_", 2)
        result = toggle_block_slot(date_str, time_str)
        msg = (f"🟡 Заблокировано: {time_str} на {fmt_date(date_str)}"
               if result == "blocked"
               else f"🟢 Разблокировано: {time_str} на {fmt_date(date_str)}")
        await q.edit_message_text(msg, reply_markup=kb_admin_menu())
        return ST_ADM_MENU

    # ── Отмена записи администратором ────────────────────────────────────────
    if data.startswith("acncl_"):
        apt_id = data[6:]
        apt = cancel_appointment(apt_id)
        if apt:
            await q.edit_message_text(
                f"✅ Запись отменена:\n"
                f"{apt['name']} · {apt['phone']}\n"
                f"{fmt_date(apt['date'])} {apt['time']} · {apt['procedure']}",
                reply_markup=kb_admin_menu())
            try:
                await ctx.bot.send_message(
                    apt["user_id"],
                    f"❌ Ваша запись на {fmt_date(apt['date'])} в {apt['time']} "
                    f"({apt['procedure']}) отменена мастером.\n"
                    "Свяжитесь для переноса.")
            except Exception:
                pass
        else:
            await q.edit_message_text("Запись не найдена или уже отменена.",
                                      reply_markup=kb_admin_menu())
        return ST_ADM_MENU

    return ST_ADM_MENU


# ─── Текстовые обработчики состояний администратора ──────────────────────────

@admin_only
async def adm_broadcast_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uids = get_all_users()
    sent, failed = 0, 0
    for uid in uids:
        try:
            await ctx.bot.send_message(uid, f"📢 Сообщение от мастера:\n\n{text}")
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(
        f"✅ Рассылка завершена: отправлено {sent}, ошибок {failed}.",
        reply_markup=kb_admin_menu())
    return ST_ADM_MENU


@admin_only
async def adm_search_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    results = search_clients(update.message.text.strip())
    if not results:
        await update.message.reply_text("🔍 Ничего не найдено.", reply_markup=kb_admin_menu())
        return ST_ADM_MENU
    lines = ["🔍 Результаты:\n"]
    for r in results:
        lines.append(f"👤 {r['name']}  📞 {r['phone']}  ID: <code>{r['user_id']}</code>")
    await update.message.reply_text("\n".join(lines), parse_mode="HTML",
                                    reply_markup=kb_admin_menu())
    return ST_ADM_MENU


@admin_only
async def adm_workhours_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = update.message.text.strip().split()
    try:
        ws, we, sm = int(parts[0]), int(parts[1]), int(parts[2])
        assert 0 <= ws < we <= 24 and 15 <= sm <= 240
    except Exception:
        await update.message.reply_text(
            "❌ Неверный формат. Пример: <code>9 19 45</code>", parse_mode="HTML")
        return ST_ADM_WORKHOURS
    set_setting("work_start", ws)
    set_setting("work_end",   we)
    set_setting("slot_minutes", sm)
    await update.message.reply_text(
        f"✅ Обновлено: {ws}:00 — {we}:00, слот {sm} мин.",
        reply_markup=kb_admin_menu())
    return ST_ADM_MENU


@admin_only
async def adm_ban_input(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        uid = int(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Введите числовой Telegram ID.")
        return ST_ADM_BAN
    result = toggle_ban(uid)
    emoji = "🚷" if result == "banned" else "✅"
    text  = "заблокирован" if result == "banned" else "разблокирован"
    await update.message.reply_text(f"{emoji} Пользователь {uid} — {text}.",
                                    reply_markup=kb_admin_menu())
    return ST_ADM_MENU


# ─────────────────────────────────────────────────────────────────────────────
# JOB: НАПОМИНАНИЯ
# ─────────────────────────────────────────────────────────────────────────────
async def send_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    for hours_ahead, label in [(24, "завтра"), (2, "через 2 часа")]:
        target = now + timedelta(hours=hours_ahead)
        t_date = target.strftime("%Y-%m-%d")
        t_time = target.strftime("%H:%M")
        with db_lock, sqlite3.connect(DB_FILE) as conn:
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM appointments "
                      "WHERE date=? AND time=? AND status='confirmed'", (t_date, t_time))
            apts = [dict(r) for r in c.fetchall()]
        for a in apts:
            try:
                await ctx.bot.send_message(
                    a["user_id"],
                    f"⏰ *Напоминание!*\n\n"
                    f"Ваша запись *{label}*:\n"
                    f"📅 {fmt_date(a['date'])} в {a['time']}\n"
                    f"💆 {a['procedure']}\n"
                    f"📍 {MASTER_NAME} · {MASTER_PHONE}",
                    parse_mode="Markdown")
            except Exception as e:
                log.warning("Напоминание не отправлено %s: %s", a["user_id"], e)


# ─────────────────────────────────────────────────────────────────────────────
# ЗАПУСК
# ─────────────────────────────────────────────────────────────────────────────

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    # ─── Клиентский диалог ───────────────────────────────────────────────────
    # Важно: в ST_CLIENT_MENU НЕ используем голый CallbackQueryHandler без паттерна
    # чтобы не перехватывать чужие колбэки. Все паттерны прописаны явно.
    client_conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={
            ST_CLIENT_MENU: [
                CallbackQueryHandler(client_callback, pattern="^back_main$"),
                CallbackQueryHandler(client_callback, pattern="^about$"),
                CallbackQueryHandler(client_callback, pattern="^my_apts$"),
                CallbackQueryHandler(client_callback, pattern="^cancel_apt_"),
                CallbackQueryHandler(client_callback, pattern="^book$"),
                CallbackQueryHandler(client_callback, pattern="^repeat_proc$"),
            ],
            ST_CHOOSE_PROC: [
                CallbackQueryHandler(client_callback, pattern="^proc_"),
                CallbackQueryHandler(client_callback, pattern="^back_main$"),
            ],
            ST_CHOOSE_DATE: [
                CallbackQueryHandler(client_callback, pattern="^cdate_"),
                CallbackQueryHandler(client_callback, pattern="^back_main$"),
                CallbackQueryHandler(client_callback, pattern="^book$"),
            ],
            ST_CHOOSE_TIME: [
                CallbackQueryHandler(client_callback, pattern="^ctime_"),
                CallbackQueryHandler(client_callback, pattern="^book$"),
            ],
            ST_ENTER_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_name),
            ],
            ST_ENTER_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, enter_phone),
            ],
            ST_CONFIRM_BOOK: [
                CallbackQueryHandler(confirm_book, pattern="^confirm_"),
            ],
        },
        fallbacks=[CommandHandler("start", cmd_start)],
        per_message=False,
        # Сохраняем состояние между перезапусками через persistent=False (по умолчанию)
        # allow_reentry позволяет /start в любой момент перезапустить диалог
        allow_reentry=True,
    )

    # ─── Административный диалог ─────────────────────────────────────────────
    admin_conv = ConversationHandler(
        entry_points=[CommandHandler("admin", cmd_admin)],
        states={
            ST_ADM_MENU: [
                CallbackQueryHandler(adm_callback, pattern="^adm_"),
                CallbackQueryHandler(adm_callback, pattern="^adate_"),
                CallbackQueryHandler(adm_callback, pattern="^blk_"),
                CallbackQueryHandler(adm_callback, pattern="^acncl_"),
                CallbackQueryHandler(adm_callback, pattern="^stats_"),
                CallbackQueryHandler(adm_callback, pattern="^adm_back$"),
            ],
            ST_ADM_CHOOSE_DATE: [
                CallbackQueryHandler(adm_callback, pattern="^adate_"),
                CallbackQueryHandler(adm_callback, pattern="^adm_back$"),
                CallbackQueryHandler(adm_callback, pattern="^adm_"),
            ],
            ST_ADM_CHOOSE_SLOT: [
                CallbackQueryHandler(adm_callback, pattern="^blk_"),
                CallbackQueryHandler(adm_callback, pattern="^adm_back$"),
            ],
            ST_ADM_CANCEL_APT: [
                CallbackQueryHandler(adm_callback, pattern="^acncl_"),
                CallbackQueryHandler(adm_callback, pattern="^adm_back$"),
            ],
            ST_ADM_STATS: [
                CallbackQueryHandler(adm_callback, pattern="^stats_"),
                CallbackQueryHandler(adm_callback, pattern="^adm_stats$"),
                CallbackQueryHandler(adm_callback, pattern="^adm_back$"),
            ],
            ST_ADM_BROADCAST: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_broadcast_input),
            ],
            ST_ADM_SEARCH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_search_input),
            ],
            ST_ADM_WORKHOURS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_workhours_input),
            ],
            ST_ADM_BAN: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, adm_ban_input),
            ],
        },
        fallbacks=[CommandHandler("admin", cmd_admin)],
        per_message=False,
        allow_reentry=True,
    )

    app.add_handler(client_conv)
    app.add_handler(admin_conv)

    # ── Напоминания ──────────────────────────────────────────────────────────
    jq = app.job_queue
    if jq:
        jq.run_repeating(send_reminders, interval=1800, first=60)
        log.info("Напоминания активны.")
    else:
        log.warning("JobQueue недоступен. pip install \"python-telegram-bot[job-queue]\"")

    log.info("Бот запущен.")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
