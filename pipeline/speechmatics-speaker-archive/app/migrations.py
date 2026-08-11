from __future__ import annotations

from sqlalchemy import Engine, text


SCHEMA_VERSION = "20260730_enrichment_v1"


def ensure_database_features(engine: Engine) -> None:
    """Create SQLite-only auxiliary objects that SQLAlchemy cannot model."""
    if engine.dialect.name != "sqlite":
        return
    statements = (
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS semantic_documents_fts USING fts5(
            text,
            content='semantic_documents',
            content_rowid='id',
            tokenize='unicode61'
        )
        """,
        """
        CREATE TRIGGER IF NOT EXISTS semantic_documents_ai
        AFTER INSERT ON semantic_documents BEGIN
            INSERT INTO semantic_documents_fts(rowid, text) VALUES (new.id, new.text);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS semantic_documents_ad
        AFTER DELETE ON semantic_documents BEGIN
            INSERT INTO semantic_documents_fts(
                semantic_documents_fts, rowid, text
            ) VALUES ('delete', old.id, old.text);
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS semantic_documents_au
        AFTER UPDATE OF text ON semantic_documents BEGIN
            INSERT INTO semantic_documents_fts(
                semantic_documents_fts, rowid, text
            ) VALUES ('delete', old.id, old.text);
            INSERT INTO semantic_documents_fts(rowid, text) VALUES (new.id, new.text);
        END
        """,
    )
    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        connection.execute(
            text(
                "INSERT OR IGNORE INTO schema_migrations(version) VALUES (:version)"
            ),
            {"version": SCHEMA_VERSION},
        )


def rebuild_semantic_fts(engine: Engine) -> None:
    if engine.dialect.name != "sqlite":
        return
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO semantic_documents_fts(semantic_documents_fts) "
                "VALUES ('rebuild')"
            )
        )
