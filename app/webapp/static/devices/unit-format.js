/* The AC units' pure helpers: the labels and temperature formats the rows, the
 * Home stepper and the unit sheet share, and the offline test. No DOM, no state
 * beyond the unit object passed in, so units.js keeps only what draws and sends.
 */

'use strict';

const DEFAULT_RANGE = [16, 31];
const MODE_LABELS = { Heat: 'Heat', Cool: 'Cool', Automatic: 'Auto', Dry: 'Dry', Fan: 'Fan' };
const NUMBER_WORDS = { One: '1', Two: '2', Three: '3', Four: '4', Five: '5' };

export function tempRange(unit) {
  let rng = unit.temp_ranges && unit.temp_ranges[unit.operation_mode];
  if (!rng && unit.temp_ranges) {
    const vals = Object.values(unit.temp_ranges);
    if (vals.length) rng = vals[0];
  }
  return rng && rng.length === 2 ? rng : DEFAULT_RANGE;
}

export function fmtTemp(v) {
  return v == null ? '—' : Number(v).toFixed(1) + '°';
}

// A summary's temperature: "24°", or "24.5°" when it has a half.
export function fmtTempShort(v) {
  if (v == null) return '—';
  const n = Math.round(Number(v) * 10) / 10;
  return (Number.isInteger(n) ? String(n) : n.toFixed(1)) + '°';
}

// Signed boost offset for the chip — e.g. "-2" while cooling, "+1.5"
// while heating. Trims a whole-number offset to "-2" rather than "-2.0" to
// stay compact; the sign itself always comes from the server's boost_delta_c,
// never re-derived from operation_mode here.
export function fmtBoostDelta(v) {
  const rounded = Math.round(v * 10) / 10;
  const abs = Math.abs(rounded);
  const magnitude = Number.isInteger(abs) ? String(abs) : abs.toFixed(1);
  return (rounded < 0 ? '-' : '+') + magnitude;
}

// A fan speed or vane position as words: "One" → "1", "LeftCentre" → "Left
// centre". The API value stays the value; only the label changes.
export function optionLabel(v) {
  if (v == null || v === '') return '—';
  if (NUMBER_WORDS[v]) return NUMBER_WORDS[v];
  const words = String(v).replace(/([a-z])([A-Z])/g, '$1 $2');
  return words.charAt(0) + words.slice(1).toLowerCase();
}

export function modeLabel(mode) {
  return MODE_LABELS[mode] || mode || '—';
}

export function ruleTargetForMode(rule, mode) {
  if (!rule || rule.enabled !== true) return null;
  if (mode === 'Cool' || mode === 'Dry') return rule.cool_target == null ? null : rule.cool_target;
  if (mode === 'Heat') return rule.heat_target == null ? null : rule.heat_target;
  return null;
}

export function activeRuleTarget(unit) {
  const rule = unit.temperature_rule || {};
  return rule.enabled && rule.active_target != null ? rule.active_target : null;
}

export function scheduleCount(unit) {
  const sched = unit.schedule || {};
  if (Number.isFinite(Number(sched.count))) return Number(sched.count);
  return sched.enabled === true ? 1 : 0;
}

// A unit whose WiFi adapter has lost its cloud connection (`reachable: false`
// from /api/units). Commands sent to it are silently swallowed by
// the cloud, so its controls are inerted rather than left looking live. Only an
// explicit `false` counts — an absent field (older payload, restored snapshot)
// means "unknown", and unknown must never lock a working unit out.
export function isOffline(unit) {
  return unit.reachable === false;
}

export function displayLabel(unit) {
  return unit.display_name || unit.name || '';
}
