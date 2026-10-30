/* Shared tab-poll timer helper.
 *
 * `createPoller(fn)` is the one place every tab-aware controller module
 * (network/lights/energy/plugs/security/ups/vm) gets its poll-timer handle:
 * it hides the interval handle in its own closure and returns a
 * `schedule(ms)` with the same contract for every caller:
 * `ms > 0` (re)starts a `setInterval(fn, ms)`, clearing any previous one first;
 * `ms <= 0` clears it and leaves polling stopped.
 */

'use strict';

export function createPoller(fn) {
  let timer = null;
  return function schedule(ms) {
    if (timer) clearInterval(timer);
    timer = ms > 0 ? setInterval(fn, ms) : null;
  };
}
