/* Daily blind up/down schedule editor (issue #871) and the alarm-pairing
 * switch (issue #875).
 *
 * Lives in the Devices tab's Blinds card, under the group row and the blind
 * rows. The same shape as the alarm-schedule editor (security-schedules.js):
 * summary rows + a staged edit dialog through the shared denseListEditor,
 * persisted as one list via GET/PUT /api/blinds/schedules. The server-side
 * engine (app/webapp/blind_schedules.py) fires due entries through the same
 * parallel group move as the card's All up / All down buttons.
 *
 * Each entry adds two things to the alarm schedule's time + days + action:
 * which blinds it moves (none picked = every blind) and a presence condition
 * judged at fire time (always / someone home / nobody home).
 */

'use strict';

import { state, els, toast } from './state.js';
import { jsonApi, isAuthRequired, reportActionFailure } from './api.js';
import { isToggleOn, setToggleState, wireToggle } from './toggle.js';
import { denseListEditor, renderSummaryRow } from './dense-editor.js';

const DAYS = [
  ['mon', 'Mon'],
  ['tue', 'Tue'],
  ['wed', 'Wed'],
  ['thu', 'Thu'],
  ['fri', 'Fri'],
  ['sat', 'Sat'],
  ['sun', 'Sun'],
];
const ALL_DAYS = DAYS.map(function (day) { return day[0]; });
const ACTION_LABELS = { open: 'Up', close: 'Down' };
const PRESENCE_LABELS = { any: '', home: 'Someone home', away: 'Nobody home' };

function scheduleDefaults() {
  return {
    id: 'blind-schedule-' + Date.now().toString(36),
    enabled: true,
    time: '08:00',
    days: ALL_DAYS.slice(),
    action: 'open',
    targets: [],
    presence: 'any',
  };
}

function normalizedSchedules(entries) {
  return (entries || state.blindSchedules || []).map(function (entry, idx) {
    const days = Array.isArray(entry.days)
      ? ALL_DAYS.filter(function (day) { return entry.days.includes(day); })
      : [];
    return {
      id: entry.id || ('blind-schedule-' + (idx + 1)),
      enabled: entry.enabled !== false,
      time: entry.time || '08:00',
      days: days.length ? days : ALL_DAYS.slice(),
      action: ACTION_LABELS[entry.action] ? entry.action : 'open',
      targets: Array.isArray(entry.targets) ? entry.targets.slice() : [],
      presence: Object.prototype.hasOwnProperty.call(PRESENCE_LABELS, entry.presence)
        ? entry.presence : 'any',
    };
  });
}

// The blinds known to the Blinds card (GET /api/tuya, loaded by plugs.js on
// the same tab), in the card's own name order.
function knownBlinds() {
  return state.plugs
    .filter(function (d) { return d.has_cover === true; })
    .map(function (d) { return { id: d.device_id, name: d.display_name || d.name || d.device_id }; })
    .sort(function (a, b) { return a.name.localeCompare(b.name); });
}

function blindName(id) {
  const blind = knownBlinds().find(function (b) { return b.id === id; });
  return blind ? blind.name : 'Removed blind';
}

function daysSummary(days) {
  if (days.length === 7) return 'Every day';
  if (days.join(',') === 'mon,tue,wed,thu,fri') return 'Weekdays';
  if (days.join(',') === 'sat,sun') return 'Weekends';
  return DAYS.filter(function (day) { return days.includes(day[0]); })
    .map(function (day) { return day[1]; }).join(', ');
}

function targetsSummary(targets) {
  if (!targets.length) return 'All blinds';
  if (targets.length === 1) return blindName(targets[0]);
  return targets.length + ' blinds';
}

function entryMeta(entry) {
  return [ACTION_LABELS[entry.action], daysSummary(entry.days), PRESENCE_LABELS[entry.presence],
    targetsSummary(entry.targets)].filter(Boolean).join(' · ');
}

function chip(text, active, onClick) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'alarm-schedule-day' + (active ? ' active' : '');
  btn.textContent = text;
  btn.setAttribute('aria-pressed', active ? 'true' : 'false');
  btn.addEventListener('click', onClick);
  return btn;
}

function renderEditorDays() {
  const staged = scheduleEditor.staged;
  if (!els.blindScheduleDays || !staged) return;
  els.blindScheduleDays.innerHTML = '';
  DAYS.forEach(function (day) {
    els.blindScheduleDays.appendChild(chip(day[1], staged.days.includes(day[0]), function () {
      const current = staged.days.slice();
      const pos = current.indexOf(day[0]);
      if (pos >= 0 && current.length > 1) current.splice(pos, 1);
      else if (pos < 0) current.push(day[0]);
      staged.days = ALL_DAYS.filter(function (value) { return current.includes(value); });
      renderEditorDays();
    }));
  });
}

// "All blinds" is the empty target list; picking a blind narrows it, and
// un-picking the last one falls back to all. A target no longer in
// devices.json still shows (pressed) so it can be removed.
function renderEditorTargets() {
  const staged = scheduleEditor.staged;
  if (!els.blindScheduleTargets || !staged) return;
  els.blindScheduleTargets.innerHTML = '';
  els.blindScheduleTargets.appendChild(chip('All blinds', !staged.targets.length, function () {
    staged.targets = [];
    renderEditorTargets();
  }));
  const known = knownBlinds();
  const ids = known.map(function (b) { return b.id; });
  const options = known.concat(staged.targets
    .filter(function (id) { return !ids.includes(id); })
    .map(function (id) { return { id: id, name: 'Removed blind' }; }));
  options.forEach(function (blind) {
    els.blindScheduleTargets.appendChild(chip(blind.name, staged.targets.includes(blind.id), function () {
      const pos = staged.targets.indexOf(blind.id);
      if (pos >= 0) staged.targets.splice(pos, 1);
      else staged.targets.push(blind.id);
      renderEditorTargets();
    }));
  });
}

const scheduleEditor = denseListEditor({
  dialog: els.blindScheduleDialog,
  addButton: els.blindScheduleAdd,
  closeButton: els.blindScheduleEditorClose,
  saveButton: els.blindScheduleSave,
  deleteButton: els.blindScheduleDelete,
  titleEl: els.blindScheduleEditorTitle,
  listEl: els.blindSchedules,
  focusEl: els.blindScheduleTime,
  rowIdAttr: 'data-blind-schedule-id',
  titles: { add: 'Add schedule', edit: 'Edit schedule' },
  deleteConfirm: {
    title: 'Delete this blind schedule?',
    message: 'This schedule will be removed permanently.',
  },
  toasts: { saved: 'Schedules saved', failed: "Couldn't save schedules" },
  defaults: scheduleDefaults,
  stage: function (source) {
    return Object.assign({}, source, { days: source.days.slice(), targets: source.targets.slice() });
  },
  getEntries: function () { return state.blindSchedules; },
  setEntries: function (entries) { state.blindSchedules = entries; },
  normalize: normalizedSchedules,
  render: renderBlindSchedules,
  populate: function (staged) {
    setToggleState(els.blindScheduleEnabled, staged.enabled);
    els.blindScheduleTime.value = staged.time;
    els.blindScheduleAction.value = staged.action;
    els.blindSchedulePresence.value = staged.presence;
    renderEditorDays();
    renderEditorTargets();
  },
  collect: function (staged) {
    staged.enabled = isToggleOn(els.blindScheduleEnabled);
    staged.time = els.blindScheduleTime.value || '08:00';
    staged.action = ACTION_LABELS[els.blindScheduleAction.value] ? els.blindScheduleAction.value : 'open';
    staged.presence = Object.prototype.hasOwnProperty.call(PRESENCE_LABELS, els.blindSchedulePresence.value)
      ? els.blindSchedulePresence.value : 'any';
  },
  endpoint: '/api/blinds/schedules',
  bodyKey: 'entries',
});

export function renderBlindSchedules() {
  if (!els.blindSchedules || !els.blindSchedulesNote) return;
  els.blindSchedules.innerHTML = '';
  state.blindSchedules = normalizedSchedules();
  if (!state.blindSchedules.length) {
    els.blindSchedulesNote.hidden = false;
    els.blindSchedulesNote.textContent = 'No blind schedules.';
    return;
  }
  els.blindSchedulesNote.hidden = true;
  const sorted = state.blindSchedules
    .map(function (entry, idx) { return { entry: entry, idx: idx }; })
    .sort(function (a, b) { return a.entry.time.localeCompare(b.entry.time); });
  sorted.forEach(function (item) {
    const entry = item.entry;
    els.blindSchedules.appendChild(renderSummaryRow({
      id: entry.id,
      idAttr: 'blindScheduleId',
      title: entry.time,
      meta: entryMeta(entry),
      openLabel: 'Edit blind schedule at ' + entry.time,
      onOpen: function (main) { scheduleEditor.open(item.idx, main); },
      toggleName: 'blind-schedule-enabled',
      toggleOn: entry.enabled,
      toggleLabel: 'Enable blind schedule at ' + entry.time,
      onToggle: function (on) {
        scheduleEditor.save(state.blindSchedules.map(function (schedule, index) {
          return index === item.idx ? Object.assign({}, schedule, { enabled: on }) : schedule;
        }));
      },
    }));
  });
}

// The "Follow the automatic alarm" switch (#875): read on entry, written on
// tap, rolled back when the save fails so the switch never shows a state the
// server does not hold.
async function loadAlarmPairing() {
  if (!els.blindsFollowAlarm) return;
  try {
    const body = await jsonApi('/api/blinds/alarm-pairing');
    setToggleState(els.blindsFollowAlarm, !!(body && body.follow_alarm));
  } catch (exc) {
    if (!isAuthRequired(exc)) reportActionFailure(exc, "Couldn't read the alarm pairing");
  }
}

async function saveAlarmPairing(on) {
  try {
    const body = await jsonApi('/api/blinds/alarm-pairing', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ follow_alarm: on }),
    });
    setToggleState(els.blindsFollowAlarm, !!(body && body.follow_alarm));
    toast(on ? 'Blinds follow the alarm' : 'Blinds no longer follow the alarm', 'success');
  } catch (exc) {
    setToggleState(els.blindsFollowAlarm, !on);
    reportActionFailure(exc, "Couldn't save the alarm pairing");
  }
}

export async function loadBlindSchedules() {
  if (!els.blindSchedules) return;
  try {
    const body = await jsonApi('/api/blinds/schedules');
    state.blindSchedules = (body && body.entries) || [];
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    state.blindSchedules = [];
    if (els.blindSchedulesNote) {
      els.blindSchedulesNote.hidden = false;
      els.blindSchedulesNote.textContent = exc.message || 'Failed to load blind schedules.';
    }
    return;
  }
  renderBlindSchedules();
}

// The list changes only through this editor, so one read per visit to the
// Devices tab is enough — no polling.
export function onBlindSchedulesTab(tab) {
  if (tab !== 'iot') return;
  loadBlindSchedules();
  loadAlarmPairing();
}

export function wireBlindSchedules() {
  if (!els.blindScheduleAdd || !els.blindScheduleDialog) return;
  wireToggle(els.blindScheduleEnabled, function (on) {
    if (scheduleEditor.staged) scheduleEditor.staged.enabled = on;
  });
  wireToggle(els.blindsFollowAlarm, saveAlarmPairing);
  // Blind names come from GET /api/tuya; re-render once they land so a
  // single-blind row shows the blind's name rather than "Removed blind".
  // Only then: a re-render on every 15 s Plugs poll would rebuild rows under
  // the user's finger.
  document.addEventListener('plugs:rendered', function () {
    if (state.blindSchedules.some(function (e) { return e.targets.length === 1; })) {
      renderBlindSchedules();
    }
  });
  scheduleEditor.wire();
}
