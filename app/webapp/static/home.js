/* The Home tab's dashboard parts (#885, Step 7/8 of #872).
 *
 * Home is a pure dashboard: the House glance card, the Climate group and
 * Next up. Each card's rows belong to the module that owns the fact (the arm
 * control to security-alarm.js, the flow to energy.js, who is home to
 * presence.js, the AC rows to units.js, the Next up rows to wake-alarms.js
 * and reminders.js). This module owns only what several of them share:
 *
 * - the House card's exception chips. "Is the house OK?" A normal house shows
 *   none; an exception (a triggered alarm, detector trouble, the UPS on
 *   battery) is a chip in the card's head, in the tone map of design.md's
 *   status chip. Each chip opens the tab that owns the fact. The alarm and
 *   the UPS report their parts separately, so the chips are rebuilt from
 *   every part, danger first, the way head-status.js builds a header line.
 * - Next up's one note: nothing coming up, or a list that failed to load.
 */

'use strict';

import { state, els } from './state.js';

const TONE_ORDER = { danger: 0, attention: 1, accent: 2 };

// source -> [{ text, tone, label, onOpen(btn) }]
const houseParts = new Map();

// Report (or, with an empty list or null, withdraw) one source's exceptions,
// then rebuild the House card's chips.
export function setHousePart(source, exceptions) {
  if (exceptions && exceptions.length) houseParts.set(source, exceptions);
  else houseParts.delete(source);
  if (!els.houseChips) return;
  const all = [];
  houseParts.forEach(function (list) { list.forEach(function (x) { all.push(x); }); });
  all.sort(function (a, b) {
    return (a.tone in TONE_ORDER ? TONE_ORDER[a.tone] : 3) - (b.tone in TONE_ORDER ? TONE_ORDER[b.tone] : 3);
  });
  els.houseChips.innerHTML = '';
  all.forEach(function (x) {
    // A chip that opens something keeps its status colours on the chip shape
    // (design.md reference pill), a real button grown to 44px (.chip-button).
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'chip chip-button house-chip';
    if (x.tone) btn.dataset.tone = x.tone;
    btn.textContent = x.text;
    if (x.label) btn.setAttribute('aria-label', x.label);
    btn.addEventListener('click', function () { x.onOpen(btn); });
    els.houseChips.appendChild(btn);
  });
}

// The lists Next up shows, and the ones read so far; source -> message for a
// list that failed to load.
const NEXT_UP_SOURCES = ['alarms', 'timers', 'reminders'];
const nextUpLoaded = new Set();
const nextUpErrors = new Map();

// Next up's one line under its rows: why a list is missing, else, once every
// list has been read (never on a first paint still waiting for one), what to
// do when there is nothing coming up. `source` records one list's read
// first: its failure message, or null when it loaded.
export function renderNextUpNote(source, error) {
  if (source) {
    nextUpLoaded.add(source);
    if (error) nextUpErrors.set(source, error);
    else nextUpErrors.delete(source);
  }
  if (!els.nextUpNote) return;
  let text = '';
  if (nextUpErrors.size) {
    text = Array.from(nextUpErrors.values()).join(' ');
  } else if (NEXT_UP_SOURCES.every(function (s) { return nextUpLoaded.has(s); })
    && !(state.wakeAlarms || []).length && !(state.wakeTimers || []).length
    && !(state.reminders || []).length) {
    text = 'Nothing coming up. Add an alarm, a timer or a reminder.';
  }
  els.nextUpNote.textContent = text;
  els.nextUpNote.hidden = !text;
}
