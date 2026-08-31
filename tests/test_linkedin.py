#!/usr/bin/env python3
"""
Parser regression tests for the LinkedIn cards, against a saved response.

Why a fixture: this is the one source whose shape we do not control and cannot
re-request cheaply. A silent parser break here looks exactly like "LinkedIn had no jobs
today" - the same failure class as every other bug in this project. The fixture makes
the break loud without touching the network.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline import parse_li_cards, li_priority, enrich_descriptions  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE = open(os.path.join(HERE, "linkedin_search_sample.html"), encoding="utf-8").read()

fails = []


def check(label, cond):
    if not cond:
        fails.append(label)


cards = parse_li_cards(SAMPLE)
check("parses at least one card", len(cards) >= 1)
check("every card has a title", all(c["title"] for c in cards))
check("every card has a company", all(c["company"] for c in cards))
check("every card has a location", all(c["location"] for c in cards))
check("every url is a jobs/view link", all(
    c["url"].startswith("https://www.linkedin.com/jobs/view/") for c in cards))
check("source_id is numeric", all(c["source_id"].isdigit() for c in cards))
check("posted is an ISO date", all(
    c["posted"] and len(c["posted"]) == 10 and c["posted"][4] == "-" for c in cards))
check("urls are unique", len({c["url"] for c in cards}) == len(cards))
check("ats is tagged", all(c["ats"] == "linkedin" for c in cards))
# Phase one must NOT carry a description - the two-pass design depends on it.
check("descriptions are empty in phase one", all(c["jd"] == "" for c in cards))
# HTML entities and tags must be gone from the visible fields.
check("no markup leaks into titles", not any("<" in c["title"] for c in cards))
check("no entities leak into titles", not any("&amp;" in c["title"] for c in cards))
# Degenerate input must yield nothing rather than raising.
check("empty input is empty output", parse_li_cards("") == [])
check("junk input is empty output", parse_li_cards("<html><li>nope</li></html>") == [])

# --- budget policy: the cap has to choose between cards, so how it chooses matters ---
KW = ["assistenz", "assistant", "projektmanager", "coordinator"]
check("target title sorts first", li_priority("Projektassistenz (m/w/d)", KW) == 0)
check("off-profile title sorts last", li_priority("Senior SAP Architect", KW) == 1)
check("empty title does not crash", li_priority(None, KW) == 1)
check("no keywords means no preference", li_priority("Anything", []) == 1)

# A posting already in the ledger must never have its description bought twice.
known = {"https://www.linkedin.com/jobs/view/1/"}
posts = [{"ats": "linkedin", "url": u, "jd": "", "source_id": "1", "title": "x"}
         for u in ("https://www.linkedin.com/jobs/view/1/",)]
done, probs = enrich_descriptions(posts, cap=10, delay=0, known=known)
check("known postings are never re-fetched", done == 0 and probs == [])
check("known postings are marked", posts[0].get("already_known") is True)

# Over-cap must be reported, never silently truncated.
many = [{"ats": "linkedin", "url": f"u{i}", "jd": "", "source_id": str(i), "title": "x"}
        for i in range(5)]
_, probs2 = enrich_descriptions(many, cap=0, delay=0)
check("over-cap is reported", probs2 and "cap" in probs2[0]["error"])

for f in fails:
    print("FAIL", f)
print(f"\n{24 - len(fails)}/24 passed ({len(cards)} cards in fixture)")
sys.exit(1 if fails else 0)
