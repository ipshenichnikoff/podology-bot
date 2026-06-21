#!/usr/bin/env python3
"""
PODOLOG BOT — стабильная версия.
Установка: pip install "python-telegram-bot[job-queue]"
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove,
    Update, WebAppInfo,
)
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ContextTypes, MessageHandler, PicklePersistence, filters,
)

# ═══════════════════════════════════════════════════════════════════════════════
# НАСТРОЙКИ
# ═══════════════════════════════════════════════════════════════════════════════

BOT_TOKEN     = os.getenv("BOT_TOKEN", "СЮДА_ТОКЕН")
ADMIN_ID      = int(os.getenv("ADMIN_ID", "223326752"))

# URL Mini App на GitHub Pages (оставьте пустым если не используете)
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://ipshenichnikoff.github.io/podology-bot/webapp/index.html")
BOT_HOST   = os.getenv("BOT_HOST",   "https://bot-1781876805-1609-igorpshenichnikov.bothost.tech")

MASTER_NAME   = "Екатерина Шлейфер"
MASTER_PHONE  = "8 (920) 649 26-16"
MASTER_TG     = "@master"
MASTER_SINCE  = "2018"
MASTER_RATING = "4.9"
MASTER_ABOUT  = (
    "Профессиональный подолог с 2018 года.\n"
    "Более 200 довольных клиентов.\n"
    "Работаю аккуратно, без боли и стресса. 🌸"
)

MOSCOW_TZ  = ZoneInfo("Europe/Moscow")
WORK_DAYS  = [0, 1, 2, 3, 4, 5]
WORK_START = 10
WORK_END   = 20
SLOT_MIN   = 60
DAYS_AHEAD = 14
MAX_ACTIVE = 2
DB_FILE    = "podolog.db"

RATE_LIMIT_COUNT  = 5
RATE_LIMIT_WINDOW = 10

# Формат: (название, длительность мин, цена_от, цена_до, описание)
# Если цена фиксированная — цена_от == цена_до
PROCEDURES = [
    ("Педикюр", 60, 2500, 3500,
     "Дезинфекция и очищение стоп, аппаратная обработка стоп: удаление огрубевшей кожи, мозолей, натоптышей. "
     "Ручная обработка кутикулы и придание формы ногтям, расслабляющий массаж с питательным кремом. "
     "Результат — ухоженные стопы и ногти на 3–4 недели."),
    ("Удаление мозоли", 30, 1200, 2200,
     "Осмотр и определение типа мозоли. Аккуратное удаление уплотнения с минимальным воздействием на здоровые ткани. "
     "Антисептическая обработка. Результат — гладкая кожа и комфорт при ходьбе."),
    ("Удаление вросшего ногтя", 45, 1800, 3500,
     "Диагностика состояния ногтевой пластины. Аккуратное удаление вросшей части ногтя. "
     "Наложение стерильной повязки. Результат — устранение боли и воспаления."),
    ("Протезирование", 30, 1500, 3500,
     "Осмотр и оценка состояния ногтевого ложа. Подбор материала и формы протеза. "
     "Фиксация протеза, коррекция формы и полировка. Результат — естественный вид ногтя."),
    ("Титановая нить", 40, 3000, 6500,
     "Осмотр и оценка состояния ногтя. Подбор толщины и натяжения нити. "
     "Фиксация нити на ногтевой пластине. Результат — здоровый рост ногтя без хирургии."),
    ("ЗТО коррекционная система", 60, 4000, 6000,
     "Осмотр и оценка состояния ногтя. Подготовка ногтевой пластины. "
     "Установка и фиксация системы ЗТО. Результат — здоровый рост ногтя без хирургии."),
    ("Унибрейс", 60, 4500, 6500,
     "Диагностика врастания, индивидуальный подбор скобы, атравматичная установка конструкции. "
     "Подходит для взрослых и детей при начальных и умеренных стадиях врастания."),
    ("Обработка трещин", 50, 1700, 3000,
     "Диагностика состояния кожи, аппаратное удаление гиперкератоза, антисептическая обработка трещин, "
     "нанесение заживляющих составов. Подходит для глубоких и застарелых трещин."),
    ("Зачистка ногтевой пластины", 30, 500, 2000,
     "Осмотр и оценка состояния ногтя. Зачистка поражённой части ногтя до полного его отрастания."),
]

# Состояния
ST_MAIN            = "main"
ST_PROC            = "proc"
ST_DATE            = "date"
ST_TIME            = "time"
ST_PHONE           = "phone"
ST_CONFIRM         = "confirm"
ST_RESCHEDULE_DATE = "reschedule_date"
ST_RESCHEDULE_TIME = "reschedule_time"
ST_ADM             = "adm"
ST_ADM_ADD_NAME    = "adm_add_name"
ST_ADM_ADD_PHONE   = "adm_add_phone"
ST_ADM_BROADCAST   = "adm_broadcast"

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
# HTTP СЕРВЕР ДЛЯ MINI APP
# ═══════════════════════════════════════════════════════════════════════════════

# Глобальная ссылка на event loop бота — нужна чтобы запускать async-код
# из синхронных обработчиков FastAPI (они работают в отдельном потоке).
_bot_loop = None
_bot_app  = None

def start_http_server():
    """Запускает FastAPI сервер — bothost использует его как обёртку."""
    try:
        from fastapi import FastAPI, Request
        from fastapi.responses import JSONResponse, PlainTextResponse
        from fastapi.middleware.cors import CORSMiddleware
        import uvicorn
        import asyncio

        app_api = FastAPI()
        app_api.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        @app_api.get("/")
        @app_api.get("/health")
        def health():
            return PlainTextResponse("OK")

        @app_api.get("/webapp-data")
        def webapp_data():
            return JSONResponse(get_webapp_data())

        def _render_webapp_page():
            from fastapi.responses import HTMLResponse
            data = get_webapp_data()
            html = render_webapp_html(data)
            return HTMLResponse(
                html,
                headers={
                    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                    "Pragma": "no-cache",
                    "Expires": "0",
                },
            )

        @app_api.get("/webapp-page")
        def webapp_page():
            return _render_webapp_page()

        @app_api.get("/webapp-page/{cache_key}")
        def webapp_page_with_key(cache_key: str):
            return _render_webapp_page()

        @app_api.post("/webapp-book")
        async def webapp_book(request: Request):
            """
            Принимает заявку напрямую от Mini App через fetch (вместо tg.sendData).
            Это надёжнее на мобильных платформах где sendData может работать нестабильно.
            """
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"ok": False, "error": "bad json"}, status_code=400)

            uid       = body.get("uid")
            proc_idx  = body.get("proc_idx", 0)
            proc_name = body.get("proc_name", "")
            date      = body.get("date", "")
            time      = body.get("time", "")
            pf        = body.get("proc_price_raw", 0)
            dur       = body.get("proc_dur", 60)
            name      = body.get("user_name", "Клиент")

            if not uid or not date or not time or not proc_name:
                return JSONResponse({"ok": False, "error": "missing fields"}, status_code=400)

            uid = int(uid)

            # Запускаем обработку в event loop бота
            if _bot_loop and _bot_app:
                future = asyncio.run_coroutine_threadsafe(
                    _process_webapp_booking(uid, proc_idx, proc_name, date, time, pf, dur, name),
                    _bot_loop
                )
                try:
                    result = future.result(timeout=10)
                    return JSONResponse(result)
                except Exception as e:
                    log.error("Ошибка обработки веб-заявки: %s", e)
                    return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
            else:
                return JSONResponse({"ok": False, "error": "bot not ready"}, status_code=503)

        port = int(os.getenv("PORT", "3000"))
        registered_paths = [r.path for r in app_api.routes]
        log.info("FastAPI маршруты зарегистрированы: %s", registered_paths)
        log.info("FastAPI сервер запущен на порту %d", port)
        uvicorn.run(app_api, host="0.0.0.0", port=port, log_level="warning")
    except ImportError:
        # Fallback на встроенный HTTP сервер если нет FastAPI
        from http.server import HTTPServer, BaseHTTPRequestHandler
        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path in ("/webapp-data",):
                    body = json.dumps(get_webapp_data(), ensure_ascii=False).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Access-Control-Allow-Origin", "*")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"OK")
            def log_message(self, *a): pass
        port = int(os.getenv("PORT", "3000"))
        HTTPServer(("0.0.0.0", port), H).serve_forever()

# ═══════════════════════════════════════════════════════════════════════════════
# АНТИСПАМ
# ═══════════════════════════════════════════════════════════════════════════════

_rate_data: dict = defaultdict(list)
_rate_lock = threading.Lock()

def is_rate_limited(uid: int) -> bool:
    now = time.time()
    with _rate_lock:
        _rate_data[uid] = [t for t in _rate_data[uid] if now - t < RATE_LIMIT_WINDOW]
        if len(_rate_data[uid]) >= RATE_LIMIT_COUNT:
            return True
        _rate_data[uid].append(now)
        return False

# ═══════════════════════════════════════════════════════════════════════════════
# БАЗА ДАННЫХ
# ═══════════════════════════════════════════════════════════════════════════════

def _db():
    c = sqlite3.connect(DB_FILE, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    with _lock, _db() as c:
        c.executescript("""
            CREATE TABLE IF NOT EXISTS appointments (
                id TEXT PRIMARY KEY, date TEXT, time TEXT,
                name TEXT, phone TEXT, user_id INTEGER,
                procedure TEXT, duration INTEGER, price INTEGER,
                status TEXT DEFAULT 'pending', created TEXT
            );
            CREATE TABLE IF NOT EXISTS blocked (
                date TEXT, time TEXT, PRIMARY KEY(date,time)
            );
            CREATE TABLE IF NOT EXISTS dayoff (
                date TEXT PRIMARY KEY, reason TEXT DEFAULT 'Выходной'
            );
            CREATE TABLE IF NOT EXISTS clients (
                user_id INTEGER PRIMARY KEY,
                name TEXT, phone TEXT, last_proc TEXT,
                visits INTEGER DEFAULT 0, is_banned INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS reviews (
                apt_id TEXT PRIMARY KEY, user_id INTEGER,
                rating INTEGER, comment TEXT, created TEXT
            );
            CREATE TABLE IF NOT EXISTS reminders (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                apt_id    TEXT NOT NULL,
                user_id   INTEGER NOT NULL,
                fire_time TEXT NOT NULL,
                type      TEXT NOT NULL,
                label     TEXT DEFAULT '',
                sent      INTEGER DEFAULT 0
            );
        """)
        for table, col, defn in [
            ("appointments", "duration",  "INTEGER DEFAULT 60"),
            ("clients",      "is_banned", "INTEGER DEFAULT 0"),
        ]:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
            except Exception:
                pass

# ── Вспомогательные ──────────────────────────────────────────────────────────

def today():
    return datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d")

def fmt_date(d):
    dt = datetime.strptime(d, "%Y-%m-%d")
    return f"{WEEKDAYS[dt.weekday()]}, {dt.day} {MONTHS[dt.month]}"

def fmt_price(pf, pt):
    if pf == pt:
        return f"{pf:,} ₽".replace(",", " ")
    return f"от {pf:,} ₽".replace(",", " ")

def validate_phone(p):
    return 7 <= len(re.sub(r"\D", "", p)) <= 15

def set_state(ctx, state):
    ctx.user_data["state"] = state

def get_state(ctx):
    return ctx.user_data.get("state", ST_MAIN)

# ── Клиенты ──────────────────────────────────────────────────────────────────

def save_client(uid, name, phone):
    with _lock, _db() as c:
        c.execute("""
            INSERT INTO clients(user_id,name,phone,visits) VALUES(?,?,?,1)
            ON CONFLICT(user_id) DO UPDATE SET name=?,phone=?,visits=visits+1
        """, (uid, name, phone, name, phone))

def get_client(uid):
    with _lock, _db() as c:
        row = c.execute("SELECT * FROM clients WHERE user_id=?", (uid,)).fetchone()
    return dict(row) if row else None

def set_last_proc(uid, proc):
    with _lock, _db() as c:
        c.execute("""
            INSERT INTO clients(user_id,last_proc) VALUES(?,?)
            ON CONFLICT(user_id) DO UPDATE SET last_proc=?
        """, (uid, proc, proc))

def is_banned(uid):
    with _lock, _db() as c:
        row = c.execute("SELECT is_banned FROM clients WHERE user_id=?", (uid,)).fetchone()
    return bool(row and row["is_banned"])

def toggle_ban(uid):
    with _lock, _db() as c:
        row = c.execute("SELECT is_banned FROM clients WHERE user_id=?", (uid,)).fetchone()
        new_val = 0 if (row and row["is_banned"]) else 1
        c.execute("""
            INSERT INTO clients(user_id,is_banned) VALUES(?,?)
            ON CONFLICT(user_id) DO UPDATE SET is_banned=?
        """, (uid, new_val, new_val))
    return "banned" if new_val else "unbanned"

def get_all_client_ids():
    with _lock, _db() as c:
        rows = c.execute("SELECT user_id FROM clients WHERE is_banned=0").fetchall()
    return [r["user_id"] for r in rows]

# ── Отзывы ───────────────────────────────────────────────────────────────────

def save_review(apt_id, uid, rating):
    with _lock, _db() as c:
        c.execute("""
            INSERT OR REPLACE INTO reviews(apt_id,user_id,rating,created)
            VALUES(?,?,?,?)
        """, (apt_id, uid, rating, datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M")))

def get_reviews_stats():
    with _lock, _db() as c:
        row    = c.execute("SELECT COUNT(*) cnt, ROUND(AVG(rating),1) avg FROM reviews").fetchone()
        recent = c.execute(
            "SELECT rating, created FROM reviews ORDER BY created DESC LIMIT 5"
        ).fetchall()
    return dict(row), [dict(r) for r in recent]

# ── Слоты и расписание ───────────────────────────────────────────────────────

def is_dayoff(date):
    with _lock, _db() as c:
        return c.execute("SELECT 1 FROM dayoff WHERE date=?", (date,)).fetchone() is not None

def toggle_dayoff(date):
    with _lock, _db() as c:
        if c.execute("SELECT 1 FROM dayoff WHERE date=?", (date,)).fetchone():
            c.execute("DELETE FROM dayoff WHERE date=?", (date,))
            return "removed"
        c.execute("INSERT INTO dayoff(date) VALUES(?)", (date,))
        return "added"

def _booked(date):
    with _lock, _db() as c:
        return {r["time"] for r in c.execute(
            "SELECT time FROM appointments WHERE date=? AND status NOT IN ('cancelled','rejected')",
            (date,)
        )}

def _blocked(date):
    with _lock, _db() as c:
        return {r["time"] for r in c.execute("SELECT time FROM blocked WHERE date=?", (date,))}

def free_slots(date):
    if is_dayoff(date):
        return []
    taken = _booked(date) | _blocked(date)
    slots, t = [], WORK_START * 60
    while t + SLOT_MIN <= WORK_END * 60:
        hh, mm = divmod(t, 60)
        s = f"{hh:02d}:{mm:02d}"
        if s not in taken:
            slots.append(s)
        t += SLOT_MIN
    return slots

def free_slots_count(date):
    return len(free_slots(date))

def all_slots_status(date):
    b, bl = _booked(date), _blocked(date)
    result, t = [], WORK_START * 60
    while t + SLOT_MIN <= WORK_END * 60:
        hh, mm = divmod(t, 60)
        s  = f"{hh:02d}:{mm:02d}"
        st = "booked" if s in b else ("blocked" if s in bl else "free")
        result.append((s, st))
        t += SLOT_MIN
    return result

def available_dates():
    now = datetime.now(MOSCOW_TZ)
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

def all_blocked_slots():
    with _lock, _db() as c:
        rows = c.execute(
            "SELECT date,time FROM blocked WHERE date>=? ORDER BY date,time", (today(),)
        ).fetchall()
    return [dict(r) for r in rows]

def block_slot(date, time):
    with _lock, _db() as c:
        c.execute("INSERT OR IGNORE INTO blocked(date,time) VALUES(?,?)", (date, time))

def unblock_slot(date, time):
    with _lock, _db() as c:
        c.execute("DELETE FROM blocked WHERE date=? AND time=?", (date, time))

# ── Записи ───────────────────────────────────────────────────────────────────

def add_apt(date, time, name, phone, uid, proc, dur, price):
    apt_id = f"{date}_{time.replace(':','')}_{uid}"
    with _lock, _db() as c:
        if c.execute(
            "SELECT 1 FROM appointments WHERE date=? AND time=? AND status NOT IN ('cancelled','rejected')",
            (date, time)
        ).fetchone():
            raise ValueError("Этот слот уже занят — выберите другое время.")
        c.execute(
            "INSERT OR REPLACE INTO appointments "
            "(id,date,time,name,phone,user_id,procedure,duration,price,status,created) "
            "VALUES(?,?,?,?,?,?,?,?,?,'pending',?)",
            (apt_id, date, time, name, phone, uid, proc, dur, price,
             datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M"))
        )
    save_client(uid, name, phone)
    set_last_proc(uid, proc)
    return apt_id

def confirm_apt_by_admin(apt_id):
    with _lock, _db() as c:
        row = c.execute(
            "SELECT * FROM appointments WHERE id=? AND status='pending'", (apt_id,)
        ).fetchone()
        if row:
            c.execute("UPDATE appointments SET status='confirmed' WHERE id=?", (apt_id,))
            return dict(row)
    return None

def reject_apt_by_admin(apt_id):
    with _lock, _db() as c:
        row = c.execute(
            "SELECT * FROM appointments WHERE id=? AND status='pending'", (apt_id,)
        ).fetchone()
        if row:
            c.execute("UPDATE appointments SET status='rejected' WHERE id=?", (apt_id,))
            return dict(row)
    return None

def cancel_apt(apt_id):
    with _lock, _db() as c:
        row = c.execute(
            "SELECT * FROM appointments WHERE id=? AND status IN ('confirmed','pending')",
            (apt_id,)
        ).fetchone()
        if row:
            c.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (apt_id,))
            return dict(row)
    return None

def reschedule_apt(apt_id, new_date, new_time):
    with _lock, _db() as c:
        row = c.execute(
            "SELECT * FROM appointments WHERE id=? AND status IN ('confirmed','pending')",
            (apt_id,)
        ).fetchone()
        if not row:
            return None, None
        apt = dict(row)
        if c.execute(
            "SELECT 1 FROM appointments WHERE date=? AND time=? "
            "AND status NOT IN ('cancelled','rejected') AND id!=?",
            (new_date, new_time, apt_id)
        ).fetchone():
            raise ValueError("Этот слот уже занят.")
        new_id = f"{new_date}_{new_time.replace(':','')}_{apt['user_id']}"
        c.execute("UPDATE appointments SET status='cancelled' WHERE id=?", (apt_id,))
        c.execute(
            "INSERT OR REPLACE INTO appointments "
            "(id,date,time,name,phone,user_id,procedure,duration,price,status,created) "
            "VALUES(?,?,?,?,?,?,?,?,?,'pending',?)",
            (new_id, new_date, new_time, apt["name"], apt["phone"],
             apt["user_id"], apt["procedure"], apt["duration"], apt["price"],
             datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M"))
        )
    return apt, new_id

def get_apt(apt_id):
    with _lock, _db() as c:
        row = c.execute("SELECT * FROM appointments WHERE id=?", (apt_id,)).fetchone()
    return dict(row) if row else None

def user_apts(uid):
    with _lock, _db() as c:
        rows = c.execute(
            "SELECT * FROM appointments WHERE user_id=? "
            "AND status IN ('confirmed','pending') AND date>=? ORDER BY date,time",
            (uid, today())
        ).fetchall()
    return [dict(r) for r in rows]

def count_active(uid):
    with _lock, _db() as c:
        return c.execute(
            "SELECT COUNT(*) FROM appointments WHERE user_id=? "
            "AND status IN ('confirmed','pending') AND date>=?",
            (uid, today())
        ).fetchone()[0]

def day_apts(date):
    with _lock, _db() as c:
        rows = c.execute(
            "SELECT * FROM appointments WHERE date=? "
            "AND status IN ('confirmed','pending') ORDER BY time",
            (date,)
        ).fetchall()
    return [dict(r) for r in rows]

def get_stats():
    now         = datetime.now(MOSCOW_TZ)
    today_str   = today()
    month_start = now.replace(day=1).strftime("%Y-%m-%d")
    week_start  = (now - timedelta(days=now.weekday())).strftime("%Y-%m-%d")
    with _lock, _db() as c:
        def rev(start):
            row = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(price),0) FROM appointments "
                "WHERE status IN ('confirmed','pending') AND date BETWEEN ? AND ?",
                (start, today_str)
            ).fetchone()
            return row[0], row[1]
        tc, tr = rev(today_str)
        wc, wr = rev(week_start)
        mc, mr = rev(month_start)
        top = c.execute(
            "SELECT procedure, COUNT(*) cnt FROM appointments "
            "WHERE status IN ('confirmed','pending') "
            "GROUP BY procedure ORDER BY cnt DESC LIMIT 3"
        ).fetchall()
        total_clients = c.execute("SELECT COUNT(*) FROM clients").fetchone()[0]
        avg_rating    = c.execute("SELECT ROUND(AVG(rating),1) FROM reviews").fetchone()[0]
    return dict(
        today_count=tc, today_rev=tr,
        week_count=wc,  week_rev=wr,
        month_count=mc, month_rev=mr,
        top=top, total_clients=total_clients,
        avg_rating=avg_rating or "—"
    )

def export_week():
    now   = datetime.now(MOSCOW_TZ)
    lines = [f"📋 Расписание на 7 дней ({now.strftime('%d.%m.%Y')})\n"]
    total_rev = 0
    for i in range(7):
        d    = (now + timedelta(days=i)).strftime("%Y-%m-%d")
        apts = day_apts(d)
        free = free_slots_count(d)
        off  = is_dayoff(d)
        lines.append(f"\n{'🚫 ' if off else ''}📅 {fmt_date(d)}")
        if apts:
            for a in apts:
                icon = "✅" if a["status"] == "confirmed" else "⏳"
                lines.append(
                    f"  {icon} {a['time']} {a['name']} — "
                    f"{a['procedure']} ({a['price']:,} ₽)".replace(",", " ")
                )
                total_rev += a["price"]
        else:
            lines.append("  _(пусто)_")
        lines.append(f"  🟢 Свободно: {free}")
    lines.append(f"\n💰 Итого за неделю: {total_rev:,} ₽".replace(",", " "))
    return "\n".join(lines)

# ═══════════════════════════════════════════════════════════════════════════════
# НАПОМИНАНИЯ
# ═══════════════════════════════════════════════════════════════════════════════

def schedule_reminders(app, apt_id, uid, date, time):
    """Сохраняет напоминания в БД — работает даже после перезапуска бота."""
    try:
        naive_dt = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M")
        apt_dt   = naive_dt.replace(tzinfo=MOSCOW_TZ)
        now_msk  = datetime.now(MOSCOW_TZ)
        with _lock, _db() as c:
            for hours, label in [(24, "завтра"), (2, "через 2 часа")]:
                fire = apt_dt - timedelta(hours=hours)
                if fire > now_msk:
                    fire_str = fire.strftime("%Y-%m-%d %H:%M")
                    c.execute(
                        "INSERT OR IGNORE INTO reminders(apt_id,user_id,fire_time,type,label) "
                        "VALUES(?,?,?,'reminder',?)",
                        (apt_id, uid, fire_str, label)
                    )
            review_fire = apt_dt + timedelta(hours=24)
            if review_fire > now_msk:
                fire_str = review_fire.strftime("%Y-%m-%d %H:%M")
                c.execute(
                    "INSERT OR IGNORE INTO reminders(apt_id,user_id,fire_time,type,label) "
                    "VALUES(?,?,?,'review','')",
                    (apt_id, uid, fire_str)
                )
        log.info("Напоминания запланированы для записи %s", apt_id)
    except Exception as e:
        log.warning("Ошибка планирования напоминания: %s", e)

def cancel_reminders(app, apt_id):
    """Отменяет напоминания — помечает как отправленные в БД."""
    with _lock, _db() as c:
        c.execute("UPDATE reminders SET sent=1 WHERE apt_id=?", (apt_id,))

async def _process_reminders(ctx: ContextTypes.DEFAULT_TYPE):
    """Запускается каждую минуту. Отправляет напоминания из БД."""
    now_str = datetime.now(MOSCOW_TZ).strftime("%Y-%m-%d %H:%M")
    with _lock, _db() as c:
        rows = c.execute(
            "SELECT * FROM reminders WHERE sent=0 AND fire_time<=?", (now_str,)
        ).fetchall()
    
    for row in rows:
        r = dict(row)
        try:
            apt = get_apt(r["apt_id"])
            if not apt or apt["status"] not in ("confirmed", "pending"):
                # Запись отменена — просто помечаем как отправленное
                with _lock, _db() as c:
                    c.execute("UPDATE reminders SET sent=1 WHERE id=?", (r["id"],))
                continue

            if r["type"] == "reminder":
                await ctx.bot.send_message(
                    r["user_id"],
                    f"🔔 *Напоминание о визите!*\n\n"
                    f"Вы записаны *{r['label']}*:\n"
                    f"📅 {fmt_date(apt['date'])} в {apt['time']}\n"
                    f"💆 {apt['procedure']}\n\n"
                    f"Вопросы: {MASTER_PHONE}",
                    parse_mode="Markdown",
                )
            elif r["type"] == "review":
                if apt["status"] == "confirmed":
                    await ctx.bot.send_message(
                        r["user_id"],
                        f"🌸 Как прошёл визит?\n\n"
                        f"💆 {apt['procedure']} · {fmt_date(apt['date'])}\n\n"
                        "Поставьте оценку:",
                        reply_markup=InlineKeyboardMarkup([[
                            InlineKeyboardButton("⭐ 1", callback_data=f"rev_{apt['id']}_1"),
                            InlineKeyboardButton("⭐ 2", callback_data=f"rev_{apt['id']}_2"),
                            InlineKeyboardButton("⭐ 3", callback_data=f"rev_{apt['id']}_3"),
                            InlineKeyboardButton("⭐ 4", callback_data=f"rev_{apt['id']}_4"),
                            InlineKeyboardButton("⭐ 5", callback_data=f"rev_{apt['id']}_5"),
                        ]]),
                    )

            # Помечаем как отправленное
            with _lock, _db() as c:
                c.execute("UPDATE reminders SET sent=1 WHERE id=?", (r["id"],))
            log.info("Напоминание отправлено: %s type=%s", r["apt_id"], r["type"])

        except Exception as e:
            log.warning("Ошибка отправки напоминания id=%s: %s", r["id"], e)

# ═══════════════════════════════════════════════════════════════════════════════
# КЛАВИАТУРЫ
# ═══════════════════════════════════════════════════════════════════════════════

def render_webapp_html(data: dict) -> str:
    """
    Генерирует полностью самодостаточную HTML-страницу Mini App
    с данными, встроенными прямо в JS (без отдельного fetch-запроса).
    """
    import json as _json
    data_json = _json.dumps(data, ensure_ascii=False)

    return """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0">
<title>Запись к подологу</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  :root {
    --bg: #f5f5f5; --card: #ffffff;
    --primary: #e91e8c; --primary-light: #fce4f3;
    --text: #1a1a1a; --text-muted: #888;
    --border: #eee; --radius: 16px;
  }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
    background: var(--bg); color: var(--text);
    min-height: 100vh; padding-bottom: 30px;
  }
  .header {
    background: linear-gradient(135deg, #e91e8c, #c2185b);
    color: white; padding: 20px 16px 24px; text-align: center;
  }
  .header h1 { font-size: 20px; font-weight: 700; }
  .header p  { font-size: 13px; opacity: 0.85; margin-top: 4px; }
  .steps {
    display: flex; justify-content: center; gap: 8px;
    padding: 16px; background: white; border-bottom: 1px solid var(--border);
  }
  .step {
    width: 32px; height: 32px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-size: 13px; font-weight: 600;
    background: var(--border); color: var(--text-muted); transition: all .3s;
  }
  .step.active { background: var(--primary); color: white; }
  .step.done   { background: #4caf50; color: white; }
  .step-line   { flex: 1; height: 2px; background: var(--border); align-self: center; max-width: 40px; }
  .screen { display: none; padding: 16px; }
  .screen.active { display: block; }
  .section-title { font-size: 16px; font-weight: 700; margin-bottom: 12px; }
  .proc-card {
    background: var(--card); border-radius: var(--radius);
    padding: 14px 16px; margin-bottom: 10px;
    border: 2px solid transparent; cursor: pointer; display: flex; align-items: center; gap: 12px;
  }
  .proc-card.selected { border-color: var(--primary); background: var(--primary-light); }
  .proc-icon {
    width: 44px; height: 44px; border-radius: 12px; background: var(--primary-light);
    display: flex; align-items: center; justify-content: center; font-size: 22px; flex-shrink: 0;
  }
  .proc-info { flex: 1; }
  .proc-name { font-size: 15px; font-weight: 600; }
  .proc-meta { font-size: 12px; color: var(--text-muted); margin-top: 2px; }
  .proc-price { font-size: 14px; font-weight: 700; color: var(--primary); white-space: nowrap; }
  .dates-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 8px; margin-bottom: 16px; }
  .date-btn {
    background: var(--card); border: 2px solid transparent;
    border-radius: 12px; padding: 10px 4px; text-align: center; cursor: pointer;
  }
  .date-btn.selected { border-color: var(--primary); background: var(--primary-light); }
  .date-btn .day-name { font-size: 11px; color: var(--text-muted); }
  .date-btn .day-num  { font-size: 18px; font-weight: 700; margin: 2px 0; }
  .date-btn .day-mon  { font-size: 11px; color: var(--text-muted); }
  .date-btn .dot { width: 6px; height: 6px; border-radius: 50%; margin: 4px auto 0; }
  .dot.green { background: #4caf50; } .dot.yellow { background: #ff9800; } .dot.red { background: #f44336; }
  .times-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .time-btn {
    background: var(--card); border: 2px solid transparent;
    border-radius: 12px; padding: 12px 8px; text-align: center;
    font-size: 16px; font-weight: 600; cursor: pointer;
  }
  .time-btn.selected { border-color: var(--primary); background: var(--primary-light); color: var(--primary); }
  .summary-card { background: var(--card); border-radius: var(--radius); padding: 20px; margin-bottom: 16px; }
  .summary-row { display: flex; align-items: flex-start; gap: 12px; padding: 10px 0; border-bottom: 1px solid var(--border); }
  .summary-row:last-child { border-bottom: none; }
  .summary-icon { font-size: 20px; width: 28px; text-align: center; flex-shrink: 0; }
  .summary-label { font-size: 12px; color: var(--text-muted); }
  .summary-value { font-size: 15px; font-weight: 600; margin-top: 2px; }
  .master-card { background: var(--card); border-radius: var(--radius); padding: 16px; display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
  .master-avatar { width: 52px; height: 52px; border-radius: 50%; background: linear-gradient(135deg, #e91e8c, #c2185b); display: flex; align-items: center; justify-content: center; font-size: 24px; flex-shrink: 0; }
  .master-name { font-size: 15px; font-weight: 700; }
  .master-meta { font-size: 12px; color: var(--text-muted); margin-top: 2px; }
  .success-screen { text-align: center; padding: 40px 20px; }
  .success-icon { font-size: 64px; margin-bottom: 16px; }
  .success-screen h2 { font-size: 22px; font-weight: 700; margin-bottom: 8px; }
  .success-screen p  { color: var(--text-muted); font-size: 14px; line-height: 1.5; }
  .empty { text-align: center; color: var(--text-muted); padding: 32px 0; font-size: 14px; }
</style>
</head>
<body>

<div class="header">
  <h1>💆 Запись к подологу</h1>
  <p id="masterInfo">""" + data.get("master_name", "") + """ · ⭐ """ + str(data.get("rating", "4.9")) + """</p>
</div>

<div class="steps">
  <div class="step active" id="step1">1</div>
  <div class="step-line"></div>
  <div class="step" id="step2">2</div>
  <div class="step-line"></div>
  <div class="step" id="step3">3</div>
  <div class="step-line"></div>
  <div class="step" id="step4">✓</div>
</div>

<div class="screen active" id="screen1">
  <div class="section-title">Выберите процедуру</div>
  <div id="procList"></div>
</div>

<div class="screen" id="screen2">
  <div class="section-title">Выберите дату</div>
  <div class="dates-grid" id="dateGrid"></div>
  <div class="section-title" id="timesTitle" style="display:none">Выберите время</div>
  <div class="times-grid" id="timesGrid"></div>
</div>

<div class="screen" id="screen3">
  <div class="section-title">Подтверждение записи</div>
  <div class="summary-card" id="summaryCard"></div>
  <div class="master-card">
    <div class="master-avatar">👩‍⚕️</div>
    <div>
      <div class="master-name">""" + data.get("master_name", "") + """</div>
      <div class="master-meta">""" + data.get("master_phone", "") + """</div>
    </div>
  </div>
</div>

<div class="screen" id="screen4">
  <div class="success-screen">
    <div class="success-icon">🎉</div>
    <h2>Заявка отправлена!</h2>
    <p>Мастер подтвердит запись в ближайшее время.<br>Вы получите уведомление в этом чате.</p>
  </div>
</div>

<script>
  // Данные встроены статически — никакого fetch при загрузке не требуется.
  // Это устраняет асинхронность, которая могла мешать tg.sendData() на iOS.
  var appData = """ + data_json + """;

  var tg = window.Telegram && window.Telegram.WebApp ? window.Telegram.WebApp : null;
  var tgUser = { first_name: 'Клиент', id: 0 };
  if (tg) {
    tg.ready();
    tg.expand();
    if (tg.initDataUnsafe && tg.initDataUnsafe.user) {
      tgUser = tg.initDataUnsafe.user;
    }
  }

  var state = { step: 1, proc: null, date: null, time: null };
  var ICONS    = ['🦶','🩺','💉','💅','🔧','⚙️','🛡️','🩹','🔬'];
  var MONTHS_S = ['янв','фев','мар','апр','май','июн','июл','авг','сен','окт','ноя','дек'];
  var DAYS_S   = ['Вс','Пн','Вт','Ср','Чт','Пт','Сб'];

  function renderProcs() {
    var list = document.getElementById('procList');
    var procs = appData.procedures || [];
    if (!procs.length) {
      list.innerHTML = '<div class="empty">Нет доступных услуг</div>';
      return;
    }
    var html = '';
    for (var i = 0; i < procs.length; i++) {
      var p = procs[i];
      html += '<div class="proc-card" data-idx="' + i + '">' +
        '<div class="proc-icon">' + ICONS[i % ICONS.length] + '</div>' +
        '<div class="proc-info"><div class="proc-name">' + p.name + '</div>' +
        '<div class="proc-meta">⏱ ' + p.dur + ' мин</div></div>' +
        '<div class="proc-price">' + p.price + '</div></div>';
    }
    list.innerHTML = html;
    var cards = list.querySelectorAll('.proc-card');
    for (var j = 0; j < cards.length; j++) {
      cards[j].addEventListener('click', function() {
        selectProc(parseInt(this.getAttribute('data-idx'), 10));
      });
    }
  }

  function selectProc(i) {
    state.proc = i;
    var cards = document.querySelectorAll('.proc-card');
    for (var j = 0; j < cards.length; j++) cards[j].classList.toggle('selected', i === j);
    setTimeout(function() { goToStep(2); }, 250);
  }

  function renderDates() {
    var dates = appData.dates || [];
    var grid  = document.getElementById('dateGrid');
    if (!dates.length) {
      grid.innerHTML = '<div class="empty" style="grid-column:1/-1">Свободных дат нет</div>';
      return;
    }
    var html = '';
    for (var i = 0; i < dates.length; i++) {
      var d   = dates[i];
      var dt  = new Date(d.date + 'T00:00:00');
      var dot = d.free >= d.total ? 'green' : (d.free > Math.floor(d.total/2) ? 'yellow' : 'red');
      html += '<div class="date-btn" data-date="' + d.date + '">' +
        '<div class="day-name">' + DAYS_S[dt.getDay()] + '</div>' +
        '<div class="day-num">' + dt.getDate() + '</div>' +
        '<div class="day-mon">' + MONTHS_S[dt.getMonth()] + '</div>' +
        '<div class="dot ' + dot + '"></div></div>';
    }
    grid.innerHTML = html;
    var btns = grid.querySelectorAll('.date-btn');
    for (var j = 0; j < btns.length; j++) {
      btns[j].addEventListener('click', function() {
        selectDate(this.getAttribute('data-date'));
      });
    }
  }

  function selectDate(date) {
    state.date = date; state.time = null;
    var btns = document.querySelectorAll('.date-btn');
    for (var i = 0; i < btns.length; i++)
      btns[i].classList.toggle('selected', btns[i].getAttribute('data-date') === date);
    var d = null, dates = appData.dates || [];
    for (var j = 0; j < dates.length; j++) if (dates[j].date === date) { d = dates[j]; break; }
    var slots = d ? d.slots : [];
    document.getElementById('timesTitle').style.display = slots.length ? 'block' : 'none';
    var timesGrid = document.getElementById('timesGrid');
    var html = '';
    for (var k = 0; k < slots.length; k++) {
      html += '<div class="time-btn" data-time="' + slots[k] + '">' + slots[k] + '</div>';
    }
    timesGrid.innerHTML = html;
    var tbtns = timesGrid.querySelectorAll('.time-btn');
    for (var m = 0; m < tbtns.length; m++) {
      tbtns[m].addEventListener('click', function() {
        selectTime(this.getAttribute('data-time'));
      });
    }
  }

  function selectTime(t) {
    state.time = t;
    var btns = document.querySelectorAll('.time-btn');
    for (var i = 0; i < btns.length; i++)
      btns[i].classList.toggle('selected', btns[i].getAttribute('data-time') === t);
    setTimeout(function() { goToStep(3); }, 250);
  }

  function renderSummary() {
    var p  = (appData.procedures || [])[state.proc] || {};
    var dt = new Date(state.date + 'T00:00:00');
    var dateStr = DAYS_S[dt.getDay()] + ', ' + dt.getDate() + ' ' + MONTHS_S[dt.getMonth()];
    document.getElementById('summaryCard').innerHTML =
      row('💆','Процедура', p.name || '—') +
      row('📅','Дата и время', dateStr + ' в ' + state.time) +
      row('⏱','Длительность', p.dur + ' мин') +
      row('💰','Стоимость', p.price) +
      row('👤','Клиент', tgUser.first_name || 'Клиент');
  }

  function row(icon, label, value) {
    return '<div class="summary-row"><div class="summary-icon">' + icon + '</div>' +
      '<div><div class="summary-label">' + label + '</div>' +
      '<div class="summary-value">' + value + '</div></div></div>';
  }

  function goToStep(n) {
    var screens = document.querySelectorAll('.screen');
    for (var i = 0; i < screens.length; i++) screens[i].classList.toggle('active', i+1 === n);
    for (var j = 1; j <= 4; j++) {
      var el = document.getElementById('step' + j);
      el.className = 'step' + (j < n ? ' done' : j === n ? ' active' : '');
      el.textContent = j < n ? '✓' : j;
    }
    state.step = n;
    if (n === 2) { renderDates(); hideMainButton(); }
    if (n === 3) { renderSummary(); showMainButton('Отправить заявку ✓', submitBooking); }
  }

  var currentMainButtonHandler = null;
  function showMainButton(text, callback) {
    if (tg && tg.MainButton) {
      tg.MainButton.setText(text);
      if (currentMainButtonHandler) tg.MainButton.offClick(currentMainButtonHandler);
      currentMainButtonHandler = callback;
      tg.MainButton.onClick(currentMainButtonHandler);
      tg.MainButton.show();
      tg.MainButton.enable();
    }
  }
  function hideMainButton() {
    if (tg && tg.MainButton) tg.MainButton.hide();
  }

  function submitBooking() {
    var p = (appData.procedures || [])[state.proc] || {};
    var payload = JSON.stringify({
      action: 'book', proc_idx: state.proc,
      proc_name: p.name, proc_dur: p.dur, proc_price_raw: p.price_raw,
      date: state.date, time: state.time,
    });
    if (tg && tg.sendData) {
      try {
        tg.sendData(payload);
      } catch(e) {
        alert('Ошибка отправки: ' + e.message);
      }
    } else {
      alert('Ошибка: Telegram WebApp недоступен');
    }
  }

  renderProcs();
</script>
</body>
</html>"""

def get_webapp_data() -> dict:
    """Формирует данные для Mini App."""
    dates_data = []
    total_slots = (WORK_END - WORK_START) * 60 // SLOT_MIN
    for d in available_dates():
        slots = free_slots(d)
        if slots:
            dates_data.append({"date": d, "slots": slots, "free": len(slots), "total": total_slots})
    return {
        "master_name":  MASTER_NAME,
        "master_phone": MASTER_PHONE,
        "rating":       MASTER_RATING,
        "procedures":   [{"name": name, "dur": dur, "price": fmt_price(pf, pt), "price_raw": pf}
                         for name, dur, pf, pt, _ in PROCEDURES],
        "dates": dates_data,
    }

def build_webapp_url(uid: int) -> str:
    import time, random
    cache_key = f"{int(time.time())}{random.randint(1000,9999)}"
    return f"{BOT_HOST.rstrip('/')}/webapp-page/{cache_key}"

def kb_main(uid):
    client = get_client(uid)
    rows   = []
    if client and client.get("last_proc"):
        short = client["last_proc"][:28] + "…" if len(client["last_proc"]) > 28 else client["last_proc"]
        rows.append([InlineKeyboardButton(f"🔄 Повторить: {short}", callback_data="repeat")])
    if WEBAPP_URL:
        rows.append([InlineKeyboardButton("📱 Записаться (приложение)", web_app=WebAppInfo(url=build_webapp_url(uid)))])
    rows += [
        [InlineKeyboardButton("📅 Записаться",    callback_data="book"),
         InlineKeyboardButton("📋 Мои записи",    callback_data="my_apts")],
        [InlineKeyboardButton("💆 Услуги и цены", callback_data="prices"),
         InlineKeyboardButton("👩‍⚕️ О мастере",  callback_data="about")],
        [InlineKeyboardButton("📞 Контакты",       callback_data="contact")],
    ]
    if uid == ADMIN_ID:
        rows.append([InlineKeyboardButton("🔐 Панель мастера", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

def kb_back(cb="main"):
    return InlineKeyboardMarkup([[InlineKeyboardButton("◀️ Назад", callback_data=cb)]])

def kb_procs():
    rows = [[InlineKeyboardButton(
        f"{name}  •  {fmt_price(pf, pt)}  •  {dur} мин",
        callback_data=f"proc_{i}"
    )] for i, (name, dur, pf, pt, _) in enumerate(PROCEDURES)]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="main")])
    return InlineKeyboardMarkup(rows)

def kb_dates(dates):
    total = (WORK_END - WORK_START) * 60 // SLOT_MIN
    rows, row = [], []
    for d in dates:
        dt   = datetime.strptime(d, "%Y-%m-%d")
        free = free_slots_count(d)
        ind  = "🟢" if free == total else ("🟡" if free > total // 2 else "🔴")
        label = f"{ind} {dt.day} {MONTHS[dt.month][:3]} {WEEKDAYS[dt.weekday()]} ({free})"
        row.append(InlineKeyboardButton(label, callback_data=f"date_{d}"))
        if len(row) == 2:
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
        InlineKeyboardButton("✅ Отправить заявку", callback_data="confirm_yes"),
        InlineKeyboardButton("❌ Отмена",           callback_data="main"),
    ]])

def kb_my_apts(apts):
    rows = [[InlineKeyboardButton(
        f"{'⏳' if a['status']=='pending' else '✅'} "
        f"{fmt_date(a['date'])} {a['time']} — {a['procedure'][:18]}",
        callback_data=f"apt_detail_{a['id']}"
    )] for a in apts]
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="main")])
    return InlineKeyboardMarkup(rows)

def kb_apt_detail(apt_id, status):
    rows = []
    if status in ("confirmed", "pending"):
        rows.append([InlineKeyboardButton("📅 Перенести", callback_data=f"reschedule_{apt_id}")])
        rows.append([InlineKeyboardButton("❌ Отменить",  callback_data=f"cancel_{apt_id}")])
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="my_apts")])
    return InlineKeyboardMarkup(rows)

def kb_phone():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 Поделиться номером", request_contact=True)]],
        resize_keyboard=True, one_time_keyboard=True,
    )

def kb_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📅 Расписание на день",   callback_data="adm_sched"),
         InlineKeyboardButton("📆 На неделю",            callback_data="adm_week")],
        [InlineKeyboardButton("📊 Статистика",           callback_data="adm_stats"),
         InlineKeyboardButton("⭐ Отзывы",               callback_data="adm_reviews")],
        [InlineKeyboardButton("➕ Добавить запись",      callback_data="adm_add")],
        [InlineKeyboardButton("🚫 Заблок. слот",         callback_data="adm_block"),
         InlineKeyboardButton("🔓 Разблок. слот",        callback_data="adm_unblock")],
        [InlineKeyboardButton("📵 Выходной день",        callback_data="adm_dayoff"),
         InlineKeyboardButton("❌ Отменить запись",       callback_data="adm_cancel")],
        [InlineKeyboardButton("📢 Рассылка",             callback_data="adm_broadcast"),
         InlineKeyboardButton("🚷 Бан клиента",          callback_data="adm_ban")],
        [InlineKeyboardButton("◀️ Главное меню",         callback_data="main")],
    ])

def kb_adm_dates(days=14, prefix="adm_d"):
    now = datetime.now(MOSCOW_TZ)
    rows, row = [], []
    for i in range(days):
        d  = (now + timedelta(days=i)).strftime("%Y-%m-%d")
        dt = now + timedelta(days=i)
        off   = "🚫" if is_dayoff(d) else ""
        label = f"{dt.day} {MONTHS[dt.month][:3]} {WEEKDAYS[dt.weekday()]} {off}".strip()
        row.append(InlineKeyboardButton(label, callback_data=f"{prefix}_{d}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
    return InlineKeyboardMarkup(rows)

def confirm_text(d):
    return (
        "📋 *Подтвердите запись:*\n\n"
        f"💆 {d['proc']}\n"
        f"📅 {fmt_date(d['date'])} в {d['time']}\n"
        f"⏱ {d['dur']} мин  •  💰 {d['price']:,} ₽\n\n".replace(",", " ") +
        f"👤 {d['name']}\n"
        f"📱 {d['phone']}\n\n"
        "_⏳ Ожидает подтверждения мастером_"
    )

# ═══════════════════════════════════════════════════════════════════════════════
# ОБЩИЕ ФУНКЦИИ
# ═══════════════════════════════════════════════════════════════════════════════

async def show_main(update, ctx):
    uid    = update.effective_user.id
    client = get_client(uid)
    set_state(ctx, ST_MAIN)
    greeting = (
        f"С возвращением! 👋 Вы у нас уже {client['visits']} раз — спасибо! 🌸"
        if client and client.get("visits", 0) > 1
        else "Главное меню — выберите действие:"
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(greeting, reply_markup=kb_main(uid))
    else:
        await update.message.reply_text(greeting, reply_markup=kb_main(uid))

async def start_booking(update, ctx, proc_idx=None):
    uid = update.effective_user.id
    if is_banned(uid):
        text = "⛔ Вы заблокированы. Свяжитесь с мастером."
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text)
        return
    if count_active(uid) >= MAX_ACTIVE:
        text = f"⚠️ У вас уже {MAX_ACTIVE} активных записи.\nОтмените одну, чтобы записаться снова."
        kb   = InlineKeyboardMarkup([[
            InlineKeyboardButton("Мои записи", callback_data="my_apts"),
            InlineKeyboardButton("◀️ Назад",   callback_data="main"),
        ]])
        if update.callback_query:
            await update.callback_query.edit_message_text(text, reply_markup=kb)
        else:
            await update.message.reply_text(text, reply_markup=kb)
        return
    if proc_idx is not None:
        name, dur, pf, pt, desc = PROCEDURES[proc_idx]
        ctx.user_data.update(proc=name, dur=dur, price=pf)
        dates = available_dates()
        if not dates:
            text = "😔 Свободных дат пока нет. Загляните позже."
            if update.callback_query:
                await update.callback_query.edit_message_text(text, reply_markup=kb_back())
            else:
                await update.message.reply_text(text, reply_markup=kb_back())
            return
        set_state(ctx, ST_DATE)
        text = f"✅ *{name}*\n_{desc}_\n💰 {fmt_price(pf,pt)}\n\n📅 Выберите дату:"
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb_dates(dates))
        else:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb_dates(dates))
    else:
        set_state(ctx, ST_PROC)
        text = "💆 *Выберите процедуру:*"
        if update.callback_query:
            await update.callback_query.edit_message_text(text, parse_mode="Markdown", reply_markup=kb_procs())
        else:
            await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb_procs())

# ═══════════════════════════════════════════════════════════════════════════════
# КОМАНДЫ
# ═══════════════════════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    uid    = update.effective_user.id
    name   = update.effective_user.first_name or "гость"
    client = get_client(uid)
    set_state(ctx, ST_MAIN)
    if client:
        text = (f"С возвращением, *{name}*! 👋\n\nВы у нас уже {client['visits']} раз — спасибо! 🌸\nЧем могу помочь?")
    else:
        text = (f"Привет, *{name}*! 👋\n\nЯ помогу записаться к подологу *{MASTER_NAME}*.\n\n⭐ Рейтинг {MASTER_RATING}  •  Работает с {MASTER_SINCE} года\n\nВыберите действие:")
    await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb_main(uid))

async def cmd_book(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await start_booking(update, ctx)

async def cmd_my_apts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid  = update.effective_user.id
    apts = user_apts(uid)
    if not apts:
        await update.message.reply_text("У вас нет предстоящих записей.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("📅 Записаться", callback_data="book")]])); return
    lines = ["📋 *Ваши записи:*\n"]
    for a in apts:
        lines.append(f"• {fmt_date(a['date'])} в {a['time']} — {a['procedure']} ({'⏳' if a['status']=='pending' else '✅'})")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb_my_apts(apts))

async def cmd_prices(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lines = ["💆 *Услуги и цены:*\n"]
    for name, dur, pf, pt, desc in PROCEDURES:
        lines.append(f"*{name}*\n⏱ {dur} мин  •  💰 {fmt_price(pf,pt)}\n_{desc}_\n")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb_back())

async def cmd_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📞 *Контакты:*\n\nТелефон: {MASTER_PHONE}\nTelegram: {MASTER_TG}\n\nРежим работы: пн–сб, {WORK_START}:00–{WORK_END}:00",
        parse_mode="Markdown", reply_markup=kb_back())

async def cmd_about(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👩‍⚕️ *{MASTER_NAME}*\n\n{MASTER_ABOUT}\n\n⭐ Рейтинг: {MASTER_RATING}\n📅 Принимает с {MASTER_SINCE} года",
        parse_mode="Markdown", reply_markup=kb_back())

async def cmd_export(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет доступа."); return
    await update.message.reply_text(export_week(), parse_mode="Markdown", reply_markup=kb_back("admin"))

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("⛔ Нет доступа."); return
    set_state(ctx, ST_ADM)
    await update.message.reply_text("🔐 *Панель мастера:*", parse_mode="Markdown", reply_markup=kb_admin())

# ═══════════════════════════════════════════════════════════════════════════════
# РОУТЕР
# ═══════════════════════════════════════════════════════════════════════════════

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q    = update.callback_query
    await q.answer()
    data = q.data
    uid  = update.effective_user.id

    if is_rate_limited(uid):
        await q.answer("⏳ Не так быстро!", show_alert=True)
        return

    if data == "main":
        ctx.user_data.clear()
        await show_main(update, ctx)
        return

    if data == "prices":
        lines = ["💆 *Услуги и цены:*\n"]
        for name, dur, pf, pt, desc in PROCEDURES:
            lines.append(f"*{name}*\n⏱ {dur} мин  •  💰 {fmt_price(pf,pt)}\n_{desc}_\n")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb_back())
        return

    if data == "about":
        await q.edit_message_text(
            f"👩‍⚕️ *{MASTER_NAME}*\n\n{MASTER_ABOUT}\n\n⭐ Рейтинг: {MASTER_RATING}\n📅 Принимает с {MASTER_SINCE} года",
            parse_mode="Markdown", reply_markup=kb_back())
        return

    if data == "contact":
        await q.edit_message_text(
            f"📞 *Контакты:*\n\nТелефон: {MASTER_PHONE}\nTelegram: {MASTER_TG}\n\nРежим работы: пн–сб, {WORK_START}:00–{WORK_END}:00",
            parse_mode="Markdown", reply_markup=kb_back())
        return

    if data == "my_apts":
        apts = user_apts(uid)
        if not apts:
            await q.edit_message_text("У вас нет предстоящих записей.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📅 Записаться", callback_data="book")],
                    [InlineKeyboardButton("◀️ Назад", callback_data="main")],
                ])); return
        lines = ["📋 *Ваши записи:*\n"]
        for a in apts:
            lines.append(f"• {fmt_date(a['date'])} в {a['time']} — {a['procedure']} ({'⏳' if a['status']=='pending' else '✅'})")
        await q.edit_message_text("\n".join(lines) + "\n\nВыберите запись:",
            parse_mode="Markdown", reply_markup=kb_my_apts(apts))
        return

    if data.startswith("apt_detail_"):
        apt_id = data[len("apt_detail_"):]
        apt    = get_apt(apt_id)
        if not apt:
            await q.edit_message_text("Запись не найдена.", reply_markup=kb_back("my_apts")); return
        status_text = "⏳ Ожидает подтверждения" if apt["status"] == "pending" else "✅ Подтверждена"
        await q.edit_message_text(
            f"📋 *Детали записи:*\n\n💆 {apt['procedure']}\n📅 {fmt_date(apt['date'])} в {apt['time']}\n💰 {apt['price']:,} ₽\n\nСтатус: {status_text}".replace(",", " "),
            parse_mode="Markdown", reply_markup=kb_apt_detail(apt_id, apt["status"]))
        return

    if data.startswith("cancel_"):
        apt_id = data[7:]
        apt    = cancel_apt(apt_id)
        if apt:
            cancel_reminders(ctx.application, apt_id)
            try:
                await ctx.bot.send_message(ADMIN_ID,
                    f"⚠️ *Клиент отменил запись*\n\n👤 {apt['name']}  📱 {apt['phone']}\n💆 {apt['procedure']}\n📅 {fmt_date(apt['date'])} в {apt['time']}",
                    parse_mode="Markdown")
            except Exception: pass
            await q.edit_message_text(f"✅ Запись на {fmt_date(apt['date'])} в {apt['time']} отменена.", reply_markup=kb_back("main"))
        else:
            await q.edit_message_text("Запись не найдена.", reply_markup=kb_back("my_apts"))
        return

    if data.startswith("reschedule_"):
        apt_id = data[len("reschedule_"):]
        ctx.user_data["reschedule_apt_id"] = apt_id
        dates  = available_dates()
        if not dates:
            await q.edit_message_text("😔 Свободных дат нет.", reply_markup=kb_back("my_apts")); return
        set_state(ctx, ST_RESCHEDULE_DATE)
        await q.edit_message_text("📅 Выберите новую дату:", reply_markup=kb_dates(dates))
        return

    if data.startswith("rdate_"):
        date   = data[6:]
        apt_id = ctx.user_data.get("reschedule_apt_id", "")
        slots  = free_slots(date)
        if not slots:
            await q.edit_message_text("На этот день нет свободных слотов.", reply_markup=kb_dates(available_dates())); return
        ctx.user_data["reschedule_date"] = date
        set_state(ctx, ST_RESCHEDULE_TIME)
        rows, row = [], []
        for s in slots:
            row.append(InlineKeyboardButton(f"🕐 {s}", callback_data=f"rtime_{s}"))
            if len(row) == 4: rows.append(row); row = []
        if row: rows.append(row)
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data=f"reschedule_{apt_id}")])
        await q.edit_message_text(f"📅 *{fmt_date(date)}*\n\n🕐 Выберите новое время:",
            parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("rtime_"):
        new_time = data[6:]
        new_date = ctx.user_data.get("reschedule_date", "")
        apt_id   = ctx.user_data.get("reschedule_apt_id", "")
        try:
            old_apt, new_id = reschedule_apt(apt_id, new_date, new_time)
        except ValueError as e:
            await q.edit_message_text(str(e), reply_markup=kb_back("my_apts")); return
        if not old_apt:
            await q.edit_message_text("Запись не найдена.", reply_markup=kb_back("my_apts")); return
        cancel_reminders(ctx.application, apt_id)
        schedule_reminders(ctx.application, new_id, uid, new_date, new_time)
        set_state(ctx, ST_MAIN)
        try:
            await ctx.bot.send_message(ADMIN_ID,
                f"🔄 *Клиент перенёс запись*\n\n👤 {old_apt['name']}  📱 {old_apt['phone']}\n💆 {old_apt['procedure']}\nБыло: {fmt_date(old_apt['date'])} в {old_apt['time']}\nСтало: {fmt_date(new_date)} в {new_time}\n\nПодтвердите:",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Подтвердить", callback_data=f"adm_ok_{new_id}"),
                    InlineKeyboardButton("❌ Отклонить",   callback_data=f"adm_rej_{new_id}"),
                ]]), parse_mode="Markdown")
        except Exception: pass
        await q.edit_message_text(
            f"🔄 *Запись перенесена!*\n\n📅 {fmt_date(new_date)} в {new_time}\n💆 {old_apt['procedure']}\n\n⏳ Ожидает подтверждения мастером.",
            parse_mode="Markdown", reply_markup=kb_back("main"))
        return

    if data.startswith("rev_"):
        parts  = data.split("_")
        rating = int(parts[-1])
        apt_id = "_".join(parts[1:-1])
        save_review(apt_id, uid, rating)
        await q.edit_message_text(
            f"{'⭐' * rating}\n\nСпасибо за оценку! Рады видеть вас снова! 🌸",
            reply_markup=kb_back("main"))
        try:
            apt = get_apt(apt_id)
            await ctx.bot.send_message(ADMIN_ID,
                f"⭐ *Новый отзыв!*\n\nОценка: {'⭐' * rating}\nКлиент: {apt['name'] if apt else uid}\nПроцедура: {apt['procedure'] if apt else '—'}",
                parse_mode="Markdown")
        except Exception: pass
        return

    if data == "book":
        await start_booking(update, ctx); return

    if data == "repeat":
        client = get_client(uid)
        if client and client.get("last_proc"):
            for i, (name, *_) in enumerate(PROCEDURES):
                if name == client["last_proc"]:
                    await start_booking(update, ctx, proc_idx=i); return
        await start_booking(update, ctx); return

    if data.startswith("proc_"):
        if ctx.user_data.get("adm_action") == "add_proc":
            idx = int(data[5:])
            name, dur, pf, pt, _ = PROCEDURES[idx]
            ctx.user_data.update(adm_proc=name, adm_dur=dur, adm_price=pf)
            set_state(ctx, ST_ADM_ADD_NAME)
            await q.edit_message_text("👤 Введите имя клиента:"); return
        idx = int(data[5:])
        name, dur, pf, pt, desc = PROCEDURES[idx]
        ctx.user_data.update(proc=name, dur=dur, price=pf)
        dates = available_dates()
        if not dates:
            await q.edit_message_text("😔 Свободных дат нет.", reply_markup=kb_back()); return
        set_state(ctx, ST_DATE)
        await q.edit_message_text(
            f"✅ *{name}*\n_{desc}_\n💰 {fmt_price(pf,pt)}\n\n📅 Выберите дату:",
            parse_mode="Markdown", reply_markup=kb_dates(dates)); return

    if data == "back_date":
        dates = available_dates()
        set_state(ctx, ST_DATE)
        await q.edit_message_text(
            f"💆 *{ctx.user_data.get('proc','Процедура')}*\n\n📅 Выберите дату:",
            parse_mode="Markdown", reply_markup=kb_dates(dates)); return

    if data.startswith("date_"):
        date  = data[5:]
        slots = free_slots(date)
        if not slots:
            await q.edit_message_text("На этот день нет свободных слотов.", reply_markup=kb_dates(available_dates())); return
        ctx.user_data["date"] = date
        set_state(ctx, ST_TIME)
        await q.edit_message_text(f"📅 *{fmt_date(date)}*\n\n🕐 Выберите время:",
            parse_mode="Markdown", reply_markup=kb_times(slots)); return

    if data.startswith("time_"):
        time   = data[5:]
        client = get_client(uid)
        ctx.user_data["time"] = time
        ctx.user_data["name"] = update.effective_user.full_name or update.effective_user.first_name or "Клиент"
        if client and client.get("phone"):
            ctx.user_data["phone"] = client["phone"]
            set_state(ctx, ST_CONFIRM)
            await q.edit_message_text(confirm_text(ctx.user_data), parse_mode="Markdown", reply_markup=kb_confirm())
        else:
            set_state(ctx, ST_PHONE)
            await q.edit_message_text("📱 Для записи нужен ваш номер телефона.\n\nНажмите кнопку ниже 👇")
            await ctx.bot.send_message(uid, "Поделитесь номером:", reply_markup=kb_phone())
        return

    if data == "confirm_yes":
        d = ctx.user_data
        if not d.get("proc"):
            await q.edit_message_text("⚠️ Сессия устарела. Начните заново.", reply_markup=kb_main(uid)); return
        proc  = d["proc"];  date  = d["date"]
        time  = d["time"];  name  = d["name"]
        phone = d["phone"]; price = d["price"]
        dur   = d["dur"]
        try:
            apt_id = add_apt(date, time, name, phone, uid, proc, dur, price)
        except ValueError as e:
            await q.edit_message_text(str(e), reply_markup=kb_main(uid)); return
        ctx.user_data.clear()
        set_state(ctx, ST_MAIN)
        try:
            await ctx.bot.send_message(ADMIN_ID,
                f"🔔 *Новая заявка!*\n\n👤 {name}  📱 {phone}\n💆 {proc}\n📅 {fmt_date(date)} в {time}\n💰 {price:,} ₽\n\nПодтвердите:".replace(",", " "),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Подтвердить", callback_data=f"adm_ok_{apt_id}"),
                    InlineKeyboardButton("❌ Отклонить",   callback_data=f"adm_rej_{apt_id}"),
                ]]), parse_mode="Markdown")
        except Exception: pass
        await q.edit_message_text(
            f"🎉 *Заявка отправлена!*\n\n💆 {proc}\n📅 {fmt_date(date)} в {time}\n\n⏳ Мастер подтвердит запись в ближайшее время.\nВопросы: {MASTER_PHONE}",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("📋 Мои записи", callback_data="my_apts"),
                InlineKeyboardButton("◀️ В меню",     callback_data="main"),
            ]])); return

    if data.startswith("adm_ok_"):
        if uid != ADMIN_ID: await q.answer("⛔", show_alert=True); return
        apt_id = data[len("adm_ok_"):]
        apt    = confirm_apt_by_admin(apt_id)
        if apt:
            schedule_reminders(ctx.application, apt_id, apt["user_id"], apt["date"], apt["time"])
            try:
                await ctx.bot.send_message(apt["user_id"],
                    f"✅ *Запись подтверждена!*\n\n💆 {apt['procedure']}\n📅 {fmt_date(apt['date'])} в {apt['time']}\n\nНапомню за сутки и за 2 часа. 🔔",
                    parse_mode="Markdown")
            except Exception: pass
            await q.edit_message_text(f"✅ Подтверждено: {apt['name']} {fmt_date(apt['date'])} в {apt['time']}")
        else:
            await q.edit_message_text("Запись уже обработана.")
        return

    if data.startswith("adm_rej_"):
        if uid != ADMIN_ID: await q.answer("⛔", show_alert=True); return
        apt_id = data[len("adm_rej_"):]
        apt    = reject_apt_by_admin(apt_id)
        if apt:
            try:
                await ctx.bot.send_message(apt["user_id"],
                    f"❌ *Запись отклонена*\n\n💆 {apt['procedure']}\n📅 {fmt_date(apt['date'])} в {apt['time']}\n\nСвяжитесь: {MASTER_PHONE}",
                    parse_mode="Markdown")
            except Exception: pass
            await q.edit_message_text(f"❌ Отклонено: {apt['name']} {fmt_date(apt['date'])} в {apt['time']}")
        else:
            await q.edit_message_text("Запись уже обработана.")
        return

    if uid != ADMIN_ID and data.startswith("adm"):
        await q.answer("⛔ Нет доступа.", show_alert=True); return

    if data == "admin":
        set_state(ctx, ST_ADM)
        await q.edit_message_text("🔐 *Панель мастера:*", parse_mode="Markdown", reply_markup=kb_admin()); return

    if data == "adm_stats":
        s = get_stats()
        lines = [
            "📊 *Статистика:*\n",
            f"👥 Всего клиентов: {s['total_clients']}",
            f"⭐ Средняя оценка: {s['avg_rating']}",
            f"\n📅 Сегодня: {s['today_count']} зап. · {s['today_rev']:,} ₽".replace(",", " "),
            f"📅 Неделя:  {s['week_count']} зап. · {s['week_rev']:,} ₽".replace(",", " "),
            f"📅 Месяц:   {s['month_count']} зап. · {s['month_rev']:,} ₽".replace(",", " "),
        ]
        if s["top"]:
            lines.append("\n🏆 *Топ процедуры:*")
            for proc, cnt in s["top"]:
                lines.append(f"  • {proc}: {cnt} раз")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb_back("admin")); return

    if data == "adm_reviews":
        stats, recent = get_reviews_stats()
        lines = [f"⭐ *Отзывы:*\n", f"Всего: {stats['cnt']}  •  Средняя: {stats['avg'] or '—'} ⭐\n"]
        for r in recent:
            lines.append(f"{'⭐' * r['rating']}  {r['created'][:10]}")
        await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb_back("admin")); return

    if data == "adm_sched":
        ctx.user_data["adm_action"] = "sched"
        await q.edit_message_text("📅 Выберите день:", reply_markup=kb_adm_dates(14, "adm_d")); return

    if data == "adm_week":
        await q.edit_message_text(export_week(), parse_mode="Markdown", reply_markup=kb_back("admin")); return

    if data == "adm_block":
        ctx.user_data["adm_action"] = "block"
        await q.edit_message_text("🚫 Выберите дату:", reply_markup=kb_adm_dates(14, "adm_d")); return

    if data == "adm_unblock":
        bl = all_blocked_slots()
        if not bl:
            await q.edit_message_text("Нет заблокированных слотов.", reply_markup=kb_back("admin")); return
        rows = [[InlineKeyboardButton(f"🔓 {fmt_date(b['date'])} в {b['time']}", callback_data=f"adm_unblk_{b['date']}|{b['time']}")] for b in bl]
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
        await q.edit_message_text("🔓 Выберите слот:", reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("adm_unblk_"):
        d, t = data[len("adm_unblk_"):].split("|", 1)
        unblock_slot(d, t)
        await q.edit_message_text(f"✅ Слот {fmt_date(d)} в {t} разблокирован.", reply_markup=kb_admin()); return

    if data == "adm_dayoff":
        ctx.user_data["adm_action"] = "dayoff"
        await q.edit_message_text("📵 Выберите день:", reply_markup=kb_adm_dates(30, "adm_d")); return

    if data == "adm_add":
        ctx.user_data["adm_action"] = "add"
        await q.edit_message_text("➕ Выберите дату:", reply_markup=kb_adm_dates(14, "adm_d")); return

    if data == "adm_cancel":
        now  = datetime.now(MOSCOW_TZ)
        apts = []
        for i in range(30):
            apts.extend(day_apts((now + timedelta(days=i)).strftime("%Y-%m-%d")))
        if not apts:
            await q.edit_message_text("Нет активных записей.", reply_markup=kb_back("admin")); return
        rows = [[InlineKeyboardButton(
            f"{'⏳' if a['status']=='pending' else '✅'} {a['date']} {a['time']} — {a['name']}",
            callback_data=f"adm_cncl_{a['id']}"
        )] for a in apts]
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
        await q.edit_message_text("❌ Выберите запись:", reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("adm_cncl_"):
        apt_id = data[len("adm_cncl_"):]
        apt    = cancel_apt(apt_id)
        if apt:
            cancel_reminders(ctx.application, apt_id)
            try:
                await ctx.bot.send_message(apt["user_id"],
                    f"⚠️ *Ваша запись отменена мастером*\n\n📅 {fmt_date(apt['date'])} в {apt['time']}\n💆 {apt['procedure']}\n\nСвяжитесь: {MASTER_PHONE}",
                    parse_mode="Markdown")
            except Exception: pass
            await q.edit_message_text(f"✅ Запись {apt['name']} отменена.", reply_markup=kb_admin()); return

    if data == "adm_broadcast":
        set_state(ctx, ST_ADM_BROADCAST)
        await q.edit_message_text("📢 Введите текст рассылки.\nДля отмены напишите /admin"); return

    if data == "adm_ban":
        now  = datetime.now(MOSCOW_TZ)
        apts = []
        for i in range(30):
            apts.extend(day_apts((now + timedelta(days=i)).strftime("%Y-%m-%d")))
        seen, rows = set(), []
        for a in apts:
            if a["user_id"] not in seen:
                seen.add(a["user_id"])
                banned = is_banned(a["user_id"])
                rows.append([InlineKeyboardButton(
                    f"{'🚫' if banned else '👤'} {a['name']} ({a['phone']})",
                    callback_data=f"adm_toggle_ban_{a['user_id']}"
                )])
        if not rows:
            await q.edit_message_text("Нет клиентов.", reply_markup=kb_back("admin")); return
        rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
        await q.edit_message_text("🚷 Выберите клиента:\n🚫 — заблокирован  👤 — активен",
            reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("adm_toggle_ban_"):
        ban_uid = int(data[len("adm_toggle_ban_"):])
        result  = toggle_ban(ban_uid)
        await q.edit_message_text(
            "🚫 Клиент заблокирован." if result == "banned" else "✅ Клиент разблокирован.",
            reply_markup=kb_admin()); return

    if data.startswith("adm_d_"):
        date   = data[len("adm_d_"):]
        action = ctx.user_data.get("adm_action", "sched")

        if action == "sched":
            apts  = day_apts(date)
            off   = is_dayoff(date)
            lines = [f"📅 *{fmt_date(date)}*\n"]
            if off: lines.append("🚫 Выходной день\n")
            if apts:
                total = sum(a["price"] for a in apts)
                for a in apts:
                    icon = "⏳" if a["status"] == "pending" else "✅"
                    lines.append(f"{icon} {a['time']}  {a['name']}  {a['phone']}\n   {a['procedure']} — {a['price']:,} ₽".replace(",", " "))
                lines.append(f"\n💰 Итого: {total:,} ₽  •  {len(apts)} записей".replace(",", " "))
            else:
                lines.append("_Записей нет._")
            lines.append(f"\n🟢 Свободных слотов: {free_slots_count(date)}")
            await q.edit_message_text("\n".join(lines), parse_mode="Markdown", reply_markup=kb_back("admin")); return

        if action == "dayoff":
            result = toggle_dayoff(date)
            await q.edit_message_text(
                f"🚫 {fmt_date(date)} — добавлен как выходной." if result == "added" else f"✅ {fmt_date(date)} — убран из выходных.",
                reply_markup=kb_admin()); return

        if action == "block":
            statuses = all_slots_status(date)
            rows = []
            for t, s in statuses:
                e  = "🔴" if s == "booked" else ("🟡" if s == "blocked" else "🟢")
                cb = f"adm_blk_{date}|{t}" if s == "free" else "adm_noop"
                rows.append([InlineKeyboardButton(f"{e} {t}", callback_data=cb)])
            rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
            await q.edit_message_text(f"*{fmt_date(date)}*\n🟢 свободен  🟡 заблокирован  🔴 занят",
                parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(rows)); return

        if action == "add":
            ctx.user_data["adm_date"] = date
            slots = free_slots(date)
            if not slots:
                await q.edit_message_text("Нет свободных слотов.", reply_markup=kb_back("admin")); return
            rows, row = [], []
            for s in slots:
                row.append(InlineKeyboardButton(s, callback_data=f"adm_addt_{date}|{s}"))
                if len(row) == 4: rows.append(row); row = []
            if row: rows.append(row)
            rows.append([InlineKeyboardButton("◀️ Назад", callback_data="admin")])
            await q.edit_message_text(f"🕐 Выберите время на {fmt_date(date)}:",
                reply_markup=InlineKeyboardMarkup(rows)); return

    if data.startswith("adm_blk_"):
        date, time = data[len("adm_blk_"):].split("|", 1)
        block_slot(date, time)
        await q.edit_message_text(f"🚫 Слот {fmt_date(date)} в {time} заблокирован.", reply_markup=kb_admin()); return

    if data == "adm_noop":
        return

    if data.startswith("adm_addt_"):
        date, time = data[len("adm_addt_"):].split("|", 1)
        ctx.user_data["adm_date"]   = date
        ctx.user_data["adm_time"]   = time
        ctx.user_data["adm_action"] = "add_proc"
        await q.edit_message_text("💆 Выберите процедуру:", reply_markup=kb_procs()); return

# ═══════════════════════════════════════════════════════════════════════════════
# ОБРАБОТЧИК КОНТАКТА И ТЕКСТА
# ═══════════════════════════════════════════════════════════════════════════════

async def _process_webapp_booking(uid, proc_idx, proc_name, date, time, pf, dur, name):
    """
    Общая логика обработки заявки из Mini App.
    Используется и из tg.sendData (on_web_app_data), и из прямого HTTP POST (/webapp-book).
    Возвращает dict с результатом для ответа клиенту.
    """
    bot = _bot_app.bot
    client = get_client(uid)
    phone  = client["phone"] if client and client.get("phone") else None

    if not phone:
        # Просим телефон через обычный чат с ботом
        try:
            await bot.send_message(
                uid, "📱 Для завершения записи нужен ваш номер телефона.\n\n"
                     "Нажмите кнопку ниже 👇")
            await bot.send_message(uid, "Поделитесь номером:",
                                    reply_markup=kb_phone())
        except Exception as e:
            log.warning("Не удалось запросить телефон: %s", e)
        # Сохраняем контекст записи во временную БД-таблицу clients, чтобы
        # подхватить её когда придёт номер телефона через on_contact/on_message.
        # Простое решение: используем глобальный словарь в памяти.
        _pending_bookings[uid] = dict(
            proc=proc_name, dur=dur, price=pf, date=date, time=time, name=name
        )
        return {"ok": True, "status": "phone_requested"}

    try:
        apt_id = add_apt(date, time, name, phone, uid, proc_name, dur, pf)
    except ValueError as e:
        try:
            await bot.send_message(uid, str(e), reply_markup=kb_main(uid))
        except Exception:
            pass
        return {"ok": False, "error": str(e)}

    try:
        await bot.send_message(
            ADMIN_ID,
            f"🔔 *Новая заявка (Mini App)!*\n\n👤 {name}  📱 {phone}\n💆 {proc_name}\n"
            f"📅 {fmt_date(date)} в {time}\n💰 {pf:,} ₽\n\nПодтвердите:".replace(",", " "),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"adm_ok_{apt_id}"),
                InlineKeyboardButton("❌ Отклонить",   callback_data=f"adm_rej_{apt_id}"),
            ]]), parse_mode="Markdown")
    except Exception as e:
        log.warning("Не удалось уведомить админа: %s", e)

    try:
        await bot.send_message(
            uid,
            f"🎉 *Заявка отправлена!*\n\n💆 {proc_name}\n📅 {fmt_date(date)} в {time}\n\n"
            "⏳ Мастер подтвердит запись в ближайшее время.",
            parse_mode="Markdown", reply_markup=kb_main(uid))
    except Exception as e:
        log.warning("Не удалось уведомить клиента: %s", e)

    return {"ok": True, "status": "booked", "apt_id": apt_id}

# Временное хранилище незавершённых записей (ждут номер телефона)
_pending_bookings = {}

async def on_web_app_data(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    import json
    log.info("WEB_APP_DATA получено от uid=%s: %s", update.effective_user.id,
              update.message.web_app_data.data if update.message.web_app_data else "NONE")
    try:
        data = json.loads(update.message.web_app_data.data)
    except Exception as e:
        log.warning("Ошибка парсинга web_app_data: %s", e)
        return
    if data.get("action") != "book":
        log.info("action != book, пропускаем: %s", data.get("action"))
        return
    uid       = update.effective_user.id
    proc_idx  = data.get("proc_idx", 0)
    proc_name = data.get("proc_name", "")
    date      = data.get("date", "")
    time      = data.get("time", "")
    pf        = data.get("proc_price_raw", 0)
    dur       = data.get("proc_dur", 60)
    name      = update.effective_user.full_name or update.effective_user.first_name or "Клиент"
    client    = get_client(uid)
    phone     = client["phone"] if client and client.get("phone") else None
    if not phone:
        ctx.user_data.update(proc=proc_name, dur=dur, price=pf, date=date, time=time, name=name)
        set_state(ctx, ST_PHONE)
        await update.message.reply_text("📱 Нужен ваш номер телефона:", reply_markup=kb_phone())
        return
    try:
        apt_id = add_apt(date, time, name, phone, uid, proc_name, dur, pf)
    except ValueError as e:
        await update.message.reply_text(str(e), reply_markup=kb_main(uid)); return
    ctx.user_data.clear(); set_state(ctx, ST_MAIN)
    try:
        await ctx.bot.send_message(ADMIN_ID,
            f"🔔 *Новая заявка (Mini App)!*\n\n👤 {name}  📱 {phone}\n💆 {proc_name}\n📅 {fmt_date(date)} в {time}\n💰 {pf:,} ₽\n\nПодтвердите:".replace(",", " "),
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("✅ Подтвердить", callback_data=f"adm_ok_{apt_id}"),
                InlineKeyboardButton("❌ Отклонить",   callback_data=f"adm_rej_{apt_id}"),
            ]]), parse_mode="Markdown")
    except Exception: pass
    await update.message.reply_text(
        f"🎉 *Заявка отправлена!*\n\n💆 {proc_name}\n📅 {fmt_date(date)} в {time}\n\n⏳ Мастер подтвердит запись в ближайшее время.",
        parse_mode="Markdown", reply_markup=kb_main(uid))

async def on_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    phone = update.message.contact.phone_number
    if not phone.startswith("+"): phone = "+" + phone
    uid = update.effective_user.id

    # Если есть незавершённая запись из Mini App — обрабатываем здесь
    if uid in _pending_bookings:
        pending = _pending_bookings.pop(uid)
        await update.message.reply_text(f"✅ Номер получен: {phone}", reply_markup=ReplyKeyboardRemove())
        try:
            apt_id = add_apt(pending["date"], pending["time"], pending["name"], phone,
                              uid, pending["proc"], pending["dur"], pending["price"])
        except ValueError as e:
            await update.message.reply_text(str(e), reply_markup=kb_main(uid)); return
        try:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"🔔 *Новая заявка (Mini App)!*\n\n👤 {pending['name']}  📱 {phone}\n"
                f"💆 {pending['proc']}\n📅 {fmt_date(pending['date'])} в {pending['time']}\n"
                f"💰 {pending['price']:,} ₽\n\nПодтвердите:".replace(",", " "),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Подтвердить", callback_data=f"adm_ok_{apt_id}"),
                    InlineKeyboardButton("❌ Отклонить",   callback_data=f"adm_rej_{apt_id}"),
                ]]), parse_mode="Markdown")
        except Exception: pass
        set_state(ctx, ST_MAIN)
        await update.message.reply_text(
            f"🎉 *Заявка отправлена!*\n\n💆 {pending['proc']}\n📅 {fmt_date(pending['date'])} в {pending['time']}\n\n"
            "⏳ Мастер подтвердит запись в ближайшее время.",
            parse_mode="Markdown", reply_markup=kb_main(uid))
        return

    ctx.user_data["phone"] = phone
    await update.message.reply_text(f"✅ Номер получен: {phone}", reply_markup=ReplyKeyboardRemove())
    set_state(ctx, ST_CONFIRM)
    await update.message.reply_text(confirm_text(ctx.user_data), parse_mode="Markdown", reply_markup=kb_confirm())

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text  = update.message.text.strip()
    state = get_state(ctx)
    uid   = update.effective_user.id

    if is_rate_limited(uid):
        await update.message.reply_text("⏳ Не так быстро!"); return

    # Если есть незавершённая запись из Mini App (HTTP-флоу) — обрабатываем номер здесь
    if uid in _pending_bookings:
        if not validate_phone(text):
            await update.message.reply_text("❌ Неверный формат. Нажмите кнопку ниже 👇", reply_markup=kb_phone()); return
        phone = re.sub(r"\D", "", text)
        if phone.startswith("8"): phone = "7" + phone[1:]
        phone = "+" + phone
        pending = _pending_bookings.pop(uid)
        await update.message.reply_text("✅ Номер принят.", reply_markup=ReplyKeyboardRemove())
        try:
            apt_id = add_apt(pending["date"], pending["time"], pending["name"], phone,
                              uid, pending["proc"], pending["dur"], pending["price"])
        except ValueError as e:
            await update.message.reply_text(str(e), reply_markup=kb_main(uid)); return
        try:
            await ctx.bot.send_message(
                ADMIN_ID,
                f"🔔 *Новая заявка (Mini App)!*\n\n👤 {pending['name']}  📱 {phone}\n"
                f"💆 {pending['proc']}\n📅 {fmt_date(pending['date'])} в {pending['time']}\n"
                f"💰 {pending['price']:,} ₽\n\nПодтвердите:".replace(",", " "),
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("✅ Подтвердить", callback_data=f"adm_ok_{apt_id}"),
                    InlineKeyboardButton("❌ Отклонить",   callback_data=f"adm_rej_{apt_id}"),
                ]]), parse_mode="Markdown")
        except Exception: pass
        set_state(ctx, ST_MAIN)
        await update.message.reply_text(
            f"🎉 *Заявка отправлена!*\n\n💆 {pending['proc']}\n📅 {fmt_date(pending['date'])} в {pending['time']}\n\n"
            "⏳ Мастер подтвердит запись в ближайшее время.",
            parse_mode="Markdown", reply_markup=kb_main(uid))
        return

    if state == ST_PHONE:
        if not validate_phone(text):
            await update.message.reply_text("❌ Неверный формат. Нажмите кнопку ниже 👇", reply_markup=kb_phone()); return
        phone = re.sub(r"\D", "", text)
        if phone.startswith("8"): phone = "7" + phone[1:]
        phone = "+" + phone
        ctx.user_data["phone"] = phone
        await update.message.reply_text("✅ Номер принят.", reply_markup=ReplyKeyboardRemove())
        set_state(ctx, ST_CONFIRM)
        await update.message.reply_text(confirm_text(ctx.user_data), parse_mode="Markdown", reply_markup=kb_confirm()); return

    if state == ST_ADM_ADD_NAME:
        ctx.user_data["adm_name"] = text
        set_state(ctx, ST_ADM_ADD_PHONE)
        await update.message.reply_text("📱 Введите телефон клиента:"); return

    if state == ST_ADM_ADD_PHONE:
        if not validate_phone(text):
            await update.message.reply_text("❌ Неверный формат. Введите ещё раз:"); return
        d = ctx.user_data
        try:
            apt_id = add_apt(d["adm_date"], d["adm_time"], d["adm_name"], text,
                             ADMIN_ID, d["adm_proc"], d["adm_dur"], d["adm_price"])
            confirm_apt_by_admin(apt_id)
        except ValueError as e:
            await update.message.reply_text(str(e), reply_markup=kb_admin())
            set_state(ctx, ST_ADM); return
        await update.message.reply_text(
            f"✅ *Запись создана:*\n\n👤 {d['adm_name']}  📱 {text}\n💆 {d['adm_proc']}\n📅 {fmt_date(d['adm_date'])} в {d['adm_time']}",
            parse_mode="Markdown", reply_markup=kb_admin())
        set_state(ctx, ST_ADM); return

    if state == ST_ADM_BROADCAST:
        uids = get_all_client_ids()
        sent, failed = 0, 0
        await update.message.reply_text(f"📢 Отправляю {len(uids)} клиентам...")
        for cid in uids:
            try:
                await ctx.bot.send_message(cid, f"📢 *Сообщение от мастера {MASTER_NAME}:*\n\n{text}", parse_mode="Markdown")
                sent += 1
            except Exception:
                failed += 1
        set_state(ctx, ST_ADM)
        await update.message.reply_text(f"✅ Готово. Отправлено: {sent}  •  Ошибок: {failed}", reply_markup=kb_admin()); return

    await update.message.reply_text("Используйте кнопки меню 👇", reply_markup=kb_main(uid))

# ═══════════════════════════════════════════════════════════════════════════════
# ЗАПУСК
# ═══════════════════════════════════════════════════════════════════════════════

async def _post_init(app):
    """Вызывается после старта event loop — сохраняем ссылки для HTTP сервера."""
    global _bot_app, _bot_loop
    _bot_app  = app
    _bot_loop = asyncio.get_running_loop()
    log.info("Bot app и event loop зарегистрированы для HTTP сервера")

def main():
    init_db()
    persistence = PicklePersistence(filepath="podolog_data")
    app = (Application.builder()
           .token(BOT_TOKEN)
           .persistence(persistence)
           .post_init(_post_init)
           .build())

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("book",    cmd_book))
    app.add_handler(CommandHandler("my_apts", cmd_my_apts))
    app.add_handler(CommandHandler("prices",  cmd_prices))
    app.add_handler(CommandHandler("contact", cmd_contact))
    app.add_handler(CommandHandler("about",   cmd_about))
    app.add_handler(CommandHandler("export",  cmd_export))
    app.add_handler(CommandHandler("admin",   cmd_admin))

    # Диагностика: логируем абсолютно все входящие сообщения (группа -1 = раньше остальных,
    # не блокирует дальнейшую обработку благодаря отдельной группе)
    async def _log_all_messages(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        if update.message:
            wad = getattr(update.message, "web_app_data", None)
            log.info(
                "RAW UPDATE: uid=%s text=%r web_app_data=%r",
                update.effective_user.id if update.effective_user else "?",
                update.message.text,
                wad.data if wad else None,
            )
    app.add_handler(MessageHandler(filters.ALL, _log_all_messages), group=-1)

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_web_app_data))
    app.add_handler(MessageHandler(filters.CONTACT, on_contact))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    # Напоминания из БД — работает даже после перезапуска
    if app.job_queue:
        app.job_queue.run_repeating(_process_reminders, interval=60, first=10)
        log.info("Планировщик напоминаний из БД запущен ✅")
    else:
        log.warning('JobQueue недоступен. pip install "python-telegram-bot[job-queue]"')

    # Запускаем HTTP сервер для Mini App в фоне
    http_thread = threading.Thread(target=start_http_server, daemon=True)
    http_thread.start()

    log.info("Бот запущен ✅")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
