# Подключить свой аватар HeyGen

Эта инструкция объясняет человеку и агенту, как взять этот бот за основу и
подключить **свой** аватар HeyGen. Аватар и голос задаются через `.env` — код
менять не нужно.

## Что нужно заранее

- Аккаунт HeyGen с готовым аватаром (цифровой двойник или говорящее фото).
- Активная подписка HeyGen (план-кредиты). Рендер видео и синтез речи
  расходуют кредиты подписки.
- Отдельный Telegram-бот у [@BotFather](https://t.me/BotFather) для режима
  `media`.

## Термины HeyGen

- **Avatar / look id** — идентификатор образа, который рендерит HeyGen.
  В этом проекте он передаётся в `HEYGEN_AVATAR_ID`. Для цифрового двойника это
  `look_id`; для говорящего фото — `talking_photo_id`.
- **Group id** — идентификатор группы «my avatars». Необязателен:
  `HEYGEN_AVATAR_GROUP_ID` автоматически резолвится в look id, если он совпал.
- **Voice id** — идентификатор голоса. Передаётся в `HEYGEN_DEFAULT_VOICE_ID`.

## Шаг 1. Авторизация HeyGen (OAuth Bearer)

Видео и речь должны идти через OAuth Bearer, иначе запрос уходит в пустой API
Wallet и падает с `HTTP 402 Insufficient API credits`.

Запустите локальный OAuth-вход (поднимет callback на 127.0.0.1, откроет
браузер, сохранит токены в `data/heygen_oauth.json`):

```bash
python3 scripts/heygen_oauth_login.py
```

Готовую пару токенов можно сохранить и вручную:

```bash
python3 scripts/save_heygen_session.py "<access_token>" "<refresh_token>"
```

Файл `data/heygen_oauth.json` — секрет, он в `.gitignore`. Не коммитьте его.
Токен обновляется автоматически; при `invalid_grant` бот удаляет файл и просит
войти заново.

## Шаг 2. Узнать id аватара

Вариант A — через API (Bearer-токен уже сохранён):

```bash
python3 - <<'PY'
import json, httpx
token = json.load(open("data/heygen_oauth.json"))["oauth"]["access_token"]
r = httpx.get("https://api.heygen.com/v3/avatars/looks",
              headers={"Authorization": f"Bearer {token}"}, timeout=20)
for look in r.json().get("data", {}).get("looks", []):
    print(look.get("id"), "|", look.get("name"), "|", look.get("group_id"))
PY
```

Вариант B — в веб-интерфейсе HeyGen. Откройте страницу **My Avatars**. Id
образа виден в URL вида
`https://app.heygen.com/avatar/my-avatars/<look_id>`.

Для говорящего фото (talking photo) используйте `talking_photo_id` и задайте
`HEYGEN_AVATAR_TYPE=photo_avatar`.

## Шаг 3. Узнать voice id

```bash
python3 - <<'PY'
import json, httpx
token = json.load(open("data/heygen_oauth.json"))["oauth"]["access_token"]
r = httpx.get("https://api.heygen.com/v2/voices",
              headers={"Authorization": f"Bearer {token}"}, timeout=20)
for v in r.json().get("data", {}).get("voices", []):
    print(v.get("voice_id"), "|", v.get("name"), "|", v.get("language"))
PY
```

Выберите русский голос и возьмите его `voice_id`.

## Шаг 4. Заполнить `.env`

```bash
HEYGEN_AVATAR_ID=<look_id или talking_photo_id>
HEYGEN_AVATAR_GROUP_ID=<необязательно, если у вас есть group id>
HEYGEN_AVATAR_TYPE=digital_twin        # или photo_avatar
HEYGEN_AVATAR_NAME=Имя эксперта        # показывается в интерфейсе бота
HEYGEN_AVATAR_LOOK_NAME=Основной образ
HEYGEN_AVATAR_CATEGORY=live
HEYGEN_AVATAR_STORIES_FIT=best         # best = портрет 9:16; иначе источник 16:9 и кроп в FFmpeg
HEYGEN_DEFAULT_VOICE_ID=<voice_id>
HEYGEN_API_KEY=<api key, только fallback>
BOT_TOKEN=<токен медиабота>
```

Пустой `HEYGEN_AVATAR_ID` блокирует меню рендера с подсказкой. Пустой
`HEYGEN_DEFAULT_VOICE_ID` заставляет бота спросить voice id в чате.

## Шаг 5. Перезапустить и проверить

```bash
docker compose up -d --build media-bot
docker compose logs --tail=50 media-bot
```

Проверка без Telegram и без трат кредитов (тестовый режим HeyGen):

```bash
BOT_MODE=media python3 scripts/render_and_send.py \
  --text "Проверка подключения аватара. Это тестовый ролик." \
  --chat-id <ваш_telegram_id> --test
```

Проверка баланса: в боте `💳 Баланс` или
`GET https://api.heygen.com/v2/user/remaining_quota` с Bearer-токеном.

## Где это в коде (для агента)

- `bot/config.py` — переменные `HEYGEN_AVATAR_*`, `HEYGEN_DEFAULT_VOICE_ID`.
- `bot/services/heygen_service.py` — `build_experts()` строит каталог аватаров
  из настроек; `create_avatar_video()` рендерит и резолвит group id → look id.
- `bot/handlers/avatar.py` — FSM-флоу: аватар → голос → текст → подтверждение →
  рендер → FFmpeg (кружок или рилс) → отправка.
- `scripts/heygen_oauth_login.py` — OAuth 2.0 PKCE логин.
- `scripts/render_and_send.py` — CLI-рендер.

Аватар не хранится в коде и не выбирается в UI: единственный настроенный
эксперт используется автоматически. Чтобы добавить несколько аватаров,
понадобится расширить `build_experts()` и меню.

## Частые проблемы

- **HTTP 402 `Insufficient API credits`** — запрос ушёл по `X-Api-Key` в пустой
  API Wallet. Убедитесь, что `data/heygen_oauth.json` существует и валиден;
  OAuth Bearer используется по умолчанию.
- **HTTP 401** — токен истёк или отозван. Перезапустите
  `scripts/heygen_oauth_login.py`.
- **`avatar_not_found`** — неверный `HEYGEN_AVATAR_ID`. Возьмите look id из
  шага 2.
- **Рилс с чёрными полосами** — для цифрового двойника источник 16:9; ролик
  кадрируется в FFmpeg. Тег `HEYGEN_AVATAR_STORIES_FIT=best` включает запрос
  портрета 9:16 для нативных вертикальных аватаров.

## Безопасность

- Не коммитьте `.env`, `data/`, `data/heygen_oauth.json`, Telethon `.session`.
- Не печатайте токены в логи и чат.
- При публикации `X-Api-Key`, `WEB_SESSION_SECRET` и токен бота считайте
  скомпрометированными и перевыпустите их.
