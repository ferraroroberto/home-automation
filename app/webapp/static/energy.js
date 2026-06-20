/* Energy data + Energy-tab controller.
 *
 * Owns everything energy: the compact Home tile, the Energy-tab SMA-style stack
 * (live flow diagram, deficit/surplus banner, efficiency tiles, today's split
 * cards, savings), the live flowing chart, and the hourly/daily/monthly bars.
 *
 * Cadence is tab-aware: the live snapshot polls fast (LIVE_MS) only while the
 * Energy tab is open, falling back to SLOW_MS elsewhere so the Home tile still
 * updates without hammering the SMA devices. Today's slow-moving kWh totals
 * refresh on their own TODAY_MS cadence while the Energy tab is open. Charts are
 * created lazily on the first Energy-tab visit (Chart.js is a heavy global). */

'use strict';

import { state, els } from './state.js';
import { jsonApi } from './api.js';
import {
  createLiveChart, setLiveData, pushLivePoint,
  createAggChart, setAggData, restyle,
} from './charts.js';

const LIVE_MS = 5_000;
const SLOW_MS = 30_000;
const TODAY_MS = 60_000;      // today's kWh totals move slowly — refresh gently
const LIVE_WINDOW_MIN = 60;   // minutes of recent history seeded into the live chart
const LIVE_MAX_POINTS = 400;  // ring-buffer cap on the live chart

// Rough, clearly-labelled estimates for the savings card.
const CO2_KG_PER_KWH = 0.4;       // grid emission factor (kg CO₂ avoided / kWh)
const CO2_KG_PER_TREE_YEAR = 21;  // sequestration per tree-year

let energyTimer = null;
let todayTimer = null;

// --------------------------------------------------------------- formatting
// Group digits in threes with a comma — "3745" → "3,745".
function group(n) {
  return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ',');
}

function fmtW(v) {
  return v == null ? '—' : group(Math.round(Number(v))) + ' W';
}

function fmtSignedW(v) {
  if (v == null) return '—';
  const rounded = Math.round(Number(v));
  if (rounded === 0) return '0 W';
  return (rounded > 0 ? '+' : '−') + group(Math.abs(rounded)) + ' W';
}

function fmtKwh(wh) {
  return wh == null ? '—' : (Number(wh) / 1000).toFixed(2) + ' kWh';
}

function fmtPct(frac) {
  return frac == null ? '—' : Math.round(frac * 100) + ' %';
}

function clamp01(x) {
  return Math.max(0, Math.min(1, x));
}

function nowLabel() {
  return new Date().toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

// --------------------------------------------------- live-flow derivations
// Solar covering the load: min(solar, house). Asleep PV counts as 0 solar for
// self-sufficiency, but self-consumption is undefined (null) — nothing produced.
function selfSufficiencyFrac(solar, house) {
  if (house == null || house <= 0) return null;
  if (solar == null) return 0;
  return clamp01(Math.max(0, solar) / house);
}

function selfConsumptionFrac(solar, house) {
  if (solar == null || solar <= 0) return null;
  if (house == null) return null;
  return clamp01(Math.max(0, Math.min(solar, house)) / solar);
}

// ----------------------------------------------------- render a live snapshot
export function renderEnergy(e) {
  const solar = e.inverter_reachable ? e.pv_power_w : null;

  // --- Home compact tile (Production / Consumption / Net grid). ---
  els.enPv.textContent = e.inverter_reachable ? fmtW(e.pv_power_w) : 'asleep';
  els.enHouse.textContent = fmtW(e.house_consumption_w);
  els.enSurplus.textContent = fmtSignedW(e.pv_surplus_w);
  if (els.enUpdated) els.enUpdated.textContent = 'Updated ' + nowLabel();
  els.energyFlow.hidden = false;

  // --- Energy-tab flow diagram. ---
  els.flowTime.textContent = nowLabel();
  els.flowPv.textContent = e.inverter_reachable ? fmtW(e.pv_power_w) : 'asleep';
  els.flowGrid.textContent = fmtW(gridFlowW(e));
  els.flowHouse.textContent = fmtW(e.house_consumption_w);
  els.flowNodePv.classList.toggle('is-idle', !e.inverter_reachable);

  // Solar → Home arrow: green ▶ while producing, dim · when asleep/zero.
  const producing = solar != null && solar > 0;
  els.wirePv.classList.toggle('is-active', producing);
  els.wirePv.textContent = producing ? '▶' : '·';

  // Home ↔ Grid arrow (Grid sits on the right): ◀ importing (grid feeds home),
  // ▶ exporting (home feeds grid back), · when balanced.
  const surplus = e.pv_surplus_w;
  els.wireGrid.classList.remove('is-import', 'is-export');
  if (surplus != null && surplus > 1) {
    els.wireGrid.classList.add('is-export');
    els.wireGrid.textContent = '▶';
  } else if (surplus != null && surplus < -1) {
    els.wireGrid.classList.add('is-import');
    els.wireGrid.textContent = '◀';
  } else {
    els.wireGrid.textContent = '·';
  }

  // --- Solar deficit / surplus banner. ---
  if (surplus == null) {
    els.flowBanner.hidden = true;
  } else {
    els.flowBanner.hidden = false;
    const exporting = surplus > 0;
    const deficit = surplus < 0;
    els.flowBanner.classList.toggle('is-surplus', exporting);
    els.flowBanner.classList.toggle('is-deficit', deficit);
    els.flowBannerLabel.textContent =
      exporting ? 'Solar surplus' : deficit ? 'Solar deficit' : 'Balanced';
    els.flowBannerValue.textContent = fmtSignedW(surplus);
  }

  // --- Live efficiency tiles. ---
  els.liveSelfSuff.textContent = fmtPct(selfSufficiencyFrac(solar, e.house_consumption_w));
  els.liveSelfCons.textContent = fmtPct(selfConsumptionFrac(solar, e.house_consumption_w));

  // --- append to the live chart (Generation / Grid-supplied / Consumption) ---
  if (state.liveChart) {
    pushLivePoint(
      state.liveChart, Math.floor(Date.now() / 1000),
      solar, e.grid_import_w, e.house_consumption_w, LIVE_MAX_POINTS,
    );
    els.liveMeta.textContent = '· ' + nowLabel();
  }
}

// Power at the grid connection point — whichever side is active (one is ~0).
function gridFlowW(e) {
  const imp = e.grid_import_w || 0;
  const exp = e.grid_export_w || 0;
  if (imp <= 0 && exp <= 0) return e.grid_import_w == null && e.grid_export_w == null ? null : 0;
  return imp >= exp ? imp : exp;
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

// ------------------------------------------------------- today's split cards
function renderToday(b) {
  const pvWh = b && !b.pv_missing ? b.pv_wh : null;
  const houseWh = b ? b.house_wh : null;
  const exportWh = b ? (b.export_wh || 0) : 0;
  const importWh = b ? (b.import_wh || 0) : 0;

  // Generation: self-consumed (pv − fed-in) vs grid feed-in.
  els.genTotal.textContent = fmtKwh(pvWh);
  if (pvWh != null && pvWh > 0) {
    const selfWh = Math.max(0, pvWh - exportWh);
    const frac = clamp01(selfWh / pvWh);
    els.genSelf.textContent = fmtKwh(selfWh);
    els.genFeed.textContent = fmtKwh(exportWh);
    els.genBar.style.width = (frac * 100) + '%';
    els.genPct.textContent = fmtPct(frac) + ' self-consumed';
  } else {
    els.genSelf.textContent = '—';
    els.genFeed.textContent = '—';
    els.genBar.style.width = '0%';
    els.genPct.textContent = '—';
  }

  // Consumption: covered by solar (house − imported) vs grid-supplied.
  els.consTotal.textContent = fmtKwh(houseWh);
  if (houseWh != null && houseWh > 0) {
    const selfWh = Math.max(0, houseWh - importWh);
    const frac = clamp01(selfWh / houseWh);
    els.consSelf.textContent = fmtKwh(selfWh);
    els.consGrid.textContent = fmtKwh(importWh);
    els.consBar.style.width = (frac * 100) + '%';
    els.consPct.textContent = fmtPct(frac) + ' self-sufficient';
  } else {
    els.consSelf.textContent = '—';
    els.consGrid.textContent = '—';
    els.consBar.style.width = '0%';
    els.consPct.textContent = '—';
  }

  // Savings: from today's clean PV generation. Rough, labelled an estimate.
  const co2 = pvWh != null ? (pvWh / 1000) * CO2_KG_PER_KWH : null;
  els.savCo2.textContent = co2 != null ? co2.toFixed(1) + ' kg' : '—';
  els.savTrees.textContent = co2 != null ? (co2 / CO2_KG_PER_TREE_YEAR).toFixed(2) : '—';
}

async function loadToday() {
  try {
    const body = await jsonApi('/api/energy/today');
    renderToday(body && body.bucket);
  } catch (_) {
    // Secondary — keep whatever the last successful read rendered.
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
  els.rangeBtns.forEach(function (btn) {
    btn.classList.toggle('active', btn.dataset.range === range);
  });
  if (state.aggChart) loadAggregate(range);
}

export function wireEnergyControls() {
  els.rangeBtns.forEach(function (btn) {
    btn.addEventListener('click', function () { setRange(btn.dataset.range); });
  });
}

// --------------------------------------------------------- cadence + tabs
function schedule(ms) {
  if (energyTimer) clearInterval(energyTimer);
  energyTimer = setInterval(loadEnergy, ms);
}

function scheduleToday(on) {
  if (todayTimer) { clearInterval(todayTimer); todayTimer = null; }
  if (on) todayTimer = setInterval(loadToday, TODAY_MS);
}

// Called by the tab switcher whenever the active tab changes.
export function onEnergyTab(tab) {
  if (tab === 'energy') {
    ensureCharts();
    loadLiveHistory();
    loadAggregate(state.range);
    loadEnergy();          // immediate refresh on entry
    loadToday();           // today's split cards + savings
    schedule(LIVE_MS);
    scheduleToday(true);
  } else {
    schedule(SLOW_MS);
    scheduleToday(false);
  }
}

// Initial poll cadence at boot, before any tab interaction.
export function startEnergyPolling(initialTab) {
  schedule(initialTab === 'energy' ? LIVE_MS : SLOW_MS);
}

// Theme toggle hook — re-read CSS-var colors into both charts.
export function restyleEnergyCharts() {
  restyle(state.liveChart, 'W');
  restyle(state.aggChart, 'kWh');
}
