"""Offline checks for board parsing rules. Run by `make test`; no network or database needed."""
from crawler.boards.indeed import IndeedBoard, UK_EVIDENCE
from crawler.boards.linkedin import LinkedInBoard
from crawler.boards.reed import ReedBoard
from crawler.dedupe.canonical import UnionFind, assign_canonical, build_clusters, norm_title
from crawler.parse.normalize import html_to_text


def _indeed_job(**kw):
    job = {"key": "abc123", "title": "Driver", "expired": False, "datePublished": 1790000000000,
           "location": {"countryCode": "GB", "formatted": {"long": "Leeds LS1 1AA"}},
           "employer": {"name": "Acme", "dossier": {"images": {"squareLogoUrl": "https://img/x.png"}}},
           "compensation": None, "attributes": [{"label": "Full-time"}], "description": {"text": "Drive vans."}}
    job.update(kw)
    return job


def _salary(range_, unit="HOUR"):
    return {"currencyCode": "GBP", "baseSalary": {"unitOfWork": unit, "range": range_}}


def run() -> list[str]:
    errs = []

    def check(name, got, want):
        if got != want:
            errs.append(f"unit: {name}: got {got!r}, expected {want!r}")

    # UK evidence for remote postings
    for text, want in [("Remote role, UK based", True), ("Open to workers in South Wales", True),
                       ("Based in New South Wales, Australia", False), ("Relocate to New England, USA", False),
                       ("Salary £40,000", True), ("Work from anywhere", False),
                       ("Remote - United Kingdom only", True), ("Scotland", True), ("Northern Ireland", True)]:
        check(f"UK_EVIDENCE[{text!r}]", bool(UK_EVIDENCE.search(text)), want)

    ind = IndeedBoard()
    for label, rng, want in [("range", {"min": 10.0, "max": 12.5}, (10.0, 12.5)),
                             ("exact", {"value": 15.0}, (15.0, 15.0)),
                             ("at least", {"min": 20.0}, (20.0, None)),
                             ("at most", {"max": 30.0}, (None, 30.0)),
                             ("empty", {}, (None, None))]:
        j = ind.parse(_indeed_job(compensation=_salary(rng))).job
        check(f"indeed salary {label}", (j["salary_min"], j["salary_max"]), want)
        check(f"indeed salary period {label}", j["salary_period"], "hour" if want != (None, None) else None)
    check("indeed logo", ind.parse(_indeed_job()).job["company_logo_url"], "https://img/x.png")
    check("indeed week salary is null", ind.parse(_indeed_job(compensation=_salary({"min": 500.0}, "WEEK"))).job["salary_min"], None)

    remote = ind.parse(_indeed_job(location={"countryCode": "GB", "formatted": {"long": "Remote"}}))
    check("remote without UK evidence -> skip", (remote.kind, remote.reason), ("skip", "remote_without_uk_evidence"))
    check("remote skip is not permanent", remote.permanent, False)
    check("remote with UK evidence -> job", ind.parse(_indeed_job(
        location={"countryCode": "GB", "formatted": {"long": "Remote"}},
        description={"text": "Fully remote, open to UK-based candidates."})).kind, "job")
    check("indeed non-GB -> skip", ind.parse(_indeed_job(location={"countryCode": "US", "formatted": {"long": "Austin"}})).kind, "skip")
    check("indeed non-GB skip is permanent", ind.parse(_indeed_job(location={"countryCode": "US", "formatted": {"long": "x"}})).permanent, True)
    check("indeed expired", ind.parse(_indeed_job(expired=True)).kind, "expired")
    check("indeed empty description", ind.parse(_indeed_job(description={"text": " "})).kind, "failed")

    for loc, want in [("London, England, United Kingdom", True), ("Dublin, County Dublin, Ireland", False),
                      (None, True), ("", True), ("New York, United States", False)]:
        check(f"linkedin _is_uk[{loc!r}]", LinkedInBoard._is_uk(loc), want)
    for text, want in [("£33,000.00/yr - £42,300.00/yr", (33000.0, 42300.0, "year")),
                       ("£15.50/hr - £18.00/hr", (15.5, 18.0, "hour")),
                       ("$90,000/yr - $120,000/yr", (None, None, None)), (None, (None, None, None))]:
        check(f"linkedin _salary[{text!r}]", LinkedInBoard._salary(text), want)

    check("reed hidden salary -> null", ReedBoard._salary({"displaySalary": "Competitive salary", "from": 20000, "to": 100000,
                                                            "type": {"name": "per annum"}}), (None, None, None))
    check("reed shown salary", ReedBoard._salary({"displaySalary": "£30,000 - £40,000 per annum", "from": 30000, "to": 40000,
                                                   "type": {"name": "per annum"}}), (30000, 40000, "year"))
    check("html_to_text lists", html_to_text("<ul><li>One</li><li>Two</li></ul><p>End</p>"), "- One\n- Two\n\nEnd")
    check("html_to_text drops escaped tags", html_to_text("&lt;a href='x'&gt;Apply&lt;/a&gt; now &lt;img src='data:image/png;base64,AAAA'&gt;"),
          "Apply now")
    check("html_to_text keeps non-HTML angle text", html_to_text("&lt;JF-FA-ES&gt; template"), "<JF-FA-ES> template")
    check("title keeps role in brackets", norm_title("Member of Technical Staff (Backend Software)"),
          "member of technical staff backend software")
    check("title drops boilerplate brackets", norm_title("Care Assistant (m/f/d) (Permanent) [REF123]"), "care assistant")
    row = lambda i, src, title: {"id": i, "source": src, "title": title, "company": "Perplexity", "location": "London",
                                 "description": "Join us to build answer engines. " * 20}
    groups = build_clusters([row("indeed:1", "indeed", "Member of Technical Staff"),
                             row("linkedin:1", "linkedin", "Member of Technical Staff (AI Research)"),
                             row("linkedin:2", "linkedin", "Member of Technical Staff (Backend Software)")])
    check("distinct roles on one board never share a cluster", all(len({i for i in g if i.startswith("linkedin")}) < 2 for g in groups), True)
    uf = UnionFind()
    uf.register("a", "reed", "net developer"); uf.register("b", "reed", "net developer"); uf.register("c", "reed", "java developer")
    uf.union("a", "b")
    check("reposts of one role may merge", uf.compatible("a", "b"), True)
    check("different roles on one board may not", uf.compatible("a", "c"), False)
    desc = "Join our team to deliver great customer service every day. " * 15
    mk = lambda i, src, title, first, canon=None: {"id": i, "source": src, "title": title, "company": "Acme",
                                                   "location": "Leeds", "description": desc, "first": first, "canonical": canon}
    copies = [mk("indeed:1", "indeed", "Care Assistant", 1), mk("reed:1", "reed", "Care Assistant", 2)]
    first = assign_canonical(copies)
    check("copies share the oldest id", first, {"indeed:1": "indeed:1", "reed:1": "indeed:1"})
    for d in copies:
        d["canonical"] = first[d["id"]]
    check("canonical ids are stable on rerun", assign_canonical(copies), first)
    stale = [mk("indeed:2", "indeed", "Care Assistant", 1, "indeed:2"), mk("reed:2", "reed", "Chef", 2, "indeed:2")]
    check("a link the rules no longer support is dropped", assign_canonical(stale),
          {"indeed:2": "indeed:2", "reed:2": "reed:2"})
    split = [mk("indeed:3", "indeed", "Care Assistant", 1, "indeed:3"), mk("reed:3", "reed", "Care Assistant", 2, "indeed:3"),
             mk("linkedin:3", "linkedin", "Chef", 3, "indeed:3"), mk("reed:4", "reed", "Chef", 4, "indeed:3")]
    check("a split cluster keeps its id on the oldest part only", assign_canonical(split),
          {"indeed:3": "indeed:3", "reed:3": "indeed:3", "linkedin:3": "linkedin:3", "reed:4": "linkedin:3"})
    print("unit checks:", "OK" if not errs else "FAILED")
    return errs
