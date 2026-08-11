from __future__ import annotations

import wave
from pathlib import Path

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import SourceFolder, TranscriptionJob, Video
from app.scanner import scan_source_folder


def test_scanner_queues_each_file_once(tmp_path: Path) -> None:
    wav_path = tmp_path / "one.wav"
    with wave.open(str(wav_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 1600)

    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        source = SourceFolder(path=str(tmp_path), recursive=False, auto_scan=False)
        session.add(source)
        session.flush()
        first = scan_source_folder(session, source)
        second = scan_source_folder(session, source)
        session.commit()
        assert first == (1, 0)
        assert second == (0, 1)
        assert session.scalar(select(func.count()).select_from(Video)) == 1
        assert session.scalar(select(func.count()).select_from(TranscriptionJob)) == 1
