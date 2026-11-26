import os, json, hmac, hashlib, html, time, threading, re, logging, random, uuid, asyncio, base64
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl

import httpx
import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Update, Message, PreCheckoutQuery, InlineKeyboardMarkup,
    InlineKeyboardButton, WebAppInfo, LabeledPrice, MenuButtonWebApp
)

BOT_TOKEN = (os.getenv("BOT_TOKEN") or "").strip()
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if not BOT_TOKEN:
    raise RuntimeError("Missing required environment variable: BOT_TOKEN")
if not DATABASE_URL:
    raise RuntimeError("Missing required environment variable: DATABASE_URL")
BASE_URL = (os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
ADMIN_IDS = {
    int(x.strip())
    for x in (os.getenv("ADMIN_IDS") or "").split(",")
    if x.strip().isdigit()
}
LOG_RETENTION_DAYS = max(1, int(os.getenv("LOG_RETENTION_DAYS", "10")))
LOG_MAX_ROWS = max(500, int(os.getenv("LOG_MAX_ROWS", "5000")))

AI_KEY = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
AI_MODEL = os.getenv("AI_MODEL", "gemini-2.5-flash")
AI_FALLBACK_MODELS = [
    m.strip()
    for m in os.getenv(
        "AI_FALLBACK_MODELS",
        "gemini-2.5-flash,gemini-2.5-flash-lite,gemini-2.0-flash",
    ).split(",")
    if m.strip()
]
FREE_FALLBACK_API_KEY = (os.getenv("FREE_FALLBACK_API_KEY") or os.getenv("OPENROUTER_API_KEY") or "").strip()
FREE_FALLBACK_BASE_URL = (os.getenv("FREE_FALLBACK_BASE_URL") or "https://openrouter.ai/api/v1/chat/completions").strip()
FREE_FALLBACK_MODELS = [
    m.strip()
    for m in (os.getenv("FREE_FALLBACK_MODELS") or os.getenv("FREE_FALLBACK_MODEL") or "openrouter/free").split(",")
    if m.strip()
]

FREE_FALLBACK_2_API_KEY = (os.getenv("FREE_FALLBACK_2_API_KEY") or "").strip()
FREE_FALLBACK_2_BASE_URL = (os.getenv("FREE_FALLBACK_2_BASE_URL") or "https://openrouter.ai/api/v1/chat/completions").strip()
FREE_FALLBACK_2_MODELS = [
    m.strip()
    for m in (os.getenv("FREE_FALLBACK_2_MODELS") or os.getenv("FREE_FALLBACK_2_MODEL") or "openrouter/free").split(",")
    if m.strip()
]

FREE_FALLBACK_3_API_KEY = (os.getenv("FREE_FALLBACK_3_API_KEY") or "").strip()
FREE_FALLBACK_3_BASE_URL = (os.getenv("FREE_FALLBACK_3_BASE_URL") or "https://openrouter.ai/api/v1/chat/completions").strip()
FREE_FALLBACK_3_MODELS = [
    m.strip()
    for m in (os.getenv("FREE_FALLBACK_3_MODELS") or os.getenv("FREE_FALLBACK_3_MODEL") or "openrouter/free").split(",")
    if m.strip()
]

FREE_FALLBACK_4_API_KEY = (os.getenv("FREE_FALLBACK_4_API_KEY") or "").strip()
FREE_FALLBACK_4_BASE_URL = (os.getenv("FREE_FALLBACK_4_BASE_URL") or "https://openrouter.ai/api/v1/chat/completions").strip()
FREE_FALLBACK_4_MODELS = [
    m.strip()
    for m in (os.getenv("FREE_FALLBACK_4_MODELS") or os.getenv("FREE_FALLBACK_4_MODEL") or "openrouter/free").split(",")
    if m.strip()
]
OPENAI_API_KEY = (os.getenv("OPENAI_API_KEY") or "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()
AI_CONCURRENCY = max(1, int(os.getenv("AI_CONCURRENCY", "4")))
AI_CACHE_TTL = max(60, int(os.getenv("AI_CACHE_TTL", "86400")))
log = logging.getLogger("uvicorn.error")
AI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

YOOKASSA_SHOP_ID = (os.getenv("YOOKASSA_SHOP_ID") or "REPLACE_WITH_YOOKASSA_SHOP_ID").strip()
YOOKASSA_SECRET_KEY = (os.getenv("YOOKASSA_SECRET_KEY") or "REPLACE_WITH_YOOKASSA_SECRET_KEY").strip()
YOOKASSA_RETURN_URL = (
    os.getenv("YOOKASSA_RETURN_URL")
    or (f"{BASE_URL}/?yookassa_return=1" if BASE_URL else "https://t.me/snapeat_manager")
).strip()
YOOKASSA_API_BASE = "https://api.yookassa.ru/v3"
SUPPORT_HANDLE = "@snapeat_manager"
SUPPORT_URL = "https://t.me/snapeat_manager"
CHANNEL_HANDLE = "@snapeat_app"
CHANNEL_URL = "https://t.me/snapeat_app"

_ai_sem = asyncio.Semaphore(AI_CONCURRENCY)
_ai_cache: dict[str, tuple[float, object]] = {}
_ai_cache_lock = threading.Lock()
_admin_stats_cache: tuple[float, dict] | None = None


def _cache_get(key: str):
    now = time.time()
    with _ai_cache_lock:
        entry = _ai_cache.get(key)
        if not entry:
            return None
        exp, val = entry
        if now > exp:
            _ai_cache.pop(key, None)
            return None
        return val


def _cache_set(key: str, val):
    with _ai_cache_lock:
        if len(_ai_cache) > 2000:
            oldest_keys = sorted(_ai_cache.keys(), key=lambda k: _ai_cache[k][0])[:300]
            for ok in oldest_keys:
                _ai_cache.pop(ok, None)
        _ai_cache[key] = (time.time() + AI_CACHE_TTL, val)


FREE_MONTHLY_LIMIT = int(os.getenv("FREE_MONTHLY_AI_LIMIT", os.getenv("FREE_AI_LIMIT", "5")))
PRO_LIMIT = int(os.getenv("PRO_AI_LIMIT", "40"))
PRO_MONTHLY_REQUEST_CAP = int(os.getenv("PRO_MONTHLY_REQUEST_CAP", "100"))
FREE_MEALPLAN_LIMIT = max(0, int(os.getenv("FREE_MEALPLAN_LIMIT", "1")))
WH_SECRET = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")

CATALOG = {
    "pack_10": {
        "kind": "pack",
        "credits": 10,
        "price_rub": 99,
        "price_stars": 50,
        "price": 50,
        "title": "Пакет 10 запросов",
        "desc": "+10 ИИ-запросов на баланс. Не сгорают в конце месяца.",
    },
    "pack_30": {
        "kind": "pack",
        "credits": 30,
        "price_rub": 229,
        "price_stars": 115,
        "price": 115,
        "badge": "Популярный",
        "title": "Пакет 30 запросов",
        "desc": "+30 ИИ-запросов на баланс. Не сгорают в конце месяца.",
    },
    "sub_30d": {
        "kind": "pro",
        "days": 30,
        "monthly_cap": PRO_MONTHLY_REQUEST_CAP,
        "price_rub": 299,
        "price_stars": 150,
        "price": 150,
        "badge": "Основной",
        "title": "SnapEat Pro · 1 мес",
        "desc": (
            f"Подписка на 30 дней: до {PRO_LIMIT} ИИ-запросов в день "
            f"(лимит до {PRO_MONTHLY_REQUEST_CAP} в месяц), безлимитный ИИ-рацион, "
            "график веса, быстрый повтор приёмов пищи и аналитика."
        ),
    },
    "mealplan_1": {
        "kind": "mealplan",
        "credits": 1,
        "price_rub": 69,
        "price_stars": 35,
        "price": 35,
        "title": "ИИ-рацион под остаток КБЖУ",
        "desc": "Разовая генерация персонального меню под ваши цели, тренировки и пожелания. В Pro — безлимитно!",
    },
}
CATALOG_LIST = [{"id": k, **v} for k, v in CATALOG.items()]


def _pg_sql(sql: str) -> str:
    return sql.replace("?", "%s")


_pool: ConnectionPool | None = None
_pool_lock = threading.Lock()


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is not None and not _pool.closed:
        return _pool
    with _pool_lock:
        if _pool is None or _pool.closed:
            _pool = ConnectionPool(
                conninfo=DATABASE_URL,
                min_size=1,
                max_size=10,
                open=True,
                kwargs={"autocommit": False, "row_factory": dict_row, "connect_timeout": 10},
            )
    return _pool


def _close_pool():
    global _pool
    with _pool_lock:
        if _pool is not None and not _pool.closed:
            try:
                _pool.close()
            except Exception:
                pass
        _pool = None


def _run_in_conn(fn):
    pool = _get_pool()
    for attempt in range(2):
        try:
            with pool.connection() as conn:
                try:
                    res = fn(conn)
                    conn.commit()
                    return res
                except Exception:
                    try:
                        conn.rollback()
                    except Exception:
                        pass
                    raise
        except psycopg.OperationalError as e:
            log.warning("PostgreSQL operational error (attempt %d): %s", attempt + 1, _err(e))
            if attempt == 1:
                raise
            try:
                pool.check()
            except Exception:
                pass
            time.sleep(0.3)


def run(sql, args=()):
    q = _pg_sql(sql)

    def _op(conn):
        with conn.cursor() as cur:
            cur.execute(q, args)
            return [dict(r) for r in cur.fetchall()] if cur.description else []

    return _run_in_conn(_op)


def run_rc(sql, args=()):
    q = _pg_sql(sql)

    def _op(conn):
        with conn.cursor() as cur:
            cur.execute(q, args)
            return cur.rowcount

    return _run_in_conn(_op)


async def arun(sql, args=()):
    return await asyncio.to_thread(run, sql, args)


async def arun_rc(sql, args=()):
    return await asyncio.to_thread(run_rc, sql, args)


def init_db():
    run("""CREATE TABLE IF NOT EXISTS users(
        id BIGINT PRIMARY KEY,
        username TEXT DEFAULT '',
        name TEXT,
        sex TEXT,
        age INT,
        height DOUBLE PRECISION,
        weight DOUBLE PRECISION,
        act DOUBLE PRECISION,
        goal TEXT,
        kcal INT,
        p INT,
        f INT,
        c INT,
        pro_until BIGINT DEFAULT 0,
        ai_day TEXT DEFAULT '',
        ai_n INT DEFAULT 0,
        trial_month TEXT DEFAULT '',
        trial_used INT DEFAULT 0,
        pro_month TEXT DEFAULT '',
        pro_month_used INT DEFAULT 0,
        target_weight DOUBLE PRECISION,
        start_weight DOUBLE PRECISION,
        pace DOUBLE PRECISION DEFAULT 0.5,
        manual INT DEFAULT 0,
        extra_ai_credits INT DEFAULT 0,
        mealplan_credits INT DEFAULT 0,
        free_mealplan_used INT DEFAULT 0,
        workouts_per_week INT DEFAULT 2,
        workout_type TEXT DEFAULT 'mixed',
        food_pref TEXT DEFAULT 'balanced',
        ref_by BIGINT,
        ref_count INT DEFAULT 0,
        remind_hour INT DEFAULT 20,
        remind_on INT DEFAULT 1,
        last_reminded TEXT DEFAULT '',
        tz_offset INT DEFAULT 180,
        created_at BIGINT DEFAULT 0,
        last_seen BIGINT DEFAULT 0
    )""")
    run("ALTER TABLE users ADD COLUMN IF NOT EXISTS username TEXT DEFAULT ''")
    run("ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at BIGINT DEFAULT 0")
    run("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen BIGINT DEFAULT 0")
    run("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_month TEXT DEFAULT ''")
    run("ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_used INT DEFAULT 0")
    run("ALTER TABLE users ADD COLUMN IF NOT EXISTS pro_month TEXT DEFAULT ''")
    run("ALTER TABLE users ADD COLUMN IF NOT EXISTS pro_month_used INT DEFAULT 0")
    run("ALTER TABLE users ADD COLUMN IF NOT EXISTS free_mealplan_used INT DEFAULT 0")
    run("ALTER TABLE users ADD COLUMN IF NOT EXISTS workouts_per_week INT DEFAULT 2")
    run("ALTER TABLE users ADD COLUMN IF NOT EXISTS workout_type TEXT DEFAULT 'mixed'")
    run("ALTER TABLE users ADD COLUMN IF NOT EXISTS food_pref TEXT DEFAULT 'balanced'")
    run("CREATE INDEX IF NOT EXISTS ix_users_username ON users (LOWER(username))")

    run("""CREATE TABLE IF NOT EXISTS entries(
        id BIGSERIAL PRIMARY KEY,
        uid BIGINT,
        day TEXT,
        name TEXT,
        grams DOUBLE PRECISION,
        kcal DOUBLE PRECISION,
        p DOUBLE PRECISION,
        f DOUBLE PRECISION,
        c DOUBLE PRECISION,
        src TEXT,
        ts BIGINT,
        meal TEXT DEFAULT 'snack'
    )""")
    run("CREATE INDEX IF NOT EXISTS ix_entries ON entries(uid, day)")
    run("CREATE TABLE IF NOT EXISTS water(uid BIGINT, day TEXT, ml INT DEFAULT 0, PRIMARY KEY(uid, day))")
    run("""CREATE TABLE IF NOT EXISTS favs(
        id BIGSERIAL PRIMARY KEY,
        uid BIGINT,
        name TEXT,
        grams DOUBLE PRECISION,
        kcal DOUBLE PRECISION,
        p DOUBLE PRECISION,
        f DOUBLE PRECISION,
        c DOUBLE PRECISION
    )""")
    run("""CREATE TABLE IF NOT EXISTS payments(
        charge_id TEXT PRIMARY KEY,
        uid BIGINT,
        item_id TEXT,
        stars INT DEFAULT 0,
        amount_rub DOUBLE PRECISION DEFAULT 0,
        provider TEXT DEFAULT 'stars',
        status TEXT DEFAULT 'succeeded',
        metadata_json TEXT DEFAULT '{}',
        ts BIGINT
    )""")
    run("ALTER TABLE payments ADD COLUMN IF NOT EXISTS amount_rub DOUBLE PRECISION DEFAULT 0")
    run("ALTER TABLE payments ADD COLUMN IF NOT EXISTS provider TEXT DEFAULT 'stars'")
    run("ALTER TABLE payments ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'succeeded'")
    run("ALTER TABLE payments ADD COLUMN IF NOT EXISTS metadata_json TEXT DEFAULT '{}'")
    run("CREATE INDEX IF NOT EXISTS ix_payments_ts ON payments(ts DESC)")
    run("CREATE TABLE IF NOT EXISTS weights(uid BIGINT, day TEXT, weight DOUBLE PRECISION, PRIMARY KEY(uid, day))")

    run("""CREATE TABLE IF NOT EXISTS bot_logs(
        id BIGSERIAL PRIMARY KEY,
        ts BIGINT NOT NULL,
        level VARCHAR(10) NOT NULL DEFAULT 'INFO',
        uid BIGINT,
        event VARCHAR(64) NOT NULL,
        details VARCHAR(360) DEFAULT ''
    )""")
    run("CREATE INDEX IF NOT EXISTS ix_bot_logs_ts ON bot_logs(ts DESC)")
    run("CREATE INDEX IF NOT EXISTS ix_bot_logs_level_ts ON bot_logs(level, ts DESC)")

    run("""CREATE TABLE IF NOT EXISTS support_requests(
        id BIGSERIAL PRIMARY KEY,
        uid BIGINT NOT NULL,
        username TEXT DEFAULT '',
        user_name TEXT DEFAULT '',
        category VARCHAR(24) DEFAULT 'general',
        message VARCHAR(800) NOT NULL,
        status VARCHAR(20) DEFAULT 'new',
        admin_note VARCHAR(400) DEFAULT '',
        created_at BIGINT NOT NULL,
        updated_at BIGINT NOT NULL
    )""")
    run("CREATE INDEX IF NOT EXISTS ix_support_requests_status ON support_requests(status, created_at DESC)")


for _db_attempt in range(5):
    try:
        init_db()
        break
    except Exception as _db_err:
        if _db_attempt == 4:
            raise
        log.warning("Database init retry %d/5: %s", _db_attempt + 1, _db_err)
        time.sleep(2.0)


def _normalize_username(raw_username: str) -> str:
    u = re.sub(r"[^A-Za-z0-9_]", "", (raw_username or "").lstrip("@").strip())
    return u[:40]


def db_log(level: str, event: str, details: str = "", uid: int | None = None):
    try:
        lvl = (level or "INFO").upper()[:10]
        ev = (event or "event")[:64]
        det = _err(details)[:350]
        run(
            "INSERT INTO bot_logs(ts, level, uid, event, details) VALUES(?,?,?,?,?)",
            (int(time.time()), lvl, uid, ev, det),
        )
    except Exception:
        pass


def prune_old_logs():
    try:
        cutoff = int(time.time()) - LOG_RETENTION_DAYS * 86400
        deleted_old = run_rc("DELETE FROM bot_logs WHERE ts < ?", (cutoff,))
        deleted_cap = run_rc(
            "DELETE FROM bot_logs WHERE id IN ("
            "SELECT id FROM bot_logs ORDER BY ts DESC OFFSET ?"
            ")",
            (LOG_MAX_ROWS,),
        )
        if deleted_old or deleted_cap:
            log.info("Pruned bot_logs: old=%d, cap=%d", deleted_old, deleted_cap)
    except Exception as e:
        log.warning("prune_old_logs failed: %s", _err(e))


def _sanitize_name(raw_name: str, fallback: str = "Друг") -> str:
    cleaned = re.sub(r"\s+", " ", (raw_name or "").strip())
    if not cleaned:
        return fallback
    return cleaned[:60]


def get_user(uid, name="", ref_by=None, username=""):
    safe_name = _sanitize_name(name, "")
    clean_un = _normalize_username(username)
    now_ts = int(time.time())
    existing = run("SELECT * FROM users WHERE id=?", (uid,))
    if not existing:
        valid_ref = None
        if ref_by and ref_by != uid:
            ref_u = run("SELECT id FROM users WHERE id=?", (ref_by,))
            if ref_u:
                valid_ref = ref_by
        inserted = run_rc(
            "INSERT INTO users(id, username, name, extra_ai_credits, ref_by, created_at, last_seen) "
            "VALUES(?,?,?,?,?,?,?) ON CONFLICT (id) DO NOTHING",
            (uid, clean_un, safe_name, 5 if valid_ref else 0, valid_ref, now_ts, now_ts),
        )
        if inserted > 0:
            db_log("INFO", "user_registered", f"@{clean_un or '-'} {safe_name}".strip(), uid=uid)
        if valid_ref and inserted > 0:
            run(
                "UPDATE users SET extra_ai_credits=COALESCE(extra_ai_credits,0)+5, ref_count=COALESCE(ref_count,0)+1 WHERE id=?",
                (valid_ref,),
            )
    else:
        u0 = existing[0]
        updates = []
        params = []
        if safe_name and not (u0.get("name") or "").strip():
            updates.append("name=?")
            params.append(safe_name)
        if clean_un and clean_un != (u0.get("username") or ""):
            updates.append("username=?")
            params.append(clean_un)
        if now_ts - int(u0.get("last_seen") or 0) > 300:
            updates.append("last_seen=?")
            params.append(now_ts)
        if updates:
            params.append(uid)
            run(f"UPDATE users SET {', '.join(updates)} WHERE id=?", tuple(params))
    return run("SELECT * FROM users WHERE id=?", (uid,))[0]


def auth(init: str) -> dict:
    try:
        if not init or not isinstance(init, str):
            raise ValueError
        d = dict(parse_qsl(init, keep_blank_values=True))
        got = d.pop("hash")
        check = "\n".join(f"{k}={v}" for k, v in sorted(d.items()))
        key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(hmac.new(key, check.encode(), hashlib.sha256).hexdigest(), got):
            raise ValueError
        if time.time() - int(d["auth_date"]) > 172800:
            raise ValueError
        return json.loads(d["user"])
    except Exception:
        raise HTTPException(401, "Откройте приложение из Telegram")


def require_admin(init: str) -> dict:
    tu = auth(init)
    uid = int(tu.get("id") or 0)
    if not ADMIN_IDS or uid not in ADMIN_IDS:
        db_log("WARNING", "admin_access_denied", f"@{tu.get('username') or '-'}", uid=uid)
        raise HTTPException(403, "Доступ запрещён")
    return tu


def calc_norms(
    sex: str,
    age: int,
    height: float,
    weight: float,
    act: float,
    goal: str,
    pace: float = 0.5,
    workouts_per_week: int = 2,
    workout_type: str = "mixed",
    food_pref: str = "balanced",
):
    bmr = 10.0 * weight + 6.25 * height - 5.0 * age + (5.0 if sex == "m" else -161.0)
    workout_bonus = max(0, min(int(workouts_per_week or 0), 14)) * (
        28.0 if workout_type == "cardio" else 22.0 if workout_type == "strength" else 20.0
    )
    goal_mult = {"lose": -1.0, "keep": 0.0, "gain": 1.0}[goal]
    kcal = (bmr * act) + workout_bonus + (goal_mult * float(pace) * 1100.0)
    min_kcal = 1500.0 if sex == "m" else 1200.0
    kcal = max(kcal, min_kcal)

    protein_mult = {"lose": 2.0, "keep": 1.6, "gain": 1.85}[goal]
    if workout_type == "strength" or int(workouts_per_week or 0) >= 4:
        protein_mult += 0.15
    if food_pref == "high_protein":
        protein_mult += 0.2
    p = max(40.0, weight * protein_mult)

    fat_mult = 0.9
    if food_pref == "low_carb":
        fat_mult = 1.05
    elif food_pref == "low_sweet":
        fat_mult = 0.92
    f = max(25.0, weight * fat_mult)

    c = max((kcal - p * 4.0 - f * 9.0) / 4.0, 0.0)
    if food_pref == "low_carb":
        c = min(c, max(60.0, (kcal * 0.25) / 4.0))
        f = max(f, (kcal - p * 4.0 - c * 4.0) / 9.0)

    return {
        "kcal": round(kcal),
        "p": round(p),
        "f": round(f),
        "c": round(c),
    }


def water_goal(weight, workouts_per_week=0):
    base = (weight or 66) * 30.0
    extra = min(max(int(workouts_per_week or 0), 0), 10) * 100.0
    return int(min(max(round((base + extra) / 50.0) * 50, 1500), 4200))


FOODS = [
    ("Куриная грудка варёная", 165, 31, 3.6, 0, 150), ("Творог 5%", 121, 17, 5, 1.8, 150),
    ("Греческий йогурт", 60, 10, 0.4, 4, 200), ("Яйца варёные", 155, 13, 11, 1.1, 110),
    ("Тунец в собственном соку", 116, 26, 1, 0, 100), ("Лосось запечённый", 208, 20, 13, 0, 120),
    ("Говядина тушёная", 190, 27, 9, 0, 120), ("Гречка варёная", 110, 4, 1, 21, 200),
    ("Рис варёный", 130, 2.7, 0.3, 28, 200), ("Овсянка на воде", 71, 2.5, 1.5, 12, 250),
    ("Банан", 89, 1.1, 0.3, 23, 120), ("Яблоко", 52, 0.3, 0.2, 14, 180),
    ("Миндаль", 579, 21, 50, 22, 30), ("Авокадо", 160, 2, 15, 9, 100),
    ("Арахисовая паста", 588, 25, 50, 20, 20), ("Цельнозерновой хлеб", 247, 13, 4, 41, 60),
    ("Кефир 1%", 40, 3, 1, 4, 250), ("Огурцы и помидоры", 18, 0.9, 0.2, 3.5, 200),
    ("Протеиновый батончик", 340, 32, 11, 28, 60), ("Протеин сывороточный", 380, 76, 6, 7, 30),
    ("Сыр сулугуни", 290, 20, 22, 0, 40), ("Индейка филе запечённое", 135, 27, 2.5, 0, 150),
]
QUICK = {"Греческий йогурт", "Творог 5%", "Банан", "Яблоко", "Миндаль", "Авокадо", "Кефир 1%",
         "Тунец в собственном соку", "Арахисовая паста", "Огурцы и помидоры", "Протеиновый батончик", "Сыр сулугуни"}


def suggest(rem, mode="", seed=0):
    if rem["kcal"] < 70:
        return []
    rnd, out = random.Random(seed), []
    for name, k, p, f, c, port in FOODS:
        if (mode == "quick" and name not in QUICK) or (mode == "light" and k > 100):
            continue
        cap = min(rem["kcal"], 220) if mode == "light" else rem["kcal"]
        g = min(port, int(cap / k * 100 // 10 * 10))
        if g < 20:
            continue
        m = {"kcal": k * g / 100, "p": p * g / 100, "f": f * g / 100, "c": c * g / 100}
        gain = (6 if mode == "protein" else 3) * min(m["p"], max(rem["p"], 0)) \
             + min(m["f"], max(rem["f"], 0)) + min(m["c"], max(rem["c"], 0))
        over = 3 * max(m["p"] - rem["p"], 0) + 3 * max(m["f"] - rem["f"], 0) + 2 * max(m["c"] - rem["c"], 0)
        out.append((gain - over + (rnd.random() * 5 if seed else 0),
                    {"name": name, "grams": g, **{x: round(v, 1) for x, v in m.items()}}))
    out.sort(key=lambda t: -t[0])
    return [o for sc, o in out[:6] if sc > 0]


PROMPT_FOOD = (
    'Ты опытный нутрициолог. Определи блюда/продукты (или прочитай таблицу пищевой ценности на этикетке/упаковке, если на фото этикетка). '
    'Оцени вес порции и КБЖУ. Отвечай ТОЛЬКО валидным JSON без markdown: '
    '{"items":[{"name":"название","grams":число,"kcal":число,"p":число,"f":число,"c":число}]}. '
    'Значения — на всю съеденную порцию (или указанную в комментарии порцию), p/f/c в граммах. '
    'Если еды или данных нет — {"items":[]}. Названия по-русски.'
)

PROMPT_MEALPLAN = (
    'Ты персональный нутрициолог в стиле лучших приложений (Yazio, FatSecret, Lifesum). '
    'Составь вкусный, реалистичный и сбалансированный план питания из простых понятных продуктов с точными граммовками '
    'строго под целевые КБЖУ и пожелания пользователя (количество сладкого, продукты в холодильнике, исключения). '
    'Отвечай ТОЛЬКО валидным JSON без markdown: '
    '{"meals":[{"meal":"breakfast|lunch|dinner|snack","title":"Приём пищи","items":[{"name":"продукт/блюдо","grams":число,"kcal":число,"p":число,"f":число,"c":число}]}],"tip":"короткий совет на 1 предложение"}. '
    'Названия по-русски.'
)


class Item(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    grams: float = Field(0, ge=0, le=5000)
    kcal: float = Field(ge=0, le=5000)
    p: float = Field(0, ge=0, le=500)
    f: float = Field(0, ge=0, le=500)
    c: float = Field(0, ge=0, le=1000)

    @model_validator(mode="after")
    def _normalize_item(self):
        self.name = re.sub(r"\s+", " ", self.name.strip())
        if not self.name:
            raise ValueError("Название продукта не может быть пустым")
        self.grams = round(float(self.grams or 0), 1)
        self.p = round(float(self.p or 0), 1)
        self.f = round(float(self.f or 0), 1)
        self.c = round(float(self.c or 0), 1)
        k = float(self.kcal or 0)
        if k <= 0 and (self.p > 0 or self.f > 0 or self.c > 0):
            k = min(5000.0, self.p * 4.0 + self.f * 9.0 + self.c * 4.0)
        self.kcal = round(k, 1)
        return self


_ALL_SECRETS = (
    BOT_TOKEN,
    DATABASE_URL,
    AI_KEY,
    FREE_FALLBACK_API_KEY,
    FREE_FALLBACK_2_API_KEY,
    FREE_FALLBACK_3_API_KEY,
    FREE_FALLBACK_4_API_KEY,
    OPENAI_API_KEY,
    YOOKASSA_SECRET_KEY,
)


def _err(msg) -> str:
    msg = " ".join(str(msg).split())
    for secret in _ALL_SECRETS:
        if secret:
            msg = msg.replace(secret, "***")
    return msg[:240]


IMG_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 3_000_000


def split_image(image: str):
    if not image or not isinstance(image, str):
        raise HTTPException(400, "Некорректное изображение")
    raw = image.strip()
    mime = "image/jpeg"
    if raw.startswith("data:"):
        head, sep, b64_part = raw.partition(",")
        if not sep:
            raise HTTPException(400, "Неверный формат data URL изображения")
        m = head[5:].split(";")[0].strip().lower()
        m = "image/jpeg" if m == "image/jpg" else m
        if m not in IMG_MIME:
            raise HTTPException(400, "Поддерживаются только изображения JPEG, PNG, WEBP или GIF")
        mime = m
        raw = b64_part.strip()

    cleaned_b64 = re.sub(r"\s+", "", raw)
    if not cleaned_b64 or not re.fullmatch(r"[A-Za-z0-9+/=]+", cleaned_b64):
        raise HTTPException(400, "Некорректные base64-данные изображения")
    try:
        decoded = base64.b64decode(cleaned_b64, validate=True)
    except Exception:
        raise HTTPException(400, "Не удалось декодировать изображение")
    if not decoded or len(decoded) < 16:
        raise HTTPException(400, "Файл изображения слишком мал или повреждён")
    if len(decoded) > MAX_IMAGE_BYTES:
        raise HTTPException(400, "Размер изображения превышает допустимый лимит (3 МБ)")
    return mime, cleaned_b64


def _extract_json_obj(raw: str) -> dict:
    cleaned = re.sub(r"```(?:json)?", "", raw or "", flags=re.I).replace("```", "").strip()
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    m = re.search(r"\{.*\}", cleaned, re.S)
    if not m:
        raise ValueError("JSON object not found")
    parsed = json.loads(m.group(0))
    if not isinstance(parsed, dict):
        raise ValueError("JSON root is not an object")
    return parsed


def _model_candidates() -> list[str]:
    seen, out = set(), []
    for m in [AI_MODEL, *AI_FALLBACK_MODELS]:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


async def _call_openai_compatible(
    url: str,
    api_key: str,
    model: str,
    provider_name: str,
    system_prompt: str,
    parts: list,
    temp: float = 0.2,
    use_json_format: bool = True,
) -> str:
    if not api_key:
        raise RuntimeError(f"{provider_name}: ключ не задан")
    content = []
    for p in parts:
        if "text" in p:
            content.append({"type": "text", "text": p["text"]})
        elif "inline_data" in p:
            mime = p["inline_data"]["mime_type"]
            b64 = p["inline_data"]["data"]
            content.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}})

    payload = {
        "model": model,
        "temperature": temp,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
    }
    if use_json_format:
        payload["response_format"] = {"type": "json_object"}

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    if "openrouter.ai" in url:
        headers["HTTP-Referer"] = BASE_URL or "https://snapeat.app"
        headers["X-Title"] = "SnapEat"

    async with httpx.AsyncClient(timeout=45) as cl:
        for attempt in range(2):
            try:
                r = await cl.post(url, headers=headers, json=payload)
            except Exception as e:
                if attempt == 1:
                    raise RuntimeError(f"{provider_name} сеть: {type(e).__name__}")
                await asyncio.sleep(0.6)
                continue
            if r.status_code in (429, 500, 502, 503, 504) and attempt == 0:
                await asyncio.sleep(0.8)
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"{provider_name} {r.status_code}: {_err(r.text[:140])}")
            data = r.json()
            txt = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
            if not txt:
                raise RuntimeError(f"{provider_name} вернул пустой ответ")
            _extract_json_obj(txt)
            return txt
    raise RuntimeError(f"{provider_name}: исчерпаны попытки")


def _free_fallback_targets() -> list[tuple[str, str, str, str]]:
    slots = [
        ("Бесплатный резерв 1", FREE_FALLBACK_API_KEY, FREE_FALLBACK_BASE_URL, FREE_FALLBACK_MODELS),
        ("Бесплатный резерв 2", FREE_FALLBACK_2_API_KEY, FREE_FALLBACK_2_BASE_URL, FREE_FALLBACK_2_MODELS),
        ("Бесплатный резерв 3", FREE_FALLBACK_3_API_KEY, FREE_FALLBACK_3_BASE_URL, FREE_FALLBACK_3_MODELS),
        ("Бесплатный резерв 4", FREE_FALLBACK_4_API_KEY, FREE_FALLBACK_4_BASE_URL, FREE_FALLBACK_4_MODELS),
    ]
    targets = []
    for label, key, url, models in slots:
        if key and url and models:
            for m in models:
                targets.append((label, key, url, m))
    return targets


async def _call_free_fallback(system_prompt: str, parts: list, temp: float = 0.2) -> str:
    targets = _free_fallback_targets()
    if not targets:
        raise RuntimeError("Бесплатные резервные провайдеры не настроены")
    errs = []
    for label, key, url, model in targets:
        try:
            log.info("Trying free fallback [%s] model %s", label, model)
            return await _call_openai_compatible(
                url,
                key,
                model,
                f"{label} ({model})",
                system_prompt,
                parts,
                temp=temp,
                use_json_format=("openrouter.ai" not in url),
            )
        except Exception as e:
            errs.append(f"{label}/{model}: {_err(e)}")
            log.warning("Free fallback target failed [%s / %s]: %s", label, model, _err(e))
    raise RuntimeError(" | ".join(errs))


async def _call_openai_fallback(system_prompt: str, parts: list, temp: float = 0.2) -> str:
    return await _call_openai_compatible(
        OPENAI_CHAT_URL,
        OPENAI_API_KEY,
        OPENAI_MODEL,
        "OpenAI",
        system_prompt,
        parts,
        temp=temp,
        use_json_format=True,
    )


async def _call_gemini_raw(system_prompt: str, parts: list, schema: dict, temp: float = 0.2) -> str:
    has_free = bool(_free_fallback_targets())
    has_any_fallback = has_free or bool(OPENAI_API_KEY)
    if not AI_KEY and not has_any_fallback:
        raise HTTPException(503, "ИИ не настроен: задайте GEMINI_API_KEY, бесплатный резерв или OPENAI_API_KEY")

    body = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {
            "temperature": temp,
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
            "responseSchema": schema,
        },
    }
    modes = ["bearer", "query"] if AI_KEY.startswith("AQ.") else ["query"]
    models = _model_candidates() if AI_KEY else []
    primary_err = None
    last_err = "ИИ временно перегружен, попробуйте через минуту"

    async with _ai_sem:
        if models:
            async with httpx.AsyncClient(timeout=45) as cl:
                for model in models:
                    url = AI_URL.format(model)
                    for attempt in range(2):
                        r = None
                        for mode in modes:
                            headers, params = {"Content-Type": "application/json"}, {}
                            if mode == "bearer":
                                headers["Authorization"] = f"Bearer {AI_KEY}"
                            else:
                                params["key"] = AI_KEY
                            try:
                                r = await cl.post(url, headers=headers, params=params, json=body)
                            except Exception as e:
                                last_err = f"Gemini сеть ({model}): {type(e).__name__}"
                                if not primary_err:
                                    primary_err = last_err
                                log.warning("Gemini request failed (%s, %s): %s", model, mode, _err(e))
                                r = None
                                break
                            if r.status_code not in (401, 403):
                                break

                        if r is None:
                            if attempt == 0:
                                await asyncio.sleep(0.6)
                                continue
                            break

                        if r.status_code in (429, 500, 502, 503, 504):
                            try:
                                msg = r.json()["error"]["message"]
                            except Exception:
                                msg = r.text[:120]
                            err_txt = f"Gemini {r.status_code} ({model}): {_err(msg)}"
                            if not primary_err:
                                primary_err = err_txt
                            last_err = err_txt
                            log.warning("Transient %s on %s (attempt %d): %s", r.status_code, model, attempt + 1, _err(msg))
                            if attempt == 0:
                                await asyncio.sleep(0.8)
                                continue
                            break

                        if r.status_code == 404:
                            if not primary_err:
                                last_err = f"Модель {model} недоступна (404)"
                            log.warning("Model 404: %s", model)
                            break

                        if r.status_code >= 400:
                            try:
                                msg = r.json()["error"]["message"]
                            except Exception:
                                msg = r.text[:160]
                            err_txt = f"Gemini {r.status_code}: {_err(msg)}"
                            if not primary_err:
                                primary_err = err_txt
                            last_err = err_txt
                            log.error("Gemini HTTP %s (%s): %s", r.status_code, model, _err(r.text[:500]))
                            if has_any_fallback:
                                break
                            db_log("ERROR", "ai_http_error", err_txt)
                            raise HTTPException(502, _err(err_txt))

                        try:
                            data = r.json()
                            cands = data.get("candidates") or []
                            if not cands:
                                raise ValueError(f"no candidates: {data.get('promptFeedback')}")
                            raw = "".join(
                                p.get("text", "")
                                for p in (cands[0].get("content") or {}).get("parts", [])
                                if not p.get("thought")
                            )
                            if not raw.strip():
                                raise ValueError("empty text")
                            _extract_json_obj(raw)
                            return raw
                        except Exception as e:
                            err_txt = f"Gemini ответ ({model}): {_err(e)}"
                            if not primary_err:
                                primary_err = err_txt
                            last_err = err_txt
                            log.warning("Bad response payload from %s: %s", model, _err(e))
                            break

        if has_free:
            try:
                return await _call_free_fallback(system_prompt, parts, temp=temp)
            except Exception as fe:
                log.warning("All free fallbacks failed: %s", _err(fe))
                last_err = f"{primary_err or last_err} | бесплатные резервы: {_err(fe)}"

        if OPENAI_API_KEY:
            try:
                log.info("Falling back to OpenAI model %s", OPENAI_MODEL)
                return await _call_openai_fallback(system_prompt, parts, temp=temp)
            except Exception as oe:
                log.error("OpenAI fallback failed: %s", _err(oe))
                last_err = f"{last_err} | OpenAI резерв: {_err(oe)}"

    db_log("ERROR", "ai_all_failed", primary_err or last_err)
    raise HTTPException(502, _err(primary_err or last_err))


_inflight_ai: dict[str, asyncio.Future] = {}
_inflight_lock = threading.Lock()

COMMON_FOOD_100G = {
    "куриная грудка": ("Куриная грудка варёная", 165, 31.0, 3.6, 0.0),
    "курица": ("Куриная грудка варёная", 165, 31.0, 3.6, 0.0),
    "творог": ("Творог 5%", 121, 17.0, 5.0, 1.8),
    "творог 5%": ("Творог 5%", 121, 17.0, 5.0, 1.8),
    "греческий йогурт": ("Греческий йогурт", 60, 10.0, 0.4, 4.0),
    "яйцо": ("Яйцо варёное", 155, 13.0, 11.0, 1.1),
    "яйца": ("Яйца варёные", 155, 13.0, 11.0, 1.1),
    "тунец": ("Тунец в собственном соку", 116, 26.0, 1.0, 0.0),
    "лосось": ("Лосось запечённый", 208, 20.0, 13.0, 0.0),
    "говядина": ("Говядина тушёная", 190, 27.0, 9.0, 0.0),
    "гречка": ("Гречка варёная", 110, 4.0, 1.0, 21.0),
    "рис": ("Рис варёный", 130, 2.7, 0.3, 28.0),
    "овсянка": ("Овсянка на воде", 71, 2.5, 1.5, 12.0),
    "банан": ("Банан", 89, 1.1, 0.3, 23.0),
    "яблоко": ("Яблоко", 52, 0.3, 0.2, 14.0),
    "миндаль": ("Миндаль", 579, 21.0, 50.0, 22.0),
    "авокадо": ("Авокадо", 160, 2.0, 15.0, 9.0),
    "кефир": ("Кефир 1%", 40, 3.0, 1.0, 4.0),
    "хлеб": ("Цельнозерновой хлеб", 247, 13.0, 4.0, 41.0),
    "цельнозерновой хлеб": ("Цельнозерновой хлеб", 247, 13.0, 4.0, 41.0),
    "индейка": ("Индейка филе запечённое", 135, 27.0, 2.5, 0.0),
    "сыр": ("Сыр твёрдый", 350, 25.0, 27.0, 0.0),
}


def _try_local_food_parse(text: str, image: str, label_mode: bool):
    if image or label_mode or not text:
        return None
    s = re.sub(r"\s+", " ", text.strip().lower().replace(",", "."))
    m = re.fullmatch(r"([а-яёa-z0-9\s%_-]{2,60}?)\s+(\d{1,4}(?:\.\d)?)\s*(?:г|гр|g)?", s)
    if not m:
        return None
    prod_key, grams_str = m.group(1).strip(), m.group(2)
    try:
        grams = float(grams_str)
    except ValueError:
        return None
    if grams <= 0 or grams > 3000:
        return None
    base = COMMON_FOOD_100G.get(prod_key)
    if not base:
        return None
    title, k100, p100, f100, c100 = base
    ratio = grams / 100.0
    return [{
        "name": title,
        "grams": round(grams, 1),
        "kcal": round(k100 * ratio, 1),
        "p": round(p100 * ratio, 1),
        "f": round(f100 * ratio, 1),
        "c": round(c100 * ratio, 1),
    }]


async def _run_singleflight(key: str, coro_factory):
    loop = asyncio.get_running_loop()
    with _inflight_lock:
        existing_fut = _inflight_ai.get(key)
        if existing_fut and not existing_fut.done():
            wait_for_shared = True
        else:
            existing_fut = loop.create_future()
            _inflight_ai[key] = existing_fut
            wait_for_shared = False

    if wait_for_shared:
        return await asyncio.shield(existing_fut)

    try:
        res = await coro_factory()
        if not existing_fut.done():
            existing_fut.set_result(res)
        return res
    except BaseException as e:
        if not existing_fut.done():
            existing_fut.set_exception(e)
        raise
    finally:
        with _inflight_lock:
            if _inflight_ai.get(key) is existing_fut:
                _inflight_ai.pop(key, None)


async def ask_ai(text: str, image: str, label_mode: bool = False):
    prepared_img = split_image(image) if image else None
    img_hash = hashlib.sha256(prepared_img[1].encode()).hexdigest()[:16] if prepared_img else ""
    norm_q = re.sub(r"\s+", " ", (text or "").strip().lower())
    cache_key = f"food:{int(label_mode)}:{img_hash}:{norm_q}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return [dict(x) for x in cached]

    local_hit = _try_local_food_parse(text, image, label_mode)
    if local_hit is not None:
        _cache_set(cache_key, local_hit)
        return [dict(x) for x in local_hit]

    async def _compute():
        parts = []
        if prepared_img:
            mime, b64 = prepared_img
            parts.append({"inline_data": {"mime_type": mime, "data": b64}})
        default_prompt = (
            "Прочитай этикетку/таблицу КБЖУ на упаковке и пересчитай на указанную порцию (если вес не указан — на 100 г)."
            if label_mode else "Что на фото? Оцени порцию и КБЖУ."
        )
        parts.append({"text": text.strip() or default_prompt})

        num = {"type": "NUMBER"}
        schema = {
            "type": "OBJECT",
            "required": ["items"],
            "properties": {
                "items": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "required": ["name", "grams", "kcal", "p", "f", "c"],
                        "properties": {
                            "name": {"type": "STRING"},
                            "grams": num,
                            "kcal": num,
                            "p": num,
                            "f": num,
                            "c": num,
                        },
                    },
                }
            },
        }
        raw = await _call_gemini_raw(PROMPT_FOOD, parts, schema, temp=0.2)
        try:
            obj = _extract_json_obj(raw)
            items = obj.get("items") or []
            parsed = [Item(**i).model_dump() for i in items if isinstance(i, dict)][:15]
            _cache_set(cache_key, parsed)
            return parsed
        except Exception as e:
            log.error("AI bad JSON (%s): %s", type(e).__name__, _err(raw[:1000]))
            db_log("ERROR", "ai_bad_json", f"{type(e).__name__}: {raw[:180]}")
            raise HTTPException(502, _err(f"Не удалось разобрать ответ ИИ: {raw}"))

    res = await _run_singleflight(cache_key, _compute)
    return [dict(x) for x in res]


async def ask_mealplan_ai(rem: dict, prefs: str = "", user_ctx: dict | None = None):
    ctx = user_ctx or {}
    clean_prefs = re.sub(r"\s+", " ", (prefs or "").strip())[:300]
    norm_rem = {
        "kcal": max(round(float(rem.get("kcal") or 0) / 25) * 25, 200),
        "p": max(round(float(rem.get("p") or 0) / 5) * 5, 10),
        "f": max(round(float(rem.get("f") or 0) / 5) * 5, 5),
        "c": max(round(float(rem.get("c") or 0) / 5) * 5, 10),
    }
    cache_key = "mealplan:" + hashlib.sha256(
        json.dumps({"r": norm_rem, "p": clean_prefs.lower(), "c": ctx}, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:20]
    cached = _cache_get(cache_key)
    if cached is not None:
        return json.loads(json.dumps(cached))

    async def _compute():
        num = {"type": "NUMBER"}
        item_schema = {
            "type": "OBJECT",
            "required": ["name", "grams", "kcal", "p", "f", "c"],
            "properties": {"name": {"type": "STRING"}, "grams": num, "kcal": num, "p": num, "f": num, "c": num},
        }
        schema = {
            "type": "OBJECT",
            "required": ["meals", "tip"],
            "properties": {
                "meals": {
                    "type": "ARRAY",
                    "items": {
                        "type": "OBJECT",
                        "required": ["meal", "title", "items"],
                        "properties": {
                            "meal": {"type": "STRING"},
                            "title": {"type": "STRING"},
                            "items": {"type": "ARRAY", "items": item_schema},
                        },
                    },
                },
                "tip": {"type": "STRING"},
            },
        }
        ctx_parts = []
        if ctx.get("goal"):
            ctx_parts.append(f"цель: {ctx['goal']}")
        if ctx.get("workouts_per_week") is not None:
            ctx_parts.append(f"тренировок в неделю: {ctx['workouts_per_week']} ({ctx.get('workout_type') or 'mixed'})")
        if ctx.get("food_pref"):
            ctx_parts.append(f"предпочтение по рациону: {ctx['food_pref']}")
        ctx_line = f"Контекст пользователя: {', '.join(ctx_parts)}. " if ctx_parts else ""

        prompt = (
            f"Составь меню на остаток дня (или на весь день, если ещё ничего не съедено). "
            f"Целевые показатели: {norm_rem['kcal']} ккал, Белки {norm_rem['p']} г, "
            f"Жиры {norm_rem['f']} г, Углеводы {norm_rem['c']} г. "
            f"{ctx_line}"
            f"Пожелания пользователя (учти строго, включая побольше/поменьше сладкого и продукты в холодильнике): "
            f"{clean_prefs or 'обычные доступные продукты, сбалансированный вкус, без экзотики'}. "
            f"В поле meal используй строго одно из значений: breakfast, lunch, dinner, snack."
        )
        raw = await _call_gemini_raw(PROMPT_MEALPLAN, [{"text": prompt}], schema, temp=0.35)
        try:
            data = _extract_json_obj(raw)
            meals = []
            for ml in (data.get("meals") or [])[:4]:
                if not isinstance(ml, dict):
                    continue
                m_key = ml.get("meal") if ml.get("meal") in MEALS else "snack"
                valid_items = []
                for it in (ml.get("items") or [])[:6]:
                    if isinstance(it, dict):
                        try:
                            valid_items.append(Item(**it).model_dump())
                        except Exception:
                            continue
                if valid_items:
                    meals.append({"meal": m_key, "title": str(ml.get("title") or m_key)[:60], "items": valid_items})
            if not meals:
                raise ValueError("No valid meals generated")
            res = {"meals": meals, "tip": str(data.get("tip") or "")[:240]}
            _cache_set(cache_key, res)
            return res
        except Exception as e:
            log.error("Mealplan JSON error: %s", _err(e))
            db_log("ERROR", "mealplan_bad_json", _err(e))
            raise HTTPException(502, "Не удалось сгенерировать рацион, попробуйте ещё раз")

    res = await _run_singleflight(cache_key, _compute)
    return json.loads(json.dumps(res))


async def ask_ai_norms_adjustment(profile_dict: dict, base_norms: dict) -> dict | None:
    has_ai = bool(AI_KEY or _free_fallback_targets() or OPENAI_API_KEY)
    if not has_ai:
        return None
    cache_key = "norms:" + hashlib.sha256(
        json.dumps({"p": profile_dict, "n": base_norms}, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:20]
    cached = _cache_get(cache_key)
    if cached is not None:
        return dict(cached)

    schema = {
        "type": "OBJECT",
        "required": ["kcal", "p", "f", "c", "note"],
        "properties": {
            "kcal": {"type": "NUMBER"},
            "p": {"type": "NUMBER"},
            "f": {"type": "NUMBER"},
            "c": {"type": "NUMBER"},
            "note": {"type": "STRING"},
        },
    }
    sys_p = (
        "Ты спортивный нутрициолог. Проверь базовую норму КБЖУ и при необходимости сделай тонкую корректировку "
        "(в пределах ±12% от базовых ккал и ±15% от макросов) под возраст, тренировки и цель пользователя. "
        "Отвечай ТОЛЬКО JSON."
    )
    usr_p = (
        f"Профиль: {json.dumps(profile_dict, ensure_ascii=False)}. "
        f"Базовый расчёт: {json.dumps(base_norms, ensure_ascii=False)}."
    )
    try:
        raw = await asyncio.wait_for(
            _call_gemini_raw(sys_p, [{"text": usr_p}], schema, temp=0.2),
            timeout=10.0,
        )
        data = _extract_json_obj(raw)
        min_kcal = 1500 if profile_dict.get("sex") == "m" else 1200
        low_k = max(min_kcal, round(base_norms["kcal"] * 0.88))
        high_k = min(10000, round(base_norms["kcal"] * 1.12))
        kcal = max(low_k, min(high_k, round(float(data.get("kcal", base_norms["kcal"])))))

        p = max(30, min(600, round(float(data.get("p", base_norms["p"])))))
        f = max(20, min(600, round(float(data.get("f", base_norms["f"])))))
        c = max(0, min(1500, round(float(data.get("c", base_norms["c"])))))

        res = {"kcal": kcal, "p": p, "f": f, "c": c, "note": str(data.get("note") or "")[:180]}
        _cache_set(cache_key, res)
        return dict(res)
    except Exception as e:
        log.info("AI norm refinement skipped/fallback: %s", _err(e))
        return None


def _current_month_str() -> str:
    return date.today().strftime("%Y-%m")


def ai_status(u):
    now_ts = time.time()
    cur_month = _current_month_str()
    today_str = date.today().isoformat()
    pro = (u.get("pro_until") or 0) > now_ts

    trial_used = (u.get("trial_used") or 0) if (u.get("trial_month") or "") == cur_month else 0
    trial_remaining = max(FREE_MONTHLY_LIMIT - trial_used, 0)

    used_today = (u.get("ai_n") or 0) if (u.get("ai_day") or "") == today_str else 0
    pro_month_used = (u.get("pro_month_used") or 0) if (u.get("pro_month") or "") == cur_month else 0
    pro_month_remaining = max(PRO_MONTHLY_REQUEST_CAP - pro_month_used, 0) if pro else 0

    free_mp_used = int(u.get("free_mealplan_used") or 0)
    free_mp_remaining = max(FREE_MEALPLAN_LIMIT - free_mp_used, 0)

    daily_limit = PRO_LIMIT if pro else FREE_MONTHLY_LIMIT
    remaining_daily_today = min(max(PRO_LIMIT - used_today, 0), pro_month_remaining) if pro else trial_remaining
    extra_credits = int(u.get("extra_ai_credits") or 0)

    return {
        "pro": pro,
        "is_pro": pro,
        "trial_monthly_limit": FREE_MONTHLY_LIMIT,
        "trial_month": cur_month,
        "trial_used": trial_used,
        "trial_remaining": trial_remaining,
        "trial_exhausted": (not pro) and (trial_remaining <= 0) and (extra_credits <= 0),
        "used": trial_used if not pro else used_today,
        "used_today": used_today,
        "free_daily_limit": FREE_MONTHLY_LIMIT,
        "daily_limit": daily_limit,
        "remaining_daily_today": remaining_daily_today,
        "pro_monthly_cap": PRO_MONTHLY_REQUEST_CAP,
        "pro_month_used": pro_month_used,
        "pro_month_remaining": pro_month_remaining,
        "extra_ai_credits": extra_credits,
        "mealplan_credits": int(u.get("mealplan_credits") or 0),
        "free_mealplan_limit": FREE_MEALPLAN_LIMIT,
        "free_mealplan_used": free_mp_used,
        "free_mealplan_remaining": free_mp_remaining,
        "pro_until": int(u.get("pro_until") or 0),
        "support_handle": SUPPORT_HANDLE,
        "support_url": SUPPORT_URL,
    }


def consume_ai_quota(uid: int) -> tuple[str, str]:
    cur_month = _current_month_str()
    today_str = date.today().isoformat()
    now_ts = int(time.time())

    def _op(conn):
        with conn.cursor() as cur:
            cur.execute(
                _pg_sql(
                    "SELECT pro_until, trial_month, trial_used, ai_day, ai_n, pro_month, pro_month_used, extra_ai_credits "
                    "FROM users WHERE id=? FOR UPDATE"
                ),
                (uid,),
            )
            row = cur.fetchone()
            if not row:
                return "", ""

            is_pro = (row.get("pro_until") or 0) > now_ts
            t_month = row.get("trial_month") or ""
            t_used = (row.get("trial_used") or 0) if t_month == cur_month else 0

            a_day = row.get("ai_day") or ""
            a_n = (row.get("ai_n") or 0) if a_day == today_str else 0

            p_month = row.get("pro_month") or ""
            p_used = (row.get("pro_month_used") or 0) if p_month == cur_month else 0

            extra = int(row.get("extra_ai_credits") or 0)

            if is_pro and a_n < PRO_LIMIT and p_used < PRO_MONTHLY_REQUEST_CAP:
                cur.execute(
                    _pg_sql("UPDATE users SET ai_day=?, ai_n=?, pro_month=?, pro_month_used=? WHERE id=?"),
                    (today_str, a_n + 1, cur_month, p_used + 1, uid),
                )
                return "pro", cur_month

            if (not is_pro) and t_used < FREE_MONTHLY_LIMIT:
                cur.execute(
                    _pg_sql("UPDATE users SET trial_month=?, trial_used=?, ai_day=?, ai_n=? WHERE id=?"),
                    (cur_month, t_used + 1, today_str, a_n + 1, uid),
                )
                return "trial", cur_month

            if extra > 0:
                cur.execute(
                    _pg_sql("UPDATE users SET extra_ai_credits=?, ai_day=?, ai_n=? WHERE id=?"),
                    (extra - 1, today_str, a_n + 1, uid),
                )
                return "extra", cur_month

            return "", cur_month

    return _run_in_conn(_op)


def refund_ai_quota(uid: int, source: str, period_month: str):
    if not source:
        return
    today_str = date.today().isoformat()

    def _op(conn):
        with conn.cursor() as cur:
            if source == "pro":
                cur.execute(
                    _pg_sql("UPDATE users SET ai_n=GREATEST(COALESCE(ai_n,0)-1,0) WHERE id=? AND ai_day=?"),
                    (uid, today_str),
                )
                if period_month:
                    cur.execute(
                        _pg_sql("UPDATE users SET pro_month_used=GREATEST(COALESCE(pro_month_used,0)-1,0) WHERE id=? AND pro_month=?"),
                        (uid, period_month),
                    )
            elif source == "trial":
                cur.execute(
                    _pg_sql("UPDATE users SET ai_n=GREATEST(COALESCE(ai_n,0)-1,0) WHERE id=? AND ai_day=?"),
                    (uid, today_str),
                )
                if period_month:
                    cur.execute(
                        _pg_sql("UPDATE users SET trial_used=GREATEST(COALESCE(trial_used,0)-1,0) WHERE id=? AND trial_month=?"),
                        (uid, period_month),
                    )
            elif source == "extra":
                cur.execute(
                    _pg_sql("UPDATE users SET extra_ai_credits=COALESCE(extra_ai_credits,0)+1, ai_n=GREATEST(COALESCE(ai_n,0)-1,0) WHERE id=?"),
                    (uid,),
                )

    _run_in_conn(_op)


def consume_mealplan_quota(uid: int) -> str:
    now_ts = int(time.time())

    def _op(conn):
        with conn.cursor() as cur:
            cur.execute(
                _pg_sql(
                    "SELECT pro_until, mealplan_credits, free_mealplan_used "
                    "FROM users WHERE id=? FOR UPDATE"
                ),
                (uid,),
            )
            row = cur.fetchone()
            if not row:
                return ""
            is_pro = (row.get("pro_until") or 0) > now_ts
            if is_pro:
                return "pro"
            free_used = int(row.get("free_mealplan_used") or 0)
            if free_used < FREE_MEALPLAN_LIMIT:
                cur.execute(
                    _pg_sql("UPDATE users SET free_mealplan_used=? WHERE id=?"),
                    (free_used + 1, uid),
                )
                return "free_trial"
            tickets = int(row.get("mealplan_credits") or 0)
            if tickets > 0:
                cur.execute(
                    _pg_sql("UPDATE users SET mealplan_credits=? WHERE id=?"),
                    (tickets - 1, uid),
                )
                return "ticket"
            return ""

    return _run_in_conn(_op)


def refund_mealplan_quota(uid: int, source: str):
    if source == "free_trial":
        run(
            "UPDATE users SET free_mealplan_used=GREATEST(COALESCE(free_mealplan_used,0)-1,0) WHERE id=?",
            (uid,),
        )
    elif source == "ticket":
        run(
            "UPDATE users SET mealplan_credits=COALESCE(mealplan_credits,0)+1 WHERE id=?",
            (uid,),
        )


bot = Bot(BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def on_start(m: Message):
    ref_id = None
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) > 1 and parts[1].startswith("ref_"):
        try:
            ref_id = int(parts[1][4:])
        except ValueError:
            pass
    await asyncio.to_thread(
        get_user,
        m.from_user.id,
        m.from_user.full_name,
        ref_id,
        m.from_user.username or "",
    )
    rows_kb = []
    if BASE_URL:
        rows_kb.append([InlineKeyboardButton(text="🥗 Открыть дневник", web_app=WebAppInfo(url=BASE_URL))])
        if m.from_user.id in ADMIN_IDS:
            rows_kb.append([InlineKeyboardButton(text="🛡️ Админ-панель", web_app=WebAppInfo(url=f"{BASE_URL}/admin"))])
    rows_kb.append([InlineKeyboardButton(text="📢 Канал SnapEat", url=CHANNEL_URL)])
    rows_kb.append([InlineKeyboardButton(text=f"💬 Поддержка ({SUPPORT_HANDLE})", url=SUPPORT_URL)])
    kb = InlineKeyboardMarkup(inline_keyboard=rows_kb)
    bonus_txt = "\n🎁 По приглашению друга тебе уже начислено +5 бонусных ИИ-запросов!" if ref_id else ""
    await m.answer(
        "Привет! Я <b>SnapEat</b> — умный дневник питания и КБЖУ.\n\n"
        "📸 Фотографируй еду или этикетки, пиши текстом или выбирай из базы — я сам посчитаю калории, белки, жиры и углеводы.\n"
        f"Бесплатный пробный лимит — <b>{FREE_MONTHLY_LIMIT} ИИ-запросов в месяц</b> и <b>{FREE_MEALPLAN_LIMIT} бесплатный рацион</b>.\n\n"
        f"📢 Хочешь следить за новостями, обновлениями и продвижением проекта? Подпишись на канал (по желанию): <a href=\"{CHANNEL_URL}\">{CHANNEL_HANDLE}</a>\n"
        f"💬 Вопросы и оплата: {SUPPORT_HANDLE}"
        f"{bonus_txt}",
        parse_mode="HTML",
        disable_web_page_preview=True,
        reply_markup=kb,
    )


@dp.message(Command("admin"))
async def on_admin_cmd(m: Message):
    if m.from_user.id not in ADMIN_IDS:
        return
    if not BASE_URL:
        await m.answer("⚠️ WEBAPP_URL не задан в переменных окружения.")
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛡️ Открыть админ-панель", web_app=WebAppInfo(url=f"{BASE_URL}/admin"))
    ]])
    await m.answer("Панель управления SnapEat:", reply_markup=kb)


@dp.message(Command("request"))
async def on_request_cmd(m: Message):
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await m.answer("Напишите обращение после команды, например:\n<code>/request Не пришла оплата</code>", parse_mode="HTML")
        return
    u = await asyncio.to_thread(get_user, m.from_user.id, m.from_user.full_name, None, m.from_user.username or "")
    msg_txt = re.sub(r"\s+", " ", parts[1].strip())[:800]
    now_ts = int(time.time())
    await arun(
        "INSERT INTO support_requests(uid, username, user_name, category, message, status, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (u["id"], u.get("username") or "", u.get("name") or "", "bot", msg_txt, "new", now_ts, now_ts),
    )
    await asyncio.to_thread(db_log, "INFO", "support_request", f"@{u.get('username') or '-'}: {msg_txt[:120]}", u["id"])
    await m.answer("✅ Ваше обращение отправлено администратору. Мы ответим вам в этом чате!")


def parse_payload(payload):
    try:
        uid, item, _nonce = payload.split(":")
        return (int(uid), item) if item in CATALOG else (None, None)
    except Exception:
        return None, None


def credit_payment(uid, item_id, charge_id, stars=0, amount_rub=0.0, provider="stars", status="succeeded", metadata=None) -> bool:
    if item_id not in CATALOG:
        return False
    it = CATALOG[item_id]
    meta_str = json.dumps(metadata or {}, ensure_ascii=False)
    now_ts = int(time.time())

    def _op(conn):
        with conn.cursor() as cur:
            cur.execute(
                _pg_sql(
                    "INSERT INTO payments(charge_id,uid,item_id,stars,amount_rub,provider,status,metadata_json,ts) "
                    "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT (charge_id) DO NOTHING"
                ),
                (charge_id, uid, item_id, int(stars or 0), float(amount_rub or 0.0), provider, status, meta_str, now_ts),
            )
            if cur.rowcount == 0:
                return False
            if it["kind"] == "pack":
                cur.execute(
                    _pg_sql("UPDATE users SET extra_ai_credits=COALESCE(extra_ai_credits,0)+? WHERE id=?"),
                    (it["credits"], uid),
                )
            elif it["kind"] == "mealplan":
                cur.execute(
                    _pg_sql("UPDATE users SET mealplan_credits=COALESCE(mealplan_credits,0)+? WHERE id=?"),
                    (it["credits"], uid),
                )
            else:
                cur.execute(_pg_sql("SELECT pro_until FROM users WHERE id=?"), (uid,))
                row = cur.fetchone()
                cur_until = int((row["pro_until"] if row else 0) or 0)
                until = int(max(time.time(), cur_until) + it["days"] * 86400)
                cur.execute(_pg_sql("UPDATE users SET pro_until=? WHERE id=?"), (until, uid))
            return True

    ok = bool(_run_in_conn(_op))
    if ok:
        global _admin_stats_cache
        _admin_stats_cache = None
        db_log("INFO", "payment_credited", f"{item_id} via {provider} ({stars}⭐ / {amount_rub}₽)", uid=uid)
    return ok


@dp.pre_checkout_query()
async def on_pre_checkout(q: PreCheckoutQuery):
    uid, item = parse_payload(q.invoice_payload)
    expected_stars = CATALOG[item]["price_stars"] if item else None
    ok = bool(item) and uid == q.from_user.id and q.currency == "XTR" and q.total_amount == expected_stars
    if ok:
        await q.answer(ok=True)
    else:
        log.warning("pre_checkout rejected: payload=%s currency=%s amount=%s", q.invoice_payload, q.currency, q.total_amount)
        await asyncio.to_thread(db_log, "WARNING", "pre_checkout_rejected", f"payload={q.invoice_payload} amount={q.total_amount}", q.from_user.id)
        await q.answer(ok=False, error_message="Не удалось проверить заказ. Откройте оплату в приложении заново.")


@dp.message(F.successful_payment)
async def on_paid(m: Message):
    sp = m.successful_payment
    uid, item = parse_payload(sp.invoice_payload)
    expected_stars = CATALOG[item]["price_stars"] if item else None
    if not item or uid != m.from_user.id or sp.currency != "XTR" or sp.total_amount != expected_stars:
        log.error("bad successful_payment: payload=%s currency=%s amount=%s", sp.invoice_payload, sp.currency, sp.total_amount)
        await asyncio.to_thread(db_log, "ERROR", "bad_stars_payment", f"payload={sp.invoice_payload}", m.from_user.id)
        return
    await asyncio.to_thread(get_user, uid, m.from_user.full_name, None, m.from_user.username or "")
    credited = await asyncio.to_thread(credit_payment, uid, item, sp.telegram_payment_charge_id, sp.total_amount, 0.0, "stars")
    if not credited:
        return
    u = await asyncio.to_thread(get_user, uid)
    it = CATALOG[item]
    if it["kind"] == "pack":
        text = f"⭐ Начислено +{it['credits']} ИИ-запросов. На балансе: {u['extra_ai_credits']}."
    elif it["kind"] == "mealplan":
        text = "🥗 Оплачен персональный ИИ-рацион! Открой вкладку «Рацион» в приложении, чтобы сгенерировать меню."
    else:
        text = f"👑 SnapEat Pro активен до {datetime.fromtimestamp(u['pro_until'], tz=timezone.utc):%d.%m.%Y}. Спасибо!"
    try:
        await m.answer(text)
    except Exception as e:
        log.warning("payment credited, but notify failed: %s", _err(e))


async def _ping_self():
    keepalive_url = (os.getenv("KEEPALIVE_URL") or "").strip() or (f"{BASE_URL}/health" if BASE_URL else "")
    if not keepalive_url:
        return
    try:
        async with httpx.AsyncClient(timeout=15) as cl:
            r = await cl.get(keepalive_url)
            log.debug("Keepalive ping %s -> %s", keepalive_url, r.status_code)
            if r.status_code >= 400:
                log.warning("Keepalive ping returned %s for %s", r.status_code, keepalive_url)
    except Exception as e:
        log.warning("Keepalive ping failed: %s", _err(e))


async def reminder_loop():
    keepalive_enabled = (os.getenv("KEEPALIVE_ENABLED", "1") or "1").strip().lower() not in ("0", "false", "no", "off")
    last_prune_ts = 0
    while True:
        await asyncio.sleep(600)
        now_s = int(time.time())
        if now_s - last_prune_ts >= 21600:
            await asyncio.to_thread(prune_old_logs)
            last_prune_ts = now_s
        if keepalive_enabled:
            await _ping_self()
        if not BASE_URL:
            continue
        try:
            now_utc = datetime.now(timezone.utc)
            users = await arun("SELECT id, name, kcal, remind_hour, remind_on, last_reminded, tz_offset FROM users WHERE remind_on=1 AND kcal IS NOT NULL")
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🥗 Записать приём пищи", web_app=WebAppInfo(url=BASE_URL))
            ]])
            for u in users:
                local_dt = now_utc + timedelta(minutes=u["tz_offset"] or 180)
                local_day = local_dt.date().isoformat()
                if u["last_reminded"] == local_day:
                    continue
                if local_dt.hour != (u["remind_hour"] if u["remind_hour"] is not None else 20):
                    continue
                _, tot = await asyncio.to_thread(day_totals, u["id"], local_day)
                rem_kcal = round((u["kcal"] or 0) - tot["kcal"])
                await arun("UPDATE users SET last_reminded=? WHERE id=?", (local_day, u["id"]))
                if tot["kcal"] == 0:
                    msg = "👋 Привет! Ты ещё не записал(а) еду за сегодня. Загляни в дневник, чтобы не прервать серию!"
                elif rem_kcal > 150:
                    msg = f"🍽 До дневной цели осталось <b>{rem_kcal} ккал</b>. Не забудь записать ужин или перекус!"
                else:
                    continue
                try:
                    await bot.send_message(u["id"], msg, parse_mode="HTML", reply_markup=kb)
                except Exception:
                    pass
        except Exception as e:
            log.warning("reminder_loop error: %s", _err(e))


ALLOWED_UPDATES = ["message", "pre_checkout_query"]


@asynccontextmanager
async def lifespan(app):
    global BOT_USERNAME
    await asyncio.to_thread(prune_old_logs)
    if not BOT_USERNAME:
        try:
            me = await bot.get_me()
            BOT_USERNAME = me.username or ""
        except Exception:
            pass
    if BASE_URL:
        try:
            await bot.set_webhook(f"{BASE_URL}/webhook/{WH_SECRET}", secret_token=WH_SECRET,
                                  allowed_updates=ALLOWED_UPDATES)
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Дневник", web_app=WebAppInfo(url=BASE_URL)))
        except Exception as e:
            log.error("webhook setup failed: %s", _err(e))
    task = asyncio.create_task(reminder_loop())
    yield
    task.cancel()
    _close_pool()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, HTTPException):
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    log.error("Unhandled error on %s %s: %s", request.method, request.url.path, _err(exc))
    await asyncio.to_thread(db_log, "ERROR", "unhandled_http_error", f"{request.method} {request.url.path}: {_err(exc)}")
    return JSONResponse(status_code=500, content={"detail": "Внутренняя ошибка сервера. Попробуйте ещё раз."})


@app.post("/webhook/{secret}")
async def webhook(secret: str, req: Request):
    if not WH_SECRET or not hmac.compare_digest(secret or "", WH_SECRET) or not hmac.compare_digest(req.headers.get("X-Telegram-Bot-Api-Secret-Token") or "", WH_SECRET):
        raise HTTPException(403)
    try:
        payload = await req.json()
        upd = Update.model_validate(payload, context={"bot": bot})
    except Exception as e:
        log.warning("Invalid Telegram webhook payload: %s", _err(e))
        return {"ok": False, "ignored": True}
    await dp.feed_update(bot, upd)
    return {"ok": True}


def norm_day(day):
    try:
        return date.fromisoformat(day).isoformat()
    except Exception:
        return date.today().isoformat()


MEALS = ("breakfast", "lunch", "dinner", "snack")


def day_totals(uid, day):
    es = run("SELECT * FROM entries WHERE uid=? AND day=? ORDER BY ts", (uid, day))
    return es, {k: round(sum(e[k] for e in es), 1) for k in ("kcal", "p", "f", "c")}


def calc_streak(uid, today):
    days = {r["day"] for r in run("SELECT DISTINCT day FROM entries WHERE uid=? ORDER BY day DESC LIMIT 400", (uid,))}
    d = date.fromisoformat(today)
    if d.isoformat() not in days:
        d -= timedelta(days=1)
    n = 0
    while d.isoformat() in days:
        n += 1
        d -= timedelta(days=1)
    return n


@app.get("/api/state")
async def state(day: str = "", today: str = "", tz: int = 180, init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    u = await asyncio.to_thread(get_user, tu["id"], tu.get("first_name", ""), None, tu.get("username", ""))
    if -720 <= tz <= 840 and u.get("tz_offset") != tz:
        await arun("UPDATE users SET tz_offset=? WHERE id=?", (tz, u["id"]))
    day = norm_day(day)
    ai = ai_status(u)
    entries, total = await asyncio.to_thread(day_totals, u["id"], day)
    has = u["kcal"] is not None
    norm = {k: u[k] for k in ("kcal", "p", "f", "c")} if has else None
    rem = {k: norm[k] - total[k] for k in total} if has else None
    prof = {
        "name": u.get("name") or tu.get("first_name") or "",
        **{
            k: u[k]
            for k in (
                "sex", "age", "height", "weight", "start_weight", "act", "goal",
                "target_weight", "pace", "manual", "workouts_per_week", "workout_type",
                "food_pref", "remind_hour", "remind_on",
            )
        },
    } if has else {
        "name": u.get("name") or tu.get("first_name") or "",
        "weight": u.get("weight"),
        "target_weight": u.get("target_weight"),
        "start_weight": u.get("start_weight"),
    }
    w = await arun("SELECT ml FROM water WHERE uid=? AND day=?", (u["id"], day))
    recent = await arun(
        "SELECT name, grams, kcal, p, f, c, last_ts FROM ("
        "SELECT DISTINCT ON (name, ROUND(COALESCE(grams, 0)::numeric)) name, grams, kcal, p, f, c, ts AS last_ts "
        "FROM entries WHERE uid=? "
        "ORDER BY name, ROUND(COALESCE(grams, 0)::numeric), ts DESC"
        ") sub ORDER BY last_ts DESC LIMIT 25",
        (u["id"],),
    )
    weight_log = await arun("SELECT day, weight FROM weights WHERE uid=? ORDER BY day DESC LIMIT 30", (u["id"],))
    weight_log.reverse()
    favs = await arun("SELECT * FROM favs WHERE uid=? ORDER BY id DESC LIMIT 40", (u["id"],))
    streak = await asyncio.to_thread(calc_streak, u["id"], norm_day(today) if today else date.today().isoformat())
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{u['id']}" if BOT_USERNAME else ""
    return {
        "user_name": u.get("name") or tu.get("first_name") or "",
        "is_admin": int(u["id"]) in ADMIN_IDS,
        "has_profile": has,
        "profile": prof,
        "norm": norm,
        "total": total,
        "entries": entries,
        "ai": ai,
        **{k: ai[k] for k in ("free_daily_limit", "daily_limit", "used_today", "remaining_daily_today", "extra_ai_credits", "mealplan_credits", "is_pro", "pro_until")},
        "suggest": suggest(rem) if has else [],
        "water": w[0]["ml"] if w else 0,
        "water_goal": water_goal(u["weight"], u.get("workouts_per_week") or 0),
        "streak": streak,
        "favs": favs,
        "recent": recent,
        "weight_log": weight_log,
        "ref_link": ref_link,
        "ref_count": u.get("ref_count") or 0,
        "catalog": CATALOG_LIST,
        "yookassa_configured": _yookassa_configured(),
        "support": {"handle": SUPPORT_HANDLE, "url": SUPPORT_URL},
        "channel": {"handle": CHANNEL_HANDLE, "url": CHANNEL_URL},
    }


class SupportRequestCreate(BaseModel):
    category: Literal["general", "payment", "subscription", "bug"] = "general"
    message: str = Field(min_length=3, max_length=800)


@app.post("/api/support/request")
async def create_support_request(r: SupportRequestCreate, init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    u = await asyncio.to_thread(get_user, tu["id"], tu.get("first_name", ""), None, tu.get("username", ""))
    msg_txt = re.sub(r"\s+", " ", r.message.strip())
    if len(msg_txt) < 3:
        raise HTTPException(400, "Опишите ваш вопрос чуть подробнее")
    now_ts = int(time.time())
    open_cnt = await arun("SELECT COUNT(*) AS c FROM support_requests WHERE uid=? AND status IN ('new','in_progress')", (u["id"],))
    if (open_cnt[0]["c"] if open_cnt else 0) >= 5:
        raise HTTPException(429, "У вас уже есть 5 активных обращений. Дождитесь ответа администратора.")
    await arun(
        "INSERT INTO support_requests(uid, username, user_name, category, message, status, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (u["id"], u.get("username") or "", u.get("name") or "", r.category, msg_txt, "new", now_ts, now_ts),
    )
    await asyncio.to_thread(db_log, "INFO", "support_request", f"[{r.category}] @{u.get('username') or '-'}: {msg_txt[:100]}", u["id"])
    return {"ok": True}


@app.get("/api/suggest")
async def api_suggest(day: str = "", mode: str = "", seed: int = 0, init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    u = await asyncio.to_thread(get_user, tu["id"], tu.get("first_name", ""), None, tu.get("username", ""))
    if u["kcal"] is None:
        return {"items": []}
    _, t = await asyncio.to_thread(day_totals, u["id"], norm_day(day))
    return {"items": suggest({k: u[k] - t[k] for k in t}, mode if mode in ("protein", "quick", "light") else "", seed)}


@app.get("/api/week")
async def week(day: str = "", init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    end = date.fromisoformat(norm_day(day))
    start = end - timedelta(days=6)
    rows = await arun("SELECT day, SUM(kcal) kcal, SUM(p) p, SUM(f) f, SUM(c) c FROM entries "
               "WHERE uid=? AND day BETWEEN ? AND ? GROUP BY day", (tu["id"], start.isoformat(), end.isoformat()))
    by = {r["day"]: r for r in rows}
    out = []
    for i in range(7):
        d = (start + timedelta(days=i)).isoformat()
        out.append({"day": d, **{k: round((by.get(d) or {}).get(k) or 0) for k in ("kcal", "p", "f", "c")}})
    return {"days": out}


class Profile(BaseModel):
    name: str | None = Field(None, max_length=60)
    sex: Literal["m", "f"]
    age: int = Field(ge=10, le=100)
    height: float = Field(ge=100, le=250)
    weight: float = Field(ge=30, le=300)
    act: float = Field(ge=1.2, le=1.9)
    goal: Literal["lose", "keep", "gain"]
    workouts_per_week: int = Field(2, ge=0, le=14)
    workout_type: Literal["mixed", "strength", "cardio"] = "mixed"
    food_pref: Literal["balanced", "high_protein", "low_sweet", "low_carb"] = "balanced"
    use_ai_tuning: bool = True
    target_weight: float | None = Field(None, ge=30, le=300)
    pace: float = Field(0.5, ge=0.1, le=1.0)
    manual: bool = False
    kcal: int | None = Field(None, ge=800, le=10000)
    p: int | None = Field(None, ge=0, le=600)
    f: int | None = Field(None, ge=0, le=600)
    c: int | None = Field(None, ge=0, le=1500)
    remind_on: bool = True
    remind_hour: int = Field(20, ge=8, le=23)


@app.post("/api/profile")
async def save_profile(p: Profile, init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    cur_u = await asyncio.to_thread(get_user, tu["id"], tu.get("first_name", ""), None, tu.get("username", ""))
    ai_note = ""
    if p.manual:
        if None in (p.kcal, p.p, p.f, p.c):
            raise HTTPException(400, "Укажите калории, белки, жиры и углеводы")
        n = {"kcal": p.kcal, "p": p.p, "f": p.f, "c": p.c}
    else:
        n = calc_norms(
            p.sex, p.age, p.height, p.weight, p.act, p.goal, p.pace,
            workouts_per_week=p.workouts_per_week,
            workout_type=p.workout_type,
            food_pref=p.food_pref,
        )
        if p.use_ai_tuning:
            tuned = await ask_ai_norms_adjustment(
                {
                    "sex": p.sex,
                    "age": p.age,
                    "height": p.height,
                    "weight": p.weight,
                    "target_weight": p.target_weight,
                    "act": p.act,
                    "goal": p.goal,
                    "pace": p.pace,
                    "workouts_per_week": p.workouts_per_week,
                    "workout_type": p.workout_type,
                    "food_pref": p.food_pref,
                },
                n,
            )
            if tuned:
                ai_note = tuned.get("note") or ""
                n = {"kcal": tuned["kcal"], "p": tuned["p"], "f": tuned["f"], "c": tuned["c"]}

    start_w = (cur_u.get("start_weight") or p.weight) if p.target_weight else None
    clean_name = _sanitize_name(p.name or "", cur_u.get("name") or tu.get("first_name") or "")
    await arun(
        "UPDATE users SET name=?,sex=?,age=?,height=?,weight=?,start_weight=?,act=?,goal=?,"
        "workouts_per_week=?,workout_type=?,food_pref=?,target_weight=?,pace=?,manual=?,"
        "kcal=?,p=?,f=?,c=?,remind_on=?,remind_hour=? WHERE id=?",
        (
            clean_name, p.sex, p.age, p.height, p.weight, start_w, p.act, p.goal,
            p.workouts_per_week, p.workout_type, p.food_pref, p.target_weight, p.pace,
            int(p.manual), n["kcal"], n["p"], n["f"], n["c"], int(p.remind_on), p.remind_hour, tu["id"],
        ),
    )
    await arun(
        "INSERT INTO weights(uid,day,weight) VALUES(?,?,?) ON CONFLICT(uid,day) DO UPDATE SET weight=EXCLUDED.weight",
        (tu["id"], date.today().isoformat(), p.weight),
    )
    return {"norm": n, "ai_note": ai_note, "name": clean_name}


class WeightLogReq(BaseModel):
    day: str = ""
    weight: float = Field(ge=30, le=300)


@app.post("/api/weight")
async def log_weight(r: WeightLogReq, init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    u = await asyncio.to_thread(get_user, tu["id"], tu.get("first_name", ""), None, tu.get("username", ""))
    d = norm_day(r.day)
    await arun(
        "INSERT INTO weights(uid,day,weight) VALUES(?,?,?) ON CONFLICT(uid,day) DO UPDATE SET weight=EXCLUDED.weight",
        (tu["id"], d, r.weight),
    )
    if not u.get("start_weight") and u.get("target_weight"):
        await arun("UPDATE users SET weight=?, start_weight=? WHERE id=?", (r.weight, r.weight, tu["id"]))
    else:
        await arun("UPDATE users SET weight=? WHERE id=?", (r.weight, tu["id"]))
    return {"ok": True, "weight": r.weight}


class AddReq(BaseModel):
    day: str
    items: list[Item] = Field(min_length=1, max_length=25)
    src: Literal["manual", "text", "photo", "suggest", "fav", "copy", "mealplan"] = "manual"
    meal: Literal["breakfast", "lunch", "dinner", "snack"] = "snack"


@app.post("/api/entry")
async def add_entry(r: AddReq, init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    await asyncio.to_thread(get_user, tu["id"], tu.get("first_name", ""), None, tu.get("username", ""))
    day = norm_day(r.day)
    for i, it in enumerate(r.items):
        await arun("INSERT INTO entries(uid,day,name,grams,kcal,p,f,c,src,ts,meal) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (tu["id"], day, it.name, it.grams, it.kcal, it.p, it.f, it.c, r.src, int(time.time() * 1000) + i, r.meal))
    return {"ok": True}


class CopyMealReq(BaseModel):
    from_day: str
    to_day: str
    meal: Literal["breakfast", "lunch", "dinner", "snack"]


@app.post("/api/copy-meal")
async def copy_meal(r: CopyMealReq, init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    fd, td = norm_day(r.from_day), norm_day(r.to_day)
    rows = await arun("SELECT * FROM entries WHERE uid=? AND day=? AND meal=? ORDER BY ts", (tu["id"], fd, r.meal))
    if not rows:
        raise HTTPException(404, "В выбранном приёме пищи ничего нет")
    for i, e in enumerate(rows):
        await arun("INSERT INTO entries(uid,day,name,grams,kcal,p,f,c,src,ts,meal) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (tu["id"], td, e["name"], e["grams"], e["kcal"], e["p"], e["f"], e["c"], "copy", int(time.time() * 1000) + i, r.meal))
    return {"ok": True, "count": len(rows)}


class EditReq(BaseModel):
    grams: float = Field(gt=0, le=5000)


@app.patch("/api/entry/{eid}")
async def edit_entry(eid: int, r: EditReq, init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    rows = await arun("SELECT * FROM entries WHERE id=? AND uid=?", (eid, tu["id"]))
    if not rows:
        raise HTTPException(404, "Запись не найдена")
    e = rows[0]
    old_g = float(e.get("grams") or 0)
    if old_g > 0:
        k = r.grams / old_g
        new_kcal = round(e["kcal"] * k, 1)
        new_p = round(e["p"] * k, 1)
        new_f = round(e["f"] * k, 1)
        new_c = round(e["c"] * k, 1)
    else:
        # Если вес ранее не был указан (0 г), сохраняем исходные КБЖУ, просто проставляя граммовку
        new_kcal, new_p, new_f, new_c = e["kcal"], e["p"], e["f"], e["c"]
    await arun(
        "UPDATE entries SET grams=?,kcal=?,p=?,f=?,c=? WHERE id=? AND uid=?",
        (round(r.grams, 1), new_kcal, new_p, new_f, new_c, eid, tu["id"]),
    )
    return {"ok": True}


@app.delete("/api/entry/{eid}")
async def del_entry(eid: int, init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    await arun("DELETE FROM entries WHERE id=? AND uid=?", (eid, tu["id"]))
    return {"ok": True}


class WaterReq(BaseModel):
    day: str
    ml: int = Field(ge=-2000, le=2000)


@app.post("/api/water")
async def add_water(r: WaterReq, init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    day = norm_day(r.day)
    cur = await arun("SELECT ml FROM water WHERE uid=? AND day=?", (tu["id"], day))
    new = max(0, min(10000, (cur[0]["ml"] if cur else 0) + r.ml))
    await arun(
        "INSERT INTO water(uid,day,ml) VALUES(?,?,?) ON CONFLICT(uid,day) DO UPDATE SET ml=EXCLUDED.ml",
        (tu["id"], day, new),
    )
    return {"water": new}


@app.post("/api/fav")
async def add_fav(it: Item, init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    existing = await arun("SELECT id FROM favs WHERE uid=? AND name=? AND ROUND(COALESCE(grams,0)::numeric)=ROUND(?::numeric)", (tu["id"], it.name, it.grams))
    if not existing:
        await arun("INSERT INTO favs(uid,name,grams,kcal,p,f,c) VALUES(?,?,?,?,?,?,?)",
            (tu["id"], it.name, it.grams, it.kcal, it.p, it.f, it.c))
    return {"ok": True}


@app.delete("/api/fav/{fid}")
async def del_fav(fid: int, init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    await arun("DELETE FROM favs WHERE id=? AND uid=?", (fid, tu["id"]))
    return {"ok": True}


class AIReq(BaseModel):
    text: str = Field("", max_length=1000)
    image: str = Field("", max_length=4_500_000)
    label_mode: bool = False


@app.post("/api/ai")
async def ai_analyze(r: AIReq, init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    u = await asyncio.to_thread(get_user, tu["id"], tu.get("first_name", ""), None, tu.get("username", ""))
    if not r.text.strip() and not r.image:
        raise HTTPException(400, "Опишите еду или приложите фото")
    if r.image:
        split_image(r.image)

    source, period_month = await asyncio.to_thread(consume_ai_quota, u["id"])
    if not source:
        raise HTTPException(
            402,
            f"Пробный лимит ({FREE_MONTHLY_LIMIT} запросов в месяц) закончился. Оформите пакет или подписку либо напишите {SUPPORT_HANDLE}.",
        )
    try:
        items = await ask_ai(r.text, r.image, label_mode=r.label_mode)
    except BaseException:
        await asyncio.to_thread(refund_ai_quota, u["id"], source, period_month)
        raise
    updated_u = await asyncio.to_thread(get_user, u["id"])
    return {"items": items, "source": source, "ai": ai_status(updated_u)}


class MealPlanReq(BaseModel):
    day: str = ""
    prefs: str = Field("", max_length=300)


@app.post("/api/ai/mealplan")
async def ai_mealplan(r: MealPlanReq, init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    u = await asyncio.to_thread(get_user, tu["id"], tu.get("first_name", ""), None, tu.get("username", ""))
    if u["kcal"] is None:
        raise HTTPException(400, "Сначала заполните профиль и расчёт КБЖУ")

    mp_source = await asyncio.to_thread(consume_mealplan_quota, u["id"])
    if not mp_source:
        mp_rub = CATALOG["mealplan_1"]["price_rub"]
        mp_stars = CATALOG["mealplan_1"]["price_stars"]
        raise HTTPException(
            402,
            f"Бесплатный рацион уже использован. Дальше генерация доступна в PRO или по разовому билету ({mp_rub} ₽ / {mp_stars} ⭐).",
        )

    _, tot = await asyncio.to_thread(day_totals, u["id"], norm_day(r.day))
    rem = {k: (u[k] or 0) - tot[k] for k in tot}
    if rem["kcal"] < 250:
        rem = {k: u[k] or 0 for k in ("kcal", "p", "f", "c")}

    try:
        plan_data = await ask_mealplan_ai(
            rem,
            r.prefs,
            user_ctx={
                "goal": u.get("goal"),
                "workouts_per_week": u.get("workouts_per_week"),
                "workout_type": u.get("workout_type"),
                "food_pref": u.get("food_pref"),
            },
        )
    except BaseException:
        await asyncio.to_thread(refund_mealplan_quota, u["id"], mp_source)
        raise
    updated_u = await asyncio.to_thread(get_user, u["id"])
    return {**plan_data, "plan_source": mp_source, "ai": ai_status(updated_u)}


TariffId = Literal["pack_10", "pack_30", "sub_30d", "mealplan_1"]


class InvoiceReq(BaseModel):
    item_id: TariffId


@app.post("/api/billing/create-invoice")
async def create_invoice(r: InvoiceReq, init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    await asyncio.to_thread(get_user, tu["id"], tu.get("first_name", ""), None, tu.get("username", ""))
    it = CATALOG[r.item_id]
    stars_amount = int(it["price_stars"])
    payload = f"{tu['id']}:{r.item_id}:{uuid.uuid4().hex[:12]}"
    try:
        link = await bot.create_invoice_link(
            title=it["title"],
            description=it["desc"],
            payload=payload,
            provider_token="",
            currency="XTR",
            prices=[LabeledPrice(label=it["title"], amount=stars_amount)],
        )
    except Exception as e:
        log.error("create_invoice_link failed: %s", _err(e))
        await asyncio.to_thread(db_log, "ERROR", "stars_invoice_error", _err(e), tu["id"])
        raise HTTPException(502, _err(f"Telegram: не удалось создать счёт ({type(e).__name__}: {e})"))
    return {"invoice_url": link, "item_id": r.item_id, "stars": stars_amount}


def _yookassa_configured() -> bool:
    return bool(
        YOOKASSA_SHOP_ID
        and YOOKASSA_SECRET_KEY
        and "REPLACE_WITH_" not in YOOKASSA_SHOP_ID
        and "REPLACE_WITH_" not in YOOKASSA_SECRET_KEY
        and YOOKASSA_SHOP_ID != "YOUR_SHOP_ID"
        and YOOKASSA_SECRET_KEY != "YOUR_SECRET_KEY"
    )


class YooKassaCreateReq(BaseModel):
    item_id: TariffId
    payment_method: Literal["sbp", "bank_card"] = "sbp"


@app.post("/api/billing/yookassa/create")
async def create_yookassa_payment(r: YooKassaCreateReq, init_data: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data or init_data)
    await asyncio.to_thread(get_user, tu["id"], tu.get("first_name", ""), None, tu.get("username", ""))
    if not _yookassa_configured():
        raise HTTPException(
            503,
            f"Оплата через ЮKassa скоро подключится после активации ключей. Пока можно оплатить звёздами или написать {SUPPORT_HANDLE}.",
        )
    it = CATALOG[r.item_id]
    rub_amount = float(it["price_rub"])
    idempotence_key = str(uuid.uuid4())
    body = {
        "amount": {
            "value": f"{rub_amount:.2f}",
            "currency": "RUB",
        },
        "confirmation": {
            "type": "redirect",
            "return_url": YOOKASSA_RETURN_URL,
        },
        "payment_method_data": {
            "type": r.payment_method,
        },
        "description": f"SnapEat: {it['title']} ({int(rub_amount)} ₽)",
        "metadata": {
            "user_id": str(tu["id"]),
            "item_id": r.item_id,
            "payment_method": r.payment_method,
        },
    }
    try:
        async with httpx.AsyncClient(timeout=20) as cl:
            resp = await cl.post(
                f"{YOOKASSA_API_BASE}/payments",
                json=body,
                auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
                headers={"Idempotence-Key": idempotence_key},
            )
    except Exception as e:
        log.error("YooKassa create payment network error: %s", _err(e))
        await asyncio.to_thread(db_log, "ERROR", "yookassa_create_net_err", _err(e), tu["id"])
        raise HTTPException(502, "Не удалось связаться с ЮKassa. Попробуйте позже.")

    if resp.status_code not in (200, 201):
        log.error("YooKassa create payment HTTP %s: %s", resp.status_code, _err(resp.text[:300]))
        await asyncio.to_thread(db_log, "ERROR", "yookassa_create_http_err", f"{resp.status_code}: {resp.text[:160]}", tu["id"])
        raise HTTPException(502, "ЮKassa отклонила создание платежа. Попробуйте ещё раз.")

    data = resp.json()
    payment_id = data.get("id")
    confirmation_url = (data.get("confirmation") or {}).get("confirmation_url")
    if not payment_id or not confirmation_url:
        raise HTTPException(502, "ЮKassa не вернула ссылку подтверждения оплаты")

    return {
        "payment_id": payment_id,
        "status": data.get("status") or "pending",
        "confirmation_url": confirmation_url,
        "item_id": r.item_id,
        "amount_rub": rub_amount,
        "payment_method": r.payment_method,
    }


@app.post("/api/billing/yookassa/webhook")
async def yookassa_webhook(req: Request):
    try:
        event = await req.json()
    except Exception:
        raise HTTPException(400, "Некорректный JSON вебхука")

    event_type = event.get("event")
    obj = event.get("object") or {}
    payment_id = obj.get("id")
    if not isinstance(payment_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", payment_id.strip()):
        return {"ok": True, "ignored": True}
    payment_id = payment_id.strip()

    if event_type != "payment.succeeded":
        return {"ok": True, "event": event_type}

    if not _yookassa_configured():
        log.warning("YooKassa webhook received while credentials are not configured")
        raise HTTPException(503, "ЮKassa credentials not configured")

    try:
        async with httpx.AsyncClient(timeout=20) as cl:
            verify_resp = await cl.get(
                f"{YOOKASSA_API_BASE}/payments/{payment_id}",
                auth=(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            )
    except Exception as e:
        log.error("YooKassa verify payment error (%s): %s", payment_id, _err(e))
        await asyncio.to_thread(db_log, "ERROR", "yookassa_verify_err", f"{payment_id}: {_err(e)}")
        raise HTTPException(502, "Failed to verify payment")

    if verify_resp.status_code != 200:
        log.warning("YooKassa verify payment %s returned %s", payment_id, verify_resp.status_code)
        raise HTTPException(502, "Payment verification failed")

    verified = verify_resp.json()
    if verified.get("status") != "succeeded" or verified.get("paid") is False:
        return {"ok": True, "status": verified.get("status")}

    meta = verified.get("metadata") or {}
    item_id = meta.get("item_id")
    try:
        uid = int(meta.get("user_id"))
    except Exception:
        log.warning("YooKassa payment %s missing valid user_id metadata", payment_id)
        return {"ok": True, "warning": "missing user_id"}

    if item_id not in CATALOG:
        log.warning("YooKassa payment %s has unknown item_id %s", payment_id, item_id)
        return {"ok": True, "warning": "unknown item_id"}

    amt_val = 0.0
    try:
        amt_val = float(((verified.get("amount") or {}).get("value")) or CATALOG[item_id]["price_rub"])
    except Exception:
        amt_val = float(CATALOG[item_id]["price_rub"])

    await asyncio.to_thread(get_user, uid)
    credited = await asyncio.to_thread(
        credit_payment,
        uid,
        item_id,
        f"yk:{payment_id}",
        0,
        amt_val,
        "yookassa",
        "succeeded",
        meta,
    )
    if credited:
        try:
            u = await asyncio.to_thread(get_user, uid)
            it = CATALOG[item_id]
            if it["kind"] == "pack":
                msg = f"✅ Оплата ({int(amt_val)} ₽) прошла! Начислено +{it['credits']} ИИ-запросов. Баланс: {u['extra_ai_credits']}."
            elif it["kind"] == "mealplan":
                msg = f"✅ Оплата ({int(amt_val)} ₽) прошла! Начислен 1 билет на ИИ-рацион во вкладке «Рацион»."
            else:
                msg = f"👑 Оплата ({int(amt_val)} ₽) прошла! SnapEat Pro активен до {datetime.fromtimestamp(u['pro_until'], tz=timezone.utc):%d.%m.%Y}."
            await bot.send_message(uid, msg)
        except Exception as e:
            log.warning("YooKassa payment credited, notify failed: %s", _err(e))

    return {"ok": True, "credited": credited, "payment_id": payment_id}


def _find_user_by_target(target: str) -> dict | None:
    raw = (target or "").strip()
    if not raw:
        return None
    if raw.lstrip("-").isdigit():
        rows = run("SELECT * FROM users WHERE id=?", (int(raw),))
        return rows[0] if rows else None
    clean_un = _normalize_username(raw).lower()
    if clean_un:
        rows = run("SELECT * FROM users WHERE LOWER(username)=? LIMIT 1", (clean_un,))
        if rows:
            return rows[0]
    rows = run("SELECT * FROM users WHERE LOWER(name) LIKE ? ORDER BY last_seen DESC LIMIT 1", (f"%{raw.lower()}%",))
    return rows[0] if rows else None


def _serialize_admin_user(u: dict) -> dict:
    now_ts = int(time.time())
    pro_until = int(u.get("pro_until") or 0)
    is_pro = pro_until > now_ts
    cur_month = _current_month_str()
    today_str = date.today().isoformat()
    return {
        "id": int(u["id"]),
        "username": u.get("username") or "",
        "name": u.get("name") or "",
        "is_pro": is_pro,
        "pro_until": pro_until,
        "pro_days_left": max(0, round((pro_until - now_ts) / 86400, 1)) if is_pro else 0,
        "extra_ai_credits": int(u.get("extra_ai_credits") or 0),
        "mealplan_credits": int(u.get("mealplan_credits") or 0),
        "trial_used": int(u.get("trial_used") or 0) if (u.get("trial_month") or "") == cur_month else 0,
        "ai_used_today": int(u.get("ai_n") or 0) if (u.get("ai_day") or "") == today_str else 0,
        "pro_month_used": int(u.get("pro_month_used") or 0) if (u.get("pro_month") or "") == cur_month else 0,
        "ref_count": int(u.get("ref_count") or 0),
        "goal": u.get("goal") or "",
        "weight": u.get("weight"),
        "target_weight": u.get("target_weight"),
        "kcal": u.get("kcal"),
        "created_at": int(u.get("created_at") or 0),
        "last_seen": int(u.get("last_seen") or 0),
    }


@app.get("/api/admin/stats")
async def admin_stats(force: int = 0, init_data: str = "", x_init_data: str = Header(default="")):
    require_admin(x_init_data or init_data)
    global _admin_stats_cache
    now_ts = int(time.time())
    if not force and _admin_stats_cache and (now_ts - _admin_stats_cache[0] < 30):
        return _admin_stats_cache[1]

    today_str = date.today().isoformat()
    cur_month = _current_month_str()
    u_agg = (await arun(
        "SELECT "
        "COUNT(*) AS total_users, "
        "COUNT(*) FILTER (WHERE pro_until > ?) AS active_pro, "
        "COUNT(*) FILTER (WHERE created_at >= ?) AS new_24h, "
        "COUNT(*) FILTER (WHERE created_at >= ?) AS new_7d, "
        "COUNT(*) FILTER (WHERE last_seen >= ?) AS active_24h, "
        "COALESCE(SUM(ai_n) FILTER (WHERE ai_day = ?), 0) AS ai_today, "
        "COALESCE(SUM(pro_month_used) FILTER (WHERE pro_month = ?), 0) + "
        "COALESCE(SUM(trial_used) FILTER (WHERE trial_month = ?), 0) AS ai_month "
        "FROM users",
        (now_ts, now_ts - 86400, now_ts - 7 * 86400, now_ts - 86400, today_str, cur_month, cur_month),
    ))[0]

    p_agg = (await arun(
        "SELECT "
        "COUNT(*) AS total_payments, "
        "COALESCE(SUM(amount_rub), 0) AS total_rub, "
        "COALESCE(SUM(stars), 0) AS total_stars, "
        "COALESCE(SUM(amount_rub) FILTER (WHERE ts >= ?), 0) AS rub_30d, "
        "COALESCE(SUM(stars) FILTER (WHERE ts >= ?), 0) AS stars_30d "
        "FROM payments WHERE status='succeeded'",
        (now_ts - 30 * 86400, now_ts - 30 * 86400),
    ))[0]

    req_agg = (await arun(
        "SELECT COUNT(*) FILTER (WHERE status IN ('new','in_progress')) AS open_requests FROM support_requests"
    ))[0]
    log_agg = (await arun(
        "SELECT COUNT(*) AS total_logs, COUNT(*) FILTER (WHERE level='ERROR' AND ts >= ?) AS errors_24h FROM bot_logs",
        (now_ts - 86400,),
    ))[0]

    start_7d = now_ts - 6 * 86400
    pay_rows = await arun(
        "SELECT TO_CHAR(TO_TIMESTAMP(ts) AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS d, "
        "COUNT(*) AS cnt, COALESCE(SUM(amount_rub), 0) AS rub, COALESCE(SUM(stars), 0) AS stars "
        "FROM payments WHERE status='succeeded' AND ts >= ? GROUP BY d ORDER BY d",
        (start_7d,),
    )
    by_day = {r["d"]: r for r in pay_rows}
    daily_chart = []
    for i in range(7):
        ds = (date.today() - timedelta(days=6 - i)).isoformat()
        row = by_day.get(ds) or {}
        daily_chart.append({
            "day": ds,
            "count": int(row.get("cnt") or 0),
            "rub": float(row.get("rub") or 0),
            "stars": int(row.get("stars") or 0),
        })

    res = {
        **{k: int(u_agg[k] or 0) for k in ("total_users", "active_pro", "new_24h", "new_7d", "active_24h", "ai_today", "ai_month")},
        "total_payments": int(p_agg["total_payments"] or 0),
        "total_rub": float(p_agg["total_rub"] or 0),
        "total_stars": int(p_agg["total_stars"] or 0),
        "rub_30d": float(p_agg["rub_30d"] or 0),
        "stars_30d": int(p_agg["stars_30d"] or 0),
        "open_requests": int(req_agg["open_requests"] or 0),
        "total_logs": int(log_agg["total_logs"] or 0),
        "errors_24h": int(log_agg["errors_24h"] or 0),
        "log_retention_days": LOG_RETENTION_DAYS,
        "daily_chart": daily_chart,
    }
    _admin_stats_cache = (now_ts, res)
    return res


@app.get("/api/admin/users")
async def admin_users(q: str = "", filter: str = "all", limit: int = 30, offset: int = 0, init_data: str = "", x_init_data: str = Header(default="")):
    require_admin(x_init_data or init_data)
    lim = max(1, min(int(limit or 30), 100))
    off = max(0, int(offset or 0))
    now_ts = int(time.time())
    where = ["1=1"]
    params: list = []

    if filter == "pro":
        where.append("pro_until > ?")
        params.append(now_ts)
    elif filter == "credits":
        where.append("(COALESCE(extra_ai_credits,0) > 0 OR COALESCE(mealplan_credits,0) > 0)")

    raw_q = (q or "").strip()
    if raw_q:
        if raw_q.lstrip("-").isdigit():
            where.append("(id = ? OR LOWER(username) LIKE ? OR LOWER(name) LIKE ?)")
            params.extend([int(raw_q), f"%{raw_q.lower()}%", f"%{raw_q.lower()}%"])
        else:
            clean_q = raw_q.lstrip("@").lower()
            where.append("(LOWER(username) LIKE ? OR LOWER(name) LIKE ?)")
            params.extend([f"%{clean_q}%", f"%{clean_q}%"])

    where_sql = " AND ".join(where)
    total = (await arun(f"SELECT COUNT(*) AS c FROM users WHERE {where_sql}", tuple(params)))[0]["c"]
    rows = await arun(
        f"SELECT * FROM users WHERE {where_sql} ORDER BY last_seen DESC, id DESC LIMIT ? OFFSET ?",
        tuple([*params, lim, off]),
    )
    return {"total": int(total or 0), "users": [_serialize_admin_user(r) for r in rows]}


class AdminSubActionReq(BaseModel):
    target: str = Field(min_length=1, max_length=80)
    action: Literal["grant_pro", "revoke_pro", "add_credits", "set_credits", "add_mealplan", "reset_limits"]
    days: int = Field(30, ge=1, le=3650)
    amount: int = Field(0, ge=-10000, le=10000)
    notify: bool = True


@app.post("/api/admin/users/action")
async def admin_user_action(r: AdminSubActionReq, init_data: str = "", x_init_data: str = Header(default="")):
    admin = require_admin(x_init_data or init_data)
    u = await asyncio.to_thread(_find_user_by_target, r.target)
    if not u:
        raise HTTPException(404, f"Пользователь «{r.target}» не найден. Он должен хотя бы раз запустить бота.")

    uid = int(u["id"])
    now_ts = int(time.time())
    notify_msg = ""
    log_detail = ""

    if r.action == "grant_pro":
        cur_until = int(u.get("pro_until") or 0)
        new_until = int(max(now_ts, cur_until) + r.days * 86400)
        await arun("UPDATE users SET pro_until=? WHERE id=?", (new_until, uid))
        dt_str = datetime.fromtimestamp(new_until).strftime("%d.%m.%Y")
        notify_msg = f"👑 Администратор активировал вам подписку <b>SnapEat Pro</b> на {r.days} дн. (до {dt_str})!"
        log_detail = f"grant_pro +{r.days}d -> {dt_str}"

    elif r.action == "revoke_pro":
        await arun("UPDATE users SET pro_until=0 WHERE id=?", (uid,))
        notify_msg = "ℹ️ Ваша подписка SnapEat Pro была отключена администратором."
        log_detail = "revoke_pro"

    elif r.action == "add_credits":
        cur_c = int(u.get("extra_ai_credits") or 0)
        new_c = max(0, cur_c + r.amount)
        await arun("UPDATE users SET extra_ai_credits=? WHERE id=?", (new_c, uid))
        if r.amount > 0:
            notify_msg = f"🎁 Вам начислено +{r.amount} бонусных ИИ-запросов! Текущий баланс: {new_c}."
        log_detail = f"add_credits {r.amount:+d} (now {new_c})"

    elif r.action == "set_credits":
        new_c = max(0, r.amount)
        await arun("UPDATE users SET extra_ai_credits=? WHERE id=?", (new_c, uid))
        log_detail = f"set_credits -> {new_c}"

    elif r.action == "add_mealplan":
        cur_m = int(u.get("mealplan_credits") or 0)
        new_m = max(0, cur_m + r.amount)
        await arun("UPDATE users SET mealplan_credits=? WHERE id=?", (new_m, uid))
        if r.amount > 0:
            notify_msg = f"🥗 Вам начислено +{r.amount} билетов на ИИ-рацион! Текущий баланс: {new_m}."
        log_detail = f"add_mealplan {r.amount:+d} (now {new_m})"

    elif r.action == "reset_limits":
        await arun("UPDATE users SET trial_used=0, ai_n=0, pro_month_used=0, free_mealplan_used=0 WHERE id=?", (uid,))
        log_detail = "reset_limits"

    global _admin_stats_cache
    _admin_stats_cache = None
    await asyncio.to_thread(db_log, "INFO", "admin_user_action", f"admin={admin['id']} -> @{u.get('username') or uid}: {log_detail}", uid)

    if r.notify and notify_msg:
        try:
            await bot.send_message(uid, notify_msg, parse_mode="HTML")
        except Exception as e:
            log.warning("Failed to notify user %s after admin action: %s", uid, _err(e))

    updated = (await arun("SELECT * FROM users WHERE id=?", (uid,)))[0]
    return {"ok": True, "user": _serialize_admin_user(updated)}


class AdminBroadcastReq(BaseModel):
    audience: Literal["all", "pro", "free"] = "all"
    message: str = Field(min_length=3, max_length=2000)


@app.post("/api/admin/broadcast")
async def admin_broadcast(r: AdminBroadcastReq, init_data: str = "", x_init_data: str = Header(default="")):
    admin = require_admin(x_init_data or init_data)
    now_ts = int(time.time())
    if r.audience == "pro":
        rows = await arun("SELECT id FROM users WHERE pro_until > ?", (now_ts,))
    elif r.audience == "free":
        rows = await arun("SELECT id FROM users WHERE COALESCE(pro_until, 0) <= ?", (now_ts,))
    else:
        rows = await arun("SELECT id FROM users")

    sent = 0
    failed = 0
    safe_msg = html.escape(r.message.strip())
    kb = (
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🥗 Открыть SnapEat", web_app=WebAppInfo(url=BASE_URL))]])
        if BASE_URL else None
    )
    for row in rows[:500]:
        try:
            await bot.send_message(int(row["id"]), safe_msg, parse_mode="HTML", reply_markup=kb)
            sent += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1

    await asyncio.to_thread(
        db_log,
        "INFO",
        "admin_broadcast",
        f"admin={admin['id']} aud={r.audience} sent={sent} failed={failed}",
        admin["id"],
    )
    return {"ok": True, "sent": sent, "failed": failed}


@app.get("/api/admin/payments")
async def admin_payments(q: str = "", limit: int = 40, offset: int = 0, init_data: str = "", x_init_data: str = Header(default="")):
    require_admin(x_init_data or init_data)
    lim = max(1, min(int(limit or 40), 100))
    off = max(0, int(offset or 0))
    where = ["1=1"]
    params: list = []
    raw_q = (q or "").strip()
    if raw_q:
        if raw_q.lstrip("-").isdigit():
            where.append("(p.uid = ? OR p.charge_id LIKE ?)")
            params.extend([int(raw_q), f"%{raw_q}%"])
        else:
            cq = raw_q.lstrip("@").lower()
            where.append("(LOWER(COALESCE(u.username,'')) LIKE ? OR LOWER(COALESCE(u.name,'')) LIKE ? OR LOWER(p.item_id) LIKE ? OR LOWER(p.charge_id) LIKE ?)")
            params.extend([f"%{cq}%", f"%{cq}%", f"%{cq}%", f"%{cq}%"])
    w_sql = " AND ".join(where)
    rows = await arun(
        f"SELECT p.*, COALESCE(u.username,'') AS username, COALESCE(u.name,'') AS user_name "
        f"FROM payments p LEFT JOIN users u ON u.id = p.uid "
        f"WHERE {w_sql} ORDER BY p.ts DESC LIMIT ? OFFSET ?",
        tuple([*params, lim, off]),
    )
    for r in rows:
        it = CATALOG.get(r.get("item_id") or "")
        r["item_title"] = it["title"] if it else (r.get("item_id") or "")
    return {"payments": rows}


@app.get("/api/admin/requests")
async def admin_requests(status: str = "open", limit: int = 40, init_data: str = "", x_init_data: str = Header(default="")):
    require_admin(x_init_data or init_data)
    lim = max(1, min(int(limit or 40), 100))
    if status == "open":
        rows = await arun(
            "SELECT * FROM support_requests WHERE status IN ('new','in_progress') ORDER BY created_at DESC LIMIT ?",
            (lim,),
        )
    elif status in ("new", "in_progress", "done", "rejected"):
        rows = await arun(
            "SELECT * FROM support_requests WHERE status=? ORDER BY created_at DESC LIMIT ?",
            (status, lim),
        )
    else:
        rows = await arun("SELECT * FROM support_requests ORDER BY created_at DESC LIMIT ?", (lim,))
    return {"requests": rows}


class AdminRequestUpdate(BaseModel):
    status: Literal["new", "in_progress", "done", "rejected"]
    admin_note: str = Field("", max_length=400)
    reply_to_user: str = Field("", max_length=1000)


@app.post("/api/admin/requests/{req_id}")
async def admin_update_request(req_id: int, r: AdminRequestUpdate, init_data: str = "", x_init_data: str = Header(default="")):
    admin = require_admin(x_init_data or init_data)
    rows = await arun("SELECT * FROM support_requests WHERE id=?", (req_id,))
    if not rows:
        raise HTTPException(404, "Запрос не найден")
    sr = rows[0]
    now_ts = int(time.time())
    note = (r.admin_note or "").strip()[:400]
    await arun(
        "UPDATE support_requests SET status=?, admin_note=?, updated_at=? WHERE id=?",
        (r.status, note, now_ts, req_id),
    )
    global _admin_stats_cache
    _admin_stats_cache = None
    await asyncio.to_thread(db_log, "INFO", "admin_request_upd", f"admin={admin['id']} req=#{req_id} -> {r.status}", sr["uid"])

    reply_txt = (r.reply_to_user or "").strip()
    sent_ok = False
    if reply_txt:
        try:
            await bot.send_message(
                int(sr["uid"]),
                f"💬 <b>Ответ поддержки SnapEat</b> (по обращению #{req_id}):\n\n{html.escape(reply_txt)}",
                parse_mode="HTML",
            )
            sent_ok = True
        except Exception as e:
            log.warning("Failed to send support reply to %s: %s", sr["uid"], _err(e))

    return {"ok": True, "reply_sent": sent_ok}


@app.get("/api/admin/logs")
async def admin_logs(level: str = "", q: str = "", limit: int = 60, init_data: str = "", x_init_data: str = Header(default="")):
    require_admin(x_init_data or init_data)
    lim = max(1, min(int(limit or 60), 150))
    where = ["1=1"]
    params: list = []
    if level in ("INFO", "WARNING", "ERROR"):
        where.append("level = ?")
        params.append(level)
    raw_q = (q or "").strip()
    if raw_q:
        if raw_q.lstrip("-").isdigit():
            where.append("(uid = ? OR event LIKE ? OR details LIKE ?)")
            params.extend([int(raw_q), f"%{raw_q}%", f"%{raw_q}%"])
        else:
            where.append("(LOWER(event) LIKE ? OR LOWER(details) LIKE ?)")
            params.extend([f"%{raw_q.lower()}%", f"%{raw_q.lower()}%"])
    w_sql = " AND ".join(where)
    rows = await arun(f"SELECT * FROM bot_logs WHERE {w_sql} ORDER BY ts DESC, id DESC LIMIT ?", tuple([*params, lim]))
    return {"logs": rows, "retention_days": LOG_RETENTION_DAYS}


@app.post("/api/admin/logs/prune")
async def admin_prune_logs_now(init_data: str = "", x_init_data: str = Header(default="")):
    admin = require_admin(x_init_data or init_data)
    await asyncio.to_thread(prune_old_logs)
    await asyncio.to_thread(db_log, "INFO", "admin_manual_prune", f"admin={admin['id']}", admin["id"])
    return {"ok": True}


STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)


@app.get("/health")
async def health():
    await arun("SELECT 1")
    free_targets = _free_fallback_targets()
    return {
        "ok": True,
        "database": "postgresql",
        "webhook": bool(BASE_URL),
        "gemini": bool(AI_KEY),
        "free_fallback": bool(free_targets),
        "free_fallback_count": len(free_targets),
        "free_fallback_models": [t[3] for t in free_targets],
        "openai_fallback": bool(OPENAI_API_KEY),
        "yookassa_configured": _yookassa_configured(),
        "keepalive": (os.getenv("KEEPALIVE_ENABLED", "1") or "1").strip().lower() not in ("0", "false", "no", "off"),
        "models": _model_candidates(),
    }


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/admin")
async def admin_page():
    return FileResponse(STATIC_DIR / "admin.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
