/* Reminders — bidirectional voice/app free-text checklist (issue #314).
 *
 * Distinct from wake-alarms.js: a wake alarm rings at a set time; a reminder
 * is a checklist item, optionally due on a date/time, completed by toggling
 * it done rather than dismissing an alert. Dense-collection card (flat
 * list-row + staged edit dialog, home-automation#409) using the shared
 * denseListEditor shell — see security-schedules.js for the sibling impl.
 */

'use strict';

import { state, els, toast } from './state.js';
import { jsonApi, isAuthRequired } from './api.js';
import { isToggleOn, setToggleState, wireToggle } from './toggle.js';
import { denseListEditor, renderSummaryRow } from './dense-editor.js';
import { createPoller } from './poll.js';

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

function renderRemindersCount() {
  if (!els.remindersCount) return;
  const pending = (state.reminders || []).filter(function (e) { return !e.done; }).length;
  if (pending > 0) {
    els.remindersCount.textContent = pending + ' pending';
    els.remindersCount.hidden = false;
  } else {
    els.remindersCount.hidden = true;
  }
}

function dueSummary(entry) {
  if (!entry.date) return '';
  const day = new Date(entry.date + 'T00:00:00');
  const dateStr = day.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
  return entry.time ? dateStr + ' · ' + entry.time : dateStr;
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

export function renderReminders() {
  if (!els.remindersList || !els.remindersNote) return;
  els.remindersList.innerHTML = '';
  state.reminders = normalizedReminders();
  renderRemindersCount();
  if (!state.reminders.length) {
    els.remindersNote.hidden = false;
    els.remindersNote.textContent = 'No reminders.';
    return;
  }
  els.remindersNote.hidden = true;

  state.reminders.forEach(function (entry, idx) {
    els.remindersList.appendChild(renderSummaryRow({
      id: entry.id,
      idAttr: 'reminderId',
      rowClass: 'reminder-row' + (entry.done ? ' is-done' : ''),
      title: entry.text,
      meta: dueSummary(entry),
      openLabel: 'Edit reminder: ' + entry.text,
      onOpen: function (main) { reminderEditor.open(idx, main); },
      toggleName: 'reminder-done',
      toggleOn: entry.done,
      toggleLabel: entry.done ? 'Mark not done' : 'Mark done',
      onToggle: function (on) {
        const proposed = state.reminders.map(function (reminder, i) {
          return i === idx ? { ...reminder, done: on } : reminder;
        });
        reminderEditor.save(proposed);
      },
    }));
  });
}

export async function loadReminders() {
  if (!els.remindersList) return;
  try {
    const body = await jsonApi('/api/reminders');
    state.reminders = (body && body.entries) || [];
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    state.reminders = [];
    if (els.remindersNote) {
      els.remindersNote.hidden = false;
      els.remindersNote.textContent = exc.message || 'Failed to load reminders.';
    }
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
