/* Weekly alarm-schedule editor (split out of security.js, issue #197).
 *
 * Owns the DAYS-based schedule CRUD: load/normalise/render the schedule cards
 * and persist edits through GET/PUT /api/security/schedules. The action set
 * (ACTIONS / ACTION_LABELS) is owned by the alarm module and imported here so
 * the schedule's action dropdown stays in lockstep with the alarm pills.
 */

'use strict';

import { state, els, toast } from './state.js';
import { jsonApi } from './api.js';
import { ACTIONS, ACTION_LABELS } from './security-alarm.js';
import { buildToggle } from './toggle.js';
import { confirmAction } from './network.js';

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

function normalizedSchedules() {
  return (state.securitySchedules || []).map(function (entry, idx) {
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
    const card = document.createElement('div');
    card.className = 'schedule-entry alarm-schedule-entry';
    card.dataset.scheduleId = entry.id;

    const head = document.createElement('div');
    head.className = 'schedule-entry-head';

    const enabled = document.createElement('label');
    enabled.className = 'schedule-enabled';
    const enabledText = document.createElement('span');
    enabledText.textContent = 'Enabled';
    enabled.appendChild(enabledText);
    enabled.appendChild(buildToggle('alarm-schedule-enabled', entry.enabled, function (on) {
      state.securitySchedules[idx].enabled = on;
      saveSecuritySchedules();
    }));
    head.appendChild(enabled);

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'schedule-delete';
    del.setAttribute('aria-label', 'Delete alarm schedule');
    del.textContent = '×';
    del.addEventListener('click', async function () {
      const ok = await confirmAction({
        title: 'Delete this alarm schedule?',
        message: 'This schedule will be removed permanently.',
        okLabel: 'Delete',
        danger: true,
      });
      if (!ok) return;
      state.securitySchedules.splice(idx, 1);
      saveSecuritySchedules();
    });
    head.appendChild(del);
    card.appendChild(head);

    const fields = document.createElement('div');
    fields.className = 'alarm-schedule-fields';

    const timeLabel = document.createElement('label');
    const timeText = document.createElement('span');
    timeText.textContent = 'Time';
    const time = document.createElement('input');
    time.type = 'time';
    time.className = 'input-native alarm-schedule-time';
    time.value = entry.time;
    time.addEventListener('change', function () {
      state.securitySchedules[idx].time = time.value || '21:00';
      saveSecuritySchedules();
    });
    timeLabel.appendChild(timeText);
    timeLabel.appendChild(time);
    fields.appendChild(timeLabel);

    const actionLabel = document.createElement('label');
    const actionText = document.createElement('span');
    actionText.textContent = 'Action';
    const action = document.createElement('select');
    action.className = 'select-native alarm-schedule-action';
    ACTIONS.forEach(function (name) {
      const opt = document.createElement('option');
      opt.value = name;
      opt.textContent = ACTION_LABELS[name];
      action.appendChild(opt);
    });
    action.value = entry.action;
    action.addEventListener('change', function () {
      state.securitySchedules[idx].action = action.value;
      saveSecuritySchedules();
    });
    actionLabel.appendChild(actionText);
    actionLabel.appendChild(action);
    fields.appendChild(actionLabel);
    card.appendChild(fields);

    const days = document.createElement('div');
    days.className = 'alarm-schedule-days';
    DAYS.forEach(function (day) {
      const btn = document.createElement('button');
      const active = entry.days.includes(day[0]);
      btn.type = 'button';
      btn.className = 'alarm-schedule-day' + (active ? ' active' : '');
      btn.textContent = day[1];
      btn.setAttribute('aria-pressed', active ? 'true' : 'false');
      btn.addEventListener('click', function () {
        const current = state.securitySchedules[idx].days.slice();
        const pos = current.indexOf(day[0]);
        if (pos >= 0 && current.length > 1) current.splice(pos, 1);
        else if (pos < 0) current.push(day[0]);
        state.securitySchedules[idx].days = DAYS.map(function (d) { return d[0]; })
          .filter(function (value) { return current.includes(value); });
        saveSecuritySchedules();
      });
      days.appendChild(btn);
    });
    card.appendChild(days);

    els.securitySchedules.appendChild(card);
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

async function saveSecuritySchedules() {
  state.securitySchedules = normalizedSchedules();
  renderSchedules();
  try {
    const body = await jsonApi('/api/security/schedules', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entries: state.securitySchedules }),
    });
    state.securitySchedules = (body && body.entries) || [];
    renderSchedules();
    toast('Schedules saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Schedule save failed: ' + (exc.message || exc), 'error');
    }
  }
}

export function wireSecuritySchedules() {
  if (!els.securityScheduleAdd) return;
  els.securityScheduleAdd.addEventListener('click', function () {
    state.securitySchedules.push(scheduleDefaults());
    saveSecuritySchedules();
  });
}
