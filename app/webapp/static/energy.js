/* Energy data + Energy-tab controller.
 *
 * Owns everything energy: the compact Home tile, the Energy-tab hero numbers,
 * the live flowing chart, and the hourly/daily/monthly aggregate bars.
 *
 * Cadence is tab-aware: the live snapshot polls fast (LIVE_MS) only while the
 * Energy tab is open, falling back to SLOW_MS elsewhere so the Home tile still
 * updates without hammering the SMA devices. Charts are created lazily on the
 * first Energy-tab visit (Chart.js is a heavy global). */

'use strict';

import { state, els } from './state.js';
import { jsonApi } from './api.js';
import {
  createLiveChart, setLiveData, pushLivePoint,
  createAggChart, setAggData, restyle,
} from './charts.js';

const LIVE_MS = 10_000;
const SLOW_MS = 30_000;
const LIVE_WINDOW_MIN = 60;   // minutes of recent history seeded into the live chart
const LIVE_MAX_POINTS = 400;  // ring-buffer cap on the live chart

let energyTimer = null;

// --------------------------------------------------------------- formatting
function fmtW(v) {
  return v == null ? '—' : Math.round(Number(v)) + ' W';
}

function fmtSignedW(v) {
  if (v == null) return '—';
  const rounded = Math.round(Number(v));
  if (rounded === 0) return '0 W';
  return (rounded > 0 ? '+' : '−') + Math.abs(rounded) + ' W';
}

function nowLabel() {
  return new Date().toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

// ----------------------------------------------------- render a live snapshot
export function renderEnergy(e) {
  // --- Home compact tile (Production / Consumption / Net grid) — mirrors
  //     the Energy-tab heroes. ---
  els.enPv.textContent = e.inverter_reachable ? fmtW(e.pv_power_w) : 'asleep';
  els.enHouse.textContent = fmtW(e.house_consumption_w);
  els.enSurplus.textContent = fmtSignedW(e.pv_surplus_w);
  if (els.enUpdated) els.enUpdated.textContent = 'Updated ' + nowLabel();
  els.energyFlow.hidden = false;

  // --- Energy-tab hero numbers ---
  els.heroProd.textContent = e.inverter_reachable ? fmtW(e.pv_power_w) : 'asleep';
  els.heroCons.textContent = fmtW(e.house_consumption_w);
  els.heroNet.textContent = fmtSignedW(e.pv_surplus_w);

  // --- append to the live chart (only once it has been created) ---
  if (state.liveChart) {
    const pv = e.inverter_reachable ? e.pv_power_w : null;
    pushLivePoint(
      state.liveChart, Math.floor(Date.now() / 1000),
      pv, e.house_consumption_w, e.pv_surplus_w, LIVE_MAX_POINTS,
    );
    els.liveMeta.textContent = '· ' + nowLabel();
  }
}

export async function loadEnergy() {
  try {
    const body = await jsonApi('/api/energy');
    if (body) renderEnergy(body);
  } catch (_) {
    // Energy is secondary to unit control — fail quietly, keeping the last
    // rendered values (and staying hidden if it never loaded).
  }
}

// --------------------------------------------------------------- charts
function ensureCharts() {
  if (!state.liveChart) state.liveChart = createLiveChart(els.liveChart);
  if (!state.aggChart) state.aggChart = createAggChart(els.aggChart);
}

async function loadLiveHistory() {
  try {
    const body = await jsonApi('/api/energy/history?minutes=' + LIVE_WINDOW_MIN);
    const samples = (body && body.samples) || [];
    setLiveData(state.liveChart, samples);
  } catch (_) { /* leave whatever the live poll has gathered */ }
}

async function loadAggregate(range) {
  try {
    const body = await jsonApi('/api/energy/aggregate?range=' + encodeURIComponent(range));
    const buckets = (body && body.buckets) || [];
    setAggData(state.aggChart, buckets);
    els.aggEmpty.hidden = buckets.length > 0;
  } catch (_) {
    els.aggEmpty.hidden = false;
  }
}

function setRange(range) {
  state.range = range;
  els.rangeHourly.classList.toggle('active', range === 'hourly');
  els.rangeDaily.classList.toggle('active', range === 'daily');
  els.rangeMonthly.classList.toggle('active', range === 'monthly');
  if (state.aggChart) loadAggregate(range);
}

export function wireEnergyControls() {
  els.rangeHourly.addEventListener('click', function () { setRange('hourly'); });
  els.rangeDaily.addEventListener('click', function () { setRange('daily'); });
  els.rangeMonthly.addEventListener('click', function () { setRange('monthly'); });
}

// --------------------------------------------------------- cadence + tabs
function schedule(ms) {
  if (energyTimer) clearInterval(energyTimer);
  energyTimer = setInterval(loadEnergy, ms);
}

// Called by the tab switcher whenever the active tab changes.
export function onEnergyTab(tab) {
  if (tab === 'energy') {
    ensureCharts();
    loadLiveHistory();
    loadAggregate(state.range);
    loadEnergy();          // immediate refresh on entry
    schedule(LIVE_MS);
  } else {
    schedule(SLOW_MS);
  }
}

// Initial poll cadence at boot, before any tab interaction.
export function startEnergyPolling(initialTab) {
  schedule(initialTab === 'energy' ? LIVE_MS : SLOW_MS);
}

// Theme toggle hook — re-read CSS-var colors into both charts.
export function restyleEnergyCharts() {
  restyle(state.liveChart, 'W');
  restyle(state.aggChart, 'Wh');
}
