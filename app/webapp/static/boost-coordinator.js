/* Fleet solar-boost sequencing card for the Energy tab (issue #562).
 *
 * #554 shipped the per-unit solar boost; every eligible unit then reacted to the
 * same fleet-wide surplus reading in the same tick, which oscillates rather than
 * controls. The engine now admits and sheds one unit per settle interval — this
 * card is where those fleet-level knobs are tuned, without the tray restart the
 * `.env` boost thresholds still need.
 *
 * Deliberately *not* a dense collection (./pv-system.js) — there is no list of
 * entries here, just four system-level values, so it follows that card's other
 * half: inline `label.row` fields that validate and save on blur, roll the input
 * back on rejection, and toast the outcome. The per-unit boost opt-in
 * (boost_enabled / boost_offset_c) is untouched and stays in the unit detail
 * modal; nothing here is per room.
 *
 * Seconds are the wire format. The settle interval is *rendered* in minutes
 * because that is how the floor reads on a phone ("5 min", not "300 s"); the
 * conversion lives in exactly the two places below and nowhere else.
 */

'use strict';

import { state, els, toast } from './state.js';
import { jsonApi, isAuthRequired } from './api.js';

const ENDPOINT = '/api/hvac/boost-coordinator';
const SECONDS_PER_MIN = 60;

// Mirrors src/hvac_automation.py's own defaults, used only until the first
// response lands (and if the endpoint is unreachable, so the card renders
// something honest rather than blanks).
const DEFAULTS = {
  settle_interval_s: 300,
  admission_margin_w: 0,
  hard_deficit_w: 1000,
  ordering_policy: 'stable',
  min_settle_interval_s: 300,
  ordering_policies: ['stable'],
};

function coord() {
  return state.boostCoord || DEFAULTS;
}

// Trim a trailing ".0" so 1.5 → "1.5" but 300 → "300".
function trimNum(n) {
  return String(Number(n)).replace(/\.0$/, '');
}

// ------------------------------------------------------------------ render

export function renderBoostCoordinator() {
  const cfg = coord();
  if (els.boostCoordSummary) {
    // Reads without expanding: the cadence and the headroom, the two knobs that
    // decide how fast the fleet ramps.
    els.boostCoordSummary.textContent =
      trimNum(cfg.settle_interval_s / SECONDS_PER_MIN) + ' min · +' +
      trimNum(cfg.admission_margin_w) + ' W';
  }
  if (els.boostSettleMin) {
    els.boostSettleMin.value = trimNum(cfg.settle_interval_s / SECONDS_PER_MIN);
    // The floor is a physical constraint (the solar meter's publish cadence), so
    // it comes from the server rather than being hand-copied here.
    const floorMin = Number(cfg.min_settle_interval_s || DEFAULTS.min_settle_interval_s);
    els.boostSettleMin.min = String(Math.ceil(floorMin / SECONDS_PER_MIN));
  }
  if (els.boostAdmissionMargin) els.boostAdmissionMargin.value = trimNum(cfg.admission_margin_w);
  if (els.boostHardDeficit) els.boostHardDeficit.value = trimNum(cfg.hard_deficit_w);
  if (els.boostOrderingPolicy) els.boostOrderingPolicy.value = cfg.ordering_policy;
}

// -------------------------------------------------------------------- load

export async function loadBoostCoordinator() {
  if (!els.boostSettleMin) return;
  try {
    state.boostCoord = await jsonApi(ENDPOINT);
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    // Never blank the card on a failed read — show the defaults the engine
    // itself falls back to.
    state.boostCoord = state.boostCoord || DEFAULTS;
  }
  renderBoostCoordinator();
}

// -------------------------------------------------------------------- save

/* PUT one changed field. Only the edited key is sent — the API keeps the stored
 * value for anything omitted — so two rows edited in quick succession can never
 * have the first one's stale value written back over the second. */
async function saveField(key, value, label) {
  const body = {};
  body[key] = value;
  try {
    state.boostCoord = await jsonApi(ENDPOINT, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    renderBoostCoordinator();
    toast('Solar boost saved', 'success');
  } catch (exc) {
    renderBoostCoordinator();  // roll the input back to what is stored
    if (!isAuthRequired(exc)) {
      toast("Couldn't save " + label, 'error');
    }
  }
}

function saveSettleInterval() {
  if (!els.boostSettleMin) return;
  const minutes = Number(els.boostSettleMin.value);
  const floorMin = Number(coord().min_settle_interval_s) / SECONDS_PER_MIN;
  if (!Number.isFinite(minutes) || minutes < floorMin) {
    renderBoostCoordinator();
    toast(
      'Settle interval must be at least ' + trimNum(floorMin) +
      ' min — the solar meter only publishes that often',
      'error'
    );
    return;
  }
  const seconds = Math.round(minutes * SECONDS_PER_MIN);
  if (seconds === coord().settle_interval_s) return;
  saveField('settle_interval_s', seconds, 'the settle interval');
}

function savePositiveWatts(el, key, label) {
  if (!el) return;
  const watts = Number(el.value);
  if (!Number.isFinite(watts) || watts < 0) {
    renderBoostCoordinator();
    toast(label.charAt(0).toUpperCase() + label.slice(1) + ' must be 0 W or more', 'error');
    return;
  }
  if (watts === Number(coord()[key])) return;
  saveField(key, watts, 'the ' + label);
}

function saveOrderingPolicy() {
  if (!els.boostOrderingPolicy) return;
  const policy = els.boostOrderingPolicy.value;
  if (policy === coord().ordering_policy) return;
  saveField('ordering_policy', policy, 'the admission order');
}

// ------------------------------------------------------------------ wiring

export function wireBoostCoordinator() {
  if (!els.boostSettleMin) return;
  els.boostSettleMin.addEventListener('blur', saveSettleInterval);
  els.boostAdmissionMargin.addEventListener('blur', function () {
    savePositiveWatts(els.boostAdmissionMargin, 'admission_margin_w', 'admission margin');
  });
  els.boostHardDeficit.addEventListener('blur', function () {
    savePositiveWatts(els.boostHardDeficit, 'hard_deficit_w', 'fast-shed import');
  });
  // A select commits on change, not on blur — there is no partially-typed state.
  els.boostOrderingPolicy.addEventListener('change', saveOrderingPolicy);
}
