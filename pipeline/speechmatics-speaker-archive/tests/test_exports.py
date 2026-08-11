from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import Base
from app.exports import export_video_txt, export_video_vtt
from app.models import Utterance, Video, VideoSpeaker


def test_vtt_and_txt_exports(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        video = Video(
            title="demo",
            original_filename="demo.wav",
            source_path="/tmp/demo.wav",
            source_signature="f" * 64,
            source_size_bytes=10,
            source_modified_ns=1,
        )
        session.add(video)
        session.flush()
        speaker = VideoSpeaker(
            video_id=video.id,
            label="profile_demo",
            display_name="Тест",
            is_known=True,
        )
        session.add(speaker)
        session.flush()
        session.add(
            Utterance(
                video_id=video.id,
                speaker_id=speaker.id,
                sequence=0,
                start_time=1.25,
                end_time=3.5,
                text="Проверка.",
                average_confidence=0.99,
                word_count=1,
            )
        )
        session.commit()

        vtt = export_video_vtt(session, video, tmp_path / "demo.vtt").read_text()
        txt = export_video_txt(session, video, tmp_path / "demo.txt").read_text()

    assert vtt.startswith("WEBVTT\n")
    assert "00:00:01.250 --> 00:00:03.500" in vtt
    assert "Тест: Проверка." in vtt
    assert txt == "[00:00:01.250 --> 00:00:03.500] Тест: Проверка.\n"
