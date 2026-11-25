import os, json, hmac, hashlib, time, sqlite3, threading, re, logging, random, uuid, asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (Update, Message, PreCheckoutQuery, InlineKeyboardMarkup,
                           InlineKeyboardButton, WebAppInfo, LabeledPrice, MenuButtonWebApp)

BOT_TOKEN = os.environ["BOT_TOKEN"]
BASE_URL = (os.getenv("WEBAPP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
AI_KEY = (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()
AI_MODEL = os.getenv("AI_MODEL", "gemini-2.5-flash")
AI_FALLBACK_MODELS = [m.strip() for m in os.getenv("AI_FALLBACK_MODELS", "gemini-2.5-flash,gemini-2.0-flash").split(",") if m.strip()]
log = logging.getLogger("uvicorn.error")
AI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"

FREE_LIMIT = int(os.getenv("FREE_AI_LIMIT", "5"))
PRO_LIMIT = int(os.getenv("PRO_AI_LIMIT", "40"))
DB_PATH = os.getenv("DB_PATH", "data.db")
WH_SECRET = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")

CATALOG = {
    "pack_20": {"kind": "pack", "credits": 20, "price": 25, "title": "Старт",
                "desc": "+20 ИИ-запросов на баланс. Не сгорают в конце дня."},
    "pack_60": {"kind": "pack", "credits": 60, "price": 60, "title": "Выгодный", "badge": "Популярный",
                "desc": "+60 ИИ-запросов на баланс. Не сгорают в конце дня."},
    "pack_150": {"kind": "pack", "credits": 150, "price": 130, "title": "Запас",
                 "desc": "+150 ИИ-запросов на баланс. Не сгорают в конце дня."},
    "pro_30d": {"kind": "pro", "days": 30, "price": 149, "title": "SnapEat Pro · 1 мес",
                "desc": f"До {PRO_LIMIT} ИИ-запросов в день, ИИ-меню на день, график веса, подробная аналитика и сканер этикеток."},
    "pro_90d": {"kind": "pro", "days": 90, "price": 349, "title": "SnapEat Pro · 3 мес", "badge": "−22%",
                "desc": f"90 дней полного доступа ко всем PRO-функциям и до {PRO_LIMIT} ИИ-запросов в день. Продлевает подписку."},
    "pro_365d": {"kind": "pro", "days": 365, "price": 990, "title": "SnapEat Pro · Год", "badge": "Выгода 45%",
                 "desc": "Целый год PRO без ограничений: ИИ-генератор рациона, трекер веса, сканер штрих-кодов/упаковок и аналитика."},
    "mealplan_1": {"kind": "mealplan", "credits": 1, "price": 35, "title": "ИИ-рацион под остаток КБЖУ",
                   "desc": "Разовая генерация персонального меню (завтрак/обед/ужин) строго под твои цели и продукты. Для PRO — бесплатно!"},
}
CATALOG_LIST = [{"id": k, **v} for k, v in CATALOG.items()]

# ---------- SQLite (WAL mode for concurrency) ----------
db_dir = os.path.dirname(os.path.abspath(DB_PATH))
if db_dir:
    os.makedirs(db_dir, exist_ok=True)
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
lock = threading.Lock()


def run(sql, args=()):
    with lock:
        cur = db.execute(sql, args)
        db.commit()
        return [dict(r) for r in cur.fetchall()]


def run_rc(sql, args=()):
    with lock:
        cur = db.execute(sql, args)
        db.commit()
        return cur.rowcount


run("PRAGMA journal_mode=WAL")
run("PRAGMA synchronous=NORMAL")

run("""CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, name TEXT, sex TEXT, age INT, height REAL, weight REAL, act REAL, goal TEXT,
    kcal INT, p INT, f INT, c INT, pro_until INT DEFAULT 0, ai_day TEXT DEFAULT '', ai_n INT DEFAULT 0)""")
run("""CREATE TABLE IF NOT EXISTS entries(
    id INTEGER PRIMARY KEY AUTOINCREMENT, uid INT, day TEXT, name TEXT, grams REAL,
    kcal REAL, p REAL, f REAL, c REAL, src TEXT, ts INT)""")
run("CREATE INDEX IF NOT EXISTS ix_entries ON entries(uid, day)")

for _ddl in (
    "ALTER TABLE users ADD COLUMN target_weight REAL",
    "ALTER TABLE users ADD COLUMN start_weight REAL",
    "ALTER TABLE users ADD COLUMN pace REAL DEFAULT 0.5",
    "ALTER TABLE users ADD COLUMN manual INT DEFAULT 0",
    "ALTER TABLE entries ADD COLUMN meal TEXT DEFAULT 'snack'",
    "ALTER TABLE users ADD COLUMN extra_ai_credits INT DEFAULT 0",
    "ALTER TABLE users ADD COLUMN mealplan_credits INT DEFAULT 0",
    "ALTER TABLE users ADD COLUMN ref_by INT",
    "ALTER TABLE users ADD COLUMN ref_count INT DEFAULT 0",
    "ALTER TABLE users ADD COLUMN remind_hour INT DEFAULT 20",
    "ALTER TABLE users ADD COLUMN remind_on INT DEFAULT 1",
    "ALTER TABLE users ADD COLUMN last_reminded TEXT DEFAULT ''",
    "ALTER TABLE users ADD COLUMN tz_offset INT DEFAULT 180",
):
    try:
        run(_ddl)
    except sqlite3.OperationalError:
        pass

run("CREATE TABLE IF NOT EXISTS water(uid INT, day TEXT, ml INT DEFAULT 0, PRIMARY KEY(uid, day))")
run("""CREATE TABLE IF NOT EXISTS favs(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INT, name TEXT,
    grams REAL, kcal REAL, p REAL, f REAL, c REAL)""")
run("""CREATE TABLE IF NOT EXISTS payments(charge_id TEXT PRIMARY KEY, uid INT, item_id TEXT, stars INT, ts INT)""")
run("""CREATE TABLE IF NOT EXISTS weights(uid INT, day TEXT, weight REAL, PRIMARY KEY(uid, day))""")


def get_user(uid, name="", ref_by=None):
    existing = run("SELECT * FROM users WHERE id=?", (uid,))
    if not existing:
        valid_ref = None
        if ref_by and ref_by != uid:
            ref_u = run("SELECT id FROM users WHERE id=?", (ref_by,))
            if ref_u:
                valid_ref = ref_by
        run("INSERT OR IGNORE INTO users(id, name, extra_ai_credits, ref_by) VALUES(?,?,?,?)",
            (uid, name, 5 if valid_ref else 0, valid_ref))
        if valid_ref:
            run("UPDATE users SET extra_ai_credits=COALESCE(extra_ai_credits,0)+5, ref_count=COALESCE(ref_count,0)+1 WHERE id=?", (valid_ref,))
    elif name:
        run("UPDATE users SET name=? WHERE id=? AND (name IS NULL OR name='')", (name, uid))
    return run("SELECT * FROM users WHERE id=?", (uid,))[0]


# ---------- Auth (Telegram initData) ----------
def auth(init: str) -> dict:
    try:
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


# ---------- Расчёт нормы (Миффлин — Сан Жеор) ----------
def calc_norms(sex, age, height, weight, act, goal, pace=0.5):
    bmr = 10 * weight + 6.25 * height - 5 * age + (5 if sex == "m" else -161)
    kcal = bmr * act + {"lose": -1, "keep": 0, "gain": 1}[goal] * pace * 1100
    kcal = max(kcal, 1500 if sex == "m" else 1200)
    p = weight * {"lose": 2.0, "keep": 1.6, "gain": 1.8}[goal]
    f = weight * 0.9
    c = max((kcal - p * 4 - f * 9) / 4, 0)
    return {"kcal": round(kcal), "p": round(p), "f": round(f), "c": round(c)}


def water_goal(weight):
    return int(min(max(round((weight or 66) * 30 / 50) * 50, 1500), 4000))


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
]
QUICK = {"Греческий йогурт", "Творог 5%", "Банан", "Яблоко", "Миндаль", "Авокадо", "Кефир 1%",
         "Тунец в собственном соку", "Арахисовая паста", "Огурцы и помидоры"}


def suggest(rem, mode="", seed=0):
    if rem["kcal"] < 70:
        return []
    rnd, out = random.Random(seed), []
    for name, k, p, f, c, port in FOODS:
        if (mode == "quick" and name not in QUICK) or (mode == "light" and k > 100):
            continue
        cap = min(rem["kcal"], 200) if mode == "light" else rem["kcal"]
        g = min(port, int(cap / k * 100 // 10 * 10))
        if g < 30:
            continue
        m = {"kcal": k * g / 100, "p": p * g / 100, "f": f * g / 100, "c": c * g / 100}
        gain = (6 if mode == "protein" else 3) * min(m["p"], max(rem["p"], 0)) \
            + min(m["f"], max(rem["f"], 0)) + min(m["c"], max(rem["c"], 0))
        over = 3 * max(m["p"] - rem["p"], 0) + 3 * max(m["f"] - rem["f"], 0) + 2 * max(m["c"] - rem["c"], 0)
        out.append((gain - over + (rnd.random() * 5 if seed else 0),
                    {"name": name, "grams": g, **{x: round(v, 1) for x, v in m.items()}}))
    out.sort(key=lambda t: -t[0])
    return [o for sc, o in out[:5] if sc > 0]


# ---------- ИИ с Retry + Fallback ----------
PROMPT_FOOD = (
    'Ты опытный нутрициолог. Определи блюда/продукты (или прочитай таблицу пищевой ценности на этикетке/упаковке, если на фото этикетка). '
    'Оцени вес порции и КБЖУ. Отвечай ТОЛЬКО валидным JSON без markdown: '
    '{"items":[{"name":"название","grams":число,"kcal":число,"p":число,"f":число,"c":число}]}. '
    'Значения — на всю съеденную порцию (или указанную в комментарии порцию), p/f/c в граммах. '
    'Если еды или данных нет — {"items":[]}. Названия по-русски.'
)

PROMPT_MEALPLAN = (
    'Ты персональный нутрициолог. Составь вкусный, реалистичный и сбалансированный план питания из простых продуктов строго под целевые КБЖУ. '
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


def _err(msg) -> str:
    msg = " ".join(str(msg).split())
    if AI_KEY:
        msg = msg.replace(AI_KEY, "***")
    return msg[:200]


IMG_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def split_image(image: str):
    image, mime = image.strip(), "image/jpeg"
    if image.startswith("data:"):
        head, _, image = image.partition(",")
        m = head[5:].split(";")[0].strip().lower()
        m = "image/jpeg" if m == "image/jpg" else m
        if m in IMG_MIME:
            mime = m
    return mime, image.strip()


def _model_candidates() -> list[str]:
    seen, out = set(), []
    for m in [AI_MODEL, *AI_FALLBACK_MODELS]:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


async def _call_gemini_raw(system_prompt: str, parts: list, schema: dict, temp: float = 0.2) -> str:
    if not AI_KEY:
        raise HTTPException(503, "ИИ не настроен: не задан GEMINI_API_KEY")

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
    models = _model_candidates()
    last_err = "Gemini: неизвестная ошибка"

    async with httpx.AsyncClient(timeout=55) as cl:
        for model in models:
            url = AI_URL.format(model)
            # До 2 попыток на модель при 429/500/502/503/504
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
                        log.warning("Gemini request failed (%s, %s): %r", model, mode, e)
                        r = None
                        break
                    if r.status_code not in (401, 403):
                        break

                if r is None:
                    await asyncio.sleep(0.8)
                    continue

                if r.status_code in (429, 500, 502, 503, 504):
                    try:
                        msg = r.json()["error"]["message"]
                    except Exception:
                        msg = r.text[:120]
                    last_err = f"Gemini {r.status_code} ({model}): {msg}"
                    log.warning("Transient %s on %s (attempt %d): %s", r.status_code, model, attempt + 1, msg)
                    await asyncio.sleep(1.0 * (attempt + 1))
                    continue

                if r.status_code == 404:
                    last_err = f"Модель {model} недоступна (404)"
                    log.warning("Model 404: %s", model)
                    break  # сразу к следующей модели

                if r.status_code >= 400:
                    try:
                        msg = r.json()["error"]["message"]
                    except Exception:
                        msg = r.text
                    log.error("Gemini HTTP %s (%s): %s", r.status_code, model, r.text[:1000])
                    raise HTTPException(502, _err(f"Gemini {r.status_code}: {msg}"))

                try:
                    data = r.json()
                    cands = data.get("candidates") or []
                    if not cands:
                        raise ValueError(f"no candidates: {data.get('promptFeedback')}")
                    raw = "".join(p.get("text", "") for p in (cands[0].get("content") or {}).get("parts", []) if not p.get("thought"))
                    if not raw.strip():
                        raise ValueError("empty text")
                    return raw
                except Exception as e:
                    last_err = f"Gemini ответ ({model}): {e}"
                    log.warning("Bad response payload from %s: %r", model, e)
                    break

    raise HTTPException(502, _err(last_err))


async def ask_ai(text: str, image: str, label_mode: bool = False):
    parts = []
    if image:
        mime, b64 = split_image(image)
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
        m = re.search(r"\{.*\}", raw, re.S)
        items = json.loads(m.group(0))["items"]
        return [Item(**i).model_dump() for i in items][:15]
    except Exception as e:
        log.error("Gemini bad JSON (%s): %s", type(e).__name__, raw[:1000])
        raise HTTPException(502, _err(f"Не удалось разобрать ответ ИИ: {raw}"))


async def ask_mealplan_ai(rem: dict, prefs: str = ""):
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
    prompt = (
        f"Составь меню на остаток дня (или на весь день, если ещё ничего не съедено). "
        f"Целевые показатели: {max(round(rem['kcal']), 200)} ккал, Белки {max(round(rem['p']), 10)} г, "
        f"Жиры {max(round(rem['f']), 5)} г, Углеводы {max(round(rem['c']), 10)} г. "
        f"Пожелания пользователя: {prefs.strip() or 'обычные доступные продукты, без экзотики'}. "
        f"В поле meal используй строго одно из значений: breakfast, lunch, dinner, snack."
    )
    raw = await _call_gemini_raw(PROMPT_MEALPLAN, [{"text": prompt}], schema, temp=0.4)
    try:
        m = re.search(r"\{.*\}", raw, re.S)
        data = json.loads(m.group(0))
        meals = []
        for ml in (data.get("meals") or [])[:4]:
            m_key = ml.get("meal") if ml.get("meal") in MEALS else "snack"
            valid_items = [Item(**it).model_dump() for it in (ml.get("items") or [])[:6]]
            if valid_items:
                meals.append({"meal": m_key, "title": str(ml.get("title") or m_key)[:60], "items": valid_items})
        return {"meals": meals, "tip": str(data.get("tip") or "")[:240]}
    except Exception as e:
        log.error("Mealplan JSON error: %r", e)
        raise HTTPException(502, "Не удалось сгенерировать рацион, попробуйте ещё раз")


def ai_status(u):
    pro = (u["pro_until"] or 0) > time.time()
    used = (u["ai_n"] or 0) if u["ai_day"] == date.today().isoformat() else 0
    daily = PRO_LIMIT if pro else FREE_LIMIT
    return {
        "pro": pro,
        "is_pro": pro,
        "used": used,
        "used_today": used,
        "free_daily_limit": FREE_LIMIT,
        "daily_limit": daily,
        "remaining_daily_today": max(daily - used, 0),
        "extra_ai_credits": u["extra_ai_credits"] or 0,
        "mealplan_credits": u["mealplan_credits"] or 0,
        "pro_until": u["pro_until"] or 0,
    }


# ---------- Bot & Reminders ----------
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
    get_user(m.from_user.id, m.from_user.full_name, ref_by=ref_id)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🥗 Открыть дневник", web_app=WebAppInfo(url=BASE_URL))
    ]])
    bonus_txt = "\n🎁 По приглашению друга тебе уже начислено +5 бонусных ИИ-запросов!" if ref_id else ""
    await m.answer(
        "Привет! Я <b>SnapEat</b> — умный дневник питания и КБЖУ.\n\n"
        "📸 Фотографируй еду или этикетки, пиши текстом или выбирай из базы — я сам посчитаю калории, белки, жиры и углеводы."
        f"{bonus_txt}",
        parse_mode="HTML",
        reply_markup=kb,
    )


def parse_payload(payload):
    try:
        uid, item, _nonce = payload.split(":")
        return (int(uid), item) if item in CATALOG else (None, None)
    except Exception:
        return None, None


def credit_payment(uid, item_id, charge_id, stars) -> bool:
    it = CATALOG[item_id]
    with lock:
        try:
            db.execute("INSERT INTO payments(charge_id,uid,item_id,stars,ts) VALUES(?,?,?,?,?)",
                       (charge_id, uid, item_id, stars, int(time.time())))
        except sqlite3.IntegrityError:
            return False
        try:
            if it["kind"] == "pack":
                db.execute("UPDATE users SET extra_ai_credits=COALESCE(extra_ai_credits,0)+? WHERE id=?",
                           (it["credits"], uid))
            elif it["kind"] == "mealplan":
                db.execute("UPDATE users SET mealplan_credits=COALESCE(mealplan_credits,0)+? WHERE id=?",
                           (it["credits"], uid))
            else:
                row = db.execute("SELECT pro_until FROM users WHERE id=?", (uid,)).fetchone()
                until = int(max(time.time(), row["pro_until"] or 0) + it["days"] * 86400)
                db.execute("UPDATE users SET pro_until=? WHERE id=?", (until, uid))
            db.commit()
        except Exception:
            db.rollback()
            raise
    return True


@dp.pre_checkout_query()
async def on_pre_checkout(q: PreCheckoutQuery):
    uid, item = parse_payload(q.invoice_payload)
    ok = bool(item) and uid == q.from_user.id and q.currency == "XTR" and q.total_amount == CATALOG[item]["price"]
    if ok:
        await q.answer(ok=True)
    else:
        log.warning("pre_checkout rejected: %s %s %s", q.invoice_payload, q.currency, q.total_amount)
        await q.answer(ok=False, error_message="Не удалось проверить заказ. Откройте оплату в приложении заново.")


@dp.message(F.successful_payment)
async def on_paid(m: Message):
    sp = m.successful_payment
    uid, item = parse_payload(sp.invoice_payload)
    if not item or uid != m.from_user.id or sp.currency != "XTR" or sp.total_amount != CATALOG[item]["price"]:
        log.error("bad successful_payment: %s %s %s", sp.invoice_payload, sp.currency, sp.total_amount)
        return
    get_user(uid, m.from_user.full_name)
    if not credit_payment(uid, item, sp.telegram_payment_charge_id, sp.total_amount):
        return
    u, it = get_user(uid), CATALOG[item]
    if it["kind"] == "pack":
        text = f"⭐ Начислено +{it['credits']} ИИ-запросов. На балансе: {u['extra_ai_credits']}."
    elif it["kind"] == "mealplan":
        text = f"🥗 Оплачен персональный ИИ-рацион! Открой вкладку «Рацион» в приложении, чтобы сгенерировать меню."
    else:
        text = f"👑 SnapEat Pro активен до {datetime.fromtimestamp(u['pro_until']):%d.%m.%Y}. Спасибо!"
    try:
        await m.answer(text)
    except Exception as e:
        log.warning("payment credited, but notify failed: %r", e)


async def reminder_loop():
    """Фоновый цикл вечерних напоминаний пользователям."""
    while True:
        await asyncio.sleep(900)  # проверка каждые 15 минут
        if not BASE_URL:
            continue
        try:
            now_utc = datetime.utcnow()
            users = run("SELECT id, name, kcal, remind_hour, remind_on, last_reminded, tz_offset FROM users WHERE remind_on=1 AND kcal IS NOT NULL")
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
                _, tot = day_totals(u["id"], local_day)
                rem_kcal = round((u["kcal"] or 0) - tot["kcal"])
                run("UPDATE users SET last_reminded=? WHERE id=?", (local_day, u["id"]))
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
            log.warning("reminder_loop error: %r", e)


ALLOWED_UPDATES = ["message", "pre_checkout_query"]


@asynccontextmanager
async def lifespan(app):
    global BOT_USERNAME
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
            log.error("webhook setup failed: %r", e)
    task = asyncio.create_task(reminder_loop())
    yield
    task.cancel()
    await bot.session.close()


app = FastAPI(lifespan=lifespan)


@app.post("/webhook/{secret}")
async def webhook(secret: str, req: Request):
    if secret != WH_SECRET or req.headers.get("X-Telegram-Bot-Api-Secret-Token") != WH_SECRET:
        raise HTTPException(403)
    await dp.feed_update(bot, Update.model_validate(await req.json(), context={"bot": bot}))
    return {"ok": True}


# ---------- API ----------
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
async def state(day: str = "", today: str = "", tz: int = 180, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    u = get_user(tu["id"], tu.get("first_name", ""))
    if -720 <= tz <= 840 and u.get("tz_offset") != tz:
        run("UPDATE users SET tz_offset=? WHERE id=?", (tz, u["id"]))
    day = norm_day(day)
    ai = ai_status(u)
    entries, total = day_totals(u["id"], day)
    has = u["kcal"] is not None
    norm = {k: u[k] for k in ("kcal", "p", "f", "c")} if has else None
    rem = {k: norm[k] - total[k] for k in total} if has else None
    prof = {k: u[k] for k in ("sex", "age", "height", "weight", "start_weight", "act", "goal", "target_weight", "pace", "manual", "remind_hour", "remind_on")} if has else None
    w = run("SELECT ml FROM water WHERE uid=? AND day=?", (u["id"], day))
    recent = run(
        "SELECT name, grams, kcal, p, f, c, MAX(ts) as last_ts FROM entries WHERE uid=? "
        "GROUP BY name, ROUND(grams) ORDER BY last_ts DESC LIMIT 25",
        (u["id"],),
    )
    weight_log = run("SELECT day, weight FROM weights WHERE uid=? ORDER BY day DESC LIMIT 30", (u["id"],))
    weight_log.reverse()
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref_{u['id']}" if BOT_USERNAME else ""
    return {
        "profile": prof,
        "norm": norm,
        "total": total,
        "entries": entries,
        "ai": ai,
        **{k: ai[k] for k in ("free_daily_limit", "daily_limit", "used_today", "remaining_daily_today", "extra_ai_credits", "mealplan_credits", "is_pro", "pro_until")},
        "suggest": suggest(rem) if has else [],
        "water": w[0]["ml"] if w else 0,
        "water_goal": water_goal(u["weight"]),
        "streak": calc_streak(u["id"], date.today().isoformat()),
        "favs": run("SELECT * FROM favs WHERE uid=? ORDER BY id DESC LIMIT 40", (u["id"],)),
        "recent": recent,
        "weight_log": weight_log,
        "ref_link": ref_link,
        "ref_count": u.get("ref_count") or 0,
        "catalog": CATALOG_LIST,
    }


@app.get("/api/suggest")
async def api_suggest(day: str = "", mode: str = "", seed: int = 0, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    u = get_user(tu["id"], tu.get("first_name", ""))
    if u["kcal"] is None:
        return {"items": []}
    _, t = day_totals(u["id"], norm_day(day))
    return {"items": suggest({k: u[k] - t[k] for k in t}, mode if mode in ("protein", "quick", "light") else "", seed)}


@app.get("/api/week")
async def week(day: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    end = date.fromisoformat(norm_day(day))
    start = end - timedelta(days=6)
    rows = run("SELECT day, SUM(kcal) kcal, SUM(p) p, SUM(f) f, SUM(c) c FROM entries "
               "WHERE uid=? AND day BETWEEN ? AND ? GROUP BY day", (tu["id"], start.isoformat(), end.isoformat()))
    by = {r["day"]: r for r in rows}
    out = []
    for i in range(7):
        d = (start + timedelta(days=i)).isoformat()
        out.append({"day": d, **{k: round((by.get(d) or {}).get(k) or 0) for k in ("kcal", "p", "f", "c")}})
    return {"days": out}


class Profile(BaseModel):
    sex: Literal["m", "f"]
    age: int = Field(ge=10, le=100)
    height: float = Field(ge=100, le=250)
    weight: float = Field(ge=30, le=300)
    act: float = Field(ge=1.2, le=1.9)
    goal: Literal["lose", "keep", "gain"]
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
async def save_profile(p: Profile, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    cur_u = get_user(tu["id"], tu.get("first_name", ""))
    if p.manual:
        if None in (p.kcal, p.p, p.f, p.c):
            raise HTTPException(400, "Укажите калории, белки, жиры и углеводы")
        n = {"kcal": p.kcal, "p": p.p, "f": p.f, "c": p.c}
    else:
        n = calc_norms(p.sex, p.age, p.height, p.weight, p.act, p.goal, p.pace)
    start_w = cur_u.get("start_weight") or (p.weight if p.target_weight else None)
    run(
        "UPDATE users SET sex=?,age=?,height=?,weight=?,start_weight=?,act=?,goal=?,target_weight=?,pace=?,manual=?,"
        "kcal=?,p=?,f=?,c=?,remind_on=?,remind_hour=? WHERE id=?",
        (p.sex, p.age, p.height, p.weight, start_w, p.act, p.goal, p.target_weight, p.pace, int(p.manual),
         n["kcal"], n["p"], n["f"], n["c"], int(p.remind_on), p.remind_hour, tu["id"]),
    )
    run("INSERT INTO weights(uid,day,weight) VALUES(?,?,?) ON CONFLICT(uid,day) DO UPDATE SET weight=excluded.weight",
        (tu["id"], date.today().isoformat(), p.weight))
    return {"norm": n}


class WeightLogReq(BaseModel):
    day: str = ""
    weight: float = Field(ge=30, le=300)


@app.post("/api/weight")
async def log_weight(r: WeightLogReq, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    d = norm_day(r.day)
    run("INSERT INTO weights(uid,day,weight) VALUES(?,?,?) ON CONFLICT(uid,day) DO UPDATE SET weight=excluded.weight",
        (tu["id"], d, r.weight))
    run("UPDATE users SET weight=? WHERE id=?", (r.weight, tu["id"]))
    return {"ok": True}


class AddReq(BaseModel):
    day: str
    items: list[Item] = Field(min_length=1, max_length=25)
    src: Literal["manual", "text", "photo", "suggest", "fav", "copy", "mealplan"] = "manual"
    meal: Literal["breakfast", "lunch", "dinner", "snack"] = "snack"


@app.post("/api/entry")
async def add_entry(r: AddReq, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    get_user(tu["id"], tu.get("first_name", ""))
    day = norm_day(r.day)
    for i, it in enumerate(r.items):
        run("INSERT INTO entries(uid,day,name,grams,kcal,p,f,c,src,ts,meal) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (tu["id"], day, it.name, it.grams, it.kcal, it.p, it.f, it.c, r.src, int(time.time() * 1000) + i, r.meal))
    return {"ok": True}


class CopyMealReq(BaseModel):
    from_day: str
    to_day: str
    meal: Literal["breakfast", "lunch", "dinner", "snack"]


@app.post("/api/copy-meal")
async def copy_meal(r: CopyMealReq, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    fd, td = norm_day(r.from_day), norm_day(r.to_day)
    rows = run("SELECT * FROM entries WHERE uid=? AND day=? AND meal=? ORDER BY ts", (tu["id"], fd, r.meal))
    if not rows:
        raise HTTPException(404, "В выбранном приёме пищи ничего нет")
    for i, e in enumerate(rows):
        run("INSERT INTO entries(uid,day,name,grams,kcal,p,f,c,src,ts,meal) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (tu["id"], td, e["name"], e["grams"], e["kcal"], e["p"], e["f"], e["c"], "copy", int(time.time() * 1000) + i, r.meal))
    return {"ok": True, "count": len(rows)}


class EditReq(BaseModel):
    grams: float = Field(gt=0, le=5000)


@app.patch("/api/entry/{eid}")
async def edit_entry(eid: int, r: EditReq, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    rows = run("SELECT * FROM entries WHERE id=? AND uid=?", (eid, tu["id"]))
    if not rows:
        raise HTTPException(404, "Запись не найдена")
    e = rows[0]
    k = r.grams / e["grams"] if e["grams"] > 0 else 1
    run("UPDATE entries SET grams=?,kcal=?,p=?,f=?,c=? WHERE id=?",
        (r.grams, e["kcal"] * k, e["p"] * k, e["f"] * k, e["c"] * k, eid))
    return {"ok": True}


@app.delete("/api/entry/{eid}")
async def del_entry(eid: int, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    run("DELETE FROM entries WHERE id=? AND uid=?", (eid, tu["id"]))
    return {"ok": True}


class WaterReq(BaseModel):
    day: str
    ml: int = Field(ge=-2000, le=2000)


@app.post("/api/water")
async def add_water(r: WaterReq, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    day = norm_day(r.day)
    cur = run("SELECT ml FROM water WHERE uid=? AND day=?", (tu["id"], day))
    new = max(0, min(10000, (cur[0]["ml"] if cur else 0) + r.ml))
    run("INSERT INTO water(uid,day,ml) VALUES(?,?,?) ON CONFLICT(uid,day) DO UPDATE SET ml=excluded.ml",
        (tu["id"], day, new))
    return {"water": new}


@app.post("/api/fav")
async def add_fav(it: Item, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    if not run("SELECT id FROM favs WHERE uid=? AND name=? AND ROUND(grams)=ROUND(?)", (tu["id"], it.name, it.grams)):
        run("INSERT INTO favs(uid,name,grams,kcal,p,f,c) VALUES(?,?,?,?,?,?,?)",
            (tu["id"], it.name, it.grams, it.kcal, it.p, it.f, it.c))
    return {"ok": True}


@app.delete("/api/fav/{fid}")
async def del_fav(fid: int, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    run("DELETE FROM favs WHERE id=? AND uid=?", (fid, tu["id"]))
    return {"ok": True}


class AIReq(BaseModel):
    text: str = Field("", max_length=1000)
    image: str = Field("", max_length=4_000_000)
    label_mode: bool = False


@app.post("/api/ai")
async def ai_analyze(r: AIReq, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    u = get_user(tu["id"], tu.get("first_name", ""))
    if not r.text.strip() and not r.image:
        raise HTTPException(400, "Опишите еду или приложите фото")
    daily, today = ai_status(u)["daily_limit"], date.today().isoformat()
    if run_rc("UPDATE users SET ai_n=CASE WHEN ai_day=? THEN COALESCE(ai_n, 0) + 1 ELSE 1 END, ai_day=? "
              "WHERE id=? AND (CASE WHEN ai_day=? THEN COALESCE(ai_n, 0) ELSE 0 END) < ?",
              (today, today, u["id"], today, daily)):
        source = "daily"
    elif run_rc("UPDATE users SET extra_ai_credits=max(COALESCE(extra_ai_credits, 0) - 1, 0) "
                "WHERE id=? AND COALESCE(extra_ai_credits, 0) > 0", (u["id"],)):
        source = "extra"
    else:
        raise HTTPException(402, "Запросы закончились. Пополните баланс за звёзды или подключите Pro")
    try:
        items = await ask_ai(r.text, r.image, label_mode=r.label_mode)
    except BaseException:
        if source == "daily":
            run("UPDATE users SET ai_n=max(COALESCE(ai_n, 0) - 1, 0) WHERE id=? AND ai_day=?", (u["id"], today))
        else:
            run("UPDATE users SET extra_ai_credits=COALESCE(extra_ai_credits, 0) + 1 WHERE id=?", (u["id"],))
        raise
    return {"items": items, "source": source, "ai": ai_status(get_user(u["id"]))}


class MealPlanReq(BaseModel):
    day: str = ""
    prefs: str = Field("", max_length=300)


@app.post("/api/ai/mealplan")
async def ai_mealplan(r: MealPlanReq, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    u = get_user(tu["id"], tu.get("first_name", ""))
    if u["kcal"] is None:
        raise HTTPException(400, "Сначала заполните профиль")
    st = ai_status(u)
    source = "pro"
    if not st["is_pro"]:
        if run_rc("UPDATE users SET mealplan_credits=max(COALESCE(mealplan_credits,0)-1,0) WHERE id=? AND COALESCE(mealplan_credits,0)>0", (u["id"],)):
            source = "ticket"
        else:
            raise HTTPException(402, "Генерация рациона доступна в PRO или по разовому билету за 35 ⭐")

    _, tot = day_totals(u["id"], norm_day(r.day))
    rem = {k: (u[k] or 0) - tot[k] for k in tot}
    if rem["kcal"] < 250:
        rem = {k: u[k] or 0 for k in ("kcal", "p", "f", "c")}

    try:
        plan_data = await ask_mealplan_ai(rem, r.prefs)
    except BaseException:
        if source == "ticket":
            run("UPDATE users SET mealplan_credits=COALESCE(mealplan_credits,0)+1 WHERE id=?", (u["id"],))
        raise
    return {**plan_data, "ai": ai_status(get_user(u["id"]))}


class InvoiceReq(BaseModel):
    item_id: Literal["pack_20", "pack_60", "pack_150", "pro_30d", "pro_90d", "pro_365d", "mealplan_1"]


@app.post("/api/billing/create-invoice")
async def create_invoice(r: InvoiceReq, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    get_user(tu["id"], tu.get("first_name", ""))
    it = CATALOG[r.item_id]
    payload = f"{tu['id']}:{r.item_id}:{uuid.uuid4().hex[:12]}"
    try:
        link = await bot.create_invoice_link(
            title=it["title"], description=it["desc"], payload=payload, provider_token="", currency="XTR",
            prices=[LabeledPrice(label=it["title"], amount=it["price"])])
    except Exception as e:
        log.error("create_invoice_link failed: %r", e)
        raise HTTPException(502, _err(f"Telegram: не удалось создать счёт ({type(e).__name__}: {e})"))
    return {"invoice_url": link, "item_id": r.item_id, "stars": it["price"]}


STATIC_DIR = Path(__file__).resolve().parent / "static"


@app.get("/health")
async def health():
    run("SELECT 1")
    return {"ok": True, "webhook": bool(BASE_URL), "ai": bool(AI_KEY), "models": _model_candidates()}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
