#!/usr/bin/env python3
"""
Deterministic half of the job pipeline: fetch -> normalize -> dedupe -> hard-filter.

No third-party dependencies (the nightly container cannot install any). Python 3.9+.
This module never calls Claude and never makes a judgement call: everything here is a
literal field check or a regex, and every rejection records the rule that fired plus the
text that triggered it, so the rules can be debugged from the output alone.

Usage:
    python3 pipeline.py                    # fetch all sources, filter, write out/
    python3 pipeline.py --source Ecosia    # one company
    python3 pipeline.py --offline          # re-filter cached raw.json without refetching
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import html.parser
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "out")
UA = "ai-job-search/1.0 (personal job search; contact via repo)"
TIMEOUT = 30


# --------------------------------------------------------------------------- io

def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


def get_json(url: str):
    return json.loads(get(url))


# ------------------------------------------------------------------ html -> text

class _Text(html.parser.HTMLParser):
    """Flatten HTML to readable plain text, keeping list and paragraph breaks."""

    SKIP = {"script", "style", "head"}
    BREAK_BEFORE = {"p", "div", "br", "h1", "h2", "h3", "h4", "ul", "ol", "table", "tr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag in self.BREAK_BEFORE:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def to_text(raw: str | None) -> str:
    """HTML (or already-plain text) -> normalized plain text.

    Unescapes BEFORE stripping tags. Personio wraps HTML inside XML, so the markup
    arrives double-encoded (`&lt;p&gt;`); stripping first and unescaping second
    leaves live tags and CSS in the output, which then show up as rule evidence.
    """
    if not raw:
        return ""
    if "&lt;" in raw or "&amp;" in raw:
        raw = html.unescape(raw)
    if "<" in raw and ">" in raw:
        p = _Text()
        try:
            p.feed(raw)
            raw = "".join(p.parts)
        except Exception:
            raw = re.sub(r"<[^>]+>", " ", raw)
    raw = html.unescape(raw)
    raw = raw.replace("\xa0", " ").replace("\r", "")
    raw = re.sub(r"[ \t]+", " ", raw)
    raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
    return "\n".join(line.strip() for line in raw.split("\n")).strip()


# ---------------------------------------------------------------------- adapters
# Every adapter returns dicts with the same keys. Missing values are None, never
# omitted, so downstream rules can rely on the key existing.

def _posting(**kw) -> dict:
    base = dict(company=None, title=None, location=None, url=None, jd=None,
                employment_type=None, seniority=None, remote=None,
                deadline=None, posted=None, ats=None, source_id=None)
    base.update(kw)
    return base


def fetch_greenhouse(slug: str, company: str) -> list[dict]:
    d = get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    out = []
    for j in d.get("jobs", []):
        out.append(_posting(
            company=j.get("company_name") or company,
            title=j.get("title"),
            location=(j.get("location") or {}).get("name"),
            url=j.get("absolute_url"),
            jd=to_text(j.get("content")),
            deadline=j.get("application_deadline"),
            posted=(j.get("first_published") or "")[:10] or None,
            ats="greenhouse", source_id=str(j.get("id")),
        ))
    return out


def fetch_ashby(slug: str, company: str) -> list[dict]:
    d = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
    out = []
    for j in d.get("jobs", []):
        if j.get("isListed") is False:
            continue
        out.append(_posting(
            company=company,
            title=j.get("title"),
            location=j.get("location"),
            url=j.get("jobUrl") or j.get("applyUrl"),
            jd=to_text(j.get("descriptionPlain") or j.get("descriptionHtml")),
            employment_type=j.get("employmentType"),
            remote=j.get("isRemote"),
            posted=(j.get("publishedAt") or "")[:10] or None,
            ats="ashby", source_id=str(j.get("id")),
        ))
    return out


def fetch_personio(slug: str, company: str) -> list[dict]:
    # Personio serves descriptions per language, and which variant is populated differs
    # per board: a German employer's `?language=en` feed returns every posting with an
    # EMPTY <jobDescriptions> block (Falling Walls: 4KB vs 40KB), while an English
    # employer's default feed is the empty one (LiveEO: 47KB vs 260KB). Either way the
    # listing still parses and looks healthy, so the text rules silently have nothing to
    # match and the whole board passes the filter untested.
    #
    # A document-level "does it contain CDATA at all" check is not enough - LiveEO's
    # default has CDATA on a minority of postings. So fetch both and keep whichever
    # actually populates more descriptions. Two small requests per Personio board.
    def _parse(raw: bytes) -> list[dict]:
        rows = []
        for p in ET.fromstring(raw).findall(".//position"):
            g = lambda t: (p.findtext(t) or "").strip() or None
            desc = p.find("jobDescriptions")
            jd = to_text(ET.tostring(desc, encoding="unicode")) if desc is not None else ""
            pid = g("id")
            row = _posting(
                company=company,
                title=g("name"),
                location=g("office"),
                url=f"https://{slug}.jobs.personio.de/job/{pid}" if pid else None,
                jd=jd,
                employment_type=g("employmentType"),
                seniority=g("seniority"),
                posted=(g("createdAt") or "")[:10] or None,
                ats="personio", source_id=pid,
            )
            # Kept for the identity check in fetch_all - Personio names the owning legal
            # entity here on some boards and only in the page <title> on others.
            row["subcompany"] = g("subcompany")
            rows.append(row)
        return rows

    best: list[dict] = []
    best_filled = -1
    for suffix in ("", "?language=en"):
        try:
            rows = _parse(get(f"https://{slug}.jobs.personio.de/xml{suffix}"))
        except Exception:
            continue
        filled = sum(1 for r in rows if (r.get("jd") or "").strip())
        if filled > best_filled:
            best, best_filled = rows, filled
    return best


def fetch_lever(slug: str, company: str) -> list[dict]:
    d = get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    out = []
    for j in d:
        cats = j.get("categories") or {}
        out.append(_posting(
            company=company,
            title=j.get("text"),
            location=cats.get("location"),
            url=j.get("hostedUrl"),
            jd=to_text(j.get("descriptionPlain") or j.get("description")),
            employment_type=cats.get("commitment"),
            posted=dt.datetime.utcfromtimestamp(j["createdAt"] / 1000).date().isoformat()
                   if j.get("createdAt") else None,
            ats="lever", source_id=j.get("id"),
        ))
    return out


def fetch_smartrecruiters(slug: str, company: str) -> list[dict]:
    d = get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100")
    out = []
    for j in d.get("content", []):
        loc = j.get("location") or {}
        detail = {}
        try:  # SmartRecruiters keeps the description on the detail endpoint only
            detail = get_json(
                f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{j['id']}")
        except Exception:
            pass
        sections = ((detail.get("jobAd") or {}).get("sections") or {})
        jd = "\n\n".join(
            to_text((sections.get(k) or {}).get("text"))
            for k in ("jobDescription", "qualifications", "additionalInformation")
            if (sections.get(k) or {}).get("text")
        )
        out.append(_posting(
            company=company,
            title=j.get("name"),
            location=", ".join(x for x in (loc.get("city"), loc.get("country")) if x) or None,
            url=(j.get("applyUrl") or (j.get("ref") or "")),
            jd=jd,
            employment_type=(j.get("typeOfEmployment") or {}).get("label"),
            posted=(j.get("releasedDate") or "")[:10] or None,
            ats="smartrecruiters", source_id=j.get("id"),
        ))
    return out


def fetch_recruitee(slug: str, company: str) -> list[dict]:
    d = get_json(f"https://{slug}.recruitee.com/api/offers/")
    out = []
    for j in d.get("offers", []):
        out.append(_posting(
            company=company,
            title=j.get("title"),
            location=j.get("location") or j.get("city"),
            url=j.get("careers_url") or j.get("url"),
            jd=to_text(j.get("description")) + "\n\n" + to_text(j.get("requirements")),
            employment_type=j.get("employment_type_code") or j.get("employment_type"),
            posted=(j.get("published_at") or "")[:10] or None,
            ats="recruitee", source_id=str(j.get("id")),
        ))
    return out


# ---------------------------------------------------------------- source sanity
# Two ways a source can look healthy and be worthless. Both return HTTP 200 with
# well-formed data, so nothing downstream errors - the same silent-failure class as the
# empty-description and double-encoding bugs.
#
# 1. A Personio board can be an UNCLAIMED OR UNCONFIGURED ACCOUNT that happens to sit on
#    the slug we guessed. It serves Personio's onboarding demo content ("SEO Marketing
#    Manager" in London, "Social Media (Werkstudent)", "Teststelle") with real posting
#    ids and full description bodies. Probing on 2026-08-29 found four:
#    wwf-deutschland, adelphi, ecosia, germanwatch, dgap.
#
#    WWF Deutschland publishes vacancies on its own website and uses no ATS at all, so
#    `wwf-deutschland.jobs.personio.de` was never WWF's board. That is the real failure:
#    not "Personio serves demo data" but "we asked for a board that is not theirs and
#    something answered". Matching demo TITLES would only catch the sample sets we have
#    already seen, and would never catch a slug occupied by a different real company.
#
#    So the check is identity, not content: does the board declare WHOSE it is?
#    Personio exposes the organisation in two places, and real boards use one or the
#    other - neither alone is sufficient (verified across 17 boards on 2026-08-29):
#      - the board page <title>: "Jobs bei atmosfair gGmbH"  (ECFR, RSF, LiveEO, juna.ai)
#      - <subcompany> on a position                          (BUND, CORRECTIV)
#    Every unclaimed board had BOTH empty; every real board had at least one.
#
# 2. SmartRecruiters answers /v1/companies/<anything>/postings with 200 and
#    totalFound: 0. Every one of twenty nonexistent slugs "succeeded". For that ATS a
#    200 is not evidence the slug is real; only postings > 0 is.


def personio_board_owner(slug: str, postings: list[dict]) -> str | None:
    """The organisation a Personio board declares itself to belong to, or None if it
    declares none - which means the account was never configured and the board is not
    the organisation we asked for."""
    for p in postings:                      # <subcompany>, free - already in the XML
        if (p.get("subcompany") or "").strip():
            return p["subcompany"].strip()
    try:                                    # board page <title>, one extra request
        html = get(f"https://{slug}.jobs.personio.de/").decode("utf-8", "replace")
    except Exception:
        return None
    m = re.search(r"<title>(.*?)</title>", html, re.S)
    if not m:
        return None
    # "Jobs bei <Org>" / "Jobs at <Org>" - an unconfigured board renders the prefix with
    # nothing after it.
    name = re.sub(r"^\s*Jobs\s+(bei|at)\s*", "", m.group(1).strip(), flags=re.I).strip()
    return name or None


def _norm(s: str) -> set:
    return {w for w in re.split(r"[^a-z0-9]+", (s or "").lower()) if len(w) > 2} - {
        "gmbh", "ggmbh", "the", "und", "for", "eingetragener", "verein", "deutschland",
        "germany", "international", "foundation", "stiftung", "group",
    }


def owner_mismatch(expected: str, declared: str) -> bool:
    """True when a board's declared owner shares no meaningful word with the company we
    think we are fetching. Legal names differ from common names ('ProVeg International'
    vs ProVeg, 'Bund fuer Umwelt und Naturschutz Deutschland' vs BUND), so this only
    warns - it must never drop a board on its own."""
    a, b = _norm(expected), _norm(declared)
    if not a or not b:
        return False
    if a & b or any(x in y or y in x for x in a for y in b):
        return False
    # Acronyms: "DRI HQ" really is Democracy Reporting International, "ECFR" really is
    # the European Council on Foreign Relations. Without this the warning fires on
    # exactly the sources we most want to keep.
    def initials(words: str) -> str:
        return "".join(w[0] for w in re.split(r"[^A-Za-z]+", words) if w).lower()
    ia, ib = initials(expected), initials(declared)
    for tok in _norm(declared) | {t.lower() for t in re.split(r"[^A-Za-z]+", declared) if t}:
        if tok and (tok == ia or ia.startswith(tok) or tok.startswith(ia)) and len(tok) > 1:
            return False
    for tok in _norm(expected) | {t.lower() for t in re.split(r"[^A-Za-z]+", expected) if t}:
        if tok and (tok == ib or ib.startswith(tok) or tok.startswith(ib)) and len(tok) > 1:
            return False
    return True


# ------------------------------------------------------------------- linkedin
# LinkedIn is the only source that cannot hand us a description with the listing.
# The guest search endpoint returns cards (title, company, location, id, date); the
# body needs a SECOND request per posting. That inverts the pipeline's usual order:
# everywhere else we fetch everything and filter afterwards, because filtering is free.
# Here fetching is the expensive part, so the card-level rules run FIRST and only the
# survivors get their description fetched. See enrich_descriptions().
#
# Volume is a real constraint, not a theoretical one: automated access is against
# LinkedIn's terms, and the account at risk is the candidate's own during a job search. Hence the
# caps in sources.json, the delay between requests, and descriptions for survivors only
# - typically a handful a night rather than several hundred.
#
# Note the browser User-Agent. The module-level UA identifies the tool honestly, which
# is right for public ATS APIs; LinkedIn's guest pages return nothing useful for it.
LI_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
         "Chrome/126.0.0.0 Safari/537.36")
LI_SEARCH = ("https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
             "?keywords={kw}&location={loc}&f_TPR=r{secs}&start={start}")
LI_JOB = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{jid}"


def li_priority(title: str, keywords: list[str]) -> int:
    """Lower sorts first. Card-level rules can only see title and location, so ~90% of
    cards survive phase one and the cap has to choose between them. Choosing by feed
    order is arbitrary; choosing by how well the title matches the candidate's target titles
    spends the budget where it can pay off."""
    t = (title or "").lower()
    return 0 if any(k.lower() in t for k in keywords) else 1


def _li_get(url: str) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": LI_UA, "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read().decode("utf-8", "replace")


def _li_field(pattern: str, card: str) -> str | None:
    m = re.search(pattern, card, re.S)
    if not m:
        return None
    return html.unescape(re.sub(r"<[^>]+>", " ", m.group(1))).strip() or None


def parse_li_cards(body: str) -> list[dict]:
    """Split a guest-search response into postings. Pure function - the tests feed it
    saved HTML, so parser regressions are caught without touching the network."""
    out = []
    for card in re.split(r'<li>\s*(?=<div class="base-card)', body)[1:]:
        jid = re.search(r'data-entity-urn="urn:li:jobPosting:(\d+)"', card)
        title = _li_field(r'class="base-search-card__title"[^>]*>(.*?)</h3>', card)
        if not (jid and title):
            continue
        out.append(_posting(
            company=_li_field(r'class="base-search-card__subtitle"[^>]*>(.*?)</h4>', card),
            title=title,
            location=_li_field(r'class="job-search-card__location"[^>]*>(.*?)</span>', card),
            url=f"https://www.linkedin.com/jobs/view/{jid.group(1)}/",
            jd="",                       # deliberately empty - phase two fills it
            posted=_li_field(r'datetime="(.*?)"', card),
            ats="linkedin", source_id=jid.group(1),
        ))
    return out


def fetch_linkedin(cfg: dict, company: str = "LinkedIn") -> list[dict]:
    """Phase one: cards only. `cfg` is the `linkedin` block of sources.json."""
    import time
    seen, out, dropped = set(), [], []
    priority = cfg.get("priority_titles", []) if cfg.get("require_priority_title") else []
    days = int(cfg.get("recency_days", 7))
    per_query = int(cfg.get("max_pages_per_query", 2))
    delay = float(cfg.get("delay_seconds", 2))
    for kw in cfg.get("queries", []):
        for page in range(per_query):
            url = LI_SEARCH.format(kw=urllib.parse.quote(kw),
                                   loc=urllib.parse.quote(cfg.get("location", "Berlin, Germany")),
                                   secs=days * 86400, start=page * 10)
            try:
                cards = parse_li_cards(_li_get(url))
            except Exception:
                break                    # one bad query must not kill the whole source
            if not cards:
                break                    # no more pages
            for c in cards:
                if c["url"] in seen:
                    continue
                seen.add(c["url"])
                c["query"] = kw
                # Precision over recall, for this source only. Everywhere else we let
                # everything through and filter on the description, because that is
                # free. Here it costs a request per posting, and a broad query on
                # LinkedIn returns overwhelmingly off-profile senior and technical
                # roles: measured, 109 of 119 cards survived the card-level rules and
                # would each have cost a fetch. So a LinkedIn card must earn its
                # description by having an on-profile TITLE.
                #
                # This deliberately loses well-titled-differently postings. It is an
                # acceptable trade only because LinkedIn is Tier E and the ATS sources
                # reach the candidate's actual target employers directly - it is a top-up, not
                # the backbone. Widen `priority_titles` rather than disabling this.
                if priority and li_priority(c.get("title"), priority) != 0:
                    dropped.append(c.get("title"))
                    continue
                out.append(c)
            time.sleep(delay)
    if dropped:
        # Loud, not silent: this is a lossy filter and the run log should show its size.
        print(f"  linkedin: {len(dropped)} cards dropped for off-profile titles",
              file=sys.stderr)
    return out


def enrich_descriptions(postings: list[dict], cap: int, delay: float = 2.0,
                        known: set = frozenset(), priority_titles: list = ()) -> tuple:
    """Phase two: fetch the body for postings that survived the card-level rules.
    Returns (fetched_count, problems). Capped, because this is the expensive request."""
    import time
    todo = [p for p in postings if p.get("ats") == "linkedin"
            and not (p.get("jd") or "").strip()]
    problems, done = [], 0
    # A posting already in the ledger has been through this once and, if it survived,
    # has been reported. Re-buying its description every night is the single biggest
    # avoidable cost in the run.
    skipped_known = [p for p in todo if p.get("url") in known]
    todo = [p for p in todo if p.get("url") not in known]
    todo.sort(key=lambda p: (li_priority(p.get("title"), list(priority_titles)),
                             p.get("posted") or ""), reverse=False)
    if len(todo) > cap:
        # Truncating silently would look like a quiet night. Say it.
        problems.append({"company": "LinkedIn", "ats": "linkedin",
                         "error": f"{len(todo)} unseen survivors needed a description but the "
                                  f"cap is {cap}; {len(todo) - cap} were left without one and "
                                  f"are reported unfiltered by the text rules. Raise the cap or "
                                  f"narrow the queries if this persists"})
        todo = todo[:cap]
    for p in skipped_known:
        p["already_known"] = True
    for p in todo:
        try:
            p["jd"] = to_text(_li_get(LI_JOB.format(jid=p["source_id"])))
            done += 1
        except Exception as e:
            problems.append({"company": "LinkedIn", "ats": "linkedin",
                             "error": f"description fetch failed for {p['url']}: "
                                      f"{type(e).__name__}"})
        time.sleep(delay)
    return done, problems


ADAPTERS = {
    "greenhouse": fetch_greenhouse,
    "ashby": fetch_ashby,
    "personio": fetch_personio,
    "lever": fetch_lever,
    "smartrecruiters": fetch_smartrecruiters,
    "recruitee": fetch_recruitee,
}


# ------------------------------------------------------------------------- rules

def load_rules(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)["rules"]


def _haystack(post: dict, where: str) -> str:
    if where == "text":
        return f"{post.get('title') or ''}\n{post.get('jd') or ''}"
    return str(post.get(where) or "")


def apply_rules(post: dict, rules: list[dict], exclusions: list[dict]) -> str | None:
    """Return 'RULE_ID: "evidence"' for the first rule that rejects, else None.

    Rules are ordered: cheap exact field checks first, prose regexes last, so the
    recorded reason is the most defensible one available rather than whichever
    pattern happened to match first.
    """
    company = (post.get("company") or "").strip().lower()
    for ex in exclusions:
        if company and company == (ex.get("company") or "").strip().lower():
            return f'EXCL_MANUAL: "{ex.get("reason") or "on the exclusions list"}"'

    for rule in rules:
        where = rule.get("where", "text")
        hay = _haystack(post, where)
        # A field the feed did not supply cannot disqualify anything. Unknown is not
        # a failure: without this, every posting from a board with no `seniority`
        # field would be rejected by a `require` rule it was never able to answer.
        if not hay.strip() and rule["action"] == "require":
            continue
        pat = rule["pattern"]
        m = re.search(pat, hay, re.IGNORECASE | re.MULTILINE)
        if rule["action"] == "reject" and m:
            ev = m.group(0).strip()
            ctx = hay[max(0, m.start() - 40): m.end() + 40].replace("\n", " ").strip()
            return f'{rule["id"]}: "{ev}"  …{ctx}…' if len(ctx) > len(ev) else f'{rule["id"]}: "{ev}"'
        if rule["action"] == "require" and not m:
            return f'{rule["id"]}: "{hay.strip() or "(empty)"}"'
    return None


# --------------------------------------------------------------------------- run

def slugify(s: str, n: int = 40) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s[:n].strip("-") or "untitled"


def jd_filename(post: dict) -> str:
    d = post.get("first_seen") or dt.date.today().isoformat()
    return f"{d}_{slugify(post.get('company'), 24)}_{slugify(post.get('title'), 44)}.md"


def fetch_all(sources: list[dict], only: str | None) -> tuple[list[dict], list[dict]]:
    postings, problems = [], []
    for s in sources:
        if only and s["company"].lower() != only.lower():
            continue
        fn = ADAPTERS.get(s["ats"])
        if not fn:
            problems.append({"company": s["company"], "error": f"no adapter for {s['ats']}"})
            continue
        try:
            got = fn(s["slug"], s["company"])
            if s["ats"] == "personio" and got:
                owner = personio_board_owner(s["slug"], got)
                if not owner:
                    # Drop the whole board. Half-trusting it is worse than losing it:
                    # the postings are not this organisation's but carry its name.
                    #
                    # Sources known to be in this state keep their entry rather than
                    # being deleted - they are Tier A targets, and the run is how we
                    # learn the day a real board appears. Their message says "expected"
                    # so a newly broken source still stands out.
                    prefix = ("still unclaimed (expected, watching)"
                              if s.get("expect_unclaimed") else "UNCLAIMED BOARD")
                    problems.append({
                        "company": s["company"], "ats": s["ats"], "slug": s["slug"],
                        "expected": bool(s.get("expect_unclaimed")),
                        "error": f"{prefix} - the board declares no organisation name, so "
                                 f"it is an unconfigured Personio account, not "
                                 f"{s['company']}'s board. Its postings are demo content."})
                    continue
                if owner_mismatch(s.get("expect_name") or s["company"], owner):
                    # Not fatal - legal names differ from common ones. Loud, though:
                    # a slug can be occupied by a different real company.
                    problems.append({
                        "company": s["company"], "ats": s["ats"], "slug": s["slug"],
                        "error": f"board belongs to {owner!r}, which does not look like "
                                 f"{s['company']!r} - check the slug"})
            for p in got:
                p["tier"] = s.get("tier")
            postings.extend(got)
            if not got:
                # A valid board with zero jobs looks identical to a wrong slug.
                # Never swallow this: an unreachable watchlist entry is worse than a loud one.
                hint = ("verify the slug against the careers page")
                if s["ats"] == "smartrecruiters":
                    # 200 + totalFound:0 is what a NONEXISTENT SmartRecruiters slug
                    # returns. Reachability proves nothing here.
                    hint = ("SmartRecruiters returns 200 with 0 postings for slugs that "
                            "do not exist - this may be a wrong slug, not an empty board")
                problems.append({"company": s["company"], "ats": s["ats"], "slug": s["slug"],
                                 "error": f"board reachable but returned 0 postings - {hint}"})
            elif not any((p.get("jd") or "").strip() for p in got):
                # Listings without descriptions still look like a healthy run, but every
                # text rule then has nothing to match and the whole board passes the
                # filter untested. Loud failure, not a silent pass.
                problems.append({"company": s["company"], "ats": s["ats"], "slug": s["slug"],
                                 "error": f"{len(got)} postings but ALL job descriptions are "
                                          "empty - text rules cannot run on this source"})
        except urllib.error.HTTPError as e:
            problems.append({"company": s["company"], "ats": s["ats"], "slug": s["slug"],
                             "error": f"HTTP {e.code}"})
        except Exception as e:
            problems.append({"company": s["company"], "ats": s["ats"], "slug": s["slug"],
                             "error": f"{type(e).__name__}: {e}"})
    return postings, problems


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", help="only this company")
    ap.add_argument("--offline", action="store_true", help="re-filter out/raw.json")
    ap.add_argument("--known", help="file of URLs already in the ledger, one per line; "
                                    "their descriptions are never re-fetched")
    args = ap.parse_args()

    os.makedirs(os.path.join(OUT, "jd"), exist_ok=True)
    today = dt.date.today().isoformat()

    # --offline re-filters the cached raw.json, which by design holds the pre-enrichment
    # postings: LinkedIn descriptions are fetched AFTER raw.json is written. Writing an
    # offline pass back over postings.json therefore silently discards every description
    # the last live run paid for, and the next survivor count is quietly wrong - with no
    # error, which is this project's usual failure shape. Offline output goes to its own
    # file instead.
    out_name = "postings.offline.json" if args.offline else "postings.json"
    sources_cfg = {}
    known_urls = set()
    if args.known and os.path.exists(args.known):
        known_urls = {l.strip() for l in open(args.known, encoding="utf-8") if l.strip()}
    if args.offline:
        with open(os.path.join(OUT, "raw.json"), encoding="utf-8") as f:
            postings = json.load(f)
        problems = []
    else:
        with open(os.path.join(HERE, "sources.json"), encoding="utf-8") as f:
            sources_cfg = json.load(f)
        postings, problems = fetch_all(sources_cfg["sources"], args.source)
        li_cfg = sources_cfg.get("linkedin") or {}
        if li_cfg.get("enabled") and not args.source:
            try:
                cards = fetch_linkedin(li_cfg)
                for c in cards:
                    c["tier"] = li_cfg.get("tier", "E")
                postings.extend(cards)
                if not cards:
                    problems.append({"company": "LinkedIn", "ats": "linkedin",
                                     "error": "0 cards returned - guest endpoint may have "
                                              "changed shape, or the host is not whitelisted"})
            except Exception as e:
                problems.append({"company": "LinkedIn", "ats": "linkedin",
                                 "error": f"{type(e).__name__}: {e}"})
        with open(os.path.join(OUT, "raw.json"), "w", encoding="utf-8") as f:
            json.dump(postings, f, ensure_ascii=False, indent=1)

    # dedupe on url, the Sheet's key
    seen, deduped = set(), []
    for p in postings:
        if not p.get("url") or p["url"] in seen:
            continue
        seen.add(p["url"])
        p.setdefault("first_seen", today)
        deduped.append(p)

    rules = load_rules(os.path.join(HERE, "rules.json"))
    excl_path = os.path.join(HERE, "exclusions.json")
    exclusions = json.load(open(excl_path, encoding="utf-8")) if os.path.exists(excl_path) else []

    # Pass one. For LinkedIn the description is still empty here, so only the
    # card-level rules can fire - which is the point: they are free, the fetch is not.
    # `require` rules skip an empty field, so an absent description never rejects.
    for p in deduped:
        p["rejected_by"] = apply_rules(p, rules, exclusions)

    # Pass two: buy descriptions only for what survived, then re-run the full rules.
    li_cfg = (sources_cfg.get("linkedin") or {}) if not args.offline else {}
    if li_cfg.get("enabled"):
        survivors_needing_jd = [p for p in deduped if not p["rejected_by"]]
        got, li_problems = enrich_descriptions(
            survivors_needing_jd, int(li_cfg.get("max_descriptions_per_run", 25)),
            float(li_cfg.get("delay_seconds", 2)), known=known_urls,
            priority_titles=li_cfg.get("priority_titles", []))
        problems.extend(li_problems)
        for p in deduped:
            if p.get("ats") != "linkedin" or p["rejected_by"]:
                continue
            if (p.get("jd") or "").strip():
                p["rejected_by"] = apply_rules(p, rules, exclusions)
            else:
                # Survived the card rules but the description budget did not reach it.
                # Do NOT send it to scoring: Claude would be judging a title alone, and
                # a bad score is worse than no score because it is recorded as a
                # judgement. Hold it instead - it stays out of the ledger, so tomorrow
                # it is unseen again and competes for a fresh budget, highest-priority
                # titles first. Latency, not loss, exactly like `notified`.
                p["rejected_by"] = ("LI_NO_DESCRIPTION_YET: description budget exhausted; "
                                    "held for the next run")

    for p in deduped:
        p["jd_file"] = jd_filename(p)
        with open(os.path.join(OUT, "jd", p["jd_file"]), "w", encoding="utf-8") as f:
            f.write(f"# {p['title']} — {p['company']}\n\n"
                    f"{p.get('location') or ''} · {p.get('url') or ''}\n\n---\n\n{p.get('jd') or ''}\n")

    survivors = [p for p in deduped if not p["rejected_by"]]
    with open(os.path.join(OUT, out_name), "w", encoding="utf-8") as f:
        json.dump(deduped, f, ensure_ascii=False, indent=1)

    print(f"fetched   {len(postings)}")
    print(f"deduped   {len(deduped)}")
    print(f"rejected  {len(deduped) - len(survivors)}")
    print(f"to score  {len(survivors)}")
    if problems:
        print("\nproblems (never silent — a watchlist entry that fails quietly is worse):")
        for pr in problems:
            print(f"  ! {pr.get('company')}: {pr['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
