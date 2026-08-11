from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import main
from app.db import Base
from app.secrets import get_setting, set_setting


def test_resume_processing_clears_persisted_pause(monkeypatch) -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    test_sessions = sessionmaker(bind=engine, expire_on_commit=False)
    with test_sessions() as session:
        set_setting(session, "processing_paused", "1")
        set_setting(session, "processing_pause_reason", "account rejected")
        session.commit()

    monkeypatch.setattr(main, "SessionLocal", test_sessions)
    response = main.resume_processing()

    assert response.status_code == 303
    assert response.headers["location"].startswith("/?message=")
    with test_sessions() as session:
        assert get_setting(session, "processing_paused") == "0"
        assert get_setting(session, "processing_pause_reason") == ""
