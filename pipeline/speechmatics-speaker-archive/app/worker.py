from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .audio import prepare_for_upload
from .config import settings
from .db import SessionLocal
from .models import (
    JobKind,
    JobStatus,
    SourceFolder,
    SpeakerProfile,
    SpeakerSample,
    TranscriptionJob,
    Video,
    utcnow,
)
from .scanner import scan_source_folder
from .secrets import get_api_key, get_setting, set_setting
from .speechmatics import SpeechmaticsClient, SpeechmaticsError
from .transcript_parser import extract_enrollment_identifier, normalize_video_transcript

logger = logging.getLogger(__name__)

ACTIVE_STATUSES = {
    JobStatus.PREPARING,
    JobStatus.SUBMITTING,
    JobStatus.RUNNING,
}


def is_account_rejection(message: str) -> bool:
    """Return true only for explicit account, billing, or quota rejections."""
    normalized = message.casefold()
    if any(code in normalized for code in ("http 401", "http 402", "http 403")):
        return True
    rejection_terms = (
        "quota exceeded",
        "usage limit",
        "free usage",
        "insufficient credit",
        "insufficient funds",
        "payment required",
        "billing",
    )
    return "speechmatics http 4" in normalized and any(
        term in normalized for term in rejection_terms
    )


class ArchiveWorker:
    def __init__(self) -> None:
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._last_auto_scan = 0.0
        self.last_tick_at = None
        self.last_error: str | None = None

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="speechmatics-archive-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                self.last_tick_at = utcnow()
                await self._auto_scan_if_due()
                await self._process_one()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - worker must stay alive
                logger.exception("Worker tick failed")
                self.last_error = str(exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.worker_poll_seconds)
            except TimeoutError:
                pass

    async def _auto_scan_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_auto_scan < settings.folder_scan_interval_seconds:
            return
        self._last_auto_scan = now
        with SessionLocal() as session:
            sources = session.scalars(
                select(SourceFolder).where(
                    SourceFolder.enabled.is_(True),
                    SourceFolder.auto_scan.is_(True),
                )
            ).all()
            for source in sources:
                try:
                    scan_source_folder(session, source)
                except Exception as exc:  # noqa: BLE001
                    source.last_error = str(exc)
            session.commit()

    async def _process_one(self) -> None:
        with SessionLocal() as session:
            if get_setting(session, "processing_paused", "0") == "1":
                return
            active = session.scalar(
                select(TranscriptionJob)
                .where(TranscriptionJob.status.in_([str(s) for s in ACTIVE_STATUSES]))
                .order_by(TranscriptionJob.created_at.asc())
            )
            if active is not None:
                job_id = active.id
            else:
                now = utcnow()
                queued = session.scalars(
                    select(TranscriptionJob)
                    .where(
                        TranscriptionJob.status == JobStatus.QUEUED,
                        (TranscriptionJob.next_attempt_at.is_(None) | (TranscriptionJob.next_attempt_at <= now)),
                    )
                    .order_by(
                        # Enrollment is deliberately prioritized so video jobs use the newest voiceprints.
                        TranscriptionJob.kind.asc(),
                        TranscriptionJob.created_at.asc(),
                    )
                ).all()
                # String ordering puts enrollment before video.
                job_id = queued[0].id if queued else None

        if job_id is None:
            return

        with SessionLocal() as session:
            job = session.get(TranscriptionJob, job_id)
            if job is None:
                return
            status = JobStatus(job.status)

        if status == JobStatus.QUEUED:
            await self._prepare_and_submit(job_id)
        elif status == JobStatus.PREPARING:
            await self._prepare_and_submit(job_id, resume=True)
        elif status == JobStatus.SUBMITTING:
            await self._recover_uncertain_submission(job_id)
        elif status == JobStatus.RUNNING:
            await self._poll_running_job(job_id)

    async def _prepare_and_submit(self, job_id: int, *, resume: bool = False) -> None:
        try:
            with SessionLocal() as session:
                job = session.get(TranscriptionJob, job_id)
                if job is None:
                    return
                if not settings.mock_transcript_path and not get_api_key(session):
                    job.status = JobStatus.QUEUED
                    job.next_attempt_at = utcnow() + timedelta(seconds=30)
                    self._sync_target_status(job, JobStatus.QUEUED, "Speechmatics API key is not configured")
                    session.commit()
                    return
                job.status = JobStatus.PREPARING
                source_path, destination, config = self._build_job_inputs(session, job)
                job.config_json = config
                self._sync_target_status(job, JobStatus.PREPARING)
                session.commit()
        except Exception as exc:  # noqa: BLE001
            self._mark_failed(job_id, f"Could not build job configuration: {exc}")
            return

        try:
            prepared_path = await prepare_for_upload(source_path, destination)
        except Exception as exc:  # noqa: BLE001
            self._mark_failed(job_id, f"Audio preparation failed: {exc}")
            return

        if settings.mock_transcript_path:
            try:
                transcript = json.loads(settings.mock_transcript_path.read_text(encoding="utf-8"))
                await self._complete_job(job_id, transcript, remote_job_id=f"mock-{job_id}")
            except Exception as exc:  # noqa: BLE001
                self._mark_failed(job_id, f"Mock transcript failed: {exc}")
            return

        with SessionLocal() as session:
            job = session.get(TranscriptionJob, job_id)
            if job is None:
                return
            api_key = get_api_key(session)
            base_url = get_setting(session, "speechmatics_base_url", settings.speechmatics_base_url) or settings.speechmatics_base_url
            if not api_key:
                job.status = JobStatus.QUEUED
                job.next_attempt_at = utcnow() + timedelta(seconds=30)
                self._sync_target_status(job, JobStatus.QUEUED, "Speechmatics API key is not configured")
                session.commit()
                return
            job.prepared_path = str(prepared_path)
            job.status = JobStatus.SUBMITTING
            job.attempt_count += 1
            self._sync_target_status(job, JobStatus.SUBMITTING)
            config = dict(job.config_json or {})
            session.commit()

        client = SpeechmaticsClient(api_key, base_url)
        try:
            remote_job_id = await client.submit_job(prepared_path, config)
        except SpeechmaticsError as exc:
            message = str(exc)
            if is_account_rejection(message):
                with SessionLocal() as session:
                    set_setting(session, "processing_paused", "1")
                    set_setting(
                        session,
                        "processing_pause_reason",
                        f"Speechmatics остановил обработку: {message}",
                    )
                    session.commit()
            # A timeout can happen after the server accepted the file. Automatic resubmission
            # could charge twice, so a human-visible retry is safer.
            self._mark_failed(
                job_id,
                f"Submission failed or its outcome is unknown: {message}. Use Retry after checking the Speechmatics portal.",
            )
            return
        finally:
            await client.close()

        with SessionLocal() as session:
            job = session.get(TranscriptionJob, job_id)
            if job is None:
                return
            job.remote_job_id = remote_job_id
            job.status = JobStatus.RUNNING
            job.submitted_at = utcnow()
            job.error_message = None
            self._sync_target_status(job, JobStatus.RUNNING)
            if job.video is not None:
                job.video.remote_job_id = remote_job_id
                job.video.started_at = job.video.started_at or utcnow()
                job.video.prepared_path = str(prepared_path)
                job.video.prepared_size_bytes = prepared_path.stat().st_size
            session.commit()

    async def _recover_uncertain_submission(self, job_id: int) -> None:
        with SessionLocal() as session:
            job = session.get(TranscriptionJob, job_id)
            if job is None:
                return
            if job.remote_job_id:
                job.status = JobStatus.RUNNING
                self._sync_target_status(job, JobStatus.RUNNING)
                session.commit()
                return
        self._mark_failed(
            job_id,
            "The application restarted while uploading and did not receive a remote job ID. "
            "Check the Speechmatics portal before retrying to avoid duplicate billing.",
        )

    async def _poll_running_job(self, job_id: int) -> None:
        with SessionLocal() as session:
            job = session.get(TranscriptionJob, job_id)
            if job is None or not job.remote_job_id:
                self._mark_failed(job_id, "Running job has no remote job ID")
                return
            api_key = get_api_key(session)
            base_url = get_setting(session, "speechmatics_base_url", settings.speechmatics_base_url) or settings.speechmatics_base_url
            remote_job_id = job.remote_job_id
            if not api_key:
                return

        client = SpeechmaticsClient(api_key, base_url)
        try:
            remote = await client.get_job(remote_job_id)
            remote_status = str(remote.get("status") or "")
            if remote_status == "running":
                return
            if remote_status == "done":
                transcript = await client.get_transcript(remote_job_id)
                await self._complete_job(job_id, transcript, remote_job_id=remote_job_id)
                return
            if remote_status in {"rejected", "expired"}:
                errors = remote.get("errors") or remote.get("error") or remote
                self._mark_failed(job_id, f"Speechmatics job {remote_status}: {errors}")
                return
            self.last_error = f"Unknown remote status for {remote_job_id}: {remote_status}"
        except SpeechmaticsError as exc:
            # Polling is idempotent. Keep the job running and try again later.
            with SessionLocal() as session:
                job = session.get(TranscriptionJob, job_id)
                if job is not None:
                    job.error_message = str(exc)
                    session.commit()
        finally:
            await client.close()

    async def _complete_job(
        self,
        job_id: int,
        transcript: dict[str, Any],
        *,
        remote_job_id: str,
    ) -> None:
        raw_path: Path | None = None
        with SessionLocal() as session:
            job = session.get(TranscriptionJob, job_id)
            if job is None:
                return
            if job.kind == JobKind.ENROLLMENT:
                if job.sample is None:
                    raise RuntimeError("Enrollment job is not linked to a sample")
                identifier, _dominant_label = extract_enrollment_identifier(transcript)
                job.sample.speaker_identifier = identifier
                transcription_config = (job.config_json or {}).get("transcription_config", {})
                job.sample.enrollment_model = transcription_config.get("model")
                job.sample.enrollment_language = transcription_config.get("language")
                job.sample.status = JobStatus.COMPLETED
                job.sample.error_message = None
                job.sample.completed_at = utcnow()
            else:
                if job.video is None:
                    raise RuntimeError("Video job is not linked to a video")
                raw_path = settings.storage_dir / "transcripts" / f"video_{job.video.id}_{remote_job_id}.json"
                raw_path.write_text(json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8")
                transcript_text = normalize_video_transcript(session, job.video, transcript)
                job.video.raw_transcript_path = str(raw_path)
                job.video.transcript_text = transcript_text
                job.video.status = JobStatus.COMPLETED
                job.video.error_message = None
                job.video.completed_at = utcnow()

            job.remote_job_id = remote_job_id
            job.status = JobStatus.COMPLETED
            job.error_message = None
            job.completed_at = utcnow()
            session.commit()

            prepared_path = Path(job.prepared_path) if job.prepared_path else None

        if prepared_path and prepared_path.exists() and not settings.keep_prepared_audio:
            # Never delete an original source path. Prepared files live only under storage/prepared.
            try:
                prepared_path.resolve().relative_to((settings.storage_dir / "prepared").resolve())
                prepared_path.unlink(missing_ok=True)
            except ValueError:
                pass

    def _build_job_inputs(
        self,
        session,
        job: TranscriptionJob,
    ) -> tuple[Path, Path, dict[str, Any]]:
        language = get_setting(session, "language", "ru") or "ru"
        model = get_setting(session, "model", "enhanced") or "enhanced"

        if job.kind == JobKind.ENROLLMENT:
            sample = session.get(SpeakerSample, job.sample_id)
            if sample is None:
                raise RuntimeError("Enrollment sample no longer exists")
            source_path = Path(sample.stored_path)
            destination = settings.storage_dir / "prepared" / f"sample_{sample.id}.flac"
            config = {
                "type": "transcription",
                "transcription_config": {
                    "language": language,
                    "model": model,
                    "diarization": "speaker",
                    "speaker_diarization_config": {
                        "get_speakers": True,
                        "prefer_current_speaker": True,
                    },
                },
                "tracking": {
                    "title": f"Enroll {sample.profile.name}",
                    "reference": f"speaker-sample:{sample.id}",
                    "tags": ["speaker-enrollment"],
                },
            }
            return source_path, destination, config

        video = session.get(Video, job.video_id)
        if video is None:
            raise RuntimeError("Video source no longer exists")
        source_path = Path(video.source_path)
        # Very long, noisy streams can produce FLAC files hundreds of megabytes
        # large and repeatedly lose the direct multipart upload. Speechmatics
        # supports MP3 input, so use a compact speech-quality transport copy for
        # videos of two hours or longer. The archival WAV is never modified.
        prepared_suffix = ".mp3" if (video.duration_seconds or 0) >= 2 * 3600 else ".flac"
        destination = settings.storage_dir / "prepared" / f"video_{video.id}{prepared_suffix}"
        config = self._build_video_config(session, video)
        return source_path, destination, config

    def _build_video_config(self, session, video: Video) -> dict[str, Any]:
        profiles = session.scalars(
            select(SpeakerProfile)
            .where(SpeakerProfile.active.is_(True))
            .options(
                selectinload(SpeakerProfile.review),
                selectinload(SpeakerProfile.samples).selectinload(SpeakerSample.audit),
            )
            .order_by(SpeakerProfile.id.asc())
        ).all()

        identifiers_by_profile: list[tuple[SpeakerProfile, list[str]]] = []
        for profile in profiles:
            if not profile.review or profile.review.manual_status != "approved":
                continue
            identifiers = [
                sample.speaker_identifier
                for sample in sorted(profile.samples, key=lambda item: item.created_at)
                if (
                    sample.status == JobStatus.COMPLETED
                    and sample.speaker_identifier
                    and sample.enrollment_model == video.model
                    and sample.enrollment_language == video.language
                    and sample.audit
                    and sample.audit.manual_status == "approved"
                    and sample.audit.selected_for_enrollment
                )
            ]
            if identifiers:
                identifiers_by_profile.append((profile, identifiers))

        # Fair round-robin selection guarantees that every approved character
        # gets one voice ID before a second or third ID consumes the API limit.
        selected_by_profile: dict[int, list[str]] = {
            profile.id: [] for profile, _identifiers in identifiers_by_profile
        }
        identifier_count = 0
        round_index = 0
        while identifier_count < 50:
            added = False
            for profile, identifiers in identifiers_by_profile:
                if round_index >= len(identifiers) or identifier_count >= 50:
                    continue
                selected_by_profile[profile.id].append(identifiers[round_index])
                identifier_count += 1
                added = True
            if not added:
                break
            round_index += 1

        speakers = [
            {
                "label": profile.api_label,
                "speaker_identifiers": selected_by_profile[profile.id],
            }
            for profile, _identifiers in identifiers_by_profile
            if selected_by_profile[profile.id]
        ]

        diarization_config: dict[str, Any] = {}
        if speakers:
            diarization_config["speakers"] = speakers

        clustering_sensitivity = get_setting(session, "speaker_sensitivity")
        if clustering_sensitivity not in (None, ""):
            diarization_config["speaker_sensitivity"] = float(clustering_sensitivity)

        vocab_text = get_setting(session, "additional_vocab", "") or ""
        vocab = []
        for line in vocab_text.splitlines():
            term = line.strip()
            if term and len(term.split()) <= 6:
                vocab.append({"content": term})
            if len(vocab) >= 1000:
                break

        transcription_config: dict[str, Any] = {
            "language": video.language,
            "model": video.model,
            "diarization": "speaker",
            "speaker_diarization_config": diarization_config,
        }
        if vocab:
            transcription_config["additional_vocab"] = vocab

        return {
            "type": "transcription",
            "transcription_config": transcription_config,
            "tracking": {
                "title": video.title,
                "reference": f"video:{video.id}",
                "tags": ["speaker-archive", "russian"],
                "details": {
                    "local_video_id": video.id,
                    "source_filename": video.original_filename,
                },
            },
        }

    def _sync_target_status(
        self,
        job: TranscriptionJob,
        status: JobStatus,
        error_message: str | None = None,
    ) -> None:
        job.error_message = error_message
        if job.video is not None:
            job.video.status = status
            job.video.error_message = error_message
        if job.sample is not None:
            job.sample.status = status
            job.sample.error_message = error_message

    def _mark_failed(self, job_id: int, message: str) -> None:
        with SessionLocal() as session:
            job = session.get(TranscriptionJob, job_id)
            if job is None:
                return
            job.status = JobStatus.FAILED
            job.error_message = message
            job.completed_at = utcnow()
            self._sync_target_status(job, JobStatus.FAILED, message)
            session.commit()


worker = ArchiveWorker()
