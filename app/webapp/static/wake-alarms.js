/* Wake alarms + app-native timers (issue #304) — Home's Next up card.
 *
 * Distinct from the RISCO alarm (security.js / security-alarm.js): this
 * feature rings/notifies at a time you set, it never arms/disarms the
 * security system. Alarms are recurring (day-of-week) or one-shot (a specific
 * date); timers are ephemeral countdowns, not persisted (mirrors how Home
 * Assistant's own voice-set timers work).
 *
 * Since #885 (Step 7/8 of #872) both are rows of Next up on the shared row
 * (row.js), led by their time:
 * - a wake alarm row shows its time, label and days, with its switch; the
 *   row opens the staged wake alarm editor (denseListEditor, the dense
 *   collection's contract: Save is the only persistence boundary), which
 *   replaced inline cards that saved on every keystroke;
 * - a running timer row counts down, with × to cancel it; Start timer opens
 *   the timer sheet (presets or minutes), which starts one at once;
 * - a ringing alarm or a finished timer leads the card with Dismiss.
 */

'use strict';

import { state, els, toast } from './state.js';
import { jsonApi, isAuthRequired, reportActionFailure } from './api.js';
import { buildToggle, isToggleOn, setToggleState, wireToggle } from './toggle.js';
import { icon } from './_vendored/icons/icons.js';
import { createPoller } from './poll.js';
import { localIsoDate, friendlyError } from './format.js';
import { ALL_DAYS, daysSummary, renderDayPicker } from './days.js';
import { denseListEditor } from './dense-editor.js';
import { rowEl } from './row.js';
import { sheet } from './sheet.js';
import { renderNextUpNote } from './home.js';

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

function normalizedWakeAlarms(entries) {
  return (entries || state.wakeAlarms || []).map(function (entry, idx) {
    const days = Array.isArray(entry.days)
      ? ALL_DAYS.filter(function (day) { return entry.days.includes(day); })
      : [];
    return {
      id: entry.id || ('alarm-' + (idx + 1)),
      label: entry.label || '',
      enabled: entry.enabled !== false,
      time: entry.time || '07:00',
      days: days.length ? days : ALL_DAYS.slice(),
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

// A one-shot alarm's date in words: Today, Tomorrow, else "Sat 11 Oct".
function dateWords(iso) {
  if (iso === localIsoDate()) return 'Today';
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  if (iso === localIsoDate(tomorrow)) return 'Tomorrow';
  const day = new Date(iso + 'T00:00:00');
  if (Number.isNaN(day.getTime())) return iso;
  return day.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
}

// -------------------------------------------------------------- ringing UI
function ringingRow(iconName, text, onDismiss) {
  const row = document.createElement('div');
  row.className = 'wake-ringing-row';
  const words = document.createElement('span');
  words.innerHTML = icon(iconName) + ' ';
  words.append(text);
  row.appendChild(words);
  const dismiss = document.createElement('button');
  dismiss.type = 'button';
  dismiss.className = 'wake-ringing-dismiss';
  dismiss.textContent = 'Dismiss';
  dismiss.addEventListener('click', onDismiss);
  row.appendChild(dismiss);
  return row;
}

function renderRingingBanner() {
  if (!els.wakeRingingBanner) return;
  const ringingAlarms = (state.wakeAlarms || []).filter(function (e) { return e.ringing; });
  const ringingTimers = (state.wakeTimers || []).filter(function (t) { return t.ringing; });
  els.wakeRingingBanner.innerHTML = '';
  els.wakeRingingBanner.hidden = !ringingAlarms.length && !ringingTimers.length;
  ringingAlarms.forEach(function (entry) {
    els.wakeRingingBanner.appendChild(ringingRow('alarm-clock', (entry.label || entry.time) + ' is ringing',
      function () { dismissWakeAlarm(entry.id); }));
  });
  ringingTimers.forEach(function (timer) {
    els.wakeRingingBanner.appendChild(ringingRow('timer', (timer.label || 'Timer') + ' is done',
      function () { cancelWakeTimer(timer.id); }));
  });
}

// ------------------------------------------------------------ alarm editor
function renderEditorWhen() {
  const staged = alarmEditor.staged;
  if (!staged) return;
  const once = !!staged.date;
  els.wakeAlarmDateRow.hidden = !once;
  els.wakeAlarmDaysBlock.hidden = once;
  renderDayPicker(els.wakeAlarmDays, staged.days, function (days) {
    staged.days = days;
    renderEditorWhen();
  });
}

const alarmEditor = denseListEditor({
  dialog: els.wakeAlarmDialog,
  addButton: els.wakeAlarmAdd,
  closeButton: els.wakeAlarmEditorClose,
  saveButton: els.wakeAlarmSave,
  deleteButton: els.wakeAlarmDelete,
  titleEl: els.wakeAlarmEditorTitle,
  listEl: els.wakeAlarmsList,
  focusEl: els.wakeAlarmTime,
  rowIdAttr: 'data-alarm-id',
  titles: { add: 'Add wake alarm', edit: 'Edit wake alarm' },
  deleteConfirm: {
    title: 'Delete this wake alarm?',
    message: 'This wake alarm will be removed permanently.',
  },
  toasts: { saved: 'Wake alarms saved', failed: "Couldn't save wake alarms" },
  defaults: alarmDefaults,
  stage: function (source) {
    return { ...source, days: source.days.slice() };
  },
  getEntries: function () { return state.wakeAlarms; },
  setEntries: function (entries) { state.wakeAlarms = entries; },
  normalize: normalizedWakeAlarms,
  render: renderWakeAlarms,
  populate: function (staged) {
    setToggleState(els.wakeAlarmEnabled, staged.enabled);
    els.wakeAlarmTime.value = staged.time;
    els.wakeAlarmLabel.value = staged.label;
    setToggleState(els.wakeAlarmOnce, !!staged.date);
    els.wakeAlarmDate.value = staged.date || '';
    renderEditorWhen();
  },
  collect: function (staged) {
    staged.enabled = isToggleOn(els.wakeAlarmEnabled);
    staged.time = els.wakeAlarmTime.value || '07:00';
    staged.label = els.wakeAlarmLabel.value.trim().slice(0, 80);
    // The server fires a one-shot alarm on its local date.
    staged.date = isToggleOn(els.wakeAlarmOnce) ? (els.wakeAlarmDate.value || localIsoDate()) : null;
  },
  endpoint: '/api/wake-alarms',
  bodyKey: 'entries',
});

// -------------------------------------------------------------------- rows
// One wake alarm on the shared row: its time leads, the label (or "Alarm")
// and when it rings, the switch saves at once like every row switch.
function alarmRow(entry, idx) {
  const toggle = buildToggle('wake-alarm-enabled', entry.enabled, function (on) {
    alarmEditor.save(state.wakeAlarms.map(function (alarm, i) {
      return i === idx ? { ...alarm, enabled: on } : alarm;
    }));
  });
  toggle.setAttribute('aria-label', (entry.label || 'Wake alarm') + ' at ' + entry.time);
  const when = entry.date ? 'Once · ' + dateWords(entry.date) : daysSummary(entry.days);
  const row = rowEl({
    className: 'wake-alarm-row' + (entry.ringing ? ' is-ringing' : '') + (entry.enabled ? '' : ' is-off'),
    time: entry.time,
    title: entry.label || 'Alarm',
    meta: when,
    openLabel: 'Edit wake alarm: ' + (entry.label || 'Alarm') + ', ' + entry.time + ', ' + when,
    onOpen: function (btn) { alarmEditor.open(idx, btn); },
    trail: toggle,
  });
  row.dataset.alarmId = entry.id;
  return row;
}

export function renderWakeAlarms() {
  if (!els.wakeAlarmsList) return;
  state.wakeAlarms = normalizedWakeAlarms();
  els.wakeAlarmsList.innerHTML = '';
  renderRingingBanner();
  // By time of day; the editor still edits by the stored index.
  state.wakeAlarms
    .map(function (entry, idx) { return { entry: entry, idx: idx }; })
    .sort(function (a, b) { return a.entry.time.localeCompare(b.entry.time); })
    .forEach(function (item) { els.wakeAlarmsList.appendChild(alarmRow(item.entry, item.idx)); });
  renderNextUpNote();
}

export async function loadWakeAlarms() {
  if (!els.wakeAlarmsList) return;
  try {
    const body = await jsonApi('/api/wake-alarms');
    state.wakeAlarms = (body && body.entries) || [];
    renderNextUpNote('alarms', null);
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    state.wakeAlarms = [];
    renderNextUpNote('alarms', friendlyError(exc, 'Failed to load wake alarms.'));
  }
  renderWakeAlarms();
}

async function dismissWakeAlarm(alarmId) {
  try {
    await jsonApi('/api/wake-alarms/' + encodeURIComponent(alarmId) + '/dismiss', { method: 'POST' });
  } catch (exc) {
    reportActionFailure(exc, 'Dismiss failed');
  }
  loadWakeAlarms();
}

// ------------------------------------------------------------------ timers
// A running timer on the shared row: what is left leads (in accent, it is in
// progress), × cancels it. A finished one says Done and waits in the banner.
function timerRow(timer, now) {
  const cancel = document.createElement('button');
  cancel.type = 'button';
  cancel.className = 'icon-button wake-timer-cancel';
  cancel.setAttribute('aria-label', 'Cancel ' + (timer.label || 'timer'));
  cancel.innerHTML = icon('x');
  cancel.addEventListener('click', function () { cancelWakeTimer(timer.id); });
  const ends = new Date(timer.ends_at * 1000);
  const row = rowEl({
    className: 'wake-timer-row' + (timer.ringing ? ' is-ringing' : ''),
    time: timer.ringing ? 'Done' : fmtRemaining(timer.ends_at - now),
    title: timer.label || 'Timer',
    meta: 'Ends ' + ends.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }),
    trail: cancel,
  });
  row.dataset.timerId = timer.id;
  return row;
}

export function renderWakeTimers() {
  if (!els.wakeTimersList) return;
  els.wakeTimersList.innerHTML = '';
  renderRingingBanner();
  const now = Date.now() / 1000;
  (state.wakeTimers || []).forEach(function (timer) {
    els.wakeTimersList.appendChild(timerRow(timer, now));
  });
  renderNextUpNote();
}

// The one-second tick between polls: only the countdowns change, in place,
// so a focused × keeps its focus.
function tickWakeTimers() {
  if (!els.wakeTimersList) return;
  const now = Date.now() / 1000;
  (state.wakeTimers || []).forEach(function (timer) {
    if (timer.ringing) return;
    const row = els.wakeTimersList.querySelector('[data-timer-id="' + CSS.escape(timer.id) + '"]');
    const time = row && row.querySelector('.row-time');
    if (time) time.textContent = fmtRemaining(timer.ends_at - now);
  });
}

export async function loadWakeTimers() {
  if (!els.wakeTimersList) return;
  try {
    const body = await jsonApi('/api/wake-timers');
    state.wakeTimers = (body && body.timers) || [];
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    state.wakeTimers = [];
  }
  renderNextUpNote('timers', null);
  renderWakeTimers();
}

async function createWakeTimer(seconds, label) {
  timerSheet.close();
  try {
    await jsonApi('/api/wake-timers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ seconds: seconds, label: label || '' }),
    });
    toast('Timer started', 'success');
  } catch (exc) {
    reportActionFailure(exc, 'Timer start failed');
  }
  loadWakeTimers();
}

async function cancelWakeTimer(timerId) {
  try {
    await jsonApi('/api/wake-timers/' + encodeURIComponent(timerId), { method: 'DELETE' });
  } catch (exc) {
    reportActionFailure(exc, 'Timer cancel failed');
  }
  loadWakeTimers();
}

// The timer sheet starts a timer and closes onto its row; nothing is staged.
const timerSheet = sheet(els.wakeTimerSheet, {
  model: 'instant',
  closeButton: els.wakeTimerSheetClose,
  fallbackFocus: function () { return els.wakeTimerOpen; },
});

// ----------------------------------------------------------------- wiring
export function wireWakeAlarms() {
  if (!els.wakeAlarmDialog) return;
  wireToggle(els.wakeAlarmOnce, function (on) {
    const staged = alarmEditor.staged;
    if (!staged) return;
    staged.date = on ? (els.wakeAlarmDate.value || localIsoDate()) : null;
    els.wakeAlarmDate.value = staged.date || '';
    renderEditorWhen();
  });
  alarmEditor.wire();

  if (els.wakeTimerOpen) {
    els.wakeTimerOpen.addEventListener('click', function () {
      els.wakeTimerCustomMinutes.value = '';
      timerSheet.open(els.wakeTimerOpen);
    });
  }
  document.querySelectorAll('.wake-timer-presets .segmented-item').forEach(function (btn) {
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
    });
  }
}

// Poll only while the Home tab is active (main.js:828's idiom): the alarm
// list rarely changes server-side (only via a fire/dismiss), and timers only
// matter while the user might be looking at the countdown.
const scheduleWakeAlarms = createPoller(function () { loadWakeAlarms(); loadWakeTimers(); });
// Ticks the countdowns from already-fetched state, no network call — smooth
// ticking between the 10s server polls.
const scheduleWakeTimersTick = createPoller(tickWakeTimers);
export function onWakeAlarmsTab(tab) {
  scheduleWakeAlarms(0);
  scheduleWakeTimersTick(0);
  if (tab !== 'home') return;
  loadWakeAlarms();
  loadWakeTimers();
  scheduleWakeAlarms(10_000);
  scheduleWakeTimersTick(1000);
}
