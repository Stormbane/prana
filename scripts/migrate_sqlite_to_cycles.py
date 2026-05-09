"""Export heartbeat SQLite cycles to markdown files in the memory tree.

Reads ``data/heartbeat/memory.db`` and writes one markdown file per
heartbeat cycle to ``~/.narada/heartbeat/cycles/YYYY-MM/YYYY-MM-DD-HHMM-ACTION.md``.
Each file has YAML frontmatter matching the new ``CycleRecord`` schema
plus markdown sections for desire/plan/judgment/result text.

After running this once, the SQLite db can be archived or deleted — the
memory tree becomes the single source of truth.

Usage:
    python scripts/migrate_sqlite_to_cycles.py --dry-run
    python scripts/migrate_sqlite_to_cycles.py
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from prana.heartbeat.cycle_log import CycleRecord, write_cycle
from prana.heartbeat.wake import NARADA_ROOT


DEFAULT_DB = Path("data/heartbeat/memory.db")
CYCLES_ROOT = NARADA_ROOT / "heartbeat" / "cycles"


def _parse_ts(ts: str) -> datetime:
    """Best-effort parse of a SQLite timestamp string."""
    if not ts:
        return datetime.now(timezone.utc)
    # Prefer ISO 8601 (what memory.py writes)
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(ts, fmt)
            return dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return datetime.now(timezone.utc)


def migrate(db_path: Path, *, dry_run: bool) -> dict:
    if not db_path.exists():
        print(f"No database at {db_path}")
        return {"scanned": 0, "written": 0}

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM heartbeats ORDER BY timestamp ASC"
    ).fetchall()
    conn.close()

    written = 0
    skipped = 0
    for row in rows:
        d = dict(row)
        started_dt = _parse_ts(d.get("timestamp", ""))
        record = CycleRecord(
            started=started_dt.isoformat(),
            action=d.get("action", "") or "UNKNOWN",
            topic=d.get("topic", "") or "",
            reason=d.get("reason", "") or "",
            approved=bool(d.get("judgment_approved")),
            revisions=0,   # not tracked in old schema
            cost_usd=float(d.get("cost_usd") or 0.0),
            duration_s=0.0,  # not tracked in old schema
            desire_raw=d.get("raw_desire", "") or "",
            plan_raw=d.get("plan", "") or "",
            judgment_feedback=d.get("judgment_feedback", "") or "",
            result_summary=d.get("result_summary", "") or "",
            result_details=d.get("result_details", "") or "",
        )
        dir_part = f"heartbeat/cycles/{started_dt.strftime('%Y-%m')}"
        file_part = (
            f"{started_dt.strftime('%Y-%m-%d-%H%M')}-"
            f"{record.action}.md"
        )
        path = NARADA_ROOT / dir_part / file_part

        if path.exists():
            skipped += 1
            continue

        if dry_run:
            print(f"WOULD WRITE: {path.relative_to(NARADA_ROOT)}")
        else:
            write_cycle(path, record)
        written += 1

    print(f"\nScanned {len(rows)} rows")
    print(f"Written: {written}{' (dry-run)' if dry_run else ''}")
    print(f"Skipped: {skipped} (already exist)")
    return {"scanned": len(rows), "written": written, "skipped": skipped}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB,
        help=f"SQLite path (default: {DEFAULT_DB})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show what would be written without writing",
    )
    args = parser.parse_args()
    migrate(args.db, dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
