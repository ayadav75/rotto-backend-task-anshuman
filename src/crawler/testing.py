"""Offline lifecycle test: replays fixture runs through apply_run() into a fresh, isolated database."""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

from crawler.db import connect, migrate
from crawler.store.reconcile import Run, apply_run

TEST_DB = "jobs_test"
COUNT_KEYS = ("jobs_new", "jobs_changed", "jobs_removed")
EDGE_FIXTURE = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "edge_cases.json"


def _admin_url():
    return os.environ.get("ADMIN_DATABASE_URL", "postgresql://crawler:crawler@localhost:5432/postgres")


def _fresh_db():
    with psycopg.connect(_admin_url(), autocommit=True) as admin:
        admin.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
        admin.execute(f"CREATE DATABASE {TEST_DB}")
    return _admin_url().rsplit("/", 1)[0] + "/" + TEST_DB


def _eq(actual, expected):
    if isinstance(actual, datetime):
        return actual == datetime.fromisoformat(str(expected).replace("Z", "+00:00"))
    if actual is not None and not isinstance(actual, (str, bool)) and isinstance(expected, (int, float)):
        return float(actual) == float(expected)
    return actual == expected


def _to_run(r: dict) -> Run:
    return Run(observed_at=r["observed_at"], source=r["source"],
               discovered_ids=r.get("discovered_source_job_ids", []), details=r.get("details", []),
               failed_ids=r.get("failed_detail_source_job_ids", []),
               removed_ids=r.get("confirmed_removed_source_job_ids", []))


def _snapshot(conn):
    conn.row_factory = dict_row
    try:
        return {r["id"]: r for r in conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()}
    finally:
        conn.row_factory = psycopg.rows.tuple_row


def run_fixture(path, with_dedupe=False) -> list[str]:
    fixture = json.load(open(path))
    conn = connect(_fresh_db())
    migrate(conn)
    errors, hashes = [], {}          # hashes[run name][row id] = content_hash after that run
    for i, r in enumerate(fixture["runs"], 1):
        name = r.get("name", f"run{i}")
        tag = f"{Path(path).name}:{name}"
        counts = apply_run(conn, _to_run(r)).as_dict()
        rows = _snapshot(conn)
        hashes[name] = {k: v["content_hash"] for k, v in rows.items()}
        expect = r.get("expected", {})
        for k, v in expect.get("report", {}).items():
            if counts.get(k) != v:
                errors.append(f"{tag}: report.{k} = {counts.get(k)}, expected {v}")
        if "rows" in expect:
            if set(rows) != set(expect["rows"]):
                errors.append(f"{tag}: stored rows {sorted(rows)} != expected {sorted(expect['rows'])}")
            for jid, want in expect["rows"].items():
                have = rows.get(jid)
                if have is None:
                    continue
                for k, v in want.items():
                    if k == "content_hash_equals_run":
                        earlier = [hashes[n].get(jid) for n in list(hashes)[:-1]]
                        ok = have["content_hash"] == hashes[v].get(jid) if v else have["content_hash"] not in earlier
                        if not ok:
                            errors.append(f"{tag}: {jid} content_hash lineage != {v!r}")
                    elif not _eq(have.get(k), v):
                        errors.append(f"{tag}: {jid}.{k} = {have.get(k)!r}, expected {v!r}")
        # idempotency: applying the same run again changes nothing and counts nothing
        again = apply_run(conn, _to_run(r)).as_dict()
        if any(again.values()):
            errors.append(f"{tag}: second application counted {again}")
        if _snapshot(conn) != rows:
            errors.append(f"{tag}: second application changed stored rows")
        print(f"{tag}: {counts}")
    if with_dedupe:
        errors += dedupe_checks(conn)
    return errors


def dedupe_checks(conn) -> list[str]:
    """Same job on two boards with different wording must link; different jobs must not; ids stay stable."""
    from crawler.dedupe.canonical import link_duplicates
    base = {"location": "Manchester, England, United Kingdom", "salary_min": None, "salary_max": None}
    jobs = [
        {"id": "reed:900", "title": "Senior Software Engineer", "company": "Acme Ltd",
         "description": "Join our platform team building payment services in Python and Postgres. You will design APIs, "
                        "mentor juniors and own delivery. We offer hybrid working, pension and 25 days holiday."},
        {"id": "linkedin:900", "title": "Senior Software Engineer (Python)", "company": "ACME Limited",
         "description": "Join our platform team to build payment services using Python and Postgres. You will design APIs, "
                        "mentor junior engineers and own delivery. We offer hybrid working, a pension and 25 days holiday."},
        {"id": "indeed:900", "title": "Care Assistant", "company": "Acme Ltd",
         "description": "Provide personal care to residents in our home. Night shifts available. Training provided."},
    ]
    for j in jobs:
        conn.execute("INSERT INTO jobs (id, source, source_job_id, url, title, company, location, description,"
                     " content_hash, first_seen_at, last_seen_at) VALUES (%s,%s,%s,'u',%s,%s,%s,%s,'h',now(),now())",
                     (j["id"], j["id"].split(":")[0], j["id"].split(":")[1], j["title"], j["company"],
                      base["location"], j["description"]))
    conn.commit()
    errs = []
    n = link_duplicates(conn)
    c = dict(conn.execute("SELECT id, canonical_job_id FROM jobs WHERE id LIKE '%:900'").fetchall())
    if c["reed:900"] != c["linkedin:900"]:
        errs.append("dedupe: same job on reed+linkedin not linked")
    if c["indeed:900"] == c["reed:900"]:
        errs.append("dedupe: different job wrongly linked")
    if n != 1:
        errs.append(f"dedupe: duplicates_linked = {n}, expected 1")
    before = dict(c)
    link_duplicates(conn)
    if dict(conn.execute("SELECT id, canonical_job_id FROM jobs WHERE id LIKE '%:900'").fetchall()) != before:
        errs.append("dedupe: canonical ids not stable across reruns")
    print("dedupe checks:", "OK" if not errs else "FAILED")
    return errs


def main() -> int:
    paths = [os.environ.get("FIXTURE_PATH", "tests/fixtures/lifecycle.json")]
    if EDGE_FIXTURE.exists():
        paths.append(str(EDGE_FIXTURE))
    errors = []
    for n, p in enumerate(paths):
        errors += run_fixture(p, with_dedupe=(n == 0))
    for e in errors:
        print("FAIL", e)
    print("FAILED" if errors else "OK")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
