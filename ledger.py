#!/usr/bin/env python3
"""
Stage 7, deterministic half: the ledger.

State design (see claude/STATE_DESIGN.md in the project, plus the two amendments made
when it was reviewed):

  * The ledger is CANONICAL. The Google Sheet is a derived projection and is
    rebuildable from it, which is what makes a Drive outage a non-event.
  * SURVIVORS ONLY. Hard-rejected postings are not stored. They exist only to be
    deduped against, and re-rejecting one is free and silent - stage 4 is
    deterministic and makes no Claude call. Storing them would cost 80% of the state
    to buy something worse than nothing: it makes a false reject PERMANENT, because a
    posting rejected by a rule we later fix would already be deduped away and the fix
    could never rescue it. rules.json's own header names that hazard; three rules were
    fixed in the first two days, and each fix would have silently failed to recover the
    postings it was written for.
    Cost of the trade, named: a posting rejected in August that survives a September
    rule change gets September's first_seen. Accepted.
  * NO FIELD IS WRITTEN BY BOTH MACHINE AND HUMAN - now trivially, because the machine
    writes every field there is. The Sheet used to carry a `status` column the candidate owned,
    which the run read back into `status_mirror`; that is gone (31 Aug). The candidate
    tracks applications outside this system, so the run neither reads a sheet nor has any
    field it must not touch. The lossy markdown-table parse went with it.
  * A DAILY SHEET IS TODAY'S CATCH, NOT A DATABASE. Each sheet holds only the rows whose
    `first_seen` is that sheet's own date, so a posting caught yesterday never reappears
    in today's. That makes the sheet name and the rows agree by construction, which is
    why the sheet no longer carries a `first_seen` column - it would repeat its own
    title on every line. The FIELD stays in the ledger and is load-bearing there: it
    orders the rows, decides which held descriptions are dropped first when the budget
    is tight, and drives compaction.
  * `notified` is EXPLICIT, not a consequence of write order. If a run writes the ledger
    and then dies before the digest, those postings are already marked seen; without an
    explicit flag they would be deduped away and the candidate would never learn they existed -
    and it would look exactly like a quiet job market. A crashed run must cost a day of
    latency, not a posting. `drive_synced` does the same job for the archive step.

This module does the merge and nothing else: no network, no MCP, no credentials. The
session performs the transport, because pipeline code cannot call MCP tools. The merge
is the part that can destroy the candidate's work, so it is the part with tests.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys

SHARD = "claude/state/postings-{month}.jsonl"
RUNS = "claude/state/runs.jsonl"
DIGEST = "claude/daily/{date}.md"

# Written by the run. Everything else in a row is either the candidate's or the scorer's.
MACHINE_FIELDS = ("company", "title", "location", "jd", "tier", "ats", "posted")
NOTES_TTL_DAYS = 30      # drop the fat field after this
SHARD_TTL_DAYS = 90      # drop a shard once every row is safely in Drive
DIGEST_TTL_DAYS = 14

# A LinkedIn description is bought exactly once - the run deliberately never re-buys one
# it has already seen. So if the Drive archive step is skipped or fails that night, the
# text is gone with the container, and the 30 Aug run proved it: 29 postings kept only
# header stubs. The Drive step is "best effort" by design, which is right, but it must
# not be the only chance to keep the text.
#
# So an unarchived row carries its description IN the ledger until `drive_synced` flips,
# and drops it the moment the archive succeeds. Bounded: the hold is capped, and when
# over budget the ATS rows are dropped first because their text can simply be refetched
# from the feed tomorrow - LinkedIn's cannot.
JD_HOLD_BUDGET_BYTES = 400_000
JD_HOLD_TRUNCATE = 12_000
REFETCHABLE = {"greenhouse", "ashby", "personio", "lever", "smartrecruiters", "recruitee"}


# ------------------------------------------------------------------ jsonl io

def read_jsonl(text: str) -> list[dict]:
    """Tolerant by design: one corrupt line costs one posting, not the file."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def write_jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n"
                   for r in rows)


def shard_path(date: str) -> str:
    return SHARD.format(month=date[:7])


def shards_to_read(today: str, months: int = 3) -> list[str]:
    """Dedupe only needs recent shards: ATS feeds carry live postings only, so
    something that vanished months ago cannot come back as a duplicate."""
    y, m = int(today[:4]), int(today[5:7])
    out = []
    for _ in range(months):
        out.append(SHARD.format(month=f"{y:04d}-{m:02d}"))
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return out


# -------------------------------------------------------------------- merge

def merge(existing: list[dict], survivors: list[dict], today: str) -> tuple:
    """Fold a run's survivors into the ledger. Returns (rows, stats)."""
    by_url = {r["url"]: r for r in existing if r.get("url")}
    added = refreshed = 0
    for p in survivors:
        url = p.get("url")
        if not url:
            continue
        if url not in by_url:
            by_url[url] = {
                "url": url,
                "first_seen": p.get("first_seen") or today,
                "last_seen": today,
                "company": p.get("company") or "",
                "title": p.get("title") or "",
                "location": p.get("location") or "",
                "tier": p.get("tier") or "",
                "ats": p.get("ats") or "",
                "posted": p.get("posted") or "",
                "jd": p.get("jd_file") or "",
                "score": None,
                "notes": "",
                "notified": False,
                "drive_synced": False,
                "jd_text": (p.get("jd") or "")[:JD_HOLD_TRUNCATE],
            }
            added += 1
            continue
        row = by_url[url]
        for f in MACHINE_FIELDS:
            src = "jd_file" if f == "jd" else f
            # `jd` is the name of a file in the Drive archive, and the connector cannot
            # rewrite a file's content - only its metadata (RUNBOOK section 2). Once
            # `drive_synced` is true that file exists under the name stored here, so
            # refreshing it to tonight's date would silently point the sheet at a file
            # nobody ever wrote. The 31 Aug run caught this with 13 rows already
            # archived; it is invisible until the archive is a day older than the
            # ledger, which is why the night before looked fine.
            if f == "jd" and row.get("drive_synced"):
                continue
            if p.get(src) is not None:
                row[f] = p.get(src) or ""
        if not row.get("drive_synced") and (p.get("jd") or "").strip():
            row["jd_text"] = (p.get("jd") or "")[:JD_HOLD_TRUNCATE]
        row["last_seen"] = today          # first_seen never moves forward
        refreshed += 1
    rows = sorted(by_url.values(),
                  key=lambda r: (r.get("first_seen") or "", r.get("company") or ""))
    return rows, {"added": added, "refreshed": refreshed, "total": len(rows)}


def pending(rows: list[dict]) -> list[dict]:
    """Everything the candidate has not been told about yet, whichever run found it."""
    return [r for r in rows if not r.get("notified")]


def mark_notified(rows: list[dict], urls) -> int:
    urls = set(urls)
    n = 0
    for r in rows:
        if r.get("url") in urls and not r.get("notified"):
            r["notified"] = True
            n += 1
    return n


def mark_drive_synced(rows: list[dict], urls) -> int:
    """Flipping this releases the held description - Drive is now the archive."""
    urls = set(urls)
    n = 0
    for r in rows:
        if r.get("url") in urls and not r.get("drive_synced"):
            r["drive_synced"] = True
            r["jd_text"] = ""
            n += 1
    return n


def held_descriptions(rows: list[dict]) -> list[dict]:
    """Rows whose text exists only here. These are what the Drive step must upload."""
    return [r for r in rows if not r.get("drive_synced") and (r.get("jd_text") or "").strip()]


def prune_jd_text(rows: list[dict], budget: int = JD_HOLD_BUDGET_BYTES) -> dict:
    """Keep the hold inside the project's 2MB budget. Drops refetchable text first:
    an ATS description comes back free with tomorrow's feed, a LinkedIn one does not."""
    held = held_descriptions(rows)
    total = sum(len(r["jd_text"]) for r in held)
    dropped = {"refetchable": 0, "unrecoverable": 0}
    if total <= budget:
        return dict(dropped, held=len(held), bytes=total)
    # oldest-first within each class, refetchable class first
    order = sorted(held, key=lambda r: (r.get("ats") not in REFETCHABLE,
                                        r.get("first_seen") or ""))
    for r in order:
        if total <= budget:
            break
        total -= len(r["jd_text"])
        dropped["refetchable" if r.get("ats") in REFETCHABLE else "unrecoverable"] += 1
        r["jd_text"] = ""
    return dict(dropped, held=len(held_descriptions(rows)), bytes=total)


# ------------------------------------------------------------------ projection

SHEET_COLUMNS = ["company", "title", "location", "url", "jd", "score", "notes"]


def todays_rows(rows: list[dict], today: str) -> list[dict]:
    """The rows a sheet dated `today` may contain: the ones first seen today.

    `last_seen` is deliberately NOT the test. A posting published a week ago and first
    caught yesterday is still on the board tonight, so it is re-seen every run - keying
    on that would put it in every sheet forever, which is the repetition this design
    exists to remove. First-seen-today is the only reading under which the sheet's name
    describes its contents.
    """
    return [r for r in rows if (r.get("first_seen") or "") == today]


def sheet_csv(rows: list[dict], today: str) -> str:
    """One day's catch, rebuilt from the ledger.

    Two columns the old sheet had are deliberately absent. `status` was the candidate's; they
    track applications outside this system now, so a column nothing reads would only
    invite work that goes nowhere. `first_seen` is the sheet's own title repeated on
    every row.

    `today` is required, not defaulted: a sheet built for the wrong date is the one
    error here that produces a plausible-looking file, and this project's bugs have all
    been well-formed wrong data rather than crashes.
    """
    import csv
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=SHEET_COLUMNS, extrasaction="ignore",
                       lineterminator="\n")
    w.writeheader()
    for r in sorted(todays_rows(rows, today),
                    key=lambda x: (x.get("score") is None, -(x.get("score") or 0),
                                   x.get("company") or "")):
        w.writerow({
            "company": r.get("company", ""),
            "title": r.get("title", ""), "location": r.get("location", ""),
            "url": r.get("url", ""), "jd": r.get("jd", ""),
            "score": "" if r.get("score") is None else r["score"],
            "notes": r.get("notes", ""),
        })
    return buf.getvalue()


# --------------------------------------------------------------------- digest

def verified_empty_map(sources_cfg: dict) -> dict:
    """{company: date} from sources.json, for watchdogs(). A board confirmed empty
    against the employer's own careers page is not a fault worth repeating nightly."""
    return {s["company"]: s["verified_empty"]
            for s in (sources_cfg.get("sources") or []) if s.get("verified_empty")}


def render_digest(new_rows: list[dict], run: dict, problems: list[dict],
                  watchdogs: list[str], below: list[dict] = (),
                  unscored: list[dict] = ()) -> str:
    """`new_rows` are the postings that cleared the threshold. `below` and `unscored`
    are counted, not listed: section 11 says log them, do not report them. Counting
    them anyway is what keeps a silent scoring failure visible - 40 unscored postings
    and an empty digest must not look the same as a genuinely quiet night."""
    d = run.get("date", "")
    lines = [f"# Job digest — {d}", ""]
    if not new_rows:
        # Never let an empty digest imply the market is quiet. PIPELINE_DESIGN section 7.
        lines += ["**Nothing cleared the bar today.** This is not evidence that nobody "
                  "is hiring — most of the candidate's best-fit employers publish nowhere a feed "
                  "can reach, and the threshold is deliberately strict.", ""]
    else:
        lines += [f"**{len(new_rows)} worth a look.**", ""]
        for r in sorted(new_rows, key=lambda x: -(x.get("score") or 0)):
            score = "unscored" if r.get("score") is None else f"{r['score']}/10"
            flags = "".join(f" `{f}`" for f in (r.get("flags") or []))
            lines.append(f"### {r.get('title')} — {r.get('company')}")
            lines.append(f"{r.get('location') or 'location not stated'} · **{score}**{flags}")
            if r.get("notes"):
                lines.append("")
                lines.append(r["notes"])
            lines.append("")
            lines.append(f"{r.get('url')}")
            lines.append("")
    tail = []
    if below:
        tail.append(f"{len(below)} scored below the bar")
    if unscored:
        tail.append(f"**{len(unscored)} could not be scored** and will be offered again "
                    f"tomorrow")
    if tail:
        lines += ["---", "", " · ".join(tail), ""]
    lines += ["---", "",
              f"Run: {run.get('fetched', 0)} fetched · {run.get('hard_rejected', 0)} "
              f"filtered · {run.get('survivors', 0)} survivors · "
              f"Drive {run.get('drive', 'unknown')}", ""]
    if watchdogs:
        lines += ["**Watchdogs**", ""] + [f"- {w}" for w in watchdogs] + [""]
    if problems:
        lines += ["**Source problems**", ""]
        lines += [f"- {p.get('company', '?')}: {p.get('error', '')}" for p in problems]
        lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ watchdogs

def watchdogs(runs: list[dict], today: str, verified_empty: dict = None) -> list[str]:
    """A job that does not run also does not complain. These are the two failures that
    are otherwise invisible."""
    out = []
    if runs:
        last = max(r.get("date", "") for r in runs)
        try:
            gap = (dt.date.fromisoformat(today) - dt.date.fromisoformat(last)).days
            if gap >= 2:
                out.append(f"No run for {gap} days (last {last}) — the schedule itself "
                           f"may be broken.")
        except ValueError:
            pass
    recent = sorted(runs, key=lambda r: r.get("date", ""))[-5:]
    if len(recent) >= 5:
        empties = [set(r.get("sources_empty") or []) for r in recent]
        for company in sorted(set.intersection(*empties)) if empties else []:
            # A board confirmed empty against the employer's own careers page is not a
            # fault, and repeating it nightly is how people learn to ignore watchdogs.
            # Stay quiet for 30 days, then ask again - the slug can still rot later.
            seen = (verified_empty or {}).get(company)
            if seen:
                try:
                    age = (dt.date.fromisoformat(today) - dt.date.fromisoformat(seen)).days
                except ValueError:
                    age = 999
                if age <= 30:
                    continue
                out.append(f"{company}: still 0 postings, and the last check was {seen} "
                           f"({age} days ago) — worth re-verifying the slug.")
                continue
            out.append(f"{company}: 0 postings for 5 consecutive runs — likely a broken "
                       f"slug or a moved board, not a quiet employer.")
    return out


# ----------------------------------------------------------------- compaction

def compact(rows: list[dict], today: str) -> tuple:
    """Bound the project's 2 MB budget. Only ever drops what is safely archived."""
    t = dt.date.fromisoformat(today)
    dropped = trimmed = 0
    keep = []
    for r in rows:
        try:
            age = (t - dt.date.fromisoformat(r.get("first_seen") or today)).days
        except ValueError:
            age = 0
        if age > NOTES_TTL_DAYS and r.get("notes"):
            r["notes"] = ""
            trimmed += 1
        if age > SHARD_TTL_DAYS and r.get("drive_synced"):
            dropped += 1
            continue
        keep.append(r)
    return keep, {"dropped": dropped, "notes_trimmed": trimmed}


# ---------------------------------------------------------------------- cli

def main() -> int:
    ap = argparse.ArgumentParser(description="merge a run into the ledger")
    ap.add_argument("--postings", default="out/postings.json")
    ap.add_argument("--ledger", help="existing shard(s), concatenated jsonl")
    ap.add_argument("--outdir", default="out")
    ap.add_argument("--date", default=dt.date.today().isoformat())
    a = ap.parse_args()

    postings = json.load(open(a.postings, encoding="utf-8"))
    survivors = [p for p in postings if not (p.get("rejected_by") or "").strip()]
    existing = read_jsonl(open(a.ledger, encoding="utf-8").read() if a.ledger else "")
    rows, stats = merge(existing, survivors, a.date)
    rows, comp = compact(rows, a.date)
    hold = prune_jd_text(rows)

    os.makedirs(a.outdir, exist_ok=True)
    open(os.path.join(a.outdir, "ledger.jsonl"), "w", encoding="utf-8").write(
        write_jsonl(rows))
    open(os.path.join(a.outdir, "sheet.csv"), "w", encoding="utf-8").write(sheet_csv(rows, a.date))
    open(os.path.join(a.outdir, "pending.json"), "w", encoding="utf-8").write(
        json.dumps(pending(rows), ensure_ascii=False, indent=1))
    # What the Drive step must upload. Each carries its own text, so an archive run can
    # succeed days after the posting was fetched - or after the posting has vanished.
    open(os.path.join(a.outdir, "held.json"), "w", encoding="utf-8").write(
        json.dumps(held_descriptions(rows), ensure_ascii=False, indent=1))
    print(f"{stats['added']} new, {stats['refreshed']} refreshed, {stats['total']} total; "
          f"{len(pending(rows))} awaiting a digest; compaction {comp}; "
          f"held descriptions {hold}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
