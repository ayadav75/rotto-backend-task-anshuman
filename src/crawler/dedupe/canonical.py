"""Cross-board duplicate linking.

Candidates are blocked on (normalised company, city); within a block, jobs from *different* boards are
compared on title and description similarity. Matches are merged with union-find. canonical_job_id is
stable: a cluster keeps the canonical id of its oldest member, so existing links never flip."""
import re
from collections import defaultdict

from rapidfuzz import fuzz

LEGAL = {"ltd", "limited", "plc", "llp", "llc", "inc", "uk", "group", "the", "company", "co", "services",
         "holdings", "gb", "t", "a"}
CITY_ALIASES = {"greater london": "london", "city of london": "london", "westminster": "london"}
TITLE_NOISE = re.compile(r"\(.*?\)|\[.*?\]|\b(m/f/d|f/m/d|ref[:\s]*\w+|job id[:\s]*\w+)\b", re.I)
DESC_CHARS = 1500


def norm_company(name):
    if not name:
        return None
    toks = [t for t in re.findall(r"[a-z0-9]+", name.lower().replace("&", " and ")) if t not in LEGAL]
    return " ".join(toks) or None


def norm_city(loc):
    if not loc:
        return None
    first = re.split(r",| - |\d", loc.lower())[0].strip()
    first = re.sub(r"[^a-z ]", "", first).strip()
    return CITY_ALIASES.get(first, first) or None


def norm_title(t):
    t = TITLE_NOISE.sub(" ", t.lower())
    return " ".join(re.findall(r"[a-z0-9]+", t))


def norm_desc(d):
    return " ".join(re.findall(r"[a-z0-9]+", d[: DESC_CHARS * 2].lower()))[:DESC_CHARS]


def is_match(a, b) -> bool:
    if a["source"] == b["source"]:
        return False
    title = fuzz.token_sort_ratio(a["title"], b["title"])
    if title < 80:
        return False
    desc = max(fuzz.token_set_ratio(a["desc"], b["desc"]), fuzz.ratio(a["desc"][:600], b["desc"][:600]))
    same_company = a["company"] is not None and a["company"] == b["company"]
    if same_company:
        return (title >= 90 and desc >= 55) or desc >= 80
    # one side hides its company: demand stronger evidence
    return title >= 90 and desc >= 85


class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        self.parent[self.find(a)] = self.find(b)


def build_clusters(rows):
    """rows: dicts with id, source, title, company, location, description. Returns list of id-lists (size>1)."""
    prepared, blocks = {}, defaultdict(list)
    for r in rows:
        p = {"id": r["id"], "source": r["source"], "title": norm_title(r["title"]),
             "company": norm_company(r["company"]), "city": norm_city(r["location"]),
             "desc": norm_desc(r["description"])}
        prepared[r["id"]] = p
        if p["city"] and p["title"]:
            if p["company"]:
                blocks[("c", p["company"], p["city"])].append(p)
            # company-less rows are matched against everything in the city sharing a title prefix
            blocks[("t", p["city"], " ".join(p["title"].split()[:2]))].append(p)
    uf = UnionFind()
    for key, members in blocks.items():
        if len({m["source"] for m in members}) < 2 or len(members) > 600:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if key[0] == "t" and a["company"] and b["company"] and a["company"] != b["company"]:
                    continue
                if is_match(a, b):
                    uf.union(a["id"], b["id"])
    groups = defaultdict(list)
    for jid in list(uf.parent):
        groups[uf.find(jid)].append(jid)
    return [g for g in groups.values() if len(g) > 1]


def link_duplicates(conn) -> int:
    """Assign canonical_job_id to every row; returns the number of rows linked to an earlier copy."""
    rows = conn.execute(
        "SELECT id, source, title, company, location, description, canonical_job_id, first_seen_at FROM jobs"
    ).fetchall()
    data = [dict(zip(("id", "source", "title", "company", "location", "description", "canonical", "first"), r))
            for r in rows]
    by_id = {d["id"]: d for d in data}
    canon = {d["id"]: d["canonical"] or d["id"] for d in data}      # default: own id
    # existing links are kept: rows already sharing a canonical id stay together
    existing = defaultdict(list)
    for d in data:
        existing[canon[d["id"]]].append(d["id"])
    clusters = build_clusters([{**d, "description": d["description"]} for d in data])
    for cluster in clusters:
        members = set(cluster)
        for c in {canon[m] for m in cluster}:
            members.update(existing[c])
        oldest = min(members, key=lambda m: (by_id[m]["first"], m))
        # keep the canonical id of the oldest member (already stable), else its own id
        target = canon[oldest]
        for m in members:
            canon[m] = target
    changes = [(canon[d["id"]], d["id"]) for d in data if canon[d["id"]] != d["canonical"]]
    with conn.transaction():
        conn.cursor().executemany("UPDATE jobs SET canonical_job_id=%s WHERE id=%s", changes)
    sizes = defaultdict(int)
    for c in canon.values():
        sizes[c] += 1
    return sum(n - 1 for n in sizes.values() if n > 1)
