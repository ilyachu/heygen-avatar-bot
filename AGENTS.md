# Agent Contract

## Goal

Ship correct changes quickly while preserving the working bot, web application,
production data, and adjacent user changes.

## Required workflow

- Execute clear, low-risk steps without asking.
- Ask only for destructive or irreversible actions and hard ambiguity.
- Keep diffs small and reversible; prefer reuse over new abstractions.
- Do not add dependencies unless explicitly requested.
- Never revert changes made by another person or agent.
- Inspect `git status --short --branch` before editing and before reporting.
- Never stage with `git add .`; stage an explicit allowlist.
- For refactors, lock existing behaviour with tests before changing it.
- Run relevant tests and `git diff --check` before completion.
- Report changed files, verification evidence, and residual risks.

## Repository rules

- The product is hybrid: the web app owns planning, generation, editing,
  approval, and scheduling; Telegram owns notifications and quick operations.
- Preserve the existing HeyGen video, MiniMax audio, whitelist, and immediate
  publishing flows when changing the content platform.
- Treat `data/`, `.env`, Telethon `.session` files, exports, logs, browser
  captures, and `.omx/` as local or production runtime data. Never commit them.
- Back up the SQLite database before migrations or production deployment.
- Do not publish, approve, schedule, or broadcast real content as a test.
- Use fixtures or an isolated temporary database for automated tests.
- Validate desktop and mobile behaviour in a real browser for UI changes.
- Production operations must follow [docs/OPERATIONS.md](docs/OPERATIONS.md).
- Keep the bot roles separate: `content` owns web access, approval, broadcast,
  administration, and the only scheduler; `media` owns video, audio, and media
  publication. Never mount all routers or start the scheduler in both modes.

## Architecture invariants

- SQLite migrations are append-only files in `migrations/`; never rewrite an
  already deployed migration.
- Publication transitions must remain idempotent and guarded against duplicate
  Telegram delivery.
- Editing approved content must return it to review when required by the domain
  workflow; publishing content must not be mutable in flight.
- A webinar link may be added late, but a post marked as requiring the link must
  not be approved without it.
- Importing Telegram history must not commit raw exports or generated profiles.
- HeyGen requests (both video rendering and speech synthesis) must use OAuth Bearer auth by default to draw from the subscription plan credits. Never force `prefer_api_key=True` because `X-Api-Key` hits the empty API Wallet and fails with HTTP 402.

## Verification baseline

Run the complete test suite explicitly because plain unittest discovery does
not reliably find this repository's tests:

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
