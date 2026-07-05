/* Shared helpers for the RISCO detector-scoped editors (security-scene.js,
 * security-override.js): both build a detector dropdown from the same
 * already-loaded security state and render options into a plain <select>.
 */

'use strict';

import { state } from './state.js';

export function detectorOptions() {
  const zones = (state.security && state.security.zones) || [];
  return zones.map(function (zone) {
    return { id: zone.id, name: (zone.display_name || zone.name || String(zone.id)) };
  });
}

export function buildSelect(className, options, value, onChange) {
  const sel = document.createElement('select');
  sel.className = 'select-native ' + className;
  options.forEach(function (opt) {
    const o = document.createElement('option');
    o.value = String(opt.value);
    o.textContent = opt.label;
    sel.appendChild(o);
  });
  sel.value = value == null ? '' : String(value);
  sel.addEventListener('change', onChange);
  return sel;
}
