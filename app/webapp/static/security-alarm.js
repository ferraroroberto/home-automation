/* Alarm state + detectors controller (split out of security.js, issue #197).
 *
 * Owns the glance card's state line and arm control (segmented), the Recent
 * events group, and the Detectors group with the per-zone detail/rename sheet
 * (#882 put both groups on the shared row). State writes are one-tap POST/PUT
 * calls that re-render from the returned live state; the top-level redraw is
 * delegated back to the boot module's renderSecurity().
 *
 * ACTIONS / ACTION_LABELS are exported because the schedule editor reuses the
 * same action set.
 */

'use strict';

import { state, els, toast, persistedFlag, SECURITY_SHOW_HIDDEN_KEY } from './state.js';
import { jsonApi, reportActionFailure } from './api.js';
import { renderSecurity } from './security.js';
import { openActivity } from './activity.js';
import { rowEl } from './row.js';
import { toggleMarkup } from './toggle.js';
import { icon } from './_vendored/icons/icons.js';
import { detailModal } from './detail-modal.js';
import { chipEl } from './chip.js';
import { setHeadPart } from './head-status.js';
import { setTabBadge, showTab } from './tabs.js';
import { setHousePart } from './home.js';

// The "show hidden detectors" filter, on the shared localStorage wrapper.
const showHiddenPref = persistedFlag(SECURITY_SHOW_HIDDEN_KEY, false);

// The full alarm-control row, in display order. Always rendered; the live state
// machine decides which are tappable and which is the current (selected) one.
export const ACTIONS = ['disarm', 'partial', 'perimeter', 'arm'];
export const ACTION_LABELS = {
  disarm: 'Disarm',
  partial: 'Partial',
  arm: 'Full',
  perimeter: 'Perimeter',
};
// Optimistic ("pending") toast shown the instant an action is tapped, before the
// refresh — mirrors the plugs/lights "Sending…" convention (issue #218).
const ACTION_TOASTS = {
  disarm: 'Disarming…',
  partial: 'Arming partial…',
  perimeter: 'Arming perimeter…',
  arm: 'Arming full…',
};
// Result ("success") toast shown once the action completes — so it's clear what
// happened (command sent → done), matching the rest of the app (issue #218).
const ACTION_DONE = {
  disarm: 'Disarmed',
  partial: 'Partial armed',
  perimeter: 'Perimeter armed',
  arm: 'Armed (full)',
};
const MODE_LABELS = {
  disarmed: 'Not armed',
  armed: 'Fully armed',
  arming: 'Arming',
  partial: 'Partial',
  perimeter: 'Perimeter',
  triggered: 'Triggered',
  unknown: 'Unknown',
};

function supported(action) {
  const actions = (state.security && state.security.supported_actions) || [];
  return actions.includes(action);
}

function currentMode() {
  const security = state.security || {};
  // The backend mode is authoritative. We deliberately do NOT infer "triggered"
  // from memory_alarm/alarm_pending — those flags are sticky (they don't clear
  // on disarm), which left a badge stuck on forever (issue #223). A tamper while
  // disarmed instead relies on Disarm being always tappable to clear it.
  return security.mode || 'unknown';
}

function displayLabel() {
  const security = state.security || {};
  const mode = currentMode();
  return MODE_LABELS[mode] || security.label || 'Unknown';
}

function statusClass(mode) {
  if (mode === 'triggered') return 'is-alert';
  if (mode === 'disarmed') return 'is-disarmed';
  if (mode === 'arming') return 'is-arming';
  if (mode === 'armed') return 'is-armed';
  if (mode === 'partial') return 'is-partial';
  if (mode === 'perimeter') return 'is-perimeter';
  return '';
}

function actionAvailable(action) {
  if (!supported(action)) return false;
  // Disarm is ALWAYS tappable (issue #223): a tamper/triggered alarm while the
  // panel still reports "disarmed" must be clearable from the app, and the cloud
  // exposes no reliable "needs clearing" signal to gate on. Less intuitive (live
  // even when quiet) but robust. Arm options stay disarmed-only.
  if (action === 'disarm') return true;
  return currentMode() === 'disarmed';
}

// One panel → one in-flight action at a time (issue #368): a busy flag checked
// at entry and reflected as disabled pills, mirroring vm.js's onToggle guard.
// Double-tapping Arm/Disarm must not send a second POST to the physical alarm.
let actionBusy = false;

async function postAction(action) {
  if (actionBusy || !actionAvailable(action)) return;
  actionBusy = true;
  renderActions();
  toast(ACTION_TOASTS[action] || 'Working…', 'pending');  // fires the instant you tap
  try {
    state.security = await jsonApi('/api/security/' + encodeURIComponent(action), {
      method: 'POST',
    });
    renderSecurity();
    await loadSecurityEvents();
    toast(ACTION_DONE[action] || 'Done', 'success');
  } catch (exc) {
    reportActionFailure(exc, 'Failed');
  } finally {
    actionBusy = false;
    renderActions();
  }
}

// Per-zone in-flight guard (issue #368): bypassing zone A must not block zone B,
// but a double-tap on the same zone's toggle must not double-POST.
const bypassBusy = new Set();

async function setBypass(zone, bypass, btn) {
  if (bypassBusy.has(zone.id)) return;
  bypassBusy.add(zone.id);
  if (btn) btn.disabled = true;
  toast('Sending…', 'pending');
  try {
    state.security = await jsonApi(
      '/api/security/zones/' + encodeURIComponent(zone.id) + '/bypass',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bypass: bypass }),
      },
    );
    renderSecurity();
    await loadSecurityEvents();
    toast(zoneLabel(zone) + (bypass ? ' bypassed' : ' active'), 'success');
  } catch (exc) {
    reportActionFailure(exc, 'Failed');
  } finally {
    bypassBusy.delete(zone.id);
    // On success renderSecurity() rebuilt the row; on error the old node stays,
    // so re-enable it explicitly.
    if (btn) btn.disabled = false;
  }
}

// The segment each panel mode selects (#879). Arming and triggered select
// none: the state line names them.
const MODE_SEGMENTS = {
  disarmed: 'disarm',
  partial: 'partial',
  perimeter: 'perimeter',
  armed: 'arm',
};
// Segment labels name the mode, so the selected one reads as the state
// ("Off"); ACTION_LABELS keeps the verbs the schedule editor shows.
const SEGMENT_LABELS = {
  disarm: 'Off',
  partial: 'Partial',
  perimeter: 'Perimeter',
  arm: 'Full',
};

// Alarm controls render into every registered container — the Security tab and
// the Home tab both show the same control (issue #72). It is one segmented
// control (#879, #888, decision 3 of #872): the selected segment is the
// current mode, raised on the card like any selected segment and never red,
// because an armed alarm is normal, not an emergency. Red is kept for a triggered alarm (the state line)
// and amber for trouble. Off is always tappable (#223); the arm modes only
// from disarmed, and the rest read as unavailable.
function renderActionsInto(el) {
  if (!el) return;
  el.innerHTML = '';
  const selected = MODE_SEGMENTS[currentMode()] || null;
  ACTIONS.forEach(function (action) {
    const available = actionAvailable(action);
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'segmented-item security-action security-action-' + action;
    btn.textContent = SEGMENT_LABELS[action];
    btn.setAttribute('aria-pressed', action === selected ? 'true' : 'false');
    btn.disabled = !available || actionBusy;
    if (actionBusy) {
      btn.title = 'Working…';
    } else if (btn.disabled) {
      btn.title = currentMode() === 'unknown' ? 'State unavailable' : 'Unavailable in current state';
    }
    if (available) {
      btn.addEventListener('click', function () { postAction(action); });
    }
    el.appendChild(btn);
  });
}

export function renderActions() {
  renderActionsInto(els.securityActions);
  renderActionsInto(els.homeSecurityActions);
}

// The Security glance card's state line (#882): a shield glyph and the mode
// word, with the system exceptions as chips after it. "Alarm state:" stays
// for assistive tech only; on the card the word and the selected segment
// already say it. The trouble chip opens the detector.
function renderSecurityState() {
  const el = els.securityState;
  if (!el) return;
  const security = state.security;
  const mode = security ? currentMode() : 'unknown';
  const label = security ? displayLabel() : '—';
  el.className = 'security-state ' + statusClass(mode);
  el.innerHTML = icon('shield-check', 'security-state-icon');
  const prefix = document.createElement('span');
  prefix.className = 'visually-hidden';
  prefix.textContent = 'Alarm state: ';
  el.appendChild(prefix);
  const word = document.createElement('span');
  word.className = 'security-state-word';
  word.textContent = label;
  el.appendChild(word);
  // System-wide AC-power-lost alert (issue #99): an aggregate cloud flag,
  // clearing when false. Attention, not danger: the panel is on its backup
  // battery and still protecting (#879).
  if (security && security.ac_lost) {
    const badge = chipEl('AC power lost', 'attention', 'security-aclost-badge');
    badge.title = 'The alarm panel lost mains power and is running on backup battery';
    el.appendChild(badge);
  }
  // Detector-trouble roll-up (issue #225): count detectors reporting trouble that
  // the user hasn't ignored, so an un-ignored trouble is visible on the main card.
  // Ignored ones (a known/accepted trouble) don't contribute, keeping it quiet.
  const troubled = troubledZones();
  if (security && troubled.length > 0) {
    // A chip that opens something keeps its status colours on the chip
    // shape (design.md reference pill): one detector opens its sheet, more
    // show them at the top of the Detectors group.
    const badge = document.createElement('button');
    badge.type = 'button';
    badge.className = 'chip chip-button security-trouble-badge';
    badge.dataset.tone = 'attention';
    badge.textContent = troubled.length + ' trouble';
    badge.setAttribute('aria-label', troubleLabel(troubled));
    badge.addEventListener('click', function () { showTroubled(badge); });
    el.appendChild(badge);
  }
}

function troubleLabel(troubled) {
  return troubled.length === 1
    ? 'Trouble: open ' + zoneLabel(troubled[0])
    : troubled.length + ' detectors report trouble: show them';
}

// The alarm's exceptions, most urgent first: a triggered alarm (danger),
// detector trouble the user hasn't ignored and AC power lost (attention).
// The Security header, the Security tab badge and Home's House card all say
// these and nothing else; an armed mode is normal and adds nothing
// (decision 3 of #872).
function alarmExceptions(security) {
  const exceptions = [];
  if (currentMode() === 'triggered') exceptions.push({ text: 'Triggered', tone: 'danger' });
  const troubled = troubledZones();
  if (troubled.length) {
    exceptions.push({ text: troubled.length + ' trouble', tone: 'attention', troubled: troubled });
  }
  if (security.ac_lost) exceptions.push({ text: 'AC power lost', tone: 'attention' });
  return exceptions;
}

// Detectors reporting trouble that the user hasn't ignored (issue #225).
function troubledZones() {
  const zones = (state.security && state.security.zones) || [];
  return zones.filter(function (z) { return z.trouble && !z.trouble_ignored; });
}

function showTroubled(trigger) {
  const troubled = troubledZones();
  if (troubled.length === 1) {
    openZoneDetail(troubled[0].id, trigger);
    return;
  }
  // Alerts already sort first; clear a filter that could hide them.
  if (els.securityZoneFilter) els.securityZoneFilter.value = '';
  renderZones();
  if (els.securityZonesCard) els.securityZonesCard.scrollIntoView({ block: 'start', behavior: 'smooth' });
  const first = els.securityZones && els.securityZones.querySelector('.security-zone.is-alert .action-row-main');
  if (first) first.focus({ preventScroll: true });
}

export function renderState() {
  renderSecurityState();
  renderSecurityHead();
  renderHouseAlarm();
}

// The Security header's live line and the Security tab's count badge
// (head-status.js, #880; decision 10 of #872): the alarm's exceptions, else
// the plain detector count. The badge carries the most urgent of a triggered
// alarm or detector trouble, so it is seen from every other tab.
function renderSecurityHead() {
  const security = state.security;
  if (!security) {
    setHeadPart('security', 'alarm', null);
    setTabBadge('security', 0);
    return;
  }
  if (security.reachable === false) {
    setHeadPart('security', 'alarm', {
      exceptions: [{ text: 'Panel unreachable', tone: 'attention' }],
    });
    setTabBadge('security', 0);
    return;
  }
  const exceptions = alarmExceptions(security);
  const detectors = (security.zones || []).filter(function (z) { return !z.hidden; }).length;
  setHeadPart('security', 'alarm', {
    exceptions: exceptions,
    fact: detectors + (detectors === 1 ? ' detector' : ' detectors'),
  });
  const troubled = troubledZones().length;
  if (currentMode() === 'triggered') setTabBadge('security', 1, 'triggered', 'danger');
  else setTabBadge('security', troubled, 'trouble', 'attention');
}

// Home's House card (#885): the arm control (renderActions) shows the mode,
// so the card's head carries only the alarm's exceptions, plus an arming
// panel (accent, in progress), each a chip opening the Security tab; detector
// trouble opens the detectors there. The mode in words stays for assistive
// tech, which reads the pressed segment too.
function renderHouseAlarm() {
  const security = state.security;
  if (els.homeSecurityState) {
    els.homeSecurityState.textContent = 'Alarm state: ' + (security ? displayLabel() : '—');
  }
  if (!security) {
    setHousePart('alarm', null);
    return;
  }
  const exceptions = security.reachable === false
    ? [{ text: 'Panel unreachable', tone: 'attention' }]
    : alarmExceptions(security);
  if (security.reachable !== false && currentMode() === 'arming') {
    exceptions.push({ text: 'Arming', tone: 'accent' });
  }
  setHousePart('alarm', exceptions.map(function (x) {
    return {
      text: x.text,
      tone: x.tone,
      label: x.troubled ? troubleLabel(x.troubled) : x.text + ': open Security',
      onOpen: function (btn) {
        showTab('security');
        if (x.troubled) showTroubled(btn);
      },
    };
  }));
}

// The Recent events group (decision 8 of #872, #882): the last three on the
// shared row, time-led (design.md action-row: a tabular time is a leading
// slot), what happened as the title and who as the one meta line. The whole
// history is the activity log, opened filtered to the alarm.
const RECENT_EVENTS = 3;

// The panel names events in capitals ("SYSTEM ARMED"); a row title is
// sentence case. Mixed-case names are left as the panel wrote them.
function eventTitle(event) {
  const text = String(event.name || event.type || event.category || event.text || 'Event');
  return text === text.toUpperCase() && /[A-Z]/.test(text)
    ? text.charAt(0) + text.slice(1).toLowerCase()
    : text;
}

function sameDay(a, b) {
  return a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate();
}

// The row's time slot and the day it belongs to when that is not today.
function eventWhen(value) {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return { time: '—', day: '' };
  const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const now = new Date();
  if (sameDay(date, now)) return { time: time, day: '' };
  const yesterday = new Date(now);
  yesterday.setDate(now.getDate() - 1);
  if (sameDay(date, yesterday)) return { time: time, day: 'Yesterday' };
  return { time: time, day: date.toLocaleDateString([], { day: 'numeric', month: 'short' }) };
}

function eventRow(event) {
  const when = eventWhen(event.time);
  // Who acted, as words (#362: no actor means no badge, never a bare "-").
  const actor = event.user_id ? 'User ' + event.user_id : '';
  const meta = [when.day, actor].filter(Boolean).join(' · ');
  return rowEl({
    className: 'security-event',
    time: when.time,
    title: eventTitle(event),
    meta: meta || null,
  });
}

export function renderEvents() {
  els.securityEvents.innerHTML = '';
  const events = state.securityEvents || [];
  if (!events.length) {
    els.securityEventsNote.hidden = false;
    els.securityEventsNote.textContent = 'No recent events.';
    return;
  }
  els.securityEventsNote.hidden = true;
  const list = document.createElement('ul');
  list.className = 'action-rows';
  events.slice(0, RECENT_EVENTS).forEach(function (event) { list.appendChild(eventRow(event)); });
  els.securityEvents.appendChild(list);
}

// A detector's exception chips (#879): an active detector is the normal state
// and shows none (its switch already says it). Triggered is danger, an
// un-ignored trouble attention (#104), and bypassed or an ignored trouble a
// neutral fact kept off the main-card count (#225).
function renderZoneFlags(zone) {
  const flags = document.createElement('span');
  flags.className = 'security-zone-flags';
  if (zone.triggered) flags.appendChild(chipEl('Triggered', 'danger'));
  if (zone.bypassed) flags.appendChild(chipEl('Bypassed'));
  if (zone.trouble) {
    flags.appendChild(zone.trouble_ignored
      ? chipEl('Trouble ignored')
      : chipEl('Trouble', 'attention'));
  }
  return flags.childNodes.length ? flags : null;
}

// The Detectors group (#882): the shared row, a filter once the list is long
// (design.md action-row: a list that can exceed ~12 rows), and only the first
// few until "Show all", so the groups below stay within reach. A detector that
// needs you (triggered, or trouble not ignored) sorts first so it is never
// folded away; every other detector keeps its A–Z place, so toggling one
// never moves it.
const ZONES_FOLDED = 5;
const ZONES_FILTER_MIN = 12;
let zonesExpanded = false;

function zoneNeedsYou(zone) {
  return !!(zone.triggered || (zone.trouble && !zone.trouble_ignored));
}

function zoneRow(zone) {
  const active = !zone.bypassed;
  const toggle = document.createElement('button');
  toggle.type = 'button';
  toggle.className = 'toggle security-bypass' + (active ? ' on' : ' off');
  toggle.setAttribute('role', 'switch');
  toggle.setAttribute('aria-checked', active ? 'true' : 'false');
  toggle.setAttribute('aria-label', 'Detector active ' + zoneLabel(zone));
  toggle.innerHTML = toggleMarkup(active);
  toggle.addEventListener('click', function () { setBypass(zone, active, toggle); });

  const classes = ['security-zone', zone.bypassed ? 'is-bypassed' : 'is-active'];
  if (zone.triggered) classes.push('is-triggered');
  if (zoneNeedsYou(zone)) classes.push('is-alert');
  if (zone.hidden) classes.push('is-hidden');
  const row = rowEl({
    className: classes.join(' '),
    glyph: 'satellite-dish',
    title: zoneLabel(zone),
    chip: renderZoneFlags(zone),
    openLabel: zoneLabel(zone) + ': details',
    onOpen: function (btn) { openZoneDetail(zone.id, btn); },
    trail: toggle,
  });
  row.dataset.zoneId = zone.id;
  return row;
}

function renderZonesMeta(listed) {
  if (!els.securityZonesMeta) return;
  const bypassed = listed.filter(function (z) { return z.bypassed; }).length;
  els.securityZonesMeta.textContent = listed.length
    ? listed.length + (bypassed ? ' · ' + bypassed + ' bypassed' : '')
    : '';
}

export function renderZones() {
  els.securityZones.innerHTML = '';
  const zones = (state.security && state.security.zones) || [];
  const hiddenCount = zones.filter(function (z) { return z.hidden; }).length;
  if (els.securityHiddenToggle) {
    els.securityHiddenToggle.hidden = hiddenCount === 0;
    els.securityHiddenToggle.textContent = state.securityShowHidden
      ? 'Hide ' + hiddenCount + ' hidden'
      : 'Show ' + hiddenCount + ' hidden';
    els.securityHiddenToggle.setAttribute('aria-pressed', state.securityShowHidden ? 'true' : 'false');
  }
  if (!zones.length) {
    els.securityZonesNote.hidden = false;
    els.securityZonesNote.textContent = 'No detectors.';
    renderZonesMeta([]);
    if (els.securityZoneFilterField) els.securityZoneFilterField.hidden = true;
    if (els.securityZonesMore) els.securityZonesMore.hidden = true;
    return;
  }

  // Needs-you first, then A–Z by display label (mirrors the plugs list);
  // locale-aware so accented Spanish detector names sort naturally.
  const sorted = zones.slice().sort(function (a, b) {
    const alert = Number(zoneNeedsYou(b)) - Number(zoneNeedsYou(a));
    return alert || zoneLabel(a).localeCompare(zoneLabel(b), undefined, { sensitivity: 'base' });
  });
  // Hidden detectors drop out unless "show hidden" is on, where they render
  // dimmed so they can be un-hidden from the sheet (issue #104).
  const listed = state.securityShowHidden
    ? sorted
    : sorted.filter(function (z) { return !z.hidden; });
  renderZonesMeta(listed.filter(function (z) { return !z.hidden; }));

  const filterable = listed.length > ZONES_FILTER_MIN;
  if (els.securityZoneFilterField) els.securityZoneFilterField.hidden = !filterable;
  const query = filterable && els.securityZoneFilter
    ? els.securityZoneFilter.value.trim().toLocaleLowerCase()
    : '';
  const matched = query
    ? listed.filter(function (z) {
      return zoneLabel(z).toLocaleLowerCase().includes(query) ||
        String(z.name || '').toLocaleLowerCase().includes(query);
    })
    : listed;
  // Folding away a single row saves nothing; show it instead.
  const foldable = !query && matched.length > ZONES_FOLDED + 1;
  const folded = foldable && !zonesExpanded;
  const shown = folded ? matched.slice(0, ZONES_FOLDED) : matched;
  if (els.securityZonesMore) {
    els.securityZonesMore.hidden = !foldable;
    els.securityZonesMore.textContent = folded ? 'Show all ' + matched.length : 'Show fewer';
    els.securityZonesMore.setAttribute('aria-expanded', folded ? 'false' : 'true');
  }

  if (!listed.length) {
    els.securityZonesNote.hidden = false;
    els.securityZonesNote.textContent = 'All detectors hidden.';
  } else if (!matched.length) {
    els.securityZonesNote.hidden = false;
    els.securityZonesNote.textContent = 'No detector matches.';
  } else {
    els.securityZonesNote.hidden = true;
  }
  if (!shown.length) return;
  const list = document.createElement('ul');
  list.className = 'action-rows';
  shown.forEach(function (zone) { list.appendChild(zoneRow(zone)); });
  els.securityZones.appendChild(list);
}

// --------------------------------------------------- detector detail + rename
function zoneLabel(zone) {
  return zone.display_name || zone.name || ('Zone ' + zone.id);
}

function zoneById(zoneId) {
  const zones = (state.security && state.security.zones) || [];
  return zones.find(function (z) { return z.id === zoneId; }) || null;
}

function openZoneDetail(zoneId, trigger) {
  if (!zoneById(zoneId)) return;
  state.selectedZoneId = zoneId;
  zoneModal.open(zoneId, trigger);
}

function renderZoneHiddenToggle(zone) {
  const btn = els.zoneHiddenToggle;
  if (!btn) return;
  const hidden = !!zone.hidden;
  btn.className = 'toggle' + (hidden ? ' on' : ' off');
  btn.setAttribute('aria-checked', hidden ? 'true' : 'false');
  btn.innerHTML = toggleMarkup(hidden);
}

function renderZoneTroubleIgnoreToggle(zone) {
  const btn = els.zoneTroubleIgnoreToggle;
  if (!btn) return;
  const ignored = !!zone.trouble_ignored;
  btn.className = 'toggle' + (ignored ? ' on' : ' off');
  btn.setAttribute('aria-checked', ignored ? 'true' : 'false');
  btn.innerHTML = toggleMarkup(ignored);
}

// Detail-modal staging (#203, shared shell #699): name + Hidden commit on
// Save. zoneModal.staged holds the working Hidden/Ignore-trouble state
// captured when the modal opens; closing discards it.
function patchZone(id, patch) {
  if (state.security && Array.isArray(state.security.zones)) {
    state.security.zones = state.security.zones.map(function (z) {
      return z.id === id ? Object.assign({}, z, patch) : z;
    });
  }
}

const zoneModal = detailModal({
  dialog: els.zoneDialog,
  closeButton: els.zoneDetailClose,
  onClose: function () { state.selectedZoneId = null; },
  saveButton: els.zoneSave,
  focusEl: els.zoneDisplayName,
  getEntity: zoneById,
  stage: function (zone) {
    return { hidden: !!zone.hidden, trouble_ignored: !!zone.trouble_ignored };
  },
  populate: function (staged, zone) {
    els.zoneDetailName.textContent = zoneLabel(zone);
    els.zoneDetailType.textContent = zone.type === null || zone.type === undefined
      ? '—' : ('Type ' + zone.type);
    els.zoneDetailStatus.textContent = zone.triggered
      ? 'Triggered' : (zone.bypassed ? 'Bypassed' : 'Active');
    els.zoneDetailTrouble.innerHTML = zone.trouble ? icon('triangle-alert') + ' Yes' : 'No';
    els.zoneDisplayName.value = zone.display_name || '';
    els.zoneDisplayName.placeholder = zone.name || 'Custom label…';
    // Original RISCO name, so the custom label maps back to the physical detector.
    if (els.zoneOriginalName) {
      els.zoneOriginalName.textContent = 'System name: ' + (zone.name || ('Zone ' + zone.id));
    }
    renderZoneHiddenToggle(staged);
    renderZoneTroubleIgnoreToggle(staged);
  },
  buildOps: function (id, staged, zone) {
    const ops = [];
    const newName = els.zoneDisplayName.value.trim();
    if ((zone.display_name || '') !== newName) {
      ops.push(jsonApi('/api/security/zones/' + encodeURIComponent(id) + '/display_name', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ display_name: newName }),
      }).then(function () { patchZone(id, { display_name: newName || null }); }));
    }
    if (!!zone.hidden !== staged.hidden) {
      ops.push(jsonApi('/api/security/zones/' + encodeURIComponent(id) + '/hidden', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hidden: staged.hidden }),
      }).then(function () { patchZone(id, { hidden: staged.hidden }); }));
    }
    if (!!zone.trouble_ignored !== staged.trouble_ignored) {
      ops.push(jsonApi('/api/security/zones/' + encodeURIComponent(id) + '/trouble_ignored', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ ignored: staged.trouble_ignored }),
      }).then(function () { patchZone(id, { trouble_ignored: staged.trouble_ignored }); }));
    }
    return ops;
  },
  afterSave: function (id) {
    const z = zoneById(id);
    if (z) els.zoneDetailName.textContent = zoneLabel(z);
    renderState();  // refresh the main-card trouble count after an ignore change (#225)
  },
  render: renderZones,
});

// Stage the Hidden toggle visually only — the PUT happens on Save.
function toggleZoneHidden() {
  if (!zoneModal.staged) return;
  zoneModal.staged.hidden = !zoneModal.staged.hidden;
  renderZoneHiddenToggle(zoneModal.staged);
  zoneModal.markDirty();
}

// Stage the Ignore-trouble toggle visually only — the PUT happens on Save (#225).
function toggleZoneTroubleIgnored() {
  if (!zoneModal.staged) return;
  zoneModal.staged.trouble_ignored = !zoneModal.staged.trouble_ignored;
  renderZoneTroubleIgnoreToggle(zoneModal.staged);
  zoneModal.markDirty();
}

async function loadSecurityEvents() {
  const body = await jsonApi('/api/security/events?count=50');
  state.securityEvents = (body && body.events) || [];
  renderEvents();
}

// Wire the detector detail/rename modal once at boot (mirrors wirePlugDetail).
export function wireZoneDetail() {
  els.zoneDisplayName.addEventListener('input', zoneModal.markDirty);
  els.zoneDisplayName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); zoneModal.save(); }
  });
  if (els.zoneHiddenToggle) {
    els.zoneHiddenToggle.addEventListener('click', toggleZoneHidden);
  }
  if (els.zoneTroubleIgnoreToggle) {
    els.zoneTroubleIgnoreToggle.addEventListener('click', toggleZoneTroubleIgnored);
  }
  if (els.zoneSave) els.zoneSave.addEventListener('click', zoneModal.save);
}

// Wire the Detectors group's controls: the "show hidden" toggle (issue #104),
// the filter and Show all (#882), and Recent events' All events, which opens
// the activity log filtered to the alarm (decision 8 of #872).
export function wireSecurityHiddenToggle() {
  state.securityShowHidden = showHiddenPref.read();

  if (els.securityHiddenToggle) {
    els.securityHiddenToggle.addEventListener('click', function () {
      state.securityShowHidden = !state.securityShowHidden;
      showHiddenPref.write(state.securityShowHidden);
      renderZones();
    });
  }
  if (els.securityZoneFilter) els.securityZoneFilter.addEventListener('input', renderZones);
  if (els.securityZonesMore) {
    els.securityZonesMore.addEventListener('click', function () {
      zonesExpanded = !zonesExpanded;
      renderZones();
    });
  }
  if (els.securityEventsAll) {
    els.securityEventsAll.addEventListener('click', function () {
      openActivity({ domain: 'security', trigger: els.securityEventsAll });
    });
  }
}
