/**
 * Job Pipeline write endpoint.
 *
 * ── Access model ────────────────────────────────────────────────────────────
 * No shared-secret token. Access control is the deployment URL's own
 * ~300-bit unguessable deployment ID (the /macros/s/<id>/exec path of the web-app URL).
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
 * ── Manifest (appscript.json, same directory) ──────────────────────────────
 * Deploy this file TOGETHER WITH appscript.json — see README.md in this
 * directory. The manifest pins the scopes explicitly instead of letting Apps
 * Script infer them:
 *
 *   "https://www.googleapis.com/auth/drive"          DriveApp (folders, JD files)
 *   "https://www.googleapis.com/auth/spreadsheets"   SpreadsheetApp
 *
 * WHY THE FULL `drive` SCOPE, NOT `drive.file`: DriveApp.createFolder /
 * getFolderById / moveTo / createFile are not covered by `drive.file`. With
 * that scope every JD upload failed, caught in doPost as code "internal", with
 * "Specified permissions are not sufficient to call DriveApp.createFolder.
 * Required permissions: https://www.googleapis.com/auth/drive" in the log.
 * (Found 30 Sep 2026; it also meant the next month's spreadsheet could not be
 * created, since that goes through DriveApp.moveTo.) The grant is wider than
 * the code's behaviour: there is no read / list / share / delete operation
 * anywhere in this file, and adding one is a security-relevant change.
 * A least-privilege alternative is the Advanced Drive service under
 * `drive.file`; it needs a rewrite of the DriveApp calls and is not done.
 * "runtimeVersion": "V8" is not optional — see logErr().
 * After changing scopes: run any function once from the editor to approve
 * them, then Deploy -> Manage deployments -> New version (the URL is kept).
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
 * Everything lives under ONE existing Drive folder, named by the
 * root_folder_id Script Property. A web request never creates it: a missing
 * property once made the script silently create a second folder with the same
 * name. Set the property before the first write (fresh install: run
 * setupRootFolder() once by hand, see README.md).
 * One spreadsheet per month ("Jobs-YYYY-MM"), one tab per day ("YYYY-MM-DD"),
 * plus one JD archive folder per month ("JD-YYYY-MM"), all nested inside the
 * root folder. The month's spreadsheet and JD folder are FOUND BY NAME inside
 * the root folder on every write (nothing is cached by ID), and created there
 * if missing. Do not rename them or move them out of the root folder: the
 * script would not find them and would create new ones.
 * Script Properties in use: root_folder_id (config) and rl_day_YYYY-MM-DD
 * (request counter). Old ss_YYYY-MM / jd_folder_YYYY-MM properties are ignored.
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

// Everything this script writes lives under ONE root folder, named by the
// root_folder_id Script Property. A web request never creates it: a missing
// property used to make the script silently create a second folder with the
// same name (30 Sep 2026), splitting a month's files across two folders. Fail
// instead. (The month's spreadsheet and JD folder are looked up by name under
// this folder, see below.)
function getRootFolder() {
  var id = PropertiesService.getScriptProperties().getProperty('root_folder_id');
  if (!id) {
    logErr('root_folder_id Script Property is not set - refusing to create a ' +
           'second root folder. Set it to the existing folder\'s ID (fresh ' +
           'install: run setupRootFolder() once by hand).');
    throw new Error('root_folder_not_configured');
  }
  try {
    return DriveApp.getFolderById(id);
  } catch (err) {
    logErr('getFolderById failed for root_folder_id (' + id + '): ' + err +
           ' - NOT recreating. If the folder is genuinely gone, fix the ' +
           '"root_folder_id" script property by hand.');
    throw new Error('root_folder_unavailable');
  }
}

// The month's spreadsheet and JD folder are found BY NAME inside the root
// folder, not by a cached ID. The cached-ID scheme (ss_YYYY-MM, jd_folder_YYYY-MM
// Script Properties) failed in both directions: a stale or foreign ID made every
// write fail, and a deleted property made the script silently create a second
// "Jobs-YYYY-MM" next to the first (30 Sep / 1 Oct 2026). Looking up by name
// needs no state to drift. Called only under the script lock (see doPost), so
// two requests cannot both decide "missing" and each create one. Several
// non-trashed matches: use the OLDEST and log it, so a stray duplicate can never
// win over the original.
function oldestLive(it, what, mimeType) {
  var best = null, n = 0;
  while (it.hasNext()) {
    var x = it.next();
    if (x.isTrashed()) continue;
    if (mimeType && x.getMimeType() !== mimeType) continue;
    n++;
    if (!best || x.getDateCreated() < best.getDateCreated()) best = x;
  }
  if (n > 1) logErr('duplicate ' + what + ' (' + n + ' live copies) - using the oldest');
  return best;
}

function getOrCreateMonthSpreadsheet(month) {
  var root = getRootFolder();
  var name = 'Jobs-' + month;
  var found = oldestLive(root.getFilesByName(name), 'spreadsheet ' + name,
                         MimeType.GOOGLE_SHEETS);
  // If openById throws (transient Drive error), the request fails; nothing is
  // created on an ambiguous signal.
  if (found) return SpreadsheetApp.openById(found.getId());

  var ss = SpreadsheetApp.create(name);
  DriveApp.getFileById(ss.getId()).moveTo(root);
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
  var root = getRootFolder();
  var name = 'JD-' + month;
  var found = oldestLive(root.getFoldersByName(name), 'folder ' + name);
  if (found) return found;
  if (!createIfMissing) return null;
  // Nested under the root folder rather than created at My Drive's top level,
  // so every JD-YYYY-MM folder lands next to its month's spreadsheet.
  return root.createFolder(name);
}

// ── Diagnostics ─────────────────────────────────────────────────────────────

// Run by hand, once, on a FRESH install only (no existing folder to adopt).
// Never called by doPost. On an existing install set the root_folder_id Script
// Property to the existing folder's ID instead.
function setupRootFolder() {
  var props = PropertiesService.getScriptProperties();
  var existing = props.getProperty('root_folder_id');
  if (existing) { Logger.log('Already set: ' + existing); return; }
  var f = DriveApp.createFolder('Job Pipeline');
  props.setProperty('root_folder_id', f.getId());
  Logger.log('Created ' + f.getUrl());
}

// Run by hand from the editor (function dropdown -> Run); never called by
// doPost. Exercises the Drive path that a rows-only write does not touch, and
// prints the failure instead of the opaque "internal" a caller would see. It
// creates the JD-2026-09 folder if missing, which the first real write would
// do anyway. Change the month as needed. Logs the root folder in use and the
// parent of the JD folder - these must be the same folder.
function diagnose() {
  try {
    var root = getRootFolder();
    var f = getJdFolder('2026-09', true);
    Logger.log('root: ' + root.getUrl());
    Logger.log('OK: ' + f.getName() + ' in ' + f.getParents().next().getUrl());
  } catch (e) {
    Logger.log('FAIL: ' + e);
  }
}
