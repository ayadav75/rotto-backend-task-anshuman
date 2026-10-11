# UK job crawler: Reed, LinkedIn, Indeed → Postgres

```
docker compose up -d      # Postgres
make crawl                # PROXY_URL or PROXY_HOST/PORT/USERNAME/PASSWORD from the environment
make test                 # offline lifecycle fixtures, no proxy or internet (FIXTURE_PATH=... to swap)
```
Run `make crawl` again for an incremental run. Every run rewrites `output/jobs.jsonl` (all rows, removed ones included) and `output/report.json`.

**Note:** `output/jobs.jsonl` is stored with Git LFS because it is over GitHub's 100 MB file limit. After cloning, run `git lfs install && git lfs pull` (or clone with LFS installed) to get the real file instead of the pointer. `output/report.json` is a normal file.

## How it works
`boards/` only fetch and parse, `store/reconcile.py` is the single save path (live crawl and `make test` both call `apply_run()`), `dedupe/` links copies. Boards run concurrently with a worker pool each.

- **Reed:** `__NEXT_DATA__` JSON in search and detail pages. Search is sliced by sector (then contract type) to stay under Reed's ~10k-results pagination clamp. `dateUpdated` is a free change signal.
- **LinkedIn:** logged-out guest endpoints (`seeMoreJobPostings` paging, `jobPosting/<id>` detail). Queries are keyword × UK city, refined by date, job type and work mode only when a query hits the depth cap. Non-UK cards are dropped before any detail fetch.
- **Indeed:** HTML detail pages sit behind a challenge, so we call the GraphQL endpoint the public search page itself uses. It returns full descriptions, 100 jobs per request, with no detail fetches.
- **Fetching:** `curl_cffi` browser TLS impersonation, proxy read from env, 4 attempts with exponential backoff and jitter, a fresh exit IP per retry, block detection (status and page markers). Every request is logged to `requests`.
- **Budgets and failure handling:** boards share one wall-clock deadline (`CRAWL_MAX_MINUTES`, default 90). The proxy byte budget (`CRAWL_MAX_GB`, default 2) is split per board, evenly by default or by `CRAWL_BYTE_WEIGHTS` (e.g. `indeed=3,linkedin=10,reed=87`), and a finished board releases its unused share. Other caps: `CRAWL_MAX_REQUESTS_PER_BOARD` (6,000) and `CRAWL_MAX_JOBS_PER_BOARD` (0 = unlimited). A board gives up after `CRAWL_MAX_FAIL_STREAK` (40) consecutive failed requests. A run is `partial` if a budget is hit, a board gives up, or every request of a board failed (e.g. wrong proxy credentials).

## Lifecycle and refresh policy
- New listings insert with `first_seen_at`. Every sighting advances `last_seen_at`. `first_seen_at` never changes.
- `content_hash` covers the content fields after whitespace, tracking-query and number normalisation. A change updates the row and counts in `jobs_changed`.
- **Detail refresh:** a known listing is re-fetched when its change signal differs (Reed `dateUpdated`) or when 7 days have passed (boards without a signal). Indeed content arrives with search, so it costs no extra request.
- **Removal needs evidence:** an explicit expiry marker, or a confirmed 404/410. Absence from search, a blocked page, failed pagination or a cut-short crawl never sets `removed_at`. Live rows not seen for 2 days are re-fetched (up to 500 per board per run) to verify them. A reappearing listing clears `removed_at` and keeps `first_seen_at`.
- Estimated `posted_at` ("3d ago") is frozen once set, so it never looks like an edit.
- Interrupted runs keep every committed batch (25 jobs). A killed run is closed as `interrupted` on the next start.

## Dedupe
Candidates are blocked on normalised company + city. Within a block, copies on different boards must match on title and description similarity (`rapidfuzz`). Clusters are union-find, recomputed every run, and a cluster keeps the canonical id of its oldest member. Two different roles on one board never share a cluster. A title that only gains harmless words ("Teaching Assistant" / "SEMH Teaching Assistant", "... - 12 month FTC") matches at 95%+ description similarity, but extra words such as senior, II, night or manager never do. Other title differences need 85+ similarity. Hand-labelled on 40 random pairs each: precision about 88-98%, recall about 85-90% (near-miss candidates only).

## What worked and what didn't
- **Worked:** Indeed GraphQL with the `safari17_2_ios` profile (the default Chrome profile did not clear the search page). A 1.5 s page delay on LinkedIn. A worker ramp test (100-150 detail fetches per level) found no blocking up to 48 workers on Reed (about 36 jobs/s, levelling off near 32) and 64 on LinkedIn (about 16 jobs/s at 16 workers, 4% failed attempts), so LinkedIn's slowness is its paged discovery, not detail fetching. The defaults stay conservative (8 workers).
- **Didn't:** Indeed `/viewjob` and deep search pages (challenge). LinkedIn at ~0.3 s between pages drew a 429 after two pages. At 1.5 s, 5-14% of requests still get a 429 (retried on a fresh IP), with 8 or 24 workers alike.
- **Reed pagination** clamps at ~10k, hence slicing.

## Tradeoffs
- **Precision over recall in dedupe.** A wrong merge hurts more than a missed link.
- **Salary only from the board's structured field.** A £ figure inside free text is not parsed, because ranges, bonuses and OTE make it a guess. It stays null.
- **No salary period conversion.** Weekly or monthly pay is stored null, not annualised.
- **Indeed logos:** taken from `employer.dossier.images.squareLogoUrl`; about 60% of crawled rows have one, the rest are null because the source gives none.
- **UK validity:** a bare "remote" needs UK evidence in the title or text, otherwise the job is skipped (re-evaluated next run).
- **Weighted byte split:** one shared byte cap would let Reed (about 129 KB per job, against about 12 KB for LinkedIn and 2 KB for Indeed) use everything, so the budget is split per board. The default is even; our own runs weight it towards the expensive board.
- **LinkedIn has no change signal,** so edits are found at the weekly refresh, not sooner.

## Run results and an incident
- **First full run (10 Oct, 2 h 6 min):** 75,093 jobs, about 25k per board (Indeed 25,070, LinkedIn 25,007, Reed 25,016), 58,741 requests, 4,131 blocked (7%, all LinkedIn 429s, retried), 3.33 GB, $16.63, 857 duplicate links. LinkedIn set the time (about 3.3 jobs/s, paced discovery). Reed set the bytes (2.9 of the 3.3 GB, about 129 KB per job). Indeed needed 44 MB. A spot check of 30 listings per board matched on Reed 29/30 (one had expired) and LinkedIn 29/30 (one page returned a 429). Indeed matched its own API 29/30, but its pages block bots, so I did not check them.
- **Final run (11 Oct):** with a weighted byte split, Indeed reached 25,060 jobs in 3.5 min and Reed 22,725 in 9.4 min (it stopped at its byte share). LinkedIn had about 14.5k after 65 min. Then I stopped a leftover second Postgres container that shared the data directory. It deleted the lock file, the main server shut down, and the crawler lost its connection before it exported.
- **Recovery:** I backed up the volume, reset the WAL on a copy, and raised the transaction counter (the reset had set it below ids already used, which hid recent rows). I then dumped the data and restored it into a fresh database. About 1,100-1,600 LinkedIn jobs from the last minutes were lost. Two short LinkedIn resumes added about 470 jobs. LinkedIn's deep, refined query order makes a cheap resume hard, so I stopped at 13,891.
- **Committed export:** 61,676 jobs (Indeed 25,060, Reed 22,725, LinkedIn 13,891, 61,229 distinct `canonical_job_id`). `report.json` describes the last short run only (416 requests). All runs on this database logged 40,102 requests, 190 blocked, 2.85 GB. Lesson: run one Postgres server per data volume.

## Cost
`estimated_cost_usd` in the report = measured bytes x `PROXY_USD_PER_GB` (default $5). Bytes are estimated from response size and should be checked against the proxy dashboard. Measured over a 75k-job run (58.7k requests, 3.3 GB): about 57 KB per request overall, so **about $0.28 per 1,000 requests** at $5/GB. By board: Reed ~109 KB per request (~$0.55 per 1,000 pages), LinkedIn ~11 KB (~$0.06), Indeed ~135 KB but about 80 jobs per request.
