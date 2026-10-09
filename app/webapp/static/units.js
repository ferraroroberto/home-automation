/* AC / units — the AC tab's rows, Home's AC rows and the unit sheet.
 *
 * Split out of main.js (issue #346 maintainability split) — the one domain
 * module that had never followed the pattern every sibling domain already
 * uses (#197: security.js → security-alarm/schedules/scene/override/notify.js,
 * network.js → network-devices/wifi/dhcp.js). main.js keeps boot wiring,
 * theme toggle, nav-debug init, and login; everything unit/AC-specific lives
 * here.
 *
 * Step 3/8 of #872 (#881) moved the AC tab onto the shared parts. Both tabs
 * draw a unit with one row renderer (row.js): power is the row's switch, and
 * tapping the row opens the unit sheet. Decision 4 of #872: the setpoint lives
 * in the sheet, and the sheet is an instant one (sheet.js), so each change is
 * sent as it is made, like the switch on the row; Done only closes. The sheet's
 * main page holds the daily controls (setpoint, power, mode, fan); the rarer
 * settings (vanes, the temperature rule, schedules, the name) are settings
 * rows that open a page of the same sheet. Schedules are a dense collection
 * whose Add / Edit open the shared staged editor (dense-editor.js).
 */

'use strict';

import {
  state,
  els,
  toast,
  reportFetchOk,
  modeIcon,
} from './state.js';
import { icon } from './_vendored/icons/icons.js';
import { chipEl } from './chip.js';
import { jsonApi, isAuthRequired, reportActionFailure } from './api.js';
import { restoreSnapshot, saveSnapshot } from './snapshots.js';
import { toggleMarkup, setToggleState, isToggleOn, wireToggle } from './toggle.js';
import { createViewState, markTabFailure, renderFeedback, staleText } from './view-state.js';
import { createPoller } from './poll.js';
import { rowEl } from './row.js';
import { sheet } from './sheet.js';
import { denseListEditor, renderSummaryRow } from './dense-editor.js';
import { setHeadPart } from './head-status.js';

const DEFAULT_RANGE = [16, 31];
// A burst of − / + taps sends one setpoint, this long after the last tap, so
// three taps are one cloud write rather than three racing ones.
const SETPOINT_SETTLE_MS = 600;
const MODE_LABELS = { Heat: 'Heat', Cool: 'Cool', Automatic: 'Auto', Dry: 'Dry', Fan: 'Fan' };
const MODE_STATES = { Heat: 'heating', Cool: 'cooling', Automatic: 'auto', Dry: 'drying', Fan: 'fan only' };
const NUMBER_WORDS = { One: '1', Two: '2', Three: '3', Four: '4', Five: '5' };

const acView = createViewState('units');

// The open sheet's server-side settings, read on open (GET …/rule and
// …/schedule): null / not loaded until they land, so a summary never claims a
// value it hasn't read.
let currentRule = null;
let currentScheduleEntries = [];
let schedulesLoaded = false;
// A setpoint the user has dialled but the unit hasn't confirmed yet.
let pendingSetpoint = null;
let setpointTimer = null;
// Text and number fields save on change (blur); a close that beats the blur
// still saves what was typed.
let nameDirty = false;
let ruleDirty = false;

function renderAcFeedback() {
  if (!els.paneAc) return;
  renderFeedback(acView, els.acFeedback, {
    paneEl: els.paneAc,
    ariaBusy: true,
    icon: 'snowflake',
    loadingIcon: 'refresh-cw',
    loadingLabel: 'Reading AC units…',
    emptyLabel: 'No AC units configured',
    errorLabel: 'AC units unavailable',
    snapshotKey: 'units',
    onRetry: function () { loadUnits(); },
  });
}

function markAcFailure() {
  markTabFailure(acView, {
    hasData: state.units.length > 0,
    scope: 'units',
    label: 'AC units',
    render: function () {
      renderAll();
      renderAcSummary();
    },
  });
}

// --------------------------------------------------------------- helpers
function unitById(id) {
  return state.units.find(function (u) { return u.unit_id === id; });
}

function tempRange(unit) {
  let rng = unit.temp_ranges && unit.temp_ranges[unit.operation_mode];
  if (!rng && unit.temp_ranges) {
    const vals = Object.values(unit.temp_ranges);
    if (vals.length) rng = vals[0];
  }
  return rng && rng.length === 2 ? rng : DEFAULT_RANGE;
}

function fmtTemp(v) {
  return v == null ? '—' : Number(v).toFixed(1) + '°';
}

// A summary's temperature: "24°", or "24.5°" when it has a half.
function fmtTempShort(v) {
  if (v == null) return '—';
  const n = Math.round(Number(v) * 10) / 10;
  return (Number.isInteger(n) ? String(n) : n.toFixed(1)) + '°';
}

// Signed boost offset for the chip (#575) — e.g. "-2" while cooling, "+1.5"
// while heating. Trims a whole-number offset to "-2" rather than "-2.0" to
// stay compact; the sign itself always comes from the server's boost_delta_c,
// never re-derived from operation_mode here.
function fmtBoostDelta(v) {
  const rounded = Math.round(v * 10) / 10;
  const abs = Math.abs(rounded);
  const magnitude = Number.isInteger(abs) ? String(abs) : abs.toFixed(1);
  return (rounded < 0 ? '-' : '+') + magnitude;
}

// A fan speed or vane position as words: "One" → "1", "LeftCentre" → "Left
// centre". The API value stays the value; only the label changes.
function optionLabel(v) {
  if (v == null || v === '') return '—';
  if (NUMBER_WORDS[v]) return NUMBER_WORDS[v];
  const words = String(v).replace(/([a-z])([A-Z])/g, '$1 $2');
  return words.charAt(0) + words.slice(1).toLowerCase();
}

function modeLabel(mode) {
  return MODE_LABELS[mode] || mode || '—';
}

function ruleTargetForMode(rule, mode) {
  if (!rule || rule.enabled !== true) return null;
  if (mode === 'Cool' || mode === 'Dry') return rule.cool_target == null ? null : rule.cool_target;
  if (mode === 'Heat') return rule.heat_target == null ? null : rule.heat_target;
  return null;
}

function activeRuleTarget(unit) {
  const rule = unit.temperature_rule || {};
  return rule.enabled && rule.active_target != null ? rule.active_target : null;
}

function scheduleCount(unit) {
  const sched = unit.schedule || {};
  if (Number.isFinite(Number(sched.count))) return Number(sched.count);
  return sched.enabled === true ? 1 : 0;
}

// A unit whose WiFi adapter has lost its cloud connection (`reachable: false`
// from /api/units, issue #520). Commands sent to it are silently swallowed by
// the cloud, so its controls are inerted rather than left looking live. Only an
// explicit `false` counts — an absent field (older payload, restored snapshot)
// means "unknown", and unknown must never lock a working unit out.
function isOffline(unit) {
  return unit.reachable === false;
}

// Whether a command to the unit can be sent now: not while it is offline,
// and not from a restored snapshot or a failed poll (the readings are old).
function canCommand(unit) {
  return !isOffline(unit) && acView.state !== 'stale';
}

function displayLabel(unit) {
  return unit.display_name || unit.name || '';
}

function sortedUnits() {
  return state.units.slice().sort(function (a, b) {
    return displayLabel(a).localeCompare(displayLabel(b));
  });
}

// --------------------------------------------------- write + re-render
async function applyControl(unitId, patch) {
  try {
    toast('Sending…', 'pending');
    const updated = await jsonApi('/api/units/' + encodeURIComponent(unitId), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    state.units = state.units.map(function (u) {
      return u.unit_id === updated.unit_id ? updated : u;
    });
    renderAll();
    renderAcSummary();
    if (state.selectedId === updated.unit_id) renderSheet(updated);
    toast('Saved', 'success');
  } catch (exc) {
    reportActionFailure(exc, 'Failed');
  }
}

// ------------------------------------------------------------- the row
// The shared row (row.js, #880): the mode glyph in the avatar, badged while
// the unit runs and when it is offline; the name; one meta line with the room
// and set temperatures (and the rule's room target while it steers); the power
// switch as the one trailing item. An exception closes the meta line as a
// chip: Offline (attention), or Boost while the solar boost runs (accent).
// Home and the AC tab draw the same row, and both open the same sheet.
function acRow(u) {
  const on = u.power === true;
  const offline = isOffline(u);
  const label = displayLabel(u) || 'Unit';
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'toggle ac-line-toggle' + (on ? ' on' : '');
  toggle.setAttribute('role', 'switch');
  toggle.setAttribute('aria-checked', on ? 'true' : 'false');
  toggle.setAttribute('aria-label', 'Power ' + label);
  toggle.innerHTML = toggleMarkup(on);
  toggle.disabled = !canCommand(u);
  toggle.addEventListener('click', function () {
    applyControl(u.unit_id, { power: !on });
  });

  const room = fmtTemp(u.room_temperature);
  const rule = u.temperature_rule || {};
  let meta = 'Last read ' + room;
  let chip = null;
  if (offline) {
    chip = chipEl('Offline', 'attention', 'ac-line-offline');
  } else {
    // Room → set, the arrow a Lucide glyph with words for a screen reader.
    meta = document.createElement('span');
    meta.innerHTML = 'Room ' + room + icon('arrow-right', 'row-meta-arrow') +
      '<span class="visually-hidden"> to </span>' + fmtTemp(u.set_temperature);
    // Live "currently boosted" state (#554): automation, not a user action,
    // so it is a chip on the row; the signed delta (#575) makes the rule →
    // set-to maths legible. While it runs, the chip carries the rule's story.
    const ruleTarget = activeRuleTarget(u);
    if (rule.boost_active) {
      chip = chipEl(rule.boost_delta_c == null ? 'Boost' : 'Boost ' + fmtBoostDelta(rule.boost_delta_c),
        'accent', 'ac-line-boost');
    } else if (ruleTarget != null) {
      meta.appendChild(document.createTextNode(' · rule ' + fmtTemp(ruleTarget)));
    }
  }
  const row = rowEl({
    className: 'ac-row',
    glyph: modeIcon(u.operation_mode),
    badge: offline ? 'down' : (on ? 'up' : null),
    title: label,
    meta: meta,
    chip: chip,
    onOpen: function (btn) { openDetail(u.unit_id, btn); },
    trail: toggle,
  });
  row.dataset.unitId = u.unit_id;
  return row;
}

function unitList() {
  const list = document.createElement('ul');
  list.className = 'action-rows';
  sortedUnits().forEach(function (u) { list.appendChild(acRow(u)); });
  return list;
}

// The AC tab: the units as rows in one card. The pane's own feedback block
// (loading, empty, unavailable, stale) speaks when there is nothing to list.
function renderAll() {
  renderAcFeedback();
  if (!els.acUnits) return;
  els.acUnits.innerHTML = '';
  els.acUnits.hidden = !state.units.length;
  if (state.units.length) els.acUnits.appendChild(unitList());
}

// ------------------------------------------------ AC rows on Home
function renderAcSummary() {
  els.acSummary.innerHTML = '';
  renderAcHead();
  if (!state.units.length) {
    const empty = document.createElement('p');
    empty.className = 'muted small ac-summary-empty';
    if (acView.state === 'loading') empty.textContent = 'Reading AC units…';
    else if (acView.state === 'error') empty.textContent = 'AC units unavailable.';
    else empty.textContent = 'No AC units configured.';
    els.acSummary.appendChild(empty);
    return;
  }
  els.acSummary.appendChild(unitList());
  if (acView.state === 'stale') {
    const note = document.createElement('p');
    note.className = 'muted small snapshot-note ac-snapshot-note';
    note.textContent = staleText(acView, 'units');
    els.acSummary.appendChild(note);
  }
}

// The AC header's live line (head-status.js, #880): units offline in
// attention, else how many are running. Only from a live read: a restored
// snapshot or a failed poll says nothing rather than an old count.
function renderAcHead() {
  if (acView.state !== 'ready' && acView.state !== 'empty') {
    setHeadPart('ac', 'units', null);
    return;
  }
  const offline = state.units.filter(isOffline).length;
  const running = state.units.filter(function (u) {
    return u.power === true && !isOffline(u);
  }).length;
  setHeadPart('ac', 'units', {
    exceptions: offline ? [{ text: offline + ' offline', tone: 'attention' }] : [],
    fact: state.units.length ? running + ' running' : 'No units',
  });
}

// ------------------------------------------------------------ the sheet
const unitSheet = sheet(els.detail, {
  model: 'instant',
  closeButton: els.detailClose,
  doneButton: els.detailDone,
  // A command's read-back re-renders the rows under the sheet, so the row
  // that opened it is usually gone by now: focus its replacement.
  fallbackFocus: function () {
    if (!state.selectedId) return null;
    const rows = document.querySelectorAll('.ac-row[data-unit-id="' + CSS.escape(state.selectedId) + '"] .action-row-main');
    return Array.from(rows).find(function (btn) { return btn.offsetParent !== null; }) || null;
  },
  onClose: function () {
    const id = state.selectedId;
    flushSetpoint();
    if (id && nameDirty) saveDisplayName(id);
    if (id && ruleDirty) saveRule(id);
    state.selectedId = null;
    currentRule = null;
    currentScheduleEntries = [];
    schedulesLoaded = false;
    showPage('main');
  },
});

function sheetPages() {
  return Array.from(els.detail.querySelectorAll('.unit-sheet-page'));
}

// One page of the sheet at a time. A settings row opens its page, the header
// then names the page and shows Back; the main page carries the unit's name.
function showPage(name) {
  let shown = null;
  sheetPages().forEach(function (page) {
    page.hidden = page.dataset.page !== name;
    if (!page.hidden) shown = page;
  });
  const main = name === 'main';
  els.detail.dataset.page = name;
  els.detailBack.hidden = main;
  const unit = state.selectedId ? unitById(state.selectedId) : null;
  els.detailName.textContent = main
    ? ((unit && displayLabel(unit)) || 'Unit')
    : ((shown && shown.dataset.title) || '');
}

function openPage(name) {
  showPage(name);
  if (name === 'name') els.detailDisplayName.focus();
  else els.detailBack.focus();
}

function backToMain() {
  const from = els.detail.dataset.page;
  showPage('main');
  const link = els.detail.querySelector('[data-page-to="' + from + '"]');
  if (link) link.focus();
}

// A segmented control (design.md: the selected segment is the state) of the
// unit's own options. `glyph(value)` draws a segment as an icon with its word
// kept for assistive tech; otherwise the segment is the word.
function renderSegmented(group, options, current, opts) {
  group.innerHTML = '';
  options.forEach(function (value) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'segmented-item';
    btn.dataset.value = value;
    btn.setAttribute('aria-pressed', value === current ? 'true' : 'false');
    const word = opts.label(value);
    if (opts.glyph) {
      btn.innerHTML = icon(opts.glyph(value)) + '<span class="visually-hidden"></span>';
      btn.lastChild.textContent = word;
      btn.title = word;
    } else {
      btn.textContent = word;
    }
    btn.disabled = opts.disabled;
    btn.addEventListener('click', function () {
      if (btn.getAttribute('aria-pressed') === 'true') return;
      opts.onPick(value);
    });
    group.appendChild(btn);
  });
}

function fillSelect(sel, options, current) {
  sel.innerHTML = '';
  options.forEach(function (o) {
    const opt = document.createElement('option');
    opt.value = o;
    opt.textContent = optionLabel(o);
    if (o === current) opt.selected = true;
    sel.appendChild(opt);
  });
}

function shownSetpoint(unit) {
  if (pendingSetpoint && pendingSetpoint.unitId === unit.unit_id) return pendingSetpoint.value;
  return unit.set_temperature == null ? null : Number(unit.set_temperature);
}

function renderSetpoint(unit) {
  const [tmin, tmax] = tempRange(unit).map(Number);
  const shown = shownSetpoint(unit);
  const live = canCommand(unit);
  els.detailSetTemp.textContent = fmtTemp(shown);
  els.detailTempDown.disabled = !live || (shown != null && shown <= tmin);
  els.detailTempUp.disabled = !live || (shown != null && shown >= tmax);
  const room = fmtTemp(unit.room_temperature);
  let line;
  if (isOffline(unit)) line = 'Last read ' + room;
  else if (unit.power !== true) line = 'Room ' + room + ' · off';
  else line = 'Room ' + room + ' · ' + (MODE_STATES[unit.operation_mode] || modeLabel(unit.operation_mode).toLowerCase());
  els.detailRoomLine.textContent = line;
}

function ruleSummary(unit) {
  const rule = currentRule || unit.temperature_rule || {};
  if (rule.enabled !== true) return 'Off';
  const parts = [];
  if (rule.cool_target != null) parts.push('Cool ' + fmtTempShort(rule.cool_target));
  if (rule.heat_target != null) parts.push((parts.length ? 'heat ' : 'Heat ') + fmtTempShort(rule.heat_target));
  if (!parts.length) parts.push('On');
  if (rule.boost_enabled) parts.push('boost');
  return parts.join(' · ');
}

// Unknown until the list has been read (blank, never a guessed "None").
function scheduleSummary(unit) {
  if (!schedulesLoaded) {
    const count = scheduleCount(unit);
    return count ? count + ' on' : '';
  }
  const total = currentScheduleEntries.length;
  if (!total) return 'None';
  const on = currentScheduleEntries.filter(function (e) { return e.enabled !== false; }).length;
  return on === total ? on + ' on' : on + ' of ' + total + ' on';
}

function vanesSummary(unit) {
  const parts = [];
  if (unit.has_vane_vertical) parts.push(optionLabel(unit.vane_vertical));
  if (unit.has_vane_horizontal) parts.push(optionLabel(unit.vane_horizontal));
  return parts.join(' · ');
}

function renderLinkValues(unit) {
  els.detailVanesValue.textContent = vanesSummary(unit);
  els.detailRuleValue.textContent = ruleSummary(unit);
  els.detailSchedValue.textContent = scheduleSummary(unit);
  els.detailNameValue.textContent = displayLabel(unit) || 'Unit';
}

// Everything the sheet shows that comes from the unit itself. Called on open,
// after each command's read-back and on every poll while the sheet is open, so
// a unit dropping offline inerts its controls (#520). It never touches the
// name and rule fields, which the user may be typing into.
function renderSheet(unit) {
  const live = canCommand(unit);
  if (els.detail.dataset.page === 'main' || !els.detail.dataset.page) {
    els.detailName.textContent = displayLabel(unit) || 'Unit';
  }
  els.detailOffline.hidden = !isOffline(unit);
  renderSetpoint(unit);

  setToggleState(els.detailPower, unit.power === true);
  els.detailPower.disabled = !live;

  renderSegmented(els.detailMode, unit.operation_modes || [], unit.operation_mode, {
    label: modeLabel,
    glyph: modeIcon,
    disabled: !live,
    onPick: function (mode) { applyControl(unit.unit_id, { operation_mode: mode }); },
  });

  const fans = unit.fan_speeds || [];
  els.detailFanSpeedRow.hidden = !fans.length;
  renderSegmented(els.detailFanSpeed, fans, unit.fan_speed, {
    label: optionLabel,
    disabled: !live,
    onPick: function (fan) { applyControl(unit.unit_id, { fan_speed: fan }); },
  });

  els.detailVanesLink.hidden = !unit.has_vane_vertical && !unit.has_vane_horizontal;
  els.detailVaneVerticalRow.hidden = !unit.has_vane_vertical;
  if (unit.has_vane_vertical) fillSelect(els.detailVaneVertical, unit.vane_vertical_options || [], unit.vane_vertical);
  els.detailVaneHorizontalRow.hidden = !unit.has_vane_horizontal;
  if (unit.has_vane_horizontal) fillSelect(els.detailVaneHorizontal, unit.vane_horizontal_options || [], unit.vane_horizontal);
  els.detailVaneVertical.disabled = !live;
  els.detailVaneHorizontal.disabled = !live;

  renderLinkValues(unit);
}

function openDetail(unitId, trigger) {
  const unit = unitById(unitId);
  if (!unit) return;
  state.selectedId = unitId;
  currentRule = null;
  currentScheduleEntries = [];
  schedulesLoaded = false;
  nameDirty = false;
  ruleDirty = false;
  showPage('main');
  els.detailDisplayName.value = unit.display_name || '';
  els.detailDisplayName.placeholder = unit.name || 'Custom label…';
  els.detailOriginalName.textContent = 'Original name: ' + (unit.name || '—');
  // Until the saved rule and schedules are read, nothing on their pages may
  // save: a write built on blank fields or an empty list would erase them.
  setRuleFieldsEnabled(false);
  els.schedAdd.disabled = true;
  renderScheduleList();
  renderSheet(unit);
  unitSheet.open(trigger);
  loadAutomation(unitId);
}

// --------------------------------------------------------- the setpoint
function nudgeSetpoint(direction) {
  const unit = state.selectedId ? unitById(state.selectedId) : null;
  if (!unit || !canCommand(unit)) return;
  const [tmin, tmax] = tempRange(unit).map(Number);
  const step = Number(unit.temp_step) || 0.5;
  const shown = shownSetpoint(unit);
  const base = shown == null ? tmin : shown;
  const next = Math.min(Math.max(Math.round((base + direction * step) * 10) / 10, tmin), tmax);
  if (next === base) return;
  pendingSetpoint = { unitId: unit.unit_id, value: next };
  renderSetpoint(unit);
  clearTimeout(setpointTimer);
  setpointTimer = setTimeout(flushSetpoint, SETPOINT_SETTLE_MS);
}

// Send the dialled setpoint now (the settle timer, or the sheet closing). The
// dialled value stays on screen until the unit's read-back replaces it.
function flushSetpoint() {
  clearTimeout(setpointTimer);
  setpointTimer = null;
  const pending = pendingSetpoint;
  if (!pending || pending.sent) return;
  const unit = unitById(pending.unitId);
  if (!unit || pending.value === Number(unit.set_temperature)) {
    pendingSetpoint = null;
    if (unit && state.selectedId === unit.unit_id) renderSetpoint(unit);
    return;
  }
  pending.sent = true;
  applyControl(pending.unitId, { set_temperature: pending.value }).finally(function () {
    if (pendingSetpoint !== pending) return;
    pendingSetpoint = null;
    const now = unitById(pending.unitId);
    if (now && state.selectedId === now.unit_id) renderSetpoint(now);
  });
}

// ------------------------------------------------- name, rule, schedules
// Load the saved rule + schedules for the open unit. Failures stay quiet (the
// auth overlay handles 401): the summaries stay blank rather than guessed.
async function loadAutomation(unitId) {
  try {
    const rule = await jsonApi('/api/units/' + encodeURIComponent(unitId) + '/rule');
    if (state.selectedId !== unitId) return;
    currentRule = rule;
    fillRuleFields(rule);
    setRuleFieldsEnabled(true);
  } catch (exc) {
    if (isAuthRequired(exc)) return;
  }
  try {
    const sched = await jsonApi('/api/units/' + encodeURIComponent(unitId) + '/schedule');
    if (state.selectedId !== unitId) return;
    currentScheduleEntries = normalizeScheduleEntries(sched);
    schedulesLoaded = true;
    els.schedAdd.disabled = false;
    renderScheduleList();
  } catch (exc) {
    if (isAuthRequired(exc)) return;
  }
  const unit = unitById(unitId);
  if (unit && state.selectedId === unitId) renderLinkValues(unit);
}

function setRuleFieldsEnabled(enabled) {
  [els.ruleEnabled, els.ruleCoolTarget, els.ruleHeatTarget, els.ruleBoostEnabled, els.ruleBoostOffset]
    .forEach(function (field) { field.disabled = !enabled; });
}

function fillRuleFields(rule) {
  setToggleState(els.ruleEnabled, rule.enabled === true);
  els.ruleCoolTarget.value = rule.cool_target == null ? '' : rule.cool_target;
  els.ruleHeatTarget.value = rule.heat_target == null ? '' : rule.heat_target;
  setToggleState(els.ruleBoostEnabled, rule.boost_enabled === true);
  els.ruleBoostOffset.value = rule.boost_offset_c == null ? '' : rule.boost_offset_c;
}

async function saveDisplayName(unitId) {
  nameDirty = false;
  const newName = els.detailDisplayName.value.trim();
  const unit = unitById(unitId);
  if (!unit || newName === (unit.display_name || '')) return;
  try {
    await jsonApi('/api/units/' + encodeURIComponent(unitId) + '/display_name', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ display_name: newName }),
    });
    state.units = state.units.map(function (u) {
      if (u.unit_id !== unitId) return u;
      return Object.assign({}, u, { display_name: newName || null });
    });
    renderAll();
    renderAcSummary();
    const updated = unitById(unitId);
    if (updated && state.selectedId === unitId) renderLinkValues(updated);
    toast('Name saved', 'success');
  } catch (exc) {
    reportActionFailure(exc, 'Failed to save name');
  }
}

// A number input → a float, or null when blank/invalid (clears the target).
function numOrNull(input) {
  const raw = (input.value || '').trim();
  if (raw === '') return null;
  const n = Number(raw);
  return Number.isFinite(n) ? n : null;
}

async function saveRule(unitId) {
  ruleDirty = false;
  const payload = {
    enabled: isToggleOn(els.ruleEnabled),
    cool_target: numOrNull(els.ruleCoolTarget),
    heat_target: numOrNull(els.ruleHeatTarget),
    boost_enabled: isToggleOn(els.ruleBoostEnabled),
    // Not Optional server-side (always steers by a real offset) — fall back
    // to the same 2.0 default rather than sending null.
    boost_offset_c: numOrNull(els.ruleBoostOffset) == null ? 2.0 : numOrNull(els.ruleBoostOffset),
  };
  try {
    await jsonApi('/api/units/' + encodeURIComponent(unitId) + '/rule', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    state.units = state.units.map(function (u) {
      if (u.unit_id !== unitId) return u;
      return Object.assign({}, u, {
        temperature_rule: {
          enabled: payload.enabled,
          active_target: ruleTargetForMode(payload, u.operation_mode),
          boost_enabled: payload.boost_enabled,
          boost_offset_c: payload.boost_offset_c,
          // Live engine state — saving the rule doesn't change it immediately.
          boost_active: (u.temperature_rule || {}).boost_active === true,
          boost_delta_c: (u.temperature_rule || {}).boost_delta_c,
        },
      });
    });
    if (state.selectedId === unitId) currentRule = payload;
    renderAll();
    renderAcSummary();
    const unit = unitById(unitId);
    if (unit && state.selectedId === unitId) renderLinkValues(unit);
    toast('Rule saved', 'success');
  } catch (exc) {
    reportActionFailure(exc, 'Failed to save rule');
  }
}

function newScheduleId() {
  if (window.crypto && typeof window.crypto.randomUUID === 'function') {
    return 'sched-' + window.crypto.randomUUID().slice(0, 8);
  }
  return 'sched-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2, 6);
}

function normalizeScheduleEntries(body) {
  let raw = [];
  if (Array.isArray(body)) raw = body;
  else if (body && Array.isArray(body.entries)) raw = body.entries;
  else if (body && (body.enabled === true || body.time || body.operation_mode || body.set_temperature != null)) raw = [body];
  return raw.map(function (entry, idx) {
    return Object.assign({ id: 'schedule-' + (idx + 1), enabled: true, time: '08:00', power: true }, entry);
  });
}

function scheduleDefaults() {
  const unit = unitById(state.selectedId) || {};
  return {
    id: newScheduleId(),
    enabled: true,
    time: '08:00',
    power: true,
    operation_mode: unit.operation_mode || null,
    set_temperature: unit.set_temperature == null ? null : unit.set_temperature,
    target_temperature: null,
    fan_speed: unit.fan_speed || null,
    vane_vertical_direction: unit.has_vane_vertical ? (unit.vane_vertical || null) : null,
    vane_horizontal_direction: unit.has_vane_horizontal ? (unit.vane_horizontal || null) : null,
  };
}

// "On · cool 25.0° · fan 2" — what the entry will do, in words.
function scheduleMeta(entry) {
  if (entry.power === false) return 'Off';
  const parts = ['On'];
  if (entry.operation_mode) {
    parts.push(modeLabel(entry.operation_mode).toLowerCase() +
      (entry.set_temperature == null ? '' : ' ' + fmtTempShort(entry.set_temperature)));
  } else if (entry.set_temperature != null) {
    parts.push(fmtTempShort(entry.set_temperature));
  }
  if (entry.target_temperature != null) parts.push('room ' + fmtTempShort(entry.target_temperature));
  if (entry.fan_speed) parts.push('fan ' + optionLabel(entry.fan_speed).toLowerCase());
  return parts.join(' · ');
}

function renderScheduleList() {
  els.schedList.innerHTML = '';
  els.schedNote.hidden = !schedulesLoaded || currentScheduleEntries.length > 0;
  currentScheduleEntries
    .map(function (entry, idx) { return { entry: entry, idx: idx }; })
    .sort(function (a, b) { return (a.entry.time || '').localeCompare(b.entry.time || ''); })
    .forEach(function (item) {
      const entry = item.entry;
      els.schedList.appendChild(renderSummaryRow({
        id: entry.id,
        idAttr: 'acScheduleId',
        title: entry.time || '08:00',
        meta: scheduleMeta(entry),
        openLabel: 'Edit schedule at ' + entry.time,
        onOpen: function (main) { scheduleEditor.open(item.idx, main); },
        toggleName: 'ac-schedule-enabled',
        toggleOn: entry.enabled !== false,
        toggleLabel: 'Enable schedule at ' + entry.time,
        onToggle: function (on) {
          scheduleEditor.save(currentScheduleEntries.map(function (e, index) {
            return index === item.idx ? Object.assign({}, e, { enabled: on }) : e;
          }));
        },
      }));
    });
  const unit = state.selectedId ? unitById(state.selectedId) : null;
  if (unit) els.detailSchedValue.textContent = scheduleSummary(unit);
}

function syncScheduleProfile() {
  els.acScheduleProfile.hidden = els.acSchedulePower.value === 'false';
}

const scheduleEditor = denseListEditor({
  dialog: els.acScheduleDialog,
  addButton: els.schedAdd,
  closeButton: els.acScheduleEditorClose,
  saveButton: els.acScheduleSave,
  deleteButton: els.acScheduleDelete,
  titleEl: els.acScheduleEditorTitle,
  listEl: els.schedList,
  focusEl: els.acScheduleTime,
  rowIdAttr: 'data-ac-schedule-id',
  titles: { add: 'Add schedule', edit: 'Edit schedule' },
  deleteConfirm: {
    title: 'Delete this schedule?',
    message: 'This schedule will be removed permanently.',
  },
  toasts: { saved: 'Schedules saved', failed: "Couldn't save schedules" },
  defaults: scheduleDefaults,
  getEntries: function () { return currentScheduleEntries; },
  setEntries: function (entries) { currentScheduleEntries = entries; },
  normalize: normalizeScheduleEntries,
  render: renderScheduleList,
  populate: function (staged) {
    const unit = unitById(state.selectedId) || {};
    setToggleState(els.acScheduleEnabled, staged.enabled !== false);
    els.acScheduleTime.value = staged.time || '08:00';
    els.acSchedulePower.value = staged.power === false ? 'false' : 'true';
    fillSelect(els.acScheduleMode, unit.operation_modes || [], staged.operation_mode || unit.operation_mode);
    Array.from(els.acScheduleMode.options).forEach(function (opt) { opt.textContent = modeLabel(opt.value); });
    els.acScheduleTemp.value = staged.set_temperature == null ? '' : staged.set_temperature;
    els.acScheduleTarget.value = staged.target_temperature == null ? '' : staged.target_temperature;
    fillSelect(els.acScheduleFan, unit.fan_speeds || [], staged.fan_speed || unit.fan_speed);
    els.acScheduleVvRow.hidden = !unit.has_vane_vertical;
    if (unit.has_vane_vertical) {
      fillSelect(els.acScheduleVv, unit.vane_vertical_options || [], staged.vane_vertical_direction || unit.vane_vertical);
    }
    els.acScheduleVhRow.hidden = !unit.has_vane_horizontal;
    if (unit.has_vane_horizontal) {
      fillSelect(els.acScheduleVh, unit.vane_horizontal_options || [], staged.vane_horizontal_direction || unit.vane_horizontal);
    }
    syncScheduleProfile();
  },
  collect: function (staged) {
    staged.enabled = isToggleOn(els.acScheduleEnabled);
    staged.time = els.acScheduleTime.value || '08:00';
    staged.power = els.acSchedulePower.value !== 'false';
    staged.operation_mode = els.acScheduleMode.value || null;
    staged.set_temperature = numOrNull(els.acScheduleTemp);
    staged.target_temperature = numOrNull(els.acScheduleTarget);
    staged.fan_speed = els.acScheduleFan.value || null;
    staged.vane_vertical_direction = els.acScheduleVvRow.hidden ? null : (els.acScheduleVv.value || null);
    staged.vane_horizontal_direction = els.acScheduleVhRow.hidden ? null : (els.acScheduleVh.value || null);
  },
  get endpoint() {
    return '/api/units/' + encodeURIComponent(state.selectedId) + '/schedule';
  },
  bodyKey: 'entries',
  // The unit payload carries a summary (how many are on); keep it in step so
  // a later open shows the right count before its list is read.
  afterSave: function (entries) {
    const id = state.selectedId;
    const on = entries.filter(function (e) { return e.enabled !== false; }).length;
    state.units = state.units.map(function (u) {
      if (u.unit_id !== id) return u;
      return Object.assign({}, u, { schedule: Object.assign({}, u.schedule, { enabled: on > 0, count: on }) });
    });
  },
});

// --------------------------------------------------------------- boot
export async function loadUnits() {
  if (!state.units.length) {
    acView.set('loading', { liveUnavailable: false });
    renderAll();
    renderAcSummary();
  }
  try {
    const body = await jsonApi('/api/units');
    reportFetchOk('units');
    saveSnapshot('units', body);
    state.units = (body && body.units) || [];
    acView.set(state.units.length ? 'ready' : 'empty', {
      updatedAt: new Date(),
      liveUnavailable: false,
    });
    renderAll();
    renderAcSummary();
    refreshOpenSheet();
  } catch (exc) {
    // A 401 already surfaced the login overlay (api.js → showLogin); stay quiet.
    if (isAuthRequired(exc)) return;
    markAcFailure();
    refreshOpenSheet();
  }
}

// An open sheet is not part of renderAll(): refresh it from the new read, so
// a unit that drops offline (or a poll that fails) inerts its controls rather
// than leaving them live (#520).
function refreshOpenSheet() {
  if (!state.selectedId) return;
  const selected = unitById(state.selectedId);
  if (selected) renderSheet(selected);
}

export function restoreUnitsSnapshot() {
  const body = restoreSnapshot('units');
  if (!body) return;
  state.units = (body && body.units) || [];
  acView.set(state.units.length ? 'stale' : 'empty', {
    updatedAt: state.snapshotUpdatedAt.units,
    liveUnavailable: false,
  });
  renderAll();
  renderAcSummary();
}

// AC units only matter on Home (rows) and AC (rows), so poll them only while
// one of those tabs is active rather than every 30s everywhere (#209). The
// initial boot fetch is the caller's `loadUnits()` call.
const scheduleUnits = createPoller(loadUnits);
export function onUnitsTab(tab) {
  scheduleUnits(0);
  if (tab === 'home' || tab === 'ac') {
    loadUnits();
    scheduleUnits(30_000);
  }
}

// --------------------------------------------------------------- wire up
// Called once from main.js's boot(), alongside every other domain's
// wire*Controls() function.
export function wireUnitsControls() {
  els.detailBack.addEventListener('click', backToMain);
  els.detail.querySelectorAll('[data-page-to]').forEach(function (link) {
    link.addEventListener('click', function () { openPage(link.dataset.pageTo); });
  });

  els.detailTempDown.addEventListener('click', function () { nudgeSetpoint(-1); });
  els.detailTempUp.addEventListener('click', function () { nudgeSetpoint(1); });
  els.detailPower.addEventListener('click', function () {
    const unit = state.selectedId ? unitById(state.selectedId) : null;
    if (unit && canCommand(unit)) applyControl(unit.unit_id, { power: unit.power !== true });
  });
  els.detailVaneVertical.addEventListener('change', function () {
    if (state.selectedId) applyControl(state.selectedId, { vane_vertical_direction: els.detailVaneVertical.value });
  });
  els.detailVaneHorizontal.addEventListener('change', function () {
    if (state.selectedId) applyControl(state.selectedId, { vane_horizontal_direction: els.detailVaneHorizontal.value });
  });

  els.detailDisplayName.addEventListener('input', function () { nameDirty = true; });
  els.detailDisplayName.addEventListener('change', function () {
    if (state.selectedId && nameDirty) saveDisplayName(state.selectedId);
  });
  els.detailDisplayName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); els.detailDisplayName.blur(); }
  });

  // The temperature rule saves as it changes: a switch on tap, a number on
  // change (blur), each with its toast.
  wireToggle(els.ruleEnabled, function () { if (state.selectedId) saveRule(state.selectedId); });
  wireToggle(els.ruleBoostEnabled, function () { if (state.selectedId) saveRule(state.selectedId); });
  [els.ruleCoolTarget, els.ruleHeatTarget, els.ruleBoostOffset].forEach(function (input) {
    input.addEventListener('input', function () { ruleDirty = true; });
    input.addEventListener('change', function () {
      if (state.selectedId && ruleDirty) saveRule(state.selectedId);
    });
  });

  wireToggle(els.acScheduleEnabled, function (on) {
    if (scheduleEditor.staged) scheduleEditor.staged.enabled = on;
  });
  els.acSchedulePower.addEventListener('change', syncScheduleProfile);
  scheduleEditor.wire();
}
