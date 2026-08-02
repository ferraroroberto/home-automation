/* PV-system editor for the Energy tab (issue #561).
 *
 * The write side of the solar forecast's inputs: the panel rows (kWp + tilt +
 * azimuth each), the shared performance ratio, and the home coordinates the
 * forecast is computed at. #555 built the multi-sub-array engine but left the
 * config file-only — this card makes it editable without an SSH session.
 *
 * Same summary-row + staged-dialog contract as ./presence-places.js: the row
 * list is a dense collection driven by ./dense-editor.js (Save is the only
 * persistence boundary, the whole list is PUT back, failures roll back), while
 * the system-level fields are inline rows that save on blur — the same split
 * ./presence-location.js already uses for the home location.
 *
 * Coordinates deliberately keep living in config/location.json behind
 * PUT /api/location (shared with the weather tile) rather than being copied
 * into config/pv_system.json: one house, one source of truth for where it is.
 * Only the arrays + performance ratio go to /api/energy/pv-system.
 *
 * Owns the azimuth/format helpers the forecast card also renders with, so the
 * row summaries and the card's params line can never drift apart. Imported
 * one-way by ./energy.js, which hands in the "re-read the forecast" callback —
 * that keeps the dependency acyclic.
 */

'use strict';

import { state, els, toast } from './state.js';
import { jsonApi } from './api.js';
import { denseListEditor } from './dense-editor.js';
import { emptyStateEl } from './empty-state.js';
import { putLocation } from './presence-location.js';

const DEFAULT_PERFORMANCE_RATIO = 0.8;
const DEFAULT_TILT_DEG = 30;

// Called after any successful save so the forecast curve/headline/params line
// re-read the config they were computed from. Registered by ./energy.js and
// resolves with the recomputed day estimate (or null) for the save toast.
let onSaved = function () {};

export function setPvSystemSavedHook(fn) {
  onSaved = typeof fn === 'function' ? fn : function () {};
}

// A save must always end in a toast — if the forecast refetch hangs or
// rejects, race it against a timeout so the confirmation still fires with the
// plain fallback text (issue #564) rather than never appearing.
const SAVED_ESTIMATE_TIMEOUT_MS = 4000;

function safeOnSaved() {
  return Promise.race([
    Promise.resolve().then(onSaved).catch(function () { return null; }),
    new Promise(function (resolve) {
      setTimeout(function () { resolve(null); }, SAVED_ESTIMATE_TIMEOUT_MS);
    }),
  ]);
}

// Appends the recomputed day estimate to a save confirmation, e.g.
// "PV system saved · today's estimate 55.2 kWh" — shared by all three save
// paths (row editor, performance ratio, coordinates) so they can't drift.
function withEstimateSuffix(base, total) {
  return total != null ? base + " · today's estimate " + total + ' kWh' : base;
}

// ------------------------------------------------------ shared formatting

// Azimuth (Open-Meteo convention: 0=S, -90=E, 90=W, ±180=N) → 8-point compass.
const COMPASS = ['N', 'NE', 'E', 'SE', 'S', 'SW', 'W', 'NW', 'N'];
const COMPASS_LONG = {
  N: 'facing north', NE: 'facing north-east', E: 'facing east', SE: 'facing south-east',
  S: 'facing south', SW: 'facing south-west', W: 'facing west', NW: 'facing north-west',
};

export function azimuthCompass(deg) {
  return COMPASS[Math.round(Number(deg) / 45) + 4];
}

// Compass azimuth (0=N, 90=E, 180=S, 270=W — src.sun_position's convention,
// used only by the horizon profile below; deliberately not azimuthCompass()'s
// Open-Meteo south-relative one) → the same 8-point letter (COMPASS_LONG above
// already maps letter → label, and that mapping doesn't depend on convention).
export function compassFromNorth(deg) {
  const n = Math.round((((Number(deg) % 360) + 360) % 360) / 45) % 8;
  return COMPASS[n];
}

// Trim a trailing ".0" so 1.5 → "1.5" but 8 → "8".
export function trimNum(n) {
  return String(Number(n)).replace(/\.0$/, '');
}

// One sub-array's "1.5 kWp · 35° · S".
export function arraySummary(a) {
  return trimNum(a.kwp) + ' kWp · ' + trimNum(a.tilt_deg) + '° · ' + azimuthCompass(a.azimuth_deg);
}

// ------------------------------------------------------------ row rendering

function arrayDefaults() {
  return {
    // Index-derived so the id an appended row is opened with still matches the
    // row after normalize() renumbers — that is what restores focus on close.
    id: 'pv-' + (state.pvArrays || []).length,
    kwp: 1,
    tilt_deg: DEFAULT_TILT_DEG,
    azimuth_deg: 0,
  };
}

// Rows carry no identity on disk (the file is a plain list), so ids are
// positional and re-derived on every set — and stripped again before the PUT.
function normalizedArrays(entries) {
  return (entries || state.pvArrays || []).map(function (entry, idx) {
    return {
      id: 'pv-' + idx,
      kwp: Number(entry.kwp) || 0,
      tilt_deg: Number(entry.tilt_deg) || 0,
      azimuth_deg: Number(entry.azimuth_deg) || 0,
    };
  });
}

function totalKwp(entries) {
  return (entries || []).reduce(function (sum, a) { return sum + (Number(a.kwp) || 0); }, 0);
}

export function renderPvSystem() {
  if (!els.pvArrayList) return;
  els.pvArrayList.innerHTML = '';
  state.pvArrays = normalizedArrays();

  if (els.pvSystemTotal) {
    els.pvSystemTotal.textContent = state.pvArrays.length
      ? trimNum(Math.round(totalKwp(state.pvArrays) * 100) / 100) + ' kWp'
      : '—';
  }

  if (!state.pvArrays.length) {
    els.pvArrayList.appendChild(
      emptyStateEl('sun', 'No panel rows yet — add one to enable the solar forecast.')
    );
    return;
  }

  state.pvArrays.forEach(function (entry, idx) {
    const row = document.createElement('div');
    row.className = 'list-row automation-summary-row';
    row.dataset.pvArrayId = entry.id;

    const main = document.createElement('button');
    main.type = 'button';
    main.className = 'automation-summary-main';
    main.setAttribute('aria-label', 'Edit panel row ' + arraySummary(entry));

    const copy = document.createElement('span');
    copy.className = 'automation-summary-copy';
    const title = document.createElement('span');
    title.className = 'automation-summary-title';
    title.textContent = arraySummary(entry);
    const meta = document.createElement('span');
    meta.className = 'automation-summary-meta';
    meta.textContent = COMPASS_LONG[azimuthCompass(entry.azimuth_deg)] || '';
    copy.appendChild(title);
    copy.appendChild(meta);
    main.appendChild(copy);

    const chevron = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    chevron.setAttribute('class', 'icon automation-summary-chevron');
    chevron.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-chevron-right');
    chevron.appendChild(use);
    main.appendChild(chevron);

    main.addEventListener('click', function () { arrayEditor.open(idx, main); });
    row.appendChild(main);
    els.pvArrayList.appendChild(row);
  });
}

// --------------------------------------- horizon/shading profile (issue #578b)

function horizonDefaults() {
  return {
    // Same index-derived-id trick as arrayDefaults() above.
    id: 'ph-' + (state.pvHorizonProfile || []).length,
    azimuth_deg: 0,
    elevation_deg: 0,
  };
}

// Points carry no identity on disk either — same positional-id contract as
// normalizedArrays() above.
function normalizedHorizonProfile(entries) {
  return (entries || state.pvHorizonProfile || []).map(function (entry, idx) {
    return {
      id: 'ph-' + idx,
      azimuth_deg: Number(entry.azimuth_deg) || 0,
      elevation_deg: Number(entry.elevation_deg) || 0,
    };
  });
}

// One horizon point's "165° · 5° elevation".
function horizonSummary(p) {
  return trimNum(p.azimuth_deg) + '° · ' + trimNum(p.elevation_deg) + '° elevation';
}

export function renderPvHorizonProfile() {
  if (!els.pvHorizonList) return;
  els.pvHorizonList.innerHTML = '';
  state.pvHorizonProfile = normalizedHorizonProfile();

  if (!state.pvHorizonProfile.length) {
    els.pvHorizonList.appendChild(
      emptyStateEl('sun', 'No horizon points yet — the shading term stays inert either way.')
    );
    return;
  }

  state.pvHorizonProfile.forEach(function (entry, idx) {
    const row = document.createElement('div');
    row.className = 'list-row automation-summary-row';
    row.dataset.pvHorizonId = entry.id;

    const main = document.createElement('button');
    main.type = 'button';
    main.className = 'automation-summary-main';
    main.setAttribute('aria-label', 'Edit horizon point ' + horizonSummary(entry));

    const copy = document.createElement('span');
    copy.className = 'automation-summary-copy';
    const title = document.createElement('span');
    title.className = 'automation-summary-title';
    title.textContent = horizonSummary(entry);
    const meta = document.createElement('span');
    meta.className = 'automation-summary-meta';
    meta.textContent = COMPASS_LONG[compassFromNorth(entry.azimuth_deg)] || '';
    copy.appendChild(title);
    copy.appendChild(meta);
    main.appendChild(copy);

    const chevron = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    chevron.setAttribute('class', 'icon automation-summary-chevron');
    chevron.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-chevron-right');
    chevron.appendChild(use);
    main.appendChild(chevron);

    main.addEventListener('click', function () { horizonEditor.open(idx, main); });
    row.appendChild(main);
    els.pvHorizonList.appendChild(row);
  });
}

// ------------------------------------------------------------------- load

export async function loadPvSystem() {
  if (!els.pvArrayList) return;
  try {
    const body = await jsonApi('/api/energy/pv-system');
    state.pvArrays = (body && body.arrays) || [];
    state.pvPerformanceRatio = (body && body.performance_ratio) || DEFAULT_PERFORMANCE_RATIO;
    state.pvHorizonProfile = (body && body.horizon_profile) || [];
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    state.pvArrays = [];
    state.pvHorizonProfile = [];
  }
  if (els.pvPerformanceRatio) els.pvPerformanceRatio.value = state.pvPerformanceRatio;
  renderPvSystem();
  renderPvHorizonProfile();
  await loadPvLocation();
}

async function loadPvLocation() {
  if (!els.pvLat || !els.pvLon) return;
  try {
    state.location = await jsonApi('/api/location');
  } catch (_) {
    return;  // the fields simply stay as they are; the forecast note explains
  }
  els.pvLat.value = state.location.lat == null ? '' : state.location.lat;
  els.pvLon.value = state.location.lon == null ? '' : state.location.lon;
}

// ------------------------------------------------------- field validation

// One error line per dialog field, shown against the offending input rather
// than as a generic toast — a clamp-free write path is only useful if the user
// is told which value was rejected.
const FIELD_ERRORS = [
  ['pvArrayKwp', 'pvArrayKwpError'],
  ['pvArrayTilt', 'pvArrayTiltError'],
  ['pvArrayAzimuth', 'pvArrayAzimuthError'],
  ['pvHorizonAzimuth', 'pvHorizonAzimuthError'],
  ['pvHorizonElevation', 'pvHorizonElevationError'],
];

function clearFieldErrors() {
  FIELD_ERRORS.forEach(function (pair) {
    const input = els[pair[0]];
    const line = els[pair[1]];
    if (input) input.removeAttribute('aria-invalid');
    if (line) { line.hidden = true; line.textContent = ''; }
  });
}

function showFieldError(inputKey, lineKey, message) {
  const input = els[inputKey];
  const line = els[lineKey];
  if (line) { line.textContent = message; line.hidden = false; }
  if (input) { input.setAttribute('aria-invalid', 'true'); input.focus(); }
  return false;
}

// ------------------------------------------------------------ row editor

const arrayEditor = denseListEditor({
  dialog: els.pvArrayDialog,
  addButton: els.pvArrayAdd,
  closeButton: els.pvArrayEditorClose,
  saveButton: els.pvArraySave,
  deleteButton: els.pvArrayDelete,
  titleEl: els.pvArrayEditorTitle,
  listEl: els.pvArrayList,
  focusEl: els.pvArrayKwp,
  rowIdAttr: 'data-pv-array-id',
  titles: { add: 'Add panel row', edit: 'Panel row' },
  deleteConfirm: {
    title: 'Delete this panel row?',
    message: 'The forecast will stop counting these panels.',
  },
  toasts: {
    saved: function (total) { return withEstimateSuffix('PV system saved', total); },
    failed: "Couldn't save the PV system",
  },
  defaults: arrayDefaults,
  getEntries: function () { return state.pvArrays; },
  setEntries: function (entries) { state.pvArrays = entries; },
  normalize: normalizedArrays,
  render: renderPvSystem,
  populate: function (staged) {
    clearFieldErrors();
    els.pvArrayKwp.value = staged.kwp;
    els.pvArrayTilt.value = staged.tilt_deg;
    els.pvArrayAzimuth.value = staged.azimuth_deg;
    renderAzimuthEcho();
  },
  collect: function (staged) {
    clearFieldErrors();

    const kwp = Number(els.pvArrayKwp.value);
    if (!Number.isFinite(kwp) || kwp <= 0) {
      return showFieldError('pvArrayKwp', 'pvArrayKwpError', 'Peak power must be greater than 0.');
    }
    const tilt = Number(els.pvArrayTilt.value);
    if (!Number.isFinite(tilt) || tilt < 0 || tilt > 90) {
      return showFieldError(
        'pvArrayTilt', 'pvArrayTiltError',
        'Tilt must be between 0 and 90° — a panel facing the other way is expressed with azimuth, not a negative tilt.'
      );
    }
    const azimuth = Number(els.pvArrayAzimuth.value);
    if (!Number.isFinite(azimuth) || azimuth < -180 || azimuth > 180) {
      return showFieldError(
        'pvArrayAzimuth', 'pvArrayAzimuthError', 'Azimuth must be between -180 and 180°.'
      );
    }

    staged.kwp = kwp;
    staged.tilt_deg = tilt;
    staged.azimuth_deg = azimuth;
  },
  // Positional ids are a rendering concern — the file stores a plain list.
  payloadEntries: function (entries) {
    return entries.map(function (a) {
      return { kwp: a.kwp, tilt_deg: a.tilt_deg, azimuth_deg: a.azimuth_deg };
    });
  },
  endpoint: '/api/energy/pv-system',
  bodyKey: 'arrays',
  afterSave: function () { return safeOnSaved(); },
});

function renderAzimuthEcho() {
  if (!els.pvArrayAzimuthEcho) return;
  const deg = Number(els.pvArrayAzimuth.value);
  const compass = Number.isFinite(deg) ? azimuthCompass(deg) : null;
  els.pvArrayAzimuthEcho.textContent = compass ? COMPASS_LONG[compass] : '';
}

// --------------------------------------- horizon point editor (issue #578b)

const horizonEditor = denseListEditor({
  dialog: els.pvHorizonDialog,
  addButton: els.pvHorizonAdd,
  closeButton: els.pvHorizonEditorClose,
  saveButton: els.pvHorizonSave,
  deleteButton: els.pvHorizonDelete,
  titleEl: els.pvHorizonEditorTitle,
  listEl: els.pvHorizonList,
  focusEl: els.pvHorizonAzimuth,
  rowIdAttr: 'data-pv-horizon-id',
  titles: { add: 'Add horizon point', edit: 'Horizon point' },
  deleteConfirm: {
    title: 'Delete this horizon point?',
    message: 'The profile will no longer include this direction.',
  },
  toasts: {
    saved: 'Horizon profile saved',
    failed: "Couldn't save the horizon profile",
  },
  defaults: horizonDefaults,
  getEntries: function () { return state.pvHorizonProfile; },
  setEntries: function (entries) { state.pvHorizonProfile = entries; },
  normalize: normalizedHorizonProfile,
  render: renderPvHorizonProfile,
  populate: function (staged) {
    clearFieldErrors();
    els.pvHorizonAzimuth.value = staged.azimuth_deg;
    els.pvHorizonElevation.value = staged.elevation_deg;
    renderHorizonAzimuthEcho();
  },
  collect: function (staged) {
    clearFieldErrors();

    const azimuth = Number(els.pvHorizonAzimuth.value);
    if (!Number.isFinite(azimuth) || azimuth < 0 || azimuth >= 360) {
      return showFieldError(
        'pvHorizonAzimuth', 'pvHorizonAzimuthError', 'Azimuth must be between 0 and 359°.'
      );
    }
    const elevation = Number(els.pvHorizonElevation.value);
    if (!Number.isFinite(elevation) || elevation < 0 || elevation > 90) {
      return showFieldError(
        'pvHorizonElevation', 'pvHorizonElevationError', 'Obstruction elevation must be between 0 and 90°.'
      );
    }

    staged.azimuth_deg = azimuth;
    staged.elevation_deg = elevation;
  },
  // Positional ids are a rendering concern — the file stores a plain list.
  payloadEntries: function (entries) {
    return entries.map(function (p) {
      return { azimuth_deg: p.azimuth_deg, elevation_deg: p.elevation_deg };
    });
  },
  endpoint: '/api/energy/pv-system',
  bodyKey: 'horizon_profile',
  // No afterSave/estimate suffix (unlike the panel-row editor above): the
  // switch that would apply this profile has no editor control and stays
  // off, so saving a point never changes the forecast's numbers.
});

function renderHorizonAzimuthEcho() {
  if (!els.pvHorizonAzimuthEcho) return;
  const deg = Number(els.pvHorizonAzimuth.value);
  const compass = Number.isFinite(deg) ? compassFromNorth(deg) : null;
  els.pvHorizonAzimuthEcho.textContent = compass ? COMPASS_LONG[compass] : '';
}

// ------------------------------------------------- system-level (inline)

async function savePerformanceRatio() {
  if (!els.pvPerformanceRatio) return;
  const ratio = Number(els.pvPerformanceRatio.value);
  if (!Number.isFinite(ratio) || ratio <= 0 || ratio > 1) {
    els.pvPerformanceRatio.value = state.pvPerformanceRatio;
    toast('Performance ratio must be between 0 and 1 (typically 0.75–0.85)', 'error');
    return;
  }
  if (ratio === state.pvPerformanceRatio) return;
  try {
    const body = await jsonApi('/api/energy/pv-system', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        arrays: (state.pvArrays || []).map(function (a) {
          return { kwp: a.kwp, tilt_deg: a.tilt_deg, azimuth_deg: a.azimuth_deg };
        }),
        performance_ratio: ratio,
      }),
    });
    state.pvPerformanceRatio = (body && body.performance_ratio) || ratio;
    const total = await safeOnSaved();
    toast(withEstimateSuffix('PV system saved', total), 'success');
  } catch (exc) {
    els.pvPerformanceRatio.value = state.pvPerformanceRatio;
    if (String(exc.message) !== 'auth required') {
      toast("Couldn't save the PV system", 'error');
    }
  }
}

async function savePvLocation() {
  if (!els.pvLat || !els.pvLon) return;
  const lat = Number(els.pvLat.value);
  const lon = Number(els.pvLon.value);
  if (!Number.isFinite(lat) || lat < -90 || lat > 90) {
    toast('Latitude must be between -90 and 90', 'error');
    return;
  }
  if (!Number.isFinite(lon) || lon < -180 || lon > 180) {
    toast('Longitude must be between -180 and 180', 'error');
    return;
  }
  const home = state.location || {};
  if (Number(home.lat) === lat && Number(home.lon) === lon) return;
  try {
    await putLocation({ lat: lat, lon: lon, label: (home.label || '').trim() });
    const total = await safeOnSaved();
    toast(withEstimateSuffix('Location saved', total), 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast("Couldn't save the location", 'error');
    }
  }
}

// ----------------------------------------------------------------- wiring

export function wirePvSystem() {
  if (!els.pvArrayDialog) return;
  arrayEditor.wire();
  if (els.pvArrayAzimuth) els.pvArrayAzimuth.addEventListener('input', renderAzimuthEcho);
  if (els.pvPerformanceRatio) els.pvPerformanceRatio.addEventListener('blur', savePerformanceRatio);
  [els.pvLat, els.pvLon].forEach(function (el) {
    if (el) el.addEventListener('blur', savePvLocation);
  });
  if (els.pvHorizonDialog) {
    horizonEditor.wire();
    if (els.pvHorizonAzimuth) els.pvHorizonAzimuth.addEventListener('input', renderHorizonAzimuthEcho);
  }
}
