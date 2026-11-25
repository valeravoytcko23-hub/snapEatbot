import os, json, hmac, hashlib, time, sqlite3, threading, re, logging
from contextlib import asynccontextmanager
from datetime import date, datetime
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
log = logging.getLogger("uvicorn.error")
AI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{}:generateContent"
FREE_LIMIT = int(os.getenv("FREE_AI_LIMIT", "5"))      # ИИ-запросов в день без подписки
PRO_LIMIT = int(os.getenv("PRO_AI_LIMIT", "100"))      # ИИ-запросов в день с PRO
DB_PATH = os.getenv("DB_PATH", "data.db")
WH_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()[:32]
PLANS = {  # цена в Telegram Stars (XTR)
    "month": {"price": int(os.getenv("PRICE_MONTH", "150")), "days": 30, "title": "PRO на 1 месяц"},
    "year": {"price": int(os.getenv("PRICE_YEAR", "1200")), "days": 365, "title": "PRO на 1 год"},
}

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
def calc_norms(sex, age, height, weight, act, goal):
    bmr = 10 * weight + 6.25 * height - 5 * age + (5 if sex == "m" else -161)
    kcal = bmr * act * {"lose": 0.85, "keep": 1.0, "gain": 1.10}[goal]
    kcal = max(kcal, 1500 if sex == "m" else 1200)
    p = weight * {"lose": 2.0, "keep": 1.6, "gain": 1.8}[goal]
    f = weight * 0.9
    c = max((kcal - p * 4 - f * 9) / 4, 0)
    return {"kcal": round(kcal), "p": round(p), "f": round(f), "c": round(c)}


# ---------- «Что доесть» ----------
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


def suggest(rem):
    if rem["kcal"] < 70:
        return []
    out = []
    for name, k, p, f, c, port in FOODS:
        g = min(port, int(rem["kcal"] / k * 100 // 10 * 10))
        if g < 30:
            continue
        m = {"kcal": k * g / 100, "p": p * g / 100, "f": f * g / 100, "c": c * g / 100}
        gain = 3 * min(m["p"], max(rem["p"], 0)) + min(m["f"], max(rem["f"], 0)) + min(m["c"], max(rem["c"], 0))
        over = 3 * max(m["p"] - rem["p"], 0) + 3 * max(m["f"] - rem["f"], 0) + 2 * max(m["c"] - rem["c"], 0)
        out.append((gain - over, {"name": name, "grams": g, **{x: round(v, 1) for x, v in m.items()}}))
    out.sort(key=lambda t: -t[0])
    return [o for s, o in out[:5] if s > 0]


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


async def ask_ai(text: str, image: str):
    if not AI_KEY:
        raise HTTPException(503, "ИИ не настроен: не задан GEMINI_API_KEY")
    parts = []
    if image:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": image.split(",")[-1]}})
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
    used = u["ai_n"] if u["ai_day"] == date.today().isoformat() else 0
    return {"pro": pro, "used": used, "limit": PRO_LIMIT if pro else FREE_LIMIT, "pro_until": u["pro_until"]}


# ---------- Bot ----------
bot = Bot(BOT_TOKEN)
dp = Dispatcher()


@dp.message(CommandStart())
async def on_start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🥗 Открыть дневник", web_app=WebAppInfo(url=BASE_URL))]])
    await m.answer("Привет! Я считаю калории и БЖУ. Заполните профиль, и я покажу, сколько ещё можно съесть сегодня. "
                   "Еду можно добавить текстом, фото или вручную.", reply_markup=kb)


@dp.pre_checkout_query()
async def on_pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@dp.message(F.successful_payment)
async def on_paid(m: Message):
    plan = m.successful_payment.invoice_payload.split(":")[1]
    u = get_user(m.from_user.id, m.from_user.full_name)
    until = int(max(time.time(), u["pro_until"]) + PLANS[plan]["days"] * 86400)
    run("UPDATE users SET pro_until=? WHERE id=?", (until, u["id"]))
    await m.answer(f"⭐ PRO активен до {datetime.fromtimestamp(until):%d.%m.%Y}. Спасибо!")


@asynccontextmanager
async def lifespan(app):
    if BASE_URL:
        await bot.set_webhook(f"{BASE_URL}/webhook/{WH_SECRET}", secret_token=WH_SECRET,
                              allowed_updates=["message", "pre_checkout_query"])
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Дневник", web_app=WebAppInfo(url=BASE_URL)))
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


@app.get("/api/state")
async def state(day: str = "", x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    u = get_user(tu["id"], tu.get("first_name", ""))
    day = norm_day(day)
    entries = run("SELECT * FROM entries WHERE uid=? AND day=? ORDER BY ts", (u["id"], day))
    total = {k: round(sum(e[k] for e in entries), 1) for k in ("kcal", "p", "f", "c")}
    has = u["kcal"] is not None
    norm = {k: u[k] for k in ("kcal", "p", "f", "c")} if has else None
    rem = {k: norm[k] - total[k] for k in total} if has else None
    prof = {k: u[k] for k in ("sex", "age", "height", "weight", "act", "goal")} if has else None
    return {"profile": prof, "norm": norm, "total": total, "entries": entries, "ai": ai_status(u),
            "suggest": suggest(rem) if has else [],
            "plans": [{"id": k, **v} for k, v in PLANS.items()]}


class Profile(BaseModel):
    sex: Literal["m", "f"]
    age: int = Field(ge=10, le=100)
    height: float = Field(ge=100, le=250)
    weight: float = Field(ge=30, le=300)
    act: float = Field(ge=1.2, le=1.9)
    goal: Literal["lose", "keep", "gain"]


@app.post("/api/profile")
async def save_profile(p: Profile, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    get_user(tu["id"], tu.get("first_name", ""))
    n = calc_norms(p.sex, p.age, p.height, p.weight, p.act, p.goal)
    run("UPDATE users SET sex=?,age=?,height=?,weight=?,act=?,goal=?,kcal=?,p=?,f=?,c=? WHERE id=?",
        (p.sex, p.age, p.height, p.weight, p.act, p.goal, n["kcal"], n["p"], n["f"], n["c"], tu["id"]))
    return {"norm": n}


class AddReq(BaseModel):
    day: str
    items: list[Item] = Field(min_length=1, max_length=20)
    src: Literal["manual", "text", "photo", "suggest"] = "manual"


@app.post("/api/entry")
async def add_entry(r: AddReq, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    get_user(tu["id"], tu.get("first_name", ""))
    day = norm_day(r.day)
    for i, it in enumerate(r.items):
        run("INSERT INTO entries(uid,day,name,grams,kcal,p,f,c,src,ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (tu["id"], day, it.name, it.grams, it.kcal, it.p, it.f, it.c, r.src, int(time.time() * 1000) + i))
    return {"ok": True}


@app.delete("/api/entry/{eid}")
async def del_entry(eid: int, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    run("DELETE FROM entries WHERE id=? AND uid=?", (eid, tu["id"]))
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
    st = ai_status(u)
    if st["used"] >= st["limit"]:
        raise HTTPException(402, "limit")
    items = await ask_ai(r.text, r.image)  # лимит списывается только за успешный запрос
    run("UPDATE users SET ai_day=?, ai_n=? WHERE id=?", (date.today().isoformat(), st["used"] + 1, u["id"]))
    return {"items": items, "ai": ai_status(get_user(u["id"]))}


class PayReq(BaseModel):
    plan: Literal["month", "year"]


@app.post("/api/pro/invoice")
async def invoice(r: PayReq, x_init_data: str = Header(default="")):
    tu = auth(x_init_data)
    pl = PLANS[r.plan]
    link = await bot.create_invoice_link(
        title=pl["title"], description="Расширенный лимит ИИ-распознавания еды по тексту и фото",
        payload=f"{tu['id']}:{r.plan}", provider_token="", currency="XTR",
        prices=[LabeledPrice(label=pl["title"], amount=pl["price"])])
    return {"link": link}


@app.get("/health")
async def health():
    return {"ok": True}


@app.get("/")
async def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
