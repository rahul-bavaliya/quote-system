"""Quote log: every quote the API gives is saved so actual hours can be recorded later and
accuracy tracked. SQLite keeps it separate from the read-only statistics database."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path


class QuoteStore:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.db = sqlite3.connect(str(path), check_same_thread=False, timeout=30)
        self.db.execute("pragma journal_mode=WAL")      # readers never block the writer
        self.db.execute("pragma busy_timeout=30000")    # several workers share one file
        self.db.execute("""create table if not exists quotes (
            id text primary key, created_at text, make text, model text, job_key text,
            p50_hours real, p80_hours real, request text, response text,
            actual_hours real, actual_notes text, actual_at text)""")
        self.db.commit()

    def save(self, request: dict, response: dict) -> str:
        qid = uuid.uuid4().hex[:12]
        total = response.get("total") or {}
        with self._lock:
            self.db.execute("insert into quotes (id, created_at, make, model, job_key, p50_hours, p80_hours, request, response) "
                            "values (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (qid, datetime.now(UTC).isoformat(timespec="seconds"),
                             response["machine"]["make"], response["machine"]["model"], "+".join(sorted(response["jobs"])) or (
                                 "PARTS:" + ",".join(k["part_number"] for k in response["parts_query"])
                                 if response.get("parts_query") else "SIMILAR:" + (response.get("query") or "")),
                             total.get("p50_hours"), total.get("p80_hours"), json.dumps(request), json.dumps(response)))
            self.db.commit()
        return qid

    def get(self, qid: str) -> dict | None:
        with self._lock:
            row = self.db.execute("select id, created_at, response, actual_hours, actual_notes, actual_at "
                                  "from quotes where id=?", (qid,)).fetchone()
        if not row:
            return None
        return {"quote_id": row[0], "created_at": row[1], **json.loads(row[2]),
                "actual": None if row[3] is None else {"actual_hours": row[3], "notes": row[4], "recorded_at": row[5]}}

    def record_actual(self, qid: str, hours: float, notes: str | None) -> bool:
        with self._lock:
            cur = self.db.execute("update quotes set actual_hours=?, actual_notes=?, actual_at=? where id=?",
                                  (hours, notes, datetime.now(UTC).isoformat(timespec="seconds"), qid))
            self.db.commit()
        return cur.rowcount > 0

    def make_entries(self, limit: int = 200) -> list[dict]:
        """Make spellings that were not an exact/alias match, most frequent first."""
        with self._lock:
            rows = self.db.execute("""
                select coalesce(json_extract(response, '$.machine.make_entered'), '') as entered,
                       json_extract(response, '$.machine.make') as read_as,
                       json_extract(response, '$.machine.make_resolved_by') as how,
                       count(*), max(created_at)
                from quotes
                where json_extract(response, '$.machine.make_resolved_by')
                      in ('word', 'fuzzy', 'from_model', 'unknown', 'empty')
                group by 1, 2, 3 order by 4 desc, 5 desc limit ?""", (limit,)).fetchall()
        return [{"entered": e, "read_as": r, "how": h, "quotes": n, "last_seen": ls} for e, r, h, n, ls in rows]

    def accuracy(self) -> dict:
        with self._lock:
            return self._accuracy()

    def _accuracy(self) -> dict:
        r = self.db.execute("""select count(*), avg(abs(actual_hours - p50_hours)),
                                      avg(case when actual_hours <= p80_hours then 1.0 else 0.0 end)
                               from quotes where actual_hours is not null and p50_hours is not null""").fetchone()
        total = self.db.execute("select count(*) from quotes").fetchone()[0]
        return {"quotes": total, "with_actuals": r[0], "mae_hours_vs_p50": r[1], "share_within_p80": r[2]}
