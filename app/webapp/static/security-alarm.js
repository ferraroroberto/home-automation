/* Alarm state + detectors controller (split out of security.js, issue #197).
 *
 * Owns the alarm state line, the action pills (disarm/partial/perimeter/arm),
 * the recent-events list, and the detector list with per-zone detail/rename
 * modals. State writes are one-tap POST/PUT calls that re-render from the
 * returned live state; the top-level redraw is delegated back to the boot
 * module's renderSecurity().
 *
 * ACTIONS / ACTION_LABELS are exported because the schedule editor reuses the
 * same action set. fmtTime is imported from presence.js (its busiest consumer)
 * rather than duplicated.
 */

'use strict';

import { state, els, toast, persistedFlag, SECURITY_SHOW_HIDDEN_KEY } from './state.js';
import { jsonApi, reportActionFailure } from './api.js';
import { fmtTime } from './presence.js';
import { renderSecurity } from './security.js';
import { toggleMarkup } from './toggle.js';
import { icon } from './_vendored/icons/icons.js';
import { detailModal } from './detail-modal.js';
import { chipEl } from './chip.js';

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
// control (#879, decision 3 of #872): the selected segment is the current
// mode, in the accent like any selected state, because an armed alarm is
// normal, not an emergency. Red is kept for a triggered alarm (the state line)
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

function renderStateInto(el) {
  if (!el) return;
  const security = state.security;
  const mode = security ? currentMode() : 'unknown';
  const label = security ? displayLabel() : '—';
  el.className = 'security-state ' + statusClass(mode);
  el.innerHTML = '';
  const prefix = document.createElement('span');
  prefix.textContent = 'Alarm state:';
  el.appendChild(prefix);
  const word = document.createElement('span');
  word.className = 'security-state-word';
  word.textContent = label;
  el.appendChild(word);
  // System-wide AC-power-lost alert (issue #99): an aggregate cloud flag,
  // dual-rendered onto Home + Security, clearing when false. Attention, not
  // danger: the panel is on its backup battery and still protecting (#879).
  if (security && security.ac_lost) {
    const badge = chipEl('AC power lost', 'attention', 'security-aclost-badge');
    badge.title = 'The alarm panel lost mains power and is running on backup battery';
    el.appendChild(badge);
  }
  // Detector-trouble roll-up (issue #225): count detectors reporting trouble that
  // the user hasn't ignored, so an un-ignored trouble is visible on the main card.
  // Ignored ones (a known/accepted trouble) don't contribute, keeping it quiet.
  const troubled = troubledNotIgnoredCount();
  if (security && troubled > 0) {
    const badge = chipEl(troubled + ' trouble', 'attention', 'security-trouble-badge');
    badge.title = troubled + ' detector(s) reporting trouble — see the Detectors list';
    el.appendChild(badge);
  }
}

// Detectors reporting trouble that the user hasn't ignored (issue #225).
function troubledNotIgnoredCount() {
  const zones = (state.security && state.security.zones) || [];
  return zones.filter(function (z) { return z.trouble && !z.trouble_ignored; }).length;
}

export function renderState() {
  renderStateInto(els.securityState);
  renderStateInto(els.homeSecurityState);
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

  const hasActor = events.some(function (event) {
    return event.user_id !== null && event.user_id !== undefined && event.user_id !== '' && event.user_id !== 0;
  });

  events.slice(0, 20).forEach(function (event) {
    const row = document.createElement('div');
    row.className = 'security-event';

    // Lead with what happened, then when (#805, J-10): event · actor · time.
    const body = document.createElement('span');
    body.className = 'security-event-body';
    body.textContent = event.name || event.type || event.category || event.text || 'Event';
    row.appendChild(body);

    // Render nothing for events with no actor — a literal "-" badge next to a
    // real "U1"/"U3" one reads as broken data, not "no user" (issue #362).
    if (hasActor && event.user_id) {
      const actor = document.createElement('span');
      actor.className = 'security-event-actor';
      actor.textContent = 'U' + event.user_id;
      row.appendChild(actor);
    }

    const time = document.createElement('span');
    time.className = 'security-event-time';
    time.textContent = fmtTime(event.time);
    row.appendChild(time);

    els.securityEvents.appendChild(row);
  });
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
  return flags;
}

export function renderZones() {
  els.securityZones.innerHTML = '';
  const zones = (state.security && state.security.zones) || [];
  if (!zones.length) {
    els.securityZonesNote.hidden = false;
    els.securityZonesNote.textContent = 'No detectors.';
    if (els.securityHiddenCount) els.securityHiddenCount.hidden = true;
    if (els.securityHiddenToggle) els.securityHiddenToggle.hidden = true;
    return;
  }
  els.securityZonesNote.hidden = true;

  // A–Z by display label (mirrors the plugs list); locale-aware so accented
  // Spanish detector names sort naturally.
  const sorted = zones.slice().sort(function (a, b) {
    return zoneLabel(a).localeCompare(zoneLabel(b), undefined, { sensitivity: 'base' });
  });

  // Hidden detectors drop out unless "show hidden" is on, where they render
  // dimmed so they can be un-hidden from the modal (issue #104).
  const hiddenCount = sorted.filter(function (z) { return z.hidden; }).length;
  const visible = state.securityShowHidden
    ? sorted
    : sorted.filter(function (z) { return !z.hidden; });

  if (els.securityHiddenCount) {
    if (hiddenCount > 0) {
      els.securityHiddenCount.textContent = hiddenCount + ' hidden';
      els.securityHiddenCount.hidden = false;
    } else {
      els.securityHiddenCount.hidden = true;
    }
  }
  if (els.securityHiddenToggle) {
    els.securityHiddenToggle.hidden = hiddenCount === 0;
    els.securityHiddenToggle.textContent = state.securityShowHidden ? 'Hide' : 'Show hidden';
    els.securityHiddenToggle.classList.toggle('active', state.securityShowHidden);
  }

  if (!visible.length) {
    els.securityZonesNote.hidden = false;
    els.securityZonesNote.textContent = 'All detectors hidden.';
  }

  visible.forEach(function (zone) {
    const row = document.createElement('div');
    row.className = 'security-zone';
    if (zone.triggered) row.classList.add('is-triggered');
    if (zone.bypassed) row.classList.add('is-bypassed');
    else row.classList.add('is-active');
    if (zone.hidden) row.classList.add('is-hidden');

    const main = document.createElement('div');
    main.className = 'security-zone-main';

    // The name opens the detector detail/rename modal (mirrors the AC/plug card
    // header). A button keeps it keyboard-reachable without nesting interactive
    // controls inside the bypass toggle.
    const name = document.createElement('button');
    name.type = 'button';
    name.className = 'security-zone-name';
    name.textContent = zoneLabel(zone);
    name.title = 'Detector details · rename';
    name.addEventListener('click', function () { openZoneDetail(zone.id); });
    main.appendChild(name);

    main.appendChild(renderZoneFlags(zone));
    row.appendChild(main);

    const toggle = document.createElement('button');
    toggle.type = 'button';
    const active = !zone.bypassed;
    toggle.className = 'toggle security-bypass' + (active ? ' on' : ' off');
    toggle.setAttribute('role', 'switch');
    toggle.setAttribute('aria-checked', active ? 'true' : 'false');
    toggle.setAttribute('aria-label', 'Detector active ' + zoneLabel(zone));
    toggle.innerHTML = toggleMarkup(active);
    toggle.addEventListener('click', function () { setBypass(zone, active, toggle); });
    row.appendChild(toggle);

    els.securityZones.appendChild(row);
  });
}

// --------------------------------------------------- detector detail + rename
function zoneLabel(zone) {
  return zone.display_name || zone.name || ('Zone ' + zone.id);
}

function zoneById(zoneId) {
  const zones = (state.security && state.security.zones) || [];
  return zones.find(function (z) { return z.id === zoneId; }) || null;
}

function openZoneDetail(zoneId) {
  if (!zoneById(zoneId)) return;
  state.selectedZoneId = zoneId;
  zoneModal.open(zoneId);
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

function closeZoneDetail() {
  state.selectedZoneId = null;
  zoneModal.close();
}

async function loadSecurityEvents() {
  const body = await jsonApi('/api/security/events?count=50');
  state.securityEvents = (body && body.events) || [];
  renderEvents();
}

// Wire the detector detail/rename modal once at boot (mirrors wirePlugDetail).
export function wireZoneDetail() {
  els.zoneDetailClose.addEventListener('click', closeZoneDetail);
  els.zoneDialog.addEventListener('click', function (ev) {
    if (ev.target === els.zoneDialog) closeZoneDetail();  // backdrop click
  });
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

// Wire the "show hidden" detectors toggle (issue #104) — in the card body's
// toolbar since #779, so its click no longer needs keeping off the <summary>.
export function wireSecurityHiddenToggle() {
  state.securityShowHidden = showHiddenPref.read();

  if (!els.securityHiddenToggle) return;
  els.securityHiddenToggle.addEventListener('click', function () {
    state.securityShowHidden = !state.securityShowHidden;
    showHiddenPref.write(state.securityShowHidden);
    renderZones();
  });
}
