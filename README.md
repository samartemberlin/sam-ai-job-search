# pipeline/ — deterministic sourcing and hard filtering

The cheap, non-Claude half of the nightly job search. Fetches public ATS feeds, normalizes
them to one schema, deduplicates, and applies the hard exclusions from
`Job_Search_Profiles.md` §2. Claude never runs in here; everything is a literal field check
or a regex, and every rejection is logged with the text that caused it.

Not in this directory: scoring, ranking, CV or letter drafting. Those are Claude's stages and
consume what this one emits.

## Design constraints

- **Zero third-party dependencies.** The nightly container cannot reach package registries and
  is rebuilt from scratch every run, so there is nothing to install. Python 3.9+ stdlib only.
- **No personal data, ever.** This repo is public. the candidate's profile, CV, rubric and the
  application tracker live in Google Drive. Nothing here may identify them.
- **Fail loud.** A watchlist entry that returns nothing is reported, never passed over — a
  board that silently yields zero looks identical to one whose slug is wrong.

## Run

```sh
python3 pipeline.py --known out/known.txt   # fetch every source, filter, write out/
python3 pipeline.py --source Ecosia         # a single company
python3 pipeline.py --offline               # re-filter out/raw.json without refetching
python3 test_rules.py                       # rule regression tests

# stage 7 - merge this run into the current month's shard and refresh the dedupe index:
python3 ledger.py --postings out/postings.json --ledger <this-month's-shard-local-copy> \
    --known-index <known-urls.jsonl-local-copy> --date <today>
# then, after ledger.py has written out/ledger.jsonl:
APPS_SCRIPT_URL=... python3 sheet_sync.py --ledger out/ledger.jsonl --date <today>
```

`--known`/`--known-index` want the CURRENT LOCAL COPY of `claude/state/known-urls.jsonl`
(project doc) — the whole reason this file exists is that the caller (the nightly run,
orchestrated outside this repo per RUNBOOK.md section 4) no longer needs to fetch and
re-read the last 2-3 months of full shards just to build a url list. See
`ledger.py`'s "known-url index" section and TOKEN_BUDGET.md section 3.

Outputs, all gitignored:

| file | contents |
|---|---|
| `out/raw.json` | everything fetched, pre-filter — replay input for `--offline` |
| `out/postings.json` | normalized and filtered; each row carries `rejected_by` |
| `out/jd/*.md` | one archived job description per posting |
| `out/ledger.jsonl` | the merged **current-month shard only**; `sheet_sync.py` rewrites it in place |
| `out/held.json` | descriptions not yet archived — what `sheet_sync.py` uploads |
| `out/known-urls.jsonl` | the refreshed cumulative dedupe index (`{url, d}`, all retained months) — written back to `claude/state/known-urls.jsonl` |
| `out/known.txt` | the same index as one url per line — what `pipeline.py --known` reads next run |

`out/jd/` exists because postings vanish. By the time the candidate looks at a shortlist a week later,
some URLs are dead — the archived text is what survives. The descriptions arrive inside the
same feed response as the listings, so this costs no extra requests.

## Files

- **`sources.json`** — which boards to fetch. Each entry is `{company, ats, slug, tier}`.
  Also records organisations with **no** public feed, so they stay visible as gaps rather
  than being quietly forgotten.
- **`rules.json`** — the hard exclusions, as data. Ordered: exact structured-field checks
  first, prose regexes last.
- **`pipeline.py`** — adapters, normalization, dedup, rule engine.
- **`ledger.py`** — the merge. No network, no credentials.
- **`sheet_sync.py`** — the only file here that talks to Google: one HTTPS POST to an
  Apps Script endpoint that writes a monthly spreadsheet (a tab per day) and a
  `YYYY-MM-JD/` Drive folder of archived descriptions. The endpoint URL is the
  credential, so it is read from `APPS_SCRIPT_URL` and never appears in this repo.
- **`test_rules.py`** — regression tests for every rule.

## Adding a source

Find the ATS and its slug, add a line to `sources.json`, run
`python3 pipeline.py --source "<company>"`. If it returns zero postings, the slug is probably
wrong — check the careers page rather than assuming a hiring freeze. Supported: Greenhouse,
Ashby, Personio, Lever, SmartRecruiters, Recruitee.

## Why the rules are deliberately too permissive

A posting rejected here is never scored, so **a wrong rejection is invisible** — nobody ever
learns the job was good. A posting wrongly passed through costs one Claude call. The errors
are not symmetric, so the rules only reject on unambiguous signals.

Three exclusions from §2 are deliberately *absent* because they need judgement, not pattern
matching: required degree field, technical-skill depth, and German-only-and-German-facing.
They fall through to scoring. `rules.json` records each one and why.

Structured fields are strict, prose is not. `employment_type == "working_student"` is exact
and safe; the same idea expressed in German prose is not, so the text rules stay narrow. This
split is what catches *Studentische Hilfskraft* postings reliably.

**Every posting is kept**, including rejected ones, with the rule id and the literal matched
substring in `rejected_by`:

```
HARD_LANG_DE: "Fluent German"  …ecosystem - Fluent German and English - Willingness to travel…
```

The evidence half is the point. `HARD_LANG_DE` alone would mean reopening the posting to judge
whether the rule was right; with the matched text in the cell you can scan fifty rejections and
spot an overreaching pattern in about a minute.

## Known gap: reachable is not the same as complete

A public ATS feed is not necessarily an organisation's whole job list. adelphi is
Berlin-headquartered, but its Personio board carries only Munich student roles — the Berlin
postings are published somewhere else. WWF Deutschland's board shows three jobs, which is
plainly not all of WWF Germany's hiring. Treat coverage per source as an open question, and
do not read "0 new jobs from X" as "X is not hiring".
