from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.db import Base, engine  # noqa: E402
from app.enrichment import seed_mafia_roles  # noqa: E402
from app.migrations import ensure_database_features  # noqa: E402
from app.db import SessionLocal  # noqa: E402


def main() -> None:
    database = ROOT / "data" / "app.db"
    backup_dir = ROOT / "storage" / "exports" / "pre_enrichment"
    backup_dir.mkdir(parents=True, exist_ok=True)
    destination = backup_dir / f"app-{datetime.now():%Y%m%d-%H%M%S}.db"
    with sqlite3.connect(database) as source, sqlite3.connect(destination) as target:
        source.backup(target)
    with sqlite3.connect(destination) as connection:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        destination.unlink(missing_ok=True)
        raise SystemExit(f"Резервная копия повреждена: {integrity}")
    Base.metadata.create_all(bind=engine)
    ensure_database_features(engine)
    with SessionLocal() as session:
        seed_mafia_roles(session)
        session.commit()
    print(f"Backup: {destination}")
    print("Migration: OK")


if __name__ == "__main__":
    main()
