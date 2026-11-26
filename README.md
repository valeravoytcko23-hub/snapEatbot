# SnapEat — Telegram Mini App и бот для учёта КБЖУ

## Что исправлено в этой сборке
1. **Нормализация URL резервных OpenAI-совместимых провайдеров** (`_call_openai_compatible`): автоматически добавляет `https://`, убирает лишние пробелы и кавычки, исключая ошибку `UnsupportedProtocol`.
2. **Удалена устаревшая модель `gemini-2.0-flash`** из дефолтного `AI_FALLBACK_MODELS` (оставлены `gemini-2.5-flash` и `gemini-2.5-flash-lite`), а пользователю возвращается аккуратное сообщение вместо сырого дампа провайдеров.
3. **Исправлена вёрстка `.toast` в `static/index.html`**: убран `border-radius: 999px` и добавлены ограничения ширины/высоты и переноса строк, чтобы уведомления не раздувались в круг поверх шапки.

## Переменные окружения
* `BOT_TOKEN` — токен бота от `@BotFather`
* `DATABASE_URL` — строка подключения к PostgreSQL
* `WEBAPP_URL` — публичный HTTPS URL сервиса (например, `https://snapeat.onrender.com`)
* `GEMINI_API_KEY` — ключ Google Gemini API
* `FREE_FALLBACK_API_KEY` / `FREE_FALLBACK_BASE_URL` / `FREE_FALLBACK_MODELS` (и слоты `_2` .. `_8`) — до 8 резервных бесплатных OpenAI-совместимых провайдеров (например, OpenRouter, Groq, Together, Mistral и др.)
* `ADMIN_IDS` — Telegram ID администраторов через запятую
