/* Shared switch-button helpers (issue #360).
 *
 * The app's one canonical boolean control is the `.toggle` button — a
 * shadcn-style track + knob, `role="switch"`, green when on (see
 * units.js's original power-toggle markup, styles.css's `.toggle` rules).
 * Every native checkbox-type boolean setting (alarm schedules, scene-capture
 * pairings, notification-preference lists, the temperature-rule/presence
 * toggles, and the DHCP reservation picker) now builds/reads this same
 * button instead of duplicating the on/off markup at each call site.
 */

'use strict';

export function toggleMarkup(on) {
  return '<span class="knob"></span><span class="toggle-label">' + (on ? 'ON' : 'OFF') + '</span>';
}

// String form for call sites that build rows via innerHTML template
// literals (e.g. units.js's schedule-entry cards) rather than DOM nodes.
export function toggleHtml(className, on) {
  return '<button type="button" class="toggle' + (className ? ' ' + className : '') + (on ? ' on' : '') +
    '" role="switch" aria-checked="' + (on ? 'true' : 'false') + '">' + toggleMarkup(on) + '</button>';
}

export function setToggleState(btn, on) {
  if (!btn) return;
  btn.classList.toggle('on', !!on);
  btn.setAttribute('aria-checked', on ? 'true' : 'false');
  const label = btn.querySelector('.toggle-label');
  if (label) label.textContent = on ? 'ON' : 'OFF';
}

export function isToggleOn(btn) {
  return !!btn && btn.classList.contains('on');
}

// Builds a standalone <button class="toggle" role="switch"> node for
// dynamically-rendered rows (schedule/pairing/override/wake-alarm entries).
// `onToggle(next)` receives the new boolean state; the caller owns persistence.
export function buildToggle(className, on, onToggle) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'toggle' + (className ? ' ' + className : '') + (on ? ' on' : '');
  btn.setAttribute('role', 'switch');
  btn.setAttribute('aria-checked', on ? 'true' : 'false');
  btn.innerHTML = toggleMarkup(on);
  btn.addEventListener('click', function () {
    const next = !isToggleOn(btn);
    setToggleState(btn, next);
    if (onToggle) onToggle(next);
  });
  return btn;
}

// Wires a click handler onto an already-present static button (index.html
// notify-prefs / presence toggles): flips the visual state, then calls
// `onChange(next)` so the caller can persist.
export function wireToggle(btn, onChange) {
  if (!btn) return;
  btn.addEventListener('click', function () {
    const next = !isToggleOn(btn);
    setToggleState(btn, next);
    if (onChange) onChange(next);
  });
}
