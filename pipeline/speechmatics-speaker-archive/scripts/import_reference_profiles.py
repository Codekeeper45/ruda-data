#!/usr/bin/env python3
"""Idempotently import audited reference folders into the local review UI.

The source files are copied, never moved or edited. Importing does not create
Speechmatics jobs; samples stay in ``pending_review`` until the user approves
profiles and explicitly opens the enrollment gate.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import joinedload  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import Base, SessionLocal, engine  # noqa: E402
from app.models import ProfileReview, SampleAudit, SpeakerProfile, SpeakerSample  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--audit-json", type=Path, required=True)
    args = parser.parse_args()
    audit_path = args.audit_json.expanduser().resolve()
    payload = json.loads(audit_path.read_text(encoding="utf-8"))
    rows = payload.get("samples") or []
    if not rows:
        raise SystemExit("Отчёт не содержит образцов")

    Base.metadata.create_all(bind=engine)
    imported_root = settings.storage_dir / "samples" / "imported"
    imported_root.mkdir(parents=True, exist_ok=True)

    created_profiles = created_samples = updated_samples = copied_files = 0
    with SessionLocal() as session:
        for row in rows:
            profile_name = str(row["profile_name"]).strip()
            source = Path(row["source_path"]).expanduser().resolve()
            if not source.is_file():
                raise RuntimeError(f"Исходный образец пропал: {source}")

            profile = session.scalar(
                select(SpeakerProfile)
                .where(SpeakerProfile.name == profile_name)
                .options(joinedload(SpeakerProfile.review))
            )
            if profile is None:
                profile = SpeakerProfile(
                    name=profile_name,
                    api_label="pending",
                    active=True,
                    notes="Импортирован из проверенных reference_uploads; ждёт ручного подтверждения.",
                )
                session.add(profile)
                session.flush()
                profile.api_label = f"profile_{profile.id}"
                session.add(ProfileReview(profile_id=profile.id, manual_status="pending"))
                created_profiles += 1
            elif profile.review is None:
                session.add(ProfileReview(profile_id=profile.id, manual_status="pending"))

            sample = session.scalar(
                select(SpeakerSample)
                .where(
                    SpeakerSample.profile_id == profile.id,
                    SpeakerSample.sha256 == row["file_sha256"],
                )
                .options(joinedload(SpeakerSample.audit))
            )
            if sample is None:
                sample = SpeakerSample(
                    profile_id=profile.id,
                    original_filename=row["filename"],
                    stored_path="pending-copy",
                    duration_seconds=float(row.get("duration_seconds") or 0),
                    size_bytes=source.stat().st_size,
                    sha256=row["file_sha256"],
                    status="pending_review",
                )
                session.add(sample)
                session.flush()
                created_samples += 1

            profile_dir = imported_root / f"profile_{profile.id}"
            profile_dir.mkdir(parents=True, exist_ok=True)
            destination = profile_dir / f"sample_{sample.id}.wav"
            if not destination.exists() or destination.stat().st_size != source.stat().st_size:
                temp = destination.with_suffix(".part.wav")
                shutil.copy2(source, temp)
                temp.replace(destination)
                copied_files += 1
            sample.stored_path = str(destination.resolve())
            sample.duration_seconds = float(row.get("duration_seconds") or 0)
            sample.size_bytes = destination.stat().st_size
            if not sample.speaker_identifier:
                sample.status = "pending_review"

            audit = sample.audit
            if audit is None:
                audit = SampleAudit(
                    sample_id=sample.id,
                    manual_status="pending",
                    selected_for_enrollment=bool(row.get("selected_for_enrollment")),
                )
                session.add(audit)
            elif audit.manual_status == "pending":
                # Re-running the automatic audit may improve the default
                # selection, but it never overwrites a user's final decision.
                audit.selected_for_enrollment = bool(row.get("selected_for_enrollment"))
                updated_samples += 1

            audit.source_path = str(source)
            audit.pcm_sha256 = row.get("pcm_sha256")
            audit.sample_rate = row.get("sample_rate")
            audit.channels = row.get("channels")
            audit.bit_depth = row.get("bit_depth")
            audit.codec = row.get("codec")
            audit.rms_dbfs = row.get("rms_dbfs")
            audit.peak_dbfs = row.get("peak_dbfs")
            audit.silence_ratio = row.get("silence_ratio")
            audit.clipping_ratio = row.get("clipping_ratio")
            audit.within_profile_similarity = row.get("within_profile_similarity")
            audit.closest_other_profile = row.get("closest_other_profile")
            audit.closest_other_similarity = row.get("closest_other_similarity")
            audit.quality_status = row.get("quality_status") or "unknown"
            audit.quality_issues = row.get("quality_issues") or []

        session.commit()

    print(
        json.dumps(
            {
                "profiles_created": created_profiles,
                "samples_created": created_samples,
                "samples_auto_updated": updated_samples,
                "files_copied": copied_files,
                "jobs_created": 0,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
