from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobKind(StrEnum):
    ENROLLMENT = "enrollment"
    VIDEO = "video"


class JobStatus(StrEnum):
    QUEUED = "queued"
    PREPARING = "preparing"
    SUBMITTING = "submitting"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class SpeakerProfile(Base):
    __tablename__ = "speaker_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    api_label: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    notes: Mapped[str | None] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    samples: Mapped[list[SpeakerSample]] = relationship(
        back_populates="profile", cascade="all, delete-orphan"
    )
    review: Mapped[ProfileReview | None] = relationship(
        back_populates="profile", cascade="all, delete-orphan", uselist=False
    )
    video_speakers: Mapped[list[VideoSpeaker]] = relationship(back_populates="profile")


class SpeakerSample(Base):
    __tablename__ = "speaker_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(ForeignKey("speaker_profiles.id", ondelete="CASCADE"), index=True)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    stored_path: Mapped[str] = mapped_column(Text, nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    speaker_identifier: Mapped[str | None] = mapped_column(Text)
    enrollment_model: Mapped[str | None] = mapped_column(String(30))
    enrollment_language: Mapped[str | None] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(30), default=JobStatus.QUEUED, nullable=False, index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    profile: Mapped[SpeakerProfile] = relationship(back_populates="samples")
    audit: Mapped[SampleAudit | None] = relationship(
        back_populates="sample", cascade="all, delete-orphan", uselist=False
    )
    jobs: Mapped[list[TranscriptionJob]] = relationship(back_populates="sample")

    __table_args__ = (
        UniqueConstraint("profile_id", "sha256", name="uq_profile_sample_hash"),
    )


class ProfileReview(Base):
    __tablename__ = "profile_reviews"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_id: Mapped[int] = mapped_column(
        ForeignKey("speaker_profiles.id", ondelete="CASCADE"), unique=True, index=True
    )
    manual_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    profile: Mapped[SpeakerProfile] = relationship(back_populates="review")


class SampleAudit(Base):
    __tablename__ = "sample_audits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    sample_id: Mapped[int] = mapped_column(
        ForeignKey("speaker_samples.id", ondelete="CASCADE"), unique=True, index=True
    )
    source_path: Mapped[str | None] = mapped_column(Text)
    pcm_sha256: Mapped[str | None] = mapped_column(String(64), index=True)
    sample_rate: Mapped[int | None] = mapped_column(Integer)
    channels: Mapped[int | None] = mapped_column(Integer)
    bit_depth: Mapped[int | None] = mapped_column(Integer)
    codec: Mapped[str | None] = mapped_column(String(80))
    rms_dbfs: Mapped[float | None] = mapped_column(Float)
    peak_dbfs: Mapped[float | None] = mapped_column(Float)
    silence_ratio: Mapped[float | None] = mapped_column(Float)
    clipping_ratio: Mapped[float | None] = mapped_column(Float)
    within_profile_similarity: Mapped[float | None] = mapped_column(Float)
    closest_other_profile: Mapped[str | None] = mapped_column(String(200))
    closest_other_similarity: Mapped[float | None] = mapped_column(Float)
    quality_status: Mapped[str] = mapped_column(String(30), default="unknown", nullable=False, index=True)
    quality_issues: Mapped[list | None] = mapped_column(JSON)
    manual_status: Mapped[str] = mapped_column(String(30), default="pending", nullable=False, index=True)
    manual_notes: Mapped[str | None] = mapped_column(Text)
    selected_for_enrollment: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    audited_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sample: Mapped[SpeakerSample] = relationship(back_populates="audit")


class SourceFolder(Base):
    __tablename__ = "source_folders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    recursive: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    auto_scan: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    videos: Mapped[list[Video]] = relationship(back_populates="source_folder")


class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_folder_id: Mapped[int | None] = mapped_column(
        ForeignKey("source_folders.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(500), nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_signature: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    source_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    source_modified_ns: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    prepared_path: Mapped[str | None] = mapped_column(Text)
    prepared_size_bytes: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default=JobStatus.QUEUED, nullable=False, index=True)
    language: Mapped[str] = mapped_column(String(20), default="ru", nullable=False)
    model: Mapped[str] = mapped_column(String(30), default="enhanced", nullable=False)
    remote_job_id: Mapped[str | None] = mapped_column(String(100), index=True)
    raw_transcript_path: Mapped[str | None] = mapped_column(Text)
    transcript_text: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    source_folder: Mapped[SourceFolder | None] = relationship(back_populates="videos")
    jobs: Mapped[list[TranscriptionJob]] = relationship(back_populates="video")
    speakers: Mapped[list[VideoSpeaker]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )
    utterances: Mapped[list[Utterance]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )
    words: Mapped[list[Word]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )


class TranscriptionJob(Base):
    __tablename__ = "transcription_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(30), default=JobStatus.QUEUED, nullable=False, index=True)
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    sample_id: Mapped[int | None] = mapped_column(ForeignKey("speaker_samples.id", ondelete="CASCADE"), index=True)
    remote_job_id: Mapped[str | None] = mapped_column(String(100), index=True)
    config_json: Mapped[dict | None] = mapped_column(JSON)
    prepared_path: Mapped[str | None] = mapped_column(Text)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    video: Mapped[Video | None] = relationship(back_populates="jobs")
    sample: Mapped[SpeakerSample | None] = relationship(back_populates="jobs")

    __table_args__ = (
        Index("ix_job_active_order", "status", "kind", "created_at"),
    )


class VideoSpeaker(Base):
    __tablename__ = "video_speakers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    profile_id: Mapped[int | None] = mapped_column(ForeignKey("speaker_profiles.id", ondelete="SET NULL"), index=True)
    is_known: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    total_speech_seconds: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    utterance_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    video: Mapped[Video] = relationship(back_populates="speakers")
    profile: Mapped[SpeakerProfile | None] = relationship(back_populates="video_speakers")
    utterances: Mapped[list[Utterance]] = relationship(back_populates="speaker")
    words: Mapped[list[Word]] = relationship(back_populates="speaker")

    __table_args__ = (
        UniqueConstraint("video_id", "label", name="uq_video_speaker_label"),
    )


class Utterance(Base):
    __tablename__ = "utterances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    speaker_id: Mapped[int] = mapped_column(ForeignKey("video_speakers.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    average_confidence: Mapped[float | None] = mapped_column(Float)
    word_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    video: Mapped[Video] = relationship(back_populates="utterances")
    speaker: Mapped[VideoSpeaker] = relationship(back_populates="utterances")
    words: Mapped[list[Word]] = relationship(back_populates="utterance")

    __table_args__ = (
        UniqueConstraint("video_id", "sequence", name="uq_video_utterance_sequence"),
    )


class Word(Base):
    __tablename__ = "words"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    utterance_id: Mapped[int] = mapped_column(ForeignKey("utterances.id", ondelete="CASCADE"), index=True)
    speaker_id: Mapped[int] = mapped_column(ForeignKey("video_speakers.id", ondelete="CASCADE"), index=True)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    token_type: Mapped[str] = mapped_column(String(30), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    language: Mapped[str | None] = mapped_column(String(20))
    attaches_to: Mapped[str | None] = mapped_column(String(20))
    is_eos: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    raw_json: Mapped[dict | None] = mapped_column(JSON)

    video: Mapped[Video] = relationship(back_populates="words")
    utterance: Mapped[Utterance] = relationship(back_populates="words")
    speaker: Mapped[VideoSpeaker] = relationship(back_populates="words")

    __table_args__ = (
        UniqueConstraint("video_id", "sequence", name="uq_video_word_sequence"),
    )


class VideoEnrichment(Base):
    __tablename__ = "video_enrichments"

    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True
    )
    content_type: Mapped[str] = mapped_column(
        String(30), default="unknown", nullable=False, index=True
    )
    has_mafia: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(
        String(30), default="pending", nullable=False, index=True
    )
    review_status: Mapped[str] = mapped_column(
        String(30), default="pending", nullable=False, index=True
    )
    extractor_model: Mapped[str | None] = mapped_column(String(120))
    extractor_version: Mapped[str | None] = mapped_column(String(80))
    source_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    error_message: Mapped[str | None] = mapped_column(Text)
    raw_result: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MafiaRole(Base):
    __tablename__ = "mafia_roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    faction: Mapped[str] = mapped_column(String(80), default="unknown", nullable=False, index=True)
    aliases: Mapped[list | None] = mapped_column(JSON)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MafiaRound(Base):
    __tablename__ = "mafia_rounds"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round_number: Mapped[int] = mapped_column(Integer, nullable=False)
    start_time: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    start_utterance_id: Mapped[int | None] = mapped_column(
        ForeignKey("utterances.id", ondelete="SET NULL"), index=True
    )
    end_utterance_id: Mapped[int | None] = mapped_column(
        ForeignKey("utterances.id", ondelete="SET NULL"), index=True
    )
    is_partial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    winning_faction: Mapped[str] = mapped_column(
        String(80), default="unknown", nullable=False, index=True
    )
    winner_summary: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    review_status: Mapped[str] = mapped_column(
        String(30), default="pending", nullable=False, index=True
    )
    extractor_version: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("video_id", "round_number", name="uq_mafia_round_video_number"),
        Index("ix_mafia_round_video_time", "video_id", "start_time", "end_time"),
    )


class MafiaRoundParticipant(Base):
    __tablename__ = "mafia_round_participants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("mafia_rounds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("speaker_profiles.id", ondelete="SET NULL"), index=True
    )
    video_speaker_id: Mapped[int | None] = mapped_column(
        ForeignKey("video_speakers.id", ondelete="SET NULL"), index=True
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    role_id: Mapped[int | None] = mapped_column(
        ForeignKey("mafia_roles.id", ondelete="SET NULL"), index=True
    )
    faction: Mapped[str] = mapped_column(
        String(80), default="unknown", nullable=False, index=True
    )
    outcome: Mapped[str] = mapped_column(
        String(30), default="unknown", nullable=False, index=True
    )
    confidence: Mapped[float | None] = mapped_column(Float)
    role_confidence: Mapped[float | None] = mapped_column(Float)
    review_status: Mapped[str] = mapped_column(
        String(30), default="pending", nullable=False, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint("round_id", "display_name", name="uq_mafia_participant_round_name"),
    )


class MafiaPhase(Base):
    __tablename__ = "mafia_phases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("mafia_rounds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    phase_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    phase_number: Mapped[int | None] = mapped_column(Integer)
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    is_partial: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    review_status: Mapped[str] = mapped_column(
        String(30), default="pending", nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_mafia_phase_round_time", "round_id", "start_time", "end_time"),
    )


class MafiaEvent(Base):
    __tablename__ = "mafia_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    round_id: Mapped[int] = mapped_column(
        ForeignKey("mafia_rounds.id", ondelete="CASCADE"), nullable=False, index=True
    )
    phase_id: Mapped[int | None] = mapped_column(
        ForeignKey("mafia_phases.id", ondelete="SET NULL"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(60), nullable=False, index=True)
    actor_participant_id: Mapped[int | None] = mapped_column(
        ForeignKey("mafia_round_participants.id", ondelete="SET NULL"), index=True
    )
    target_participant_id: Mapped[int | None] = mapped_column(
        ForeignKey("mafia_round_participants.id", ondelete="SET NULL"), index=True
    )
    start_time: Mapped[float] = mapped_column(Float, nullable=False)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    review_status: Mapped[str] = mapped_column(
        String(30), default="pending", nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EnrichmentEvidence(Base):
    __tablename__ = "enrichment_evidence"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entity_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    entity_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    field_name: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    utterance_id: Mapped[int | None] = mapped_column(
        ForeignKey("utterances.id", ondelete="SET NULL"), index=True
    )
    start_time: Mapped[float | None] = mapped_column(Float)
    end_time: Mapped[float | None] = mapped_column(Float)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_ref: Mapped[str | None] = mapped_column(Text)
    excerpt: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        Index("ix_enrichment_evidence_entity", "entity_type", "entity_id", "field_name"),
    )


class EnrichmentRun(Base):
    __tablename__ = "enrichment_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int | None] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), index=True
    )
    stage: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(30), default="queued", nullable=False, index=True
    )
    model: Mapped[str | None] = mapped_column(String(120))
    pipeline_version: Mapped[str] = mapped_column(String(80), nullable=False)
    input_hash: Mapped[str | None] = mapped_column(String(64), index=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    raw_output_path: Mapped[str | None] = mapped_column(Text)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "video_id", "stage", "pipeline_version", "input_hash",
            name="uq_enrichment_run_identity",
        ),
    )


class SemanticDocument(Base):
    __tablename__ = "semantic_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_type: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=False, index=True
    )
    round_id: Mapped[int | None] = mapped_column(
        ForeignKey("mafia_rounds.id", ondelete="CASCADE"), index=True
    )
    start_time: Mapped[float] = mapped_column(Float, nullable=False, index=True)
    end_time: Mapped[float] = mapped_column(Float, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    pipeline_version: Mapped[str] = mapped_column(String(80), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        UniqueConstraint(
            "document_type", "video_id", "round_id", "start_time", "content_hash",
            name="uq_semantic_document_identity",
        ),
        Index("ix_semantic_document_video_time", "video_id", "start_time", "end_time"),
    )


class SemanticDocumentUtterance(Base):
    __tablename__ = "semantic_document_utterances"

    document_id: Mapped[int] = mapped_column(
        ForeignKey("semantic_documents.id", ondelete="CASCADE"), primary_key=True
    )
    utterance_id: Mapped[int] = mapped_column(
        ForeignKey("utterances.id", ondelete="CASCADE"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)


class EmbeddingVector(Base):
    __tablename__ = "embedding_vectors"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("semantic_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    dtype: Mapped[str] = mapped_column(String(20), default="float32", nullable=False)
    vector: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint(
            "document_id", "model", "dimensions", name="uq_embedding_document_model"
        ),
    )


class EmbeddingJob(Base):
    __tablename__ = "embedding_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    document_id: Mapped[int] = mapped_column(
        ForeignKey("semantic_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), default="queued", nullable=False, index=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "document_id", "model", "dimensions", "input_hash",
            name="uq_embedding_job_identity",
        ),
    )
