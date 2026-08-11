from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import SpeakerProfile, Utterance, Video, VideoSpeaker, Word
from app.transcript_parser import (
    extract_enrollment_identifier,
    extract_tokens,
    group_turns,
    normalize_video_transcript,
)

FIXTURE = Path(__file__).parent / "fixtures" / "sample_transcript.json"


def make_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_extract_and_group_turns() -> None:
    transcript = json.loads(FIXTURE.read_text(encoding="utf-8"))
    tokens = extract_tokens(transcript)
    turns = group_turns(tokens)
    assert len(tokens) == 9
    assert len(turns) == 3
    assert turns[0].text == "Привет, как дела?"
    assert turns[1].text == "Хорошо."
    assert turns[2].text == "Продолжим."


def test_normalize_transcript_into_relational_database() -> None:
    transcript = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with make_session() as session:
        profile = SpeakerProfile(name="Эмир", api_label="profile_1")
        session.add(profile)
        session.flush()
        video = Video(
            title="demo",
            original_filename="demo.wav",
            source_path="/tmp/demo.wav",
            source_signature="signature",
            source_size_bytes=123,
            source_modified_ns=1,
            language="ru",
            model="enhanced",
        )
        session.add(video)
        session.flush()
        text = normalize_video_transcript(session, video, transcript)
        session.commit()

        speakers = session.scalars(select(VideoSpeaker).order_by(VideoSpeaker.label)).all()
        utterances = session.scalars(select(Utterance).order_by(Utterance.sequence)).all()
        words = session.scalars(select(Word).order_by(Word.sequence)).all()

        assert len(speakers) == 2
        assert {row.display_name for row in speakers} == {"Эмир", "S1"}
        assert len(utterances) == 3
        assert len(words) == 9
        assert "Эмир: Привет, как дела?" in text
        assert utterances[0].speaker.profile_id == profile.id


def test_enrollment_selects_single_speaker() -> None:
    transcript = json.loads(FIXTURE.read_text(encoding="utf-8"))
    transcript["speakers"] = [
        {"label": "S1", "speaker_identifiers": ["encrypted-id-unknown"]}
    ]
    for result in transcript["results"]:
        for alternative in result.get("alternatives", []):
            if result.get("type") in {"word", "entity"}:
                alternative["speaker"] = "S1"
    identifier, label = extract_enrollment_identifier(transcript)
    assert label == "S1"
    assert identifier == "encrypted-id-unknown"


def test_enrollment_rejects_mixed_sample() -> None:
    transcript = json.loads(FIXTURE.read_text(encoding="utf-8"))
    try:
        extract_enrollment_identifier(transcript)
    except ValueError as exc:
        assert "several speakers" in str(exc)
    else:
        raise AssertionError("A mixed enrollment sample must be rejected")
