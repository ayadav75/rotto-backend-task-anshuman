"""The single save path for jobs. `make crawl` and `make test` both call apply_run().

A run is one batch of normalised observations from one board:
  discovered_ids  listings seen live (advance last_seen_at; clear removed_at on reappearance)
  details         detail pages fetched successfully (insert new rows, update changed content)
  failed_ids      failed fetches: NO evidence, change nothing
  removed_ids     explicit removal evidence (expired marker, confirmed 404/410): the ONLY way to set removed_at

Absence from discovery is never removal evidence. Applying the same run twice changes nothing.
"""
import hashlib
import os
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlsplit

REFRESH_DAYS = 7
HASHED = ["url", "title", "company", "company_logo_url", "location", "work_type",
          "salary_min", "salary_max", "salary_period", "posted_at", "description"]
COLUMNS = ["id", "source", "source_job_id", "url", "title", "company", "company_logo_url",
           "location", "work_type", "salary_min", "salary_max", "salary_period",
           "posted_at", "description", "canonical_job_id", "content_hash", "source_signal"]
NOT_UPDATED = {"id", "source", "source_job_id", "canonical_job_id", "content_hash"}


@dataclass
class Run:
    observed_at: datetime | str
    source: str
    discovered_ids: list = field(default_factory=list)
    details: list = field(default_factory=list)
    failed_ids: list = field(default_factory=list)
    removed_ids: list = field(default_factory=list)


@dataclass
class Counts:
    jobs_new: int = 0
    jobs_changed: int = 0
    jobs_removed: int = 0

    def add(self, other: "Counts"):
        self.jobs_new += other.jobs_new
        self.jobs_changed += other.jobs_changed
        self.jobs_removed += other.jobs_removed

    def as_dict(self):
        return {"jobs_new": self.jobs_new, "jobs_changed": self.jobs_changed,
                "jobs_removed": self.jobs_removed}


def parse_ts(ts):
    if ts is None or isinstance(ts, datetime):
        return ts
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _norm(field_name, value):
    if value is None:
        return None
    if field_name in ("salary_min", "salary_max"):
        return format(Decimal(str(value)).normalize(), "f")
    if field_name in ("url", "company_logo_url"):
        return urlsplit(value)._replace(query="", fragment="").geturl()   # drop tracking / expiry tokens
    if field_name == "posted_at":
        return _utc(value).isoformat()
    if isinstance(value, str):
        return re.sub(r"\s+", " ", value).strip()
    return value


def content_hash(job: dict) -> str:
    return hashlib.sha256(json.dumps([_norm(f, job.get(f)) for f in HASHED]).encode()).hexdigest()


def _utc(ts):
    return parse_ts(ts).astimezone(timezone.utc)


def apply_run(conn, run: Run) -> Counts:
    at = _utc(run.observed_at)
    counts = Counts()
    removed = {f"{run.source}:{i}" for i in run.removed_ids}
    with conn.transaction():
        for sid in run.discovered_ids:
            jid = f"{run.source}:{sid}"
            if jid in removed:
                continue
            conn.execute("UPDATE jobs SET last_seen_at=GREATEST(last_seen_at,%s), removed_at=NULL"
                         " WHERE id=%s", (at, jid))
        for job in run.details:
            job = {**job, "source": job.get("source") or run.source}
            job["id"] = f"{job['source']}:{job['source_job_id']}"
            _apply_detail(conn, job, at, counts, revive=job["id"] not in removed)
        for jid in removed:
            cur = conn.execute("UPDATE jobs SET removed_at=%s WHERE id=%s AND removed_at IS NULL", (at, jid))
            counts.jobs_removed += cur.rowcount
    return counts


def _apply_detail(conn, job, at, counts, revive):
    due = at + timedelta(days=REFRESH_DAYS)
    row = conn.execute("SELECT content_hash, posted_at FROM jobs WHERE id=%s FOR UPDATE",
                       (job["id"],)).fetchone()
    old_hash, old_posted = row if row else (None, None)
    if row and os.environ.get("CHANGE_DEBUG"):
        _log_diff(conn, job)
    # an estimated posted_at ("3d ago") is frozen once set: it must not look like a content change
    if row and old_posted and (job.get("posted_at_estimated") or job.get("posted_at") is None):
        job["posted_at"] = old_posted
    h = content_hash(job)
    vals = {c: job.get(c) for c in COLUMNS}
    vals.update(content_hash=h, at=at, due=due)
    if row is None:
        conn.execute(
            f"INSERT INTO jobs ({','.join(COLUMNS)}, first_seen_at, last_seen_at, last_detail_fetched_at,"
            f" next_detail_due_at) VALUES ({','.join('%(' + c + ')s' for c in COLUMNS)},"
            " %(at)s, %(at)s, %(at)s, %(due)s)", vals)
        counts.jobs_new += 1
        return
    if old_hash != h:
        counts.jobs_changed += 1
    sets = [c for c in COLUMNS if c not in NOT_UPDATED]
    conn.execute(
        "UPDATE jobs SET " + ",".join(f"{c}=%({c})s" for c in sets) +
        ", content_hash=%(content_hash)s, last_seen_at=GREATEST(last_seen_at,%(at)s),"
        " last_detail_fetched_at=%(at)s, next_detail_due_at=%(due)s"
        + (", removed_at=NULL" if revive else "") + " WHERE id=%(id)s", vals)


def _log_diff(conn, job):
    """Debug aid (CHANGE_DEBUG=1): print which hashed fields differ from the stored row."""
    cur = conn.execute(f"SELECT {','.join(HASHED)} FROM jobs WHERE id=%s", (job["id"],))
    old = dict(zip(HASHED, cur.fetchone()))
    diff = [f for f in HASHED if _norm(f, old[f]) != _norm(f, job.get(f))]
    if diff:
        print("CHANGE", job["id"], diff, {f: (str(_norm(f, old[f]))[:40], str(_norm(f, job.get(f)))[:40]) for f in diff if f != "description"})
