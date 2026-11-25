import os, json, hmac, hashlib, time, sqlite3, threading, re, logging, random, uuid
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
AI_MODEL = os.getenv("AI_MODEL", "gemini-3.8-flash")
log = logging.getLogger("uvicorn.error")
AI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
FREE_LIMIT = int(os.getenv("FREE_AI_LIMIT", "5"))      # ИИ-запросов в день без подписки
PRO_LIMIT = int(os.getenv("PRO_AI_LIMIT", "35"))       # ИИ-запросов в день с PRO
DB_PATH = os.getenv("DB_PATH", "data.db")
WH_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
CATALOG = {  # цены в целых звёздах Telegram (XTR)
    "pack_20": {"kind": "pack", "credits": 20, "price": 25, "title": "Старт",
                "desc": "+20 ИИ-запросов на баланс. Не сгорают в конце дня."},
    "pack_60": {"kind": "pack", "credits": 60, "price": 60, "title": "Выгодный", "badge": "Популярный",
                "desc": "+60 ИИ-запросов на баланс. Не сгорают в конце дня."},
    "pack_150": {"kind": "pack", "credits": 150, "price": 130, "title": "Запас",
                 "desc": "+150 ИИ-запросов на баланс. Не сгорают в конце дня."},
    "pro_30d": {"kind": "pro", "days": 30, "price": 149, "title": "SnapEat Pro на 30 дней",
                "desc": f"До {PRO_LIMIT} ИИ-запросов в сутки вместо {FREE_LIMIT}, расширенная аналитика недели. "
                        "Продлевает действующую подписку."},
}
CATALOG_LIST = [{"id": k, **v} for k, v in CATALOG.items()]

# ---------- SQLite ----------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
lock = threading.Lock()


def run(sql, args=()):
    with lock:
        cur = db.execute(sql, args)
        db.commit()
        return [dict(r) for r in cur.fetchall()]


run("""CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, name TEXT, sex TEXT, age INT, height REAL, weight REAL, act REAL, goal TEXT,
    kcal INT, p INT, f INT, c INT, pro_until INT DEFAULT 0, ai_day TEXT DEFAULT '', ai_n INT DEFAULT 0)""")
run("""CREATE TABLE IF NOT EXISTS entries(
    id INTEGER PRIMARY KEY AUTOINCREMENT, uid INT, day TEXT, name TEXT, grams REAL,
    kcal REAL, p REAL, f REAL, c REAL, src TEXT, ts INT)""")
run("CREATE INDEX IF NOT EXISTS ix_entries ON entries(uid, day)")

for _ddl in ("ALTER TABLE users ADD COLUMN target_weight REAL", "ALTER TABLE users ADD COLUMN start_weight REAL", "ALTER TABLE users ADD COLUMN pace REAL DEFAULT 0.5",
             "ALTER TABLE users ADD COLUMN manual INT DEFAULT 0", "ALTER TABLE entries ADD COLUMN meal TEXT DEFAULT 'snack'"):
    try:
        run(_ddl)
    except sqlite3.OperationalError:
        pass
run("CREATE TABLE IF NOT EXISTS water(uid INT, day TEXT, ml INT DEFAULT 0, PRIMARY KEY(uid, day))")
run("""CREATE TABLE IF NOT EXISTS favs(id INTEGER PRIMARY KEY AUTOINCREMENT, uid INT, name TEXT,
    grams REAL, kcal REAL, p REAL, f REAL, c REAL)""")
try:
    run("ALTER TABLE users ADD COLUMN extra_ai_credits INT DEFAULT 0")
except sqlite3.OperationalError:
    pass
run("""CREATE TABLE IF NOT EXISTS payments(charge_id TEXT PRIMARY KEY, uid INT, item_id TEXT, stars INT, ts INT)""")


def run_rc(sql, args=()):
    """Выполнить UPDATE/INSERT и вернуть число затронутых строк (для атомарных резервов)."""
    with lock:
        cur = db.execute(sql, args)
        db.commit()
        return cur.rowcount


def get_user(uid, name=""):
    run("INSERT OR IGNORE INTO users(id, name) VALUES(?,?)", (uid, name))
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
    kcal = bmr * act + {"lose": -1, "keep": 0, "gain": 1}[goal] * pace * 1100  # 1 кг ≈ 7700 ккал
    kcal = max(kcal, 1500 if sex == "m" else 1200)
    p = weight * {"lose": 2.0, "keep": 1.6, "gain": 1.8}[goal]
    f = weight * 0.9
    c = max((kcal - p * 4 - f * 9) / 4, 0)
    return {"kcal": round(kcal), "p": round(p), "f": round(f), "c": round(c)}


def water_goal(weight):
    return int(min(max(round((weight or 66) * 30 / 50) * 50, 1500), 4000))


# name, ккал/100г, Б, Ж, У (на 100 г), типичная порция, г
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


# ---------- ИИ ----------
PROMPT = ('Ты нутрициолог. Определи блюда/продукты, оцени вес порции и КБЖУ. Отвечай ТОЛЬКО JSON без markdown: '
          '{"items":[{"name":"название","grams":число,"kcal":число,"p":число,"f":число,"c":число}]}. '
          'Значения — на всю съеденную порцию, p/f/c в граммах. Если еды нет — {"items":[]}. Названия по-русски.')


class Item(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    grams: float = Field(0, ge=0, le=5000)
    kcal: float = Field(ge=0, le=5000)
    p: float = Field(0, ge=0, le=500)
    f: float = Field(0, ge=0, le=500)
    c: float = Field(0, ge=0, le=1000)


def _err(msg) -> str:
    """Одна строка до 200 символов, без ключа."""
    msg = " ".join(str(msg).split())
    if AI_KEY:
        msg = msg.replace(AI_KEY, "***")
    return msg[:200]


IMG_MIME = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def split_image(image: str):
    """'data:image/png;base64,XXXX' -> ('image/png', 'XXXX'); без префикса или с неизвестным типом — image/jpeg."""
    image, mime = image.strip(), "image/jpeg"
    if image.startswith("data:"):
        head, _, image = image.partition(",")
        m = head[5:].split(";")[0].strip().lower()
        m = "image/jpeg" if m == "image/jpg" else m
        if m in IMG_MIME:
            mime = m
    return mime, image.strip()


async def ask_ai(text: str, image: str):
    if not AI_KEY:
        raise HTTPException(503, "ИИ не настроен: не задан GEMINI_API_KEY")
    parts = []
    if image:
        mime, b64 = split_image(image)
        parts.append({"inline_data": {"mime_type": mime, "data": b64}})
    parts.append({"text": text.strip() or "Что на фото? Оцени порцию и КБЖУ."})

    num = {"type": "NUMBER"}
    schema = {"type": "OBJECT", "required": ["items"], "properties": {"items": {"type": "ARRAY", "items": {
        "type": "OBJECT", "required": ["name", "grams", "kcal", "p", "f", "c"],
        "properties": {"name": {"type": "STRING"}, "grams": num, "kcal": num, "p": num, "f": num, "c": num}}}}}
    body = {
        "system_instruction": {"parts": [{"text": PROMPT}]},
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 2048,
                             "responseMimeType": "application/json", "responseSchema": schema,
                             "thinkingConfig": {"thinkingBudget": 0}},
    }
    url = AI_URL.format(AI_MODEL)
    modes = ["bearer", "query"] if AI_KEY.startswith("AQ.") else ["query"]

    # 1) запрос (для ключей AQ. сначала Bearer, при 401/403 повтор через ?key=)
    r = None
    async with httpx.AsyncClient(timeout=60) as cl:
        for mode in modes:
            headers, params = {"Content-Type": "application/json"}, {}
            if mode == "bearer":
                headers["Authorization"] = f"Bearer {AI_KEY}"
            else:
                params["key"] = AI_KEY
            try:
                r = await cl.post(url, headers=headers, params=params, json=body)
            except Exception as e:
                log.error("Gemini request failed (%s): %r", mode, e)
                raise HTTPException(502, _err(f"Gemini: сеть ({mode}) {type(e).__name__}: {e}"))
            if r.status_code not in (401, 403):
                break

    # 2) HTTP-ошибка
    if r.status_code >= 400:
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = r.text
        log.error("Gemini HTTP %s (%s): %s", r.status_code, AI_MODEL, r.text[:1500])
        raise HTTPException(502, _err(f"Gemini {r.status_code}: {msg}"))

    # 3) разбор ответа
    try:
        data = r.json()
    except ValueError:
        log.error("Gemini non-JSON body: %s", r.text[:1500])
        raise HTTPException(502, _err(f"Gemini: ответ не JSON: {r.text}"))
    cands = data.get("candidates") or []
    if not cands:
        log.error("Gemini no candidates: %s", json.dumps(data, ensure_ascii=False)[:1500])
        raise HTTPException(502, _err(f"Gemini: нет ответа, promptFeedback={data.get('promptFeedback')}"))
    cand = cands[0]
    raw = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts", []) if not p.get("thought"))
    if not raw.strip():
        log.error("Gemini empty text: %s", json.dumps(cand, ensure_ascii=False)[:1500])
        raise HTTPException(502, _err(f"Gemini: пустой текст, finishReason={cand.get('finishReason')}"))
    try:
        m = re.search(r"\{.*\}", raw, re.S)
        items = json.loads(m.group(0))["items"]
        return [Item(**i).model_dump() for i in items][:15]
    except Exception as e:
        log.error("Gemini bad JSON (%s): %s", type(e).__name__, raw[:1500])
        raise HTTPException(502, _err(f"Gemini: невалидный ответ ({type(e).__name__}): {raw}"))


def ai_status(u):
    pro = u["pro_until"] > time.time()
    used = (u["ai_n"] or 0) if u["ai_day"] == date.today().isoformat() else 0
    daily = PRO_LIMIT if pro else FREE_LIMIT
    return {"pro": pro, "is_pro": pro, "used": used, "used_today": used,
            "free_daily_limit": FREE_LIMIT,      # бесплатный лимит — всегда FREE_LIMIT
            "daily_limit": daily,                # действующий дневной лимит (PRO_LIMIT при активном Pro)
            "remaining_daily_today": max(daily - used, 0),
            "extra_ai_credits": u["extra_ai_credits"] or 0, "pro_until": u["pro_until"]}


# ---------- Bot ----------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def on_start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🥗 Открыть дневник", web_app=WebAppInfo(url=BASE_URL))]])
    await m.answer("Привет! Я считаю калории и БЖУ. Заполните профиль, и я покажу, сколько ещё можно съесть сегодня. "
                   "Еду можно добавить текстом, фото или вручную.", reply_markup=kb)


def parse_payload(payload):
    """payload = '<user_id>:<item_id>:<nonce>'"""
    try:
        uid, item, _nonce = payload.split(":")
        return (int(uid), item) if item in CATALOG else (None, None)
    except Exception:
        return None, None


def credit_payment(uid, item_id, charge_id, stars) -> bool:
    """Идемпотентное начисление: charge_id — PRIMARY KEY, повторный апдейт вернёт False без начисления."""
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
        return  # повторная доставка апдейта — уже начислено
    u, it = get_user(uid), CATALOG[item]
    log.info("payment credited: uid=%s item=%s charge=%s", uid, item, sp.telegram_payment_charge_id)
    text = (f"⭐ Начислено +{it['credits']} ИИ-запросов. На балансе: {u['extra_ai_credits']}." if it["kind"] == "pack"
            else f"⭐ SnapEat Pro активен до {datetime.fromtimestamp(u['pro_until']):%d.%m.%Y}. Спасибо!")
    try:
        await m.answer(text)
    except Exception as e:  # товар уже начислен, сбой уведомления не должен вызывать повторы вебхука
        log.warning("payment credited, but notify failed: %r", e)


ALLOWED_UPDATES = ["message", "pre_checkout_query"]  # message нужен для successful_payment


@asynccontextmanager
async def lifespan(app):
    if BASE_URL:
        try:
            await bot.set_webhook(f"{BASE_URL}/webhook/{WH_SECRET}", secret_token=WH_SECRET,
                                  allowed_updates=ALLOWED_UPDATES)
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Дневник", web_app=WebAppInfo(url=BASE_URL)))
        except Exception as e:
            log.error("webhook setup failed: %r", e)
    else:
        log.warning("Не задан WEBAPP_URL / RENDER_EXTERNAL_URL: вебхук не установлен, оплата работать не будет")
    yield
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
async def state(day: str = "", today: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    u = get_user(tu["id"], tu.get("first_name", ""))
    day, today = norm_day(day), norm_day(today)
    ai = ai_status(u)
    entries, total = day_totals(u["id"], day)
    has = u["kcal"] is not None
    norm = {k: u[k] for k in ("kcal", "p", "f", "c")} if has else None
    rem = {k: norm[k] - total[k] for k in total} if has else None
    prof = {k: u[k] for k in ("sex", "age", "height", "weight", "start_weight", "act", "goal", "target_weight", "pace", "manual")} if has else None
    w = run("SELECT ml FROM water WHERE uid=? AND day=?", (u["id"], day))
    return {"profile": prof, "norm": norm, "total": total, "entries": entries, "ai": ai,
            **{k: ai[k] for k in ("free_daily_limit", "daily_limit", "used_today", "remaining_daily_today", "extra_ai_credits", "is_pro", "pro_until")},
            "suggest": suggest(rem) if has else [], "water": w[0]["ml"] if w else 0,
            "water_goal": water_goal(u["weight"]), "streak": calc_streak(u["id"], date.today().isoformat()),  # только серверная дата; query-параметр today игнорируется
            "favs": run("SELECT * FROM favs WHERE uid=? ORDER BY id DESC LIMIT 40", (u["id"],)),
            "catalog": CATALOG_LIST}


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


@app.post("/api/profile")
async def save_profile(p: Profile, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    get_user(tu["id"], tu.get("first_name", ""))
    if p.manual:
        if None in (p.kcal, p.p, p.f, p.c):
            raise HTTPException(400, "Укажите калории, белки, жиры и углеводы")
        n = {"kcal": p.kcal, "p": p.p, "f": p.f, "c": p.c}
    else:
        n = calc_norms(p.sex, p.age, p.height, p.weight, p.act, p.goal, p.pace)
    cur_u = get_user(tu["id"], tu.get("first_name", ""))
    start_w = cur_u.get("start_weight") or (p.weight if p.target_weight else None)
    run("UPDATE users SET sex=?,age=?,height=?,weight=?,start_weight=?,act=?,goal=?,target_weight=?,pace=?,manual=?,"
        "kcal=?,p=?,f=?,c=? WHERE id=?",
        (p.sex, p.age, p.height, p.weight, start_w, p.act, p.goal, p.target_weight, p.pace, int(p.manual),
         n["kcal"], n["p"], n["f"], n["c"], tu["id"]))
    return {"norm": n}


class AddReq(BaseModel):
    day: str
    items: list[Item] = Field(min_length=1, max_length=20)
    src: Literal["manual", "text", "photo", "suggest", "fav"] = "manual"
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


@app.post("/api/ai")
async def ai_analyze(r: AIReq, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    u = get_user(tu["id"], tu.get("first_name", ""))
    if not r.text.strip() and not r.image:
        raise HTTPException(400, "Опишите еду или приложите фото")
    daily, today = ai_status(u)["daily_limit"], date.today().isoformat()
    # 1) атомарный резерв (один UPDATE — безопасно при параллельных запросах): сначала дневной лимит, затем купленные
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
        items = await ask_ai(r.text, r.image)
    except BaseException:
        # 2) ошибка ИИ (или обрыв запроса) — резерв возвращается, ничего не списано
        if source == "daily":
            run("UPDATE users SET ai_n=max(COALESCE(ai_n, 0) - 1, 0) WHERE id=? AND ai_day=?", (u["id"], today))
        else:
            run("UPDATE users SET extra_ai_credits=COALESCE(extra_ai_credits, 0) + 1 WHERE id=?", (u["id"],))
        raise
    return {"items": items, "source": source, "ai": ai_status(get_user(u["id"]))}


class InvoiceReq(BaseModel):
    item_id: Literal["pack_20", "pack_60", "pack_150", "pro_30d"]


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
    run("SELECT 1")  # проверка БД
    return {"ok": True, "webhook": bool(BASE_URL), "ai": bool(AI_KEY)}


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
