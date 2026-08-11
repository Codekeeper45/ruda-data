from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .models import SpeakerProfile, Utterance, Video, VideoSpeaker, Word


@dataclass(slots=True)
class ParsedToken:
    sequence: int
    token_type: str
    content: str
    start_time: float
    end_time: float
    confidence: float | None
    language: str | None
    speaker_label: str
    attaches_to: str | None
    is_eos: bool
    raw: dict[str, Any]


@dataclass(slots=True)
class ParsedTurn:
    speaker_label: str
    start_time: float
    end_time: float
    tokens: list[ParsedToken] = field(default_factory=list)

    @property
    def text(self) -> str:
        output = ""
        attach_next = False
        for token in self.tokens:
            if token.token_type == "punctuation":
                if token.attaches_to == "previous" or output:
                    output = output.rstrip() + token.content
                else:
                    output += token.content
                attach_next = token.attaches_to == "next"
                continue
            if output and not output.endswith((" ", "\n")) and not attach_next:
                output += " "
            output += token.content
            attach_next = False
        return output.strip()


def _best_alternative(item: dict[str, Any]) -> dict[str, Any]:
    alternatives = item.get("alternatives") or []
    if alternatives and isinstance(alternatives[0], dict):
        return alternatives[0]
    return {}


def extract_tokens(transcript: dict[str, Any]) -> list[ParsedToken]:
    tokens: list[ParsedToken] = []
    current_speaker = "UU"

    for sequence, item in enumerate(transcript.get("results") or []):
        if not isinstance(item, dict):
            continue
        token_type = str(item.get("type") or "")
        if token_type not in {"word", "punctuation", "entity"}:
            continue
        alternative = _best_alternative(item)
        content = alternative.get("content")
        if not isinstance(content, str) or not content:
            continue

        speaker = alternative.get("speaker")
        if isinstance(speaker, str) and speaker:
            current_speaker = speaker
        elif token_type == "punctuation":
            speaker = current_speaker
        else:
            speaker = "UU"
            current_speaker = speaker

        tokens.append(
            ParsedToken(
                sequence=sequence,
                token_type=token_type,
                content=content,
                start_time=float(item.get("start_time") or 0.0),
                end_time=float(item.get("end_time") or item.get("start_time") or 0.0),
                confidence=(
                    float(alternative["confidence"])
                    if isinstance(alternative.get("confidence"), (int, float))
                    else None
                ),
                language=(str(alternative["language"]) if alternative.get("language") else None),
                speaker_label=str(speaker or "UU"),
                attaches_to=(str(item["attaches_to"]) if item.get("attaches_to") else None),
                is_eos=bool(item.get("is_eos", False)),
                raw=item,
            )
        )
    return tokens


def group_turns(tokens: list[ParsedToken], max_silence_gap: float = 2.0) -> list[ParsedTurn]:
    turns: list[ParsedTurn] = []
    current: ParsedTurn | None = None

    for token in tokens:
        if current is None:
            current = ParsedTurn(token.speaker_label, token.start_time, token.end_time, [token])
            continue

        speaker_changed = token.speaker_label != current.speaker_label
        long_gap = token.start_time - current.end_time > max_silence_gap
        if speaker_changed or long_gap:
            turns.append(current)
            current = ParsedTurn(token.speaker_label, token.start_time, token.end_time, [token])
        else:
            current.tokens.append(token)
            current.end_time = max(current.end_time, token.end_time)

    if current is not None:
        turns.append(current)
    return turns


def extract_enrollment_identifier(transcript: dict[str, Any]) -> tuple[str, str]:
    speakers = transcript.get("speakers") or []
    if not speakers:
        raise ValueError("Speechmatics returned no speaker identifier. Use a clear 5–30 second voice sample.")

    identifiers_by_label: dict[str, list[str]] = {}
    for speaker in speakers:
        if not isinstance(speaker, dict):
            continue
        label = str(speaker.get("label") or "")
        identifiers = [str(value) for value in (speaker.get("speaker_identifiers") or []) if value]
        if label and identifiers:
            identifiers_by_label[label] = identifiers

    if not identifiers_by_label:
        raise ValueError("Speechmatics returned an empty speaker identifier list")

    duration_by_label: dict[str, float] = defaultdict(float)
    for token in extract_tokens(transcript):
        if token.token_type in {"word", "entity"}:
            duration_by_label[token.speaker_label] += max(0.0, token.end_time - token.start_time)

    dominant_label = max(
        identifiers_by_label,
        key=lambda label: duration_by_label.get(label, 0.0),
    )
    total = sum(duration_by_label.get(label, 0.0) for label in identifiers_by_label)
    dominant = duration_by_label.get(dominant_label, 0.0)
    if len(identifiers_by_label) > 1 and total > 0 and dominant / total < 0.80:
        raise ValueError(
            "The enrollment sample contains several speakers. Upload a clip where only the target person speaks."
        )
    return identifiers_by_label[dominant_label][0], dominant_label


def normalize_video_transcript(
    session: Session,
    video: Video,
    transcript: dict[str, Any],
) -> str:
    session.execute(delete(Word).where(Word.video_id == video.id))
    session.execute(delete(Utterance).where(Utterance.video_id == video.id))
    session.execute(delete(VideoSpeaker).where(VideoSpeaker.video_id == video.id))
    session.flush()

    profiles = {
        profile.api_label: profile
        for profile in session.scalars(select(SpeakerProfile).where(SpeakerProfile.active.is_(True))).all()
    }
    tokens = extract_tokens(transcript)
    turns = group_turns(tokens)
    labels = sorted({token.speaker_label for token in tokens})

    speaker_rows: dict[str, VideoSpeaker] = {}
    for label in labels:
        profile = profiles.get(label)
        row = VideoSpeaker(
            video_id=video.id,
            label=label,
            display_name=profile.name if profile else label,
            profile_id=profile.id if profile else None,
            is_known=profile is not None,
        )
        session.add(row)
        session.flush()
        speaker_rows[label] = row

    transcript_lines: list[str] = []
    token_db_sequence = 0
    for turn_sequence, turn in enumerate(turns):
        speaker = speaker_rows[turn.speaker_label]
        word_tokens = [token for token in turn.tokens if token.token_type in {"word", "entity"}]
        confidences = [token.confidence for token in word_tokens if token.confidence is not None]
        utterance = Utterance(
            video_id=video.id,
            speaker_id=speaker.id,
            sequence=turn_sequence,
            start_time=turn.start_time,
            end_time=turn.end_time,
            text=turn.text,
            average_confidence=(sum(confidences) / len(confidences) if confidences else None),
            word_count=len(word_tokens),
        )
        session.add(utterance)
        session.flush()

        for token in turn.tokens:
            session.add(
                Word(
                    video_id=video.id,
                    utterance_id=utterance.id,
                    speaker_id=speaker.id,
                    sequence=token_db_sequence,
                    token_type=token.token_type,
                    content=token.content,
                    start_time=token.start_time,
                    end_time=token.end_time,
                    confidence=token.confidence,
                    language=token.language,
                    attaches_to=token.attaches_to,
                    is_eos=token.is_eos,
                    raw_json=token.raw,
                )
            )
            token_db_sequence += 1

        speaker.utterance_count += 1
        speaker.total_speech_seconds += max(0.0, turn.end_time - turn.start_time)
        transcript_lines.append(
            f"[{format_timestamp(turn.start_time)}] {speaker.display_name}: {turn.text}"
        )

    return "\n".join(transcript_lines)


def format_timestamp(seconds: float, *, srt: bool = False) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    separator = "," if srt else "."
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"
