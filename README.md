# SnapEat — Дневник питания и КБЖУ (Telegram Mini App + Bot)

## Состав архива и структура файлов
- `main.py` — бэкенд FastAPI + Telegram-бот (`aiogram`) + работа с PostgreSQL (`psycopg` pool).
- `static/index.html` — клиентский интерфейс дневника питания и КБЖУ (Telegram WebApp).
- `static/admin.html` — панель администратора (метрики, пользователи, платежи, обращения, рассылки, логи).
- `render.yaml` — конфигурация сервиса и проверки `/health` для Render.
- `requirements.txt` — список Python-зависимостей.
- `README.md` — документация по запуску и настройке.

## Основные возможности и доработки
1. **Единая структура `static/`**: фронтенд-файлы упакованы в `static/index.html` и `static/admin.html` в полном соответствии с маршрутами `/` и `/admin` в `main.py`.
2. **Ненавязчивое уведомление о канале `@snapeat_app`**:
   - В `static/index.html` отображается компактный необязательный баннер на `https://t.me/snapeat_app` с возможностью закрыть его на текущую сессию (`✕`).
   - Для пользователей, уже состоящих в канале (проверка через `_check_user_in_channel` при `/start` и `/api/state`), баннер и строка с ссылкой в приветствии скрываются.
   - По умолчанию используется `@snapeat_app` (при необходимости можно переопределить через переменную окружения `CHANNEL_ID`).
3. **ИИ-контур и устойчивость**:
   - Поддержка Gemini, Groq (`GROQ_API_KEY`), бесплатных резервных слотов (`FREE_FALLBACK_*`) и OpenAI (`OPENAI_API_KEY`) с маскированием секретов в логах.
   - Корректный возврат квот при ошибках ИИ и передача источника (`photo` / `text`) при сохранении распознанных позиций.

## Быстрый старт на Render / сервере
1. Установите зависимости: `pip install -r requirements.txt`.
2. Задайте переменные окружения: `BOT_TOKEN`, `DATABASE_URL`, `WEBAPP_URL`, `ADMIN_IDS`, а также ключи ИИ (`GEMINI_API_KEY`, `GROQ_API_KEY`, `FREE_FALLBACK_API_KEY` или `OPENAI_API_KEY`).
3. Запустите приложение: `uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}`.
