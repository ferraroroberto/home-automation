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

import { reportFetchFailure, state } from './state.js';
import { emptyStateEl } from './empty-state.js';
import { snapshotLabel } from './snapshots.js';

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

// The thin "showing older data" line every tab paints while stale (issue #571
// — hand-rolled in six modules, each with its own near-identical CSS class).
// The shared look is `.stale-note`; `extraClass` carries only the per-tab
// margin modifiers that genuinely differ (security, ups).
export function staleNoteEl(text, extraClass) {
  const note = document.createElement('p');
  note.className = 'muted small stale-note' + (extraClass ? ' ' + extraClass : '');
  note.textContent = text;
  return note;
}

/** The stale line's text: a snapshot-backed tab says "Last saved …" until a
 *  live fetch has actually failed; a tab with no snapshot always says the
 *  live source is unavailable. */
export function staleText(view, snapshotKey) {
  if (snapshotKey && !view.liveUnavailable) return snapshotLabel(snapshotKey);
  return view.lastUpdatedLabel() + ' · live data unavailable';
}

/* Render one tab's loading / empty / error / stale feedback into its dedicated
 * feedback host (issue #571). Every tab with such a host — AC, energy, plugs,
 * network, security — used to hand-roll the identical branch ladder: stamp
 * `data-state`, clear the host, unhide it, then append either an `emptyStateEl`
 * (loading/empty/error) or the stale note, falling through to `hidden = true`
 * when the view is ready.
 *
 * `opts`:
 *   paneEl       element carrying `data-state` (defaults to the host itself —
 *                the Plugs card has no separate pane)
 *   ariaBusy     also stamp `aria-busy` on that element (AC + energy do)
 *   icon         glyph for the empty/error blocks
 *   loadingIcon  glyph for the loading block (defaults to `icon`)
 *   loadingLabel / emptyLabel / errorLabel   copy per branch; omit `emptyLabel`
 *                for a tab that never reports 'empty'
 *   snapshotKey  snapshot whose "Last saved" label the stale line falls back to
 *   staleClass   extra class on the stale note (per-tab margin modifier)
 *   onRetry      handler behind the empty/error blocks' Retry button
 */
export function renderFeedback(view, hostEl, opts) {
  if (!hostEl) return;
  const options = opts || {};
  const stateEl = options.paneEl || hostEl;
  stateEl.dataset.state = view.state;
  if (options.ariaBusy) {
    stateEl.setAttribute('aria-busy', view.state === 'loading' ? 'true' : 'false');
  }
  hostEl.innerHTML = '';
  hostEl.hidden = false;
  const retry = options.onRetry
    ? { actionLabel: 'Retry', onAction: options.onRetry }
    : null;

  if (view.state === 'loading') {
    hostEl.appendChild(emptyStateEl(options.loadingIcon || options.icon, options.loadingLabel));
    return;
  }
  if (view.state === 'empty' && options.emptyLabel) {
    hostEl.appendChild(emptyStateEl(options.icon, options.emptyLabel, retry));
    return;
  }
  if (view.state === 'error') {
    hostEl.appendChild(emptyStateEl(options.icon, options.errorLabel, retry));
    return;
  }
  if (view.state === 'stale') {
    hostEl.appendChild(staleNoteEl(staleText(view, options.snapshotKey), options.staleClass));
    return;
  }
  hostEl.hidden = true;
}

/* Move one tab's view into its post-failure state (issue #571): degrade to
 * 'stale' when last-good data is still on screen and 'error' when there is
 * nothing to fall back to, toast the failure transition once, then repaint.
 * `hasData` is the tab's own "is there anything left to show" test; `scope` and
 * `label` are `reportFetchFailure`'s; `render` does the tab's repaint plus any
 * extra it needs (disabling stale actions, updating a meta line).
 */
export function markTabFailure(view, opts) {
  view.set(opts.hasData ? 'stale' : 'error', { liveUnavailable: true });
  reportFetchFailure(opts.scope, { message: 'live data unavailable' }, opts.label);
  if (opts.render) opts.render();
}
