"""LinkedIn logged-out pages. The /jobs/search page always serves the first 60 cards, but the guest
seeMoreJobPostings endpoint pages with `start=` (10 cards a page, ~1000 deep, 400 beyond that), so
discovery = page each (keyword x location) query to its end; only a query that hits the depth cap is
split further along filter dimensions."""
import itertools
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Iterator
from urllib.parse import quote

from selectolax.parser import HTMLParser

from crawler.boards.base import Board, Card, DetailResult
from crawler.parse.normalize import html_to_text

log = logging.getLogger("crawler")
SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search?"
DETAIL = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/"
UK_GEO = "101165590"
PAGE_SIZE = 10
DEPTH_CAP = 1000         # guest endpoint answers 400 from start=1000
PAGE_DELAY = 1.5         # seconds between pages; ~0.3s drew a 429 after two pages
STALE_PAGES = 3          # stop paging a query after this many consecutive pages with no unseen cards
REFINE_MIN_NEW = 15      # a depth-capped query refines only if it yielded at least this many unseen UK cards
UK_MARKERS = ("united kingdom", "england", "scotland", "wales", "northern ireland")

KEYWORDS = [
    "nurse", "care assistant", "teacher", "teaching assistant", "software engineer", "developer",
    "data analyst", "accountant", "finance", "project manager", "sales", "account manager",
    "customer service", "administrator", "receptionist", "driver", "warehouse", "engineer",
    "electrician", "plumber", "mechanic", "chef", "kitchen", "waiter", "bar staff", "cleaner",
    "security", "retail assistant", "store manager", "marketing", "hr", "recruitment", "solicitor",
    "paralegal", "surveyor", "architect", "construction", "labourer", "site manager", "support worker",
    "social worker", "pharmacist", "doctor", "dentist", "physiotherapist", "carer", "manager",
    "director", "consultant", "analyst", "designer", "devops", "cyber security", "business analyst",
    "buyer", "logistics", "procurement", "operations", "apprentice", "graduate", "intern",
    "bookkeeper", "payroll", "underwriter", "actuary", "banking", "insurance", "mortgage adviser",
    "lecturer", "researcher", "scientist", "laboratory", "technician", "machine operator",
    "forklift", "delivery driver", "hgv driver", "courier", "pilot", "cabin crew", "hotel",
    "housekeeper", "gardener", "fitness instructor", "hairdresser", "beauty therapist", "veterinary",
]
LOCATIONS = [
    ("United Kingdom", UK_GEO), "London", "Manchester", "Birmingham", "Leeds", "Glasgow", "Liverpool",
    "Bristol", "Sheffield", "Edinburgh", "Cardiff", "Belfast", "Newcastle upon Tyne", "Nottingham",
    "Leicester", "Southampton", "Reading", "Milton Keynes", "Cambridge", "Oxford", "Brighton", "Norwich",
    "Aberdeen", "Swansea", "Plymouth", "Exeter", "York", "Coventry", "Derby", "Stoke-on-Trent",
    "Wolverhampton", "Portsmouth", "Bournemouth", "Ipswich", "Peterborough", "Luton", "Swindon",
    "Gloucester", "Cheltenham", "Bath", "Dundee", "Inverness", "Newport", "Wrexham", "Hull", "Bradford",
    "Preston", "Blackpool", "Middlesbrough", "Sunderland", "Northampton", "Watford", "Slough",
    "Guildford", "Croydon", "Stockport", "Warrington", "Lincoln", "Doncaster",
]
TPR = ["r86400", "r604800", "r2592000"]           # past day / week / month
JOB_TYPES = ["F", "P", "C", "T"]                   # full, part, contract, temporary
WORK_MODES = ["1", "2", "3"]                       # on-site, remote, hybrid


def _loc(l):
    return (l, None) if isinstance(l, str) else l


def _text(node):
    return re.sub(r"\s+", " ", node.text()).strip() if node else None


class LinkedInBoard(Board):
    name = "linkedin"

    def __init__(self):
        self.dropped_non_uk = 0

    # ---- discovery -------------------------------------------------------
    def _query(self, client, kw, loc, extra: dict, start=0):
        name, geo = _loc(loc)
        params = {"keywords": kw, "location": name if geo else f"{name}, United Kingdom"}
        if geo:
            params["geoId"] = geo
        params.update(extra)
        params["start"] = start
        url = SEARCH + "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
        r = client.get(url, source=self.name)
        if r.status != 200 or r.blocked:
            return None
        return list(self.parse_cards(r.text))

    def parse_cards(self, html: str) -> Iterator[Card]:
        for li in HTMLParser(html).css("li"):
            div = li.css_first("div.base-search-card")
            if div is None:
                continue
            urn = div.attributes.get("data-entity-urn", "")
            jid = urn.rsplit(":", 1)[-1]
            if not jid.isdigit():
                continue
            t = li.css_first("time")
            img = li.css_first("img.artdeco-entity-image")
            yield Card(jid, f"https://www.linkedin.com/jobs/view/{jid}", extra={
                "title": _text(li.css_first(".base-search-card__title")),
                "company": _text(li.css_first(".base-search-card__subtitle")),
                "location": _text(li.css_first(".job-search-card__location")),
                "posted": t.attributes.get("datetime") if t else None,
                "logo": img.attributes.get("data-delayed-url") if img else None,
            })

    @staticmethod
    def _is_uk(location):
        """Card-level UK check, so non-UK cards never cost a detail fetch. Unknown location -> keep."""
        return not location or any(m in location.lower() for m in UK_MARKERS)

    def _refine(self, client, kw, loc, extra, dims, seen, should_stop) -> Iterator[Card]:
        """Page a query to its end and yield unseen UK cards. Refine along the next filter dimension
        only when the query hit the depth cap AND still found enough new jobs."""
        fresh_total = pages_seen = stale = 0
        for start in range(0, DEPTH_CAP, PAGE_SIZE):
            if should_stop():
                return
            cards = self._query(client, kw, loc, extra, start)
            if not cards:                      # blocked, error or end of results
                break
            pages_seen += 1
            unseen = [c for c in cards if c.source_job_id not in seen]
            seen.update(c.source_job_id for c in cards)
            fresh = [c for c in unseen if self._is_uk(c.extra.get("location"))]
            for c in unseen:
                if not self._is_uk(c.extra.get("location")):
                    self.dropped_non_uk += 1
                    log.debug("linkedin card dropped as non-UK: %r", c.extra.get("location"))
            fresh_total += len(fresh)
            yield from fresh
            stale = 0 if unseen else stale + 1
            if stale >= STALE_PAGES:
                return                         # mostly seen already: not worth deeper pages
            time.sleep(PAGE_DELAY)
        else:
            if dims and fresh_total >= REFINE_MIN_NEW:
                (key, values), rest = dims[0], dims[1:]
                for v in values:
                    yield from self._refine(client, kw, loc, {**extra, key: v}, rest, seen, should_stop)

    def cards(self, client, should_stop) -> Iterator[Card]:
        seen = set()
        dims = [("f_TPR", TPR), ("f_JT", JOB_TYPES), ("f_WT", WORK_MODES)]
        # broad locations first so the budget buys breadth before depth
        try:
            # CRAWL_LINKEDIN_SKIP_QUERIES lets an interrupted crawl restart past the queries it already covered
            skip = int(os.environ.get("CRAWL_LINKEDIN_SKIP_QUERIES", "0"))
            for loc, kw in itertools.islice(itertools.product(LOCATIONS, KEYWORDS), skip, None):
                yield from self._refine(client, kw, loc, {}, dims, seen, should_stop)
                if should_stop():
                    return
        finally:
            log.info("linkedin: %d unique cards seen, %d dropped as non-UK before any detail fetch",
                     len(seen), self.dropped_non_uk)

    # ---- detail ----------------------------------------------------------
    def detail(self, client, card: Card) -> DetailResult:
        r = client.get(DETAIL + card.source_job_id, source=self.name)
        if r.status in (404, 410):
            return DetailResult("gone", reason=f"http_{r.status}")
        if r.status != 200 or r.blocked:
            return DetailResult("failed", reason=r.block_reason or f"http_{r.status}")
        return self.parse(r.text, card)

    def parse(self, html: str, card: Card) -> DetailResult:
        tree = HTMLParser(html)
        title = _text(tree.css_first(".top-card-layout__title"))
        markup = tree.css_first(".show-more-less-html__markup")
        if title is None or markup is None:
            if re.search(r"no longer accepting applications|closed-job", html):
                return DetailResult("expired", reason="closed")
            return DetailResult("failed", reason="unparseable")
        if tree.css_first(".closed-job, .top-card-layout__cta--closed") or \
                re.search(r"No longer accepting applications", html):
            return DetailResult("expired", reason="closed")
        location = _text(tree.css_first(".topcard__flavor--bullet"))
        if not location or not any(m in location.lower() for m in UK_MARKERS):
            return DetailResult("skip", reason="non-uk")
        description = html_to_text(markup.html)
        description = re.sub(r"\s*Show (more|less)\s*$", "", description).strip()
        if not description:
            return DetailResult("failed", reason="empty_description")
        crit = {}
        for li in tree.css("li.description__job-criteria-item"):
            crit[_text(li.css_first("h3")).lower()] = _text(li.css_first("span"))
        work_type = {"full-time": "full time", "part-time": "part time", "contract": "contract",
                     "temporary": "temporary"}.get((crit.get("employment type") or "").lower())
        smin, smax, period = self._salary(_text(tree.css_first(".salary")))
        img = tree.css_first("img.artdeco-entity-image")
        logo = (img.attributes.get("data-delayed-url") or img.attributes.get("src")) if img else None
        posted, estimated = self._posted(card, _text(tree.css_first(".posted-time-ago__text")))
        jid = card.source_job_id
        job = {
            "id": f"linkedin:{jid}", "source": "linkedin", "source_job_id": jid, "url": card.url,
            "title": title, "company": _text(tree.css_first(".topcard__org-name-link")) or card.extra.get("company"),
            "company_logo_url": logo, "location": location, "work_type": work_type,
            "salary_min": smin, "salary_max": smax, "salary_period": period,
            "posted_at": posted, "posted_at_estimated": estimated, "description": description,
            "source_signal": None,
        }
        return DetailResult("job", job)

    @staticmethod
    def _posted(card, relative):
        iso = card.extra.get("posted")
        if iso:
            return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc), False
        m = re.match(r"(\d+)\s+(minute|hour|day|week|month)s?\s+ago", relative or "")
        if not m:
            return None, True
        n, unit = int(m.group(1)), m.group(2)
        delta = {"minute": timedelta(minutes=n), "hour": timedelta(hours=n), "day": timedelta(days=n),
                 "week": timedelta(weeks=n), "month": timedelta(days=30 * n)}[unit]
        return datetime.now(timezone.utc) - delta, True

    @staticmethod
    def _salary(text):
        """'£33,000.00/yr - £42,300.00/yr' -> (33000, 42300, 'year'); non-GBP or odd periods -> null."""
        if not text or "£" not in text:
            return None, None, None
        nums = [float(x.replace(",", "")) for x in re.findall(r"£([\d,]+(?:\.\d+)?)", text)]
        per = re.search(r"/(yr|hr|day)", text)
        period = {"yr": "year", "hr": "hour", "day": "day"}.get(per.group(1)) if per else None
        if not nums or not period:
            return None, None, None
        return min(nums), max(nums), period
