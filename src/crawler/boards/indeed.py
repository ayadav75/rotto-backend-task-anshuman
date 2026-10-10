"""Indeed UK via the public GraphQL endpoint the site's own pages use.

HTML detail pages (/viewjob) and deep search pages sit behind a bot challenge; the GraphQL API
(authorised by the public key embedded in the search page) returns full descriptions inline,
100 jobs per request, so no detail fetches are needed. A query's cursor chain ends after ~500 unique
jobs, so volume comes from many keyword x location queries."""
import itertools
import json
import re
from datetime import datetime, timezone
from typing import Iterator

from crawler.boards.base import Board, Card, DetailResult
from crawler.boards.linkedin import KEYWORDS, LOCATIONS

HOME = "https://uk.indeed.com/jobs?q=nurse&l=London"
GRAPHQL = "https://apis.indeed.com/graphql"
PROFILE = "safari17_2_ios"          # the only profile that clears the search page for us
LIMIT = 100
FIELDS = ("key title expired datePublished "
          "location{countryCode formatted{long} } employer{name dossier{images{squareLogoUrl}}} "
          "compensation{baseSalary{unitOfWork range{... on Range{min max} ... on AtLeast{min} ... on AtMost{max} ... on Exactly{value}}} currencyCode} "
          "attributes{label} description{text}")
# A bare "Remote" location is not UK evidence (the posting is only listed on the UK site), so keep
# such jobs only when title or description says UK workers are eligible.
UK_EVIDENCE = re.compile(r"\b(uk|u\.k\.|united kingdom|great britain|scotland|northern ireland)\b"
                         r"|(?<!new )\bengland\b|(?<!new south )\bwales\b|£", re.I)
PERIODS = {"YEAR": "year", "DAY": "day", "HOUR": "hour"}


def _q(s: str) -> str:
    return json.dumps(s)


class IndeedBoard(Board):
    name = "indeed"

    def __init__(self):
        self.api_key = None

    # ---- transport -------------------------------------------------------
    def _ensure_key(self, client, force=False) -> bool:
        if self.api_key and not force:
            return True
        r = client.get(HOME, source=self.name, profile=PROFILE)
        m = re.search(r'"oneGraphApiKey"\s*:\s*"([0-9a-f]+)"', r.text) if r.status == 200 else None
        self.api_key = m.group(1) if m else None
        return self.api_key is not None

    def _gql(self, client, query):
        for _ in range(2):
            if not self._ensure_key(client):
                return None
            key = self.api_key                # local copy: another worker may reset it concurrently
            if not key:
                continue
            r = client.post_json(GRAPHQL, {"query": query}, source=self.name, profile=PROFILE, headers={
                "indeed-api-key": key, "indeed-co": "GB", "Origin": "https://uk.indeed.com",
                "Referer": HOME, "content-type": "application/json"})
            if r.status in (401, 403) or r.blocked:
                self.api_key = None          # refresh the key once, then give up
                continue
            try:
                body = json.loads(r.text)
            except ValueError:
                return None
            return None if "errors" in body and not body.get("data") else body["data"]
        return None

    # ---- discovery -------------------------------------------------------
    def cards(self, client, should_stop) -> Iterator[Card]:
        seen = set()
        for where, what in itertools.product([l if isinstance(l, str) else "United Kingdom" for l in LOCATIONS],
                                             [""] + KEYWORDS):
            cursor = None
            while not should_stop():
                cur = f", cursor:{_q(cursor)}" if cursor else ""
                data = self._gql(client, (
                    "query { jobSearch(what:%s, location:{where:%s, radius:25, radiusUnit:MILES}, limit:%d%s)"
                    " { pageInfo{nextCursor} results{ job{ %s } } } }") % (_q(what), _q(where), LIMIT, cur, FIELDS))
                if not data:
                    break
                js = data["jobSearch"]
                new = 0
                for res in js["results"]:
                    job = res["job"]
                    if job["key"] in seen:
                        continue
                    seen.add(job["key"])
                    new += 1
                    yield self.to_card(job)
                cursor = js["pageInfo"]["nextCursor"]
                if not cursor or new == 0:      # chain exhausted, or only repeats from here on
                    break
            if should_stop():
                return

    def to_card(self, job: dict) -> Card:
        return Card(job["key"], f"https://uk.indeed.com/viewjob?jk={job['key']}", inline=self.parse(job))

    # ---- parsing ---------------------------------------------------------
    def parse(self, job: dict) -> DetailResult:
        if job.get("expired"):
            return DetailResult("expired", reason="expired")
        loc = job.get("location") or {}
        if loc.get("countryCode") != "GB":
            return DetailResult("skip", reason="non-uk")
        text = ((job.get("description") or {}).get("text") or "").strip()
        if not text or not job.get("title"):
            return DetailResult("failed", reason="empty_description")
        place = ((loc.get("formatted") or {}).get("long") or "").strip().lower()
        if place in ("", "remote") and not UK_EVIDENCE.search(job["title"] + "\n" + text):
            # not permanent: the data arrives free with each search, so re-evaluate if the text changes
            return DetailResult("skip", reason="remote_without_uk_evidence", permanent=False)
        labels = {a["label"].lower() for a in job.get("attributes") or []}
        work_type = next((wt for key, wt in (("contract", "contract"), ("temporary", "temporary"),
                                             ("part-time", "part time"), ("full-time", "full time"))
                          if any(key in l for l in labels)), None)
        smin = smax = period = None
        comp = job.get("compensation") or {}
        base = comp.get("baseSalary") or {}
        if comp.get("currencyCode") == "GBP" and base.get("unitOfWork") in PERIODS:
            rng = base.get("range") or {}
            # Range -> min+max, AtLeast -> min only, AtMost -> max only, Exactly -> value for both
            lo, hi = rng.get("min", rng.get("value")), rng.get("max", rng.get("value"))
            smin, smax = (round(v, 2) if v else None for v in (lo, hi))
            period = PERIODS[base["unitOfWork"]] if (smin or smax) else None
        logo = (((job.get("employer") or {}).get("dossier") or {}).get("images") or {}).get("squareLogoUrl")
        published = job.get("datePublished")
        return DetailResult("job", {
            "id": f"indeed:{job['key']}", "source": "indeed", "source_job_id": job["key"],
            "url": f"https://uk.indeed.com/viewjob?jk={job['key']}", "title": job["title"].strip(),
            "company": (job.get("employer") or {}).get("name"), "company_logo_url": logo,
            "location": (loc.get("formatted") or {}).get("long"), "work_type": work_type,
            "salary_min": smin, "salary_max": smax, "salary_period": period,
            "posted_at": datetime.fromtimestamp(published / 1000, tz=timezone.utc) if published else None,
            "description": text, "source_signal": None})

    # ---- re-check a known listing (removal evidence) ----------------------
    def detail(self, client, card: Card) -> DetailResult:
        data = self._gql(client, 'query { jobData(input:{jobKeys:[%s]}) { results{ job{ %s } } } }'
                         % (_q(card.source_job_id), FIELDS))
        if data is None:
            return DetailResult("failed", reason="api_error")
        results = data["jobData"]["results"]
        if not results or results[0].get("job") is None:
            return DetailResult("gone", reason="not_found")
        return self.parse(results[0]["job"])
