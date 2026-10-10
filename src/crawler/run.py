"""`make crawl` entrypoint: discovery -> selective detail fetch -> removal checks -> dedupe -> export."""
import logging
import os
import signal
import sys
import threading
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone

from crawler.boards.base import Board, DetailResult
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


# Concurrent detail fetches per board (override: CRAWL_WORKERS or CRAWL_WORKERS_<BOARD>).
DEFAULT_WORKERS = {"reed": 8, "linkedin": 8, "indeed": 4}


def workers_for(name: str) -> int:
    v = os.environ.get(f"CRAWL_WORKERS_{name.upper()}") or os.environ.get("CRAWL_WORKERS")
    return max(1, int(v)) if v else DEFAULT_WORKERS.get(name, 4)


class Budget:
    """Global caps (wall-clock time, proxy bytes) shared by the boards, which run concurrently;
    per-board request cap."""

    def __init__(self, n_boards):
        self.deadline = time.time() + float(os.environ.get("CRAWL_MAX_MINUTES", "90")) * 60
        self.max_bytes = float(os.environ.get("CRAWL_MAX_GB", "2")) * 1e9
        self.max_requests = int(os.environ.get("CRAWL_MAX_REQUESTS_PER_BOARD", "6000"))
        self.n_boards = n_boards
        self.stop_flag = False

    def exhausted(self, client, board) -> bool:
        return (self.stop_flag or time.time() > self.deadline
                or client.bytes_total > self.max_bytes
                or client.requests_by_source[board] > self.max_requests)


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
        self.conn.commit()                    # per-board connection: persist each batch, not just at exit
        if self.events:
            self.conn.cursor().executemany(
                "INSERT INTO job_events (run_id, source, source_job_id, outcome, reason) VALUES (%s,%s,%s,%s,%s)",
                self.events)
            self.conn.commit()
        self.discovered, self.details, self.failed, self.removed = [], [], [], []
        self.reasons, self.events = {}, []


def crawl_board(board, client, conn, run_id, run_start, budget, totals: Counts, stats):
    known, skipped = _load_known(conn, board.name)
    stop = lambda: budget.exhausted(client, board.name)
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

    n_workers = workers_for(board.name)
    pool = ThreadPoolExecutor(n_workers, thread_name_prefix=board.name)
    pending = {}                              # future -> (job id, is_recheck); DB work stays on this thread

    def finish(f):
        jid, recheck = pending.pop(f)
        try:
            res = f.result()
        except Exception:
            log.exception("%s detail fetch crashed for %s", board.name, jid)
            res = DetailResult("failed", reason="exception")
        if recheck:
            if res.kind == "job":
                batch.discovered.append(jid)     # still live
                batch.event(jid, "verified_live", "recheck_ok")
            if res.kind != "skip":
                handle(res, jid)
        else:
            handle(res, jid)
        if len(batch) >= BATCH:
            batch.flush()

    def settle(max_pending):
        while len(pending) > max_pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for f in done:
                finish(f)
        for f in [f for f in pending if f.done()]:
            finish(f)

    def submit(fn, jid, recheck=False):
        pending[pool.submit(fn)] = (jid, recheck)
        settle(2 * n_workers)

    try:
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
                submit(lambda c=card: board.detail(client, c), jid)
            else:
                signal_old, due, removed_at, _ = k
                if card.expired:
                    batch.remove(jid, "expired:card_expiry_date")
                elif removed_at is not None or card.signal != signal_old or (due and due <= now):
                    if not stop():               # refresh due: re-fetch detail (change signal or periodic)
                        submit(lambda c=card: board.detail(client, c), jid)
            if len(batch) >= BATCH:
                batch.flush()
        settle(0)
        batch.flush()

        # Removal verification: absence from search is NOT evidence, so re-fetch stale live rows.
        stale = conn.execute(
            "SELECT source_job_id, url FROM jobs WHERE source=%s AND removed_at IS NULL AND last_seen_at < %s"
            " ORDER BY last_seen_at LIMIT %s",
            (board.name, run_start - STALE_AFTER, STALE_CHECKS_PER_BOARD)).fetchall()
        for jid, url in stale:
            if budget.stop_flag or time.time() > budget.deadline:
                break
            submit(lambda j=jid, u=url: board.check(client, j, u), jid, recheck=True)
        settle(0)
        batch.flush()
    finally:
        pool.shutdown(wait=True)


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
        results = {}

        def run_board(board):
            board_conn, board_totals = connect(), Counts()
            stats = {"skipped": 0, "failed": 0}
            log.info("crawling %s with %d workers", board.name, workers_for(board.name))
            try:
                crawl_board(board, client, board_conn, run_id, started, budget, board_totals, stats)
                results[board.name] = (board_totals, True)
            except Exception:
                log.exception("board %s failed; keeping data saved so far", board.name)
                results[board.name] = (board_totals, False)
            finally:
                board_conn.close()
            log.info("%s done: %s requests, %.1f MB total so far", board.name,
                     client.requests_by_source[board.name], client.bytes_total / 1e6)

        threads = [threading.Thread(target=run_board, args=(b,), name=b.name) for b in BOARDS]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for board_totals, ok in results.values():
            totals.add(board_totals)
            if not ok:
                status = "partial"
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
