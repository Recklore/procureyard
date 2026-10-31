"""Export OpenCode V2 sessions to JSONL, in the same spirit as the V1 per-session
.jsonl exports: first line = session metadata, then every raw message row in
sequence order with its original data payload, unedited.

Usage: python scripts/export_opencode_sessions.py [output_dir] [session_id ...]
       (no session ids -> all sessions found in the store)
"""
import json
import os
import sqlite3
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DB = Path(os.environ.get("OPENCODE_DB", Path.home() / ".local/share/opencode/opencode.db"))
OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "ai-logs")
WANTED = set(sys.argv[2:])


def row_dict(cur, row):
    return {d[0]: row[i] for i, d in enumerate(cur.description)}


def main():
    if not DB.exists():
        print(f"session store not found at {DB}")
        sys.exit(1)
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)  # read-only, live-safe
    OUT.mkdir(parents=True, exist_ok=True)

    cur = con.cursor()
    sessions = [row_dict(cur, r) for r in cur.execute("select * from session_v2 order by time_created")]
    for s in sessions:
        if WANTED and s["id"] not in WANTED:
            continue
        sid = s["id"]
        title = s.get("slug") or sid
        out_path = OUT / f"opencode-session-{title}-{sid}.jsonl"
        n = 0
        with open(out_path, "w", encoding="utf-8") as f:
            meta = dict(s)
            f.write(json.dumps({"type": "session", "data": meta}, ensure_ascii=False, sort_keys=True) + "\n")
            mcur = con.cursor()
            for msg in mcur.execute(
                "select id, session_id, type, seq, time_created, time_updated, data "
                "from session_message where session_id = ? order by seq", (sid,)
            ):
                row = {
                    "type": "message",
                    "id": msg[0], "session_id": msg[1], "kind": msg[2], "seq": msg[3],
                    "time_created": msg[4], "time_updated": msg[5],
                    "data": json.loads(msg[6]) if msg[6] is not None else None,
                }
                f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
                n += 1
        print(f"exported {out_path} ({n} messages)")


if __name__ == "__main__":
    main()
