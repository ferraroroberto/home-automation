/* Weekly alarm-schedule editor (split out of security.js, issue #197).
 *
 * Owns the DAYS-based schedule CRUD: load/normalise/render the schedule cards
 * and persist edits through GET/PUT /api/security/schedules. The action set
 * (ACTIONS / ACTION_LABELS) is owned by the alarm module and imported here so
 * the schedule's action dropdown stays in lockstep with the alarm pills.
 */

'use strict';

import { state, els } from './state.js';
import { jsonApi } from './api.js';
import { icon } from './_vendored/icons/icons.js';
import { ACTIONS, ACTION_LABELS } from './security-alarm.js';
import { buildToggle, isToggleOn, setToggleState, wireToggle } from './toggle.js';
import { denseListEditor } from './dense-editor.js';

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

function renderScheduleCount() {
  if (!els.securitySchedulesCount) return;
  const enabled = (state.securitySchedules || []).filter(function (entry) { return entry.enabled !== false; }).length;
  if (enabled > 0) {
    els.securitySchedulesCount.textContent = enabled + ' active';
    els.securitySchedulesCount.hidden = false;
  } else {
    els.securitySchedulesCount.hidden = true;
  }
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
  if (!state.securitySchedules.length) {
    els.securitySchedulesNote.hidden = false;
    els.securitySchedulesNote.textContent = 'No alarm schedules.';
    return;
  }
  els.securitySchedulesNote.hidden = true;

  state.securitySchedules.forEach(function (entry, idx) {
    const row = document.createElement('div');
    row.className = 'list-row automation-summary-row';
    row.dataset.scheduleId = entry.id;

    const main = document.createElement('button');
    main.type = 'button';
    main.className = 'automation-summary-main';
    main.setAttribute('aria-label', 'Edit schedule at ' + entry.time);

    const copy = document.createElement('span');
    copy.className = 'automation-summary-copy';
    const title = document.createElement('span');
    title.className = 'automation-summary-title';
    title.textContent = entry.time;
    const meta = document.createElement('span');
    meta.className = 'automation-summary-meta';
    meta.textContent = ACTION_LABELS[entry.action] + ' · ' + daysSummary(entry.days);
    copy.appendChild(title);
    copy.appendChild(meta);
    main.appendChild(copy);
    main.insertAdjacentHTML('beforeend', icon('chevron-right', 'automation-summary-chevron'));
    main.addEventListener('click', function () { scheduleEditor.open(idx, main); });
    row.appendChild(main);

    const enabled = buildToggle('alarm-schedule-enabled', entry.enabled, function (on) {
      const proposed = state.securitySchedules.map(function (schedule, scheduleIndex) {
        return scheduleIndex === idx ? { ...schedule, enabled: on } : schedule;
      });
      scheduleEditor.save(proposed);
    });
    enabled.setAttribute('aria-label', 'Enable schedule at ' + entry.time);
    row.appendChild(enabled);
    els.securitySchedules.appendChild(row);
  });
}

export async function loadSecuritySchedules() {
  if (!els.securitySchedules) return;
  try {
    const body = await jsonApi('/api/security/schedules');
    state.securitySchedules = (body && body.entries) || [];
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    state.securitySchedules = [];
    if (els.securitySchedulesNote) {
      els.securitySchedulesNote.hidden = false;
      els.securitySchedulesNote.textContent = exc.message || 'Failed to load schedules.';
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
