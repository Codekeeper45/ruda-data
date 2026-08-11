from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.models import (
    JobKind,
    JobStatus,
    ProfileReview,
    SampleAudit,
    SpeakerProfile,
    SpeakerSample,
    TranscriptionJob,
    Video,
)
from app.secrets import set_setting
from app.worker import ArchiveWorker, is_account_rejection


def make_session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_video_config_contains_known_speakers_and_vocab() -> None:
    with make_session() as session:
        set_setting(session, "speaker_sensitivity", "0.6")
        set_setting(session, "additional_vocab", "GigaAM\nT-One\nслишком длинная фраза из семи отдельных русских слов здесь")
        profile = SpeakerProfile(name="Эмир", api_label="profile_1")
        session.add(profile)
        session.flush()
        session.add(ProfileReview(profile_id=profile.id, manual_status="approved"))
        sample = SpeakerSample(
            profile_id=profile.id,
            original_filename="sample.wav",
            stored_path="/tmp/sample.wav",
            size_bytes=10,
            sha256="a" * 64,
            speaker_identifier="voice-id",
            enrollment_model="enhanced",
            enrollment_language="ru",
            status=JobStatus.COMPLETED,
        )
        session.add(sample)
        session.flush()
        session.add(
            SampleAudit(
                sample_id=sample.id,
                quality_status="good",
                manual_status="approved",
                selected_for_enrollment=True,
            )
        )
        video = Video(
            title="demo",
            original_filename="demo.wav",
            source_path="/tmp/demo.wav",
            source_signature="b" * 64,
            source_size_bytes=10,
            source_modified_ns=1,
            model="enhanced",
            language="ru",
        )
        session.add(video)
        session.flush()

        config = ArchiveWorker()._build_video_config(session, video)
        tc = config["transcription_config"]
        dc = tc["speaker_diarization_config"]
        assert tc["diarization"] == "speaker"
        assert dc["speakers"] == [
            {"label": "profile_1", "speaker_identifiers": ["voice-id"]}
        ]
        assert "prefer_current_speaker" not in dc
        assert "speakers_sensitivity" not in dc
        assert dc["speaker_sensitivity"] == 0.6
        assert tc["additional_vocab"] == [{"content": "GigaAM"}, {"content": "T-One"}]
        assert "enable_entities" not in tc


def test_video_config_ignores_identifiers_from_another_model() -> None:
    with make_session() as session:
        profile = SpeakerProfile(name="Эмир", api_label="profile_1")
        session.add(profile)
        session.flush()
        session.add(ProfileReview(profile_id=profile.id, manual_status="approved"))
        sample = SpeakerSample(
            profile_id=profile.id,
            original_filename="sample.wav",
            stored_path="/tmp/sample.wav",
            size_bytes=10,
            sha256="c" * 64,
            speaker_identifier="standard-id",
            enrollment_model="standard",
            enrollment_language="ru",
            status=JobStatus.COMPLETED,
        )
        session.add(sample)
        session.flush()
        session.add(
            SampleAudit(
                sample_id=sample.id,
                quality_status="good",
                manual_status="approved",
                selected_for_enrollment=True,
            )
        )
        video = Video(
            title="demo",
            original_filename="demo.wav",
            source_path="/tmp/demo.wav",
            source_signature="d" * 64,
            source_size_bytes=10,
            source_modified_ns=1,
            model="enhanced",
            language="ru",
        )
        session.add(video)
        session.flush()

        config = ArchiveWorker()._build_video_config(session, video)
        dc = config["transcription_config"]["speaker_diarization_config"]
        assert "speakers" not in dc


def test_account_rejection_only_matches_hard_account_failures() -> None:
    assert is_account_rejection("Speechmatics HTTP 402: payment required")
    assert is_account_rejection("Speechmatics HTTP 403: quota exceeded")
    assert is_account_rejection("Speechmatics HTTP 401: invalid token")
    assert not is_account_rejection("Could not submit job: ReadTimeout")
    assert not is_account_rejection("Speechmatics HTTP 429: too many requests")
    assert not is_account_rejection("Speechmatics HTTP 400: invalid config")


def test_long_video_uses_compact_mp3_transport() -> None:
    with make_session() as session:
        video = Video(
            title="long demo",
            original_filename="long.wav",
            source_path="/tmp/long.wav",
            source_signature="e" * 64,
            source_size_bytes=10,
            source_modified_ns=1,
            duration_seconds=3 * 3600,
            model="enhanced",
            language="ru",
        )
        session.add(video)
        session.flush()
        job = TranscriptionJob(kind=JobKind.VIDEO, video_id=video.id)
        session.add(job)
        session.flush()

        _source, destination, _config = ArchiveWorker()._build_job_inputs(session, job)
        assert destination.name == f"video_{video.id}.mp3"
