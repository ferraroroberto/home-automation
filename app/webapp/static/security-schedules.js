/* Weekly alarm-schedule editor (split out of security.js, issue #197).
 *
 * Owns the DAYS-based schedule CRUD: load/normalise/render the schedule rows
 * (in the Automations › Schedules sheet since #882, plus the glance card's
 * "Next:" line) and persist edits through GET/PUT /api/security/schedules. The action set
 * (ACTIONS / ACTION_LABELS) is owned by the alarm module and imported here so
 * the schedule's action dropdown stays in lockstep with the alarm pills.
 */

'use strict';

import { state, els } from './state.js';
import { jsonApi, isAuthRequired } from './api.js';
import { ACTIONS, ACTION_LABELS } from './security-alarm.js';
import { isToggleOn, setToggleState, wireToggle } from './toggle.js';
import { denseListEditor, renderSummaryRow } from './dense-editor.js';
import { friendlyError } from './format.js';

const DAYS = [
  ['mon', 'Mon'],
  ['tue', 'Tue'],
  ['wed', 'Wed'],
  ['thu', 'Thu'],
  ['fri', 'Fri'],
  ['sat', 'Sat'],
  ['sun', 'Sun'],
];

function scheduleDefaults() {
  return {
    id: 'schedule-' + Date.now().toString(36),
    enabled: true,
    time: '21:00',
    days: ['mon', 'tue', 'wed', 'thu', 'fri'],
    action: 'arm',
  };
}

function normalizedSchedules(entries) {
  return (entries || state.securitySchedules || []).map(function (entry, idx) {
    const days = Array.isArray(entry.days) && entry.days.length
      ? entry.days.filter(function (day) { return DAYS.some(function (d) { return d[0] === day; }); })
      : DAYS.map(function (day) { return day[0]; });
    return {
      id: entry.id || ('schedule-' + (idx + 1)),
      enabled: entry.enabled !== false,
      time: entry.time || '21:00',
      days: days.length ? days : DAYS.map(function (day) { return day[0]; }),
      action: ACTIONS.includes(entry.action) ? entry.action : 'arm',
    };
  });
}

// The Automations row's value (#882): how many are on, else "None".
function renderScheduleCount() {
  if (!els.securitySchedulesCount) return;
  const enabled = (state.securitySchedules || []).filter(function (entry) { return entry.enabled !== false; }).length;
  els.securitySchedulesCount.textContent = enabled > 0 ? enabled + ' active' : 'None';
}

// The next enabled schedule from `now`, as {entry, at}: the soonest time on
// one of its days within the coming week (the engine runs them in this
// house's local time, which is this device's).
export function nextSchedule(entries, now) {
  let best = null;
  (entries || []).forEach(function (entry) {
    if (entry.enabled === false) return;
    const parts = String(entry.time || '').split(':');
    const hour = Number(parts[0]);
    const minute = Number(parts[1]);
    if (!Number.isFinite(hour) || !Number.isFinite(minute)) return;
    for (let offset = 0; offset < 8; offset += 1) {
      const at = new Date(now);
      at.setDate(now.getDate() + offset);
      at.setHours(hour, minute, 0, 0);
      if (at <= now) continue;
      // getDay(): 0 = Sunday; DAYS starts on Monday.
      if (!entry.days.includes(DAYS[(at.getDay() + 6) % 7][0])) continue;
      if (!best || at < best.at) best = { entry: entry, at: at };
      break;
    }
  });
  return best;
}

// The schedule's action as a verb phrase for the "Next:" line.
const NEXT_VERBS = {
  disarm: 'Disarm',
  partial: 'Arm partial',
  perimeter: 'Arm perimeter',
  arm: 'Arm full',
};

// The glance card's one schedule line (#882): what the alarm does next on its
// own, so "will it arm tonight?" needs no tap. Hidden with no schedule on.
function renderNextSchedule() {
  if (!els.securityNext) return;
  const now = new Date();
  const next = nextSchedule(state.securitySchedules, now);
  if (!next) {
    els.securityNext.hidden = true;
    els.securityNext.textContent = '';
    return;
  }
  const tomorrow = new Date(now);
  tomorrow.setDate(now.getDate() + 1);
  const day = next.at.toDateString() === now.toDateString()
    ? ''
    : next.at.toDateString() === tomorrow.toDateString()
      ? 'tomorrow '
      : DAYS[(next.at.getDay() + 6) % 7][1] + ' ';
  els.securityNext.textContent = 'Next: ' + NEXT_VERBS[next.entry.action] + ' ' + day + 'at ' + next.entry.time;
  els.securityNext.hidden = false;
}

function daysSummary(days) {
  const active = DAYS.map(function (day) { return day[0]; }).filter(function (day) {
    return days.includes(day);
  });
  if (active.length === 7) return 'Every day';
  if (active.join(',') === 'mon,tue,wed,thu,fri') return 'Weekdays';
  if (active.join(',') === 'sat,sun') return 'Weekends';
  return DAYS.filter(function (day) { return active.includes(day[0]); })
    .map(function (day) { return day[1]; }).join(', ');
}

function renderEditorDays() {
  const staged = scheduleEditor.staged;
  if (!els.securityScheduleDays || !staged) return;
  els.securityScheduleDays.innerHTML = '';
  DAYS.forEach(function (day) {
    const btn = document.createElement('button');
    const active = staged.days.includes(day[0]);
    btn.type = 'button';
    btn.className = 'alarm-schedule-day' + (active ? ' active' : '');
    btn.textContent = day[1];
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    btn.addEventListener('click', function () {
      const current = staged.days.slice();
      const pos = current.indexOf(day[0]);
      if (pos >= 0 && current.length > 1) current.splice(pos, 1);
      else if (pos < 0) current.push(day[0]);
      staged.days = DAYS.map(function (d) { return d[0]; })
        .filter(function (value) { return current.includes(value); });
      renderEditorDays();
    });
    els.securityScheduleDays.appendChild(btn);
  });
}

const scheduleEditor = denseListEditor({
  dialog: els.securityScheduleDialog,
  addButton: els.securityScheduleAdd,
  closeButton: els.securityScheduleEditorClose,
  saveButton: els.securityScheduleSave,
  deleteButton: els.securityScheduleDelete,
  titleEl: els.securityScheduleEditorTitle,
  listEl: els.securitySchedules,
  focusEl: els.securityScheduleTime,
  rowIdAttr: 'data-schedule-id',
  titles: { add: 'Add schedule', edit: 'Edit schedule' },
  deleteConfirm: {
    title: 'Delete this alarm schedule?',
    message: 'This schedule will be removed permanently.',
  },
  toasts: { saved: 'Schedules saved', failed: "Couldn't save schedules" },
  defaults: scheduleDefaults,
  stage: function (source) {
    return {
      id: source.id,
      enabled: source.enabled !== false,
      time: source.time || '21:00',
      days: source.days.slice(),
      action: source.action,
    };
  },
  getEntries: function () { return state.securitySchedules; },
  setEntries: function (entries) { state.securitySchedules = entries; },
  normalize: normalizedSchedules,
  render: renderSchedules,
  populate: function (staged) {
    setToggleState(els.securityScheduleEnabled, staged.enabled);
    els.securityScheduleTime.value = staged.time;
    els.securityScheduleAction.value = staged.action;
    renderEditorDays();
  },
  collect: function (staged) {
    staged.enabled = isToggleOn(els.securityScheduleEnabled);
    staged.time = els.securityScheduleTime.value || '21:00';
    staged.action = ACTIONS.includes(els.securityScheduleAction.value)
      ? els.securityScheduleAction.value : 'arm';
  },
  endpoint: '/api/security/schedules',
  bodyKey: 'entries',
});

export function renderSchedules() {
  if (!els.securitySchedules || !els.securitySchedulesNote) return;
  els.securitySchedules.innerHTML = '';
  state.securitySchedules = normalizedSchedules();
  renderScheduleCount();
  renderNextSchedule();
  if (!state.securitySchedules.length) {
    els.securitySchedulesNote.hidden = false;
    els.securitySchedulesNote.textContent = 'No alarm schedules.';
    return;
  }
  els.securitySchedulesNote.hidden = true;

  state.securitySchedules.forEach(function (entry, idx) {
    els.securitySchedules.appendChild(renderSummaryRow({
      id: entry.id,
      idAttr: 'scheduleId',
      title: entry.time,
      meta: ACTION_LABELS[entry.action] + ' · ' + daysSummary(entry.days),
      openLabel: 'Edit schedule at ' + entry.time,
      onOpen: function (main) { scheduleEditor.open(idx, main); },
      toggleName: 'alarm-schedule-enabled',
      toggleOn: entry.enabled,
      toggleLabel: 'Enable schedule at ' + entry.time,
      onToggle: function (on) {
        const proposed = state.securitySchedules.map(function (schedule, scheduleIndex) {
          return scheduleIndex === idx ? { ...schedule, enabled: on } : schedule;
        });
        scheduleEditor.save(proposed);
      },
    }));
  });
}

export async function loadSecuritySchedules() {
  if (!els.securitySchedules) return;
  try {
    const body = await jsonApi('/api/security/schedules');
    state.securitySchedules = (body && body.entries) || [];
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    state.securitySchedules = [];
    if (els.securitySchedulesNote) {
      els.securitySchedulesNote.hidden = false;
      els.securitySchedulesNote.textContent = friendlyError(exc, 'Failed to load schedules.');
    }
  }
  renderSchedules();
}

export function wireSecuritySchedules() {
  if (!els.securityScheduleAdd || !els.securityScheduleDialog) return;
  ACTIONS.forEach(function (name) {
    const option = document.createElement('option');
    option.value = name;
    option.textContent = ACTION_LABELS[name];
    els.securityScheduleAction.appendChild(option);
  });
  wireToggle(els.securityScheduleEnabled, function (on) {
    if (scheduleEditor.staged) scheduleEditor.staged.enabled = on;
  });
  scheduleEditor.wire();
}
