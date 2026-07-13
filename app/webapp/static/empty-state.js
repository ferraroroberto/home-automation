/* Home Automation — canonical empty-state block (issue #362).
 *
 * The `icon()` helper this composes lives in the vendored fleet component at
 * _vendored/icons/icons.js (adopted verbatim in issue #407); this file keeps
 * only the app-specific empty-state builder, whose action button rides the
 * app's `range-tab` class rather than the fleet empty-state component's CSS.
 */

'use strict';

import { icon } from './_vendored/icons/icons.js';

// A muted glyph, a one-line reason, and an optional action button — the one
// pattern every list/grid that can be legitimately empty (Lights with no
// reachable fixtures, etc.) renders instead of a silent blank area.
// `opts.actionLabel` + `opts.onAction` are both optional; omit them for a
// message-only empty state.
export function emptyStateEl(name, message, opts) {
  const wrap = document.createElement('div');
  wrap.className = 'empty-state';
  wrap.innerHTML = icon(name, 'empty-state-icon');
  const msg = document.createElement('p');
  msg.className = 'empty-state-message';
  msg.textContent = message;
  wrap.appendChild(msg);
  if (opts && opts.actionLabel) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'range-tab empty-state-action';
    btn.textContent = opts.actionLabel;
    if (opts.onAction) btn.addEventListener('click', opts.onAction);
    wrap.appendChild(btn);
  }
  return wrap;
}
