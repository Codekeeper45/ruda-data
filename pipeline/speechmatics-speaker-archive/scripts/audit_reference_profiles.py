#!/usr/bin/env python3
"""Read-only, multi-level audit of character reference WAV folders.

Run this with the project's WhisperX/ECAPA Python environment. The script does
not modify source WAV files. It writes a JSON and CSV report that can be
imported into the review UI.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import subprocess
from collections import defaultdict
from pathlib import Path
from typing import Any

import torch
import torchaudio
from speechbrain.inference.speaker import EncoderClassifier


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def probe(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name,sample_rate,channels,bits_per_sample,bits_per_raw_sample,duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    stream = json.loads(completed.stdout)["streams"][0]
    bit_depth = int(stream.get("bits_per_raw_sample") or stream.get("bits_per_sample") or 0) or None
    return {
        "codec": stream.get("codec_name"),
        "sample_rate": int(stream["sample_rate"]),
        "channels": int(stream["channels"]),
        "bit_depth": bit_depth,
        "probe_duration": float(stream["duration"]) if stream.get("duration") else None,
    }


def normalized_pcm_hash(mono: torch.Tensor, sample_rate: int) -> str:
    if sample_rate != 16000:
        mono = torchaudio.functional.resample(mono, sample_rate, 16000)
    pcm = (
        mono.squeeze(0)
        .clamp(-1, 1)
        .mul(32767)
        .round()
        .to(torch.int16)
        .cpu()
        .numpy()
    )
    return hashlib.sha256(pcm.tobytes()).hexdigest()


def signal_metrics(mono: torch.Tensor, sample_rate: int) -> dict[str, float]:
    signal = mono.squeeze(0).float()
    if signal.numel() == 0:
        return {
            "rms_dbfs": float("-inf"),
            "peak_dbfs": float("-inf"),
            "silence_ratio": 1.0,
            "clipping_ratio": 0.0,
        }
    rms = torch.sqrt(torch.mean(signal.square())).item()
    peak = torch.max(torch.abs(signal)).item()
    frame_size = max(1, int(sample_rate * 0.1))
    usable = (signal.numel() // frame_size) * frame_size
    if usable:
        frames = signal[:usable].reshape(-1, frame_size)
        frame_rms = torch.sqrt(torch.mean(frames.square(), dim=1))
        silence_ratio = float((frame_rms < 10 ** (-50 / 20)).float().mean())
    else:
        silence_ratio = 1.0 if rms < 10 ** (-50 / 20) else 0.0
    clipping_ratio = float((torch.abs(signal) >= 0.999).float().mean())
    return {
        "rms_dbfs": 20 * math.log10(max(rms, 1e-12)),
        "peak_dbfs": 20 * math.log10(max(peak, 1e-12)),
        "silence_ratio": silence_ratio,
        "clipping_ratio": clipping_ratio,
    }


def normalize_embedding(value: torch.Tensor) -> torch.Tensor:
    return torch.nn.functional.normalize(value.squeeze().float().cpu(), dim=0)


def quality_score(row: dict[str, Any]) -> float:
    duration = float(row["duration_seconds"])
    within = float(row.get("within_profile_similarity") or 0)
    cross = float(row.get("closest_other_similarity") or 0)
    rms = float(row.get("rms_dbfs") or -80)
    silence = float(row.get("silence_ratio") or 0)
    duration_score = max(0.0, 1.0 - abs(duration - 18.0) / 18.0)
    level_score = max(0.0, 1.0 - abs(rms + 20.0) / 30.0)
    return 3.0 * within - 1.2 * cross + duration_score + level_score - silence


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path.home() / ".cache" / "speechmatics-speaker-archive" / "ecapa",
    )
    args = parser.parse_args()
    source = args.source.expanduser().resolve()
    paths = sorted(source.glob("*/*.wav"), key=lambda item: (item.parent.name, item.name))
    if not paths:
        raise SystemExit(f"WAV-файлы не найдены: {source}")

    print(f"Найдено {len(paths)} WAV в {len({path.parent.name for path in paths})} профилях")
    model = EncoderClassifier.from_hparams(
        source="speechbrain/spkrec-ecapa-voxceleb",
        savedir=str(args.model_dir),
        run_opts={"device": "cpu"},
    )

    rows: list[dict[str, Any]] = []
    embeddings: dict[str, torch.Tensor] = {}
    for index, path in enumerate(paths, 1):
        relative = path.relative_to(source)
        row: dict[str, Any] = {
            "profile_name": path.parent.name,
            "filename": path.name,
            "source_path": str(path),
            "relative_path": str(relative),
            "file_sha256": sha256_file(path),
            "errors": [],
        }
        try:
            row.update(probe(path))
            waveform, sample_rate = torchaudio.load(str(path))
            if waveform.numel() == 0:
                raise ValueError("декодирован пустой звуковой поток")
            mono = waveform.mean(dim=0, keepdim=True) if waveform.shape[0] > 1 else waveform
            duration = float(mono.shape[1] / sample_rate)
            row["duration_seconds"] = duration
            row.update(signal_metrics(mono, sample_rate))
            row["pcm_sha256"] = normalized_pcm_hash(mono, sample_rate)
            with torch.inference_mode():
                embeddings[str(path)] = normalize_embedding(model.encode_batch(mono))
        except Exception as exc:  # noqa: BLE001 - every bad sample must remain visible in the report
            row["errors"].append(f"Файл не читается: {exc}")
            row.setdefault("duration_seconds", 0.0)
            row.setdefault("pcm_sha256", None)
        rows.append(row)
        if index % 10 == 0 or index == len(paths):
            print(f"Проверено {index}/{len(paths)}")

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[row["profile_name"]].append(row)

    centroids: dict[str, torch.Tensor] = {}
    for name, group in groups.items():
        vectors = [embeddings[row["source_path"]] for row in group if row["source_path"] in embeddings]
        if vectors:
            centroids[name] = normalize_embedding(torch.stack(vectors).mean(dim=0))

    for name, group in groups.items():
        for row in group:
            embedding = embeddings.get(row["source_path"])
            if embedding is None:
                continue
            same = [
                embeddings[other["source_path"]]
                for other in group
                if other is not row and other["source_path"] in embeddings
            ]
            row["within_profile_similarity"] = (
                float(torch.stack([torch.dot(embedding, item) for item in same]).mean())
                if same
                else None
            )
            cross = [
                (float(torch.dot(embedding, centroid)), other_name)
                for other_name, centroid in centroids.items()
                if other_name != name
            ]
            if cross:
                similarity, other_name = max(cross)
                row["closest_other_profile"] = other_name
                row["closest_other_similarity"] = similarity

    by_pcm_hash: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row.get("pcm_sha256"):
            by_pcm_hash[row["pcm_sha256"]].append(row)

    for row in rows:
        issues: list[str] = list(row.pop("errors", []))
        blocked = bool(issues)
        duration = float(row.get("duration_seconds") or 0)
        rms = float(row.get("rms_dbfs") or -240)
        peak = float(row.get("peak_dbfs") or -240)
        silence = float(row.get("silence_ratio") or 0)
        clipping = float(row.get("clipping_ratio") or 0)
        within = row.get("within_profile_similarity")
        cross = row.get("closest_other_similarity")

        if not 5.0 <= duration <= 30.0:
            issues.append(f"Длительность {duration:.1f} сек. вне допуска Speechmatics 5–30 сек.")
            blocked = True
        elif duration < 10:
            issues.append(f"Короткий образец: {duration:.1f} сек.")
        elif duration > 25:
            issues.append(f"Длинный образец: {duration:.1f} сек.")
        if rms < -50:
            issues.append(f"Почти тишина: средняя громкость {rms:.1f} дБ")
            blocked = True
        elif rms < -35:
            issues.append(f"Тихий образец: средняя громкость {rms:.1f} дБ")
        if peak > -0.2 and clipping > 0.001:
            issues.append(f"Возможна перегрузка звука: {clipping * 100:.2f}% отсчётов")
        if silence > 0.5:
            issues.append(f"Много тишины: {silence * 100:.0f}%")
        if within is not None and within < 0.60:
            issues.append(f"Голос отличается от остальных клипов этой папки: {within:.3f}")
        if cross is not None and cross >= 0.80:
            issues.append(
                f"Голос похож на профиль «{row['closest_other_profile']}»: {cross:.3f}; обязательно прослушать"
            )

        duplicate_rows = by_pcm_hash.get(row.get("pcm_sha256"), [])
        if len(duplicate_rows) > 1:
            others = [item for item in duplicate_rows if item is not row]
            other_profiles = sorted({item["profile_name"] for item in others if item["profile_name"] != row["profile_name"]})
            if other_profiles:
                issues.append(f"Один и тот же звук лежит в чужом профиле: {', '.join(other_profiles)}")
                blocked = True
            else:
                issues.append("Точный звуковой дубль внутри профиля")

        row["quality_issues"] = issues
        row["quality_status"] = "blocked" if blocked else ("warning" if issues else "good")
        row["selection_score"] = quality_score(row)
        row["selected_for_enrollment"] = False

    # Select two best non-duplicate, non-blocked clips per profile. All clips stay
    # in the report and UI; selection only controls future paid enrollment.
    for group in groups.values():
        seen_pcm: set[str] = set()
        eligible = sorted(
            (row for row in group if row["quality_status"] != "blocked"),
            key=lambda row: row["selection_score"],
            reverse=True,
        )
        chosen = 0
        for row in eligible:
            pcm_hash = row.get("pcm_sha256")
            if pcm_hash and pcm_hash in seen_pcm:
                continue
            row["selected_for_enrollment"] = True
            if pcm_hash:
                seen_pcm.add(pcm_hash)
            chosen += 1
            if chosen == 2:
                break

    summary = {
        "source": str(source),
        "profiles": len(groups),
        "samples": len(rows),
        "good": sum(row["quality_status"] == "good" for row in rows),
        "warning": sum(row["quality_status"] == "warning" for row in rows),
        "blocked": sum(row["quality_status"] == "blocked" for row in rows),
        "selected": sum(bool(row["selected_for_enrollment"]) for row in rows),
    }
    payload = {"summary": summary, "samples": rows}
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    with args.output_csv.open("w", newline="", encoding="utf-8-sig") as handle:
        fieldnames = [
            "profile_name", "filename", "duration_seconds", "sample_rate", "channels", "bit_depth",
            "codec", "rms_dbfs", "peak_dbfs", "silence_ratio", "clipping_ratio",
            "within_profile_similarity", "closest_other_profile", "closest_other_similarity",
            "quality_status", "selected_for_enrollment", "quality_issues", "source_path",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            csv_row = dict(row)
            csv_row["quality_issues"] = " | ".join(row["quality_issues"])
            writer.writerow(csv_row)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"JSON: {args.output_json}")
    print(f"CSV: {args.output_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
