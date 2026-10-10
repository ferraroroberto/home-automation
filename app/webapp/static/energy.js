/* Energy data + Energy-tab controller.
 *
 * Owns everything energy: the compact Home flow card and the Energy tab, which
 * since #883 (Step 5/8 of #872) is the glance card (the live flow and one line
 * of self-sufficiency), the Today card (two meters and the day's savings), one
 * History card (one period picker, an Energy / Money switch, the live flowing
 * chart behind Live), and the solar forecast. The export-rate editor it used to
 * carry lives in Settings.
 *
 * Cadence is tab-aware: the live snapshot polls fast (LIVE_MS) only while the
 * Energy tab is open, falling back to SLOW_MS elsewhere so the Home tile still
 * updates without hammering the FusionSolar cloud. Today's slow-moving kWh totals
 * refresh on their own TODAY_MS cadence while the Energy tab is open. Charts are
 * created lazily on the first Energy-tab visit (Chart.js is a heavy global). */

'use strict';

import { state, els, reportFetchOk, toast } from './state.js';
import { jsonApi, isAuthRequired } from './api.js';
import { esc, group, fmtW, fmtPct, localIsoDate } from './format.js';
import { icon } from './_vendored/icons/icons.js';
import { isSnapshotRestored, restoreSnapshot, saveSnapshot, snapshotLabel } from './snapshots.js';
import {
  createLiveChart, setLiveData, pushLivePoint,
  createAggChart, setAggData, restyle,
  createExportCreditChart, setExportCreditData, restyleExportCredit,
  createForecastChart, setForecastData, restyleForecast,
  createSunOverlayChart, setSunOverlayData, restyleSunOverlay, loadChartJs,
} from './charts.js';
import { createPoller } from './poll.js';
import { createViewState, markTabFailure, renderFeedback } from './view-state.js';
import { confirmAction } from './confirm.js';
import { setHeadPart } from './head-status.js';
import { rowEl } from './row.js';
import { sheet } from './sheet.js';
import { showSettings } from './tabs.js';
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
      setHeadPart('energy', 'flow', {
        exceptions: [{ text: 'Live unavailable', tone: 'attention' }],
      });
      if (energyLastGood) {
        els.liveMeta.textContent = energyView.lastUpdatedLabel() + ' · live data unavailable';
      }
    },
  });
}

// --------------------------------------------------------------- formatting
// group / fmtW / fmtPct / esc live in the shared format.js (issue #383).

function fmtKwh(wh) {
  return wh == null ? '—' : (Number(wh) / 1000).toFixed(2) + ' kWh';
}

// The same figure without its unit, for a Today meter's split line, where the
// total beside the bar already says kWh.
function fmtKwhBare(wh) {
  return wh == null ? '—' : (Number(wh) / 1000).toFixed(2);
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
// Element groupings for the two *identical* Solar → Home ← Grid flows: the
// Energy tab's glance card and the Home tab's House card (#885). Same view,
// rendered once (issue #57).
const energyFlowRefs = {
  pv: els.flowPv, grid: els.flowGrid, house: els.flowHouse,
  nodePv: els.flowNodePv, wirePv: els.wirePv, wireGrid: els.wireGrid,
  gridName: els.flowGridName,
};
const homeFlowRefs = {
  pv: els.homeFlowPv, grid: els.homeFlowGrid, house: els.homeFlowHouse,
  nodePv: els.homeFlowNodePv, wirePv: els.homeWirePv, wireGrid: els.homeWireGrid,
  gridName: els.homeFlowGridName,
};

// Fill one flow card from a snapshot, against whichever ref set is passed in.
function renderFlowCard(r, e, solar) {
  r.pv.textContent = e.inverter_reachable ? fmtW(e.pv_power_w) : 'asleep';
  r.grid.textContent = fmtW(gridFlowW(e));
  r.house.textContent = fmtW(e.house_consumption_w);
  r.nodePv.classList.toggle('is-idle', !e.inverter_reachable);

  // Solar → Home wire: a Lucide arrow while producing, a dim dot when
  // asleep/zero (Lucide glyphs since #779 — no ▶ ◀ · characters as icons).
  // Direction is not a status, so the arrows carry no colour (#883).
  const producing = solar != null && solar > 0;
  r.wirePv.classList.toggle('is-active', producing);
  r.wirePv.innerHTML = icon(producing ? 'arrow-right' : 'dot');

  // Home ↔ Grid wire (Grid sits on the right): left while importing (grid
  // feeds home), right while exporting (home feeds grid back), a dot balanced.
  // The Grid node's name says the same in words, so the direction never rests
  // on the arrow alone.
  const surplus = e.pv_surplus_w;
  r.wireGrid.classList.remove('is-import', 'is-export');
  if (surplus != null && surplus > 1) {
    r.wireGrid.classList.add('is-export');
    r.wireGrid.innerHTML = icon('arrow-right');
    r.gridName.textContent = 'Exporting';
  } else if (surplus != null && surplus < -1) {
    r.wireGrid.classList.add('is-import');
    r.wireGrid.innerHTML = icon('arrow-left');
    r.gridName.textContent = 'Importing';
  } else {
    r.wireGrid.innerHTML = icon('dot');
    r.gridName.textContent = 'Grid';
  }
}

// The Energy header's live line (head-status.js, #880): what the house does
// with the grid right now, or why that isn't known. A restored snapshot says
// nothing: it is not live.
function renderEnergyHead(e) {
  if (energyView.state !== 'ready') {
    setHeadPart('energy', 'flow', null);
    return;
  }
  if (e.snapshot && e.snapshot.stale) {
    setHeadPart('energy', 'flow', { exceptions: [{ text: 'Not refreshing', tone: 'attention' }] });
    return;
  }
  if (e.meter_reachable === false) {
    setHeadPart('energy', 'flow', { exceptions: [{ text: 'Live unavailable', tone: 'attention' }] });
    return;
  }
  const imp = e.grid_import_w || 0;
  const exp = e.grid_export_w || 0;
  let fact = '';
  if (e.grid_import_w == null && e.grid_export_w == null) fact = '';
  else if (exp > imp && exp > 1) fact = 'Exporting ' + fmtW(exp);
  else if (imp > 1) fact = 'Importing ' + fmtW(imp);
  else fact = 'Balanced';
  setHeadPart('energy', 'flow', { fact: fact });
}

export function renderEnergy(e) {
  const solar = e.inverter_reachable ? e.pv_power_w : null;
  renderEnergyHead(e);

  // Energy-tab flow + the matching one in Home's House card (revealed once it has data).
  renderFlowCard(energyFlowRefs, e, solar);
  renderFlowCard(homeFlowRefs, e, solar);
  els.homeEnergyFlow.hidden = false;
  renderEnergyFeedback();

  // --- The glance card's line: self-sufficiency and self-consumption now. ---
  // Self-consumption is undefined while nothing is produced (night), so that
  // half of the line goes rather than reading "— of solar used here".
  els.liveSelfSuff.textContent = fmtFracPct(selfSufficiencyFrac(solar, e.house_consumption_w));
  const selfCons = selfConsumptionFrac(solar, e.house_consumption_w);
  els.liveSelfCons.textContent = fmtFracPct(selfCons);
  els.liveSelfConsPart.hidden = selfCons == null;

  // --- live availability note ---
  // The meter carries grid + house power; without it there is no live snapshot
  // to plot (an asleep inverter alone is normal at night). Say *why* on the meta
  // line instead of leaving the tiles at a bare "—" with no explanation.
  // Two distinct causes, worth telling apart: solar still reading means the
  // inverter is fine and only the power sensor is bad, which is a hardware
  // fault to chase; nothing reading at all is just no data from the source.
  // A third, distinct state (#771): the server is answering with its last good
  // reading because the source stopped refreshing, so it isn't live and isn't
  // plotted as if it were.
  const stale = !!(e.snapshot && e.snapshot.stale);
  const liveNote = stale
    ? 'Last reading ' + Math.round(e.snapshot.age_seconds / 60) + ' min ago — source not refreshing'
    : e.meter_reachable === false
      ? (e.inverter_reachable
        ? 'Grid and home unavailable — the power sensor is reporting invalid readings'
        : 'Live unavailable — no reading from the inverter')
      : null;

  // --- append to the live chart (Generation / Grid-supplied / Consumption) ---
  if (state.liveChart && !stale) {
    pushLivePoint(
      state.liveChart, Math.floor(Date.now() / 1000),
      solar, e.grid_import_w, e.house_consumption_w, LIVE_MAX_POINTS,
    );
    els.liveMeta.textContent = liveNote || (isSnapshotRestored('energyLive') ? snapshotLabel('energyLive') : 'Updated ' + nowLabel());
  } else if (liveNote) {
    els.liveMeta.textContent = liveNote;
  } else if (isSnapshotRestored('energyLive')) {
    els.liveMeta.textContent = snapshotLabel('energyLive');
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

// One Today meter (design.md `meter`): the fill is a share, so it has no pace
// and stays accent; role="meter" carries the same share for assistive tech.
function setMeter(bar, frac) {
  bar.style.transform = 'scaleX(' + (frac || 0) + ')';
  bar.parentElement.setAttribute('aria-valuenow', String(Math.round((frac || 0) * 100)));
}

function renderToday(b, gapHours) {
  const pvWh = b && !b.pv_missing ? b.pv_wh : null;
  const houseWh = b ? b.house_wh : null;
  const exportWh = b ? (b.export_wh || 0) : 0;
  const importWh = b ? (b.import_wh || 0) : 0;

  // Made: self-consumed (pv − fed-in) vs grid feed-in.
  els.genTotal.textContent = fmtKwh(pvWh);
  if (pvWh != null && pvWh > 0) {
    const selfWh = Math.max(0, pvWh - exportWh);
    const frac = clamp01(selfWh / pvWh);
    els.genSelf.textContent = fmtKwhBare(selfWh);
    els.genFeed.textContent = fmtKwhBare(exportWh);
    setMeter(els.genBar, frac);
    els.genPct.textContent = fmtFracPct(frac);
  } else {
    els.genSelf.textContent = '—';
    els.genFeed.textContent = '—';
    setMeter(els.genBar, 0);
    els.genPct.textContent = '—';
  }

  // Used: covered by solar (house − imported) vs grid-supplied.
  els.consTotal.textContent = fmtKwh(houseWh);
  if (houseWh != null && houseWh > 0) {
    const selfWh = Math.max(0, houseWh - importWh);
    const frac = clamp01(selfWh / houseWh);
    els.consSelf.textContent = fmtKwhBare(selfWh);
    els.consGrid.textContent = fmtKwhBare(importWh);
    setMeter(els.consBar, frac);
    els.consPct.textContent = fmtFracPct(frac);
  } else {
    els.consSelf.textContent = '—';
    els.consGrid.textContent = '—';
    setMeter(els.consBar, 0);
    els.consPct.textContent = '—';
  }

  // Savings: CO₂/trees credit all of today's clean PV generation. The €
  // figures are filled by loadSavingsEur() from the tiered tariff (avoided grid
  // cost of the self-consumed PV, and export income) so they agree with the
  // History card's Money view on Day.
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

// ---------------------------------------------------- History › Money (#883)
function currencySymbol(cur) {
  return cur === 'EUR' ? '€' : (cur ? cur + ' ' : '€');
}

function num2(v) {
  return Number(v || 0).toFixed(2);
}

// One figure of a `.kpis` strip: a muted label over a bold tabular value.
// Figures are plain ink: a good number is not a status (design.md tone map).
function kpiHtml(label, value) {
  return '<div class="kpi"><span class="kpi-label">' + esc(label) + '</span>'
    + '<span class="kpi-value">' + esc(value) + '</span></div>';
}

// One label/value line of the All figures sheet (the detail card's `.row`).
function figureRow(label, value) {
  return '<div class="row"><span>' + esc(label) + '</span><span class="figure-value">' + esc(value) + '</span></div>';
}

function rangeLabel(range) {
  const btn = els.rangeBtns.find(function (b) { return b.dataset.range === range; });
  return btn ? btn.textContent : range;
}

// The Money view (one row per tariff period) and the All figures sheet: every
// figure the old seven-column table and eight-stat strip carried, no longer as
// a spreadsheet at 390px.
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
  els.energyFiguresOpen.disabled = !hasData;
  if (!hasData) {
    els.costSummary.innerHTML = '';
    els.costPeriods.replaceChildren();
    els.energyFiguresBody.innerHTML = '';
    els.costNote.textContent = '';
    return;
  }

  els.costSummary.innerHTML = summary ? [
    kpiHtml('Bill estimate', sym + num2(summary.estimated_bill)),
    kpiHtml('Without solar', sym + num2(summary.cost_without_solar)),
    kpiHtml('Saved', sym + num2(totals.savings)),
  ].join('') : '';

  // A tariff period per row: its rate, grid kWh and hours on the meta line,
  // what the grid cost in it as the value. The hours go last, so a long list
  // of them is what an ellipsis cuts; they and the rest of the period's
  // figures are in the All figures sheet.
  els.costPeriods.replaceChildren.apply(els.costPeriods, periods.map(function (p) {
    const meta = [p.rate_eur_kwh != null ? sym + Number(p.rate_eur_kwh).toFixed(3) + '/kWh' : '',
      num2(p.grid_kwh) + ' kWh grid', p.hours].filter(Boolean).join(' · ');
    const value = document.createElement('span');
    value.className = 'row-value';
    value.textContent = sym + num2(p.grid_cost);
    return rowEl({ title: p.label, meta: meta, trail: value, className: 'cost-period-row' });
  }));

  const blocks = [];
  if (summary) {
    blocks.push('<div class="energy-figures-group">'
      + figureRow('Generated', num2(totals.generation_kwh) + ' kWh')
      + figureRow('Saved', sym + num2(totals.savings))
      // Surplus-compensation credit already netted into Est. bill; shown so the
      // figure is visible (renders €0.00 when export_eur_kwh is unconfigured).
      + figureRow('Export income', sym + num2(summary.export_credit))
      + figureRow('Total solar benefit', sym + num2(summary.total_solar_benefit))
      + figureRow('Grid cost', sym + num2(totals.grid_cost))
      + figureRow('Fixed', sym + num2(summary.fixed_cost))
      + figureRow('Est. bill', sym + num2(summary.estimated_bill))
      + figureRow('Without solar', sym + num2(summary.cost_without_solar))
      + '</div>');
  }
  periods.concat(totals ? [Object.assign({ label: 'Total', export_credit: summary && summary.export_credit }, totals)] : [])
    .forEach(function (p) {
      blocks.push('<h3 class="energy-figures-head">' + esc(p.label)
        + (p.hours ? ' <span class="muted">· ' + esc(p.hours) + '</span>' : '') + '</h3>'
        + '<div class="energy-figures-group">'
        + (p.rate_eur_kwh != null ? figureRow('Rate', sym + Number(p.rate_eur_kwh).toFixed(3) + ' / kWh') : '')
        + figureRow('Grid', num2(p.grid_kwh) + ' kWh')
        + figureRow('Solar', num2(p.solar_kwh) + ' kWh')
        + figureRow('Spent', sym + num2(p.grid_cost))
        + figureRow('Saved', sym + num2(p.savings))
        + figureRow('Earned', sym + num2(p.export_credit))
        + '</div>');
    });
  els.energyFiguresBody.innerHTML = blocks.join('');

  if (body && body.configured === false) {
    els.costNote.textContent = 'Flat €0.10/kWh estimate — set config/tariff.json for tiered rates.';
  } else if (body) {
    els.costNote.textContent = (body.tariff_name || 'Tariff') + ' · estimate, all-in prices.';
  } else {
    els.costNote.textContent = '';
  }
  els.energyFiguresRange.textContent = rangeLabel(state.range) + ' · ' + els.costNote.textContent;
}

async function loadCost(range) {
  try {
    const body = await jsonApi('/api/energy/cost?range=' + encodeURIComponent(range));
    renderCost(body);
  } catch (_) {
    els.costEmpty.hidden = false;
  }
}

// Re-read the Money view after an export-rate change, if it is on screen.
function reloadMoney() {
  if (state.historyView === 'money' && state.range !== 'live') return loadCost(state.range);
  return Promise.resolve();
}

// ------------------------------------- export compensation (Settings, #883)
// The editor lives in Settings since #883; the Money view's Export rate row
// shows the current rate and opens it there.
function renderExportRates(body) {
  state.exportRates = (body && body.rates) || [];
  const current = body && body.current_export_eur_kwh;
  els.exportRateCurrent.textContent = current == null ? '—' : '€' + Number(current).toFixed(5) + '/kWh';
  els.exportRateRowMeta.textContent = current == null ? 'Not set' : '€' + Number(current).toFixed(5) + ' / kWh';
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
    await reloadMoney();
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
    await reloadMoney();
    toast('Export rate deleted', 'success');
  } catch (exc) {
    if (!isAuthRequired(exc)) toast("Couldn't delete the export rate", 'error');
  }
}

// The Today card's € figures are always "today" (their own day query),
// independent of the History card's period: the tiered avoided cost, and the
// export income.
async function loadSavingsEur() {
  try {
    const body = await jsonApi('/api/energy/cost?range=day');
    const sym = currencySymbol(body && body.currency);
    const s = body && body.totals ? body.totals.savings : null;
    const x = body && body.summary ? body.summary.export_credit : null;
    els.savEur.textContent = s != null ? sym + Number(s).toFixed(2) : '—';
    els.savExport.textContent = x != null ? sym + Number(x).toFixed(2) : '—';
  } catch (_) {
    // keep the last rendered value
  }
}

// The selected segment is the state (#879): aria-pressed drives both the
// styling and what assistive tech announces.
function markSegments(btns, key, value) {
  btns.forEach(function (btn) {
    btn.setAttribute('aria-pressed', btn.dataset[key] === value ? 'true' : 'false');
  });
}

// ------------------------------------------------- History card (#883)
// One period for both views. Live is the last hour of the flow and has no
// money view, so while it is selected Money is unavailable, and while Money is
// shown Live is; the other segment always says why in its title.
function syncHistory() {
  const live = state.range === 'live';
  const money = !live && state.historyView === 'money';
  markSegments(els.rangeBtns, 'range', state.range);
  markSegments(els.historyViewBtns, 'view', live ? 'energy' : state.historyView);
  els.rangeBtns.forEach(function (btn) {
    const off = btn.dataset.range === 'live' && money;
    btn.disabled = off;
    if (off) btn.title = 'Live shows energy only';
    else btn.removeAttribute('title');
  });
  els.historyViewBtns.forEach(function (btn) {
    const off = btn.dataset.view === 'money' && live;
    btn.disabled = off;
    if (off) btn.title = 'Pick a period for money';
    else btn.removeAttribute('title');
  });
  els.historyLive.hidden = !live;
  els.historyEnergy.hidden = live || money;
  els.historyMoney.hidden = !money;
  els.historyMoneyRows.hidden = !money;
  // A chart created while its view was hidden has no size yet.
  const shown = live ? state.liveChart : money ? state.exportCreditChart : state.aggChart;
  if (shown) shown.resize();
}

// Read whatever the History card now shows.
function loadHistory() {
  if (state.range === 'live') {
    if (state.liveChart) loadLiveHistory();
  } else if (state.historyView === 'money') {
    loadCost(state.range);
  } else if (state.aggChart) {
    loadAggregate(state.range);
  }
}

function setRange(range) {
  state.range = range;
  syncHistory();
  loadHistory();
}

function setHistoryView(view) {
  state.historyView = view;
  syncHistory();
  loadHistory();
}

// --------------------------------------------------- solar forecast card
// A clearer note per reason; the default HTML note covers the common case.
// Both now point at the PV-system card (in Settings since #779) rather than at a file on disk —
// the config is editable in the app since issue #561.
const FORECAST_NOTES = {
  not_configured: 'Add your panel rows in the PV system card in Settings to enable the forecast.',
  no_location: 'Set the home coordinates in the PV system card in Settings to enable the forecast.',
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
    ? 'estimate vs actual' + (gap ? ' · feed offline ' + gap : '')
    : 'estimate';
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
  markSegments(els.forecastDayBtns, 'day', day);
  loadForecast(day);
}

// ------------------------------------------- sun-position diagnostic (#590)
// Read-only companion to the forecast card: the day's measured performance
// ratio against where the sun actually was. Folded away by default, and only
// loaded once opened — it costs an extra irradiance read, and it answers an
// occasional question rather than a glanceable one.
const SUN_OVERLAY_NOTES = {
  not_configured: 'Add your panel rows in the PV system card in Settings.',
  no_location: 'Set the home coordinates in the PV system card in Settings.',
  too_old: 'Irradiance history only reaches back about three months.',
  rate_limited: 'Weather provider is rate-limiting us right now — retrying shortly.',
};

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

async function ensureSunOverlay() {
  if (!els.sunOverlayChart) return;
  // Created on first open, not on tab entry: a canvas inside a closed
  // <details> has no layout box, so Chart.js would size it to zero.
  if (!state.sunOverlayChart) {
    try { await loadChartJs(); } catch (_) { return; }
    if (!state.sunOverlayChart) state.sunOverlayChart = createSunOverlayChart(els.sunOverlayChart);
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
// Resolves once Chart.js is loaded and the charts exist; offline, it resolves
// with the charts still absent and every loader below skips them.
async function ensureCharts() {
  try { await loadChartJs(); } catch (_) { return; }
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
      kpiHtml('Production', num2(sums.production / 1000) + ' kWh'),
      kpiHtml('Consumption', num2(sums.consumption / 1000) + ' kWh'),
      kpiHtml('Solar consumed', num2(sums.solar / 1000) + ' kWh'),
      kpiHtml('Grid imported', num2(sums.grid / 1000) + ' kWh'),
      kpiHtml('Solar exported', num2(sums.exported / 1000) + ' kWh'),
    ].join('');
    els.aggEmpty.hidden = buckets.length > 0;
  } catch (_) {
    els.aggEmpty.hidden = false;
  }
}

export function wireEnergyControls() {
  els.rangeBtns.forEach(function (btn) {
    btn.addEventListener('click', function () { setRange(btn.dataset.range); });
  });
  els.historyViewBtns.forEach(function (btn) {
    btn.addEventListener('click', function () { setHistoryView(btn.dataset.view); });
  });
  syncHistory();
  // History › Money: All figures is read-only (Done only); Export rate opens
  // its editor where it lives now, in Settings (#883).
  const figures = sheet(els.energyFiguresSheet, {
    model: 'instant',
    closeButton: document.getElementById('energyFiguresSheetClose'),
    doneButton: document.getElementById('energyFiguresSheetDone'),
  });
  els.energyFiguresOpen.addEventListener('click', function () { figures.open(els.energyFiguresOpen); });
  els.exportRateOpen.addEventListener('click', function () { showSettings(els.exportRateCard); });
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
    // The chart-fed loaders wait for Chart.js, loaded on the first visit (#760).
    ensureCharts().then(function () {
      syncHistory();   // size the chart that is showing
      loadHistory();   // whatever the History card shows: live, energy or money
      loadForecast(state.forecastDay);  // solar expected-generation forecast
    });
    loadExportRates();     // the Money view's Export rate row
    loadPvSystem();        // the array config that forecast is computed from
    loadEnergy();          // immediate refresh on entry
    loadToday();           // today's split cards + savings
    schedule(LIVE_MS);
    scheduleToday(true);
  } else {
    schedule(SLOW_MS);
    scheduleToday(false);
  }
  // The PV system, Solar boost and sun-position cards live in Settings since
  // #779, export compensation since #883. The sun-position diagnostic refreshes only while it is open (#590) —
  // closed, it costs nothing.
  if (tab === 'settings') {
    loadPvSystem();
    loadExportRates();       // export compensation, moved here from Energy (#883)
    loadBoostCoordinator();  // fleet solar-boost sequencing knobs (#562)
    if (els.sunOverlayCard && els.sunOverlayCard.open) ensureSunOverlay();
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
