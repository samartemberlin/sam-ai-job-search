# apps-script/ — the write endpoint

Everything in this directory is uploaded **by hand** to the Apps Script web editor. Apps
Script does not deploy from this repo, and the nightly run never touches these files: it
only POSTs to the deployed URL (`sheet_sync.py`).

| file | goes where in the editor |
|---|---|
| `Code.gs` | the script file `Code.gs` (replace its whole contents) |
| `appscript.json` | the manifest — Project Settings → "Show `appsscript.json` manifest file in editor", then replace its contents |

**`appscript.json` is not optional.** It pins the OAuth scopes and the runtime. Without it
Apps Script infers scopes from the code and can fall back to the legacy runtime, where
`console` is undefined and the endpoint's opaque-failure design breaks (see `logErr()`).

## What the manifest sets

- `runtimeVersion: "V8"` — required, see above.
- `timeZone: "Europe/Berlin"`.
- `webapp.executeAs: "USER_DEPLOYING"` and `access: "ANYONE_ANONYMOUS"` — the endpoint runs
  as the deploying account and is reachable without a Google login. **There is no token:
  the deployment URL is the only credential.** Keep it out of the repo.
- `oauthScopes`: `auth/drive` and `auth/spreadsheets`.

### Why the full `drive` scope

`drive.file` (only files the script created) is not enough for the `DriveApp` calls this code
makes (`createFolder`, `getFolderById`, `moveTo`, `createFile`). With it, every JD upload
returned `code: "internal"` and the log said *"Specified permissions are not sufficient to
call DriveApp.createFolder. Required permissions: …/auth/drive"*. Creating a new month's
spreadsheet would have failed the same way. The wider grant does not widen what the code
does: there is no read, list, share or delete operation in `Code.gs`, and adding one is a
security-relevant change. The least-privilege alternative is the Advanced Drive service under
`drive.file`; that is a rewrite of the Drive calls and has not been done.

## Deploying

First time:

1. Create a **standalone** Apps Script project (not one opened from a spreadsheet's
   Extensions menu — a container-bound script is deleted along with its spreadsheet).
2. Paste `Code.gs` and `appscript.json` as above.
3. Optional: to keep files in an existing Drive folder, add the Script Property
   `root_folder_id` = that folder's ID (Project Settings → Script Properties). Otherwise the
   script creates its own root folder on first write.
4. Run `diagnose` once from the editor and approve the permission prompt. The Execution log
   should print `OK: JD-… in <folder url>`.
5. Deploy → New deployment → Web app. The URL it gives is the endpoint; store it as
   `APPS_SCRIPT_URL` (outside this repo).

After any change to `Code.gs` or `appscript.json`: paste the new contents, then Deploy →
Manage deployments → edit → **New version**. The URL does not change. Changing scopes
additionally needs one run from the editor to approve them first. Archiving a deployment and
creating a new one mints a new URL.

## Storage layout

One root folder containing, per month, a spreadsheet `Jobs-YYYY-MM` (a tab per day,
`YYYY-MM-DD`) and a JD archive folder `JD-YYYY-MM` (one `.md` per posting). Month → spreadsheet
and folder IDs are cached in Script Properties (`ss_YYYY-MM`, `jd_folder_YYYY-MM`,
`root_folder_id`). The script refuses to recreate storage when a cached ID stops resolving —
delete the stale property by hand if the object is really gone.

## Debugging

The endpoint always returns HTTP 200 with `{"ok": false, "code": …}`; `invalid`, `ratelimit`,
`busy` and `internal` are all the caller learns. An execution marked *Completed* in the
Executions list can still be a failure — `doPost` catches errors and returns the JSON. The
real error is the `console.error` line under the execution, or run `diagnose()` from the
editor to see a Drive failure directly. The day ceiling is 30 requests (UTC day), counted in
the `rl_day_YYYY-MM-DD` property; runs from the editor do not count.

To call the endpoint by hand with curl, use `curl -L -d '<json>' -H 'Content-Type:
application/json' <url>` **without** `-X POST`; forcing POST on the redirect returns 405.
