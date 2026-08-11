#!/usr/bin/env python3
"""Fail when a public source tree contains common secrets or runtime data."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FORBIDDEN_NAMES = {".env", "app.db", "chats.db"}
FORBIDDEN_PARTS = {".git", ".venv", "__pycache__", ".pytest_cache"}
RUNTIME_ROOTS = {"audio", "data", "storage"}
ALLOWED_RUNTIME_FILES = {
    Path("audio/.gitkeep"),
    Path("data/.gitkeep"),
    Path("storage/samples/.gitkeep"),
    Path("storage/prepared/.gitkeep"),
    Path("storage/transcripts/.gitkeep"),
    Path("storage/exports/.gitkeep"),
    Path("storage/enrichment/.gitkeep"),
    Path("storage/clips/.gitkeep"),
}
PATTERNS = {
    "OpenRouter key": re.compile(r"sk-or-v1-[A-Za-z0-9_-]{20,}"),
    "Hugging Face token": re.compile(r"hf_[A-Za-z0-9]{20,}"),
    "Google API key": re.compile(r"AIza[A-Za-z0-9_-]{20,}"),
    "Meta model key": re.compile(r"LLM_[A-Za-z0-9_-]{20,}"),
    "private home path": re.compile(r"/(?:var/)?home/[A-Za-z0-9._-]+/"),
}


def main() -> int:
    findings: list[str] = []
    checked = 0
    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(ROOT)
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            continue
        if path.name in FORBIDDEN_NAMES:
            findings.append(f"forbidden file: {relative}")
            continue
        if relative.parts[0] in RUNTIME_ROOTS and relative not in ALLOWED_RUNTIME_FILES:
            findings.append(f"runtime artifact: {relative}")
            continue
        if path.stat().st_size > 5 * 1024 * 1024:
            findings.append(f"unexpected large file: {relative}")
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append(f"unexpected binary file: {relative}")
            continue
        checked += 1
        for label, pattern in PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{label}: {relative}")
    if findings:
        print("Public-tree check failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        return 1
    print(f"OK: checked {checked} text files; no secrets or runtime artifacts found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
