/* Shared tab-poll timer helper.
 *
 * `createPoller(fn)` is the one place every tab-aware controller module
 * (network/lights/energy/plugs/security/ups/vm) gets its poll-timer handle,
 * built on the vendored `startPoll` + `singleFlight`, so every poll is
 * self-scheduling (the next read starts `ms` after the last one settled),
 * single-flight, and silent on a hidden tab apart from one catch-up read on
 * the return. It returns a `schedule(ms)` with the same contract for every
 * caller: `ms > 0` (re)starts the poll, stopping any previous one first, with
 * its first read `ms` later; `ms <= 0` stops it. `fn` returns its read's
 * promise, so a slow server stretches the gap instead of stacking requests.
 */

'use strict';

import { singleFlight, startPoll } from './_vendored/poll/poll.js';

export function createPoller(fn) {
  const read = singleFlight(fn);
  let handle = null;
  return function schedule(ms) {
    if (handle) handle.stop();
    handle = ms > 0 ? startPoll(read, ms) : null;
  };
}
