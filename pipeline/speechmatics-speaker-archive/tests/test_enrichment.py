from __future__ import annotations

import copy

from sqlalchemy import create_engine, event, func, select
from sqlalchemy.orm import Session

from app.enrichment import (
    _role_for_name,
    persist_enrichment_result,
    results_conflict,
    seed_mafia_roles,
    transcript_hash,
)
from app.migrations import ensure_database_features
from app.models import (
    Base,
    EmbeddingJob,
    MafiaPhase,
    MafiaRound,
    MafiaRoundParticipant,
    SemanticDocument,
    SpeakerProfile,
    Utterance,
    Video,
    VideoSpeaker,
)
from app.openrouter import _extract_json
from app.semantic_search import build_semantic_documents_for_video


def database() -> tuple[Session, Video, list[Utterance]]:
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    ensure_database_features(engine)
    session = Session(engine)
    profile = SpeakerProfile(name="Клод", api_label="claude")
    session.add(profile)
    session.flush()
    video = Video(
        title="Тестовая мафия [abcdefghijk]",
        original_filename="test.wav",
        source_path="/tmp/test.wav",
        source_signature="signature",
        source_size_bytes=100,
        source_modified_ns=1,
        duration_seconds=120,
        status="completed",
    )
    session.add(video)
    session.flush()
    speaker = VideoSpeaker(
        video_id=video.id,
        label="S1",
        display_name="Клод",
        profile_id=profile.id,
        is_known=True,
    )
    session.add(speaker)
    session.flush()
    utterances = [
        Utterance(
            video_id=video.id,
            speaker_id=speaker.id,
            sequence=1,
            start_time=1,
            end_time=10,
            text="Начинаем игру.",
            word_count=3,
        ),
        Utterance(
            video_id=video.id,
            speaker_id=speaker.id,
            sequence=2,
            start_time=60,
            end_time=70,
            text="Город засыпает, наступает ночь.",
            word_count=5,
        ),
        Utterance(
            video_id=video.id,
            speaker_id=speaker.id,
            sequence=3,
            start_time=110,
            end_time=118,
            text="Победили мирные.",
            word_count=2,
        ),
    ]
    session.add_all(utterances)
    seed_mafia_roles(session)
    session.commit()
    return session, video, utterances


def test_persist_enrichment_requires_role_evidence() -> None:
    session, video, utterances = database()
    result = {
        "content_type": "mafia_only",
        "has_mafia": True,
        "confidence": 0.95,
        "summary": "Одна игра",
        "rounds": [
            {
                "round_number": 1,
                "start_utterance_id": utterances[0].id,
                "end_utterance_id": utterances[2].id,
                "is_partial": False,
                "winning_faction": "civilians",
                "winner_summary": "Победили мирные",
                "confidence": 0.94,
                "winner_evidence": [utterances[2].id],
                "participants": [
                    {
                        "name": "Клод",
                        "actual_role": "Шериф",
                        "faction": "civilians",
                        "outcome": "won",
                        "confidence": 0.9,
                        "role_confidence": 0.9,
                        "participant_evidence": [utterances[0].id],
                        "role_evidence": [],
                        "notes": "Роль была только заявлена игроком",
                    }
                ],
                "phases": [
                    {
                        "phase_type": "night",
                        "phase_number": 1,
                        "start_utterance_id": utterances[1].id,
                        "end_utterance_id": utterances[1].id,
                        "is_partial": False,
                        "confidence": 0.95,
                        "evidence": [utterances[1].id],
                    }
                ],
                "events": [],
            }
        ],
    }
    persist_enrichment_result(
        session,
        video=video,
        utterances=utterances,
        source_hash=transcript_hash(video, utterances),
        result=result,
        model="test",
    )
    session.commit()
    participant = session.scalar(select(MafiaRoundParticipant))
    assert participant is not None
    assert participant.role_id is None
    assert participant.faction == "unknown"
    assert session.scalar(
        select(func.count())
        .select_from(MafiaPhase)
        .where(MafiaPhase.phase_type == "night")
    ) == 1


def test_semantic_documents_are_linked_and_idempotent() -> None:
    session, video, utterances = database()
    session.add(
        MafiaRound(
            video_id=video.id,
            round_number=1,
            start_time=utterances[0].start_time,
            end_time=utterances[-1].end_time,
            winning_faction="civilians",
            review_status="confirmed",
        )
    )
    session.commit()
    first = build_semantic_documents_for_video(session, video)
    second = build_semantic_documents_for_video(session, video)
    assert first >= 3
    assert second == first
    assert session.scalar(select(func.count()).select_from(SemanticDocument)) == first
    assert session.scalar(select(func.count()).select_from(EmbeddingJob)) == first


def test_persist_enrichment_requires_winner_evidence() -> None:
    session, video, utterances = database()
    result = {
        "content_type": "mafia_only",
        "has_mafia": True,
        "confidence": 0.95,
        "summary": "Одна игра",
        "rounds": [
            {
                "round_number": 1,
                "start_utterance_id": utterances[0].id,
                "end_utterance_id": utterances[-1].id,
                "is_partial": False,
                "winning_faction": "mafia",
                "winner_summary": "Модель угадала без ссылки",
                "confidence": 0.99,
                "winner_evidence": [],
                "participants": [],
                "phases": [],
                "events": [],
            }
        ],
    }
    persist_enrichment_result(
        session,
        video=video,
        utterances=utterances,
        source_hash=transcript_hash(video, utterances),
        result=copy.deepcopy(result),
        model="test",
    )
    session.commit()
    round_row = session.scalar(select(MafiaRound))
    assert round_row is not None
    assert round_row.winning_faction == "unknown"
    assert round_row.confidence == 0.5


def test_conflict_detection_and_free_model_json_parser() -> None:
    base = {
        "content_type": "mafia_only",
        "has_mafia": True,
        "rounds": [
            {
                "start_utterance_id": 10,
                "end_utterance_id": 20,
                "winning_faction": "civilians",
                "confidence": 0.95,
                "participants": [],
            }
        ],
    }
    assert not results_conflict(base, base)
    changed = {**base, "rounds": [{**base["rounds"][0], "winning_faction": "mafia"}]}
    assert results_conflict(base, changed)
    neighbouring_boundary = {
        **base,
        "rounds": [
            {
                **base["rounds"][0],
                "start_utterance_id": 18,
                "end_utterance_id": 28,
            }
        ],
    }
    assert not results_conflict(base, neighbouring_boundary)
    distant_boundary = {
        **base,
        "rounds": [{**base["rounds"][0], "start_utterance_id": 30}],
    }
    assert results_conflict(base, distant_boundary)
    assert _extract_json("```json\n{\"ok\": true}\n```") == {"ok": True}


def test_role_resolver_rejects_character_name_as_mafia_role() -> None:
    session, _video, _utterances = database()
    seed_mafia_roles(session)
    session.flush()

    assert _role_for_name(session, "Клод", "civilians") is None
    assert _role_for_name(session, "Шериф", "civilians") is not None


def test_extract_json_accepts_embedded_json_after_reasoning() -> None:
    assert _extract_json('сначала проверка\\n{"ok": true}\\nконец') == {"ok": True}


def test_normalize_enrichment_accepts_openrouter_content_parts() -> None:
    from app.enrichment import normalize_enrichment_data

    result = normalize_enrichment_data(
        [
            {"type": "reasoning", "text": "Проверяю расшифровку."},
            {
                "type": "text",
                "text": (
                    '{"content_type":"talk_only","has_mafia":false,'
                    '"confidence":0.9,"summary":"Разговор","rounds":[]}'
                ),
            },
        ]
    )
    assert result["content_type"] == "talk_only"
    assert result["has_mafia"] is False
