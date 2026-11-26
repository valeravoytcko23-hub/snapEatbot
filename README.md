# SnapEat — Дневник питания и КБЖУ (Telegram Mini App + Bot)

## Что исправлено и улучшено в сборке
1. **Структура файлов и запуск**:
   - Основной модуль бэкенда — `main.py` (совместим с `uvicorn main:app` из `render.yaml`).
   - Статические страницы находятся в `static/index.html` и `static/admin.html`.
2. **ИИ-контур и устойчивость**:
   - `GROQ_API_KEY` добавлен в `_ALL_SECRETS` для маскирования в логах и ошибках, а также подключён в `_free_fallback_targets()` через `GROQ_BASE_URL` и `GROQ_MODELS` (с поддержкой мультимодальных и текстовых моделей).
   - Улучшен разбор JSON в `_extract_json_obj` и безопасная обработка ответов OpenAI-совместимых провайдеров.
   - При сохранении распознанного ИИ-блюда в `static/index.html` корректно передаётся `src: "photo"` при распознавании по изображению и `src: "text"` при текстовом запросе.
   - Асинхронные операции возврата квот (`refund_ai_quota` и `refund_mealplan_quota`) выполняются через `asyncio.to_thread` без блокировки event loop.
3. **Telegram WebApp и UX**:
   - Скрипт `telegram-web-app.js` подключается единообразно в `static/index.html` и `static/admin.html`.
   - Сохранены все эндпоинты API, оплата через Telegram Stars (`XTR`), поддержка ЮKassa (СБП / карта), система обращений, реферальная программа и админ-панель.

## Быстрый старт на Render / сервере
1. Установите зависимости: `pip install -r requirements.txt`.
2. Задайте переменные окружения: `BOT_TOKEN`, `DATABASE_URL`, `WEBAPP_URL`, `ADMIN_IDS`, а также ключи ИИ (`GEMINI_API_KEY`, `GROQ_API_KEY`, `FREE_FALLBACK_API_KEY` / `OPENROUTER_API_KEY` или `OPENAI_API_KEY`).
3. Запустите приложение: `uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}`.
