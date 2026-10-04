# 🤖 Контент-бот с HeyGen-аватаром

Гибридная платформа для Telegram из трёх частей:

- `content` — контентный бот: веб-вход, планирование, согласование и
  публикация постов, рассылки, scheduler.
- `media` — медиабот: видео-кружки и рилсы 9:16 с вашим аватаром HeyGen,
  монтаж через FFmpeg, озвучка.
- `web` — панель контент-плана (FastAPI + Jinja2).

**Подключить свой аватар HeyGen:** [docs/CONNECT_YOUR_AVATAR.md](docs/CONNECT_YOUR_AVATAR.md).
Эксплуатация и деплой: [docs/OPERATIONS.md](docs/OPERATIONS.md).
Правила для агентов: [AGENTS.md](AGENTS.md).

---

## ⚡ Ключевые возможности

1. **Контент-план:** паспорта каналов, месячное планирование, генерация связки
   `полезный → прогревающий → продающий`, массовая редактура, апрув, метрики.
2. **Контентный бот:** присылает пост ответственному, действия
   `Опубликовать`, `Отложить на час`, `На завтра`, `Отменить`.
3. **Медиабот (HeyGen + FFmpeg):** рендер кружка (квадрат, до 60 сек) и рилса
   9:16 с вашим цифровым двойником или говорящим фото, голос из вашего
   HeyGen-аккаунта.
4. **Защита бюджета:** whitelist по Telegram ID, контроль хронометража, экран
   подтверждения, тестовый режим (0 кредитов), проверка баланса HeyGen.
5. **Аудио:** озвучка через HeyGen или MiniMax.
6. **Durable-очередь AI-задач** и сбор метрик постов через Telethon.

---

## 🚀 Быстрый старт

1. Подготовьте `.env`:
   ```bash
   cp .env.example .env
   chmod 600 .env
   ```
   Заполните `BOT_TOKEN`, `MEDIA_BOT_TOKEN`, `HEYGEN_API_KEY`,
   `HEYGEN_AVATAR_ID`, `HEYGEN_DEFAULT_VOICE_ID`, `WEB_SESSION_SECRET`,
   `ADMIN_IDS`. Как получить аватар и голос — в
   [docs/CONNECT_YOUR_AVATAR.md](docs/CONNECT_YOUR_AVATAR.md).

2. Авторизуйте HeyGen (сохраняет `data/heygen_oauth.json`):
   ```bash
   python3 scripts/heygen_oauth_login.py
   ```

3. Запустите сервисы:
   ```bash
   ./deploy.sh          # docker compose build && up -d
   docker compose ps
   ```
   Веб-панель: `http://<host>:8000` (Docker публикует только `127.0.0.1`).

---

## 🛠 Локальный запуск

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
ffmpeg -version
```

В отдельных терминалах:

```bash
BOT_MODE=content python -m bot.main
BOT_MODE=media BOT_TOKEN="$MEDIA_BOT_TOKEN" python -m bot.main
uvicorn web.app:app --host 127.0.0.1 --port 8000 --no-access-log
```

Только `content` владеет scheduler. Не запускайте два `content` процесса.

Веб-панель без пароля: разрешённый Telegram-пользователь отправляет боту
`/web` и открывает одноразовую ссылку.

---

## 🎬 Рендер видео из CLI

```bash
python3 scripts/render_and_send.py --text "Текст ролика" --chat-id <telegram_id> --test
```

`--test` использует тестовый режим HeyGen (0 кредитов, водяной знак).

---

## 🧪 Тесты

Tests используют временные базы и моки внешних сервисов:

```bash
python -m unittest \
  tests.test_admin_handlers \
  tests.test_avatar_formats \
  tests.test_bot_modes \
  tests.test_broadcast_handlers \
  tests.test_campaigns \
  tests.test_channel_profiles \
  tests.test_content_automation \
  tests.test_content_jobs \
  tests.test_content_approval_handlers \
  tests.test_content_planning \
  tests.test_content_quality \
  tests.test_delivery_scheduler \
  tests.test_metrics_collector \
  tests.test_migrations_and_posts \
  tests.test_publish_handlers \
  tests.test_telegram_exports \
  tests.test_telethon_join \
  tests.test_web_auth
git diff --check
```

---

## 📁 Структура

- `bot/` — aiogram-бот: `main.py`, `config.py`, `handlers/`, `services/`.
- `web/` — FastAPI-панель.
- `domain/` — доменная логика (без Telegram).
- `workers/` — scheduler, durable-очередь, метрики.
- `migrations/` — append-only SQL-миграции.
- `scripts/` — CLI-утилиты, OAuth HeyGen, импорт, рендер.
- `templates/`, `static/` — веб-интерфейс.
- `fixtures/` — примеры паспортов каналов и карты экспорта.
