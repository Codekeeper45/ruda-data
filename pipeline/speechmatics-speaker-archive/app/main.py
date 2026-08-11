from __future__ import annotations

import hashlib
import json
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload, selectinload

from .audio import build_listening_clip, probe_duration
from .config import BASE_DIR, settings
from .db import Base, SessionLocal, engine
from .enrichment import seed_mafia_roles
from .exports import (
    backup_sqlite_database,
    export_video_csv,
    export_video_json,
    export_video_srt,
    export_video_txt,
    export_video_vtt,
)
from .models import (
    EmbeddingJob,
    EmbeddingVector,
    EnrichmentEvidence,
    EnrichmentRun,
    JobKind,
    JobStatus,
    MafiaEvent,
    MafiaPhase,
    MafiaRole,
    MafiaRound,
    MafiaRoundParticipant,
    ProfileReview,
    SampleAudit,
    SemanticDocument,
    SourceFolder,
    SpeakerProfile,
    SpeakerSample,
    TranscriptionJob,
    Utterance,
    Video,
    VideoEnrichment,
    VideoSpeaker,
    Word,
)
from .migrations import ensure_database_features
from .openrouter import OpenRouterClient, OpenRouterError
from .scanner import scan_source_folder
from .secrets import get_api_key, get_openrouter_api_key, get_setting, set_setting
from .semantic_search import SearchFilters, answer_with_rag, hybrid_search
from .transcript_parser import format_timestamp
from .worker import worker


@asynccontextmanager
async def lifespan(_app: FastAPI):
    Base.metadata.create_all(bind=engine)
    ensure_database_features(engine)
    with SessionLocal() as session:
        seed_mafia_roles(session)
        defaults = {
            "language": "ru",
            "model": "enhanced",
            "speechmatics_base_url": settings.speechmatics_base_url,
            "speakers_sensitivity": "0.5",
            "speaker_sensitivity": "",
            "additional_vocab": "",
            "enrichment_model": settings.enrichment_model,
            "enrichment_verifier_model": settings.enrichment_verifier_model,
            "enrichment_escalation_model": settings.enrichment_escalation_model,
            "enrichment_vision_model": settings.enrichment_vision_model,
            "embedding_model": settings.embedding_model,
            "rag_answer_model": settings.rag_answer_model,
        }
        for key, value in defaults.items():
            if get_setting(session, key) is None:
                set_setting(session, key, value)
        session.commit()
    if settings.worker_enabled:
        worker.start()
    yield
    if settings.worker_enabled:
        await worker.stop()


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")
templates.env.filters["timestamp"] = format_timestamp
templates.env.filters["filesize"] = lambda value: _human_size(value or 0)
templates.env.filters["duration"] = lambda value: _human_duration(value)
CONTENT_TYPE_LABELS = {
    "talk_only": "только разговор",
    "mixed": "разговор и мафия",
    "mafia_only": "только мафия",
    "unknown": "не определено",
}
FACTION_LABELS = {
    "civilians": "мирные",
    "mafia": "мафия",
    "neutral": "нейтральная сторона",
    "draw": "ничья",
    "unknown": "неизвестно",
}
REVIEW_STATUS_LABELS = {
    "auto_verified": "проверено автоматически",
    "confirmed": "подтверждено вручную",
    "needs_review": "нужна проверка",
    "unknown": "неизвестно",
    "pending": "ожидает",
}
PHASE_TYPE_LABELS = {
    "introduction": "представление",
    "day": "день",
    "voting": "голосование",
    "last_words": "последнее слово",
    "night": "ночь",
    "result": "итог",
    "intermission": "перерыв",
    "unknown": "неизвестно",
}
OUTCOME_LABELS = {
    "won": "победил",
    "lost": "проиграл",
    "unknown": "неизвестно",
}
RUN_STATUS_LABELS = {
    "completed": "готово",
    "failed": "ошибка",
    "running": "в работе",
    "queued": "в очереди",
}
STAGE_LABELS = {
    "extract": "извлечение структуры",
    "frames": "проверка кадров",
    "documents": "смысловые фрагменты",
    "embeddings": "эмбеддинги",
}
templates.env.filters["content_type_label"] = (
    lambda value: CONTENT_TYPE_LABELS.get(value, value)
)
templates.env.filters["faction_label"] = lambda value: FACTION_LABELS.get(value, value)
templates.env.filters["review_status_label"] = (
    lambda value: REVIEW_STATUS_LABELS.get(value, value)
)
templates.env.filters["phase_type_label"] = (
    lambda value: PHASE_TYPE_LABELS.get(value, value)
)
templates.env.filters["outcome_label"] = lambda value: OUTCOME_LABELS.get(value, value)
templates.env.filters["run_status_label"] = (
    lambda value: RUN_STATUS_LABELS.get(value, value)
)
templates.env.filters["stage_label"] = lambda value: STAGE_LABELS.get(value, value)


def _human_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TB"


def _human_duration(value: float | None) -> str:
    if value is None:
        return "—"
    seconds = int(round(value))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def redirect(path: str, message: str | None = None, *, error: bool = False) -> RedirectResponse:
    if message:
        base, marker, fragment = path.partition("#")
        separator = "&" if "?" in base else "?"
        base = f"{base}{separator}{'error' if error else 'message'}={quote(message)}"
        path = f"{base}#{fragment}" if marker else base
    return RedirectResponse(path, status_code=303)


def mutation_response(
    request: Request,
    path: str,
    message: str,
    *,
    payload: dict[str, Any] | None = None,
    error: bool = False,
):
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse(
            {"ok": not error, "message": message, **(payload or {})},
            status_code=400 if error else 200,
        )
    return redirect(path, message, error=error)


def template(request: Request, name: str, **context):
    context.update(
        request=request,
        app_name=settings.app_name,
        message=request.query_params.get("message"),
        error=request.query_params.get("error"),
        worker=worker,
    )
    return templates.TemplateResponse(name, context)


@app.get("/health", response_class=JSONResponse)
def health() -> dict[str, object]:
    return {
        "ok": True,
        "worker_enabled": settings.worker_enabled,
        "worker_last_tick": worker.last_tick_at.isoformat() if worker.last_tick_at else None,
        "worker_error": worker.last_error,
    }


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    with SessionLocal() as session:
        counts = {
            "profiles": session.scalar(select(func.count()).select_from(SpeakerProfile)) or 0,
            "identifiers": session.scalar(
                select(func.count()).select_from(SpeakerSample).where(SpeakerSample.speaker_identifier.is_not(None))
            ) or 0,
            "videos": session.scalar(select(func.count()).select_from(Video)) or 0,
            "queued": session.scalar(
                select(func.count()).select_from(TranscriptionJob).where(TranscriptionJob.status == JobStatus.QUEUED)
            ) or 0,
            "running": session.scalar(
                select(func.count()).select_from(TranscriptionJob).where(
                    TranscriptionJob.status.in_([JobStatus.PREPARING, JobStatus.SUBMITTING, JobStatus.RUNNING])
                )
            ) or 0,
            "completed": session.scalar(
                select(func.count()).select_from(Video).where(Video.status == JobStatus.COMPLETED)
            ) or 0,
            "failed": session.scalar(
                select(func.count()).select_from(Video).where(Video.status == JobStatus.FAILED)
            ) or 0,
        }
        recent_videos = session.scalars(select(Video).order_by(Video.created_at.desc()).limit(12)).all()
        active_job = session.scalar(
            select(TranscriptionJob)
            .where(TranscriptionJob.status.in_([JobStatus.PREPARING, JobStatus.SUBMITTING, JobStatus.RUNNING]))
            .options(joinedload(TranscriptionJob.video), joinedload(TranscriptionJob.sample))
            .order_by(TranscriptionJob.created_at)
        )
        api_configured = bool(get_api_key(session))
        processing_paused = get_setting(session, "processing_paused", "0") == "1"
        processing_pause_reason = get_setting(session, "processing_pause_reason", "") or ""
        return template(
            request,
            "dashboard.html",
            counts=counts,
            recent_videos=recent_videos,
            active_job=active_job,
            api_configured=api_configured,
            worker_enabled=settings.worker_enabled,
            processing_paused=processing_paused,
            processing_pause_reason=processing_pause_reason,
        )


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    with SessionLocal() as session:
        values = {
            key: get_setting(session, key, "") or ""
            for key in (
                "language",
                "model",
                "speechmatics_base_url",
                "speaker_sensitivity",
                "additional_vocab",
            )
        }
        return template(
            request,
            "settings.html",
            values=values,
            api_configured=bool(get_api_key(session)),
            api_from_env=bool(settings.speechmatics_api_key),
            openrouter_configured=bool(get_openrouter_api_key(session)),
            openrouter_from_env=bool(settings.openrouter_api_key),
            enrichment_models={
                "primary": settings.enrichment_model,
                "verifier": settings.enrichment_verifier_model,
                "escalation": settings.enrichment_escalation_model,
                "vision": settings.enrichment_vision_model,
                "embedding": settings.embedding_model,
                "rag": settings.rag_answer_model,
            },
            app_secret_is_default=settings.app_secret_key == "change-me-in-production",
        )


@app.post("/settings")
def update_settings(
    api_key: Annotated[str, Form()] = "",
    openrouter_api_key: Annotated[str, Form()] = "",
    language: Annotated[str, Form()] = "ru",
    model: Annotated[str, Form()] = "enhanced",
    speechmatics_base_url: Annotated[str, Form()] = settings.speechmatics_base_url,
    speaker_sensitivity: Annotated[str, Form()] = "",
    additional_vocab: Annotated[str, Form()] = "",
):
    if model not in {"standard", "enhanced"}:
        raise HTTPException(400, "Unsupported model")
    if speaker_sensitivity:
        value = float(speaker_sensitivity)
        if not 0 <= value <= 1:
            raise HTTPException(400, "Diarization sensitivity must be between 0 and 1")
    with SessionLocal() as session:
        if api_key.strip() and not settings.speechmatics_api_key:
            set_setting(session, "speechmatics_api_key", api_key.strip(), encrypted=True)
        if openrouter_api_key.strip() and not settings.openrouter_api_key:
            set_setting(
                session,
                "openrouter_api_key",
                openrouter_api_key.strip(),
                encrypted=True,
            )
        set_setting(session, "language", language.strip() or "ru")
        set_setting(session, "model", model)
        set_setting(session, "speechmatics_base_url", speechmatics_base_url.rstrip("/"))
        set_setting(session, "speaker_sensitivity", speaker_sensitivity.strip())
        set_setting(session, "additional_vocab", additional_vocab.strip())
        session.commit()
    return redirect("/settings", "Settings saved")


@app.get("/profiles", response_class=HTMLResponse)
def profiles_page(request: Request):
    with SessionLocal() as session:
        profiles = session.scalars(
            select(SpeakerProfile)
            .options(
                selectinload(SpeakerProfile.review),
                selectinload(SpeakerProfile.samples).selectinload(SpeakerSample.audit),
            )
            .order_by(SpeakerProfile.name)
        ).all()
        return template(request, "profiles.html", profiles=profiles)


@app.get("/profiles/review", response_class=HTMLResponse)
def profiles_review_page(request: Request):
    with SessionLocal() as session:
        profiles = _load_review_profiles(session)
        totals, ready_to_enroll = _review_totals(profiles)
        return template(
            request,
            "profiles_review.html",
            profiles=profiles,
            totals=totals,
            ready_to_enroll=ready_to_enroll,
        )


def _load_review_profiles(session) -> list[SpeakerProfile]:
    return list(
        session.scalars(
            select(SpeakerProfile)
            .options(
                selectinload(SpeakerProfile.review),
                selectinload(SpeakerProfile.samples).selectinload(SpeakerSample.audit),
            )
            .order_by(SpeakerProfile.name)
        ).all()
    )


def _review_totals(profiles: list[SpeakerProfile]) -> tuple[dict[str, int], bool]:
    totals = {
        "profiles": len(profiles),
        "approved_profiles": sum(
            1 for profile in profiles if profile.review and profile.review.manual_status == "approved"
        ),
        "rejected_profiles": sum(
            1 for profile in profiles if profile.review and profile.review.manual_status == "rejected"
        ),
        "samples": sum(len(profile.samples) for profile in profiles),
        "approved_samples": sum(
            1
            for profile in profiles
            for sample in profile.samples
            if sample.audit and sample.audit.manual_status == "approved"
        ),
        "rejected_samples": sum(
            1
            for profile in profiles
            for sample in profile.samples
            if sample.audit and sample.audit.manual_status == "rejected"
        ),
        "selected_samples": sum(
            1
            for profile in profiles
            for sample in profile.samples
            if sample.audit and sample.audit.selected_for_enrollment
        ),
        "warning_samples": sum(
            1
            for profile in profiles
            for sample in profile.samples
            if sample.audit and sample.audit.quality_status == "warning"
        ),
        "blocked_samples": sum(
            1
            for profile in profiles
            for sample in profile.samples
            if sample.audit and sample.audit.quality_status == "blocked"
        ),
    }
    ready = bool(profiles) and all(
        profile.review and profile.review.manual_status in {"approved", "rejected"}
        for profile in profiles
    ) and all(
        any(
            sample.audit
            and sample.audit.manual_status == "approved"
            and sample.audit.selected_for_enrollment
            and sample.audit.quality_status != "blocked"
            for sample in profile.samples
        )
        for profile in profiles
        if profile.review and profile.review.manual_status == "approved"
    )
    return totals, ready


def _profile_review_payload(
    profiles: list[SpeakerProfile],
    profile_id: int,
) -> dict[str, Any]:
    profile = next(item for item in profiles if item.id == profile_id)
    totals, ready = _review_totals(profiles)
    samples = [
        {
            "id": sample.id,
            "manual_status": sample.audit.manual_status,
            "selected": bool(sample.audit.selected_for_enrollment),
            "blocked": sample.audit.quality_status == "blocked",
        }
        for sample in profile.samples
        if sample.audit
    ]
    return {
        "profile": {
            "id": profile.id,
            "status": profile.review.manual_status if profile.review else "pending",
            "samples_count": len(profile.samples),
            "approved_count": sum(item["manual_status"] == "approved" for item in samples),
            "selected_count": sum(item["selected"] for item in samples),
            "samples": samples,
        },
        "totals": totals,
        "ready_to_enroll": ready,
    }


@app.post("/profiles/{profile_id}/review")
def review_profile(
    request: Request,
    profile_id: int,
    manual_status: Annotated[str, Form()],
    notes: Annotated[str, Form()] = "",
):
    if manual_status not in {"pending", "approved", "rejected"}:
        raise HTTPException(400, "Неизвестное решение")
    with SessionLocal() as session:
        profile = session.scalar(
            select(SpeakerProfile)
            .where(SpeakerProfile.id == profile_id)
            .options(
                selectinload(SpeakerProfile.review),
                selectinload(SpeakerProfile.samples).selectinload(SpeakerSample.audit),
            )
        )
        if profile is None:
            raise HTTPException(404, "Профиль не найден")
        review = profile.review or ProfileReview(profile_id=profile.id)
        review.manual_status = manual_status
        review.notes = notes.strip() or None
        review.reviewed_at = datetime.now().astimezone() if manual_status != "pending" else None
        session.add(review)
        if manual_status == "approved":
            profile.active = True
            for sample in profile.samples:
                if (
                    sample.audit
                    and sample.audit.manual_status == "pending"
                    and sample.audit.quality_status != "blocked"
                ):
                    sample.audit.manual_status = "approved"
                    sample.audit.reviewed_at = review.reviewed_at
        elif manual_status == "rejected":
            profile.active = False
        session.commit()
        profile_name = profile.name
        payload = _profile_review_payload(_load_review_profiles(session), profile_id)
    return mutation_response(
        request,
        f"/profiles/review#profile-{profile_id}",
        f"Профиль «{profile_name}» подтверждён" if manual_status == "approved"
        else f"Решение по профилю «{profile_name}» сохранено",
        payload=payload,
    )


@app.post("/samples/{sample_id}/review")
def review_sample(
    request: Request,
    sample_id: int,
    manual_status: Annotated[str, Form()],
    manual_notes: Annotated[str, Form()] = "",
):
    if manual_status not in {"pending", "approved", "rejected"}:
        raise HTTPException(400, "Неизвестное решение")
    with SessionLocal() as session:
        sample = session.scalar(
            select(SpeakerSample)
            .where(SpeakerSample.id == sample_id)
            .options(joinedload(SpeakerSample.audit))
        )
        if sample is None or sample.audit is None:
            raise HTTPException(404, "Образец или его проверка не найдены")
        if manual_status == "approved" and sample.audit.quality_status == "blocked":
            return mutation_response(
                request,
                f"/profiles/review#profile-{sample.profile_id}",
                f"Образец {sample.original_filename} заблокирован технической проверкой",
                error=True,
            )
        sample.audit.manual_status = manual_status
        sample.audit.manual_notes = manual_notes.strip() or None
        sample.audit.reviewed_at = datetime.now().astimezone() if manual_status != "pending" else None
        if manual_status == "rejected":
            sample.audit.selected_for_enrollment = False
        elif manual_status == "approved" and not sample.audit.selected_for_enrollment:
            selected_count = session.scalar(
                select(func.count())
                .select_from(SampleAudit)
                .join(SpeakerSample, SpeakerSample.id == SampleAudit.sample_id)
                .where(
                    SpeakerSample.profile_id == sample.profile_id,
                    SampleAudit.selected_for_enrollment.is_(True),
                )
            ) or 0
            if selected_count < 2:
                sample.audit.selected_for_enrollment = True
        session.commit()
        profile_id = sample.profile_id
        payload = _profile_review_payload(_load_review_profiles(session), profile_id)
    message = "Голос принят" if manual_status == "approved" else "Образец отклонён"
    return mutation_response(
        request,
        f"/profiles/review#profile-{profile_id}",
        message,
        payload=payload,
    )


@app.post("/samples/{sample_id}/selection")
def select_sample_for_enrollment(
    request: Request,
    sample_id: int,
    selected: Annotated[bool, Form()] = False,
):
    with SessionLocal() as session:
        sample = session.scalar(
            select(SpeakerSample)
            .where(SpeakerSample.id == sample_id)
            .options(joinedload(SpeakerSample.audit))
        )
        if sample is None or sample.audit is None:
            raise HTTPException(404, "Образец или его проверка не найдены")
        if selected and (
            sample.audit.quality_status == "blocked" or sample.audit.manual_status == "rejected"
        ):
            return mutation_response(
                request,
                f"/profiles/review#profile-{sample.profile_id}",
                "Отклонённый или заблокированный образец нельзя отправить на регистрацию",
                error=True,
            )
        if selected and not sample.audit.selected_for_enrollment:
            selected_count = session.scalar(
                select(func.count())
                .select_from(SampleAudit)
                .join(SpeakerSample, SpeakerSample.id == SampleAudit.sample_id)
                .where(
                    SpeakerSample.profile_id == sample.profile_id,
                    SampleAudit.selected_for_enrollment.is_(True),
                )
            ) or 0
            if selected_count >= 2:
                return mutation_response(
                    request,
                    f"/profiles/review#profile-{sample.profile_id}",
                    "У персонажа уже выбраны два эталона. Сначала уберите один из них.",
                    error=True,
                )
        sample.audit.selected_for_enrollment = selected
        session.commit()
        profile_id = sample.profile_id
        payload = _profile_review_payload(_load_review_profiles(session), profile_id)
    return mutation_response(
        request,
        f"/profiles/review#profile-{profile_id}",
        "Эталон добавлен" if selected else "Эталон снят",
        payload=payload,
    )


@app.get("/samples/{sample_id}/audio")
def play_sample(sample_id: int):
    with SessionLocal() as session:
        sample = session.get(SpeakerSample, sample_id)
        if sample is None:
            raise HTTPException(404, "Образец не найден")
        stored = Path(sample.stored_path).resolve()
    samples_root = (settings.storage_dir / "samples").resolve()
    try:
        stored.relative_to(samples_root)
    except ValueError as exc:
        raise HTTPException(403, "Недопустимый путь образца") from exc
    if not stored.is_file():
        raise HTTPException(404, "Файл образца отсутствует")
    return FileResponse(stored, media_type="audio/wav")


@app.post("/profiles/enrollment/start")
def start_approved_enrollment():
    with SessionLocal() as session:
        profiles = session.scalars(
            select(SpeakerProfile)
            .options(
                selectinload(SpeakerProfile.review),
                selectinload(SpeakerProfile.samples).selectinload(SpeakerSample.audit),
            )
            .order_by(SpeakerProfile.id)
        ).all()
        pending_profiles = [
            profile.name
            for profile in profiles
            if not profile.review or profile.review.manual_status == "pending"
        ]
        if pending_profiles:
            return redirect(
                "/profiles/review",
                f"Сначала проверьте все профили. Без решения: {', '.join(pending_profiles)}",
                error=True,
            )

        selected: list[SpeakerSample] = []
        missing: list[str] = []
        for profile in profiles:
            if not profile.review or profile.review.manual_status != "approved":
                continue
            samples = [
                sample
                for sample in profile.samples
                if (
                    sample.audit
                    and sample.audit.manual_status == "approved"
                    and sample.audit.selected_for_enrollment
                    and sample.audit.quality_status != "blocked"
                )
            ]
            if not samples:
                missing.append(profile.name)
            selected.extend(samples)
        if missing:
            return redirect(
                "/profiles/review",
                f"Нет выбранного подтверждённого образца: {', '.join(missing)}",
                error=True,
            )
        if len(selected) > 50:
            return redirect(
                "/profiles/review",
                f"Выбрано {len(selected)} образцов, а Speechmatics принимает максимум 50",
                error=True,
            )

        queued = 0
        for sample in selected:
            if sample.speaker_identifier or sample.status in {
                JobStatus.QUEUED,
                JobStatus.PREPARING,
                JobStatus.SUBMITTING,
                JobStatus.RUNNING,
                JobStatus.COMPLETED,
            }:
                continue
            sample.status = JobStatus.QUEUED
            sample.error_message = None
            session.add(
                TranscriptionJob(
                    kind=JobKind.ENROLLMENT,
                    status=JobStatus.QUEUED,
                    sample_id=sample.id,
                )
            )
            queued += 1
        session.commit()
    return redirect("/profiles/review", f"В очередь регистрации поставлено: {queued}")


@app.post("/profiles")
def create_profile(
    name: Annotated[str, Form()],
    notes: Annotated[str, Form()] = "",
):
    clean_name = name.strip()
    if not clean_name:
        raise HTTPException(400, "Profile name is required")
    with SessionLocal() as session:
        profile = SpeakerProfile(name=clean_name, api_label="pending", notes=notes.strip() or None)
        session.add(profile)
        try:
            session.flush()
            profile.api_label = f"profile_{profile.id}"
            session.add(ProfileReview(profile_id=profile.id, manual_status="pending"))
            session.commit()
        except IntegrityError:
            session.rollback()
            return redirect("/profiles", "A profile with this name already exists", error=True)
        return redirect(f"/profiles/{profile.id}", "Profile created. Add one or more clean voice samples.")


@app.get("/profiles/{profile_id}", response_class=HTMLResponse)
def profile_detail(request: Request, profile_id: int):
    with SessionLocal() as session:
        profile = session.scalar(
            select(SpeakerProfile)
            .where(SpeakerProfile.id == profile_id)
            .options(
                selectinload(SpeakerProfile.review),
                selectinload(SpeakerProfile.samples).selectinload(SpeakerSample.jobs),
                selectinload(SpeakerProfile.samples).selectinload(SpeakerSample.audit),
            )
        )
        if profile is None:
            raise HTTPException(404, "Profile not found")
        return template(request, "profile_detail.html", profile=profile)


@app.post("/profiles/{profile_id}/samples")
async def upload_profile_samples(
    profile_id: int,
    files: Annotated[list[UploadFile], File()],
):
    saved = 0
    errors: list[str] = []
    for upload in files:
        filename = Path(upload.filename or "sample.wav").name
        if Path(filename).suffix.lower() != ".wav":
            errors.append(f"{filename}: only WAV is accepted for enrollment samples")
            continue
        temp_path = settings.storage_dir / "samples" / f"upload_{datetime.now().timestamp()}_{filename}"
        digest = hashlib.sha256()
        size = 0
        try:
            with temp_path.open("wb") as output:
                while chunk := await upload.read(1024 * 1024):
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
            duration = probe_duration(temp_path)
            if size == 0:
                temp_path.unlink(missing_ok=True)
                errors.append(f"{filename}: empty file")
                continue
            if duration is not None and not 5.0 <= duration <= 30.0:
                temp_path.unlink(missing_ok=True)
                errors.append(f"{filename}: sample must be 5–30 seconds, got {duration:.1f}s")
                continue
            with SessionLocal() as session:
                profile = session.get(SpeakerProfile, profile_id)
                if profile is None:
                    temp_path.unlink(missing_ok=True)
                    raise HTTPException(404, "Profile not found")
                sample = SpeakerSample(
                    profile_id=profile_id,
                    original_filename=filename,
                    stored_path=str(temp_path.resolve()),
                    duration_seconds=duration,
                    size_bytes=size,
                    sha256=digest.hexdigest(),
                    status=JobStatus.QUEUED,
                )
                session.add(sample)
                try:
                    session.flush()
                    final_path = settings.storage_dir / "samples" / f"sample_{sample.id}.wav"
                    temp_path.replace(final_path)
                    sample.stored_path = str(final_path.resolve())
                    sample.status = "pending_review"
                    session.add(
                        SampleAudit(
                            sample_id=sample.id,
                            source_path=str(final_path.resolve()),
                            quality_status="warning",
                            quality_issues=["Загружен вручную — требуется проверка"],
                            manual_status="pending",
                            selected_for_enrollment=False,
                        )
                    )
                    session.commit()
                    saved += 1
                except IntegrityError:
                    session.rollback()
                    temp_path.unlink(missing_ok=True)
                    errors.append(f"{filename}: this exact sample already exists in the profile")
        finally:
            await upload.close()

    message = f"Сохранено для проверки: {saved}"
    if errors:
        message += "; " + "; ".join(errors)
    return redirect(f"/profiles/{profile_id}", message, error=(saved == 0 and bool(errors)))


@app.post("/samples/{sample_id}/retry")
def retry_sample(sample_id: int):
    with SessionLocal() as session:
        sample = session.get(SpeakerSample, sample_id)
        if sample is None:
            raise HTTPException(404, "Sample not found")
        sample.status = JobStatus.QUEUED
        sample.error_message = None
        session.add(
            TranscriptionJob(kind=JobKind.ENROLLMENT, status=JobStatus.QUEUED, sample_id=sample.id)
        )
        session.commit()
        return redirect(f"/profiles/{sample.profile_id}", "Sample queued again")


@app.post("/profiles/{profile_id}/toggle")
def toggle_profile(profile_id: int):
    with SessionLocal() as session:
        profile = session.get(SpeakerProfile, profile_id)
        if profile is None:
            raise HTTPException(404, "Profile not found")
        profile.active = not profile.active
        session.commit()
        return redirect(f"/profiles/{profile_id}", "Profile state updated")


@app.post("/profiles/{profile_id}/delete")
def delete_profile(profile_id: int):
    with SessionLocal() as session:
        profile = session.get(SpeakerProfile, profile_id)
        if profile is None:
            raise HTTPException(404, "Profile not found")
        # Remove stored sample files from disk
        for sample in profile.samples:
            stored = Path(sample.stored_path)
            if stored.exists():
                stored.unlink()
        session.delete(profile)
        session.commit()
        return redirect("/profiles", "Profile deleted")


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    with SessionLocal() as session:
        sources = session.scalars(
            select(SourceFolder).options(selectinload(SourceFolder.videos)).order_by(SourceFolder.created_at.desc())
        ).all()
        return template(request, "sources.html", sources=sources)


@app.post("/sources")
def create_source(
    path: Annotated[str, Form()],
    recursive: Annotated[bool, Form()] = False,
    auto_scan: Annotated[bool, Form()] = False,
):
    root = Path(path).expanduser().resolve()
    if not root.exists() or not root.is_dir():
        return redirect("/sources", f"Folder does not exist: {root}", error=True)
    with SessionLocal() as session:
        source = SourceFolder(path=str(root), recursive=recursive, auto_scan=auto_scan)
        session.add(source)
        try:
            session.flush()
            added, skipped = scan_source_folder(session, source)
            session.commit()
        except IntegrityError:
            session.rollback()
            return redirect("/sources", "This folder is already configured", error=True)
        except Exception as exc:
            session.rollback()
            return redirect("/sources", str(exc), error=True)
    return redirect("/sources", f"Folder added: {added} WAV queued, {skipped} already known")


@app.post("/sources/{source_id}/scan")
def scan_source(source_id: int):
    with SessionLocal() as session:
        source = session.get(SourceFolder, source_id)
        if source is None:
            raise HTTPException(404, "Source not found")
        try:
            added, skipped = scan_source_folder(session, source)
            session.commit()
            return redirect("/sources", f"Scan complete: {added} new, {skipped} skipped")
        except Exception as exc:
            session.rollback()
            return redirect("/sources", str(exc), error=True)


@app.post("/sources/{source_id}/toggle")
def toggle_source(source_id: int):
    with SessionLocal() as session:
        source = session.get(SourceFolder, source_id)
        if source is None:
            raise HTTPException(404, "Source not found")
        source.enabled = not source.enabled
        session.commit()
    return redirect("/sources", "Source state updated")


@app.get("/videos", response_class=HTMLResponse)
def videos_page(
    request: Request,
    status: str = "",
    q: str = "",
    page: int = Query(1, ge=1),
):
    page_size = 50
    with SessionLocal() as session:
        filters = []
        if status:
            filters.append(Video.status == status)
        if q:
            filters.append(or_(Video.title.ilike(f"%{q}%"), Video.source_path.ilike(f"%{q}%")))
        total = session.scalar(select(func.count()).select_from(Video).where(*filters)) or 0
        videos = session.scalars(
            select(Video)
            .where(*filters)
            .order_by(Video.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        pages = max(1, (total + page_size - 1) // page_size)
        return template(
            request,
            "videos.html",
            videos=videos,
            status=status,
            q=q,
            page=page,
            pages=pages,
            total=total,
        )


@app.get("/videos/{video_id}", response_class=HTMLResponse)
def video_detail(
    request: Request,
    video_id: int,
    speaker: str = "",
    q: str = "",
    page: int = Query(1, ge=1),
):
    page_size = 200
    with SessionLocal() as session:
        video = session.scalar(
            select(Video)
            .where(Video.id == video_id)
            .options(selectinload(Video.speakers), selectinload(Video.jobs))
        )
        if video is None:
            raise HTTPException(404, "Video not found")
        filters = [Utterance.video_id == video_id]
        if speaker:
            filters.append(VideoSpeaker.label == speaker)
        if q:
            filters.append(Utterance.text.ilike(f"%{q}%"))
        count_stmt = select(func.count()).select_from(Utterance).join(Utterance.speaker).where(*filters)
        total = session.scalar(count_stmt) or 0
        utterances = session.scalars(
            select(Utterance)
            .join(Utterance.speaker)
            .where(*filters)
            .options(joinedload(Utterance.speaker))
            .order_by(Utterance.sequence)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
        profiles = session.scalars(select(SpeakerProfile).order_by(SpeakerProfile.name)).all()
        pages = max(1, (total + page_size - 1) // page_size)
        return template(
            request,
            "video_detail.html",
            video=video,
            utterances=utterances,
            profiles=profiles,
            selected_speaker=speaker,
            q=q,
            page=page,
            pages=pages,
            total=total,
        )


@app.post("/videos/{video_id}/retry")
def retry_video(video_id: int):
    with SessionLocal() as session:
        video = session.get(Video, video_id)
        if video is None:
            raise HTTPException(404, "Video not found")
        video.status = JobStatus.QUEUED
        video.error_message = None
        video.remote_job_id = None
        session.add(TranscriptionJob(kind=JobKind.VIDEO, status=JobStatus.QUEUED, video_id=video.id))
        session.commit()
    return redirect(f"/videos/{video_id}", "Video queued again")


@app.post("/videos/{video_id}/speakers/{speaker_id}")
def update_video_speaker(
    video_id: int,
    speaker_id: int,
    display_name: Annotated[str, Form()] = "",
    profile_id: Annotated[str, Form()] = "",
):
    with SessionLocal() as session:
        row = session.get(VideoSpeaker, speaker_id)
        if row is None or row.video_id != video_id:
            raise HTTPException(404, "Speaker not found")
        if profile_id:
            profile = session.get(SpeakerProfile, int(profile_id))
            if profile is None:
                raise HTTPException(404, "Profile not found")
            row.profile_id = profile.id
            row.display_name = profile.name
            row.is_known = True
        else:
            row.profile_id = None
            row.is_known = False
            row.display_name = display_name.strip() or row.label
        session.commit()
    return redirect(f"/videos/{video_id}", "Speaker label updated")


def _game_rows(session, *, q: str = "", winner: str = "", review: str = ""):
    statement = (
        select(MafiaRound, Video)
        .join(Video, Video.id == MafiaRound.video_id)
        .order_by(Video.id.desc(), MafiaRound.round_number)
    )
    if q:
        statement = statement.where(Video.title.ilike(f"%{q}%"))
    if winner:
        statement = statement.where(MafiaRound.winning_faction == winner)
    if review:
        statement = statement.where(MafiaRound.review_status == review)
    rows = []
    for round_row, video in session.execute(statement).all():
        participants = session.scalar(
            select(func.count())
            .select_from(MafiaRoundParticipant)
            .where(MafiaRoundParticipant.round_id == round_row.id)
        ) or 0
        unknown_roles = session.scalar(
            select(func.count())
            .select_from(MafiaRoundParticipant)
            .where(
                MafiaRoundParticipant.round_id == round_row.id,
                MafiaRoundParticipant.role_id.is_(None),
            )
        ) or 0
        nights = session.scalar(
            select(func.count())
            .select_from(MafiaPhase)
            .where(
                MafiaPhase.round_id == round_row.id,
                MafiaPhase.phase_type == "night",
            )
        ) or 0
        rows.append(
            {
                "round": round_row,
                "video": video,
                "participants": participants,
                "unknown_roles": unknown_roles,
                "nights": nights,
            }
        )
    return rows


@app.get("/games", response_class=HTMLResponse)
def games_page(
    request: Request,
    q: str = "",
    winner: str = "",
    review: str = "",
):
    with SessionLocal() as session:
        rows = _game_rows(session, q=q, winner=winner, review=review)
        classified = session.scalar(
            select(func.count())
            .select_from(VideoEnrichment)
            .where(VideoEnrichment.status == "completed")
        ) or 0
        return template(
            request,
            "games.html",
            rows=rows,
            classified=classified,
            q=q,
            winner=winner,
            review=review,
        )


@app.get("/games/{round_id}", response_class=HTMLResponse)
def game_detail(request: Request, round_id: int):
    with SessionLocal() as session:
        round_row = session.get(MafiaRound, round_id)
        if round_row is None:
            raise HTTPException(404, "Раунд не найден")
        video = session.get(Video, round_row.video_id)
        participants = session.execute(
            select(MafiaRoundParticipant, MafiaRole)
            .outerjoin(MafiaRole, MafiaRole.id == MafiaRoundParticipant.role_id)
            .where(MafiaRoundParticipant.round_id == round_id)
            .order_by(MafiaRoundParticipant.display_name)
        ).all()
        phases = session.scalars(
            select(MafiaPhase)
            .where(MafiaPhase.round_id == round_id)
            .order_by(MafiaPhase.start_time)
        ).all()
        events = session.scalars(
            select(MafiaEvent)
            .where(MafiaEvent.round_id == round_id)
            .order_by(MafiaEvent.start_time)
        ).all()
        entity_ids = {
            "round": [round_id],
            "participant": [row.id for row, _role in participants],
            "phase": [row.id for row in phases],
            "event": [row.id for row in events],
        }
        evidence: dict[str, list[EnrichmentEvidence]] = {}
        for entity_type, ids in entity_ids.items():
            if not ids:
                continue
            for row in session.scalars(
                select(EnrichmentEvidence)
                .where(
                    EnrichmentEvidence.entity_type == entity_type,
                    EnrichmentEvidence.entity_id.in_(ids),
                )
                .order_by(EnrichmentEvidence.start_time)
            ).all():
                evidence.setdefault(f"{row.entity_type}:{row.entity_id}", []).append(row)
        roles = session.scalars(select(MafiaRole).order_by(MafiaRole.name)).all()
        return template(
            request,
            "game_detail.html",
            game=round_row,
            video=video,
            participants=participants,
            phases=phases,
            events=events,
            evidence=evidence,
            roles=roles,
        )


@app.get("/enrichment/progress", response_class=HTMLResponse)
def enrichment_progress_page(request: Request):
    with SessionLocal() as session:
        counts = {
            "videos": session.scalar(select(func.count()).select_from(Video)) or 0,
            "classified": session.scalar(
                select(func.count())
                .select_from(VideoEnrichment)
                .where(VideoEnrichment.status == "completed")
            )
            or 0,
            "running": session.scalar(
                select(func.count())
                .select_from(EnrichmentRun)
                .where(EnrichmentRun.status == "running")
            )
            or 0,
            "failed": session.scalar(
                select(func.count())
                .select_from(VideoEnrichment)
                .where(VideoEnrichment.status == "failed")
            )
            or 0,
            "mafia_videos": session.scalar(
                select(func.count())
                .select_from(VideoEnrichment)
                .where(VideoEnrichment.has_mafia.is_(True))
            )
            or 0,
            "rounds": session.scalar(select(func.count()).select_from(MafiaRound)) or 0,
            "review": (
                session.scalar(
                    select(func.count())
                    .select_from(MafiaRound)
                    .where(MafiaRound.review_status.in_(["needs_review", "unknown"]))
                )
                or 0
            )
            + (
                session.scalar(
                    select(func.count())
                    .select_from(MafiaRoundParticipant)
                    .where(
                        (MafiaRoundParticipant.review_status.in_(["needs_review", "unknown"]))
                        | (MafiaRoundParticipant.role_id.is_(None))
                    )
                )
                or 0
            ),
            "documents": session.scalar(
                select(func.count()).select_from(SemanticDocument)
            )
            or 0,
            "embeddings": session.scalar(
                select(func.count()).select_from(EmbeddingVector)
            )
            or 0,
            "embedding_failed": session.scalar(
                select(func.count())
                .select_from(EmbeddingJob)
                .where(EmbeddingJob.status == "failed")
            )
            or 0,
        }
        recent_runs = session.scalars(
            select(EnrichmentRun).order_by(EnrichmentRun.id.desc()).limit(50)
        ).all()
        return template(
            request,
            "enrichment_progress.html",
            counts=counts,
            recent_runs=recent_runs,
        )


@app.get("/enrichment/review", response_class=HTMLResponse)
def enrichment_review_page(request: Request):
    with SessionLocal() as session:
        rounds = session.execute(
            select(MafiaRound, Video)
            .join(Video, Video.id == MafiaRound.video_id)
            .where(MafiaRound.review_status.in_(["needs_review", "unknown"]))
            .order_by(Video.id, MafiaRound.round_number)
            .limit(300)
        ).all()
        participants = session.execute(
            select(MafiaRoundParticipant, MafiaRound, Video, MafiaRole)
            .join(MafiaRound, MafiaRound.id == MafiaRoundParticipant.round_id)
            .join(Video, Video.id == MafiaRound.video_id)
            .outerjoin(MafiaRole, MafiaRole.id == MafiaRoundParticipant.role_id)
            .where(
                (MafiaRoundParticipant.review_status.in_(["needs_review", "unknown"]))
                | (MafiaRoundParticipant.role_id.is_(None))
            )
            .order_by(Video.id, MafiaRound.round_number)
            .limit(500)
        ).all()
        return template(
            request,
            "enrichment_review.html",
            rounds=rounds,
            participants=participants,
        )


def _search_filters(
    *,
    video_id: int | None,
    round_id: int | None,
    profile_id: int | None,
    role_code: str,
    winner: str,
    content_type: str,
    start_time: float | None,
    end_time: float | None,
) -> SearchFilters:
    return SearchFilters(
        video_id=video_id,
        round_id=round_id,
        profile_id=profile_id,
        role_code=role_code or None,
        winning_faction=winner or None,
        content_type=content_type or None,
        start_time=start_time,
        end_time=end_time,
    )


@app.get("/search", response_class=HTMLResponse)
def search_page(
    request: Request,
    q: str = "",
    video_id: int | None = None,
    round_id: int | None = None,
    profile_id: int | None = None,
    role_code: str = "",
    winner: str = "",
    content_type: str = "",
    start_time: float | None = None,
    end_time: float | None = None,
    answer: bool = False,
):
    hits = []
    rag = None
    search_error = ""
    with SessionLocal() as session:
        videos = session.scalars(select(Video).order_by(Video.title)).all()
        profiles = session.scalars(select(SpeakerProfile).order_by(SpeakerProfile.name)).all()
        roles = session.scalars(select(MafiaRole).order_by(MafiaRole.name)).all()
        filters = _search_filters(
            video_id=video_id,
            round_id=round_id,
            profile_id=profile_id,
            role_code=role_code,
            winner=winner,
            content_type=content_type,
            start_time=start_time,
            end_time=end_time,
        )
        if q.strip():
            key = get_openrouter_api_key(session)
            if not key:
                search_error = "OpenRouter API-ключ не настроен"
            else:
                try:
                    with OpenRouterClient(key) as client:
                        if answer:
                            rag = answer_with_rag(session, client, q, filters=filters)
                            hits = rag["hits"]
                        else:
                            hits = [
                                row.to_dict()
                                for row in hybrid_search(session, client, q, filters=filters)
                            ]
                except Exception as exc:  # noqa: BLE001
                    search_error = str(exc)
        return template(
            request,
            "search.html",
            q=q,
            hits=hits,
            rag=rag,
            search_error=search_error,
            videos=videos,
            profiles=profiles,
            roles=roles,
            selected={
                "video_id": video_id,
                "round_id": round_id,
                "profile_id": profile_id,
                "role_code": role_code,
                "winner": winner,
                "content_type": content_type,
                "start_time": start_time,
                "end_time": end_time,
                "answer": answer,
            },
        )


@app.get("/api/search")
def search_api(
    q: str = Query(min_length=1),
    video_id: int | None = None,
    round_id: int | None = None,
    profile_id: int | None = None,
    role_code: str = "",
    winner: str = "",
    content_type: str = "",
    start_time: float | None = None,
    end_time: float | None = None,
):
    with SessionLocal() as session:
        key = get_openrouter_api_key(session)
        if not key:
            raise HTTPException(503, "OpenRouter API key is not configured")
        filters = _search_filters(
            video_id=video_id,
            round_id=round_id,
            profile_id=profile_id,
            role_code=role_code,
            winner=winner,
            content_type=content_type,
            start_time=start_time,
            end_time=end_time,
        )
        with OpenRouterClient(key) as client:
            return {
                "results": [
                    row.to_dict()
                    for row in hybrid_search(session, client, q, filters=filters)
                ]
            }


@app.post("/api/rag/answer")
async def rag_answer_api(request: Request):
    payload = await request.json()
    question = str(payload.get("question") or "").strip()
    if not question:
        raise HTTPException(400, "question is required")
    raw_filters = payload.get("filters") or {}
    filters = SearchFilters(
        video_id=raw_filters.get("video_id"),
        round_id=raw_filters.get("round_id"),
        profile_id=raw_filters.get("profile_id"),
        role_code=raw_filters.get("role_code"),
        winning_faction=raw_filters.get("winning_faction"),
        content_type=raw_filters.get("content_type"),
        start_time=raw_filters.get("start_time"),
        end_time=raw_filters.get("end_time"),
    )
    with SessionLocal() as session:
        key = get_openrouter_api_key(session)
        if not key:
            raise HTTPException(503, "OpenRouter API key is not configured")
        with OpenRouterClient(key) as client:
            return answer_with_rag(session, client, question, filters=filters)


@app.get("/api/games")
def games_api(q: str = "", winner: str = "", review: str = ""):
    with SessionLocal() as session:
        return {
            "games": [
                {
                    "id": row["round"].id,
                    "video_id": row["video"].id,
                    "video_title": row["video"].title,
                    "round_number": row["round"].round_number,
                    "start_time": row["round"].start_time,
                    "end_time": row["round"].end_time,
                    "winning_faction": row["round"].winning_faction,
                    "participants": row["participants"],
                    "unknown_roles": row["unknown_roles"],
                    "nights": row["nights"],
                    "confidence": row["round"].confidence,
                    "review_status": row["round"].review_status,
                }
                for row in _game_rows(session, q=q, winner=winner, review=review)
            ]
        }


@app.get("/api/games/{round_id}")
def game_api(round_id: int):
    with SessionLocal() as session:
        game = session.get(MafiaRound, round_id)
        if game is None:
            raise HTTPException(404, "Round not found")
        participants = session.execute(
            select(MafiaRoundParticipant, MafiaRole)
            .outerjoin(MafiaRole, MafiaRole.id == MafiaRoundParticipant.role_id)
            .where(MafiaRoundParticipant.round_id == round_id)
        ).all()
        phases = session.scalars(
            select(MafiaPhase).where(MafiaPhase.round_id == round_id)
        ).all()
        return {
            "id": game.id,
            "video_id": game.video_id,
            "round_number": game.round_number,
            "start_time": game.start_time,
            "end_time": game.end_time,
            "winning_faction": game.winning_faction,
            "winner_summary": game.winner_summary,
            "confidence": game.confidence,
            "review_status": game.review_status,
            "participants": [
                {
                    "id": participant.id,
                    "name": participant.display_name,
                    "role": role.name if role else None,
                    "faction": participant.faction,
                    "outcome": participant.outcome,
                    "confidence": participant.confidence,
                    "role_confidence": participant.role_confidence,
                    "review_status": participant.review_status,
                }
                for participant, role in participants
            ],
            "phases": [
                {
                    "id": phase.id,
                    "type": phase.phase_type,
                    "number": phase.phase_number,
                    "start_time": phase.start_time,
                    "end_time": phase.end_time,
                    "confidence": phase.confidence,
                }
                for phase in phases
            ],
        }


@app.get("/api/enrichment/progress")
def enrichment_progress_api():
    with SessionLocal() as session:
        return {
            "videos_total": session.scalar(select(func.count()).select_from(Video)) or 0,
            "videos_completed": session.scalar(
                select(func.count())
                .select_from(VideoEnrichment)
                .where(VideoEnrichment.status == "completed")
            )
            or 0,
            "videos_failed": session.scalar(
                select(func.count())
                .select_from(VideoEnrichment)
                .where(VideoEnrichment.status == "failed")
            )
            or 0,
            "mafia_videos": session.scalar(
                select(func.count())
                .select_from(VideoEnrichment)
                .where(VideoEnrichment.has_mafia.is_(True))
            )
            or 0,
            "rounds": session.scalar(select(func.count()).select_from(MafiaRound)) or 0,
            "review": (
                session.scalar(
                    select(func.count())
                    .select_from(MafiaRound)
                    .where(MafiaRound.review_status.in_(["needs_review", "unknown"]))
                )
                or 0
            )
            + (
                session.scalar(
                    select(func.count())
                    .select_from(MafiaRoundParticipant)
                    .where(
                        (MafiaRoundParticipant.review_status.in_(["needs_review", "unknown"]))
                        | (MafiaRoundParticipant.role_id.is_(None))
                    )
                )
                or 0
            ),
            "documents": session.scalar(
                select(func.count()).select_from(SemanticDocument)
            )
            or 0,
            "embeddings": session.scalar(
                select(func.count()).select_from(EmbeddingVector)
            )
            or 0,
            "embedding_failed": session.scalar(
                select(func.count())
                .select_from(EmbeddingJob)
                .where(EmbeddingJob.status == "failed")
            )
            or 0,
            "active_runs": session.scalar(
                select(func.count())
                .select_from(EnrichmentRun)
                .where(EnrichmentRun.status == "running")
            )
            or 0,
        }


@app.get("/videos/{video_id}/clip")
def video_clip(
    video_id: int,
    start: float = Query(ge=0),
    end: float = Query(gt=0),
):
    with SessionLocal() as session:
        video = session.get(Video, video_id)
        if video is None:
            raise HTTPException(404, "Video not found")
        duration = video.duration_seconds or end
        start_time = max(0.0, min(start, duration))
        end_time = max(start_time + 0.1, min(end, duration))
        signature = hashlib.sha256(
            f"{video.source_signature}|{start_time:.3f}|{end_time:.3f}".encode()
        ).hexdigest()[:24]
        destination = settings.storage_dir / "clips" / f"video_{video.id}_{signature}.mp3"
        try:
            build_listening_clip(
                Path(video.source_path),
                destination,
                start_time=start_time,
                end_time=end_time,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc
    return FileResponse(destination, media_type="audio/mpeg", filename=destination.name)


@app.post("/games/{round_id}/review")
def review_game(
    request: Request,
    round_id: int,
    start_time: Annotated[float, Form()],
    end_time: Annotated[float, Form()],
    winning_faction: Annotated[str, Form()] = "unknown",
    winner_summary: Annotated[str, Form()] = "",
    review_status: Annotated[str, Form()] = "confirmed",
):
    if end_time <= start_time:
        raise HTTPException(400, "Конец раунда должен быть позже начала")
    with SessionLocal() as session:
        game = session.get(MafiaRound, round_id)
        if game is None:
            raise HTTPException(404, "Раунд не найден")
        game.start_time = start_time
        game.end_time = end_time
        game.winning_faction = winning_faction
        game.winner_summary = winner_summary.strip() or None
        game.review_status = review_status
        session.commit()
    return mutation_response(
        request,
        f"/games/{round_id}#round-review",
        "Данные раунда сохранены",
        payload={"round_id": round_id, "review_status": review_status},
    )


@app.post("/games/{round_id}/participants/{participant_id}")
def review_game_participant(
    request: Request,
    round_id: int,
    participant_id: int,
    role_id: Annotated[str, Form()] = "",
    faction: Annotated[str, Form()] = "unknown",
    outcome: Annotated[str, Form()] = "unknown",
    review_status: Annotated[str, Form()] = "confirmed",
):
    with SessionLocal() as session:
        participant = session.get(MafiaRoundParticipant, participant_id)
        if participant is None or participant.round_id != round_id:
            raise HTTPException(404, "Участник не найден")
        participant.role_id = int(role_id) if role_id else None
        participant.faction = faction
        participant.outcome = outcome
        participant.review_status = review_status
        participant.role_confidence = 1.0 if review_status == "confirmed" else participant.role_confidence
        session.add(
            EnrichmentEvidence(
                entity_type="participant",
                entity_id=participant.id,
                field_name="manual_review",
                source_type="manual",
                source_ref="web",
                excerpt="Ручное подтверждение пользователя",
                confidence=1.0,
            )
        )
        session.commit()
    return mutation_response(
        request,
        f"/games/{round_id}#participant-{participant_id}",
        "Участник сохранён",
        payload={"participant_id": participant_id, "review_status": review_status},
    )


@app.post("/games/{round_id}/phases")
def add_game_phase(
    request: Request,
    round_id: int,
    phase_type: Annotated[str, Form()] = "night",
    phase_number: Annotated[int, Form()] = 1,
    start_time: Annotated[float, Form()] = 0,
    end_time: Annotated[float, Form()] = 0,
):
    if end_time <= start_time:
        raise HTTPException(400, "Конец фазы должен быть позже начала")
    with SessionLocal() as session:
        game = session.get(MafiaRound, round_id)
        if game is None:
            raise HTTPException(404, "Раунд не найден")
        phase = MafiaPhase(
            round_id=round_id,
            phase_type=phase_type,
            phase_number=phase_number,
            start_time=start_time,
            end_time=end_time,
            confidence=1.0,
            review_status="confirmed",
        )
        session.add(phase)
        session.commit()
    return mutation_response(
        request,
        f"/games/{round_id}#phases",
        "Игровая фаза добавлена",
        payload={"phase_id": phase.id},
    )


@app.post("/games/{round_id}/phases/{phase_id}/delete")
def delete_game_phase(request: Request, round_id: int, phase_id: int):
    with SessionLocal() as session:
        phase = session.get(MafiaPhase, phase_id)
        if phase is None or phase.round_id != round_id:
            raise HTTPException(404, "Фаза не найдена")
        session.delete(phase)
        session.commit()
    return mutation_response(
        request,
        f"/games/{round_id}#phases",
        "Игровая фаза удалена",
        payload={"phase_id": phase_id},
    )


@app.get("/videos/{video_id}/export/{format_name}")
def export_video(video_id: int, format_name: str):
    if format_name not in {"json", "csv", "srt", "vtt", "txt", "raw"}:
        raise HTTPException(404, "Unknown export format")
    with SessionLocal() as session:
        video = session.get(Video, video_id)
        if video is None:
            raise HTTPException(404, "Video not found")
        if format_name == "raw":
            if not video.raw_transcript_path or not Path(video.raw_transcript_path).exists():
                raise HTTPException(404, "Raw transcript is unavailable")
            return FileResponse(video.raw_transcript_path, filename=f"video_{video.id}_speechmatics_raw.json")
        destination = settings.storage_dir / "exports" / f"video_{video.id}.{format_name}"
        if format_name == "json":
            export_video_json(session, video, destination)
        elif format_name == "csv":
            export_video_csv(session, video, destination)
        elif format_name == "srt":
            export_video_srt(session, video, destination)
        elif format_name == "vtt":
            export_video_vtt(session, video, destination)
        else:
            export_video_txt(session, video, destination)
    media_types = {
        "json": "application/json",
        "csv": "text/csv",
        "srt": "application/x-subrip",
        "vtt": "text/vtt",
        "txt": "text/plain",
    }
    return FileResponse(destination, media_type=media_types[format_name], filename=destination.name)


@app.get("/export/database")
def export_database():
    destination = settings.storage_dir / "exports" / "speaker_archive.sqlite3"
    try:
        backup_sqlite_database(destination)
    except RuntimeError as exc:
        raise HTTPException(400, str(exc)) from exc
    return FileResponse(destination, media_type="application/vnd.sqlite3", filename=destination.name)
