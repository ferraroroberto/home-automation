/* Home Automation — shared value-formatting helpers (issue #383).
 *
 * One implementation for the formatting helpers that used to be duplicated
 * per tab module (the /design-sync sibling-consistency findings,
 * fleet-config#277). The copies had drifted: energy's esc() didn't null-guard
 * (rendered the string "null") and skipped the quote entity, while plugs/ups
 * showed ungrouped watts ("1234 W") where energy grouped ("1,234 W") — the
 * same physical quantity formatted differently per tab. Same quantity, same
 * format, one write path.
 *
 * Deliberately NOT here (verified distinct-by-design in the same lint run):
 * fmtTemp (units.js one-decimal AC setpoints vs weather.js rounded degrees),
 * fmtTime (activity.js epoch + same-day short form vs presence.js ISO locale
 * form), fmtUptime (network.js compact router style vs vm.js "just now" VM
 * style).
 */

'use strict';

/** HTML-escape untrusted text for innerHTML interpolation. Null-safe:
 *  null/undefined render as '' (energy's old copy rendered "null"). */
export function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** Group digits in threes with a comma — "3745" → "3,745". */
export function group(n) {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

/** Watts, grouped — "1,234 W". The grouped variant is canonical (energy's);
 *  plugs/ups used to show "1234 W" for the same quantity. */
export function fmtW(v) {
  return v == null ? '—' : group(Math.round(Number(v))) + ' W';
}

/** Percent, 0–100 in — "42%", no space (the network/ups dominant form; the
 *  energy tab used to print "42 %"). Callers holding a 0–1 fraction convert
 *  at the call site. */
export function fmtPct(v) {
  return v == null ? '—' : Math.round(Number(v)) + '%';
}

/** A date as the browser's own local "YYYY-MM-DD". `toISOString()` is UTC and
 *  names yesterday for the first hours of a local day east of Greenwich, which
 *  is not the day the server frames hourly rollups or fires one-shot alarms by. */
export function localIsoDate(d) {
  const dt = d || new Date();
  return [
    dt.getFullYear(),
    ('0' + (dt.getMonth() + 1)).slice(-2),
    ('0' + dt.getDate()).slice(-2),
  ].join('-');
}

/** The bar + fill + "NN%" trio every Wi-Fi signal cell renders (issue #571 —
 *  built verbatim in network-devices.js, network-wifi.js and
 *  network-survey.js). Returns a DocumentFragment for the caller to append
 *  into its own cell span, whose class differs per list. */
export function renderSignalBar(signal) {
  const frag = document.createDocumentFragment();
  const bar = document.createElement('span');
  bar.className = 'net-signal-bar';
  const fill = document.createElement('span');
  fill.className = 'net-signal-fill';
  fill.style.width = Math.max(0, Math.min(100, signal)) + '%';
  bar.appendChild(fill);
  frag.appendChild(bar);
  const pct = document.createElement('span');
  pct.className = 'net-signal-pct';
  pct.textContent = signal + '%';
  frag.appendChild(pct);
  return frag;
}

/* Failure copy people read (design.md "Async data & feedback", #879). A server
 * `detail` or an Error message passes through only when it reads as a sentence
 * written for people ("Time must be HH:MM"). Anything carrying infrastructure
 * detail gives the plain fallback instead: a URL, a network or hardware
 * address, a host name, an exception class, a timeout or bare HTTP status, a
 * snake_case key, a path or a structure dump. The raw text goes to the console
 * so the failure stays diagnosable; the server log keeps the full detail. */
const UNSAFE_ERROR_TEXT = [
  /[a-z][a-z0-9+.-]*:\/\//i,
  /\b\d{1,3}(?:\.\d{1,3}){3}\b/,
  /\b[0-9a-f]{2}(?:[:-][0-9a-f]{2}){5}\b/i,
  /\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}\b/i,
  /\b[A-Z][A-Za-z]*(?:Error|Exception)\b/,
  /traceback|errno|timed? ?out\b|timeout|failed to fetch|networkerror|load failed|refused|econn|ssl|certificate/i,
  /^HTTP \d{3}$/i,
  /\b[a-z][a-z0-9]*_[a-z0-9_]+\b/i,
  /(?:^|\s)\/[a-z]/i,
  /[{}<>[\]\\]/,
];
const ERROR_TEXT_MAX = 120;

/** The user-facing reason for a failure: `exc.message` (or the string itself)
 *  when it is safe to show, otherwise `fallback`. With no fallback, a bad
 *  gateway, timeout or dropped connection reads "not reachable right now" and
 *  anything else "something went wrong". */
export function friendlyError(exc, fallback) {
  const raw = String((exc && (exc.message || (typeof exc === 'string' ? exc : ''))) || '').trim();
  const status = exc && exc.status;
  const generic = fallback || (!status || status >= 502 ? 'not reachable right now' : 'something went wrong');
  if (!raw) return generic;
  const unsafe = raw.length > ERROR_TEXT_MAX || UNSAFE_ERROR_TEXT.some(function (re) { return re.test(raw); });
  if (!unsafe) return raw;
  console.warn('[home-automation] failure detail withheld from the UI:', raw);
  return generic;
}
