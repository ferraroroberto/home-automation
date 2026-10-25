/* Export-compensation editor, hosted in Settings (a sheet).
 *
 * The dated surplus-compensation rates the Money view prices exported energy
 * with: the list of rates, the add / edit / delete form, and the GET / PUT /
 * DELETE /api/energy/export-rates calls behind them. Split out of ./energy.js,
 * which keeps the Money view's Export rate row that opens this sheet.
 *
 * Imported one-way by ./energy.js, which hands in the "re-read the Money view"
 * callback after a change — that keeps the dependency acyclic, the same way
 * ./pv-system.js gets its forecast refetch.
 */

'use strict';

import { state, els, toast } from '../state.js';
import { jsonApi, isAuthRequired } from '../api.js';
import { localIsoDate } from '../format.js';
import { confirmAction } from '../confirm.js';
import { rowEl } from '../row.js';

// Called after a successful add or delete so the Money view, if it is on
// screen, re-reads the cost it priced with the old rates. Registered through
// wireExportRates; resolves when the re-read is done.
let onChanged = function () { return Promise.resolve(); };

function renderExportRates(body) {
  state.exportRates = (body && body.rates) || [];
  const current = body && body.current_export_eur_kwh;
  const rate = current == null ? 'Not set' : '€' + Number(current).toFixed(5) + ' / kWh';
  els.exportRateCurrent.textContent = rate;
  els.exportRateRowMeta.textContent = rate;
  els.exportRateList.innerHTML = '';
  if (!state.exportRates.length) {
    els.exportRateList.innerHTML = '<li><p class="muted small">No export-compensation rate yet.</p></li>';
    return;
  }
  state.exportRates.slice().reverse().forEach(function (rate) {
    const date = rate.effective_from === '0001-01-01' ? 'Legacy rate' : rate.effective_from;
    const hourly = Array.isArray(rate.hourly_eur_kwh) ? ' · hourly overrides' : '';
    const row = rowEl({
      title: date,
      meta: '€' + Number(rate.export_eur_kwh).toFixed(5) + ' / kWh' + hourly,
      onOpen: function () { editExportRate(rate.effective_from); },
      chevron: true,
    });
    row.dataset.rateDate = rate.effective_from;
    els.exportRateList.appendChild(row);
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

export async function loadExportRates() {
  if (!els.exportRateList) return;
  try {
    renderExportRates(await jsonApi('/api/energy/export-rates'));
  } catch (exc) {
    if (!isAuthRequired(exc)) els.exportRateList.innerHTML = '<li><p class="muted small">Rates unavailable.</p></li>';
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
    await onChanged();
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
    await onChanged();
    toast('Export rate deleted', 'success');
  } catch (exc) {
    if (!isAuthRequired(exc)) toast("Couldn't delete the export rate", 'error');
  }
}

export function wireExportRates(onExportRatesChanged) {
  if (typeof onExportRatesChanged === 'function') onChanged = onExportRatesChanged;
  if (!els.exportRateAdd) return;
  els.exportRateDate.value = localIsoDate();
  els.exportRateAdd.addEventListener('click', addExportRate);
  els.exportRateDelete.addEventListener('click', deleteExportRate);
}
