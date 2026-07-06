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
 * style), and the notify modules' applyPrefs/renderConfiguredNote (parallel
 * closures over their own els/FIELDS maps).
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
