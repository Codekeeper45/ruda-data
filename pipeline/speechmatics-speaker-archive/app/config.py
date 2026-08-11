from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


BASE_DIR = Path(__file__).resolve().parent.parent


class AppSettings(BaseSettings):
    app_name: str = "Speechmatics Speaker Archive"
    database_url: str = f"sqlite:///{BASE_DIR / 'data' / 'app.db'}"
    storage_dir: Path = BASE_DIR / "storage"
    app_secret_key: str = "change-me-in-production"
    speechmatics_api_key: str | None = None
    speechmatics_base_url: str = "https://eu1.asr.api.speechmatics.com/v2"
    openrouter_api_key: str | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    enrichment_model: str = "inclusionai/ling-3.0-flash:free"
    enrichment_verifier_model: str = "nvidia/nemotron-3-super-120b-a12b:free"
    enrichment_escalation_model: str = "openai/gpt-5.6-luna"
    enrichment_vision_model: str = "nvidia/nemotron-nano-12b-v2-vl:free"
    rag_answer_model: str = "inclusionai/ling-3.0-flash:free"
    embedding_model: str = "voyageai/voyage-4"
    embedding_dimensions: int = 1024
    enrichment_pipeline_version: str = "20260730-v1"
    worker_enabled: bool = True
    worker_poll_seconds: float = 10.0
    folder_scan_interval_seconds: int = 60
    max_upload_bytes: int = 980_000_000
    prepare_audio: bool = True
    prepared_sample_rate: int = 16000
    prepared_channels: int = 1
    keep_prepared_audio: bool = False
    mock_transcript_path: Path | None = None

    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    def ensure_directories(self) -> None:
        for path in (
            BASE_DIR / "data",
            self.storage_dir,
            self.storage_dir / "samples",
            self.storage_dir / "prepared",
            self.storage_dir / "transcripts",
            self.storage_dir / "exports",
            self.storage_dir / "enrichment",
            self.storage_dir / "enrichment" / "raw",
            self.storage_dir / "enrichment" / "frames",
            self.storage_dir / "clips",
        ):
            path.mkdir(parents=True, exist_ok=True)


settings = AppSettings()
settings.ensure_directories()
