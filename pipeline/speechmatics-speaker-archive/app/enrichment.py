from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, joinedload

from .config import settings
from .models import (
    EnrichmentEvidence,
    EnrichmentRun,
    MafiaEvent,
    MafiaPhase,
    MafiaRole,
    MafiaRound,
    MafiaRoundParticipant,
    SpeakerProfile,
    Utterance,
    Video,
    VideoEnrichment,
    VideoSpeaker,
    utcnow,
)
from .openrouter import OpenRouterClient, OpenRouterResult


PIPELINE_VERSION = settings.enrichment_pipeline_version
CONTENT_TYPES = {"talk_only", "mixed", "mafia_only", "unknown"}
FACTIONS = {"civilians", "mafia", "neutral", "draw", "unknown"}
REVIEW_CONFIDENCE = 0.75
AUTO_CONFIDENCE = 0.90
BOUNDARY_UTTERANCE_TOLERANCE = 12


ROLE_SEEDS = (
    ("civilian", "Мирный", "civilians", ["мирный", "мирная", "мирный житель"]),
    ("mafia", "Мафия", "mafia", ["мафия", "мафиози", "маф"]),
    ("don", "Дон мафии", "mafia", ["дон", "дон мафии"]),
    ("sheriff", "Шериф", "civilians", ["шериф", "комиссар"]),
    ("doctor", "Доктор", "civilians", ["доктор", "врач"]),
    ("neutral", "Нейтральный", "neutral", ["нейтральный", "одиночка"]),
)


def seed_mafia_roles(session: Session) -> None:
    allowed_codes = {item[0] for item in ROLE_SEEDS}
    existing = {row.code for row in session.scalars(select(MafiaRole)).all()}
    for code, name, faction, aliases in ROLE_SEEDS:
        if code not in existing:
            session.add(
                MafiaRole(code=code, name=name, faction=faction, aliases=aliases)
            )
    session.flush()
    invalid_roles = list(
        session.scalars(select(MafiaRole).where(MafiaRole.code.not_in(allowed_codes))).all()
    )
    for role in invalid_roles:
        participants = list(
            session.scalars(
                select(MafiaRoundParticipant).where(
                    MafiaRoundParticipant.role_id == role.id
                )
            ).all()
        )
        for participant in participants:
            session.execute(
                delete(EnrichmentEvidence).where(
                    EnrichmentEvidence.entity_type == "participant",
                    EnrichmentEvidence.entity_id == participant.id,
                    EnrichmentEvidence.field_name == "actual_role",
                )
            )
            participant.role_id = None
            participant.faction = "unknown"
            participant.outcome = "unknown"
            participant.role_confidence = min(
                float(participant.role_confidence or 0.0), 0.5
            )
            participant.review_status = "needs_review"
            marker = "Отклонена роль вне контролируемого словаря."
            participant.notes = (
                f"{participant.notes}\n{marker}" if participant.notes else marker
            )
        session.delete(role)


def transcript_hash(video: Video, utterances: list[Utterance]) -> str:
    digest = hashlib.sha256()
    digest.update(video.source_signature.encode("utf-8"))
    for row in utterances:
        digest.update(
            f"{row.id}|{row.speaker_id}|{row.start_time:.3f}|{row.end_time:.3f}|{row.text}\n".encode(
                "utf-8"
            )
        )
    return digest.hexdigest()


def _format_time(value: float) -> str:
    seconds = max(0, int(value))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def build_transcript_payload(utterances: list[Utterance]) -> str:
    lines = []
    for row in utterances:
        lines.append(
            f"U{row.id} [{_format_time(row.start_time)}-{_format_time(row.end_time)}] "
            f"{row.speaker.display_name}: {row.text}"
        )
    return "\n".join(lines)


def has_strong_mafia_cues(video: Video, utterances: list[Utterance]) -> bool:
    title = video.title.casefold()
    if "мафия" in title or "мафии" in title:
        return True
    transcript = " ".join(row.text.casefold() for row in utterances)
    cues = (
        "город засыпает",
        "просыпаются мафи",
        "победа маф",
        "победили мирн",
        "казнили",
        "голосование",
        "шериф провер",
    )
    return sum(cue in transcript for cue in cues) >= 3


EVIDENCE_SCHEMA: dict[str, Any] = {
    "type": "array",
    "items": {"type": "integer"},
}

PARTICIPANT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "name",
        "actual_role",
        "faction",
        "outcome",
        "confidence",
        "role_confidence",
        "participant_evidence",
        "role_evidence",
        "notes",
    ],
    "properties": {
        "name": {"type": "string"},
        "actual_role": {"type": ["string", "null"]},
        "faction": {
            "type": "string",
            "enum": ["civilians", "mafia", "neutral", "unknown"],
        },
        "outcome": {"type": "string", "enum": ["won", "lost", "unknown"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "role_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "participant_evidence": EVIDENCE_SCHEMA,
        "role_evidence": EVIDENCE_SCHEMA,
        "notes": {"type": "string"},
    },
}

PHASE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "phase_type",
        "phase_number",
        "start_utterance_id",
        "end_utterance_id",
        "is_partial",
        "confidence",
        "evidence",
    ],
    "properties": {
        "phase_type": {
            "type": "string",
            "enum": [
                "introduction",
                "day",
                "voting",
                "last_words",
                "night",
                "result",
                "intermission",
                "unknown",
            ],
        },
        "phase_number": {"type": ["integer", "null"], "minimum": 1},
        "start_utterance_id": {"type": "integer"},
        "end_utterance_id": {"type": "integer"},
        "is_partial": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": EVIDENCE_SCHEMA,
    },
}

EVENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "event_type",
        "actor",
        "target",
        "utterance_id",
        "summary",
        "confidence",
        "evidence",
    ],
    "properties": {
        "event_type": {
            "type": "string",
            "enum": [
                "game_start",
                "game_end",
                "role_reveal",
                "vote_out",
                "night_kill",
                "sheriff_check",
                "don_check",
                "save",
                "winner_announcement",
                "other",
            ],
        },
        "actor": {"type": ["string", "null"]},
        "target": {"type": ["string", "null"]},
        "utterance_id": {"type": "integer"},
        "summary": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": EVIDENCE_SCHEMA,
    },
}

ROUND_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "round_number",
        "start_utterance_id",
        "end_utterance_id",
        "is_partial",
        "winning_faction",
        "winner_summary",
        "confidence",
        "winner_evidence",
        "participants",
        "phases",
        "events",
    ],
    "properties": {
        "round_number": {"type": "integer", "minimum": 1},
        "start_utterance_id": {"type": "integer"},
        "end_utterance_id": {"type": "integer"},
        "is_partial": {"type": "boolean"},
        "winning_faction": {
            "type": "string",
            "enum": ["civilians", "mafia", "neutral", "draw", "unknown"],
        },
        "winner_summary": {"type": "string"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "winner_evidence": EVIDENCE_SCHEMA,
        "participants": {"type": "array", "items": PARTICIPANT_SCHEMA},
        "phases": {"type": "array", "items": PHASE_SCHEMA},
        "events": {"type": "array", "items": EVENT_SCHEMA},
    },
}

ENRICHMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["content_type", "has_mafia", "confidence", "summary", "rounds"],
    "properties": {
        "content_type": {
            "type": "string",
            "enum": ["talk_only", "mixed", "mafia_only", "unknown"],
        },
        "has_mafia": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "summary": {"type": "string"},
        "rounds": {"type": "array", "items": ROUND_SCHEMA},
    },
}


SYSTEM_PROMPT = """\
Ты анализируешь русскую расшифровку шоу с играми в мафию. Возвращай только данные,
которые подтверждаются строками U<id>. Одна mafia_round — одна целая игра от запуска
до объявления результата, а не игровой день.

Критические правила:
1. Не считай ведущего Тостера участником без явного доказательства, что он играет.
2. Игроки могут лгать. Фраза игрока «я шериф/мирный» является заявлением, но НЕ
   доказательством actual_role. Фактическую роль устанавливай только по словам
   ведущего, посмертному/финальному раскрытию или однозначному системному объявлению.
3. Если фактической роли нет в надёжных доказательствах, actual_role=null,
   faction=unknown и role_confidence не выше 0.5.
4. Ночь — только реально начавшаяся игровая фаза. Обсуждение слова «ночь» не считается.
5. Указывай точные существующие utterance id без буквы U.
6. Если запись начинается или заканчивается внутри игры, is_partial=true.
7. Не выдумывай участников, роли, победителя и границы.
8. content_type: talk_only — без игры; mafia_only — почти всё видео игра;
   mixed — разговор/донаты/другая передача плюс минимум одна игра.
"""


def normalize_enrichment_data(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            return normalize_enrichment_data(json.loads(value))
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            for index, character in enumerate(value):
                if character not in "[{":
                    continue
                try:
                    decoded, _end = decoder.raw_decode(value[index:])
                    return normalize_enrichment_data(decoded)
                except (json.JSONDecodeError, ValueError):
                    continue
            raise ValueError("Модель вернула строку без корректного JSON")
    if isinstance(value, dict):
        for key in ("result", "output", "data", "enrichment"):
            nested = value.get(key)
            if isinstance(nested, (dict, list)):
                return normalize_enrichment_data(nested)
        return value
    if isinstance(value, list):
        if len(value) == 1:
            return normalize_enrichment_data(value[0])
        text_parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                for key in ("text", "content", "output_text"):
                    part = item.get(key)
                    if isinstance(part, str):
                        text_parts.append(part)
                        break
        if text_parts:
            try:
                return normalize_enrichment_data("\n".join(text_parts))
            except ValueError:
                pass
        decoded: list[Any] = []
        changed = False
        for item in value:
            if isinstance(item, str):
                try:
                    decoded.append(json.loads(item))
                    changed = True
                    continue
                except json.JSONDecodeError:
                    pass
            decoded.append(item)
        if changed:
            return normalize_enrichment_data(decoded)
        flattened: list[Any] = []
        for item in value:
            flattened.extend(item if isinstance(item, list) else [item])
        if flattened != value:
            return normalize_enrichment_data(flattened)
        dictionaries = [item for item in value if isinstance(item, dict)]
        merged: dict[str, Any] = {}
        for item in dictionaries:
            merged.update(item)
        if "content_type" in merged or "rounds" in merged or "has_mafia" in merged:
            merged.setdefault("content_type", "unknown")
            merged.setdefault("has_mafia", bool(merged.get("rounds")))
            merged.setdefault("confidence", 0.5)
            merged.setdefault("summary", "")
            merged.setdefault("rounds", [])
            return merged
        rounds = [item for item in value if isinstance(item, dict)]
        if rounds and all(
            "round_number" in item or "start_utterance_id" in item for item in rounds
        ):
            return {
                "content_type": "mafia_only",
                "has_mafia": True,
                "confidence": min(
                    (_clamp_confidence(item.get("confidence")) for item in rounds),
                    default=0.5,
                ),
                "summary": "Модель вернула список раундов без верхнего объекта",
                "rounds": rounds,
            }
    raise ValueError(
        f"Модель вернула неверный верхний формат: {type(value).__name__}"
    )


def _review_status(confidence: float | None) -> str:
    value = float(confidence or 0.0)
    if value >= AUTO_CONFIDENCE:
        return "auto_verified"
    if value >= REVIEW_CONFIDENCE:
        return "needs_review"
    return "unknown"


def _clamp_confidence(value: Any) -> float:
    try:
        return min(1.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _role_for_name(
    session: Session, role_name: str, faction: str
) -> MafiaRole | None:
    """Resolve only roles from the controlled Mafia dictionary.

    Model output is untrusted here. In particular, a character name visible on
    a game frame must never silently become a new Mafia role.
    """
    normalized = role_name.strip().casefold()
    roles = session.scalars(select(MafiaRole)).all()
    allowed_codes = {item[0] for item in ROLE_SEEDS}
    for role in roles:
        if role.code not in allowed_codes:
            continue
        aliases = [str(item).casefold() for item in (role.aliases or [])]
        if normalized in {role.name.casefold(), role.code.casefold(), *aliases}:
            return role
    return None


def _valid_evidence_ids(value: Any, utterance_map: dict[int, Utterance]) -> list[int]:
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        try:
            item_id = int(item)
        except (TypeError, ValueError):
            continue
        if item_id in utterance_map and item_id not in result:
            result.append(item_id)
    return result


def _add_evidence(
    session: Session,
    *,
    entity_type: str,
    entity_id: int,
    field_name: str,
    evidence_ids: Any,
    utterance_map: dict[int, Utterance],
    confidence: float,
) -> None:
    for utterance_id in _valid_evidence_ids(evidence_ids, utterance_map):
        row = utterance_map[utterance_id]
        session.add(
            EnrichmentEvidence(
                entity_type=entity_type,
                entity_id=entity_id,
                field_name=field_name,
                utterance_id=row.id,
                start_time=row.start_time,
                end_time=row.end_time,
                source_type="transcript",
                source_ref=f"U{row.id}",
                excerpt=row.text[:2000],
                confidence=confidence,
            )
        )


def _resolve_utterance(
    value: Any,
    utterance_map: dict[int, Utterance],
    *,
    fallback: Utterance,
) -> Utterance:
    try:
        row = utterance_map.get(int(value))
    except (TypeError, ValueError):
        row = None
    return row or fallback


def _remove_auto_rounds(session: Session, video_id: int) -> None:
    rounds = session.scalars(
        select(MafiaRound).where(
            MafiaRound.video_id == video_id,
            MafiaRound.review_status != "confirmed",
        )
    ).all()
    for round_row in rounds:
        for entity_type, model in (
            ("round", MafiaRound),
            ("participant", MafiaRoundParticipant),
            ("phase", MafiaPhase),
            ("event", MafiaEvent),
        ):
            if model is MafiaRound:
                ids = [round_row.id]
            else:
                ids = list(
                    session.scalars(
                        select(model.id).where(model.round_id == round_row.id)
                    ).all()
                )
            if ids:
                session.execute(
                    delete(EnrichmentEvidence).where(
                        EnrichmentEvidence.entity_type == entity_type,
                        EnrichmentEvidence.entity_id.in_(ids),
                    )
                )
        session.delete(round_row)
    session.flush()


def persist_enrichment_result(
    session: Session,
    *,
    video: Video,
    utterances: list[Utterance],
    source_hash: str,
    result: dict[str, Any],
    model: str,
    force: bool = False,
) -> VideoEnrichment:
    utterance_map = {row.id: row for row in utterances}
    if not utterances:
        raise ValueError("У видео нет реплик")
    enrichment = session.get(VideoEnrichment, video.id)
    if enrichment is not None and enrichment.review_status == "confirmed" and not force:
        return enrichment
    if enrichment is None:
        enrichment = VideoEnrichment(video_id=video.id)
        session.add(enrichment)
    content_type = str(result.get("content_type") or "unknown")
    enrichment.content_type = content_type if content_type in CONTENT_TYPES else "unknown"
    enrichment.has_mafia = bool(result.get("has_mafia"))
    enrichment.confidence = _clamp_confidence(result.get("confidence"))
    enrichment.status = "completed"
    enrichment.review_status = _review_status(enrichment.confidence)
    enrichment.extractor_model = model
    enrichment.extractor_version = PIPELINE_VERSION
    enrichment.source_hash = source_hash
    enrichment.error_message = None
    enrichment.raw_result = result
    enrichment.completed_at = utcnow()
    session.flush()

    _remove_auto_rounds(session, video.id)
    speaker_rows = session.scalars(
        select(VideoSpeaker)
        .where(VideoSpeaker.video_id == video.id)
        .options(joinedload(VideoSpeaker.profile))
    ).all()
    profiles = session.scalars(select(SpeakerProfile)).all()
    profiles_by_name = {row.name.casefold(): row for row in profiles}
    speakers_by_name: dict[str, VideoSpeaker] = {}
    for row in speaker_rows:
        speakers_by_name[row.display_name.casefold()] = row
        speakers_by_name[row.label.casefold()] = row
        if row.profile:
            speakers_by_name[row.profile.name.casefold()] = row

    round_payloads = result.get("rounds") if isinstance(result.get("rounds"), list) else []
    for fallback_number, payload in enumerate(round_payloads, start=1):
        if not isinstance(payload, dict):
            continue
        start_utt = _resolve_utterance(
            payload.get("start_utterance_id"), utterance_map, fallback=utterances[0]
        )
        end_utt = _resolve_utterance(
            payload.get("end_utterance_id"), utterance_map, fallback=utterances[-1]
        )
        if end_utt.end_time <= start_utt.start_time:
            start_utt, end_utt = min(
                (start_utt, end_utt), key=lambda row: row.start_time
            ), max((start_utt, end_utt), key=lambda row: row.end_time)
        confidence = _clamp_confidence(payload.get("confidence"))
        round_number = int(payload.get("round_number") or fallback_number)
        while session.scalar(
            select(func.count()).select_from(MafiaRound).where(
                MafiaRound.video_id == video.id,
                MafiaRound.round_number == round_number,
            )
        ):
            round_number += 1
        faction = str(payload.get("winning_faction") or "unknown")
        winner_evidence = _valid_evidence_ids(
            payload.get("winner_evidence"), utterance_map
        )
        if faction != "unknown" and not winner_evidence:
            faction = "unknown"
            confidence = min(confidence, 0.5)
        round_row = MafiaRound(
            video_id=video.id,
            round_number=round_number,
            start_time=max(0.0, start_utt.start_time),
            end_time=min(video.duration_seconds or end_utt.end_time, end_utt.end_time),
            start_utterance_id=start_utt.id,
            end_utterance_id=end_utt.id,
            is_partial=bool(payload.get("is_partial")),
            winning_faction=faction if faction in FACTIONS else "unknown",
            winner_summary=str(payload.get("winner_summary") or "")[:4000] or None,
            confidence=confidence,
            review_status=_review_status(confidence),
            extractor_version=PIPELINE_VERSION,
        )
        session.add(round_row)
        session.flush()
        _add_evidence(
            session,
            entity_type="round",
            entity_id=round_row.id,
            field_name="winner",
            evidence_ids=winner_evidence,
            utterance_map=utterance_map,
            confidence=confidence,
        )

        participants_by_name: dict[str, MafiaRoundParticipant] = {}
        for participant_payload in payload.get("participants") or []:
            if not isinstance(participant_payload, dict):
                continue
            name = str(participant_payload.get("name") or "").strip()
            if not name:
                continue
            speaker = speakers_by_name.get(name.casefold())
            profile = profiles_by_name.get(name.casefold()) or (
                speaker.profile if speaker and speaker.profile else None
            )
            role_evidence = _valid_evidence_ids(
                participant_payload.get("role_evidence"), utterance_map
            )
            actual_role = participant_payload.get("actual_role")
            role_confidence = _clamp_confidence(
                participant_payload.get("role_confidence")
            )
            faction = str(participant_payload.get("faction") or "unknown")
            role = None
            # A claimed role without traceable evidence is never persisted as actual.
            if actual_role and role_evidence:
                role = _role_for_name(
                    session,
                    str(actual_role),
                    faction if faction in FACTIONS else "unknown",
                )
            if role is None:
                actual_role = None
                faction = "unknown"
                role_confidence = min(role_confidence, 0.5)
            participant_confidence = _clamp_confidence(
                participant_payload.get("confidence")
            )
            participant = MafiaRoundParticipant(
                round_id=round_row.id,
                profile_id=profile.id if profile else None,
                video_speaker_id=speaker.id if speaker else None,
                display_name=profile.name if profile else name[:200],
                role_id=role.id if role else None,
                faction=faction if faction in FACTIONS else "unknown",
                outcome=str(participant_payload.get("outcome") or "unknown"),
                confidence=participant_confidence,
                role_confidence=role_confidence,
                review_status=_review_status(
                    min(participant_confidence, role_confidence)
                    if role
                    else participant_confidence
                ),
                notes=str(participant_payload.get("notes") or "")[:4000] or None,
            )
            session.add(participant)
            session.flush()
            participants_by_name[name.casefold()] = participant
            participants_by_name[participant.display_name.casefold()] = participant
            _add_evidence(
                session,
                entity_type="participant",
                entity_id=participant.id,
                field_name="participation",
                evidence_ids=participant_payload.get("participant_evidence"),
                utterance_map=utterance_map,
                confidence=participant_confidence,
            )
            _add_evidence(
                session,
                entity_type="participant",
                entity_id=participant.id,
                field_name="actual_role",
                evidence_ids=role_evidence,
                utterance_map=utterance_map,
                confidence=role_confidence,
            )

        phases: list[MafiaPhase] = []
        for phase_payload in payload.get("phases") or []:
            if not isinstance(phase_payload, dict):
                continue
            phase_start = _resolve_utterance(
                phase_payload.get("start_utterance_id"),
                utterance_map,
                fallback=start_utt,
            )
            phase_end = _resolve_utterance(
                phase_payload.get("end_utterance_id"),
                utterance_map,
                fallback=phase_start,
            )
            phase_confidence = _clamp_confidence(phase_payload.get("confidence"))
            phase = MafiaPhase(
                round_id=round_row.id,
                phase_type=str(phase_payload.get("phase_type") or "unknown"),
                phase_number=phase_payload.get("phase_number"),
                start_time=max(round_row.start_time, phase_start.start_time),
                end_time=min(
                    round_row.end_time, max(phase_start.end_time, phase_end.end_time)
                ),
                is_partial=bool(phase_payload.get("is_partial")),
                confidence=phase_confidence,
                review_status=_review_status(phase_confidence),
            )
            session.add(phase)
            session.flush()
            phases.append(phase)
            _add_evidence(
                session,
                entity_type="phase",
                entity_id=phase.id,
                field_name="boundary",
                evidence_ids=phase_payload.get("evidence"),
                utterance_map=utterance_map,
                confidence=phase_confidence,
            )

        for event_payload in payload.get("events") or []:
            if not isinstance(event_payload, dict):
                continue
            event_utt = _resolve_utterance(
                event_payload.get("utterance_id"),
                utterance_map,
                fallback=start_utt,
            )
            event_confidence = _clamp_confidence(event_payload.get("confidence"))
            actor_name = str(event_payload.get("actor") or "").casefold()
            target_name = str(event_payload.get("target") or "").casefold()
            phase = next(
                (
                    item
                    for item in phases
                    if item.start_time <= event_utt.start_time <= item.end_time
                ),
                None,
            )
            event = MafiaEvent(
                round_id=round_row.id,
                phase_id=phase.id if phase else None,
                event_type=str(event_payload.get("event_type") or "other"),
                actor_participant_id=(
                    participants_by_name[actor_name].id
                    if actor_name in participants_by_name
                    else None
                ),
                target_participant_id=(
                    participants_by_name[target_name].id
                    if target_name in participants_by_name
                    else None
                ),
                start_time=event_utt.start_time,
                end_time=event_utt.end_time,
                summary=str(event_payload.get("summary") or "")[:4000],
                confidence=event_confidence,
                review_status=_review_status(event_confidence),
            )
            session.add(event)
            session.flush()
            _add_evidence(
                session,
                entity_type="event",
                entity_id=event.id,
                field_name="event",
                evidence_ids=event_payload.get("evidence"),
                utterance_map=utterance_map,
                confidence=event_confidence,
            )
    return enrichment


def _create_run(
    session: Session,
    *,
    video_id: int,
    stage: str,
    model: str,
    source_hash: str,
) -> EnrichmentRun:
    run = session.scalar(
        select(EnrichmentRun).where(
            EnrichmentRun.video_id == video_id,
            EnrichmentRun.stage == stage,
            EnrichmentRun.pipeline_version == PIPELINE_VERSION,
            EnrichmentRun.input_hash == source_hash,
        )
    )
    if run is None:
        run = EnrichmentRun(
            video_id=video_id,
            stage=stage,
            status="running",
            model=model,
            pipeline_version=PIPELINE_VERSION,
            input_hash=source_hash,
            attempt_count=1,
            started_at=utcnow(),
        )
        session.add(run)
    else:
        run.status = "running"
        run.attempt_count += 1
        run.started_at = utcnow()
        run.error_message = None
    session.flush()
    return run


def _round_signature(payload: dict[str, Any]) -> tuple[Any, ...]:
    participants = []
    for item in payload.get("participants") or []:
        if not isinstance(item, dict):
            continue
        participants.append(
            (
                str(item.get("name") or "").casefold(),
                str(item.get("actual_role") or "").casefold(),
            )
        )
    return (
        str(payload.get("winning_faction") or "unknown"),
        int(payload.get("start_utterance_id") or 0),
        int(payload.get("end_utterance_id") or 0),
        tuple(sorted(participants)),
    )


def results_conflict(primary: dict[str, Any], verified: dict[str, Any]) -> bool:
    """Escalate only material disagreements, not merely an unknown role."""
    if bool(primary.get("has_mafia")) != bool(verified.get("has_mafia")):
        return True
    if str(primary.get("content_type")) != str(verified.get("content_type")):
        return True
    first_rounds = [row for row in primary.get("rounds") or [] if isinstance(row, dict)]
    second_rounds = [row for row in verified.get("rounds") or [] if isinstance(row, dict)]
    if len(first_rounds) != len(second_rounds):
        return True
    for first, second in zip(first_rounds, second_rounds, strict=True):
        first_signature = _round_signature(first)
        second_signature = _round_signature(second)
        if first_signature[0] != second_signature[0]:
            return True
        # Exact neighbouring utterance IDs are not a material disagreement:
        # models often include or omit a short host lead-in at the same boundary.
        if (
            abs(first_signature[1] - second_signature[1])
            > BOUNDARY_UTTERANCE_TOLERANCE
        ):
            return True
        if (
            abs(first_signature[2] - second_signature[2])
            > BOUNDARY_UTTERANCE_TOLERANCE
        ):
            return True
        first_roles = {name: role for name, role in first_signature[3] if role}
        second_roles = {name: role for name, role in second_signature[3] if role}
        if any(
            name in second_roles and second_roles[name] != role
            for name, role in first_roles.items()
        ):
            return True
        if _clamp_confidence(second.get("confidence")) < REVIEW_CONFIDENCE:
            return True
    return False


def extract_video_enrichment(
    session: Session,
    client: OpenRouterClient,
    video: Video,
    *,
    force: bool = False,
    verify: bool = True,
) -> VideoEnrichment:
    utterances = session.scalars(
        select(Utterance)
        .where(Utterance.video_id == video.id)
        .options(joinedload(Utterance.speaker))
        .order_by(Utterance.sequence)
    ).all()
    if not utterances:
        raise ValueError(f"У видео {video.id} нет реплик")
    source_hash = transcript_hash(video, list(utterances))
    existing = session.get(VideoEnrichment, video.id)
    if (
        existing is not None
        and existing.status == "completed"
        and existing.source_hash == source_hash
        and existing.extractor_version == PIPELINE_VERSION
        and not force
    ):
        return existing

    run = _create_run(
        session,
        video_id=video.id,
        stage="extract",
        model=settings.enrichment_model,
        source_hash=source_hash,
    )
    session.commit()
    speakers = sorted({row.speaker.display_name for row in utterances})
    prompt = (
        f"Видео: {video.title}\n"
        f"Длительность: {_format_time(video.duration_seconds or utterances[-1].end_time)}\n"
        f"Распознанные спикеры: {', '.join(speakers)}\n\n"
        f"Расшифровка:\n{build_transcript_payload(list(utterances))}"
    )
    deterministic_mafia = has_strong_mafia_cues(video, list(utterances))
    try:
        primary = client.structured_chat(
            model=settings.enrichment_model,
            system=SYSTEM_PROMPT,
            user=prompt,
            schema_name="mafia_archive_enrichment",
            schema=ENRICHMENT_SCHEMA,
            reasoning_effort="medium",
        )
        primary.data = normalize_enrichment_data(primary.data)
        final_result = primary
        if verify and (bool(primary.data.get("has_mafia")) or deterministic_mafia):
            verifier_prompt = (
                "Проверь результат первого анализатора по исходной расшифровке. "
                "Исправь пропущенные игры, неверные границы, участников, ночи, роли и "
                "победителя. Особенно отвергай роль, основанную только на заявлении игрока.\n\n"
                f"Первичный результат:\n{json.dumps(primary.data, ensure_ascii=False)}\n\n"
                f"{prompt}"
            )
            verifier_result = client.structured_chat(
                model=settings.enrichment_verifier_model,
                system=SYSTEM_PROMPT,
                user=verifier_prompt,
                schema_name="verified_mafia_archive_enrichment",
                schema=ENRICHMENT_SCHEMA,
                reasoning_effort="high",
            )
            verifier_result.data = normalize_enrichment_data(verifier_result.data)
            final_result = verifier_result
            run.model = (
                f"{settings.enrichment_model} -> "
                f"{settings.enrichment_verifier_model}"
            )
            run.prompt_tokens += verifier_result.prompt_tokens
            run.completion_tokens += verifier_result.completion_tokens
            run.estimated_cost_usd += verifier_result.estimated_cost_usd
            if results_conflict(primary.data, verifier_result.data) or (
                deterministic_mafia and not bool(verifier_result.data.get("has_mafia"))
            ):
                escalation_prompt = (
                    "Два анализатора дали конфликтующие результаты. Проведи финальный "
                    "арбитраж строго по расшифровке. Не заполняй неизвестные роли догадкой.\n\n"
                    f"Ling:\n{json.dumps(primary.data, ensure_ascii=False)}\n\n"
                    f"Nemotron:\n{json.dumps(verifier_result.data, ensure_ascii=False)}\n\n"
                    f"{prompt}"
                )
                final_result = client.structured_chat(
                    model=settings.enrichment_escalation_model,
                    system=SYSTEM_PROMPT,
                    user=escalation_prompt,
                    schema_name="arbitrated_mafia_archive_enrichment",
                    schema=ENRICHMENT_SCHEMA,
                    reasoning_effort="high",
                )
                final_result.data = normalize_enrichment_data(final_result.data)
                run.model = (
                    f"{settings.enrichment_model} -> "
                    f"{settings.enrichment_verifier_model} -> "
                    f"{settings.enrichment_escalation_model}"
                )
                run.prompt_tokens += final_result.prompt_tokens
                run.completion_tokens += final_result.completion_tokens
                run.estimated_cost_usd += final_result.estimated_cost_usd
        run.prompt_tokens += primary.prompt_tokens
        run.completion_tokens += primary.completion_tokens
        run.estimated_cost_usd += primary.estimated_cost_usd
        raw_path = (
            settings.storage_dir
            / "enrichment"
            / "raw"
            / f"video_{video.id}_{source_hash[:12]}.json"
        )
        raw_path.write_text(
            json.dumps(final_result.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        run.raw_output_path = str(raw_path)
        enrichment = persist_enrichment_result(
            session,
            video=video,
            utterances=list(utterances),
            source_hash=source_hash,
            result=final_result.data,
            model=final_result.model,
            force=force,
        )
        run.status = "completed"
        run.completed_at = utcnow()
        session.commit()
        return enrichment
    except Exception as exc:
        session.rollback()
        failed_run = session.get(EnrichmentRun, run.id)
        if failed_run is not None:
            failed_run.status = "failed"
            failed_run.error_message = str(exc)[:4000]
            failed_run.completed_at = utcnow()
        enrichment = session.get(VideoEnrichment, video.id)
        if enrichment is None:
            enrichment = VideoEnrichment(video_id=video.id)
            session.add(enrichment)
        enrichment.status = "failed"
        enrichment.error_message = str(exc)[:4000]
        session.commit()
        raise
