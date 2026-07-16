/* Shared tab view-state machine (issue #452).
 *
 * Every tab-controller module (units/energy/plugs/lights/network/security/
 * cameras/presence/ups/vm) used to hand-roll the same trio: a
 * `<name>ViewState` / `<name>UpdatedAt` / `<name>LiveUnavailable` module-var
 * set, a `set<Name>ViewState(next, opts)` mutator, and a byte-identical
 * `lastUpdatedLabel()`. `createViewState(snapshotKey)` hides those vars in a
 * closure (same shape as poll.js's `createPoller`) and returns
 * `{state, set, liveUnavailable, lastUpdatedLabel}`. `snapshotKey` names the
 * `state.snapshotUpdatedAt` entry used as the label's fallback timestamp —
 * omit it for tabs without a persisted snapshot.
 */

'use strict';

import { state } from './state.js';

export function createViewState(snapshotKey) {
  let current = 'idle';
  let updatedAt = null;
  let liveUnavailable = false;
  return {
    get state() { return current; },
    get liveUnavailable() { return liveUnavailable; },
    set(next, opts) {
      current = next;
      if (opts && opts.updatedAt) updatedAt = opts.updatedAt;
      if (opts && Object.prototype.hasOwnProperty.call(opts, 'liveUnavailable')) {
        liveUnavailable = opts.liveUnavailable;
      }
    },
    lastUpdatedLabel() {
      const raw = updatedAt || (snapshotKey ? state.snapshotUpdatedAt[snapshotKey] : null);
      const updated = raw instanceof Date ? raw : new Date(raw || '');
      if (Number.isNaN(updated.getTime())) return 'Last updated earlier';
      return 'Last updated ' + updated.toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
      });
    },
  };
}
