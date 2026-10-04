from __future__ import annotations

import inspect
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping, Protocol

import aiosqlite

from bot.migrations import configure_connection


CRITERIA = (
    "relevance",
    "specificity",
    "style",
    "structure",
    "cta",
    "factual_safety",
    "originality",
)

TOPIC_SOFT_MAX_CHARS = 90
USEFUL_MIN_BODY_CHARS = 400

_URL_RE = re.compile(r"(?:https?://|t\.me/)", re.IGNORECASE)
_WEBINAR_CTA_RE = re.compile(
    r"(?:\bвебинар\w*|\bпрямом?\s+эфир\w*|\bна\s+эфир\b|\bэфире\b|"
    r"\bинтенсив(?:е|а|у)?\b|\bрегистрац\w*|"
    r"\bзапис(?:ь|и|аться|ывайтесь)\s+на\s+(?:вебинар|эфир|интенсив)\b|"
    r"\bприсоединя\w*\b.*\b(?:вебинар|эфир|ссылк)\b|"
    r"\bприходите\b.*\b(?:вебинар|эфир)\b)",
    re.IGNORECASE,
)
_FEMALE_ONLY_ADDRESS_RE = re.compile(
    r"(?:\bчитательниц\w*|\bдорогие\s+женщин\w*|\bмилые\s+женщин\w*|"
    r"\bдевушк\w*\b)",
    re.IGNORECASE,
)
_WOMEN_AUDIENCE_RE = re.compile(r"женщин", re.IGNORECASE)


class ContentQualityError(ValueError):
    pass


@dataclass(frozen=True)
class QualityIssue:
    code: str
    message: str
    blocking: bool


@dataclass(frozen=True)
class QualityReview:
    id: int
    post_id: int
    post_version: int
    content_hash: str
    prompt_version: str
    model: str
    total_score: float
    criteria: dict[str, float]
    is_blocking: bool
    issues: tuple[QualityIssue, ...]
    created_at: str


@dataclass(frozen=True)
class ApprovalQualityCheck:
    allowed: bool
    reason: str
    review: QualityReview | None


class AsyncQualityEvaluator(Protocol):
    async def evaluate(
        self, prompt: str, context: dict[str, Any]
    ) -> Mapping[str, Any]: ...


EvaluatorCallable = Callable[
    [str, dict[str, Any]], Awaitable[Mapping[str, Any]]
]


_GUARANTEE_RE = re.compile(
    r"(?:\bгарантир\w*|\b100\s*%\s*(?:результат\w*|эффект\w*|гарант\w*)|\bстопроцент\w*|"
    r"\bнавсегда\s+избав\w*|\bточно\s+(?:вылеч\w*|избав\w*))",
    re.IGNORECASE,
)
_MEDICAL_RE = re.compile(
    r"(?:\b(?:лечит|вылечит|излечит|исцелит)\b|"
    r"\b(?:поставим|ставим)\s+диагноз\b|"
    r"\b(?:отмените|прекратите)\s+(?:принимать\s+)?(?:лекарств\w*|препарат\w*)|"
    r"\bзамен(?:ит|яет)\s+(?:врача|лечение)\b)",
    re.IGNORECASE,
)


async def evaluate_post(
    db_path: str | Path,
    post_id: int,
    *,
    evaluator: AsyncQualityEvaluator | EvaluatorCallable | None = None,
    prompt_version: str = "quality-v1",
    model: str = "deterministic",
) -> QualityReview:
    if not isinstance(post_id, int) or isinstance(post_id, bool) or post_id <= 0:
        raise ContentQualityError("post_id must be a positive integer")
    if not prompt_version.strip():
        raise ContentQualityError("prompt_version must not be empty")
    if not model.strip():
        raise ContentQualityError("model must not be empty")

    post = await _load_post(str(db_path), post_id)
    if post is None:
        raise ContentQualityError("Post not found")

    deterministic_issues = _deterministic_issues(post)
    if evaluator is None:
        payload = _deterministic_payload(post, deterministic_issues)
    else:
        prompt = _quality_prompt(post)
        context = {"post": post, "schema_version": "quality-review-v1"}
        result = evaluator.evaluate(prompt, context) if hasattr(evaluator, "evaluate") else evaluator(prompt, context)
        if inspect.isawaitable(result):
            result = await result
        payload = _validate_payload(result)

    combined = _merge_issues(payload["issues"], deterministic_issues)
    is_blocking = bool(payload["is_blocking"] or any(issue.blocking for issue in combined))
    criteria = payload["criteria"]
    total_score = min(float(payload["total_score"]), 49.0) if is_blocking else float(payload["total_score"])

    async with aiosqlite.connect(str(db_path)) as db:
        await configure_connection(db)
        cursor = await db.execute(
            """
            INSERT INTO quality_reviews (
                post_id, post_version, content_hash, prompt_version, model, total_score,
                criteria_json, is_blocking, issues_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                post_id,
                post["version"],
                content_fingerprint(post),
                prompt_version.strip(),
                model.strip(),
                total_score,
                json.dumps(criteria, ensure_ascii=False, sort_keys=True),
                int(is_blocking),
                json.dumps(
                    [issue.__dict__ for issue in combined],
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
        review_id = cursor.lastrowid
        await db.commit()
    review = await get_latest_review(db_path, post_id)
    if review is None or review.id != review_id:
        raise RuntimeError("Quality review was not persisted")
    return review


async def evaluate_posts(
    db_path: str | Path,
    post_ids: list[int] | tuple[int, ...],
    **kwargs: Any,
) -> list[QualityReview]:
    return [await evaluate_post(db_path, post_id, **kwargs) for post_id in post_ids]


async def get_latest_review(
    db_path: str | Path, post_id: int
) -> QualityReview | None:
    async with aiosqlite.connect(str(db_path)) as db:
        await configure_connection(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM quality_reviews WHERE post_id = ? ORDER BY id DESC LIMIT 1",
            (post_id,),
        )
        row = await cursor.fetchone()
    return _review_from_row(dict(row)) if row else None


async def check_approval_quality(
    db_path: str | Path,
    post_id: int,
    *,
    minimum_score: float = 60,
    require_review: bool = True,
) -> ApprovalQualityCheck:
    if not _is_number(minimum_score) or not 0 <= float(minimum_score) <= 100:
        raise ContentQualityError("minimum_score must be between 0 and 100")
    review = await get_latest_review(db_path, post_id)
    if review is None:
        return ApprovalQualityCheck(
            allowed=not require_review,
            reason="quality_review_missing",
            review=None,
        )
    current_post = await _load_post(str(db_path), post_id)
    if current_post is None:
        return ApprovalQualityCheck(False, "post_missing", review)
    if review.content_hash != content_fingerprint(current_post):
        return ApprovalQualityCheck(False, "quality_review_stale", review)
    if review.is_blocking:
        return ApprovalQualityCheck(False, "quality_review_blocking", review)
    if review.total_score < float(minimum_score):
        return ApprovalQualityCheck(False, "quality_score_too_low", review)
    return ApprovalQualityCheck(True, "quality_review_passed", review)


async def is_post_approval_allowed(
    db_path: str | Path, post_id: int, **kwargs: Any
) -> bool:
    return (await check_approval_quality(db_path, post_id, **kwargs)).allowed


async def _load_post(db_path: str, post_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(db_path) as db:
        await configure_connection(db)
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """
            SELECT p.*, c.title AS channel_title,
                   cp.audience AS channel_audience,
                   cp.tone_of_voice, cp.cta_rules, cp.forbidden_topics_json
            FROM posts p JOIN channels c ON c.id = p.channel_id
            LEFT JOIN channel_profiles cp ON cp.channel_id = c.id
            WHERE p.id = ?
            """,
            (post_id,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        post = dict(row)
        cursor = await db.execute(
            """
            SELECT body FROM posts
            WHERE channel_id = ? AND id <> ? AND body != ''
            ORDER BY created_at DESC, id DESC LIMIT 8
            """,
            (post["channel_id"], post_id),
        )
        post["recent_bodies"] = [str(item[0])[:500] for item in await cursor.fetchall()]
    return post


def _deterministic_issues(post: Mapping[str, Any] | str) -> list[QualityIssue]:
    if isinstance(post, str):
        body = post
        topic = ""
        post_type = ""
        audience = ""
    else:
        body = str(post.get("body") or "")
        topic = str(post.get("topic") or "")
        post_type = str(post.get("post_type") or "")
        audience = str(post.get("channel_audience") or "")

    issues: list[QualityIssue] = []
    if _GUARANTEE_RE.search(body):
        issues.append(QualityIssue("guaranteed_outcome", "Обнаружена гарантия результата", True))
    if _MEDICAL_RE.search(body):
        issues.append(QualityIssue("medical_claim", "Обнаружено медицинское утверждение или назначение", True))
    if not body.strip():
        issues.append(QualityIssue("empty_body", "Текст публикации пуст", True))

    if len(topic.strip()) > TOPIC_SOFT_MAX_CHARS:
        issues.append(
            QualityIssue(
                "title_too_long",
                f"Заголовок длиннее {TOPIC_SOFT_MAX_CHARS} символов",
                True,
            )
        )

    if post_type == "useful":
        if _URL_RE.search(body):
            issues.append(
                QualityIssue("useful_has_link", "В полезном посте есть ссылка", True)
            )
        if _WEBINAR_CTA_RE.search(body):
            issues.append(
                QualityIssue(
                    "useful_webinar_cta",
                    "В полезном посте есть призыв на вебинар или эфир",
                    True,
                )
            )
        if body.strip() and len(body.strip()) < USEFUL_MIN_BODY_CHARS:
            issues.append(
                QualityIssue(
                    "useful_too_short",
                    f"Полезный пост короче {USEFUL_MIN_BODY_CHARS} символов",
                    False,
                )
            )
        if _FEMALE_ONLY_ADDRESS_RE.search(body) and not _WOMEN_AUDIENCE_RE.search(
            audience
        ):
            issues.append(
                QualityIssue(
                    "female_only_address",
                    "Обращение только к женщинам при смешанной аудитории",
                    False,
                )
            )
    return issues


def _deterministic_payload(
    post: dict[str, Any], issues: list[QualityIssue]
) -> dict[str, Any]:
    body = (post.get("body") or "").strip()
    topic = (post.get("topic") or "").strip()
    post_type = str(post.get("post_type") or "")
    criteria = {name: 75.0 for name in CRITERIA}
    if len(body) < 120:
        criteria["specificity"] = 55.0
        criteria["structure"] = 60.0
    if post_type == "useful" and len(body) < USEFUL_MIN_BODY_CHARS:
        criteria["specificity"] = min(criteria["specificity"], 50.0)
        criteria["structure"] = min(criteria["structure"], 55.0)
    if len(topic) > TOPIC_SOFT_MAX_CHARS:
        criteria["style"] = min(criteria["style"], 45.0)
    if not re.search(r"[.!?]\s*(?:\n|$)", body):
        criteria["structure"] = min(criteria["structure"], 55.0)
    if any(issue.code in {"useful_has_link", "useful_webinar_cta"} for issue in issues):
        criteria["cta"] = 0.0
    if any(
        issue.code in {"guaranteed_outcome", "medical_claim", "empty_body"}
        for issue in issues
    ):
        criteria["factual_safety"] = 0.0
    total = sum(criteria.values()) / len(criteria)
    return {
        "total_score": total,
        "criteria": criteria,
        "is_blocking": any(issue.blocking for issue in issues),
        "issues": [],
    }


def _validate_payload(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ContentQualityError("Evaluator result must be an object")
    expected = {"total_score", "criteria", "is_blocking", "issues"}
    if set(value) != expected:
        raise ContentQualityError("Evaluator result has an invalid schema")
    total = value["total_score"]
    if not _is_number(total) or not 0 <= float(total) <= 100:
        raise ContentQualityError("total_score must be between 0 and 100")
    criteria_value = value["criteria"]
    if not isinstance(criteria_value, Mapping) or set(criteria_value) != set(CRITERIA):
        raise ContentQualityError("criteria must contain the complete quality rubric")
    criteria: dict[str, float] = {}
    for key in CRITERIA:
        score = criteria_value[key]
        if not _is_number(score) or not 0 <= float(score) <= 100:
            raise ContentQualityError(f"criterion {key} must be between 0 and 100")
        criteria[key] = float(score)
    if not isinstance(value["is_blocking"], bool):
        raise ContentQualityError("is_blocking must be boolean")
    if not isinstance(value["issues"], list):
        raise ContentQualityError("issues must be an array")
    issues: list[QualityIssue] = []
    for item in value["issues"]:
        if not isinstance(item, Mapping) or set(item) != {"code", "message", "blocking"}:
            raise ContentQualityError("issue has an invalid schema")
        if not isinstance(item["code"], str) or not item["code"].strip():
            raise ContentQualityError("issue code must not be empty")
        if not isinstance(item["message"], str) or not item["message"].strip():
            raise ContentQualityError("issue message must not be empty")
        if not isinstance(item["blocking"], bool):
            raise ContentQualityError("issue blocking must be boolean")
        issues.append(QualityIssue(item["code"].strip(), item["message"].strip(), item["blocking"]))
    return {
        "total_score": float(total),
        "criteria": criteria,
        "is_blocking": value["is_blocking"],
        "issues": issues,
    }


def _merge_issues(*groups: list[QualityIssue]) -> tuple[QualityIssue, ...]:
    merged: list[QualityIssue] = []
    seen: set[str] = set()
    for group in groups:
        for issue in group:
            if issue.code not in seen:
                merged.append(issue)
                seen.add(issue.code)
    return tuple(merged)


def _quality_prompt(post: dict[str, Any]) -> str:
    post_type = str(post.get("post_type") or "")
    useful_rules = ""
    if post_type == "useful":
        useful_rules = (
            "Для полезного поста блокируй ссылки и призывы на вебинар/эфир/регистрацию. "
            "CTA полезного поста — мягкий бытовой шаг без продажи вебинара. "
            "Снижай style, если заголовок длиннее 90 символов или обращение только к женщинам "
            "при смешанной аудитории. "
        )
    return (
        "Оцени текст Telegram-поста. Не исправляй текст и не добавляй факты. "
        "Верни ровно JSON-объект с ключами: total_score (0–100), "
        "criteria (объект с числовыми ключами relevance, specificity, style, "
        "structure, cta, factual_safety, originality), is_blocking (boolean), "
        "issues (массив объектов code, message, blocking). Блокируй медицинские "
        "обещания, диагнозы, отмену лечения и гарантированный результат. "
        "Заголовок должен быть коротким (до 90 символов). "
        f"{useful_rules}"
        f"Тип: {post_type}\nТема: {post.get('topic', '')}\n"
        f"Канал: {post.get('channel_title', '')}\n"
        f"Аудитория: {post.get('channel_audience') or ''}\n"
        f"Tone of voice: {post.get('tone_of_voice') or ''}\n"
        f"Правила CTA: {post.get('cta_rules') or ''}\n"
        f"Запрещённые темы: {post.get('forbidden_topics_json') or '[]'}\n"
        "Недавние тексты для проверки повторов:\n"
        + "\n---\n".join(post.get("recent_bodies") or [])
        + "\n"
        f"Текст:\n{post.get('body', '')}"
    )


def content_fingerprint(post: Mapping[str, Any]) -> str:
    payload = {
        "channel_id": post.get("channel_id"),
        "post_type": post.get("post_type") or "",
        "topic": post.get("topic") or "",
        "body": post.get("body") or "",
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _review_from_row(row: dict[str, Any]) -> QualityReview:
    criteria = json.loads(row["criteria_json"])
    issues = tuple(QualityIssue(**item) for item in json.loads(row["issues_json"]))
    return QualityReview(
        id=row["id"],
        post_id=row["post_id"],
        post_version=row["post_version"],
        content_hash=row["content_hash"],
        prompt_version=row["prompt_version"],
        model=row["model"],
        total_score=float(row["total_score"]),
        criteria={key: float(value) for key, value in criteria.items()},
        is_blocking=bool(row["is_blocking"]),
        issues=issues,
        created_at=row["created_at"],
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)
