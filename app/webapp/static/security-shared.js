/* Shared helpers for the RISCO detector-scoped editors (security-scene.js,
 * security-override.js): both build detector options from the same
 * already-loaded security state, resolve a zone id to its label the same way,
 * and repopulate a <select> the same way (issue #571 — these last two used to
 * be byte-identical copies in both editors).
 */

'use strict';

import { state } from './state.js';

export function detectorOptions() {
  const zones = (state.security && state.security.zones) || [];
  return zones.map(function (zone) {
    return { id: zone.id, name: (zone.display_name || zone.name || String(zone.id)) };
  });
}

export function detectorName(zoneId) {
  const detector = detectorOptions().find(function (entry) { return Number(entry.id) === Number(zoneId); });
  return detector ? detector.name : 'Unknown detector';
}

// Replace a <select>'s options with `[{value, label}]` and select `value`.
export function setSelectOptions(select, options, value) {
  select.innerHTML = '';
  options.forEach(function (entry) {
    const option = document.createElement('option');
    option.value = String(entry.value);
    option.textContent = entry.label;
    select.appendChild(option);
  });
  select.value = value == null ? '' : String(value);
}
