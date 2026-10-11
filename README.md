# UK job crawler: Reed, LinkedIn, Indeed → Postgres

```
docker compose up -d      # Postgres
make crawl                # PROXY_URL or PROXY_HOST/PORT/USERNAME/PASSWORD from the environment
make test                 # offline lifecycle fixtures, no proxy or internet (FIXTURE_PATH=... to swap)
```
Run `make crawl` again for an incremental run. Every run rewrites `output/jobs.jsonl` (all rows, removed ones included) and `output/report.json`.

## How it works
`boards/` only fetch and parse, `store/reconcile.py` is the single save path (live crawl and `make test` both call `apply_run()`), `dedupe/` links copies. Boards run concurrently with a worker pool each.

- **Reed:** `__NEXT_DATA__` JSON in search and detail pages. Search is sliced by sector (then contract type) to stay under Reed's ~10k-results pagination clamp. `dateUpdated` is a free change signal.
- **LinkedIn:** logged-out guest endpoints (`seeMoreJobPostings` paging, `jobPosting/<id>` detail). Queries are keyword × UK city, refined by date, job type and work mode only when a query hits the depth cap. Non-UK cards are dropped before any detail fetch.
- **Indeed:** HTML detail pages sit behind a challenge, so we call the GraphQL endpoint the public search page itself uses. It returns full descriptions, 100 jobs per request, with no detail fetches.
- **Fetching:** `curl_cffi` browser TLS impersonation, proxy read from env, 4 attempts with exponential backoff and jitter, a fresh exit IP per retry, block detection (status and page markers). Every request is logged to `requests`. A board gives up after `CRAWL_MAX_FAIL_STREAK` (40) consecutive failures. A run is `partial` if a board fails or a budget (90 min, 2 GB, 6,000 requests per board) is hit.

## Lifecycle and refresh policy
- New listings insert with `first_seen_at`. Every sighting advances `last_seen_at`. `first_seen_at` never changes.
- `content_hash` covers the content fields after whitespace, tracking-query and number normalisation. A change updates the row and counts in `jobs_changed`.
- **Detail refresh:** a known listing is re-fetched when its change signal differs (Reed `dateUpdated`) or when 7 days have passed (boards without a signal). Indeed content arrives with search, so it costs no extra request.
- **Removal needs evidence:** an explicit expiry marker, or a confirmed 404/410. Absence from search, a blocked page, failed pagination or a cut-short crawl never sets `removed_at`. Live rows not seen for 2 days are re-fetched (up to 500 per board per run) to verify them. A reappearing listing clears `removed_at` and keeps `first_seen_at`.
- Estimated `posted_at` ("3d ago") is frozen once set, so it never looks like an edit.
- Interrupted runs keep every committed batch (25 jobs). A killed run is closed as `interrupted` on the next start.

## Dedupe
Candidates are blocked on normalised company + city. Within a block, copies on different boards must match on title and description similarity (`rapidfuzz`). Clusters are union-find, recomputed every run, and a cluster keeps the canonical id of its oldest member. Two different roles on one board never share a cluster.

## What worked and what didn't
- **Worked:** Indeed GraphQL with the `safari17_2_ios` profile (the default Chrome profile did not clear the search page). A 1.5 s page delay and 8 workers on LinkedIn.
- **Didn't:** Indeed `/viewjob` and deep search pages (challenge). LinkedIn at ~0.3 s between pages or 24 workers drew HTTP 429 (~5% of requests).
- **Reed pagination** clamps at ~10k, hence slicing.

## Tradeoffs
- **Precision over recall in dedupe.** A wrong merge hurts more than a missed link.
- **Salary only from the board's structured field.** A £ figure inside free text is not parsed, because ranges, bonuses and OTE make it a guess. It stays null.
- **No salary period conversion.** Weekly or monthly pay is stored null, not annualised.
- **Indeed logos are null:** the API returned none in our crawls.
- **UK validity:** a bare "remote" needs UK evidence in the title or text, otherwise the job is skipped (re-evaluated next run).
- **LinkedIn has no change signal,** so edits are found at the weekly refresh, not sooner.

## Cost
`estimated_cost_usd` in the report = measured bytes × `PROXY_USD_PER_GB` (default $5). Bytes are estimated from response size and should be checked against the proxy dashboard. A Reed + LinkedIn run averaged ~104 KB per request, so about **$0.52 per 1,000 pages** at $5/GB. Indeed is far cheaper per job (100 jobs per request).
