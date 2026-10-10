/* Weekdays for the schedule editors (#885).
 *
 * The alarm schedules, the blind schedules and the wake alarms each pick the
 * days an entry runs on and summarise them on its row. Their stored shape is
 * the same list of day keys, Monday first, so the list, the summary and the
 * picker live here once.
 */

'use strict';

export const DAYS = [
  ['mon', 'Mon'],
  ['tue', 'Tue'],
  ['wed', 'Wed'],
  ['thu', 'Thu'],
  ['fri', 'Fri'],
  ['sat', 'Sat'],
  ['sun', 'Sun'],
];
export const ALL_DAYS = DAYS.map(function (day) { return day[0]; });

// A row's words for its days: "Every day", "Weekdays", "Weekends", or the
// days themselves ("Mon, Wed").
export function daysSummary(days) {
  const active = ALL_DAYS.filter(function (day) { return days.includes(day); });
  if (active.length === 7) return 'Every day';
  if (active.join(',') === 'mon,tue,wed,thu,fri') return 'Weekdays';
  if (active.join(',') === 'sat,sun') return 'Weekends';
  return DAYS.filter(function (day) { return active.includes(day[0]); })
    .map(function (day) { return day[1]; }).join(', ');
}

// The editor's day picker: one pressed button per day, at least one always
// on. `onChange(days)` gets the new list in week order; the caller stores it
// and re-renders.
export function renderDayPicker(el, days, onChange) {
  if (!el) return;
  el.innerHTML = '';
  DAYS.forEach(function (day) {
    const btn = document.createElement('button');
    const active = days.includes(day[0]);
    btn.type = 'button';
    btn.className = 'alarm-schedule-day' + (active ? ' active' : '');
    btn.textContent = day[1];
    btn.setAttribute('aria-pressed', active ? 'true' : 'false');
    btn.addEventListener('click', function () {
      const current = days.slice();
      const pos = current.indexOf(day[0]);
      if (pos >= 0 && current.length > 1) current.splice(pos, 1);
      else if (pos < 0) current.push(day[0]);
      onChange(ALL_DAYS.filter(function (value) { return current.includes(value); }));
    });
    el.appendChild(btn);
  });
}
