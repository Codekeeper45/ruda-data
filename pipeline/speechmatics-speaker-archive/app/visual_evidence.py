from __future__ import annotations

import base64
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .enrichment import AUTO_CONFIDENCE, _role_for_name
from .models import (
    EnrichmentEvidence,
    MafiaRound,
    MafiaRoundParticipant,
    SpeakerProfile,
    Video,
)
from .openrouter import OpenRouterClient, OpenRouterError


YOUTUBE_ID_RE = re.compile(r"\[([A-Za-z0-9_-]{11})\]\s*$")

VISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "participant_name",
                    "actual_role",
                    "faction",
                    "frame_number",
                    "visible_text",
                    "confidence",
                ],
                "properties": {
                    "participant_name": {"type": "string"},
                    "actual_role": {
                        "type": "string",
                        "enum": [
                            "Мирный",
                            "Мафия",
                            "Дон мафии",
                            "Шериф",
                            "Доктор",
                            "Нейтральный",
                        ],
                    },
                    "faction": {
                        "type": "string",
                        "enum": ["civilians", "mafia", "neutral", "unknown"],
                    },
                    "frame_number": {"type": "integer", "minimum": 1},
                    "visible_text": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                },
            },
        }
    },
}


def youtube_id(video: Video) -> str | None:
    match = YOUTUBE_ID_RE.search(video.title) or YOUTUBE_ID_RE.search(
        video.original_filename
    )
    return match.group(1) if match else None


def _video_url(video_id: str) -> str:
    executable = shutil.which("yt-dlp")
    if not executable:
        raise RuntimeError("yt-dlp не найден")
    command = [
        executable,
        "--no-playlist",
        "-f",
        "bestvideo[height<=480]/worstvideo",
        "-g",
        f"https://www.youtube.com/watch?v={video_id}",
    ]
    result = subprocess.run(
        command, capture_output=True, text=True, check=True, timeout=90
    )
    url = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not url:
        raise RuntimeError("yt-dlp не вернул адрес видеопотока")
    return url


def extract_frames(
    video: Video,
    timestamps: list[float],
    *,
    force: bool = False,
) -> list[Path]:
    video_id = youtube_id(video)
    if not video_id:
        return []
    system_ffmpeg = Path("/usr/bin/ffmpeg")
    ffmpeg = str(system_ffmpeg) if system_ffmpeg.exists() else shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg не найден")
    destination = settings.storage_dir / "enrichment" / "frames" / f"video_{video.id}"
    destination.mkdir(parents=True, exist_ok=True)
    unique_times = sorted({max(0.0, round(value, 1)) for value in timestamps})[:16]
    missing = [
        value
        for value in unique_times
        if force or not (destination / f"{value:010.1f}.jpg").exists()
    ]
    stream_url = _video_url(video_id) if missing else ""
    frames: list[Path] = []
    for timestamp in unique_times:
        output = destination / f"{timestamp:010.1f}.jpg"
        if output.exists() and not force:
            frames.append(output)
            continue
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-ss",
            f"{timestamp:.3f}",
            "-i",
            stream_url,
            "-frames:v",
            "1",
            "-vf",
            "scale='min(1280,iw)':-2",
            "-q:v",
            "3",
            "-y",
            str(output),
        ]
        try:
            subprocess.run(command, capture_output=True, check=True, timeout=90)
        except (subprocess.SubprocessError, OSError):
            output.unlink(missing_ok=True)
            continue
        if output.exists() and output.stat().st_size > 0:
            frames.append(output)
    return frames


def _frame_data(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def fill_missing_roles_from_frames(
    session: Session,
    client: OpenRouterClient,
    video: Video,
    *,
    force_frames: bool = False,
) -> int:
    rounds = session.scalars(
        select(MafiaRound)
        .where(MafiaRound.video_id == video.id)
        .order_by(MafiaRound.round_number)
    ).all()
    updated = 0
    profiles = {
        row.name.casefold(): row for row in session.scalars(select(SpeakerProfile)).all()
    }
    for round_row in rounds:
        all_participants = session.scalars(
            select(MafiaRoundParticipant).where(
                MafiaRoundParticipant.round_id == round_row.id
            )
        ).all()
        if round_row.winning_faction not in {"unknown", "draw"}:
            for participant in all_participants:
                if participant.faction != "unknown":
                    participant.outcome = (
                        "won"
                        if participant.faction == round_row.winning_faction
                        else "lost"
                    )
        missing = session.scalars(
            select(MafiaRoundParticipant).where(
                MafiaRoundParticipant.round_id == round_row.id,
                MafiaRoundParticipant.role_id.is_(None),
            )
        ).all()
        if not missing:
            session.commit()
            continue
        span = max(0.0, round_row.end_time - round_row.start_time)
        timestamps = [
            round_row.start_time,
            round_row.start_time + min(5, span),
            round_row.start_time + min(15, span),
            max(round_row.start_time, round_row.end_time - 5),
        ]
        frames = extract_frames(video, timestamps, force=force_frames)
        if not frames:
            continue
        content: list[dict[str, Any]] = [
            {
                "type": "text",
                "text": (
                    f"Видео: {video.title}; раунд {round_row.round_number}. "
                    f"Неизвестны роли: {', '.join(row.display_name for row in missing)}. "
                    "Найди только явно видимые на экране фактические роли. "
                    "Не используй игровые заявления персонажей. Нумерация кадров с 1."
                ),
            }
        ]
        for frame in frames:
            content.append(
                {"type": "image_url", "image_url": {"url": _frame_data(frame)}}
            )
        free_result_data: Any = {}
        try:
            result = client.structured_chat(
                model=settings.enrichment_vision_model,
                system=(
                    "Ты проверяешь кадры игры в мафию. Без рассуждений верни короткий JSON. "
                    "Фиксируй роль только если имя и роль однозначно видны рядом."
                ),
                user=content,
                schema_name="mafia_visual_roles",
                schema=VISION_SCHEMA,
                reasoning_effort="low",
                max_tokens=3500,
            )
            free_result_data = result.data
        except OpenRouterError:
            free_result_data = {}
        missing_by_name = {row.display_name.casefold(): row for row in missing}

        def apply_findings(data: Any, source_model: str) -> int:
            findings = data.get("findings", []) if isinstance(data, dict) else data
            if not isinstance(findings, list):
                return 0
            applied = 0
            for finding in findings:
                if not isinstance(finding, dict):
                    continue
                name = str(finding.get("participant_name") or "").casefold()
                participant = missing_by_name.get(name)
                confidence = float(finding.get("confidence") or 0)
                frame_number = int(finding.get("frame_number") or 0)
                if (
                    participant is None
                    or participant.role_id is not None
                    or confidence < AUTO_CONFIDENCE
                    or not 1 <= frame_number <= len(frames)
                ):
                    continue
                role_name = str(finding.get("actual_role") or "").strip()
                if not role_name:
                    continue
                faction = str(finding.get("faction") or "unknown")
                role = _role_for_name(session, role_name, faction)
                if role is None:
                    continue
                participant.role_id = role.id
                participant.faction = role.faction
                participant.role_confidence = confidence
                participant.review_status = "auto_verified"
                profile = profiles.get(name)
                if profile and participant.profile_id is None:
                    participant.profile_id = profile.id
                frame = frames[frame_number - 1]
                session.add(
                    EnrichmentEvidence(
                        entity_type="participant",
                        entity_id=participant.id,
                        field_name="actual_role",
                        start_time=float(frame.stem),
                        end_time=float(frame.stem),
                        source_type="frame",
                        source_ref=str(frame),
                        excerpt=(
                            f"{finding.get('visible_text') or ''} [{source_model}]"
                        )[:2000],
                        confidence=confidence,
                    )
                )
                applied += 1
            session.flush()
            return applied

        round_updated = apply_findings(
            free_result_data, settings.enrichment_vision_model
        )
        remaining = [
            row.display_name for row in missing if row.role_id is None
        ]
        if remaining:
            escalation_content = list(content)
            escalation_content[0] = {
                "type": "text",
                "text": (
                    f"На этих кадрах остались неподтверждённые участники: "
                    f"{', '.join(remaining)}. Выполни точный арбитраж. "
                    "Верни роль только если имя и роль явно видны рядом на кадре."
                ),
            }
            escalation = client.structured_chat(
                model=settings.enrichment_escalation_model,
                system=(
                    "Ты арбитр визуальных доказательств игры в мафию. "
                    "Не угадывай и не используй заявления игроков."
                ),
                user=escalation_content,
                schema_name="mafia_visual_roles_escalation",
                schema=VISION_SCHEMA,
                reasoning_effort="medium",
                max_tokens=5000,
            )
            round_updated += apply_findings(
                escalation.data, settings.enrichment_escalation_model
            )
        updated += round_updated
        if round_row.winning_faction not in {"unknown", "draw"}:
            for participant in missing:
                if participant.faction != "unknown":
                    participant.outcome = (
                        "won"
                        if participant.faction == round_row.winning_faction
                        else "lost"
                    )
        session.commit()
    return updated
