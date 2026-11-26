# SnapEat

Telegram Mini App и бот **SnapEat** для учёта калорий, КБЖУ, воды, ИИ-анализа блюд и рационов, оплат и администрирования (FastAPI + aiogram + PostgreSQL).

## Состав

- `main.py` — FastAPI-сервер, Telegram-бот (webhook), авторизация WebApp, КБЖУ, ИИ-анализ/рационы, оплата Stars / ЮKassa, админ-API и логи.
- `static/index.html` — клиентский интерфейс Mini App с неблокирующей загрузкой Telegram WebApp SDK.
- `static/admin.html` — панель администратора.
- `render.yaml` — конфигурация деплоя на Render.
- `requirements.txt` — зависимости Python.
