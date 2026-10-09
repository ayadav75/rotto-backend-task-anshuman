import json
import math
import re
from datetime import datetime, timezone
from typing import Iterator

from crawler.boards.base import Board, Card, DetailResult
from crawler.parse.normalize import html_to_text

BASE = "https://www.reed.co.uk"
PAGE_SIZE = 25
MAX_PAGES = 399          # Reed clamps pagination at ~10k results per query
MAX_SLICE = MAX_PAGES * PAGE_SIZE
LOGO_BASE = "https://resources.reed.co.uk/profileimages/logos/thumbs/"
NEXT_DATA = re.compile(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S)


def _next_data(html: str) -> dict | None:
    m = NEXT_DATA.search(html)
    return json.loads(m.group(1))["props"]["pageProps"] if m else None


def _iso(ts: str | None):
    if not ts:
        return None
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=timezone.utc) \
        if "+" not in ts and not ts.endswith("Z") else datetime.fromisoformat(ts.replace("Z", "+00:00"))


class ReedBoard(Board):
    name = "reed"

    # ---- discovery -------------------------------------------------------
    def _search(self, client, query: str):
        r = client.get(f"{BASE}/jobs?{query}", source=self.name)
        pp = _next_data(r.text) if r.status == 200 and not r.blocked else None
        return (pp or {}).get("searchResults")

    def slices(self, client) -> Iterator[tuple[str, int]]:
        """Yield (query, count) slices that each fit under the pagination cap."""
        top = self._search(client, "pageno=1")
        if not top:
            return
        for sector, n in sorted(top["searchFacets"]["parentSectors"].items(), key=lambda x: -x[1]):
            q = f"parentSectorIds={sector}"
            if n <= MAX_SLICE:
                yield q, n
                continue
            for flag in ("perm", "temp", "contract"):    # partitions contract type
                res = self._search(client, f"{q}&{flag}=True&pageno=1")
                if res:
                    yield f"{q}&{flag}=True", res["count"]

    def cards(self, client, should_stop) -> Iterator[Card]:
        seen = set()
        for query, count in self.slices(client):
            for page in range(1, min(math.ceil(count / PAGE_SIZE), MAX_PAGES) + 1):
                if should_stop():
                    return
                res = self._search(client, f"{query}&pageno={page}")
                if not res or not res["jobs"]:
                    break
                for j in res["jobs"]:
                    d = j["jobDetail"]
                    jid = str(d["jobId"])
                    if jid in seen:
                        continue
                    seen.add(jid)
                    yield Card(jid, BASE + j["url"], signal=d.get("dateUpdated"),
                               expired=_iso(d.get("expiryDate")) is not None
                               and _iso(d["expiryDate"]) < datetime.now(timezone.utc))

    # ---- detail ----------------------------------------------------------
    def detail(self, client, card: Card) -> DetailResult:
        r = client.get(card.url, source=self.name)
        if r.status in (404, 410):
            return DetailResult("gone", reason=f"http_{r.status}")
        if r.status != 200 or r.blocked:
            return DetailResult("failed", reason=r.block_reason or f"http_{r.status}")
        pp = _next_data(r.text)
        jd = ((pp or {}).get("consolidatedJobDetails") or {}).get("jobDetails")
        if not jd:
            return DetailResult("failed", reason="no_job_details")
        return self.parse(jd, card)

    def parse(self, jd: dict, card: Card) -> DetailResult:
        exp = _iso(jd.get("expiryDate"))
        if not jd.get("isLive", True) or jd.get("isSuspended") or (exp and exp < datetime.now(timezone.utc)):
            return DetailResult("expired", reason="not_live")
        loc = jd.get("jobLocation") or {}
        if loc.get("countryCode") != "GB":
            return DetailResult("skip", reason="non-uk")
        description = html_to_text(jd.get("description"))
        if not description or not jd.get("title"):
            return DetailResult("failed", reason="empty_description")
        owner = jd.get("jobOwner") or {}
        contract = (jd.get("jobContractType") or {}).get("name", "").lower()
        hours = jd.get("jobEmploymentHours") or {}
        work_type = ("contract" if "contract" in contract else "temporary" if "temp" in contract
                     else "part time" if hours.get("isPartTime") else "full time" if hours.get("isFullTime") else None)
        smin, smax, period = self._salary(jd.get("jobSalary") or {})
        jid = str(jd["id"])
        logo = owner.get("ouLogoImage") or owner.get("profileLogoImage")
        job = {
            "id": f"reed:{jid}", "source": "reed", "source_job_id": jid, "url": card.url,
            "title": jd["title"].strip(), "company": owner.get("ouName") or owner.get("profileName"),
            "company_logo_url": LOGO_BASE + logo if logo else None,
            "location": loc.get("locationName"), "work_type": work_type,
            "salary_min": smin, "salary_max": smax, "salary_period": period,
            "posted_at": _iso(jd.get("createdDate")), "description": description,
            "source_signal": jd.get("updatedDate"),
            "remote": bool(loc.get("isRemoteJob")),
        }
        return DetailResult("job", job)

    @staticmethod
    def _salary(s: dict):
        """Only report a salary the page actually displays (hidden/competitive -> null)."""
        shown = s.get("displaySalary") or ""
        if not re.search(r"\d", shown):
            return None, None, None
        kind = ((s.get("type") or {}).get("name") or "").lower()
        period = {"per annum": "year", "per day": "day", "per hour": "hour"}.get(kind)
        lo, hi = (s.get("from"), s.get("to")) if period else (s.get("fromPerAnnum"), s.get("toPerAnnum"))
        period = period or "year"
        return (lo or None), (hi or None), period
