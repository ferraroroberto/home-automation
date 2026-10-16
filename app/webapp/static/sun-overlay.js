/* Sun-position diagnostic, hosted in Settings (#590).
 *
 * Read-only companion to the forecast card: the day's measured performance
 * ratio against where the sun actually was. Split out of ./energy.js, which
 * still restyles the chart on a theme toggle and re-opens the sheet's read when
 * Settings is entered with it open.
 */

'use strict';

import { state, els } from './state.js';
import { jsonApi } from './api.js';
import { localIsoDate } from './format.js';
import { createSunOverlayChart, setSunOverlayData, loadChartJs } from './charts.js';
import { onSettingsSheetOpen } from './settings.js';

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
    els.sunOverlayCount.textContent = '';
    return;
  }
  els.sunOverlayEmpty.hidden = points.length > 0;
  els.sunOverlayEmpty.textContent = 'No measured hours for this day.';
  els.sunOverlayNote.textContent = sunOverlayNote(body);
  els.sunOverlayCount.textContent = points.length ? points.length + ' h' : '';
}

async function loadSunOverlay(day) {
  if (!els.sunOverlaySheet) return;
  try {
    const body = await jsonApi('/api/energy/sun-overlay?date=' + encodeURIComponent(day));
    renderSunOverlay(body);
  } catch (_) {
    renderSunOverlay(null);
  }
}

export async function ensureSunOverlay() {
  if (!els.sunOverlayChart) return;
  // Created on first open, not on entering Settings: a canvas inside a
  // closed sheet has no layout box, so Chart.js would size it to zero.
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

export function wireSunOverlay() {
  if (!els.sunOverlaySheet) return;
  els.sunOverlayDate.max = localIsoDate();
  onSettingsSheetOpen('sunOverlaySheet', ensureSunOverlay);
  els.sunOverlayDate.addEventListener('change', function () {
    const day = els.sunOverlayDate.value;
    if (!day) return;
    state.sunOverlayDate = day;
    loadSunOverlay(day);
  });
}
