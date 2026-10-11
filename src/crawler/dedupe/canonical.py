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
TITLE_NOISE = re.compile(r"\b(m/f/d|f/m/d|ref[:\s]*\w+|job id[:\s]*\w+)\b", re.I)
# Parenthesised text usually names the actual role ("Engineer (Backend)"), so it is kept unless it is
# only boilerplate such as "(m/f/d)", "(Remote)", "(Permanent)" or a reference number.
BRACKETED = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
BOILERPLATE = re.compile(
    r"^(?:\W|\d|m/f/d|f/m/d|remote|hybrid|uk|gb|united kingdom|permanent|temporary|temp|contract|full[- ]?time|"
    r"part[- ]?time|fixed[- ]?term|ftc|urgent|new|ref\w*|job id\w*|[a-z]{0,3}\d+\w*)+$", re.I)
SAME_BOARD_TITLE_MIN = 90     # two copies from one board may share a cluster only if their titles are this similar
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
    t = BRACKETED.sub(lambda m: " " if BOILERPLATE.match(m.group(1).strip()) else f" {m.group(1)} ", t.lower())
    t = TITLE_NOISE.sub(" ", t)
    return " ".join(re.findall(r"[a-z0-9]+", t))


def norm_desc(d):
    return " ".join(re.findall(r"[a-z0-9]+", d[: DESC_CHARS * 2].lower()))[:DESC_CHARS]


# Extra title words that make it a different job (a level, shift or seniority), not a variant of the same ad.
ROLE_CHANGING = {"senior", "sr", "junior", "jr", "lead", "principal", "head", "chief", "graduate", "grad", "trainee",
                 "apprentice", "intern", "deputy", "assistant", "associate", "manager", "director", "supervisor",
                 "ii", "iii", "iv", "v", "level", "night", "nights", "day", "days",
                 "weekend", "evening", "bank", "relief", "locum", "fleet", "estates"}
DESC_CONTAINED_MIN = 95       # a contained title needs a near-identical description


def title_contained(a: str, b: str) -> bool:
    """One normalised title is the other plus harmless extra words ("Teaching Assistant" / "SEMH Teaching
    Assistant", "Credit Risk Analyst" / "Credit Risk Analyst 12 month FTC"). Extra words that change the job
    (senior, II, night, manager...) disqualify it. Single-word titles must match exactly."""
    ta, tb = set(a.split()), set(b.split())
    short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    return len(short) >= 2 and short <= long_ and not ((long_ - short) & ROLE_CHANGING)


def is_match(a, b) -> bool:
    if a["source"] == b["source"]:
        return False
    same_company = a["company"] is not None and a["company"] == b["company"]
    desc = max(fuzz.token_set_ratio(a["desc"], b["desc"]), fuzz.ratio(a["desc"][:600], b["desc"][:600]))
    # A title that merely gains a suffix or prefix is the same posting when the text is (near) identical.
    if same_company and desc >= DESC_CONTAINED_MIN and title_contained(a["title"], b["title"]):
        return True
    title = fuzz.token_sort_ratio(a["title"], b["title"])
    if title < 80:
        return False
    if same_company:
        # Agencies reuse one description template across different roles, so a description alone only
        # counts when the titles are also close (85+).
        return (title >= 90 and desc >= 55) or (title >= 85 and desc >= 80)
    # one side hides its company: demand stronger evidence
    return title >= 90 and desc >= 85


class UnionFind:
    def __init__(self):
        self.parent = {}
        self.titles = {}          # root -> {source: [normalised titles]}, to veto merges of distinct roles

    def register(self, x, source, title):
        self.find(x)
        self.titles.setdefault(x, {}).setdefault(source, []).append(title)

    def compatible(self, a, b):
        """A cluster may hold several copies from one board (reposts) but never two different roles."""
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return True
        ta, tb = self.titles.get(ra, {}), self.titles.get(rb, {})
        for src in ta.keys() & tb.keys():
            if any(fuzz.token_sort_ratio(x, y) < SAME_BOARD_TITLE_MIN for x in ta[src] for y in tb[src]):
                return False
        return True

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        self.parent[ra] = rb
        for src, ts in self.titles.pop(ra, {}).items():
            self.titles.setdefault(rb, {}).setdefault(src, []).extend(ts)


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
    for p in prepared.values():
        uf.register(p["id"], p["source"], p["title"])
    for key, members in blocks.items():
        if len({m["source"] for m in members}) < 2 or len(members) > 600:
            continue
        for i, a in enumerate(members):
            for b in members[i + 1:]:
                if key[0] == "t" and a["company"] and b["company"] and a["company"] != b["company"]:
                    continue
                if is_match(a, b) and uf.compatible(a["id"], b["id"]):
                    uf.union(a["id"], b["id"])
    groups = defaultdict(list)
    for jid in list(uf.parent):
        groups[uf.find(jid)].append(jid)
    return [g for g in groups.values() if len(g) > 1]


def assign_canonical(data):
    """data: dicts with id, source, title, company, location, description, canonical (stored value or None),
    first (first_seen_at). Returns {id: canonical_job_id}.

    Clusters are recomputed from scratch on every run, so a link made under older rules is dropped as soon as
    the current rules no longer support it. Stored canonical ids only keep ids stable: an unchanged cluster
    keeps the canonical id of its oldest member, and when a cluster splits, the oldest part keeps the old id
    while each other part falls back to the id of its own oldest member."""
    by_id = {d["id"]: d for d in data}
    canon = {d["id"]: d["id"] for d in data}                   # default: a job is its own canonical copy
    claimed = set()
    clusters = sorted(build_clusters(data), key=lambda c: min((by_id[m]["first"], m) for m in c))
    for cluster in clusters:
        oldest = min(cluster, key=lambda m: (by_id[m]["first"], m))
        target = by_id[oldest]["canonical"] or oldest
        if target in claimed:                                  # another cluster already owns this id (split)
            target = oldest
        claimed.add(target)
        for m in cluster:
            canon[m] = target
    return canon


def link_duplicates(conn) -> int:
    """Assign canonical_job_id to every row; returns the number of rows linked to an earlier copy."""
    rows = conn.execute(
        "SELECT id, source, title, company, location, description, canonical_job_id, first_seen_at FROM jobs"
    ).fetchall()
    data = [dict(zip(("id", "source", "title", "company", "location", "description", "canonical", "first"), r))
            for r in rows]
    canon = assign_canonical(data)
    changes = [(canon[d["id"]], d["id"]) for d in data if canon[d["id"]] != d["canonical"]]
    with conn.transaction():
        conn.cursor().executemany("UPDATE jobs SET canonical_job_id=%s WHERE id=%s", changes)
    sizes = defaultdict(int)
    for c in canon.values():
        sizes[c] += 1
    return sum(n - 1 for n in sizes.values() if n > 1)
