#!/usr/bin/env python3
"""
Stage 7, network half: push today's sheet rows and any held JD text to the
Apps Script webhook, then flip drive_synced on whatever it confirmed.

ledger.py is deliberately network-free (see its own docstring: "no network,
no MCP, no credentials"). Under the old Drive-connector design the actual
write had to be an MCP tool call, so "the session performs the transport"
(also ledger.py's words) was a real constraint - pipeline code cannot call
MCP tools.

That constraint is gone now. Calling the Apps Script webhook is a plain
HTTPS POST, exactly like pipeline.py's own calls to Greenhouse or Ashby -
no MCP tool, no Google credential in this container. So this step no longer
needs the session at all; it can run as ordinary pipeline code, right after
ledger.py, with no Claude involvement until the updated ledger needs to be
project_write-ted back.

Usage (after ledger.py has written out/ledger.jsonl for tonight):
    python3 sheet_sync.py --ledger out/ledger.jsonl --date 2026-09-04

Rewrites --ledger in place with drive_synced flipped on whatever the
webhook confirmed, and prints one line of JSON: {"ok": bool,
"spreadsheetUrl": str|None, "rows_written": int, "jd_files_written": int,
"synced": int}.

Never raises. A failed sync leaves the ledger untouched and reports
ok: false - the run continues, and tonight's unsynced rows are picked up
by tomorrow's call the same way drive_synced backlog always worked.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

from ledger import (read_jsonl, write_jsonl, todays_rows, held_descriptions,
                     mark_drive_synced)

# The Apps Script deployment URL IS the credential (see project notes — no
# separate token, access control is the URL's own ~300-bit entropy). That
# means it must never be a literal in this file: this repo is public, and
# committing the URL here would be the same mistake as committing a
# password. It's supplied at runtime only, via APPS_SCRIPT_URL — set by
# whatever invokes this script (the scheduled task's own prompt/instructions,
# not the repo, not any file that gets committed).
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL")
TIMEOUT = 30


def build_payload(rows: list[dict], date: str) -> tuple[dict, list[str]]:
    """Returns (payload, held_urls). held_urls is every URL whose JD text is
    being offered this call - if the webhook confirms success, these are
    exactly the URLs that should flip drive_synced."""
    held = {r["url"]: r for r in held_descriptions(rows) if r.get("url")}

    jd_files = [
        {"name": r["jd"], "content": r["jd_text"]}
        for r in held.values() if r.get("jd") and r.get("jd_text")
    ]

    out_rows = []
    for r in todays_rows(rows, date):
        out_rows.append({
            "url": r.get("url", ""),
            "company": r.get("company", ""),
            "title": r.get("title", ""),
            "location": r.get("location", ""),
            "score": r.get("score"),
            "notes": r.get("notes", ""),
            # Set whenever this row has an intended JD filename at all, not
            # only when it's part of this call's jd_files - once a file is
            # in Drive from a prior sync, the filename is stable and the
            # link should keep pointing at it.
            "jd_drive_name": r.get("jd") or "",
        })

    payload = {"date": date, "rows": out_rows, "jd_files": jd_files}
    return payload, list(held.keys())


def sync(rows: list[dict], date: str) -> dict:
    payload, held_urls = build_payload(rows, date)
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        APPS_SCRIPT_URL, data=data,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        # The 302 -> googleusercontent.com/macros/echo hop must be followed
        # as a GET, which is urllib's default redirect behavior. Do not
        # force POST-on-redirect - that gets a 405 from the echo endpoint
        # (confirmed against the live deployment, 3 Sep run notes).
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError):
        return {"ok": False, "synced": 0}

    if not isinstance(result, dict) or not result.get("ok"):
        return {"ok": False, "synced": 0}

    n = mark_drive_synced(rows, held_urls)
    result["synced"] = n
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True,
                     help="out/ledger.jsonl from tonight's run; rewritten in place on success")
    ap.add_argument("--date", required=True)
    a = ap.parse_args()

    if not APPS_SCRIPT_URL:
        # Fail loud and local. This message never leaves this machine's run
        # log, so it's fine for it to be specific - unlike the webhook's own
        # error responses, which stay deliberately generic for any caller.
        print(json.dumps({"ok": False,
                          "error": "APPS_SCRIPT_URL not set in the environment"}))
        return 1

    with open(a.ledger, encoding="utf-8") as f:
        rows = read_jsonl(f.read())

    result = sync(rows, a.date)

    if result.get("ok"):
        with open(a.ledger, "w", encoding="utf-8") as f:
            f.write(write_jsonl(rows))

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
