/* The car card on the Devices tab (#teslaCard).
 *
 * Reads GET /api/tesla, which serves the sampler's last stored reading and
 * never calls Tesla, so polling here costs no Fleet API request. Read-only:
 * the card has no verb or switch.
 *
 * Visibility and lifecycle (data-state on the card):
 *  - hidden  → not configured, or not known yet (first read pending or
 *              failed), so an install without a car never flashes a card;
 *  - empty   → set up but no stored reading yet (sampler off or no read so
 *              far), said in the empty-state block;
 *  - ready   → battery, range, charging and last seen;
 *  - stale   → a refresh failed after a good read: the figures stay and the
 *              head says when they were last updated.
 */

'use strict';

import { jsonApi, isAuthRequired } from '../api.js';
import { emptyStateEl } from '../empty-state.js';
import { createTabPollGate, createViewState, ViewState } from '../view-state.js';

// The store changes at most every 2 min (the sampler's active cadence), so a
// minute keeps "last seen" fresh without busy polling.
const POLL_MS = 60_000;

const view = createViewState(null);
view.set(ViewState.HIDDEN);
let latest = null;

const $ = (id) => document.getElementById(id);

const CHARGING_TITLES = {
  Charging: 'Charging',
  Starting: 'Charging',
  Complete: 'Charge complete',
  Stopped: 'Plugged in',
  NoPower: 'Plugged in',
  Disconnected: 'Not plugged in',
};

const STATE_WORDS = { online: 'Online', asleep: 'Asleep', offline: 'Offline', error: 'Unreachable' };

function fmtAgo(tsSeconds) {
  if (tsSeconds == null) return null;
  const mins = Math.max(0, Math.round((Date.now() - tsSeconds * 1000) / 60_000));
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + ' min ago';
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + ' h ago';
  return Math.floor(hours / 24) + ' d ago';
}

function chargingText(sample) {
  const state = sample.charging_state;
  const title = CHARGING_TITLES[state] || state || 'Charging state unknown';
  const charging = sample.charging === true;
  const parts = [];
  if (charging && sample.charger_power_kw != null) {
    parts.push(Number(sample.charger_power_kw).toFixed(1) + ' kW');
  }
  if ((charging || state === 'Complete') && sample.charge_energy_added_kwh) {
    parts.push('+' + Number(sample.charge_energy_added_kwh).toFixed(1) + ' kWh added');
  }
  return { title, meta: parts.join(' · ') };
}

function seenText(body) {
  const ago = fmtAgo(body.sample && body.sample.ts);
  return [STATE_WORDS[body.state], ago ? 'data from ' + ago : null].filter(Boolean).join(' · ');
}

function showEmpty(message) {
  $('teslaEmpty').replaceChildren(emptyStateEl('car', message));
  $('teslaEmpty').hidden = false;
  $('teslaRows').hidden = true;
  $('teslaState').textContent = '';
}

function renderFigures(body) {
  const s = body.sample;
  const pct = s.battery_pct == null ? null : Math.max(0, Math.min(100, Number(s.battery_pct)));
  $('teslaBattery').textContent = pct == null ? '—' : Math.round(pct) + ' %';
  $('teslaRange').textContent = s.range_km == null ? 'Range unknown' : Math.round(s.range_km) + ' km range';
  const meter = $('teslaMeter');
  if (pct == null) meter.removeAttribute('aria-valuenow');
  else meter.setAttribute('aria-valuenow', String(Math.round(pct)));
  $('teslaMeterFill').style.transform = 'scaleX(' + (pct == null ? 0 : pct / 100) + ')';

  const charging = chargingText(s);
  $('teslaCharging').textContent = charging.title;
  $('teslaChargingMeta').textContent = charging.meta;

  // The avatar badge means alive: the car answered awake at the last check.
  $('teslaSeen').textContent = seenText(body);
  const avatar = $('teslaSeenAvatar');
  if (body.awake === true) avatar.dataset.badge = 'up';
  else delete avatar.dataset.badge;

  $('teslaEmpty').hidden = true;
  $('teslaRows').hidden = false;
  $('teslaState').textContent = view.state === ViewState.STALE
    ? view.lastUpdatedLabel() + ' · live data unavailable'
    : '';
}

function render() {
  const card = $('teslaCard');
  if (!card) return;
  card.hidden = view.state === ViewState.HIDDEN;
  card.dataset.state = view.state;
  if (view.state === ViewState.EMPTY) {
    showEmpty(latest.sampler_enabled ? 'No car reading yet' : 'Car data collection is off');
  } else if (view.state === ViewState.READY || view.state === ViewState.STALE) {
    renderFigures(latest);
  }
}

export async function loadTesla() {
  try {
    const body = await jsonApi('/api/tesla');
    if (!body || !body.configured) {
      latest = null;
      view.set(ViewState.HIDDEN);
    } else {
      latest = body;
      view.set(body.sample ? ViewState.READY : ViewState.EMPTY, { updatedAt: new Date(), liveUnavailable: false });
    }
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    // Keep what is showing: figures go stale, an empty or hidden card stays.
    if (view.state === ViewState.READY) view.set(ViewState.STALE, { liveUnavailable: true });
  }
  render();
}

export const onTeslaTab = createTabPollGate(loadTesla, 'iot', POLL_MS);
