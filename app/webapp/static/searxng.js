/* Search-engine (SearXNG) status, Settings › Voice › Search engine
 * (a Settings sheet). Reads GET /api/searxng and
 * brings the container up via POST /api/searxng/start. Lighter than vm.js:
 * starting a search engine
 * isn't destructive, so there is no confirm gate and no stop control, and
 * no snapshot restore (an offline view just re-fetches on the next poll).
 */

'use strict';

import { state, els, toast, reportFetchFailure, reportFetchOk } from './state.js';
import { jsonApi, isAuthRequired } from './api.js';
import { createTabPollGate, createViewState, ViewState } from './view-state.js';
import { friendlyError } from './format.js';

const POLL_MS = 30_000;

let busy = false;
const searxngView = createViewState();

// The Settings row's value, muted while it runs or starts; only "Not
// started" is an exception, in the attention tone.
function renderSummaryState(text, modifier) {
  if (!els.searxngSummaryState) return;
  els.searxngSummaryState.textContent = text;
  if (modifier === 'unavailable') els.searxngSummaryState.dataset.tone = 'attention';
  else delete els.searxngSummaryState.dataset.tone;
}

// Docker's own state words (exited, paused, restarting...), sentence case.
function containerWord(status) {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

// The `phase` /api/searxng derives from Docker's status and the /healthz probe
// (SearxngPhase in src/voice/searxng_client.py); this view never reads
// Docker's `container_status` word to decide anything, only to show it.
const SearxngPhase = Object.freeze({
  SERVING: 'serving',
  UNREACHABLE: 'unreachable',
  DOWN: 'down',
  NOT_FOUND: 'not_found',
});

function statusBadge(sx) {
  if (sx && sx.available === true) return { mod: 'running', text: 'Running' };
  if (sx && sx.phase === SearxngPhase.DOWN && sx.container_status) {
    return { mod: 'transition', text: containerWord(sx.container_status) };
  }
  if (sx && sx.phase === SearxngPhase.UNREACHABLE) return { mod: 'transition', text: 'Starting…' };
  return { mod: 'unavailable', text: 'Not started' };
}

function render(sx) {
  if (searxngView.state === ViewState.LOADING) {
    renderSummaryState('Reading…', 'transition');
    if (els.searxngStartBtn) els.searxngStartBtn.hidden = true;
    return;
  }
  const badge = statusBadge(sx);
  renderSummaryState(badge.text, badge.mod);
  if (els.searxngNote) {
    els.searxngNote.textContent = (sx && sx.available)
      ? 'Answers "Okay Nabu" web-search questions (SearXNG, self-hosted, no cloud).'
      : friendlyError(sx && sx.error, 'Search engine is unavailable.');
  }
  if (els.searxngStartBtn) {
    const canStart = !!(sx && sx.phase !== SearxngPhase.SERVING && sx.phase !== SearxngPhase.UNREACHABLE);
    els.searxngStartBtn.hidden = !canStart;
    els.searxngStartBtn.disabled = busy;
  }
}

async function onStart() {
  if (busy) return;
  busy = true;
  render(state.searxng);
  toast('Starting the search engine…', 'pending');
  try {
    const body = await jsonApi('/api/searxng/start', { method: 'POST' });
    state.searxng = (body && body.searxng) || state.searxng;
    reportFetchOk('searxng');
    searxngView.set(state.searxng && state.searxng.available ? ViewState.READY : ViewState.ERROR, { updatedAt: new Date() });
    toast('Search engine starting', 'success');
  } catch (exc) {
    if (!isAuthRequired(exc)) {
      toast("Couldn't start the search engine", 'error');
    }
  } finally {
    busy = false;
    render(state.searxng);
    // The container takes a few seconds to answer /healthz — re-poll shortly.
    setTimeout(loadSearxng, 4_000);
  }
}

export function wireSearxng() {
  if (!els.searxngStartBtn) return;
  els.searxngStartBtn.addEventListener('click', function (ev) {
    ev.preventDefault();
    ev.stopPropagation();
    onStart();
  });
}

export function renderSearxng() {
  render(state.searxng);
}

export async function loadSearxng() {
  if (!state.searxng) {
    searxngView.set(ViewState.LOADING, {});
    renderSearxng();
  }
  try {
    const body = await jsonApi('/api/searxng');
    reportFetchOk('searxng');
    state.searxng = (body && body.searxng) || null;
    searxngView.set(state.searxng && state.searxng.available ? ViewState.READY : ViewState.ERROR, { updatedAt: new Date() });
    renderSearxng();
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    searxngView.set(ViewState.ERROR, {});
    reportFetchFailure('searxng', { message: 'live data unavailable' }, 'Search engine');
    renderSearxng();
  }
}

// The Search engine row lives in Settings.
export const onSearxngTab = createTabPollGate(loadSearxng, 'settings', POLL_MS);
