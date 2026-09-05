#!/usr/bin/env python3
"""
Ledger tests. The important half asserts what a re-run must NOT do: lose a notified
flag, move first_seen, or drop a row that has not reached Drive.

The sheet half asserts the 31 Aug contract: a daily sheet holds the rows first seen on
its own date and nothing else, and carries neither a `status` column nor a `first_seen`
one. The must-NOT cases are the point - a posting caught yesterday must not appear in
today's sheet even though tonight's run saw it again on the board.
"""
import sys

import ledger as L

T = "2026-09-15"
fails = []


def check(label, cond):
    if not cond:
        fails.append(label)


def post(**kw):
    d = dict(url="u1", company="atmosfair", title="Projektmanager", location="Berlin",
             jd_file="f.md", tier="A", ats="personio", posted="2026-09-01",
             first_seen="2026-09-01", rejected_by="")
    d.update(kw)
    return d


# --- merge -------------------------------------------------------------------
rows, st = L.merge([], [post()], T)
check("new row added", st["added"] == 1 and rows[0]["url"] == "u1")
check("new row is unnotified", rows[0]["notified"] is False)
check("new row is unsynced", rows[0]["drive_synced"] is False)
check("new row has no score", rows[0]["score"] is None)

rows[0]["notified"] = True
rows[0]["score"] = 8
rows[0]["notes"] = "strong fit"
again, st2 = L.merge(rows, [post(title="Renamed")], T)
check("re-run keeps notified", again[0]["notified"] is True)
check("re-run keeps score", again[0]["score"] == 8)
check("re-run keeps notes", again[0]["notes"] == "strong fit")
check("merge does not create a status field", "status_mirror" not in again[0])
check("re-run refreshes machine fields", again[0]["title"] == "Renamed")
check("re-run does not move first_seen", again[0]["first_seen"] == "2026-09-01")
check("re-run updates last_seen", again[0]["last_seen"] == T)
check("re-run does not duplicate", st2["total"] == 1)

check("row without url skipped", L.merge([], [post(url=None)], T)[1]["total"] == 0)
check("existing row absent from run survives",
      L.merge(again, [post(url="u2")], T)[1]["total"] == 2)

# --- notified: the crash-before-digest bug -----------------------------------
a, _ = L.merge([], [post(url="a"), post(url="b")], T)
check("both pending initially", len(L.pending(a)) == 2)
L.mark_notified(a, ["a"])
check("only unnotified stay pending", [r["url"] for r in L.pending(a)] == ["b"])
# A later run must not silently absorb the un-notified one.
a2, _ = L.merge(a, [post(url="a"), post(url="b")], "2026-09-16")
check("un-notified survives the next run", [r["url"] for r in L.pending(a2)] == ["b"])
check("mark_notified is idempotent", L.mark_notified(a2, ["a"]) == 0)

# --- the status mirror is gone --------------------------------------------------
# It was a read-back of the candidate's own column out of the Sheet, parsed from the connector's
# lossy markdown rendering. The candidate tracks applications outside this system, so the
# column, the parser and the field are all removed. Assert their absence: a leftover
# reader would silently re-introduce a field nothing writes.
for gone in ("parse_sheet_status", "apply_status_mirror", "sheet_csv"):
    check(f"{gone} is removed", not hasattr(L, gone))
# sheet_csv went with the Drive connector: the endpoint takes JSON rows, shaped by
# sheet_sync.build_chunks. Two row-shapers with one caller is how they drift apart.

# --- sheet projection: one day's catch ------------------------------------------
YDAY = "2026-09-14"
todays, _ = L.merge([], [post(url="https://x/new", first_seen=T)], T)
mixed = todays + [dict(todays[0], url="https://x/old", first_seen=YDAY,
                       last_seen=T, company="Seen Yesterday")]

check("todays_rows keys on first_seen, not last_seen",
      [r["url"] for r in L.todays_rows(mixed, T)] == ["https://x/new"])
check("todays_rows for yesterday holds yesterday's row",
      [r["url"] for r in L.todays_rows(mixed, YDAY)] == ["https://x/old"])
check("a day with no new postings yields nothing", L.todays_rows(mixed, "2026-09-13") == [])

rows3 = todays

# --- compaction --------------------------------------------------------------
old_synced = dict(rows3[0], first_seen="2026-01-01", drive_synced=True, notes="x")
old_unsynced = dict(rows3[0], url="u9", first_seen="2026-01-01", drive_synced=False)
kept, comp = L.compact([old_synced, old_unsynced], T)
urls = {r["url"] for r in kept}
check("compaction drops old archived rows", rows3[0]["url"] not in urls)
check("compaction keeps rows not yet in Drive", "u9" in urls)
# `status` used to pin a row against compaction, so a job the candidate had applied to stayed in
# the ledger forever. Nothing pins one now: after SHARD_TTL_DAYS an archived row leaves,
# and if the posting is somehow still live it can be seen as new again. 90 days is well
# past the life of a real vacancy, which is why this is accepted rather than fixed.
check("nothing survives compaction on the strength of a status field",
      not L.compact([dict(old_synced, url="u8", status_mirror="applied")], T)[0])
recent, comp2 = L.compact([dict(rows3[0], first_seen=T, drive_synced=True)], T)
check("compaction keeps recent rows", len(recent) == 1)
check("compaction trims old notes", comp["notes_trimmed"] >= 1)

# --- held descriptions: the 30 Aug data loss ---------------------------------
# 29 LinkedIn descriptions were lost because the archive step never ran and the text
# lived only in the container. A row now carries its text until Drive confirms.
h, _ = L.merge([], [post(url="li", ats="linkedin", jd="full linkedin body")], T)
check("new row holds its description", h[0]["jd_text"] == "full linkedin body")
check("held rows are listed for the archive step", [r["url"] for r in L.held_descriptions(h)] == ["li"])
L.mark_drive_synced(h, ["li"])
check("archiving releases the held text", h[0]["jd_text"] == "" and h[0]["drive_synced"])
check("archived rows are no longer held", L.held_descriptions(h) == [])
# A re-run must not resurrect the text once Drive has it.
h2, _ = L.merge(h, [post(url="li", ats="linkedin", jd="full linkedin body")], T)
check("synced row stays released", h2[0]["jd_text"] == "")
# An unsynced row refreshes its text on a later run.
u, _ = L.merge([], [post(url="u", ats="personio", jd="v1")], T)
u2, _ = L.merge(u, [post(url="u", ats="personio", jd="v2")], T)
check("unsynced row refreshes held text", u2[0]["jd_text"] == "v2")
check("long text is truncated", len(L.merge([], [post(url="x", jd="z" * 99999)], T)[0][0]["jd_text"])
      == L.JD_HOLD_TRUNCATE)

# Over budget: refetchable ATS text goes first, LinkedIn text is kept.
big = "y" * 5000
rows_b = L.merge([], [post(url=f"a{i}", ats="personio", jd=big) for i in range(10)] +
                     [post(url=f"l{i}", ats="linkedin", jd=big) for i in range(10)], T)[0]
stats = L.prune_jd_text(rows_b, budget=20000)
kept = {r["url"] for r in L.held_descriptions(rows_b)}
check("pruning respects the budget", stats["bytes"] <= 20000)
check("pruning drops refetchable text first", stats["refetchable"] > 0)
check("pruning keeps linkedin text", all(u.startswith("l") for u in kept))
check("pruning is a no-op under budget", L.prune_jd_text(h, budget=999999)["refetchable"] == 0)

# --- watchdog: a verified-empty board must not cry wolf ----------------------
five_e = [{"date": f"2026-09-{d:02d}", "sources_empty": ["Ecosia"]} for d in range(10, 15)]
check("unverified empty board still warns",
      any("Ecosia" in w for w in L.watchdogs(five_e, "2026-09-15")))
check("recently verified board stays quiet",
      L.watchdogs(five_e, "2026-09-15", {"Ecosia": "2026-09-01"}) == [])
check("stale verification asks again",
      any("re-verifying" in w for w in L.watchdogs(five_e, "2026-09-15", {"Ecosia": "2026-06-01"})))
check("verification of another source does not silence this one",
      any("Ecosia" in w for w in L.watchdogs(five_e, "2026-09-15", {"adelphi": "2026-09-01"})))

# --- jsonl -------------------------------------------------------------------
check("jsonl round-trips", L.read_jsonl(L.write_jsonl(rows3))[0]["url"] == rows3[0]["url"])
check("corrupt line costs one row only",
      len(L.read_jsonl('{"url":"a"}\nNOT JSON\n{"url":"b"}\n')) == 2)
check("blank text is empty", L.read_jsonl("") == [])
check("unicode survives", L.read_jsonl(L.write_jsonl(
    [{"t": "Öfen „quoted“"}]))[0]["t"] == "Öfen „quoted“")

# --- shard selection ---------------------------------------------------------
check("shards wrap the year", L.shards_to_read("2026-01-15") ==
      ["claude/state/postings-2026-01.jsonl", "claude/state/postings-2025-12.jsonl",
       "claude/state/postings-2025-11.jsonl"])
check("shard path from date", L.shard_path("2026-09-15") ==
      "claude/state/postings-2026-09.jsonl")

# --- known-url index (4 Sep: replaces reading 3 months of full shards nightly) ---
idx0 = L.known_index_from_rows(rows3)
check("index has no notes/score/jd fields", set(idx0[0]) == {"url", "d"})
check("index url matches the source row", idx0[0]["url"] == rows3[0]["url"])

grown = L.merge_known_index(idx0, [dict(url="new-url", first_seen="2026-09-16")], "2026-09-16")
check("merging adds a genuinely new url", any(r["url"] == "new-url" for r in grown))
check("merge grows by exactly the new urls", len(grown) == len(idx0) + 1)
regrown = L.merge_known_index(grown, [dict(url="new-url", first_seen="2026-09-17")], "2026-09-17")
check("merge does not move an existing entry's date",
      next(r for r in regrown if r["url"] == "new-url")["d"] == "2026-09-16")
check("merge does not duplicate an existing url", len(regrown) == len(grown))

old_entry = [{"url": "stale", "d": "2026-01-01"}, {"url": "fresh", "d": "2026-09-01"}]
pruned = L.prune_known_index(old_entry, "2026-09-15")
check("pruning drops entries past the shard TTL", {r["url"] for r in pruned} == {"fresh"})
check("pruning uses the same TTL as shard compaction",
      L.prune_known_index(old_entry, "2026-09-15", ttl_days=999) == old_entry)

check("index-to-url-list is plain urls, the shape pipeline.py --known reads",
      L.known_index_to_url_list([{"url": "a", "d": "x"}, {"url": "b", "d": "y"}]) == ["a", "b"])
check("a url-less row cannot poison the index",
      L.known_index_from_rows([{"first_seen": "2026-09-01"}]) == [])

rebuilt = L.rebuild_known_index(
    rows3 + [dict(rows3[0], url="stale2", first_seen="2026-01-01")], "2026-09-15")
check("rebuild applies the TTL like the incremental path does",
      "stale2" not in {r["url"] for r in rebuilt})
check("rebuild is a pure function of the shards, not the old index",
      {r["url"] for r in rebuilt} == {r["url"] for r in L.known_index_from_rows(rows3)})

# --- watchdogs ---------------------------------------------------------------
check("no-run watchdog fires", any("schedule" in w for w in L.watchdogs(
    [{"date": "2026-09-10"}], T)))
check("no-run watchdog quiet when fresh", L.watchdogs([{"date": "2026-09-15"}], T) == [])
five = [{"date": f"2026-09-{d:02d}", "sources_empty": ["adelphi"]} for d in range(10, 15)]
check("empty-source watchdog fires",
      any("adelphi" in w for w in L.watchdogs(five, "2026-09-15")))
four = [{"date": f"2026-09-{d:02d}", "sources_empty": ["adelphi"]} for d in range(11, 15)]
check("empty-source watchdog needs 5 runs",
      not any("adelphi" in w for w in L.watchdogs(four, "2026-09-15")))

# --- digest ------------------------------------------------------------------
d = L.render_digest([], {"date": T, "fetched": 200, "hard_rejected": 190,
                         "survivors": 10, "drive": "ok"}, [], [])
check("empty digest refuses to imply a quiet market", "not evidence" in d)
# Counted, not listed - and an unscorable backlog must never look like a quiet night.
d3 = L.render_digest([], {"date": T}, [], [], below=[{}] * 4, unscored=[{}] * 9)
check("below-threshold rows are counted", "4 scored below the bar" in d3)
check("unscored rows are called out", "9 could not be scored" in d3)
check("below-threshold rows are not listed", "###" not in d3)
d4 = L.render_digest([dict(rows3[0], score=8, flags=["3+ years"])], {"date": T}, [], [])
check("scored rows show the score", "8/10" in d4)
check("scored rows show flags", "`3+ years`" in d4)
d2 = L.render_digest(rows3, {"date": T}, [{"company": "Ecosia", "error": "0 postings"}],
                     ["adelphi: 0 postings for 5 consecutive runs"])
check("digest lists postings", "atmosfair" in d2)
check("digest surfaces problems", "Ecosia" in d2)
check("digest surfaces watchdogs", "adelphi" in d2)

# --- the jd pointer must not follow the run date once the file is in Drive ------
# Found by the 31 Aug run: `jd` is a machine field, so re-seeing an already-archived
# posting rewrote its archive filename to that night's date. The file in Drive keeps the
# OLD name - the connector cannot rewrite content - so 13 sheet rows pointed at files
# nobody had written. Invisible until the archive is a day older than the ledger.
synced = [dict(url="u9", first_seen="2026-09-01", last_seen="2026-09-01",
               company="atmosfair", title="Projektmanager", location="Berlin",
               tier="A", ats="personio", posted="2026-09-01",
               jd="2026-09-01_atmosfair_projektmanager.md", score=None, notes="",
               notified=True, drive_synced=True, status_mirror="new")]
again = post(url="u9", jd_file="2026-09-15_atmosfair_projektmanager.md", jd="fresh text")
rows9, _ = L.merge(synced, [again], T)
check("archived jd name never moves",
      rows9[0]["jd"] == "2026-09-01_atmosfair_projektmanager.md")
check("other machine fields still refresh on a synced row",
      rows9[0]["last_seen"] == T)
check("a synced row does not re-hold jd_text", not rows9[0].get("jd_text"))

# The unsynced case must keep refreshing: nothing is in Drive yet, so tonight's name is
# the name tonight's upload will use.
unsynced = [dict(synced[0], url="u10", drive_synced=False)]
rows10, _ = L.merge(unsynced, [post(url="u10", jd_file="2026-09-15_new.md",
                                    jd="fresh text")], T)
check("unsynced jd name still refreshes", rows10[0]["jd"] == "2026-09-15_new.md")
check("unsynced row still holds jd_text", rows10[0]["jd_text"] == "fresh text")

TOTAL = 73  # +12 for the known-url index (4 Sep, TOKEN_BUDGET.md section 3)
for f in fails:
    print("FAIL", f)
print(f"\n{TOTAL - len(fails)}/{TOTAL} passed")
sys.exit(1 if fails else 0)
