import json
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import psycopg

EXPORT_FIELDS = ["id", "source", "source_job_id", "url", "title", "company", "company_logo_url",
                 "location", "work_type", "salary_min", "salary_max", "salary_period", "posted_at",
                 "description", "canonical_job_id", "content_hash", "first_seen_at", "last_seen_at",
                 "removed_at"]


def _default(o):
    if isinstance(o, datetime):
        return o.isoformat()
    if isinstance(o, Decimal):
        return float(o) if o % 1 else int(o)
    raise TypeError(type(o))


def export_jobs(conn, out_dir="output") -> int:
    Path(out_dir).mkdir(exist_ok=True)
    n = 0
    tmp = Path(out_dir) / "jobs.jsonl.tmp"
    with conn.cursor(name="export") as cur, open(tmp, "w") as f:
        cur.execute(f"SELECT {','.join(EXPORT_FIELDS)} FROM jobs ORDER BY id")
        for row in cur:
            f.write(json.dumps(dict(zip(EXPORT_FIELDS, row)), default=_default, ensure_ascii=False) + "\n")
            n += 1
    tmp.replace(Path(out_dir) / "jobs.jsonl")
    return n


def write_report(report: dict, out_dir="output"):
    Path(out_dir).mkdir(exist_ok=True)
    (Path(out_dir) / "report.json").write_text(json.dumps(report, indent=2, default=_default))
