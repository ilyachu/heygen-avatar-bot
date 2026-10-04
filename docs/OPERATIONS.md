# Operations runbook

This document is the handoff for installing, operating, updating, backing up,
and recovering the content platform. Commands assume Linux/macOS and Python
3.11+ unless the Docker path is used.

## 1. What runs

- `bot`: контентный aiogram-бот — веб-вход, доступы, рассылки, approval,
  scheduler и публикация запланированных постов.
- `media-bot`: отдельный aiogram-бот — HeyGen-кружки, аудио, настройки и
  непосредственная публикация созданного медиа.
- `web`: FastAPI/Jinja2 content editor on container port `8000`, published only
  on host `127.0.0.1:8000` by Docker Compose.
- `data/bot.db`: shared persistent SQLite database. Both services mount `data/`.
- `temp/`: temporary media files.

The public HTTPS reverse proxy is infrastructure outside this repository. It
must proxy to `127.0.0.1:8000` and preserve the original HTTPS scheme.

Production identities: the `content` bot handles the web panel, approval and
publication; the `media` bot handles video circles/reels and audio.

## 2. Repository checkout

Clone the repository and switch to the branch you deploy:

```bash
git clone <your-repo-url>
cd <your-repo>
git switch main
```

Never copy `.env`, SQLite, Telethon sessions, or Telegram exports into Git.

## 3. Configuration

Create the runtime file and restrict its permissions:

```bash
cp .env.example .env
chmod 600 .env
```

Required for the bot:

- `BOT_TOKEN`: token of the content bot.
- `MEDIA_BOT_TOKEN`: token of the media bot. It must be different from
  `BOT_TOKEN`.
- `CONTENT_BOT_ID`, `MEDIA_BOT_ID`: public numeric Telegram bot IDs. Startup
  fails closed if a token is accidentally assigned to the wrong role.
- `ADMIN_IDS`: comma-separated Telegram user IDs allowed to administer access;
  keep the owner's ID in this list.
- `HEYGEN_API_KEY`: HeyGen API key; use `HEYGEN_TEST_MODE=true` for test mode.
  Routes requests to pay-as-you-go API Wallet (USD balance).
- `HEYGEN_OAUTH_PATH`: path to cached HeyGen OAuth credentials (defaults to
  `data/heygen_oauth.json`). Must be kept under ignored runtime storage.
  CRITICAL: The corporate HeyGen subscription (plan credits, e.g. Pro tier with
  monthly credits) is accessed via OAuth Bearer token. Both video generation
  (`/v2/video/generate`) and speech synthesis (`/v3/voices/speech`) must use
  OAuth Bearer auth by default to consume subscription plan credits rather than
  failing with `402 Insufficient API credits` against an unbilled API wallet.
- `MAX_TEXT_WORDS`: safety limit for Telegram video-note scripts, normally `125`.
- `TEMP_DIR`: temporary media directory, normally `temp`.

Required for secure web access:

- `WEB_BASE_URL`: external HTTPS origin without a trailing path.
- `WEB_SESSION_SECRET`: random value of at least 32 characters. Generate with
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- `WEB_COOKIE_SECURE=true` in production; use `false` only for local HTTP.
- `WEB_SESSION_DAYS`: session lifetime.

Required for text generation:

- `CONTENT_LLM_BASE_URL`: OpenAI-compatible `/v1` base URL.
- `CONTENT_LLM_API_KEY`: API key; never pass it on the command line.
- `CONTENT_LLM_MODEL`: provider model identifier.

Publishing and scheduler:

- `APPROVAL_USER_ID`: Telegram user that receives approval notifications.
- `SCHEDULER_INTERVAL_SECONDS`: polling interval, normally `20`.
- `CONTENT_JOB_IDLE_SECONDS`: durable AI worker polling interval, normally `2`.
- `WEEKLY_ENQUEUE_INTERVAL_SECONDS`: how often the Friday–Sunday next-week
  useful-content policy is checked, normally `900`.
- `METRICS_INTERVAL_SECONDS`: how often due 1h/24h/72h Telegram metrics are
  collected through the authorized Telethon account, normally `900`.
- `DB_PATH`: normally `data/bot.db`.

Optional integrations:

- `MINIMAX_API_KEY`, `MINIMAX_GROUP_ID`: MiniMax speech.
- `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_PHONE`: Telegram user
  account used by Telethon import helpers.
- `TELETHON_SESSION_PATH`: keep under ignored `data/telethon/`.

## 4. Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Set `WEB_COOKIE_SECURE=false` and `WEB_BASE_URL=http://localhost:8000`, then run
the services in separate terminals. The content bot is the only scheduler
owner:

```bash
BOT_MODE=content python -m bot.main
BOT_MODE=media BOT_TOKEN="$MEDIA_BOT_TOKEN" python -m bot.main
uvicorn web.app:app --host 127.0.0.1 --port 8000 --no-access-log
```

Never run two `content` processes. FSM data may share SQLite because its key
includes the Telegram bot ID, but the scheduler must have exactly one owner.

Check readiness:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
```

The web panel is not password-based. An allowed Telegram user sends `/web` to
the bot and opens the one-time link.

## 5. Tests

Tests use temporary databases and mocked external services. They must not point
at the production `.env` or database.

```bash
python -m unittest \
  tests.test_admin_handlers \
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

For UI changes, additionally verify authentication, the four content tabs,
bulk selection, desktop/mobile width, console errors, and network failures.

## 6. Database migrations

Migrations in `migrations/*.sql` run automatically when the bot initializes the
database. They are serialized and recorded in `schema_migrations`.

Rules:

1. Add a new numbered migration; never edit a migration already deployed.
2. Back up the database first.
3. Start one release and wait for readiness before any manual data import.
4. Confirm `PRAGMA integrity_check` after the deployment.

## 7. Backup and restore

SQLite uses WAL mode. Use SQLite's backup API so the snapshot is consistent
while services are running:

```bash
mkdir -p /root/backups/your-bot-YYYYMMDD-HHMMSS
docker compose exec -T web python -c 'import sqlite3; src=sqlite3.connect("data/bot.db"); dst=sqlite3.connect("/app/data/bot.backup.db"); src.backup(dst); dst.close(); src.close()'
cp data/bot.backup.db /root/backups/your-bot-YYYYMMDD-HHMMSS/bot.db
docker compose exec -T web python -c 'import sqlite3; db=sqlite3.connect("data/bot.backup.db"); print(db.execute("PRAGMA integrity_check").fetchone()[0])'
```

Move `data/bot.backup.db` out of the runtime directory after verifying it.

Restore is destructive and requires explicit approval. Never restore over a
running SQLite database. After substituting the real timestamped paths, use:

```bash
docker compose stop bot media-bot web
cp data/bot.db /root/backups/your-bot-YYYYMMDD-HHMMSS/bot.failed.db
cp /root/backups/your-bot-YYYYMMDD-HHMMSS/bot.db data/bot.db.restore
python3 -c 'import sqlite3; db=sqlite3.connect("data/bot.db.restore"); result=db.execute("PRAGMA integrity_check").fetchone()[0]; print(result); raise SystemExit(0 if result == "ok" else 1)'
mv data/bot.db.restore data/bot.db
rm -f data/bot.db-shm data/bot.db-wal
docker compose up -d bot media-bot web
curl -fsS http://127.0.0.1:8000/ready
```

The `rm` targets only stale companion files for the stopped database. If the
integrity check fails, do not replace the database; keep all files for analysis.

## 8. Production deployment

Before deployment:

```bash
git status --short --branch
git fetch origin
git switch feat/content-platform-rebuild
git pull --ff-only
```

Require a clean worktree and a verified database backup. Then build and start:

```bash
./deploy.sh
docker compose ps
docker compose logs --tail=100 bot media-bot web
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
```

Check the external HTTPS `/health` and `/ready` endpoints too. Do not consider a
deployment complete based only on container status.

`deploy.sh` itself only checks that `.env` exists, then runs Compose build/up
and prints container state. The clean-worktree check, backup, integrity check,
logs, and health checks above are mandatory manual release gates.

The content and media bot accounts must both have the Telegram channel rights
needed for their own publication flows. A Telegram `file_id` created by one bot
must not be reused by the other bot; generated media stays inside `media-bot`.
Verify permissions for both identities after deployment:

```bash
docker compose exec -T bot python -m scripts.check_bot_channel_access
docker compose exec -T media-bot python -m scripts.check_bot_channel_access
```

The repository's current deployment directory is environment-specific; confirm
it before executing commands. Runtime `.env` and `data/` must survive code
updates and must not be replaced from a developer machine.

## 9. Telegram channel history and profiles

Telegram Bot API cannot export historical channel posts. Use Telegram Desktop
HTML exports or the optional Telethon helpers. Raw exports remain outside Git.

For the mapped Telegram Desktop exports:

```bash
python -m scripts.import_telegram_html_profiles \
  --batch-map fixtures/telegram_html_export_map.json \
  --exports-root "/path/to/Telegram Desktop" \
  --output data/telethon/profiles/profiles_html_batch.json \
  --import
```

The command reads the LLM key from the environment. Review generated passports
in the web panel before relying on them for publication.

Telethon account setup, when needed:

```bash
python -m scripts.telethon_login
python -m scripts.telethon_export
```

The resulting `.session` file is a credential. Keep it only under the ignored
runtime data directory, restrict permissions, and never attach it to issues or
logs.

## 10. Safe content workflow

1. Open the web panel using `/web` in Telegram.
2. Create the monthly plan.
3. Add the webinar for the selected week and choose its channels.
4. Generate the weekly posts.
5. Review or bulk-edit drafts and verify webinar-link requirements.
6. Set a future date/time and approve explicitly.
7. Confirm status in the web panel and Telegram notifications.

The web editor also supports one shared date/time for all active-channel posts
of a selected type in the week and an explicit approval revoke action. Revoking
returns only `scheduled` posts to `review` and keeps their tentative time.

The content bot offers both a single broadcast and a scheduled 2–3 post plan.
Each plan item has its own text, optional photo/video/voice, and future time;
channels are selected once and all delivery rows are created atomically.
Confirmed future series can be opened from `Подтверждённые планы`; an admin can
change a slot's text/time for every channel or cancel all still-scheduled rows.

AI work is durable in `content_jobs`: web requests enqueue idempotent jobs and
the content bot is the only worker. Jobs have progress, lease/heartbeat,
bounded retry, cancellation, and expired-lease recovery. Every Friday through
Sunday a stable job prepares only the next Monday's `useful` posts; warming and
selling jobs require an explicitly saved webinar.

Generated and edited texts receive append-only quality reviews. The latest
review stores the content fingerprint, prompt version, model, rubric scores,
and blocking issues. Content changes invalidate the review; schedule-only
changes do not. A blocking medical/guarantee issue or score below the threshold
prevents approval.

For published Telegram messages, the authorized Telethon account captures
views, forwards, and reaction snapshots after 1, 24, and 72 hours. If Telethon
credentials/session are unavailable, publication continues and metrics report
the disabled state in content-bot logs.

Do not approve a real post merely to test the interface. The scheduler can
publish approved due content automatically.

## 11. Incident checks

Run read-only checks first:

```bash
docker compose ps
docker compose logs --tail=200 bot media-bot web
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/ready
docker compose exec -T web python -c 'import sqlite3; db=sqlite3.connect("data/bot.db"); print(db.execute("PRAGMA integrity_check").fetchone()[0]); print(db.execute("SELECT status, COUNT(*) FROM posts GROUP BY status ORDER BY status").fetchall())'
```

If delivery failed after Telegram may have accepted a message, do not blindly
retry it: first check the target channel and delivery state to avoid duplicates.
Preserve logs, database, release SHA, and timestamps before attempting recovery.

## 12. HeyGen billing & credentials runbook

HeyGen operates on two distinct balance models:

1. **Subscription Plan Credits** (e.g. Pro tier with monthly pooled credits).
   Accessed via `Authorization: Bearer <oauth_access_token>`.
2. **API Wallet Balance** (prepaid USD cash deposit for developers).
   Accessed via `X-Api-Key: sk_V2_...`.

### "Insufficient API credits" (HTTP 402) or HTTP 401 Unauthorized

If speech synthesis (`/v3/voices/speech`) or video generation (`/v2/video/generate`)
fails with:
```
Insufficient API credits. Please upgrade your plan or purchase additional credits.
```

**Root causes:**
- The request used `X-Api-Key` instead of OAuth Bearer token, which routed the
  request to the empty API Wallet ($0.00) instead of the subscription.
- `data/heygen_oauth.json` is missing, expired, or invalid.

**Verification steps on the server:**

1. Check if OAuth state exists:
   ```bash
   docker exec media-bot ls -la /app/data/heygen_oauth.json
   ```
2. Verify token status and check both balances:
   ```bash
   docker exec media-bot python -c '
   import asyncio, json, httpx
   async def check():
       with open("/app/data/heygen_oauth.json") as f:
           token = json.load(f)["oauth"]["access_token"]
       async with httpx.AsyncClient() as client:
           r = await client.get("https://api.heygen.com/v3/users/me", headers={"Authorization": f"Bearer {token}"})
           print("Subscription balance:", r.status_code, r.json().get("data", {}).get("subscription"))
   asyncio.run(check())
   '
   ```
3. In `bot/services/heygen_service.py`, `_get_headers()` must prefer OAuth Bearer
   token by default for all generation endpoints (`create_avatar_video`,
   `generate_speech`, etc.) and only fall back to `X-Api-Key` if OAuth is
   unconfigured.
4. If OAuth token refresh fails with `invalid_grant`, re-authenticate the HeyGen
   account to generate a fresh `data/heygen_oauth.json`.
