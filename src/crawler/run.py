"""`make crawl` entrypoint: discovery -> selective detail fetch -> removal checks -> dedupe -> export."""
import logging
import os
import signal
import sys
import time
from datetime import datetime, timedelta, timezone

from crawler.boards.base import Board
from crawler.boards.indeed import IndeedBoard
from crawler.boards.linkedin import LinkedInBoard
from crawler.boards.reed import ReedBoard
from crawler.db import connect, migrate
from crawler.fetch.client import Client
from crawler.store.export import export_jobs, write_report
from crawler.store.reconcile import Counts, Run, apply_run

log = logging.getLogger("crawler")
ALL_BOARDS: list[Board] = [ReedBoard(), LinkedInBoard(), IndeedBoard()]
_only = {b for b in os.environ.get("CRAWL_BOARDS", "").split(",") if b}
BOARDS = [b for b in ALL_BOARDS if not _only or b.name in _only]
BATCH = 25
USD_PER_GB = float(os.environ.get("PROXY_USD_PER_GB", "5"))
STALE_AFTER = timedelta(days=2)       # not seen this long -> verify before trusting liveness
STALE_CHECKS_PER_BOARD = int(os.environ.get("CRAWL_STALE_CHECKS", "500"))


class Budget:
    """Global caps (time, proxy bytes) split evenly over the boards still to run; per-board request cap."""

    def __init__(self, n_boards):
        self.deadline = time.time() + float(os.environ.get("CRAWL_MAX_MINUTES", "90")) * 60
        self.max_bytes = float(os.environ.get("CRAWL_MAX_GB", "2")) * 1e9
        self.max_requests = int(os.environ.get("CRAWL_MAX_REQUESTS_PER_BOARD", "6000"))
        self.n_boards = n_boards
        self.stop_flag = False
        self.board_deadline = self.deadline
        self.board_bytes_limit = self.max_bytes

    def start_board(self, index, client):
        left = self.n_boards - index
        now = time.time()
        self.board_deadline = now + max(0.0, self.deadline - now) / left
        self.board_bytes_limit = client.bytes_total + max(0.0, self.max_bytes - client.bytes_total) / left

    def exhausted(self, client, board_start_requests) -> bool:
        return (self.stop_flag or time.time() > self.board_deadline
                or client.bytes_total > self.board_bytes_limit
                or client.requests_total - board_start_requests > self.max_requests)


def _load_known(conn, source):
    rows = conn.execute("SELECT source_job_id, source_signal, next_detail_due_at, removed_at, last_seen_at"
                        " FROM jobs WHERE source=%s", (source,)).fetchall()
    skipped = {r[0] for r in conn.execute("SELECT source_job_id FROM skipped WHERE source=%s", (source,))}
    return {r[0]: r[1:] for r in rows}, skipped


class Batch:
    """Collects observations for one board and applies them through the shared apply_run()."""

    def __init__(self, conn, source, totals, run_id):
        self.conn, self.source, self.totals, self.run_id = conn, source, totals, run_id
        self.discovered, self.details, self.failed, self.removed = [], [], [], []
        self.reasons, self.events = {}, []

    def event(self, jid, outcome, reason):
        """Record why the crawler decided what it did (removed / failed / skipped / verified_live)."""
        self.events.append((self.run_id, self.source, jid, outcome, reason))

    def remove(self, jid, reason):
        self.removed.append(jid)
        self.reasons[str(jid)] = reason
        self.event(jid, "removed", reason)

    def __len__(self):
        return len(self.discovered) + len(self.details) + len(self.removed)

    def flush(self):
        if not (self.discovered or self.details or self.failed or self.removed or self.events):
            return
        self.totals.add(apply_run(self.conn, Run(
            observed_at=datetime.now(timezone.utc), source=self.source, discovered_ids=self.discovered,
            details=self.details, failed_ids=self.failed, removed_ids=self.removed,
            removed_reasons=self.reasons)))
        if self.events:
            self.conn.cursor().executemany(
                "INSERT INTO job_events (run_id, source, source_job_id, outcome, reason) VALUES (%s,%s,%s,%s,%s)",
                self.events)
            self.conn.commit()
        self.discovered, self.details, self.failed, self.removed = [], [], [], []
        self.reasons, self.events = {}, []


def crawl_board(board, client, conn, run_id, run_start, budget, totals: Counts, stats):
    known, skipped = _load_known(conn, board.name)
    start_req = client.requests_total
    stop = lambda: budget.exhausted(client, start_req)
    batch, now = Batch(conn, board.name, totals, run_id), datetime.now(timezone.utc)

    def handle(res, jid):
        if res.kind == "job":
            batch.details.append(res.job)
        elif res.kind in ("expired", "gone"):
            batch.remove(jid, f"{res.kind}:{res.reason}")
        elif res.kind == "skip":
            if res.permanent:                 # never fetch this listing again
                conn.execute("INSERT INTO skipped (source, source_job_id, reason) VALUES (%s,%s,%s)"
                             " ON CONFLICT DO NOTHING", (board.name, jid, res.reason))
                conn.commit()
            batch.event(jid, "skipped", res.reason)
            stats["skipped"] += 1
        else:
            batch.failed.append(jid)
            batch.event(jid, "failed", res.reason)
            stats["failed"] += 1

    for card in board.cards(client, stop):
        jid = card.source_job_id
        if jid in skipped:
            continue
        batch.discovered.append(jid)
        k = known.get(jid)
        if card.inline is not None:          # content arrived with the search result: free to apply
            handle(card.inline, jid)
        elif k is None:
            if stop():
                break
            handle(board.detail(client, card), jid)
        else:
            signal_old, due, removed_at, _ = k
            if card.expired:
                batch.remove(jid, "expired:card_expiry_date")
            elif removed_at is not None or card.signal != signal_old or (due and due <= now):
                if not stop():               # refresh due: re-fetch detail (change signal or periodic)
                    handle(board.detail(client, card), jid)
        if len(batch) >= BATCH:
            batch.flush()
    batch.flush()

    # Removal verification: absence from search is NOT evidence, so re-fetch stale live rows.
    stale = conn.execute(
        "SELECT source_job_id, url FROM jobs WHERE source=%s AND removed_at IS NULL AND last_seen_at < %s"
        " ORDER BY last_seen_at LIMIT %s", (board.name, run_start - STALE_AFTER, STALE_CHECKS_PER_BOARD)).fetchall()
    for jid, url in stale:
        if budget.stop_flag or time.time() > budget.deadline:
            break
        res = board.check(client, jid, url)
        if res.kind == "job":
            batch.discovered.append(jid)     # still live
            batch.event(jid, "verified_live", "recheck_ok")
        if res.kind != "skip":
            handle(res, jid)
        if len(batch) >= BATCH:
            batch.flush()
    batch.flush()


def build_report(conn, run_id, started, finished, totals, duplicates, client):
    by_status = {str(s): n for s, n in conn.execute(
        "SELECT status, count(*) FROM requests WHERE run_id=%s GROUP BY status ORDER BY status", (run_id,))}
    blocked = conn.execute("SELECT count(*) FROM requests WHERE run_id=%s AND blocked", (run_id,)).fetchone()[0]
    reasons = {r or "": n for r, n in conn.execute(
        "SELECT block_reason, count(*) FROM requests WHERE run_id=%s AND blocked GROUP BY 1", (run_id,))}
    total, nbytes = conn.execute("SELECT count(*), coalesce(sum(bytes),0) FROM requests WHERE run_id=%s",
                                 (run_id,)).fetchone()
    by_source = dict(conn.execute("SELECT source, count(*) FROM jobs WHERE removed_at IS NULL GROUP BY source"))
    decisions = {}
    for outcome, reason, n in conn.execute(
            "SELECT outcome, coalesce(reason,''), count(*) FROM job_events WHERE run_id=%s GROUP BY 1,2 ORDER BY 1,2",
            (run_id,)):
        decisions.setdefault(outcome, {})[reason] = n
    requests_by_source = {}
    for src, n, blk in conn.execute(
            "SELECT source, count(*), count(*) FILTER (WHERE blocked) FROM requests WHERE run_id=%s GROUP BY 1",
            (run_id,)):
        requests_by_source[src] = {"requests": n, "blocked": blk}
    return {
        "started_at": started.isoformat(), "finished_at": finished.isoformat(),
        "requests_total": total, "requests_blocked": blocked, "requests_by_status": by_status,
        "blocked_reasons": reasons, "jobs_by_source": by_source, "decision_reasons": decisions,
        "requests_by_source": requests_by_source,
        "duplicates_linked": duplicates, "jobs_new": totals.jobs_new,
        "jobs_removed": totals.jobs_removed, "jobs_changed": totals.jobs_changed,
        "estimated_proxy_gb": round(float(nbytes) / 1e9, 4),
        "estimated_cost_usd": round(float(nbytes) / 1e9 * USD_PER_GB, 4),
    }


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    conn = connect()
    migrate(conn)
    started = datetime.now(timezone.utc)
    run_id = conn.execute("INSERT INTO crawl_runs (started_at) VALUES (%s) RETURNING id", (started,)).fetchone()[0]
    conn.commit()
    budget = Budget(len(BOARDS))
    signal.signal(signal.SIGINT, lambda *_: setattr(budget, "stop_flag", True))
    signal.signal(signal.SIGTERM, lambda *_: setattr(budget, "stop_flag", True))
    client = Client(connect(), run_id)
    totals, status, duplicates = Counts(), "completed", 0
    try:
        for i, board in enumerate(BOARDS):
            if budget.stop_flag:
                break
            budget.start_board(i, client)
            stats = {"skipped": 0, "failed": 0}
            log.info("crawling %s", board.name)
            try:
                crawl_board(board, client, conn, run_id, started, budget, totals, stats)
            except Exception:
                log.exception("board %s failed; keeping data saved so far", board.name)
                status = "partial"
            log.info("%s done: %s requests so far, %.1f MB", board.name, client.requests_total,
                     client.bytes_total / 1e6)
        if budget.stop_flag or time.time() > budget.deadline:
            status = "partial"
        try:
            from crawler.dedupe.canonical import link_duplicates
            duplicates = link_duplicates(conn)
        except ImportError:
            pass
    finally:
        finished = datetime.now(timezone.utc)
        conn.execute("UPDATE crawl_runs SET finished_at=%s, status=%s WHERE id=%s", (finished, status, run_id))
        conn.commit()
        n = export_jobs(conn)
        write_report(build_report(conn, run_id, started, finished, totals, duplicates, client))
        log.info("exported %d jobs; status=%s", n, status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
