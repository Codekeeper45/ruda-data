#!/usr/bin/env python3
"""Вывести реплики раунда по фазам с именами спикеров (для ручного анализа)."""
import sqlite3
import sys

DB = "data/app.db"


def fmt(t: float) -> str:
    m = int(t // 60)
    s = t - m * 60
    return f"{m}:{s:05.1f}"


def main() -> None:
    round_id = int(sys.argv[1])
    phase_filter = sys.argv[2] if len(sys.argv) > 2 else None
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    r = cur.execute(
        "SELECT * FROM mafia_rounds WHERE id=?", (round_id,)
    ).fetchone()
    if not r:
        print("round not found")
        return
    v = cur.execute(
        "SELECT title FROM videos WHERE id=?", (r["video_id"],)
    ).fetchone()
    print(
        f"=== round {round_id} | video {r['video_id']} num {r['round_number']} | "
        f"{v['title'][:80]}"
    )
    print(f"    window {fmt(r['start_time'])} - {fmt(r['end_time'])} | winner {r['winning_faction']} | status {r['review_status']}")

    speakers = {
        s["id"]: s["display_name"]
        for s in cur.execute(
            "SELECT id, display_name FROM video_speakers WHERE video_id=?",
            (r["video_id"],),
        ).fetchall()
    }

    phases = cur.execute(
        "SELECT * FROM mafia_phases WHERE round_id=? ORDER BY COALESCE(phase_number,0), start_time",
        (round_id,),
    ).fetchall()
    for p in phases:
        if phase_filter and phase_filter not in (p["phase_type"], str(p["phase_number"])):
            continue
        print(f"\n--- phase {p['id']} type={p['phase_type']} #{p['phase_number']} "
              f"[{fmt(p['start_time'])} - {fmt(p['end_time'])}] status={p['review_status']}")
        us = cur.execute(
            """SELECT u.id, u.start_time, u.end_time, u.speaker_id, u.text
               FROM utterances u
               WHERE u.video_id=? AND u.start_time >= ? AND u.end_time <= ?
               ORDER BY u.start_time""",
            (r["video_id"], p["start_time"], p["end_time"]),
        ).fetchall()
        for u in us:
            spk = speakers.get(u["speaker_id"], f"spk{u['speaker_id']}")
            print(f"  [{fmt(u['start_time'])}] {spk}: {u['text'][:300]}")


if __name__ == "__main__":
    main()
