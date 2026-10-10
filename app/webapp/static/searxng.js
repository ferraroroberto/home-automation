/* Search-engine (SearXNG) status, Settings › Voice › Search engine
 * (issue #321; a Settings sheet since #886). Reads GET /api/searxng and
 * brings the container up via POST /api/searxng/start. Lighter than vm.js:
 * starting a search engine
 * isn't destructive, so there is no confirm gate and no stop control, and
 * no snapshot restore (an offline view just re-fetches on the next poll).
 */

'use strict';

import { state, els, toast, reportFetchFailure, reportFetchOk } from './state.js';
import { jsonApi, isAuthRequired } from './api.js';
import { createPoller } from './poll.js';
import { createViewState } from './view-state.js';
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

function statusBadge(sx) {
  if (sx && sx.available === true) return { mod: 'running', text: 'Running' };
  if (sx && sx.container_status && sx.container_status !== 'not_found' && sx.container_status !== 'running') {
    return { mod: 'transition', text: containerWord(sx.container_status) };
  }
  if (sx && sx.container_status === 'running') return { mod: 'transition', text: 'Starting…' };
  return { mod: 'unavailable', text: 'Not started' };
}

function render(sx) {
  if (searxngView.state === 'loading') {
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
    const canStart = !!(sx && sx.container_status !== 'running');
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
    searxngView.set(state.searxng && state.searxng.available ? 'ready' : 'error', { updatedAt: new Date() });
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
    searxngView.set('loading', {});
    renderSearxng();
  }
  try {
    const body = await jsonApi('/api/searxng');
    reportFetchOk('searxng');
    state.searxng = (body && body.searxng) || null;
    searxngView.set(state.searxng && state.searxng.available ? 'ready' : 'error', { updatedAt: new Date() });
    renderSearxng();
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    searxngView.set('error', {});
    reportFetchFailure('searxng', { message: 'live data unavailable' }, 'Search engine');
    renderSearxng();
  }
}

const schedule = createPoller(loadSearxng);

// The Search engine row lives in Settings since #779 (was a Home HA subsection).
export function onSearxngTab(tab) {
  if (tab === 'settings') {
    loadSearxng();
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}
