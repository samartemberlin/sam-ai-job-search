#!/usr/bin/env python3
"""
Stage 7, network half: push today's rows and any held JD text to the Apps
Script webhook, then flip drive_synced on whatever it confirmed.

Replaces the Drive connector. That write had to be an MCP tool call, which
pipeline code cannot make, so the session had to perform the transport. This
is a plain HTTPS POST like pipeline.py's own - no MCP tool, no credential.

Usage (after ledger.py has written out/ledger.jsonl for tonight):
    APPS_SCRIPT_URL=... python3 sheet_sync.py --ledger out/ledger.jsonl --date 2026-09-04

Rewrites --ledger in place with drive_synced flipped on whatever the webhook
confirmed, and prints one line of JSON.

Never raises. A failed sync leaves the ledger untouched and reports ok: false -
the run continues, and tonight's unsynced rows are picked up by tomorrow's call
the same way drive_synced backlog always worked. A PARTIAL sync still writes
the ledger: whatever was confirmed is confirmed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

from ledger import (read_jsonl, write_jsonl, todays_rows, held_descriptions,
                     mark_drive_synced)

# The deployment URL IS the credential (no separate token - access control is
# the URL's own ~300-bit entropy). This repo is public, so it must never be a
# literal here. Supplied at runtime by whatever invokes this script.
APPS_SCRIPT_URL = os.environ.get("APPS_SCRIPT_URL")
TIMEOUT = 30

# Mirror of the endpoint's own caps. It rejects an over-cap payload WHOLE - one
# oversized batch loses the night - so the splitting happens here.
MAX_ROWS = 100
MAX_JD_FILES = 20
MAX_JD_CONTENT = 200_000
MAX_TOTAL_JD = 900_000    # endpoint allows 1_000_000; leave headroom
MIN_INTERVAL = 21         # endpoint allows 3 requests/minute
RETRYABLE = {"ratelimit", "busy", "internal"}


def _valid_url(r: dict) -> bool:
    """url is REQUIRED and must be http(s) since the hardening pass, and a bad
    one now fails the WHOLE batch rather than that row. Filter here."""
    u = (r.get("url") or "").strip().lower()
    return u.startswith("http://") or u.startswith("https://")


def build_chunks(rows: list[dict], date: str) -> list[dict]:
    """Requests the endpoint will accept. Each is {payload, urls}: the urls are
    the held rows that request carries, i.e. what to flip if it succeeds. Rows
    ride the first request only; the endpoint dedupes on url anyway."""
    out_rows = [{
        "url": r["url"],
        "company": r.get("company", ""),
        "title": r.get("title", ""),
        "location": r.get("location", ""),
        "score": r.get("score"),
        "notes": r.get("notes", ""),
        # Set whenever the row has an intended filename at all, not only when
        # it is in this call's jd_files - once a file is in Drive from a prior
        # sync the name is stable and the link must keep pointing at it.
        "jd_drive_name": r.get("jd") or "",
    } for r in todays_rows(rows, date) if _valid_url(r)][:MAX_ROWS]

    held = [(r["url"], {"name": r["jd"], "content": r["jd_text"][:MAX_JD_CONTENT]})
            for r in held_descriptions(rows)
            if _valid_url(r) and (r.get("jd") or "").strip() and (r.get("jd_text") or "").strip()]

    chunks, i = [], 0
    while i < len(held) or not chunks:
        batch, size = [], 0
        while i < len(held) and len(batch) < MAX_JD_FILES:
            u, f = held[i]
            if batch and size + len(f["content"]) > MAX_TOTAL_JD:
                break
            batch.append((u, f))
            size += len(f["content"])
            i += 1
        chunks.append({
            "payload": {"date": date,
                        "rows": out_rows if not chunks else [],
                        "jd_files": [f for _, f in batch]},
            "urls": [u for u, _ in batch]})
    return chunks


def _post(payload: dict) -> dict:
    """One request, retried on the codes the endpoint marks retryable.

    The 302 -> googleusercontent.com hop must be followed as a GET (urllib's
    default; forcing POST-on-redirect 405s). The endpoint always returns HTTP
    200, so the body is the only signal."""
    data = json.dumps(payload).encode("utf-8")
    result = {"ok": False, "code": "internal"}
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                APPS_SCRIPT_URL, data=data,
                headers={"Content-Type": "application/json"}, method="POST")
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            if not isinstance(result, dict):
                result = {"ok": False, "code": "internal"}
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError,
                ValueError, OSError):
            result = {"ok": False, "code": "internal"}
        if result.get("ok") or result.get("code") not in RETRYABLE:
            return result   # "invalid" is permanent - retrying cannot help
        if attempt < 2:
            time.sleep(MIN_INTERVAL * (attempt + 1))
    return result


def sync(rows: list[dict], date: str) -> dict:
    chunks = build_chunks(rows, date)
    if not chunks[0]["payload"]["rows"] and not any(c["payload"]["jd_files"] for c in chunks):
        return {"ok": True, "spreadsheetUrl": None, "rows_written": 0,
                "rows_skipped": 0, "jd_files_written": 0, "synced": 0}

    totals = {"rows_written": 0, "rows_skipped": 0, "jd_files_written": 0}
    confirmed, url, code = [], None, None
    for n, c in enumerate(chunks):
        if n:
            time.sleep(MIN_INTERVAL)
        r = _post(c["payload"])
        if not r.get("ok"):
            code = r.get("code") or "internal"
            break
        for k in totals:
            totals[k] += int(r.get(k) or 0)
        url = r.get("spreadsheetUrl") or url
        confirmed += c["urls"]

    out = {"ok": code is None, "spreadsheetUrl": url, **totals,
           "synced": mark_drive_synced(rows, confirmed)}
    if code:
        out["code"] = code
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True,
                     help="out/ledger.jsonl from tonight's run; rewritten in place")
    ap.add_argument("--date", required=True)
    a = ap.parse_args()

    if not APPS_SCRIPT_URL:
        # Fail loud and local. This never leaves this machine's run log, so it
        # can be specific - unlike the endpoint's own deliberately generic errors.
        print(json.dumps({"ok": False, "error": "APPS_SCRIPT_URL not set"}))
        return 1

    with open(a.ledger, encoding="utf-8") as f:
        rows = read_jsonl(f.read())

    result = sync(rows, a.date)

    # A partial sync still earned its ledger write.
    if result.get("ok") or result.get("synced"):
        with open(a.ledger, "w", encoding="utf-8") as f:
            f.write(write_jsonl(rows))

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
