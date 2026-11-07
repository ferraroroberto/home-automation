/* The car's history sheet (#teslaHistorySheet), opened from the Car card.
 *
 * Reads GET /api/tesla/history (per-week or per-month figures) and
 * GET /api/tesla/sessions (the latest drives and charging sessions). Both
 * serve the local store and never call Tesla, so opening the sheet costs no
 * Fleet API request. Read once per open and per period change, never polled:
 * the store gains a session at most every few minutes.
 *
 * The charts show a fixed window of the last WINDOW periods ending at the
 * current one, a period with no activity as an empty bar, so a gap in use
 * reads as a gap instead of the neighbouring bars closing up.
 *
 * Lifecycle (data-state on #teslaHistoryBody): loading, ready, empty (no
 * drive or charging session stored yet, or collection off), stale (a re-read
 * failed after a good one: the figures stay, labelled), error (no good read).
 */

'use strict';

import { jsonApi, isAuthRequired } from '../api.js';
import { emptyStateEl } from '../empty-state.js';
import { localIsoDate } from '../format.js';
import { sheet } from '../sheet.js';
import {
  createTeslaChargeChart,
  createTeslaKmChart,
  loadChartJs,
  restyleTeslaCharts,
  setTeslaHistoryData,
} from '../energy/charts.js';

// Twelve bars fit a phone-width chart and cover a quarter of weeks or a year
// of months.
const WINDOW = 12;
const SESSION_LIMIT = 5;

const $ = (id) => document.getElementById(id);

let period = 'week';
let kmChart = null;
let chargeChart = null;
let lastGood = null;  // {history, sessions, at}
let loadSeq = 0;

const historySheet = sheet($('teslaHistorySheet'), {
  model: 'instant',
  closeButton: $('teslaHistoryClose'),
  doneButton: $('teslaHistoryDone'),
});

function setState(state) {
  $('teslaHistoryBody').dataset.state = state;
}

function showNote(node) {
  $('teslaHistoryNote').replaceChildren(node);
  $('teslaHistoryNote').hidden = false;
  $('teslaHistoryData').hidden = true;
}

// The window's period starts as local "YYYY-MM-DD", oldest first; the server
// keys its buckets by the same local calendar start (Monday, or the 1st).
function windowStarts() {
  const now = new Date();
  const starts = [];
  if (period === 'week') {
    const monday = new Date(now.getFullYear(), now.getMonth(), now.getDate() - ((now.getDay() + 6) % 7));
    for (let i = WINDOW - 1; i >= 0; i--) {
      starts.push(new Date(monday.getFullYear(), monday.getMonth(), monday.getDate() - 7 * i));
    }
  } else {
    for (let i = WINDOW - 1; i >= 0; i--) starts.push(new Date(now.getFullYear(), now.getMonth() - i, 1));
  }
  return starts;
}

function periodLabel(d) {
  return period === 'week'
    ? d.toLocaleDateString([], { day: 'numeric', month: 'short' })
    : d.toLocaleDateString([], { month: 'short', year: '2-digit' });
}

function windowBuckets(serverBuckets) {
  const byStart = {};
  serverBuckets.forEach(function (b) { byStart[String(b.start).slice(0, 10)] = b; });
  return windowStarts().map(function (d) {
    const b = byStart[localIsoDate(d)] || {};
    return {
      label: periodLabel(d),
      km: b.km || 0,
      charged_kwh: b.charged_kwh || 0,
      charged_home_kwh: b.charged_home_kwh || 0,
      kwh_per_100km: b.kwh_per_100km == null ? null : b.kwh_per_100km,
      drives: b.drives || 0,
      charging_sessions: b.charging_sessions || 0,
    };
  });
}

function fmtKwh(v) { return Number(v).toFixed(1) + ' kWh'; }

function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

// A figure and its unit; the unit never breaks inside itself.
function setStat(el, value, unit) {
  if (value == null) {
    el.textContent = '—';
    return;
  }
  const unitEl = document.createElement('span');
  unitEl.className = 'tesla-stat-unit';
  unitEl.textContent = unit;
  el.replaceChildren(value + ' ', unitEl);
}

function renderStats(current) {
  setStat($('teslaStatKm'), String(Math.round(current.km)), 'km');
  setStat($('teslaStatCharged'), current.charged_kwh.toFixed(1), 'kWh');
  setStat($('teslaStatEff'), current.kwh_per_100km == null ? null : current.kwh_per_100km.toFixed(1), 'kWh/100 km');
  const parts = [
    (period === 'week' ? 'This week' : 'This month') + ' so far',
    plural(current.drives, 'drive'),
    plural(current.charging_sessions, 'charging session'),
  ];
  if (current.charged_kwh > 0) parts.push(fmtKwh(current.charged_home_kwh) + ' at home');
  $('teslaStatsCaption').textContent = parts.join(' · ');
}

function fmtWhen(tsSeconds) {
  const d = new Date(tsSeconds * 1000);
  return d.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' }) + ', ' +
    d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function pctSpan(a, b) {
  return a == null || b == null ? null : Math.round(a) + ' → ' + Math.round(b) + ' %';
}

const PLACE_WORDS = { home: 'Home', away: 'Away', unknown: 'Place unknown' };

function sessionRow(title, metaParts, avatar) {
  const li = document.createElement('li');
  li.className = 'action-row';
  li.innerHTML =
    '<div class="action-row-main has-avatar">' +
    '  <span class="row-avatar" aria-hidden="true"><svg class="icon" aria-hidden="true"><use href="#i-' + avatar + '"></use></svg></span>' +
    '  <span class="action-row-text">' +
    '    <span class="action-row-title"></span>' +
    '    <span class="action-row-meta"><span class="action-row-meta-text"></span></span>' +
    '  </span>' +
    '</div>';
  li.querySelector('.action-row-title').textContent = title;
  li.querySelector('.action-row-meta-text').textContent = metaParts.filter(Boolean).join(' · ');
  return li;
}

function noneRow(text) {
  const li = document.createElement('li');
  li.className = 'action-row tesla-session-none muted small';
  li.textContent = text;
  return li;
}

function renderSessions(sessions) {
  const charges = sessions.charging.map(function (c) {
    const kwh = c.kwh_added == null ? 'Energy unknown' : fmtKwh(c.kwh_added);
    return sessionRow(kwh + ' · ' + (PLACE_WORDS[c.location] || PLACE_WORDS.unknown), [
      fmtWhen(c.start_ts),
      pctSpan(c.start_pct, c.end_pct),
      c.peak_kw ? 'peak ' + Number(c.peak_kw).toFixed(1) + ' kW' : null,
    ], 'battery-charging');
  });
  $('teslaChargeList').replaceChildren(...(charges.length ? charges : [noneRow('No charging sessions yet')]));

  const drives = sessions.drives.map(function (d) {
    return sessionRow(Number(d.km).toFixed(1) + ' km', [
      fmtWhen(d.start_ts),
      d.energy_kwh == null ? null : '~' + fmtKwh(d.energy_kwh),
      pctSpan(d.start_pct, d.end_pct),
    ], 'car');
  });
  $('teslaDriveList').replaceChildren(...(drives.length ? drives : [noneRow('No drives yet')]));
}

async function renderData(data, stale) {
  const buckets = windowBuckets(data.history.buckets);
  renderStats(buckets[buckets.length - 1]);
  renderSessions(data.sessions);
  $('teslaHistoryData').hidden = false;
  if (stale) {
    const at = data.at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    $('teslaHistoryNote').textContent = 'Last updated ' + at + ' · live data unavailable';
    $('teslaHistoryNote').hidden = false;
  } else {
    $('teslaHistoryNote').replaceChildren();
    $('teslaHistoryNote').hidden = true;
  }
  setState(stale ? 'stale' : 'ready');
  // The charts are built once the sheet is open: Chart.js sizes to a visible box.
  await loadChartJs();
  if (!kmChart) kmChart = createTeslaKmChart($('teslaKmChart'));
  if (!chargeChart) chargeChart = createTeslaChargeChart($('teslaChargeChart'));
  setTeslaHistoryData(kmChart, chargeChart, buckets);
}

async function loadHistory() {
  const seq = ++loadSeq;
  const forPeriod = period;
  if (!lastGood || lastGood.period !== forPeriod) {
    setState('loading');
    showNote(emptyStateEl('chart-line', 'Reading car history…'));
  }
  let history;
  let sessions;
  try {
    [history, sessions] = await Promise.all([
      jsonApi('/api/tesla/history?period=' + forPeriod),
      jsonApi('/api/tesla/sessions?limit=' + SESSION_LIMIT),
    ]);
  } catch (exc) {
    if (seq !== loadSeq || isAuthRequired(exc)) return;
    if (lastGood && lastGood.period === forPeriod) {
      await renderData(lastGood, true);
    } else {
      setState('error');
      showNote(emptyStateEl('triangle-alert', 'Car history is unavailable right now', {
        actionLabel: 'Retry', onAction: loadHistory,
      }));
    }
    return;
  }
  if (seq !== loadSeq) return;  // the period changed while this read was out
  const nothing = !history.buckets.length && !sessions.drives.length && !sessions.charging.length;
  if (!history.configured || nothing) {
    lastGood = null;
    setState('empty');
    const message = !history.configured
      ? 'The car is not set up'
      : history.sampler_enabled ? 'No drives or charging sessions yet' : 'Car data collection is off';
    showNote(emptyStateEl('car', message));
    return;
  }
  lastGood = { history, sessions, period: forPeriod, at: new Date() };
  await renderData(lastGood, false);
}

function setPeriod(next) {
  if (next === period) return;
  period = next;
  $('teslaHistoryPeriod').querySelectorAll('.segmented-item').forEach(function (btn) {
    btn.setAttribute('aria-pressed', String(btn.dataset.period === period));
  });
  loadHistory();
}

export function restyleTeslaHistoryCharts() {
  restyleTeslaCharts(kmChart, chargeChart);
}

export function wireTeslaHistory() {
  const opener = $('teslaHistoryOpen');
  if (!opener) return;
  opener.addEventListener('click', function () {
    historySheet.open(opener);
    loadHistory();
  });
  $('teslaHistoryPeriod').addEventListener('click', function (ev) {
    const btn = ev.target.closest('.segmented-item');
    if (btn) setPeriod(btn.dataset.period);
  });
}
