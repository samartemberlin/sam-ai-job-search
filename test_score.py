#!/usr/bin/env python3
"""
Scoring-stage tests. A score is written into the candidate's sheet and read as a judgement, so a
wrong one is worse than a missing one: every check here prefers dropping a result to
coercing it.
"""
import sys

import score as S

fails = []


def check(label, cond):
    if not cond:
        fails.append(label)


def row(**kw):
    d = dict(url="u1", company="atmosfair", title="Projektmanager", location="Berlin",
             tier="A", posted="2026-08-01", score=None, notes="", flags=[],
             notified=False, jd_text="")
    d.update(kw)
    return d


# --- batching -----------------------------------------------------------------
rows = [row(url=f"u{i}") for i in range(20)]
posts = [{"url": f"u{i}", "jd": "x" * 99999} for i in range(20)]
b = S.batch_payload(rows, posts, batch_size=8)
check("batches are chunked", [len(x) for x in b] == [8, 8, 4])
check("jd is excerpted", all(len(p["jd"]) <= S.JD_EXCERPT for c in b for p in c))
check("batch carries what the rubric needs",
      set(b[0][0]) == {"url", "company", "title", "location", "tier", "posted", "jd"})
check("already-scored rows are not re-sent",
      S.batch_payload([row(score=7)], posts) == [])
check("falls back to held text when the run has no jd",
      S.batch_payload([row(url="z", jd_text="held body")], [])[0][0]["jd"] == "held body")

# --- validation: the half that protects the ledger -----------------------------
ok, probs = S.validate([{"url": "u1", "score": 7, "notes": "good fit", "flags": []}], {"u1"})
check("a well-formed result is accepted", len(ok) == 1 and ok[0]["score"] == 7)
check("a complete batch reports no problems", probs == [])

bad = [
    ({"url": "u1", "score": 11, "notes": "n"}, "score 11 outside range"),
    ({"url": "u1", "score": -1, "notes": "n"}, "negative score"),
    ({"url": "u1", "score": "seven", "notes": "n"}, "score as prose"),
    ({"url": "u1", "score": True, "notes": "n"}, "boolean is not a score"),
    ({"url": "u1", "score": 7, "notes": ""}, "score without a reason"),
    ({"url": "u1", "score": 7, "notes": "n", "flags": "3+ years"}, "flags not a list"),
    ({"url": "other", "score": 7, "notes": "n"}, "url not in the batch"),
]
for payload, label in bad:
    a, p = S.validate([payload], {"u1"})
    check(f"rejects: {label}", a == [] and p)

a, p = S.validate([{"url": "u1", "score": 7, "notes": "a"},
                   {"url": "u1", "score": 3, "notes": "b"}], {"u1"})
check("duplicate urls are rejected", len(a) == 1 and any("duplicate" in x for x in p))
a, p = S.validate("not json at all", {"u1"})
check("non-JSON output is a problem, not a crash", a == [] and p)
a, p = S.validate('[{"url":"u1","score":6,"notes":"ok"}]', {"u1"})
check("a JSON string is parsed", len(a) == 1)
a, p = S.validate([{"url": "u1", "score": 6.6, "notes": "ok"}], {"u1"})
check("a float score is rounded", a[0]["score"] == 7)
a, p = S.validate([], {"u1", "u2"})
check("a silently dropped batch is reported", any("unscored" in x for x in p))
a, p = S.validate([{"url": "u1", "score": 5, "notes": "x" * 999}], {"u1"})
check("notes are truncated, not rejected", len(a[0]["notes"]) == S.MAX_NOTES)

# --- applying ------------------------------------------------------------------
rs = [row(url="u1"), row(url="u2")]
n = S.apply_scores(rs, [{"url": "u1", "score": 8, "notes": "strong", "flags": ["3+ years"]}])
check("apply writes score, notes and flags",
      n == 1 and rs[0]["score"] == 8 and rs[0]["flags"] == ["3+ years"])
check("apply leaves other rows alone", rs[1]["score"] is None)
check("apply ignores unknown urls",
      S.apply_scores(rs, [{"url": "ghost", "score": 9, "notes": "x", "flags": []}]) == 0)

# --- thresholds ----------------------------------------------------------------
check("tier A surfaces at 6", S.surfaces(row(score=6)))
check("tier A held below 6", not S.surfaces(row(score=5)))
check("tier E needs 7", not S.surfaces(row(score=6, tier="E")))
check("tier E surfaces at 7", S.surfaces(row(score=7, tier="E")))
check("discovery titles surface at 5", S.surfaces(row(score=5, flags=["discovery"])))
check("discovery flag is case-insensitive", S.surfaces(row(score=5, flags=["Discovery"])))
check("unscored never surfaces", not S.surfaces(row(score=None)))
# The status guard is gone with the Sheet's status column (31 Aug). `notified` is what
# actually prevents a second showing, so assert THAT, and assert the dead field really
# is dead - a leftover reader would suppress rows on a field nothing writes any more.
check("a leftover status field no longer suppresses a high score",
      S.surfaces(row(score=10, status_mirror="applied")))
check("scoring exposes no status vocabulary",
      not any("status" in n for n in dir(S)))

surfaced, below, unscored = S.split_for_digest(
    [row(url="a", score=8), row(url="b", score=2), row(url="c", score=None),
     row(url="d", score=9, notified=True)])
check("digest split: surfaced", [r["url"] for r in surfaced] == ["a"])
check("digest split: below threshold", [r["url"] for r in below] == ["b"])
check("digest split: unscored", [r["url"] for r in unscored] == ["c"])
check("digest split ignores already-notified rows",
      all(r["url"] != "d" for r in surfaced + below + unscored))

TOTAL = 36
for f in fails:
    print("FAIL", f)
print(f"\n{TOTAL - len(fails)}/{TOTAL} passed")
sys.exit(1 if fails else 0)
