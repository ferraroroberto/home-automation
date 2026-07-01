/* Wake alarms + app-native timers (issue #304) — Home-tab card.
 *
 * Distinct from the RISCO "Alarm controls" card (security.js /
 * security-alarm.js): this feature rings/notifies at a time you set, it
 * never arms/disarms the security system. Alarms are recurring (day-of-week)
 * or one-shot (a specific date); timers are ephemeral countdowns, not
 * persisted (mirrors how Home Assistant's own voice-set timers work).
 */

'use strict';

import { state, els, toast } from './state.js';
import { jsonApi } from './api.js';

const DAYS = [
  ['mon', 'Mon'],
  ['tue', 'Tue'],
  ['wed', 'Wed'],
  ['thu', 'Thu'],
  ['fri', 'Fri'],
  ['sat', 'Sat'],
  ['sun', 'Sun'],
];

const TIMER_PRESETS_S = [300, 600, 900, 1800];

function alarmDefaults() {
  return {
    id: 'alarm-' + Date.now().toString(36),
    label: '',
    enabled: true,
    time: '07:00',
    days: ['mon', 'tue', 'wed', 'thu', 'fri'],
    date: null,
    ringing: false,
  };
}

function normalizedWakeAlarms() {
  return (state.wakeAlarms || []).map(function (entry, idx) {
    return {
      id: entry.id || ('alarm-' + (idx + 1)),
      label: entry.label || '',
      enabled: entry.enabled !== false,
      time: entry.time || '07:00',
      days: Array.isArray(entry.days) && entry.days.length ? entry.days : DAYS.map(function (d) { return d[0]; }),
      date: entry.date || null,
      ringing: entry.ringing === true,
    };
  });
}

function fmtRemaining(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return m + ':' + (rem < 10 ? '0' : '') + rem;
}

// -------------------------------------------------------------- ringing UI
function renderRingingBanner() {
  if (!els.wakeRingingBanner) return;
  const ringingAlarms = (state.wakeAlarms || []).filter(function (e) { return e.ringing; });
  const ringingTimers = (state.wakeTimers || []).filter(function (t) { return t.ringing; });
  if (!ringingAlarms.length && !ringingTimers.length) {
    els.wakeRingingBanner.hidden = true;
    els.wakeRingingBanner.innerHTML = '';
    return;
  }
  els.wakeRingingBanner.hidden = false;
  els.wakeRingingBanner.innerHTML = '';
  ringingAlarms.forEach(function (entry) {
    const row = document.createElement('div');
    row.className = 'wake-ringing-row';
    const text = document.createElement('span');
    text.textContent = '⏰ ' + (entry.label || entry.time) + ' is ringing';
    row.appendChild(text);
    const dismiss = document.createElement('button');
    dismiss.type = 'button';
    dismiss.className = 'wake-ringing-dismiss';
    dismiss.textContent = 'Dismiss';
    dismiss.addEventListener('click', function () { dismissWakeAlarm(entry.id); });
    row.appendChild(dismiss);
    els.wakeRingingBanner.appendChild(row);
  });
  ringingTimers.forEach(function (timer) {
    const row = document.createElement('div');
    row.className = 'wake-ringing-row';
    const text = document.createElement('span');
    text.textContent = '⏱️ ' + (timer.label || (timer.seconds + 's')) + ' is done';
    row.appendChild(text);
    const dismiss = document.createElement('button');
    dismiss.type = 'button';
    dismiss.className = 'wake-ringing-dismiss';
    dismiss.textContent = 'Dismiss';
    dismiss.addEventListener('click', function () { cancelWakeTimer(timer.id); });
    row.appendChild(dismiss);
    els.wakeRingingBanner.appendChild(row);
  });
}

// -------------------------------------------------------------------- list
function renderWakeAlarmsCount() {
  if (!els.wakeAlarmsCount) return;
  const enabled = (state.wakeAlarms || []).filter(function (e) { return e.enabled; }).length;
  if (enabled > 0) {
    els.wakeAlarmsCount.textContent = enabled + ' active';
    els.wakeAlarmsCount.hidden = false;
  } else {
    els.wakeAlarmsCount.hidden = true;
  }
}

export function renderWakeAlarms() {
  if (!els.wakeAlarmsList || !els.wakeAlarmsNote) return;
  els.wakeAlarmsList.innerHTML = '';
  state.wakeAlarms = normalizedWakeAlarms();
  renderWakeAlarmsCount();
  renderRingingBanner();
  if (!state.wakeAlarms.length) {
    els.wakeAlarmsNote.hidden = false;
    els.wakeAlarmsNote.textContent = 'No wake alarms.';
    return;
  }
  els.wakeAlarmsNote.hidden = true;

  state.wakeAlarms.forEach(function (entry, idx) {
    const card = document.createElement('div');
    card.className = 'schedule-entry alarm-schedule-entry' + (entry.ringing ? ' is-ringing' : '');
    card.dataset.alarmId = entry.id;

    const head = document.createElement('div');
    head.className = 'schedule-entry-head';

    const enabled = document.createElement('label');
    enabled.className = 'schedule-enabled';
    enabled.innerHTML = '<input type="checkbox" class="checkbox-native wake-alarm-enabled"' +
      (entry.enabled ? ' checked' : '') + '> <span>Enabled</span>';
    enabled.querySelector('input').addEventListener('change', function (ev) {
      state.wakeAlarms[idx].enabled = ev.target.checked;
      saveWakeAlarms();
    });
    head.appendChild(enabled);

    const del = document.createElement('button');
    del.type = 'button';
    del.className = 'schedule-delete';
    del.setAttribute('aria-label', 'Delete wake alarm');
    del.textContent = '×';
    del.addEventListener('click', function () {
      state.wakeAlarms.splice(idx, 1);
      saveWakeAlarms();
    });
    head.appendChild(del);
    card.appendChild(head);

    const fields = document.createElement('div');
    fields.className = 'alarm-schedule-fields';

    const labelWrap = document.createElement('label');
    const labelText = document.createElement('span');
    labelText.textContent = 'Label';
    const labelInput = document.createElement('input');
    labelInput.type = 'text';
    labelInput.className = 'input-native wake-alarm-label';
    labelInput.maxLength = 80;
    labelInput.placeholder = 'Wake up';
    labelInput.value = entry.label;
    labelInput.addEventListener('change', function () {
      state.wakeAlarms[idx].label = labelInput.value;
      saveWakeAlarms();
    });
    labelWrap.appendChild(labelText);
    labelWrap.appendChild(labelInput);
    fields.appendChild(labelWrap);

    const timeLabel = document.createElement('label');
    const timeText = document.createElement('span');
    timeText.textContent = 'Time';
    const time = document.createElement('input');
    time.type = 'time';
    time.className = 'input-native alarm-schedule-time';
    time.value = entry.time;
    time.addEventListener('change', function () {
      state.wakeAlarms[idx].time = time.value || '07:00';
      saveWakeAlarms();
    });
    timeLabel.appendChild(timeText);
    timeLabel.appendChild(time);
    fields.appendChild(timeLabel);
    card.appendChild(fields);

    const onceWrap = document.createElement('label');
    onceWrap.className = 'wake-alarm-once';
    onceWrap.innerHTML = '<input type="checkbox" class="checkbox-native wake-alarm-once-toggle"' +
      (entry.date ? ' checked' : '') + '> <span>Just once</span>';
    onceWrap.querySelector('input').addEventListener('change', function (ev) {
      if (ev.target.checked) {
        const today = new Date();
        state.wakeAlarms[idx].date = today.toISOString().slice(0, 10);
      } else {
        state.wakeAlarms[idx].date = null;
      }
      saveWakeAlarms();
    });
    card.appendChild(onceWrap);

    if (entry.date) {
      const dateInput = document.createElement('input');
      dateInput.type = 'date';
      dateInput.className = 'input-native wake-alarm-date';
      dateInput.value = entry.date;
      dateInput.addEventListener('change', function () {
        state.wakeAlarms[idx].date = dateInput.value || entry.date;
        saveWakeAlarms();
      });
      card.appendChild(dateInput);
    } else {
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
          const current = state.wakeAlarms[idx].days.slice();
          const pos = current.indexOf(day[0]);
          if (pos >= 0 && current.length > 1) current.splice(pos, 1);
          else if (pos < 0) current.push(day[0]);
          state.wakeAlarms[idx].days = DAYS.map(function (d) { return d[0]; })
            .filter(function (value) { return current.includes(value); });
          saveWakeAlarms();
        });
        days.appendChild(btn);
      });
      card.appendChild(days);
    }

    els.wakeAlarmsList.appendChild(card);
  });
}

export async function loadWakeAlarms() {
  if (!els.wakeAlarmsList) return;
  try {
    const body = await jsonApi('/api/wake-alarms');
    state.wakeAlarms = (body && body.entries) || [];
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    state.wakeAlarms = [];
    if (els.wakeAlarmsNote) {
      els.wakeAlarmsNote.hidden = false;
      els.wakeAlarmsNote.textContent = exc.message || 'Failed to load wake alarms.';
    }
  }
  renderWakeAlarms();
}

async function saveWakeAlarms() {
  state.wakeAlarms = normalizedWakeAlarms();
  renderWakeAlarms();
  try {
    const body = await jsonApi('/api/wake-alarms', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entries: state.wakeAlarms }),
    });
    state.wakeAlarms = (body && body.entries) || [];
    renderWakeAlarms();
    toast('Wake alarms saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Wake alarm save failed: ' + (exc.message || exc), 'error');
    }
  }
}

async function dismissWakeAlarm(alarmId) {
  try {
    await jsonApi('/api/wake-alarms/' + encodeURIComponent(alarmId) + '/dismiss', { method: 'POST' });
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Dismiss failed: ' + (exc.message || exc), 'error');
    }
  }
  loadWakeAlarms();
}

// ------------------------------------------------------------------ timers
export function renderWakeTimers() {
  if (!els.wakeTimersList || !els.wakeTimersNote) return;
  els.wakeTimersList.innerHTML = '';
  renderRingingBanner();
  const timers = state.wakeTimers || [];
  if (!timers.length) {
    els.wakeTimersNote.hidden = false;
    return;
  }
  els.wakeTimersNote.hidden = true;

  const now = Date.now() / 1000;
  timers.forEach(function (timer) {
    const row = document.createElement('div');
    row.className = 'wake-timer-row' + (timer.ringing ? ' is-ringing' : '');

    const label = document.createElement('span');
    label.className = 'wake-timer-label';
    label.textContent = timer.label || 'Timer';
    row.appendChild(label);

    const remaining = document.createElement('span');
    remaining.className = 'wake-timer-remaining';
    remaining.textContent = timer.ringing ? 'Done' : fmtRemaining(timer.ends_at - now);
    row.appendChild(remaining);

    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'schedule-delete';
    cancel.setAttribute('aria-label', 'Cancel timer');
    cancel.textContent = '×';
    cancel.addEventListener('click', function () { cancelWakeTimer(timer.id); });
    row.appendChild(cancel);

    els.wakeTimersList.appendChild(row);
  });
}

export async function loadWakeTimers() {
  if (!els.wakeTimersList) return;
  try {
    const body = await jsonApi('/api/wake-timers');
    state.wakeTimers = (body && body.timers) || [];
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    state.wakeTimers = [];
  }
  renderWakeTimers();
}

async function createWakeTimer(seconds, label) {
  try {
    await jsonApi('/api/wake-timers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ seconds: seconds, label: label || '' }),
    });
    toast('Timer started', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Timer start failed: ' + (exc.message || exc), 'error');
    }
  }
  loadWakeTimers();
}

async function cancelWakeTimer(timerId) {
  try {
    await jsonApi('/api/wake-timers/' + encodeURIComponent(timerId), { method: 'DELETE' });
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Timer cancel failed: ' + (exc.message || exc), 'error');
    }
  }
  loadWakeTimers();
}

// ----------------------------------------------------------------- wiring
export function wireWakeAlarms() {
  if (els.wakeAlarmAdd) {
    els.wakeAlarmAdd.addEventListener('click', function () {
      state.wakeAlarms.push(alarmDefaults());
      saveWakeAlarms();
    });
  }
  document.querySelectorAll('.wake-timer-presets .range-tab').forEach(function (btn) {
    btn.addEventListener('click', function () {
      createWakeTimer(parseInt(btn.dataset.seconds, 10), '');
    });
  });
  if (els.wakeTimerCustomAdd && els.wakeTimerCustomMinutes) {
    els.wakeTimerCustomAdd.addEventListener('click', function () {
      const minutes = parseInt(els.wakeTimerCustomMinutes.value, 10);
      if (!minutes || minutes <= 0) {
        toast('Enter a number of minutes', 'error');
        return;
      }
      createWakeTimer(minutes * 60, '');
      els.wakeTimerCustomMinutes.value = '';
    });
  }
}

// Poll only while the Home tab is active (main.js:828's idiom): the alarm
// list rarely changes server-side (only via a fire/dismiss), and timers only
// matter while the user might be looking at the countdown.
let wakeAlarmsTimer = null;
let wakeTimersTickTimer = null;
export function onWakeAlarmsTab(tab) {
  if (wakeAlarmsTimer) { clearInterval(wakeAlarmsTimer); wakeAlarmsTimer = null; }
  if (wakeTimersTickTimer) { clearInterval(wakeTimersTickTimer); wakeTimersTickTimer = null; }
  if (tab !== 'home') return;
  loadWakeAlarms();
  loadWakeTimers();
  wakeAlarmsTimer = setInterval(function () { loadWakeAlarms(); loadWakeTimers(); }, 10_000);
  // Re-render the countdown display every second from already-fetched state,
  // no network call — smooth ticking between the 10s server polls.
  wakeTimersTickTimer = setInterval(renderWakeTimers, 1000);
}
