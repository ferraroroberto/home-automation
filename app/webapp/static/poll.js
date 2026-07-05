/* Shared tab-poll timer helper (issue #369).
 *
 * Every tab-aware controller module (network/lights/energy/plugs/security/ups/vm)
 * used to duplicate its own `<name>Timer` module variable plus a verbatim
 * `schedule(ms)` function that (re)started a `setInterval` on the module's load
 * function. `createPoller(fn)` hides that interval handle in its own closure and
 * returns a `schedule(ms)` with the same contract every caller already relied on:
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
