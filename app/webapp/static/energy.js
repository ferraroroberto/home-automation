/* Energy data + Energy-tab controller.
 *
 * Owns everything energy: the compact Home tile, the Energy-tab stacked-area
 * (live flow diagram, deficit/surplus banner, efficiency tiles, today's split
 * cards, savings), the live flowing chart, and the hourly/daily/monthly bars.
 *
 * Cadence is tab-aware: the live snapshot polls fast (LIVE_MS) only while the
 * Energy tab is open, falling back to SLOW_MS elsewhere so the Home tile still
 * updates without hammering the FusionSolar cloud. Today's slow-moving kWh totals
 * refresh on their own TODAY_MS cadence while the Energy tab is open. Charts are
 * created lazily on the first Energy-tab visit (Chart.js is a heavy global). */

'use strict';

import { state, els, reportFetchOk, toast } from './state.js';
import { jsonApi, isAuthRequired } from './api.js';
import { esc, group, fmtW, fmtPct } from './format.js';
import { isSnapshotRestored, restoreSnapshot, saveSnapshot, snapshotLabel } from './snapshots.js';
import {
  createLiveChart, setLiveData, pushLivePoint,
  createAggChart, setAggData, restyle,
  createExportCreditChart, setExportCreditData, restyleExportCredit,
  createForecastChart, setForecastData, restyleForecast,
  createSunOverlayChart, setSunOverlayData, restyleSunOverlay,
} from './charts.js';
import { createPoller } from './poll.js';
import { createViewState, markTabFailure, renderFeedback } from './view-state.js';
import { confirmAction } from './confirm.js';
import {
  arraySummary, loadPvSystem, setPvSystemSavedHook, wirePvSystem,
} from './pv-system.js';
import { loadBoostCoordinator, wireBoostCoordinator } from './boost-coordinator.js';

const LIVE_MS = 5_000;
const SLOW_MS = 30_000;
const TODAY_MS = 60_000;      // today's kWh totals move slowly — refresh gently
const LIVE_WINDOW_MIN = 60;   // minutes of recent history seeded into the live chart
const LIVE_MAX_POINTS = 400;  // ring-buffer cap on the live chart

// Rough, clearly-labelled estimates for the savings card. The € figure is no
// longer a flat rate — it comes from the tiered tariff via /api/energy/cost
// (see loadSavingsEur); only the CO₂/trees credit stays a simple factor.
const CO2_KG_PER_KWH = 0.4;       // grid emission factor (kg CO₂ avoided / kWh)
const CO2_KG_PER_TREE_YEAR = 21;  // sequestration per tree-year

let todayTimer = null;
let energyLastGood = null;
const energyView = createViewState('energyLive');

function renderEnergyFeedback() {
  if (!els.paneEnergy) return;
  renderFeedback(energyView, els.energyFeedback, {
    paneEl: els.paneEnergy,
    ariaBusy: true,
    icon: 'zap',
    loadingIcon: 'refresh-cw',
    loadingLabel: 'Reading live energy…',
    errorLabel: 'Live energy unavailable',
    snapshotKey: 'energyLive',
    onRetry: function () { loadEnergy(); },
  });
}

function markEnergyFailure() {
  markTabFailure(energyView, {
    hasData: !!energyLastGood,
    scope: 'energy',
    label: 'live energy',
    render: function () {
      renderEnergyFeedback();
      if (energyLastGood) {
        els.liveMeta.textContent = '· ' + energyView.lastUpdatedLabel() + ' · live data unavailable';
      }
    },
  });
}

// --------------------------------------------------------------- formatting
// group / fmtW / fmtPct / esc live in the shared format.js (issue #383).

function fmtKwh(wh) {
  return wh == null ? '—' : (Number(wh) / 1000).toFixed(2) + ' kWh';
}

// This tab holds 0–1 fractions; the shared fmtPct takes 0–100.
function fmtFracPct(frac) {
  return fmtPct(frac == null ? null : frac * 100);
}

// A feed-outage duration: "1.3 h" / "45 min", empty when there was none. Sub-
// hour outages are the common case and "0.8 h" reads worse than "45 min" at
// this size. Shared by the generation card and the forecast card (#579).
function fmtGap(hours) {
  const h = Number(hours) || 0;
  if (h <= 0) return '';
  return h < 1 ? Math.round(h * 60) + ' min' : h.toFixed(1) + ' h';
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
// Element groupings for the two *identical* Solar → Home ← Grid flow cards: the
// Energy tab's and the Home tab's. Same view, rendered once (issue #57).
const energyFlowRefs = {
  pv: els.flowPv, grid: els.flowGrid, house: els.flowHouse,
  nodePv: els.flowNodePv, wirePv: els.wirePv, wireGrid: els.wireGrid,
};
const homeFlowRefs = {
  pv: els.homeFlowPv, grid: els.homeFlowGrid, house: els.homeFlowHouse,
  nodePv: els.homeFlowNodePv, wirePv: els.homeWirePv, wireGrid: els.homeWireGrid,
};

// Fill one flow card from a snapshot, against whichever ref set is passed in.
function renderFlowCard(r, e, solar) {
  r.pv.textContent = e.inverter_reachable ? fmtW(e.pv_power_w) : 'asleep';
  r.grid.textContent = fmtW(gridFlowW(e));
  r.house.textContent = fmtW(e.house_consumption_w);
  r.nodePv.classList.toggle('is-idle', !e.inverter_reachable);

  // Solar → Home arrow: green ▶ while producing, dim · when asleep/zero.
  const producing = solar != null && solar > 0;
  r.wirePv.classList.toggle('is-active', producing);
  r.wirePv.textContent = producing ? '▶' : '·';

  // Home ↔ Grid arrow (Grid sits on the right): ◀ importing (grid feeds home),
  // ▶ exporting (home feeds grid back), · when balanced.
  const surplus = e.pv_surplus_w;
  r.wireGrid.classList.remove('is-import', 'is-export');
  if (surplus != null && surplus > 1) {
    r.wireGrid.classList.add('is-export');
    r.wireGrid.textContent = '▶';
  } else if (surplus != null && surplus < -1) {
    r.wireGrid.classList.add('is-import');
    r.wireGrid.textContent = '◀';
  } else {
    r.wireGrid.textContent = '·';
  }
}

export function renderEnergy(e) {
  const solar = e.inverter_reachable ? e.pv_power_w : null;

  // Energy-tab flow card + the matching Home-tab card (revealed once it has data).
  renderFlowCard(energyFlowRefs, e, solar);
  renderFlowCard(homeFlowRefs, e, solar);
  els.homeEnergyFlow.hidden = false;
  renderEnergyFeedback();

  // --- Live efficiency tiles. ---
  els.liveSelfSuff.textContent = fmtFracPct(selfSufficiencyFrac(solar, e.house_consumption_w));
  els.liveSelfCons.textContent = fmtFracPct(selfConsumptionFrac(solar, e.house_consumption_w));

  // --- live availability note ---
  // The meter carries grid + house power; without it there is no live snapshot
  // to plot (an asleep inverter alone is normal at night). Say *why* on the meta
  // line instead of leaving the tiles at a bare "—" with no explanation.
  // Two distinct causes, worth telling apart: solar still reading means the
  // inverter is fine and only the power sensor is bad, which is a hardware
  // fault to chase; nothing reading at all is just no data from the source.
  const liveNote = e.meter_reachable === false
    ? (e.inverter_reachable
      ? '· Grid and home unavailable — the power sensor is reporting invalid readings'
      : '· Live unavailable — no reading from the inverter')
    : null;

  // --- append to the live chart (Generation / Grid-supplied / Consumption) ---
  if (state.liveChart) {
    pushLivePoint(
      state.liveChart, Math.floor(Date.now() / 1000),
      solar, e.grid_import_w, e.house_consumption_w, LIVE_MAX_POINTS,
    );
    els.liveMeta.textContent = liveNote || (isSnapshotRestored('energyLive') ? '· ' + snapshotLabel('energyLive') : '· ' + nowLabel());
  } else if (liveNote) {
    els.liveMeta.textContent = liveNote;
  } else if (isSnapshotRestored('energyLive')) {
    els.liveMeta.textContent = '· ' + snapshotLabel('energyLive');
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
  if (!energyLastGood) {
    energyView.set('loading', { liveUnavailable: false });
    renderEnergyFeedback();
  }
  try {
    const body = await jsonApi('/api/energy');
    if (!body) {
      markEnergyFailure();
      return;
    }
    reportFetchOk('energy');
    saveSnapshot('energyLive', body);
    energyLastGood = body;
    energyView.set('ready', {
      updatedAt: new Date(),
      liveUnavailable: false,
    });
    renderEnergy(body);
  } catch (exc) {
    // A hard fetch failure (network/500) is surfaced once per outage; the live
    // values keep their last render. A successful fetch that simply has no live
    // data (meter/inverter unreachable) is handled inline in renderEnergy.
    if (isAuthRequired(exc)) return;
    markEnergyFailure();
  }
}

// ------------------------------------------------------- today's split cards
// The totals above stay exactly as measured; this line says why they may read
// low without blaming the array (#579).
function renderFeedGap(el, hours) {
  const text = fmtGap(hours);
  el.textContent = text ? 'Solar feed offline for ' + text + ' — measured total is short' : '';
  el.hidden = !text;
}

function renderToday(b, gapHours) {
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
    els.genBar.style.transform = 'scaleX(' + frac + ')';
    els.genPct.textContent = fmtFracPct(frac) + ' self-consumed';
  } else {
    els.genSelf.textContent = '—';
    els.genFeed.textContent = '—';
    els.genBar.style.transform = 'scaleX(0)';
    els.genPct.textContent = '—';
  }

  // Consumption: covered by solar (house − imported) vs grid-supplied.
  els.consTotal.textContent = fmtKwh(houseWh);
  if (houseWh != null && houseWh > 0) {
    const selfWh = Math.max(0, houseWh - importWh);
    const frac = clamp01(selfWh / houseWh);
    els.consSelf.textContent = fmtKwh(selfWh);
    els.consGrid.textContent = fmtKwh(importWh);
    els.consBar.style.transform = 'scaleX(' + frac + ')';
    els.consPct.textContent = fmtFracPct(frac) + ' self-sufficient';
  } else {
    els.consSelf.textContent = '—';
    els.consGrid.textContent = '—';
    els.consBar.style.transform = 'scaleX(0)';
    els.consPct.textContent = '—';
  }

  // Savings: CO₂/trees credit all of today's clean PV generation. The € figure
  // is filled by loadSavingsEur() from the tiered tariff (avoided grid cost of
  // the self-consumed PV) so it agrees with the cost breakdown below.
  const co2 = pvWh != null ? (pvWh / 1000) * CO2_KG_PER_KWH : null;
  els.savCo2.textContent = co2 != null ? co2.toFixed(1) + ' kg' : '—';
  els.savTrees.textContent = co2 != null ? (co2 / CO2_KG_PER_TREE_YEAR).toFixed(2) : '—';

  renderFeedGap(els.genGap, gapHours);
}

async function loadToday() {
  try {
    const body = await jsonApi('/api/energy/today');
    saveSnapshot('energyToday', body);
    renderToday(body && body.bucket, body && body.gap_hours);
  } catch (_) {
    // Secondary — keep whatever the last successful read rendered.
  }
  loadSavingsEur();  // tiered € for the savings card (today, all-in avoided cost)
}

export function restoreEnergySnapshots() {
  const live = restoreSnapshot('energyLive');
  if (live) {
    energyLastGood = live;
    energyView.set('stale', {
      updatedAt: state.snapshotUpdatedAt.energyLive,
      liveUnavailable: false,
    });
    renderEnergy(live);
  }
  const today = restoreSnapshot('energyToday');
  if (today) renderToday(today && today.bucket, today && today.gap_hours);
}

// --------------------------------------------------- cost & savings table
function currencySymbol(cur) {
  return cur === 'EUR' ? '€' : (cur ? cur + ' ' : '€');
}

function num2(v) {
  return Number(v || 0).toFixed(2);
}

function costRow(label, hours, rate, grid, solar, cost, saved, earned, sym, cls) {
  const name = '<th scope="row"><span class="cost-period">' + esc(label) + '</span>'
    + (hours ? '<span class="cost-hours">' + esc(hours) + '</span>' : '') + '</th>';
  const rateCell = '<td class="cost-rate">' + (rate != null ? sym + Number(rate).toFixed(3) : '') + '</td>';
  return '<tr' + (cls ? ' class="' + cls + '"' : '') + '>'
    + name
    + rateCell
    + '<td>' + num2(grid) + '</td>'
    + '<td>' + num2(solar) + '</td>'
    + '<td>' + sym + num2(cost) + '</td>'
    + '<td class="cost-saved">' + sym + num2(saved) + '</td>'
    + '<td class="cost-saved">' + sym + num2(earned) + '</td>'
    + '</tr>';
}

function costStat(label, value, cls) {
  return '<div class="cost-stat"><span class="cost-stat-label">' + esc(label) + '</span>'
    + '<span class="cost-stat-value' + (cls ? ' ' + cls : '') + '">' + esc(value) + '</span></div>';
}

function renderCost(body) {
  const periods = (body && body.periods) || [];
  const totals = body && body.totals;
  const summary = body && body.summary;
  const sym = currencySymbol(body && body.currency);
  const hasData = !!(totals && (
    totals.consumption_kwh > 0 || totals.generation_kwh > 0 || totals.export_kwh > 0
  ));

  if (state.exportCreditChart) {
    setExportCreditData(state.exportCreditChart, (body && body.money_series) || []);
  }

  els.costEmpty.hidden = hasData;
  if (!hasData) {
    els.costBody.innerHTML = '';
    els.costFoot.innerHTML = '';
    els.costSummary.innerHTML = '';
    els.costNote.textContent = '';
    return;
  }

  els.costBody.innerHTML = periods.map(function (p) {
    return costRow(p.label, p.hours, p.rate_eur_kwh, p.grid_kwh,
      p.solar_kwh, p.grid_cost, p.savings, p.export_credit, sym, '');
  }).join('');
  els.costFoot.innerHTML = totals
    ? costRow('Total', '', null, totals.grid_kwh, totals.solar_kwh,
        totals.grid_cost, totals.savings, summary.export_credit, sym, 'cost-total')
    : '';

  els.costSummary.innerHTML = (summary && totals) ? [
    costStat('Generated', num2(totals.generation_kwh) + ' kWh'),
    costStat('Saved', sym + num2(totals.savings), 'cost-pos'),
    // Surplus-compensation credit already netted into Est. bill; shown so the
    // figure is visible (renders €0.00 when export_eur_kwh is unconfigured).
    costStat('Export income', sym + num2(summary.export_credit), 'cost-pos'),
    costStat('Total solar benefit', sym + num2(summary.total_solar_benefit), 'cost-pos'),
    costStat('Grid cost', sym + num2(totals.grid_cost)),
    costStat('Fixed', sym + num2(summary.fixed_cost)),
    costStat('Est. bill', sym + num2(summary.estimated_bill)),
    costStat('Without solar', sym + num2(summary.cost_without_solar)),
  ].join('') : '';

  if (body && body.configured === false) {
    els.costNote.textContent = 'Flat €0.10/kWh estimate — set config/tariff.json for tiered rates.';
  } else if (body) {
    els.costNote.textContent = (body.tariff_name || 'Tariff') + ' · estimate, all-in prices.';
  } else {
    els.costNote.textContent = '';
  }
}

async function loadCost(range) {
  try {
    const body = await jsonApi('/api/energy/cost?range=' + encodeURIComponent(range));
    renderCost(body);
  } catch (_) {
    els.costEmpty.hidden = false;
  }
}

function renderExportRates(body) {
  state.exportRates = (body && body.rates) || [];
  const current = body && body.current_export_eur_kwh;
  els.exportRateCurrent.textContent = current == null ? '—' : '€' + Number(current).toFixed(5) + '/kWh';
  els.exportRateList.innerHTML = state.exportRates.length ? state.exportRates.slice().reverse().map(function (rate) {
    const date = rate.effective_from === '0001-01-01' ? 'Legacy rate' : rate.effective_from;
    const hourly = Array.isArray(rate.hourly_eur_kwh) ? ' · hourly overrides' : '';
    return '<div class="list-row automation-summary-row"><button type="button" class="automation-summary-main export-rate-edit" data-rate-date="'
      + esc(rate.effective_from) + '"><span class="automation-summary-copy">'
      + '<span class="automation-summary-title">' + esc(date) + '</span>'
      + '<span class="automation-summary-meta">€' + Number(rate.export_eur_kwh).toFixed(5) + ' / kWh' + hourly + '</span>'
      + '</span><svg class="icon automation-summary-chevron" aria-hidden="true"><use href="#i-chevron-right"></use></svg></button></div>';
  }).join('') : '<p class="muted small">No export-compensation rate yet.</p>';
  els.exportRateList.querySelectorAll('.export-rate-edit').forEach(function (button) {
    button.addEventListener('click', function () { editExportRate(button.dataset.rateDate); });
  });
}

function resetExportRateForm() {
  els.exportRateOriginalDate.value = '';
  els.exportRateDate.value = localIsoDate();
  els.exportRateValue.value = '';
  els.exportRateHourly.value = '';
  els.exportRateAdd.textContent = 'Add rate';
  els.exportRateDelete.hidden = true;
}

function editExportRate(effectiveFrom) {
  const rate = state.exportRates.find(function (entry) { return entry.effective_from === effectiveFrom; });
  if (!rate || effectiveFrom === '0001-01-01') return;
  els.exportRateOriginalDate.value = effectiveFrom;
  els.exportRateDate.value = effectiveFrom;
  els.exportRateValue.value = rate.export_eur_kwh;
  els.exportRateHourly.value = Array.isArray(rate.hourly_eur_kwh)
    ? rate.hourly_eur_kwh.map(function (value) { return value == null ? '' : value; }).join(', ')
    : '';
  els.exportRateAdd.textContent = 'Save changes';
  els.exportRateDelete.hidden = false;
  els.exportRateDate.focus();
}

function parseHourlyRates() {
  const raw = els.exportRateHourly.value.trim();
  if (!raw) return null;
  const values = raw.split(',').map(function (part) {
    const value = part.trim();
    return value === '' ? null : Number(value);
  });
  if (values.length !== 24 || values.some(function (value) {
    return value != null && (!Number.isFinite(value) || value < 0 || value > 10);
  })) return false;
  return values;
}

async function loadExportRates() {
  if (!els.exportRateList) return;
  try {
    renderExportRates(await jsonApi('/api/energy/export-rates'));
  } catch (exc) {
    if (!isAuthRequired(exc)) els.exportRateList.innerHTML = '<p class="muted small">Rates unavailable.</p>';
  }
}

async function addExportRate() {
  const effectiveFrom = els.exportRateDate.value;
  const rawValue = els.exportRateValue.value.trim();
  const value = Number(rawValue);
  const hourly = parseHourlyRates();
  els.exportRateError.hidden = true;
  if (!effectiveFrom) {
    els.exportRateError.textContent = 'Choose the date this rate takes effect.';
    els.exportRateError.hidden = false;
    els.exportRateDate.focus();
    return;
  }
  if (!rawValue) {
    els.exportRateError.textContent = 'Enter an export rate.';
    els.exportRateError.hidden = false;
    els.exportRateValue.focus();
    return;
  }
  if (!Number.isFinite(value) || value < 0 || value > 10) {
    els.exportRateError.textContent = 'Rate must be between 0 and 10 EUR/kWh.';
    els.exportRateError.hidden = false;
    els.exportRateValue.focus();
    return;
  }
  if (hourly === false) {
    els.exportRateError.textContent = 'Hourly overrides must contain exactly 24 comma-separated rates (blank hours use the default).';
    els.exportRateError.hidden = false;
    els.exportRateHourly.focus();
    return;
  }
  try {
    const body = await jsonApi('/api/energy/export-rates', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        effective_from: effectiveFrom,
        export_eur_kwh: value,
        hourly_eur_kwh: hourly,
        replace_effective_from: els.exportRateOriginalDate.value || null,
      }),
    });
    renderExportRates(body);
    resetExportRateForm();
    await loadCost(state.costRange);
    toast('Export rate saved', 'success');
  } catch (exc) {
    if (!isAuthRequired(exc)) toast("Couldn't save the export rate", 'error');
  }
}

async function deleteExportRate() {
  const effectiveFrom = els.exportRateOriginalDate.value;
  if (!effectiveFrom) return;
  const confirmed = await confirmAction({
    title: 'Delete this export rate?',
    message: 'Historical export on and after this date may be repriced using an earlier entry.',
    okLabel: 'Delete rate',
    danger: true,
  });
  if (!confirmed) return;
  try {
    const body = await jsonApi('/api/energy/export-rates?effective_from=' + encodeURIComponent(effectiveFrom), {
      method: 'DELETE',
    });
    renderExportRates(body);
    resetExportRateForm();
    await loadCost(state.costRange);
    toast('Export rate deleted', 'success');
  } catch (exc) {
    if (!isAuthRequired(exc)) toast("Couldn't delete the export rate", 'error');
  }
}

// The savings card € is always "today" (its own day query), independent of the
// cost table's selected range, and uses the tiered avoided-cost figure.
async function loadSavingsEur() {
  try {
    const body = await jsonApi('/api/energy/cost?range=day');
    const sym = currencySymbol(body && body.currency);
    const s = body && body.totals ? body.totals.savings : null;
    els.savEur.textContent = s != null ? sym + Number(s).toFixed(2) : '—';
  } catch (_) {
    // keep the last rendered value
  }
}

function setCostRange(range) {
  state.costRange = range;
  els.costRangeBtns.forEach(function (btn) {
    btn.classList.toggle('active', btn.dataset.crange === range);
  });
  loadCost(range);
}

// --------------------------------------------------- solar forecast card
// A clearer note per reason; the default HTML note covers the common case.
// Both now point at the PV-system card below rather than at a file on disk —
// the config is editable in the app since issue #561.
const FORECAST_NOTES = {
  not_configured: 'Add your panel rows in the PV system card below to enable the forecast.',
  no_location: 'Set the home coordinates in the PV system card below to enable the forecast.',
  // Distinct from the generic fallback below (#597): Open-Meteo is answering,
  // just refusing this request rate — not the same as a network failure. Only
  // reached when there's no cached curve recent enough to show instead.
  rate_limited: 'Weather provider is rate-limiting us right now — retrying shortly.',
};

// "1.5 kWp · 35° · S · PR 0.80" (single array) or
// "7.9 kWp · 15° · S  +  0.9 kWp · 15° · N · PR 0.80" (multi-orientation, issue #555)
// from the array params the curve used.
function forecastParamsLine(sys) {
  if (!sys || !sys.arrays || !sys.arrays.length) return '';
  const parts = sys.arrays.map(arraySummary).join('  +  ');
  return parts + ' · PR ' + Number(sys.performance_ratio).toFixed(2);
}

// Returns the rendered day estimate ("12.3") so a save confirmation can carry
// it, or null when there is nothing to show (unavailable / missing total).
function renderForecast(body) {
  const available = !!(body && body.available);
  els.forecastEmpty.hidden = available;
  if (!available) {
    els.forecastEmpty.textContent =
      FORECAST_NOTES[body && body.reason] || 'Solar forecast is unavailable right now.';
    els.forecastHeadline.textContent = '—';
    els.forecastMeta.textContent = '';
    els.forecastParams.textContent = '';
    if (state.forecastChart) setForecastData(state.forecastChart, [], null);
    return null;
  }
  if (state.forecastChart) setForecastData(state.forecastChart, body.expected, body.actual);
  const total = body.expected_total_kwh != null ? Number(body.expected_total_kwh).toFixed(1) : null;
  els.forecastHeadline.textContent = 'Expected generation +' + (total != null ? total : '—') + ' kWh';
  // The actual overlay draws under-covered hours as projections, so say when
  // any of it is inferred rather than measured — otherwise the two curves
  // agreeing looks like a measurement it isn't (#579).
  const gap = fmtGap(body.actual_gap_hours);
  els.forecastMeta.textContent = body.actual
    ? '· estimate vs actual' + (gap ? ' · feed offline ' + gap : '')
    : '· estimate';
  els.forecastParams.textContent = forecastParamsLine(body.system);
  return total;
}

async function loadForecast(day) {
  try {
    const body = await jsonApi('/api/energy/forecast?day=' + encodeURIComponent(day));
    return renderForecast(body);
  } catch (_) {
    els.forecastEmpty.hidden = false;
    return null;
  }
}

function setForecastDay(day) {
  state.forecastDay = day;
  els.forecastDayBtns.forEach(function (btn) {
    btn.classList.toggle('active', btn.dataset.day === day);
  });
  loadForecast(day);
}

// ------------------------------------------- sun-position diagnostic (#590)
// Read-only companion to the forecast card: the day's measured performance
// ratio against where the sun actually was. Folded away by default, and only
// loaded once opened — it costs an extra irradiance read, and it answers an
// occasional question rather than a glanceable one.
const SUN_OVERLAY_NOTES = {
  not_configured: 'Add your panel rows in the PV system card below.',
  no_location: 'Set the home coordinates in the PV system card below.',
  too_old: 'Irradiance history only reaches back about three months.',
  rate_limited: 'Weather provider is rate-limiting us right now — retrying shortly.',
};

// Today in the browser's own local date, which is the day the hourly rollups
// are framed by. toISOString() would be UTC and could name yesterday.
function localIsoDate(d) {
  const dt = d || new Date();
  return [
    dt.getFullYear(),
    ('0' + (dt.getMonth() + 1)).slice(-2),
    ('0' + dt.getDate()).slice(-2),
  ].join('-');
}

function plural(n, noun) {
  return n + ' ' + noun + (n === 1 ? '' : 's');
}

// Named, never merely absent: an hour silently dropped would leave the day
// looking better-measured than it was — the same class of quiet error the
// overlay exists to avoid making about shading.
function sunOverlayNote(body) {
  const parts = [plural((body.points || []).length, 'hour') + ' plotted'];
  const short = Number(body.excluded_coverage) || 0;
  const absent = Number(body.excluded_no_data) || 0;
  if (short) parts.push(plural(short, 'hour') + ' excluded — feed coverage too short');
  if (absent) parts.push(plural(absent, 'daylight hour') + ' never measured');
  return parts.join(' · ');
}

function renderSunOverlay(body) {
  const available = !!(body && body.available);
  const points = available ? (body.points || []) : [];
  if (state.sunOverlayChart) {
    setSunOverlayData(state.sunOverlayChart, points, body && body.modelled_pr);
  }
  if (!available) {
    els.sunOverlayEmpty.hidden = false;
    els.sunOverlayEmpty.textContent =
      SUN_OVERLAY_NOTES[body && body.reason] || 'Sun-position diagnostic is unavailable right now.';
    els.sunOverlayNote.textContent = '';
    els.sunOverlayCount.textContent = '—';
    return;
  }
  els.sunOverlayEmpty.hidden = points.length > 0;
  els.sunOverlayEmpty.textContent = 'No measured hours for this day.';
  els.sunOverlayNote.textContent = sunOverlayNote(body);
  els.sunOverlayCount.textContent = points.length
    ? points.length + ' h'
    : '—';
}

async function loadSunOverlay(day) {
  if (!els.sunOverlayCard) return;
  try {
    const body = await jsonApi('/api/energy/sun-overlay?date=' + encodeURIComponent(day));
    renderSunOverlay(body);
  } catch (_) {
    renderSunOverlay(null);
  }
}

function ensureSunOverlay() {
  if (!els.sunOverlayChart) return;
  // Created on first open, not on tab entry: a canvas inside a closed
  // <details> has no layout box, so Chart.js would size it to zero.
  if (!state.sunOverlayChart) {
    state.sunOverlayChart = createSunOverlayChart(els.sunOverlayChart);
  }
  if (!state.sunOverlayDate) {
    state.sunOverlayDate = localIsoDate();
    els.sunOverlayDate.value = state.sunOverlayDate;
  }
  // Re-stamped here, not only at wiring time: an installed PWA can sit open
  // across midnight, after which yesterday's ceiling would reject today.
  els.sunOverlayDate.max = localIsoDate();
  loadSunOverlay(state.sunOverlayDate);
}

function wireSunOverlay() {
  if (!els.sunOverlayCard) return;
  els.sunOverlayDate.max = localIsoDate();
  els.sunOverlayCard.addEventListener('toggle', function () {
    if (els.sunOverlayCard.open) ensureSunOverlay();
  });
  els.sunOverlayDate.addEventListener('change', function () {
    const day = els.sunOverlayDate.value;
    if (!day) return;
    state.sunOverlayDate = day;
    loadSunOverlay(day);
  });
}

// --------------------------------------------------------------- charts
function ensureCharts() {
  if (!state.liveChart) state.liveChart = createLiveChart(els.liveChart);
  if (!state.aggChart) state.aggChart = createAggChart(els.aggChart);
  if (!state.exportCreditChart) state.exportCreditChart = createExportCreditChart(els.exportCreditChart);
  if (!state.forecastChart) state.forecastChart = createForecastChart(els.forecastChart);
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
    const sums = buckets.reduce(function (out, bucket) {
      out.production += Number(bucket.pv_wh) || 0;
      out.consumption += Number(bucket.house_wh) || 0;
      out.grid += Number(bucket.import_wh) || 0;
      out.exported += Number(bucket.export_wh) || 0;
      return out;
    }, { production: 0, consumption: 0, grid: 0, exported: 0 });
    sums.solar = Math.max(0, sums.consumption - sums.grid);
    els.energySummary.innerHTML = [
      costStat('Production', num2(sums.production / 1000) + ' kWh'),
      costStat('Consumption', num2(sums.consumption / 1000) + ' kWh'),
      costStat('Solar consumed', num2(sums.solar / 1000) + ' kWh', 'cost-pos'),
      costStat('Grid imported', num2(sums.grid / 1000) + ' kWh'),
      costStat('Solar exported', num2(sums.exported / 1000) + ' kWh', 'cost-pos'),
    ].join('');
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
  els.costRangeBtns.forEach(function (btn) {
    btn.addEventListener('click', function () { setCostRange(btn.dataset.crange); });
  });
  els.forecastDayBtns.forEach(function (btn) {
    btn.addEventListener('click', function () { setForecastDay(btn.dataset.day); });
  });
  if (els.exportRateAdd) {
    els.exportRateDate.value = localIsoDate();
    els.exportRateAdd.addEventListener('click', addExportRate);
    els.exportRateDelete.addEventListener('click', deleteExportRate);
  }
  // Editing the array/coordinates changes what the forecast is computed from,
  // so every successful save re-reads the curve for the day on screen — and
  // the resolved estimate feeds the save toast (issue #564).
  setPvSystemSavedHook(function () { return loadForecast(state.forecastDay); });
  wirePvSystem();
  wireSunOverlay();
  wireBoostCoordinator();
}

// --------------------------------------------------------- cadence + tabs
const schedule = createPoller(loadEnergy);

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
    loadCost(state.costRange);  // cost & savings breakdown table
    loadExportRates();
    loadForecast(state.forecastDay);  // solar expected-generation forecast
    loadPvSystem();        // the array config that forecast is computed from
    // The sun-position diagnostic refreshes only while it is open (#590) —
    // closed, it costs nothing.
    if (els.sunOverlayCard && els.sunOverlayCard.open) ensureSunOverlay();
    loadBoostCoordinator();  // fleet solar-boost sequencing knobs (#562)
    loadEnergy();          // immediate refresh on entry
    loadToday();           // today's split cards + savings
    schedule(LIVE_MS);
    scheduleToday(true);
  } else {
    schedule(SLOW_MS);
    scheduleToday(false);
  }
}

// Theme toggle hook — re-read CSS-var colors into both charts.
export function restyleEnergyCharts() {
  restyle(state.liveChart, 'W');
  restyle(state.aggChart, 'kWh');
  restyleExportCredit(state.exportCreditChart);
  restyleForecast(state.forecastChart);
  restyleSunOverlay(state.sunOverlayChart);
}
