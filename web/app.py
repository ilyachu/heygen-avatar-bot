from __future__ import annotations

import json
import hashlib
import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import aiosqlite
from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from bot.config import settings
from bot.db import init_db
from bot.migrations import configure_connection
from domain.auth import AuthService, AuthenticationError
from domain.content_jobs import (
    JOB_CAMPAIGN,
    JOB_MONTH_PLAN,
    JOB_QUALITY,
    JOB_REVISION,
    JOB_WEBINAR,
    ContentJobQueue,
)
from domain.content_planning import (
    ContentPlanningError,
    ContentPlanningService,
)
from domain.posts import Actor, PostWorkflow, PostWorkflowError

BASE_DIR = Path(__file__).resolve().parent.parent
SESSION_COOKIE = "content_session"
MOSCOW = ZoneInfo("Europe/Moscow")
logger = logging.getLogger(__name__)


def create_app(*, auth_service: AuthService | None = None, run_startup: bool = True) -> FastAPI:
    auth = auth_service
    if auth is None and settings.WEB_SESSION_SECRET:
        auth = AuthService(
            settings.DB_PATH, settings.WEB_SESSION_SECRET, settings.WEB_SESSION_DAYS
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if auth is None:
            raise RuntimeError("WEB_SESSION_SECRET must be configured for the web service")
        if run_startup:
            await init_db()
        yield

    app = FastAPI(title="Контент-платформа", lifespan=lifespan)
    app.state.auth = auth
    templates = Jinja2Templates(directory=BASE_DIR / "templates")
    templates.env.filters["moscow_datetime"] = _moscow_datetime
    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; img-src 'self' data:; "
            "script-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    async def current_user(
        content_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    ) -> int:
        assert auth is not None
        user_id = await auth.get_session_user(content_session)
        if user_id is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return user_id

    @app.exception_handler(HTTPException)
    async def auth_exception(request: Request, exc: HTTPException):
        if exc.status_code == status.HTTP_401_UNAUTHORIZED and request.url.path != "/login":
            return RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        return HTMLResponse("Запрос отклонён", status_code=exc.status_code)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/ready")
    async def ready():
        try:
            async with aiosqlite.connect(settings.DB_PATH) as db:
                await configure_connection(db)
                cursor = await db.execute("SELECT 1 FROM schema_migrations LIMIT 1")
                await cursor.fetchone()
            return {"status": "ready"}
        except Exception:
            raise HTTPException(status_code=503, detail="Database is not ready")

    @app.get("/login", response_class=HTMLResponse)
    async def login(request: Request):
        return templates.TemplateResponse(request, "login.html", {})

    @app.get("/auth/{token}")
    async def exchange_token(token: str):
        assert auth is not None
        try:
            session = await auth.exchange_login_token(token)
        except AuthenticationError:
            return HTMLResponse(
                "Ссылка недействительна или уже использована. Запросите новую в боте.",
                status_code=status.HTTP_401_UNAUTHORIZED,
            )
        response = RedirectResponse("/editor", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(
            SESSION_COOKIE,
            session,
            max_age=settings.WEB_SESSION_DAYS * 86400,
            httponly=True,
            secure=settings.WEB_COOKIE_SECURE,
            samesite="lax",
            path="/",
        )
        return response

    @app.post("/logout")
    async def logout(
        csrf_token: str = Form(),
        content_session: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        user_id: int = Depends(current_user),
    ):
        assert auth is not None
        if not auth.validate_csrf(content_session, csrf_token):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
        await auth.revoke_session(content_session)
        response = RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    async def today(
        request: Request,
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        async with _db() as db:
            counts = await _fetch_one(
                db,
                """
                SELECT
                    SUM(CASE WHEN status = 'review' THEN 1 ELSE 0 END) AS review_count,
                    SUM(CASE WHEN status IN ('scheduled', 'notification_sent', 'postponed') THEN 1 ELSE 0 END) AS scheduled_count,
                    SUM(CASE WHEN status IN ('generation_failed', 'publish_failed') THEN 1 ELSE 0 END) AS error_count
                FROM posts
                """,
            )
            upcoming = await _fetch_all(
                db,
                """
                SELECT p.*, c.title AS channel_title, w.title AS webinar_title
                FROM posts p
                JOIN channels c ON c.id = p.channel_id
                LEFT JOIN webinars w ON w.id = p.webinar_id
                WHERE p.status IN ('review', 'scheduled', 'notification_sent', 'postponed')
                ORDER BY p.publish_at IS NULL, p.publish_at
                LIMIT 8
                """,
            )
        return templates.TemplateResponse(
            request,
            "today.html",
            _context(auth, content_session, user_id, "today", counts=counts, upcoming=upcoming),
        )

    @app.get("/channels", response_class=HTMLResponse)
    async def channels(
        request: Request,
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        async with _db() as db:
            rows = await _fetch_all(
                db,
                """
                SELECT c.*, cp.description, cp.channel_kind, cp.analysis_status,
                       cp.source_message_count, cp.updated_at AS profile_updated_at
                FROM channels c LEFT JOIN channel_profiles cp ON cp.channel_id = c.id
                ORDER BY c.created_at DESC
                """,
            )
        return templates.TemplateResponse(
            request,
            "channels.html",
            _context(auth, content_session, user_id, "channels", channels=rows),
        )

    @app.get("/channels/{channel_id}", response_class=HTMLResponse)
    async def channel_detail(
        request: Request,
        channel_id: int,
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        async with _db() as db:
            channel = await _fetch_one(
                db,
                """
                SELECT c.*, cp.* FROM channels c
                LEFT JOIN channel_profiles cp ON cp.channel_id = c.id
                WHERE c.id = ?
                """,
                (channel_id,),
            )
        if not channel:
            raise HTTPException(status_code=404)
        for field in ("key_meanings_json", "rubrics_json", "forbidden_topics_json"):
            channel[field] = "\n".join(json.loads(channel.get(field) or "[]"))
        return templates.TemplateResponse(
            request,
            "channel_detail.html",
            _context(auth, content_session, user_id, "channels", channel=channel),
        )

    @app.post("/channels/{channel_id}")
    async def update_channel_profile(
        channel_id: int,
        description: str = Form(max_length=2000),
        audience: str = Form(max_length=2000),
        purpose: str = Form(max_length=2000),
        key_meanings: str = Form(max_length=5000),
        rubrics: str = Form(max_length=3000),
        tone_of_voice: str = Form(max_length=5000),
        cta_rules: str = Form(max_length=3000),
        forbidden_topics: str = Form(default="", max_length=3000),
        csrf_token: str = Form(),
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        _validate_csrf(auth, content_session, csrf_token)
        async with _db() as db:
            cursor = await db.execute("SELECT 1 FROM channels WHERE id = ?", (channel_id,))
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=404)
            await db.execute(
                """
                INSERT INTO channel_profiles (
                    channel_id, description, audience, purpose, key_meanings_json,
                    rubrics_json, tone_of_voice, cta_rules, forbidden_topics_json,
                    analysis_status, analysis_version, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ready', 1, CURRENT_TIMESTAMP)
                ON CONFLICT(channel_id) DO UPDATE SET
                    description = excluded.description, audience = excluded.audience,
                    purpose = excluded.purpose, key_meanings_json = excluded.key_meanings_json,
                    rubrics_json = excluded.rubrics_json,
                    tone_of_voice = excluded.tone_of_voice, cta_rules = excluded.cta_rules,
                    forbidden_topics_json = excluded.forbidden_topics_json,
                    analysis_status = 'ready',
                    analysis_version = channel_profiles.analysis_version + 1,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (
                    channel_id,
                    description.strip(),
                    audience.strip(),
                    purpose.strip(),
                    _lines_json(key_meanings),
                    _lines_json(rubrics),
                    tone_of_voice.strip(),
                    cta_rules.strip(),
                    _lines_json(forbidden_topics),
                ),
            )
            await db.commit()
        return RedirectResponse(f"/channels/{channel_id}", status_code=303)

    @app.get("/publications")
    async def publications_redirect():
        return RedirectResponse("/editor", status_code=302)

    @app.get("/campaigns", response_class=HTMLResponse)
    async def campaigns(
        request: Request,
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        async with _db() as db:
            rows = await _fetch_all(
                db,
                """
                SELECT w.*, COUNT(DISTINCT wc.channel_id) AS channel_count,
                       COUNT(DISTINCT p.id) AS post_count
                FROM webinars w
                LEFT JOIN webinar_channels wc ON wc.webinar_id = w.id
                LEFT JOIN posts p ON p.webinar_id = w.id
                GROUP BY w.id ORDER BY w.starts_at DESC
                """,
            )
        return templates.TemplateResponse(
            request,
            "campaigns.html",
            _context(auth, content_session, user_id, "campaigns", campaigns=rows),
        )

    @app.get("/campaigns/new", response_class=HTMLResponse)
    async def new_campaign(
        request: Request,
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        async with _db() as db:
            channels = await _fetch_all(
                db,
                """
                SELECT c.id, c.title, cp.channel_kind FROM channels c
                LEFT JOIN channel_profiles cp ON cp.channel_id = c.id
                ORDER BY cp.channel_kind DESC, c.title
                """,
            )
        return templates.TemplateResponse(
            request,
            "campaign_new.html",
            _context(auth, content_session, user_id, "campaigns", channels=channels),
        )

    @app.post("/campaigns")
    async def create_campaign(
        title: str = Form(min_length=1, max_length=200),
        starts_at: str = Form(max_length=32),
        audience: str = Form(max_length=2000),
        problem: str = Form(max_length=3000),
        promise: str = Form(max_length=3000),
        agenda: str = Form(max_length=5000),
        offer: str = Form(max_length=5000),
        cta: str = Form(max_length=1000),
        registration_url: str = Form(default="", max_length=500),
        channel_ids: list[int] = Form(),
        csrf_token: str = Form(),
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        _validate_csrf(auth, content_session, csrf_token)
        start_time = _parse_local_datetime(starts_at)
        if start_time <= datetime.now(timezone.utc) + timedelta(days=3):
            raise HTTPException(status_code=400)
        if registration_url and not registration_url.startswith(("https://", "http://")):
            raise HTTPException(status_code=400)
        selected = sorted(set(channel_ids))
        if not selected or len(selected) > 40:
            raise HTTPException(status_code=400)
        async with _db() as db:
            placeholders = ",".join("?" for _ in selected)
            cursor = await db.execute(
                f"SELECT COUNT(*) FROM channels WHERE id IN ({placeholders})", selected
            )
            if (await cursor.fetchone())[0] != len(selected):
                raise HTTPException(status_code=400)
            cursor = await db.execute(
                """
                INSERT INTO webinars (
                    title, starts_at, audience, problem, promise, agenda,
                    offer, cta, registration_url, status, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?)
                """,
                (
                    title.strip(),
                    start_time.isoformat(),
                    audience.strip(),
                    problem.strip(),
                    promise.strip(),
                    agenda.strip(),
                    offer.strip(),
                    cta.strip(),
                    registration_url.strip(),
                    user_id,
                ),
            )
            campaign_id = cursor.lastrowid
            await db.executemany(
                "INSERT INTO webinar_channels(webinar_id, channel_id) VALUES (?, ?)",
                [(campaign_id, channel_id) for channel_id in selected],
            )
            await db.commit()
        return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)

    @app.get("/campaigns/{campaign_id}", response_class=HTMLResponse)
    async def campaign_detail(
        request: Request,
        campaign_id: int,
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        async with _db() as db:
            campaign = await _fetch_one(db, "SELECT * FROM webinars WHERE id = ?", (campaign_id,))
            channels = await _fetch_all(
                db,
                """
                SELECT c.id, c.title FROM webinar_channels wc
                JOIN channels c ON c.id = wc.channel_id
                WHERE wc.webinar_id = ? ORDER BY c.title
                """,
                (campaign_id,),
            )
            posts = await _fetch_all(
                db,
                """
                SELECT p.*, c.title AS channel_title FROM posts p
                JOIN channels c ON c.id = p.channel_id
                WHERE p.webinar_id = ? ORDER BY c.title, p.publish_at
                """,
                (campaign_id,),
            )
        if not campaign:
            raise HTTPException(status_code=404)
        return templates.TemplateResponse(
            request,
            "campaign_detail.html",
            _context(
                auth,
                content_session,
                user_id,
                "campaigns",
                campaign=campaign,
                channels=channels,
                posts=posts,
                llm_ready=bool(
                    settings.CONTENT_LLM_API_KEY and settings.CONTENT_LLM_MODEL
                ),
            ),
        )

    @app.post("/campaigns/{campaign_id}/generate")
    async def generate_campaign(
        campaign_id: int,
        csrf_token: str = Form(),
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        _validate_csrf(auth, content_session, csrf_token)
        if not settings.CONTENT_LLM_API_KEY or not settings.CONTENT_LLM_MODEL:
            raise HTTPException(status_code=503)
        async with _db() as db:
            campaign = await _fetch_one(
                db, "SELECT updated_at FROM webinars WHERE id = ?", (campaign_id,)
            )
            if campaign is None:
                raise HTTPException(status_code=404)
            await db.execute(
                "UPDATE webinars SET status = 'queued' WHERE id = ?",
                (campaign_id,),
            )
            await db.commit()
        await ContentJobQueue(settings.DB_PATH).enqueue(
            JOB_CAMPAIGN,
            {"campaign_id": campaign_id, "actor_id": user_id},
            idempotency_key=f"campaign:{campaign_id}:{campaign['updated_at']}",
        )
        return RedirectResponse(f"/campaigns/{campaign_id}", status_code=303)

    @app.get("/editor", response_class=HTMLResponse)
    async def editor(
        request: Request,
        month: str | None = None,
        week: str | None = None,
        view: str = "week",
        post_type: str = "all",
        notice: str | None = None,
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        selected_month, _, _ = _month_bounds(month)
        selected_week = _selected_week(selected_month, week)
        if view not in {"week", "month"}:
            raise HTTPException(status_code=400)
        if post_type not in {"all", "useful", "warming", "selling"}:
            raise HTTPException(status_code=400)
        week_filter = selected_week if view == "week" else None
        type_filter = None if post_type == "all" else post_type
        planning = ContentPlanningService(settings.DB_PATH)
        posts = await planning.list_plan(
            selected_month, week_start=week_filter, post_type=type_filter
        )
        async with _db() as db:
            channels = await _fetch_all(
                db,
                "SELECT id, title FROM channels WHERE is_active = 1 ORDER BY title",
            )
            webinar = await _fetch_one(
                db,
                """
                SELECT w.*,
                       GROUP_CONCAT(wc.channel_id) AS selected_channel_ids
                FROM webinars w
                LEFT JOIN webinar_channels wc ON wc.webinar_id = w.id
                WHERE w.week_start = ?
                GROUP BY w.id
                """,
                (selected_week,),
            )
            count_rows = await _fetch_all(
                db,
                """
                SELECT post_type, COUNT(*) AS amount FROM posts
                WHERE plan_month = ?
                  AND (? IS NULL OR week_start = ?)
                GROUP BY post_type
                """,
                (selected_month, week_filter, week_filter),
            )
        for post in posts:
            post["publish_at_local"] = _datetime_local(post.get("publish_at"))
            post["link_missing"] = bool(
                post.get("requires_link")
                and not post.get("webinar_registration_url")
            )
        if webinar:
            webinar["starts_at_local"] = _datetime_local(webinar.get("starts_at"))
            webinar["selected_channel_ids"] = {
                int(value)
                for value in (webinar.get("selected_channel_ids") or "").split(",")
                if value.isdigit()
            }
        counts = {row["post_type"]: row["amount"] for row in count_rows}
        jobs = await ContentJobQueue(settings.DB_PATH).list_recent(6)
        for job in jobs:
            processed = job["completed_items"] + job["failed_items"]
            job["progress_percent"] = (
                min(100, int(processed * 100 / job["total_items"]))
                if job["total_items"]
                else (100 if job["status"] == "completed" else 0)
            )
        return templates.TemplateResponse(
            request,
            "editor.html",
            _context(
                auth,
                content_session,
                user_id,
                "editor",
                month=selected_month,
                week=selected_week,
                week_end=(date.fromisoformat(selected_week) + timedelta(days=6)).isoformat(),
                view=view,
                post_type=post_type,
                channels=channels,
                posts=posts,
                webinar=webinar,
                counts=counts,
                notice=_notice_text(notice),
                llm_ready=bool(settings.CONTENT_LLM_API_KEY and settings.CONTENT_LLM_MODEL),
                jobs=jobs,
                can_manage_jobs=user_id in settings.admin_id_list,
            ),
        )

    @app.post("/editor/jobs/{job_id}/cancel")
    async def cancel_content_job(
        job_id: int,
        month: str = Form(max_length=7),
        week: str = Form(max_length=10),
        csrf_token: str = Form(),
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        _validate_csrf(auth, content_session, csrf_token)
        if user_id not in settings.admin_id_list:
            raise HTTPException(status_code=403)
        cancelled = await ContentJobQueue(settings.DB_PATH).cancel(job_id)
        notice = "job_cancelled" if cancelled else "job_not_cancelled"
        return _editor_redirect(month, week, notice)

    @app.post("/editor/jobs/{job_id}/retry")
    async def retry_content_job(
        job_id: int,
        month: str = Form(max_length=7),
        week: str = Form(max_length=10),
        csrf_token: str = Form(),
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        _validate_csrf(auth, content_session, csrf_token)
        if user_id not in settings.admin_id_list:
            raise HTTPException(status_code=403)
        retried = await ContentJobQueue(settings.DB_PATH).retry(job_id)
        notice = "job_retried" if retried else "job_not_retried"
        return _editor_redirect(month, week, notice)

    @app.post("/editor/month-plan")
    async def create_month_plan(
        month: str = Form(max_length=7),
        week: str = Form(max_length=10),
        csrf_token: str = Form(),
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        _validate_csrf(auth, content_session, csrf_token)
        selected_month, _, _ = _month_bounds(month)
        selected_week = _selected_week(selected_month, week)
        planning = ContentPlanningService(settings.DB_PATH)
        await planning.ensure_month(selected_month, user_id)
        if settings.CONTENT_LLM_API_KEY and settings.CONTENT_LLM_MODEL:
            await ContentJobQueue(settings.DB_PATH).enqueue(
                JOB_MONTH_PLAN,
                {
                    "month": selected_month,
                    "week_start": selected_week,
                    "actor_id": user_id,
                },
                idempotency_key=f"month-plan:{selected_month}",
            )
            notice_code = "month_generating"
        else:
            notice_code = "month_created"
        return _editor_redirect(selected_month, selected_week, notice_code)

    @app.post("/editor/webinar")
    async def save_weekly_webinar(
        week: str = Form(max_length=10),
        title: str = Form(min_length=1, max_length=200),
        starts_at: str = Form(max_length=32),
        audience: str = Form(default="", max_length=2000),
        problem: str = Form(default="", max_length=3000),
        promise: str = Form(default="", max_length=3000),
        agenda: str = Form(default="", max_length=5000),
        offer: str = Form(default="", max_length=5000),
        cta: str = Form(default="", max_length=1000),
        registration_url: str = Form(default="", max_length=500),
        channel_ids: list[int] = Form(),
        csrf_token: str = Form(),
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        _validate_csrf(auth, content_session, csrf_token)
        selected_week = _selected_week(starts_at[:7], week)
        fields = {
            "title": title,
            "starts_at": _parse_local_datetime(starts_at).isoformat(),
            "audience": audience,
            "problem": problem,
            "promise": promise,
            "agenda": agenda,
            "offer": offer,
            "cta": cta,
        }
        if registration_url.strip():
            fields["registration_url"] = registration_url
        planning = ContentPlanningService(settings.DB_PATH)
        try:
            await planning.upsert_weekly_webinar(
                selected_week, fields, channel_ids, user_id
            )
        except ContentPlanningError as exc:
            logger.info("Weekly webinar validation failed: %s", type(exc).__name__)
            raise HTTPException(status_code=400)
        return _editor_redirect(starts_at[:7], selected_week, "webinar_saved")

    @app.post("/editor/webinar/{webinar_id}/link")
    async def save_weekly_webinar_link(
        webinar_id: int,
        month: str = Form(max_length=7),
        week: str = Form(max_length=10),
        registration_url: str = Form(max_length=500),
        csrf_token: str = Form(),
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        _validate_csrf(auth, content_session, csrf_token)
        planning = ContentPlanningService(settings.DB_PATH)
        try:
            await planning.set_webinar_link(
                webinar_id, registration_url, user_id
            )
        except ContentPlanningError:
            raise HTTPException(status_code=400)
        return _editor_redirect(month, week, "link_saved")

    @app.post("/editor/week/generate")
    async def generate_content_week(
        month: str = Form(max_length=7),
        week: str = Form(max_length=10),
        generation_scope: str = Form(default="all", max_length=16),
        csrf_token: str = Form(),
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        _validate_csrf(auth, content_session, csrf_token)
        if not settings.CONTENT_LLM_API_KEY or not settings.CONTENT_LLM_MODEL:
            raise HTTPException(status_code=503)
        selected_month, _, _ = _month_bounds(month)
        selected_week = _selected_week(selected_month, week)
        planning = ContentPlanningService(settings.DB_PATH)
        await planning.ensure_month(selected_month, user_id)
        async with _db() as db:
            webinar = await _fetch_one(
                db,
                "SELECT id, updated_at FROM webinars WHERE week_start = ?",
                (selected_week,),
            )
        if not webinar:
            return _editor_redirect(selected_month, selected_week, "webinar_required")
        if generation_scope not in {"all", "webinar"}:
            raise HTTPException(status_code=400)
        post_types = ["warming", "selling"]
        await ContentJobQueue(settings.DB_PATH).enqueue(
            JOB_WEBINAR,
            {
                "webinar_id": webinar["id"],
                "week_start": selected_week,
                "post_types": post_types,
                "actor_id": user_id,
            },
            idempotency_key=(
                f"webinar:{webinar['id']}:{webinar['updated_at']}:warming-selling"
            ),
        )
        notice = "webinar_posts_generating" if post_types else "week_generating"
        return _editor_redirect(selected_month, selected_week, notice)

    @app.post("/editor/posts/bulk-revise")
    async def revise_content_posts(
        month: str = Form(max_length=7),
        week: str = Form(max_length=10),
        view: str = Form(default="week", max_length=8),
        post_type: str = Form(default="all", max_length=16),
        instruction: str = Form(min_length=2, max_length=2000),
        post_ids: list[int] = Form(),
        csrf_token: str = Form(),
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        _validate_csrf(auth, content_session, csrf_token)
        if len(post_ids) > 120 or not settings.CONTENT_LLM_API_KEY:
            raise HTTPException(status_code=400)
        selected_ids = sorted(set(post_ids))
        if not selected_ids:
            raise HTTPException(status_code=400)
        async with _db() as db:
            placeholders = ",".join("?" for _ in selected_ids)
            rows = await _fetch_all(
                db,
                f"SELECT id, version FROM posts WHERE id IN ({placeholders}) ORDER BY id",
                selected_ids,
            )
        if len(rows) != len(selected_ids):
            raise HTTPException(status_code=400)
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "posts": [(row["id"], row["version"]) for row in rows],
                    "instruction": instruction.strip(),
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()[:24]
        await ContentJobQueue(settings.DB_PATH).enqueue(
            JOB_REVISION,
            {
                "post_ids": selected_ids,
                "instruction": instruction.strip(),
                "actor_id": user_id,
            },
            idempotency_key=f"revision:{fingerprint}",
        )
        return _editor_redirect(month, week, "revision_started", view, post_type)

    @app.post("/editor/posts/type-schedule")
    async def schedule_content_type(
        month: str = Form(max_length=7),
        week: str = Form(max_length=10),
        view: str = Form(default="week", max_length=8),
        post_type: str = Form(default="all", max_length=16),
        scope_post_type: str = Form(max_length=16),
        bulk_publish_at: str = Form(max_length=32),
        csrf_token: str = Form(),
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        _validate_csrf(auth, content_session, csrf_token)
        selected_month, _, _ = _month_bounds(month)
        selected_week = _selected_week(selected_month, week)
        try:
            result = await ContentPlanningService(
                settings.DB_PATH
            ).update_scope_publish_time(
                selected_month,
                selected_week,
                scope_post_type,
                _parse_local_datetime(bulk_publish_at),
                user_id,
            )
        except ContentPlanningError:
            raise HTTPException(status_code=400)
        notice = "type_schedule_saved" if result["updated"] else "type_schedule_empty"
        return _editor_redirect(selected_month, selected_week, notice, view, scope_post_type)

    @app.post("/editor/posts/bulk-schedule")
    async def schedule_content_posts(
        request: Request,
        month: str = Form(max_length=7),
        week: str = Form(max_length=10),
        view: str = Form(default="week", max_length=8),
        post_type: str = Form(default="all", max_length=16),
        post_ids: list[int] = Form(),
        csrf_token: str = Form(),
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        _validate_csrf(auth, content_session, csrf_token)
        if len(post_ids) > 120:
            raise HTTPException(status_code=400)
        form = await request.form()
        values: dict[int, str] = {}
        for post_id in sorted(set(post_ids)):
            raw = str(form.get(f"publish_at_{post_id}") or "")
            if not raw:
                raise HTTPException(status_code=400)
            values[post_id] = _parse_local_datetime(raw).isoformat()
        try:
            await ContentPlanningService(settings.DB_PATH).update_publish_times(
                values, user_id
            )
        except ContentPlanningError:
            raise HTTPException(status_code=400)
        return _editor_redirect(month, week, "schedule_saved", view, post_type)

    @app.post("/editor/posts/bulk-approve")
    async def approve_content_posts(
        month: str = Form(max_length=7),
        week: str = Form(max_length=10),
        view: str = Form(default="week", max_length=8),
        post_type: str = Form(default="all", max_length=16),
        post_ids: list[int] = Form(),
        csrf_token: str = Form(),
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        _validate_csrf(auth, content_session, csrf_token)
        if len(post_ids) > 120:
            raise HTTPException(status_code=400)
        try:
            await ContentPlanningService(settings.DB_PATH).approve_posts(
                sorted(set(post_ids)), user_id
            )
        except ContentPlanningError:
            return _editor_redirect(month, week, "approval_blocked", view, post_type)
        return _editor_redirect(month, week, "approved", view, post_type)

    @app.post("/editor/posts/bulk-revoke")
    async def revoke_content_approvals(
        month: str = Form(max_length=7),
        week: str = Form(max_length=10),
        view: str = Form(default="week", max_length=8),
        post_type: str = Form(default="all", max_length=16),
        post_ids: list[int] = Form(),
        csrf_token: str = Form(),
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        _validate_csrf(auth, content_session, csrf_token)
        if len(post_ids) > 120:
            raise HTTPException(status_code=400)
        try:
            result = await ContentPlanningService(settings.DB_PATH).revoke_approvals(
                sorted(set(post_ids)), user_id
            )
        except ContentPlanningError:
            raise HTTPException(status_code=409)
        notice = "approval_revoked" if result["revoked"] else "approval_revoke_empty"
        return _editor_redirect(month, week, notice, view, post_type)

    @app.post("/editor/posts")
    async def create_post(
        channel_id: int = Form(),
        post_type: str = Form(),
        topic: str = Form(max_length=200),
        body: str = Form(default="", max_length=4096),
        publish_at: str = Form(default="", max_length=32),
        csrf_token: str = Form(),
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        _validate_csrf(auth, content_session, csrf_token)
        post_type = _validate_post_type(post_type)
        parsed_time = _parse_local_datetime(publish_at) if publish_at else None
        post_status = "review" if body.strip() and parsed_time else "draft"
        async with _db() as db:
            cursor = await db.execute("SELECT 1 FROM channels WHERE id = ?", (channel_id,))
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=400)
            cursor = await db.execute(
                """
                INSERT INTO posts (
                    channel_id, post_type, topic, body, status, publish_at, created_by
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    channel_id,
                    post_type,
                    topic.strip(),
                    body.strip(),
                    post_status,
                    parsed_time.isoformat() if parsed_time else None,
                    user_id,
                ),
            )
            post_id = cursor.lastrowid
            await db.execute(
                """
                INSERT INTO post_events (
                    post_id, event_type, from_status, to_status, actor_type, actor_id
                ) VALUES (?, 'created', NULL, ?, 'user', ?)
                """,
                (post_id, post_status, str(user_id)),
            )
            await db.commit()
        if body.strip():
            await _enqueue_quality_review([post_id], user_id)
        target_month = parsed_time.astimezone(MOSCOW).strftime("%Y-%m") if parsed_time else ""
        target_week = _selected_week(
            target_month or datetime.now(MOSCOW).strftime("%Y-%m"),
            parsed_time.astimezone(MOSCOW).date().isoformat() if parsed_time else None,
        )
        return _editor_redirect(
            target_month or datetime.now(MOSCOW).strftime("%Y-%m"),
            target_week,
            "post_created",
        )

    @app.get("/editor/posts/{post_id}", response_class=HTMLResponse)
    async def edit_post_page(
        request: Request,
        post_id: int,
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        async with _db() as db:
            post = await _fetch_one(
                db,
                """
                SELECT p.*, w.title AS webinar_title,
                       w.registration_url AS webinar_registration_url
                FROM posts p LEFT JOIN webinars w ON w.id = p.webinar_id
                WHERE p.id = ?
                """,
                (post_id,),
            )
            channels = await _fetch_all(db, "SELECT id, title FROM channels ORDER BY title")
        if not post:
            raise HTTPException(status_code=404)
        post["publish_at_local"] = _datetime_local(post.get("publish_at"))
        post_month = post.get("plan_month") or (
            post["publish_at_local"][:7] if post["publish_at_local"] else datetime.now(MOSCOW).strftime("%Y-%m")
        )
        post_week = post.get("week_start") or _selected_week(post_month, None)
        return templates.TemplateResponse(
            request,
            "post_editor.html",
            _context(
                auth,
                content_session,
                user_id,
                "editor",
                post=post,
                channels=channels,
                month=post_month,
                week=post_week,
            ),
        )

    @app.post("/editor/posts/{post_id}")
    async def update_post(
        post_id: int,
        channel_id: int = Form(),
        post_type: str = Form(),
        topic: str = Form(max_length=200),
        body: str = Form(max_length=4096),
        publish_at: str = Form(max_length=32),
        include_webinar_link: bool = Form(default=False),
        requires_link: bool = Form(default=False),
        version: int = Form(),
        csrf_token: str = Form(),
        user_id: int = Depends(current_user),
        content_session: str = Cookie(alias=SESSION_COOKIE),
    ):
        assert auth is not None
        _validate_csrf(auth, content_session, csrf_token)
        post_type = _validate_post_type(post_type)
        parsed_time = _parse_local_datetime(publish_at)
        async with _db() as db:
            cursor = await db.execute("SELECT 1 FROM channels WHERE id = ?", (channel_id,))
            if await cursor.fetchone() is None:
                raise HTTPException(status_code=400)
        try:
            await PostWorkflow(settings.DB_PATH).edit_content(
                post_id,
                Actor("user", user_id),
                channel_id=channel_id,
                post_type=post_type,
                topic=topic.strip(),
                body=body.strip(),
                publish_at=parsed_time,
                include_webinar_link=include_webinar_link,
                requires_link=requires_link and include_webinar_link,
                expected_version=version,
            )
        except PostWorkflowError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        await _enqueue_quality_review([post_id], user_id)
        month = parsed_time.astimezone(MOSCOW).strftime("%Y-%m")
        week = _selected_week(
            month, parsed_time.astimezone(MOSCOW).date().isoformat()
        )
        return _editor_redirect(month, week, "post_saved")

    return app


def _context(auth: AuthService, session: str, user_id: int, active: str, **values):
    return {
        "active": active,
        "user_id": user_id,
        "csrf_token": auth.csrf_token(session),
        **values,
    }


def _validate_csrf(auth: AuthService, session: str, token: str) -> None:
    if not auth.validate_csrf(session, token):
        raise HTTPException(status_code=403)


async def _enqueue_quality_review(post_ids: list[int], actor_id: int) -> None:
    async with _db() as db:
        placeholders = ",".join("?" for _ in post_ids)
        rows = await _fetch_all(
            db,
            f"SELECT id, version FROM posts WHERE id IN ({placeholders}) ORDER BY id",
            post_ids,
        )
    if len(rows) != len(post_ids):
        raise HTTPException(status_code=404)
    fingerprint = hashlib.sha256(
        json.dumps([(row["id"], row["version"]) for row in rows]).encode()
    ).hexdigest()[:24]
    await ContentJobQueue(settings.DB_PATH).enqueue(
        JOB_QUALITY,
        {"post_ids": post_ids, "actor_id": actor_id},
        idempotency_key=f"quality:{fingerprint}",
        total_items=len(post_ids),
    )


def _lines_json(value: str) -> str:
    return json.dumps(
        [line.strip() for line in value.splitlines() if line.strip()],
        ensure_ascii=False,
    )


def _validate_post_type(value: str) -> str:
    allowed = {"useful", "warming", "selling"}
    if value not in allowed:
        raise HTTPException(status_code=400)
    return value


def _selected_week(month: str, value: str | None) -> str:
    if value:
        try:
            selected = date.fromisoformat(value)
        except ValueError:
            raise HTTPException(status_code=400)
        selected -= timedelta(days=selected.weekday())
        return selected.isoformat()

    today = datetime.now(MOSCOW).date()
    if today.strftime("%Y-%m") == month:
        selected = today - timedelta(days=today.weekday())
        if today.weekday() == 6:
            selected += timedelta(days=7)
        return selected.isoformat()

    try:
        first = datetime.strptime(month, "%Y-%m").date()
    except ValueError:
        raise HTTPException(status_code=400)
    return (first + timedelta(days=(-first.weekday()) % 7)).isoformat()


def _editor_redirect(
    month: str,
    week: str,
    notice: str,
    view: str = "week",
    post_type: str = "all",
) -> RedirectResponse:
    selected_month, _, _ = _month_bounds(month)
    selected_week = _selected_week(selected_month, week)
    query = urlencode(
        {
            "month": selected_month,
            "week": selected_week,
            "view": view if view in {"week", "month"} else "week",
            "post_type": post_type if post_type in {"all", "useful", "warming", "selling"} else "all",
            "notice": notice,
        }
    )
    return RedirectResponse(f"/editor?{query}", status_code=303)


def _notice_text(code: str | None) -> str:
    return {
        "month_generating": "План месяца создан. ИИ формирует темы и полезные посты ближайшей недели.",
        "month_created": "План месяца создан. Для тем подключите модель генерации.",
        "webinar_saved": "Вебинар недели сохранён для выбранных каналов.",
        "webinar_required": "Сначала заполните вебинар этой недели.",
        "link_saved": "Ссылка добавлена во все связанные публикации.",
        "week_generating": "Генерация недели запущена. Обновите страницу через несколько минут.",
        "webinar_posts_generating": "Генерация прогревающих и продающих постов запущена. Обновите страницу через несколько минут.",
        "revision_started": "Массовая редактура запущена. Обновите страницу через несколько минут.",
        "schedule_saved": "Дата и время сохранены.",
        "type_schedule_saved": "Одна дата и время назначены всем постам выбранного типа за неделю.",
        "type_schedule_empty": "Для выбранного типа и недели нет постов, которым можно назначить время.",
        "approval_blocked": "Не всё готово: проверьте текст, будущее время и обязательную ссылку.",
        "approved": "Выбранные посты одобрены и поставлены в автоматическую публикацию.",
        "approval_revoked": "Разрешение на публикацию отозвано. Дата сохранена, посты возвращены на проверку.",
        "approval_revoke_empty": "Среди выбранных постов нет одобренных публикаций.",
        "post_created": "Пост добавлен в контент-план.",
        "post_saved": "Изменения поста сохранены; при необходимости нужен повторный апрув.",
        "job_running": "Такая генерация уже выполняется. Дождитесь завершения и обновите страницу.",
        "job_cancelled": "Задача отменена.",
        "job_not_cancelled": "Задача уже завершена или была отменена ранее.",
        "job_retried": "Задача снова поставлена в очередь.",
        "job_not_retried": "Повторить можно только отменённую или завершившуюся ошибкой задачу.",
    }.get(code or "", "")


def _month_bounds(value: str | None) -> tuple[str, datetime, datetime]:
    selected = value or datetime.now(MOSCOW).strftime("%Y-%m")
    try:
        start_local = datetime.strptime(selected, "%Y-%m").replace(tzinfo=MOSCOW)
    except ValueError:
        raise HTTPException(status_code=400)
    if not 2020 <= start_local.year <= 2100:
        raise HTTPException(status_code=400)
    if start_local.month == 12:
        end_local = start_local.replace(year=start_local.year + 1, month=1)
    else:
        end_local = start_local.replace(month=start_local.month + 1)
    return selected, start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _parse_local_datetime(value: str) -> datetime:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        raise HTTPException(status_code=400)
    return parsed.replace(tzinfo=MOSCOW).astimezone(timezone.utc)


def _datetime_local(value: str | None) -> str:
    if not value:
        return ""
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(MOSCOW).strftime("%Y-%m-%dT%H:%M")


class _db:
    async def __aenter__(self):
        self.connection = await aiosqlite.connect(settings.DB_PATH)
        self.connection.row_factory = aiosqlite.Row
        await configure_connection(self.connection)
        return self.connection

    async def __aexit__(self, exc_type, exc, traceback):
        await self.connection.close()


async def _fetch_one(db, query: str, params=()):
    cursor = await db.execute(query, params)
    row = await cursor.fetchone()
    return dict(row) if row else {}


async def _fetch_all(db, query: str, params=()):
    cursor = await db.execute(query, params)
    return [dict(row) for row in await cursor.fetchall()]


def _moscow_datetime(value: str | None) -> str:
    if not value:
        return "Дата не назначена"
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(MOSCOW).strftime("%d.%m.%Y · %H:%M МСК")


app = create_app()
