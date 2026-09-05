/**
 * Job Pipeline write endpoint.
 *
 * ── Access model ────────────────────────────────────────────────────────────
 * No shared-secret token. Access control is the deployment URL's own
 * ~300-bit unguessable deployment ID (script.script.google.com/macros/s/<id>/exec).
 * Deliberate choice — Apps Script doPost(e) exposes neither caller IP nor
 * headers (Google Issue Tracker #67764685, won't-fix), and "Anyone with a
 * Google account" would require the unattended caller to hold a live OAuth
 * session. See project notes.
 *
 * Because the URL is the only gate, the code assumes it may leak and is
 * written to bound the damage: hard caps, a per-DAY request ceiling (the
 * per-minute one does not bound Google's per-day quotas), a script lock, and
 * no read / list / share / delete operation anywhere.
 *
 * ── REQUIRED manifest change ────────────────────────────────────────────────
 * Apps Script infers `auth/drive` (FULL read/write/delete over the owner's
 * entire Drive) from any DriveApp call. Pin the scopes explicitly in
 * appsscript.json so the grant matches what this code actually does:
 *
 *   {
 *     "timeZone": "Europe/Berlin",
 *     "runtimeVersion": "V8",
 *     "oauthScopes": [
 *       "https://www.googleapis.com/auth/drive.file",
 *       "https://www.googleapis.com/auth/spreadsheets"
 *     ]
 *   }
 *
 * `drive.file` = only files/folders this script created. Nothing here reads a
 * file it did not create (getRootFolder() was removed for exactly that reason).
 * MIGRATION: re-authorizing narrows the grant; objects created under the old
 * broad scope may become unreachable. Switch at a month boundary, or clear the
 * cached IDs in Script Properties (ss_YYYY-MM, jd_folder_YYYY-MM) afterwards.
 * "runtimeVersion": "V8" is not optional — see logErr().
 *
 * ── Responses ───────────────────────────────────────────────────────────────
 * Success: {"ok": true, "spreadsheetUrl", "tab", "rows_written",
 *           "rows_skipped", "jd_files_written"}
 * Failure: {"ok": false, "code": "<class>"} where class is one of:
 *           "invalid"   — payload rejected. PERMANENT, do not retry.
 *           "ratelimit" — over the minute or day ceiling. Retry with backoff.
 *           "busy"      — lock contention. Retry once after a short backoff.
 *           "internal"  — unexpected. Retry once, then alert.
 * The class is all the caller gets — no field names, IDs, messages or stack
 * traces. Detail goes only to this script's Executions log. The class leaks
 * nothing worth protecting: the caller controls the payload and already knows
 * whether it is malformed, and the secret being defended is the URL.
 * NOTE: ContentService always returns HTTP 200. The body is the only signal —
 * the client must parse it, and must follow the 302 to googleusercontent.com.
 *
 * ── Layout ──────────────────────────────────────────────────────────────────
 * Everything lives under one Drive folder this script itself creates and
 * owns ("Sam - Job Pipeline", cached as root_folder_id) — required under the
 * drive.file scope, which cannot see a folder made by hand in the Drive UI.
 * One spreadsheet per month ("Jobs-YYYY-MM"), one tab per day ("YYYY-MM-DD"),
 * plus one JD archive folder per month ("JD-YYYY-MM"), all nested inside the
 * root folder. Month → spreadsheet/folder ID cached in PropertiesService.
 *
 * Expects a POST body:
 * {
 *   "date": "2026-09-04",
 *   "rows": [
 *     {"url": "https://...", "company": "...", "title": "...", "location": "...",
 *      "score": 7, "notes": "...", "jd_drive_name": "2026-09-04_co_role.md"}
 *   ],
 *   "jd_files": [
 *     {"name": "2026-09-04_co_role.md", "content": "full JD text..."}
 *   ]
 * }
 * `url` is required and must be http(s). `jd_drive_name` may reference a file
 * uploaded on a PREVIOUS call — jd_files may be empty and the link still resolves.
 */

var HEADER = ['url', 'company', 'title', 'location', 'score', 'notes', 'first_seen', 'jd_link'];

// Hard caps. A legitimate nightly batch is single-digit to low tens of rows.
var MAX_PAYLOAD_CHARS = 3000000;   // UTF-16 code units, NOT bytes
var MAX_ROWS = 100;
var MAX_JD_FILES = 20;
var MAX_FIELD_LEN = 500;           // url, company, title, location
var MAX_NOTES_LEN = 2000;
var MAX_JD_CONTENT_LEN = 200000;   // per JD file
var MAX_TOTAL_JD_CONTENT = 1000000; // aggregate across the batch — do not rely
                                    // on MAX_PAYLOAD_CHARS to bound this
var MAX_JD_LOOKUPS = 30;           // Drive round trips spent resolving jd_link

// Two ceilings. The per-minute one dampens bursts; the per-DAY one is what
// actually bounds Google's per-day quota burn (Drive file creations and total
// runtime) and therefore whether a leaked URL can take the nightly run offline.
var MAX_REQUESTS_PER_MINUTE = 3;
var MAX_REQUESTS_PER_DAY = 30;

var LOCK_TIMEOUT_MS = 10000;       // short on purpose: a long wait under attack
                                   // multiplies runtime-quota burn

var FILENAME_PATTERN = /^(?!\.+$)[A-Za-z0-9._-]{1,150}$/;
var DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
var URL_PATTERN = /^https?:\/\/\S/i;

// ── Entry points ────────────────────────────────────────────────────────────

function doGet() {
  // Without this, a GET returns an Apps Script HTML error page — confirms a
  // live deployment and breaks any health check.
  return jsonResponse(fail('invalid'));
}

function doPost(e) {
  // Rate limiting happens BEFORE the lock on purpose: a rejected request must
  // not acquire the lock, or an attacker gets to hold it for LOCK_TIMEOUT_MS
  // per request, which is the very quota burn we are trying to prevent.
  // The get-then-put in rateLimitOk() is therefore not serialized and can
  // overshoot by roughly the concurrency factor. Accepted: the overshoot is
  // small and bounded, and the day ceiling still holds the real line.
  try {
    if (!rateLimitOk()) return reject('ratelimit', 'over request ceiling');
    if (!e || !e.postData || !e.postData.contents) return reject('invalid', 'no postData');
    if (e.postData.contents.length > MAX_PAYLOAD_CHARS) {
      return reject('invalid', 'payload ' + e.postData.contents.length + ' chars');
    }

    var body;
    try {
      body = JSON.parse(e.postData.contents);
    } catch (parseErr) {
      return reject('invalid', 'JSON.parse: ' + parseErr);
    }

    var v = validate(body);
    if (!v.ok) return reject('invalid', v.reason);

    // Apps Script web apps DO execute concurrently. Everything below is a
    // check-then-act (getLastRow -> setValues, get-or-create spreadsheet /
    // tab / Drive file) and races silently without this.
    var lock = LockService.getScriptLock();
    if (!lock.tryLock(LOCK_TIMEOUT_MS)) return reject('busy', 'lock timeout');
    try {
      return jsonResponse(handleWrite(v.value));
    } finally {
      lock.releaseLock();
    }
  } catch (err) {
    logErr(err && err.stack ? err.stack : String(err));
    return jsonResponse(fail('internal'));
  }
}

// ── Response helpers ────────────────────────────────────────────────────────

function fail(code) {
  return { ok: false, code: code || 'internal' };
}

function reject(code, reason) {
  // Reason is logged, never returned. Every rejection path logs — a silently
  // dropped nightly batch with an empty Executions log is undebuggable.
  logErr('reject[' + code + ']: ' + reason);
  return jsonResponse(fail(code));
}

function jsonResponse(obj) {
  return ContentService
    .createTextOutput(JSON.stringify(obj))
    .setMimeType(ContentService.MimeType.JSON);
}

function logErr(msg) {
  // On the legacy Rhino runtime `console` is undefined, so console.error would
  // throw INSIDE the catch block and Apps Script would return its own HTML
  // error page — with a stack trace — to the caller, defeating the opaque
  // failure design. Pin runtimeVersion to V8; this is the belt and braces.
  try {
    console.error(msg);
  } catch (e1) {
    try { Logger.log(msg); } catch (e2) { /* nothing left to try */ }
  }
}

// ── Rate limiting ───────────────────────────────────────────────────────────

function rateLimitOk() {
  // Minute window: CacheService. Best-effort storage — entries can be evicted
  // before TTL, so this FAILS OPEN. Fine for burst dampening; it is explicitly
  // not the thing standing between a leaked URL and quota exhaustion.
  var cache = CacheService.getScriptCache();
  var minKey = 'rl:m:' + Math.floor(Date.now() / 60000);
  var minCount = Number(cache.get(minKey)) || 0;
  if (minCount >= MAX_REQUESTS_PER_MINUTE) return false;
  cache.put(minKey, String(minCount + 1), 70);

  // Day window: PropertiesService, which is durable. This is the real bound.
  var props = PropertiesService.getScriptProperties();
  var dayKey = 'rl_day_' + Utilities.formatDate(new Date(), 'Etc/UTC', 'yyyy-MM-dd');
  var dayCount = Number(props.getProperty(dayKey)) || 0;
  if (dayCount >= MAX_REQUESTS_PER_DAY) return false;
  props.setProperty(dayKey, String(dayCount + 1));

  if (dayCount === 0) pruneOldDayKeys(props, dayKey); // once per day
  return true;
}

function pruneOldDayKeys(props, keepKey) {
  try {
    var all = props.getProperties();
    for (var k in all) {
      if (k.indexOf('rl_day_') === 0 && k !== keepKey) props.deleteProperty(k);
    }
  } catch (err) {
    logErr('pruneOldDayKeys: ' + err); // never fail a request over housekeeping
  }
}

// ── Validation ──────────────────────────────────────────────────────────────
// Strict allow-list. Anything unexpected fails the WHOLE request rather than
// being dropped, coerced, or partially processed.
//
// Prototype pollution is a non-issue here and stays that way as long as no one
// adds a merge: JSON.parse creates "__proto__" as an OWN data property (spec
// uses CreateDataProperty), so Object.prototype is untouched, and handleWrite
// reads only known field names. Do not introduce Object.assign / spread over
// parsed input. Objects used as lookup maps below are Object.create(null).

function validate(body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return { ok: false, reason: 'body not an object' };
  }
  if (typeof body.date !== 'string' || !DATE_PATTERN.test(body.date)) {
    return { ok: false, reason: 'date missing or malformed' };
  }

  var rows = body.rows === undefined ? [] : body.rows;
  if (!Array.isArray(rows)) return { ok: false, reason: 'rows not an array' };
  if (rows.length > MAX_ROWS) {
    return { ok: false, reason: 'rows ' + rows.length + ' > ' + MAX_ROWS };
  }
  for (var i = 0; i < rows.length; i++) {
    var rowErr = validRow(rows[i]);
    if (rowErr) return { ok: false, reason: 'rows[' + i + ']: ' + rowErr };
  }

  var jdFiles = body.jd_files === undefined ? [] : body.jd_files;
  if (!Array.isArray(jdFiles)) return { ok: false, reason: 'jd_files not an array' };
  if (jdFiles.length > MAX_JD_FILES) {
    return { ok: false, reason: 'jd_files ' + jdFiles.length + ' > ' + MAX_JD_FILES };
  }
  var totalContent = 0;
  for (var j = 0; j < jdFiles.length; j++) {
    var fileErr = validJdFile(jdFiles[j]);
    if (fileErr) return { ok: false, reason: 'jd_files[' + j + ']: ' + fileErr };
    totalContent += jdFiles[j].content.length;
  }
  if (totalContent > MAX_TOTAL_JD_CONTENT) {
    return { ok: false, reason: 'total jd content ' + totalContent };
  }

  return { ok: true, value: { date: body.date, rows: rows, jd_files: jdFiles } };
}

function validRow(r) {
  if (!r || typeof r !== 'object' || Array.isArray(r)) return 'not an object';

  // url is REQUIRED. Without this, {} is a valid row and the validator does
  // not actually protect the sheet from junk. The scheme check also keeps
  // javascript: / data: out of a column something downstream may make clickable.
  if (typeof r.url !== 'string') return 'url not a string';
  if (!URL_PATTERN.test(r.url)) return 'url not http(s)';
  if (r.url.length > MAX_FIELD_LEN) return 'url too long';

  var optionalStrings = [
    ['company', MAX_FIELD_LEN],
    ['title', MAX_FIELD_LEN],
    ['location', MAX_FIELD_LEN],
    ['notes', MAX_NOTES_LEN],
    ['jd_drive_name', 150]
  ];
  for (var i = 0; i < optionalStrings.length; i++) {
    var f = optionalStrings[i][0];
    var max = optionalStrings[i][1];
    if (r[f] === undefined || r[f] === null) continue;
    if (typeof r[f] !== 'string') return f + ' not a string';
    if (r[f].length > max) return f + ' too long (' + r[f].length + ')';
  }

  if (r.jd_drive_name && !FILENAME_PATTERN.test(r.jd_drive_name)) {
    return 'jd_drive_name fails filename pattern';
  }
  if (r.score !== undefined && r.score !== null && typeof r.score !== 'number') {
    return 'score not a number';
  }
  return null;
}

function validJdFile(f) {
  if (!f || typeof f !== 'object' || Array.isArray(f)) return 'not an object';
  if (typeof f.name !== 'string') return 'name not a string';
  if (!FILENAME_PATTERN.test(f.name)) return 'name fails filename pattern';
  if (typeof f.content !== 'string') return 'content not a string';
  if (f.content.length > MAX_JD_CONTENT_LEN) return 'content too long (' + f.content.length + ')';
  return null;
}

// Row data originates from scraped job postings — untrusted. A value starting
// with =, +, - or @ is a LIVE FORMULA in Google Sheets, not merely a CSV-export
// hazard. setValues() parses input as if typed, so a leading apostrophe forces
// plain text. The leading [\s'"]* also covers a formula character hidden behind
// whitespace or a quote.
function safeCell(value) {
  var s = value == null ? '' : String(value);
  if (/^[\s'"]*[=+\-@]/.test(s)) return "'" + s;
  return s;
}

// ── Write ───────────────────────────────────────────────────────────────────

function handleWrite(body) {
  var month = body.date.slice(0, 7);
  var ss = getOrCreateMonthSpreadsheet(month);
  var sheet = getOrCreateDayTab(ss, month, body.date);

  // Filename -> Drive URL. Object.create(null) because the keys are
  // attacker-influenced and must not collide with Object.prototype members.
  var jdUrls = Object.create(null);
  var jdWritten = 0;

  if (body.jd_files.length > 0) {
    var jdFolder = getJdFolder(month, true);
    body.jd_files.forEach(function (f) {
      var it = jdFolder.getFilesByName(f.name);
      var file;
      if (it.hasNext()) {
        file = it.next();
        file.setContent(f.content);
        if (it.hasNext()) {
          // Pre-existing duplicates (from a past unlocked race) are updated
          // only in the first copy. Worth knowing about.
          logErr('duplicate JD filename in Drive, only first updated: ' + f.name);
        }
      } else {
        file = jdFolder.createFile(f.name, f.content, MimeType.PLAIN_TEXT);
      }
      jdUrls[f.name] = file.getUrl();
      jdWritten++;
    });
  }

  var linkFor = makeJdLinker(month, jdUrls);

  var written = 0;
  var skipped = 0;

  if (body.rows.length > 0) {
    // Row writes were the only non-idempotent operation here: JD files dedupe
    // by name, rows did not, so a retry after a SUCCESSFUL write silently
    // duplicated every posting. Dedupe on url within the day tab and within
    // the batch.
    var seen = existingUrls(sheet);
    var values = [];

    body.rows.forEach(function (r) {
      if (seen[r.url]) { skipped++; return; }
      seen[r.url] = true;
      values.push([
        safeCell(r.url),
        safeCell(r.company),
        safeCell(r.title),
        safeCell(r.location),
        (typeof r.score === 'number' && isFinite(r.score)) ? r.score : '',
        safeCell(r.notes),
        body.date,
        linkFor(r.jd_drive_name)
      ]);
    });

    if (values.length > 0) {
      sheet.getRange(sheet.getLastRow() + 1, 1, values.length, HEADER.length)
           .setValues(values);
      written = values.length;
    }
  }

  return {
    ok: true,
    spreadsheetUrl: ss.getUrl(),
    tab: body.date,
    rows_written: written,
    rows_skipped: skipped,
    jd_files_written: jdWritten
  };
}

// Resolves jd_drive_name -> a link, INDEPENDENTLY of whether this particular
// call uploaded any files.
//
// The bug this replaces: jd_link was gated on `jdFolder`, which was only
// non-null when jd_files.length > 0. A row referencing a JD uploaded on a
// previous night — files already in Drive, nothing to upload tonight — got a
// blank link column with no error. Reported independently while writing
// sheet_sync.py; same defect as D2 in the 2026-09-03 review.
//
// Files written this call cost nothing (we hold the File). Older references
// cost one Drive round trip each, memoized, capped at MAX_JD_LOOKUPS so a
// maximal payload cannot spend 100 round trips against the 6-minute execution
// limit. Past the cap, falls back to a month-qualified text reference.
function makeJdLinker(month, prewritten) {
  var cache = prewritten || Object.create(null);
  var folder;          // undefined = not looked up yet, null = does not exist
  var lookups = 0;

  return function (name) {
    if (!name) return '';
    if (Object.prototype.hasOwnProperty.call(cache, name)) return cache[name];
    if (lookups >= MAX_JD_LOOKUPS) return month + '-JD/' + name;

    lookups++;
    if (folder === undefined) folder = getJdFolder(month, false);
    if (!folder) {
      // No archive folder for this month yet — nothing to resolve against.
      // Do NOT create one just to service a lookup.
      cache[name] = month + '-JD/' + name;
      return cache[name];
    }

    var it = folder.getFilesByName(name);
    cache[name] = it.hasNext() ? it.next().getUrl() : (month + '-JD/' + name);
    return cache[name];
  };
}

function existingUrls(sheet) {
  var set = Object.create(null);
  var last = sheet.getLastRow();
  if (last < 2) return set;
  var vals = sheet.getRange(2, 1, last - 1, 1).getValues();
  for (var i = 0; i < vals.length; i++) {
    // Strip the safeCell apostrophe so comparison matches what was sent.
    var u = String(vals[i][0] == null ? '' : vals[i][0]).replace(/^'/, '');
    if (u) set[u] = true;
  }
  return set;
}

// ── Storage helpers ─────────────────────────────────────────────────────────

// Everything this script writes lives under one root folder, itself created
// (not just referenced) by this script — required under the drive.file
// scope, which only ever sees files/folders the script itself created.
// A folder made by hand in the Drive UI is invisible to DriveApp.getFolderById
// here, no matter its ID: drive.file grants nothing over it. Cached once in
// Script Properties, same pattern as ss_YYYY-MM and jd_folder_YYYY-MM below.
function getOrCreateRootFolder() {
  var props = PropertiesService.getScriptProperties();
  var key = 'root_folder_id';
  var id = props.getProperty(key);

  if (id) {
    try {
      return DriveApp.getFolderById(id);
    } catch (err) {
      logErr('getFolderById failed for ' + key + ' (' + id + '): ' + err +
             ' — NOT recreating. If the folder is genuinely gone, delete the "' +
             key + '" script property by hand.');
      throw new Error('root_folder_unavailable');
    }
  }

  var folder = DriveApp.createFolder('Sam - Job Pipeline');
  props.setProperty(key, folder.getId());
  return folder;
}

function getOrCreateMonthSpreadsheet(month) {
  var props = PropertiesService.getScriptProperties();
  var key = 'ss_' + month;
  var id = props.getProperty(key);

  if (id) {
    try {
      return SpreadsheetApp.openById(id);
    } catch (err) {
      // Previously this fell through and CREATED A SECOND SPREADSHEET,
      // overwriting the cached ID. openById also throws on transient Drive
      // 500s and rate limits, so a momentary blip mid-month orphaned the first
      // half of the month's data. Recreating storage is too destructive to
      // trigger on an ambiguous signal — fail the request instead.
      logErr('openById failed for ' + key + ' (' + id + '): ' + err +
             ' — NOT recreating. If the spreadsheet is genuinely gone, delete ' +
             'the "' + key + '" script property by hand.');
      throw new Error('spreadsheet_unavailable');
    }
  }

  var ss = SpreadsheetApp.create('Jobs-' + month);
  DriveApp.getFileById(ss.getId()).moveTo(getOrCreateRootFolder());
  props.setProperty(key, ss.getId());
  return ss;
}

function getOrCreateDayTab(ss, month, date) {
  var sheet = ss.getSheetByName(date);
  if (sheet) return sheet;

  try {
    sheet = ss.insertSheet(date);
  } catch (err) {
    // Lost a race, or the name appeared between the two calls.
    sheet = ss.getSheetByName(date);
    if (!sheet) throw err;
    return sheet;
  }

  sheet.getRange(1, 1, 1, HEADER.length).setValues([HEADER]);
  sheet.setFrozenRows(1);
  removeDefaultStubSheet(ss, date);
  return sheet;
}

// SpreadsheetApp.create names the default sheet per the OWNER ACCOUNT'S LOCALE
// — "Tabellenblatt1" on a German-locale account, not "Sheet1". The old
// getSheetByName('Sheet1') lookup returned null there, the stub survived, and
// the cleanup_pending flag was cleared anyway so it was never retried. Match
// on emptiness instead of name, and do not depend on a flag.
function removeDefaultStubSheet(ss, keepName) {
  try {
    var sheets = ss.getSheets();
    if (sheets.length < 2) return;
    for (var i = 0; i < sheets.length; i++) {
      var s = sheets[i];
      if (s.getName() === keepName) continue;
      if (DATE_PATTERN.test(s.getName())) continue; // a real day tab
      if (s.getLastRow() === 0 && s.getLastColumn() === 0) {
        ss.deleteSheet(s);
        return;
      }
    }
  } catch (err) {
    logErr('removeDefaultStubSheet: ' + err); // cosmetic; never fail the write
  }
}

function getJdFolder(month, createIfMissing) {
  var props = PropertiesService.getScriptProperties();
  var key = 'jd_folder_' + month;
  var id = props.getProperty(key);

  if (id) {
    try {
      return DriveApp.getFolderById(id);
    } catch (err) {
      // Same reasoning as getOrCreateMonthSpreadsheet: falling through here
      // created a SECOND archive folder and silently broke the
      // get-or-update-by-name idempotency — every file re-created instead of
      // updated, older files invisible.
      logErr('getFolderById failed for ' + key + ' (' + id + '): ' + err +
             ' — NOT recreating. If the folder is genuinely gone, delete the "' +
             key + '" script property by hand.');
      throw new Error('jd_folder_unavailable');
    }
  }

  if (!createIfMissing) return null;

  // Was DriveApp.getRootFolder().createFolder(...). getRootFolder() reads a
  // folder this script did not create, which is the one call that breaks under
  // the drive.file scope. Nested under the script's own root folder (see
  // getOrCreateRootFolder) rather than created at My Drive's top level, so
  // every JD-YYYY-MM folder lands next to its month's spreadsheet.
  var folder = getOrCreateRootFolder().createFolder('JD-' + month);
  props.setProperty(key, folder.getId());
  return folder;
}