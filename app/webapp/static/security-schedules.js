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
import { icon } from './icons.js';
import { ACTIONS, ACTION_LABELS } from './security-alarm.js';
import { buildToggle, isToggleOn, setToggleState, wireToggle } from './toggle.js';
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

let editorIndex = null;
let editorScheduleId = null;
let editorReturnFocus = null;
let stagedSchedule = null;

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
  if (!els.securityScheduleDays || !stagedSchedule) return;
  els.securityScheduleDays.innerHTML = '';
  DAYS.forEach(function (day) {
    const btn = document.createElement('button');
    const active = stagedSchedule.days.includes(day[0]);
    btn.type = 'button';
    btn.className = 'alarm-schedule-day' + (active ? ' active' : '');
    btn.textContent = day[1];
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    btn.addEventListener('click', function () {
      const current = stagedSchedule.days.slice();
      const pos = current.indexOf(day[0]);
      if (pos >= 0 && current.length > 1) current.splice(pos, 1);
      else if (pos < 0) current.push(day[0]);
      stagedSchedule.days = DAYS.map(function (d) { return d[0]; })
        .filter(function (value) { return current.includes(value); });
      renderEditorDays();
    });
    els.securityScheduleDays.appendChild(btn);
  });
}

function openScheduleEditor(index, trigger) {
  editorIndex = index;
  const source = index == null ? scheduleDefaults() : state.securitySchedules[index];
  stagedSchedule = {
    id: source.id,
    enabled: source.enabled !== false,
    time: source.time || '21:00',
    days: source.days.slice(),
    action: source.action,
  };
  editorScheduleId = stagedSchedule.id;
  editorReturnFocus = trigger || null;
  els.securityScheduleEditorTitle.textContent = index == null ? 'Add schedule' : 'Edit schedule';
  setToggleState(els.securityScheduleEnabled, stagedSchedule.enabled);
  els.securityScheduleTime.value = stagedSchedule.time;
  els.securityScheduleAction.value = stagedSchedule.action;
  els.securityScheduleDelete.hidden = index == null;
  renderEditorDays();
  if (typeof els.securityScheduleDialog.showModal === 'function') els.securityScheduleDialog.showModal();
  else els.securityScheduleDialog.setAttribute('open', '');
  els.securityScheduleTime.focus();
}

function closeScheduleEditor() {
  if (typeof els.securityScheduleDialog.close === 'function') els.securityScheduleDialog.close();
  else els.securityScheduleDialog.removeAttribute('open');
}

function restoreEditorFocus() {
  let target = editorReturnFocus && editorReturnFocus.isConnected ? editorReturnFocus : null;
  if (!target && editorScheduleId) {
    const row = els.securitySchedules.querySelector('[data-schedule-id="' + CSS.escape(editorScheduleId) + '"]');
    if (row) target = row.querySelector('.automation-summary-main');
  }
  if (!target) target = els.securityScheduleAdd;
  editorIndex = null;
  editorScheduleId = null;
  editorReturnFocus = null;
  stagedSchedule = null;
  if (target) requestAnimationFrame(function () { target.focus(); });
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
    main.addEventListener('click', function () { openScheduleEditor(idx, main); });
    row.appendChild(main);

    const enabled = buildToggle('alarm-schedule-enabled', entry.enabled, function (on) {
      const proposed = state.securitySchedules.map(function (schedule, scheduleIndex) {
        return scheduleIndex === idx ? { ...schedule, enabled: on } : schedule;
      });
      saveSecuritySchedules(proposed);
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

async function saveSecuritySchedules(entries) {
  const previous = state.securitySchedules;
  state.securitySchedules = normalizedSchedules(entries);
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
    return true;
  } catch (exc) {
    state.securitySchedules = previous;
    renderSchedules();
    if (String(exc.message) !== 'auth required') {
      toast("Couldn't save schedules", 'error');
    }
    return false;
  }
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
    if (stagedSchedule) stagedSchedule.enabled = on;
  });
  els.securityScheduleAdd.addEventListener('click', function () {
    openScheduleEditor(null, els.securityScheduleAdd);
  });
  els.securityScheduleEditorClose.addEventListener('click', closeScheduleEditor);
  els.securityScheduleDialog.addEventListener('click', function (ev) {
    if (ev.target === els.securityScheduleDialog) closeScheduleEditor();
  });
  els.securityScheduleDialog.addEventListener('close', restoreEditorFocus);
  els.securityScheduleSave.addEventListener('click', async function () {
    if (!stagedSchedule) return;
    stagedSchedule.enabled = isToggleOn(els.securityScheduleEnabled);
    stagedSchedule.time = els.securityScheduleTime.value || '21:00';
    stagedSchedule.action = ACTIONS.includes(els.securityScheduleAction.value)
      ? els.securityScheduleAction.value : 'arm';
    const proposed = state.securitySchedules.slice();
    if (editorIndex == null) proposed.push(stagedSchedule);
    else proposed[editorIndex] = stagedSchedule;
    els.securityScheduleSave.disabled = true;
    const saved = await saveSecuritySchedules(proposed);
    els.securityScheduleSave.disabled = false;
    if (saved) closeScheduleEditor();
  });
  els.securityScheduleDelete.addEventListener('click', async function () {
    if (editorIndex == null) return;
    const ok = await confirmAction({
      title: 'Delete this alarm schedule?',
      message: 'This schedule will be removed permanently.',
      okLabel: 'Delete',
      danger: true,
    });
    if (!ok) return;
    const proposed = state.securitySchedules.filter(function (_entry, idx) {
      return idx !== editorIndex;
    });
    if (await saveSecuritySchedules(proposed)) closeScheduleEditor();
  });
}
