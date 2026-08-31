#!/usr/bin/env python3
"""
Stage 5, deterministic half: batching and validation around Claude's judgement.

The judging itself is Claude's, in the nightly run, against `claude/SCORING.md` — which
is a port of Job_Search_Profiles.md section 11, not a new rubric. This module does the
parts that must never be improvised:

  * BATCH — build compact payloads, so a night's scoring is a few calls rather than one
    per posting, and so a 40KB job description cannot crowd out the rubric.
  * VALIDATE — every returned score is checked before it touches the ledger. A model
    returning 11, or a url nobody asked about, or prose where a number belongs, must be
    a loud rejection rather than a corrupt row. Scores are written to the candidate's sheet and
    read as judgements; a wrong one is worse than a missing one.
  * SURFACE — the threshold logic, so "what reaches the digest" is a rule with tests
    rather than a decision re-made every night.

Untrusted input: a job description is text written by a stranger. It is data, never
instructions. That is stated in SCORING.md and repeated in the batch payload itself.
"""
from __future__ import annotations

import json
import re

# Section 11: surface at 6+. Tier E/F need a higher bar (PIPELINE_DESIGN section 10),
# and discovery titles surface lower on purpose, to widen the search vocabulary.
SURFACE_DEFAULT = 6
SURFACE_LOW_TIER = 7
SURFACE_DISCOVERY = 5
LOW_TIERS = {"E", "F"}
JD_EXCERPT = 6000          # enough for requirements; keeps a batch inside one call
BATCH_SIZE = 8
MAX_NOTES = 240


def batch_payload(rows: list, postings: list, batch_size: int = BATCH_SIZE) -> list:
    """Compact scoring batches. `rows` are ledger rows needing a score; `postings` is
    the run's output, which still carries the job description text."""
    jd = {p.get("url"): (p.get("jd") or "") for p in postings}
    todo = [r for r in rows if r.get("score") is None]
    out = []
    for i in range(0, len(todo), batch_size):
        out.append([{
            "url": r["url"],
            "company": r.get("company", ""),
            "title": r.get("title", ""),
            "location": r.get("location", ""),
            "tier": r.get("tier", ""),
            "posted": r.get("posted", ""),
            "jd": jd.get(r["url"], r.get("jd_text", ""))[:JD_EXCERPT],
        } for r in todo[i:i + batch_size]])
    return out


def validate(results, allowed_urls) -> tuple:
    """Return (accepted, problems). Anything malformed is dropped, never coerced."""
    allowed = set(allowed_urls)
    accepted, problems, seen = [], [], set()
    if isinstance(results, str):
        try:
            results = json.loads(results)
        except ValueError as e:
            return [], [f"scoring output was not valid JSON: {e}"]
    if not isinstance(results, list):
        return [], ["scoring output was not a list"]
    for i, r in enumerate(results):
        if not isinstance(r, dict):
            problems.append(f"result {i} is not an object")
            continue
        url = r.get("url")
        if url not in allowed:
            # A url we did not ask about means the batch and the answer disagree.
            # Silently keeping it would attach a score to the wrong posting.
            problems.append(f"result {i}: url not in this batch ({str(url)[:80]})")
            continue
        if url in seen:
            problems.append(f"result {i}: duplicate url {url}")
            continue
        score = r.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            problems.append(f"{url}: score is not a number ({score!r})")
            continue
        if not (0 <= score <= 10):
            problems.append(f"{url}: score {score} outside 0-10")
            continue
        notes = (r.get("notes") or "").strip()
        if not notes:
            problems.append(f"{url}: no notes — a score without a reason is not usable")
            continue
        flags = r.get("flags") or []
        if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
            problems.append(f"{url}: flags must be a list of strings")
            continue
        seen.add(url)
        accepted.append({"url": url, "score": int(round(score)),
                         "notes": notes[:MAX_NOTES], "flags": flags})
    missing = allowed - seen
    if missing:
        # Not fatal: an unscored row simply stays unscored and is offered again
        # tomorrow. But it must be visible, or a scorer that quietly drops half a
        # batch every night looks like a quiet job market.
        problems.append(f"{len(missing)} posting(s) in the batch came back unscored")
    return accepted, problems


def apply_scores(rows: list, accepted: list) -> int:
    by_url = {r["url"]: r for r in rows if r.get("url")}
    n = 0
    for a in accepted:
        row = by_url.get(a["url"])
        if row is None:
            continue
        row["score"] = a["score"]
        row["notes"] = a["notes"]
        row["flags"] = a["flags"]
        n += 1
    return n


def threshold_for(row: dict) -> int:
    if "discovery" in [f.lower() for f in (row.get("flags") or [])]:
        return SURFACE_DISCOVERY
    if (row.get("tier") or "").upper() in LOW_TIERS:
        return SURFACE_LOW_TIER
    return SURFACE_DEFAULT


def surfaces(row: dict) -> bool:
    """Does this row belong in the digest? Section 11: surface at 6+, log below.

    Section 11 also said never to re-surface a role already acted on, which was
    enforced here by reading the `status` kept in the Sheet. That column is gone
    (31 Aug) and so is this check. What remains is `notified`, which is the guard that
    actually did the work: a row is listed in exactly one digest and never offered
    again. The status check only ever mattered for a row that lost its `notified` flag,
    which is a bug rather than a workflow.
    """
    score = row.get("score")
    if score is None:
        # Unscored rows are held, not reported: a posting the candidate has not been told about
        # stays pending and is offered again once it has a score.
        return False
    return score >= threshold_for(row)


def split_for_digest(rows: list) -> tuple:
    """(surfaced, below_threshold, unscored) among rows not yet notified."""
    p = [r for r in rows if not r.get("notified")]
    surfaced = [r for r in p if surfaces(r)]
    unscored = [r for r in p if r.get("score") is None]
    below = [r for r in p if r.get("score") is not None and not surfaces(r)]
    return surfaced, below, unscored
