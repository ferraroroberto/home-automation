/* Reminders — bidirectional voice/app free-text checklist (issue #314).
 *
 * Distinct from wake-alarms.js: a wake alarm rings at a set time; a reminder
 * is a checklist item, optionally due on a date/time, completed by toggling
 * it done rather than dismissing an alert. A dense collection (staged edit
 * dialog, home-automation#409) using the shared denseListEditor shell — see
 * security-schedules.js for the sibling impl. Since #885 its rows are Next up
 * rows on Home, on the shared row (row.js): the due time leads when there is
 * one, the done switch trails, and the row opens the editor.
 */

'use strict';

import { state, els, toast } from './state.js';
import { jsonApi, isAuthRequired } from './api.js';
import { buildToggle, isToggleOn, setToggleState, wireToggle } from './toggle.js';
import { denseListEditor } from './dense-editor.js';
import { createPoller } from './poll.js';
import { friendlyError, localIsoDate } from './format.js';
import { rowEl } from './row.js';
import { renderNextUpNote } from './home.js';

function reminderDefaults() {
  return { id: 'reminder-' + Date.now().toString(36), text: '', done: false, date: null, time: null, created_at: '' };
}

function normalizedReminders(entries) {
  return (entries || state.reminders || []).map(function (entry, idx) {
    const date = entry.date || null;
    return {
      id: entry.id || ('reminder-' + (idx + 1)),
      text: entry.text || '',
      done: entry.done === true,
      date: date,
      time: date ? (entry.time || null) : null,
      created_at: entry.created_at || '',
    };
  });
}

// The due day in words: Today, Tomorrow, else "Sat, Oct 11"; empty with no
// due date. The due time is the row's leading time, not part of this line.
function dueDay(entry) {
  if (!entry.date) return '';
  if (entry.date === localIsoDate()) return 'Today';
  const tomorrow = new Date();
  tomorrow.setDate(tomorrow.getDate() + 1);
  if (entry.date === localIsoDate(tomorrow)) return 'Tomorrow';
  const day = new Date(entry.date + 'T00:00:00');
  return day.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}

// One reminder on the shared row: its due time leads (else a bell), the text
// and the due day, and the done switch saves at once.
function reminderRow(entry, idx) {
  const toggle = buildToggle('reminder-done', entry.done, function (on) {
    reminderEditor.save(state.reminders.map(function (reminder, i) {
      return i === idx ? { ...reminder, done: on } : reminder;
    }));
  });
  toggle.setAttribute('aria-label', entry.done ? 'Mark not done' : 'Mark done');
  const day = dueDay(entry);
  const row = rowEl({
    className: 'reminder-row' + (entry.done ? ' is-done' : ''),
    time: entry.time || null,
    glyph: 'bell',
    title: entry.text,
    meta: entry.done ? (day ? 'Done · ' + day : 'Done') : (day || null),
    openLabel: 'Edit reminder: ' + entry.text,
    onOpen: function (btn) { reminderEditor.open(idx, btn); },
    trail: toggle,
  });
  row.dataset.reminderId = entry.id;
  return row;
}

const reminderEditor = denseListEditor({
  dialog: els.reminderDialog,
  addButton: els.reminderAdd,
  closeButton: els.reminderEditorClose,
  saveButton: els.reminderSave,
  deleteButton: els.reminderDelete,
  titleEl: els.reminderEditorTitle,
  listEl: els.remindersList,
  focusEl: els.reminderText,
  rowIdAttr: 'data-reminder-id',
  titles: { add: 'Add reminder', edit: 'Edit reminder' },
  deleteConfirm: {
    title: 'Delete this reminder?',
    message: 'This reminder will be removed permanently.',
  },
  toasts: { saved: 'Reminders saved', failed: "Couldn't save reminders" },
  defaults: reminderDefaults,
  stage: function (source) {
    return {
      id: source.id,
      text: source.text,
      done: source.done === true,
      date: source.date,
      time: source.time,
      created_at: source.created_at,
    };
  },
  getEntries: function () { return state.reminders; },
  setEntries: function (entries) { state.reminders = entries; },
  normalize: normalizedReminders,
  render: renderReminders,
  populate: function (staged) {
    els.reminderText.value = staged.text;
    setToggleState(els.reminderDueToggle, !!staged.date);
    els.reminderDueFields.hidden = !staged.date;
    els.reminderDate.value = staged.date || '';
    els.reminderTime.value = staged.time || '';
  },
  collect: function (staged) {
    const text = els.reminderText.value.trim();
    if (!text) {
      toast('Enter reminder text', 'error');
      return false;
    }
    staged.text = text.slice(0, 200);
    const hasDue = isToggleOn(els.reminderDueToggle);
    if (hasDue && !els.reminderDate.value) {
      toast('Pick a due date', 'error');
      return false;
    }
    staged.date = hasDue ? els.reminderDate.value : null;
    staged.time = hasDue ? (els.reminderTime.value || null) : null;
  },
  endpoint: '/api/reminders',
  bodyKey: 'entries',
});

// Pending first, by when they are due (undated after dated), then the done
// ones; the editor still edits by the stored index.
function dueKey(entry) {
  return (entry.done ? '1' : '0') + (entry.date ? entry.date + ' ' + (entry.time || '99:99') : '9');
}

export function renderReminders() {
  if (!els.remindersList) return;
  els.remindersList.innerHTML = '';
  state.reminders = normalizedReminders();
  state.reminders
    .map(function (entry, idx) { return { entry: entry, idx: idx }; })
    .sort(function (a, b) { return dueKey(a.entry).localeCompare(dueKey(b.entry)); })
    .forEach(function (item) { els.remindersList.appendChild(reminderRow(item.entry, item.idx)); });
  renderNextUpNote();
}

export async function loadReminders() {
  if (!els.remindersList) return;
  try {
    const body = await jsonApi('/api/reminders');
    state.reminders = (body && body.entries) || [];
    renderNextUpNote('reminders', null);
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    state.reminders = [];
    renderNextUpNote('reminders', friendlyError(exc, 'Failed to load reminders.'));
  }
  renderReminders();
}

export function wireReminders() {
  if (!els.reminderAdd || !els.reminderDialog) return;
  wireToggle(els.reminderDueToggle, function (on) {
    if (els.reminderDueFields) els.reminderDueFields.hidden = !on;
  });
  reminderEditor.wire();
}

// Poll only while the Home tab is active — mirrors wake-alarms.js's
// onWakeAlarmsTab: the reminder list rarely changes server-side except via
// a voice add/complete, which is rare enough not to warrant faster polling.
const scheduleReminders = createPoller(loadReminders);
export function onRemindersTab(tab) {
  scheduleReminders(0);
  if (tab !== 'home') return;
  loadReminders();
  scheduleReminders(10_000);
}
