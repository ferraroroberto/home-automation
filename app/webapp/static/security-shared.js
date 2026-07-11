/* Shared helpers for the RISCO detector-scoped editors (security-scene.js,
 * security-override.js): both build detector options from the same
 * already-loaded security state.
 */

'use strict';

import { state } from './state.js';

export function detectorOptions() {
  const zones = (state.security && state.security.zones) || [];
  return zones.map(function (zone) {
    return { id: zone.id, name: (zone.display_name || zone.name || String(zone.id)) };
  });
}
