/* Circuits (per-breaker CT clamps) data + card controller.
 *
 * Owns the Devices tab's Circuits group: a row for EVERY channel each Athom
 * BL0906 meter has — clamp fitted or not — then a row per meter. Reads
 * GET /api/circuits and writes the per-channel rename / sign-flip / hide
 * endpoints.
 *
 * Two deliberate behaviours, both because clamps get added over time:
 *  - a channel is never dropped for reading 0 W, so a clamp fitted next week
 *    starts showing a live figure with nothing to reconfigure. Hiding one is a
 *    *user* decision (issue #619), never inferred from a reading;
 *  - meters are discovered server-side over mDNS, so a new meter simply
 *    appears here on its own — which is why this card has no Refresh button.
 *
 * One number per row (issue #619): watts. Amps, cumulative kWh, mains voltage,
 * Wi-Fi signal and the meter's MAC are reference figures, so they live in the
 * detail dialog where you go looking for them deliberately.
 *
 * Cadence is tab-aware like plugs.js: poll only while the IoT tab is open. */

'use strict';

import {
  state, els, reportFetchOk, persistedFlag, CIRCUITS_SHOW_HIDDEN_KEY,
} from './state.js';
import { jsonApi, isAuthRequired, reportActionFailure } from './api.js';
import { fmtW, friendlyError } from './format.js';
import { createPoller } from './poll.js';
import { toggleMarkup } from './toggle.js';
import { detailModal } from './detail-modal.js';
import { rowEl } from './row.js';
import { chipEl } from './chip.js';
import { emptyStateEl } from './empty-state.js';

const POLL_MS = 15_000;

// The "show hidden terminals" filter, on the shared localStorage wrapper.
const showHiddenPref = persistedFlag(CIRCUITS_SHOW_HIDDEN_KEY, false);

// ------------------------------------------------------------- lookups
function allChannels() {
  return state.circuits.flatMap(function (meter) {
    return (meter.channels || []).map(function (channel) {
      return { meter: meter, channel: channel };
    });
  });
}

function channelByKey(key) {
  return allChannels().find(function (entry) { return entry.channel.key === key; }) || null;
}

function meterByKey(key) {
  return state.circuits.find(function (meter) { return meter.meter_id === key; }) || null;
}

function meterLabel(meter) {
  return meter.display_name || meter.name || meter.meter_id;
}

// An unlabelled channel still needs a stable, meaningful name — "Clamp 3" is
// what is printed next to the terminal on the meter itself.
function channelLabel(channel) {
  return channel.display_name || 'Clamp ' + channel.channel;
}

// A→Z on the label actually on screen, numeric-aware — so renaming meters
// "1 …", "2 …", "10 …" orders the board the obvious way instead of lexically
// (and instead of mDNS discovery order, which is arbitrary).
function byMeterLabel(a, b) {
  return meterLabel(a).localeCompare(meterLabel(b), undefined, {
    numeric: true, sensitivity: 'base',
  });
}

// Physical terminal order (1..N — the order printed on the meter's own terminal
// block), minus anything the user has put away.
function visibleChannels(meter) {
  const ordered = (meter.channels || []).slice().sort(function (a, b) {
    return a.channel - b.channel;
  });
  if (state.circuitsShowHidden) return ordered;
  return ordered.filter(function (channel) { return !channel.hidden; });
}

// ------------------------------------------------------------- the rows
// The shared row (row.js, #884, Step 6/8 of #872): one row per clamp, its
// watts the one trailing value (issue #619: amps, kWh, voltage and signal are
// in the dialog). The meta names the meter and terminal, so an unlabelled
// clamp can still be traced. A meter's avatar badge means connected (up) or
// should be and is not (down), on each of its clamps and its own row.
function readingEl(meter, channel) {
  const value = document.createElement('span');
  value.className = 'row-value circuit-watts';
  // A meter that is offline still lists its channels (so circuits don't
  // vanish mid-watch), but they carry no readings: say so, never 0 W.
  if (!meter.reachable || channel.power_w == null) {
    value.classList.add('is-muted');
    value.textContent = meter.reachable ? 'No reading' : '—';
    return value;
  }
  value.textContent = fmtW(channel.power_w);
  // A channel reading negative after the correction is applied is worth
  // flagging: on a load circuit it means the clamp direction is still wrong.
  if (channel.power_w < 0) {
    value.classList.add('circuit-watts-negative');
    value.title = 'Reading negative — the clamp may be fitted backwards. '
      + 'Tap the row to flip it.';
  }
  // An idle circuit and a channel with no clamp both read 0 W and are
  // indistinguishable electrically, so neither is dressed up as the other.
  if (channel.power_w === 0) value.classList.add('is-muted');
  return value;
}

function buildChannelRow(meter, channel) {
  const row = rowEl({
    className: 'circuit-row',
    glyph: 'gauge',
    badge: meter.reachable ? 'up' : 'down',
    title: channelLabel(channel),
    meta: meterLabel(meter) + ' · clamp ' + channel.channel,
    // Only listed while Show hidden is on, so it says which rows are the ones
    // normally put away (a plain fact, the neutral chip).
    chip: channel.hidden ? chipEl('Hidden', null, 'device-hidden-chip') : null,
    openLabel: channelLabel(channel) + ', readings, rename, clamp direction',
    onOpen: function (btn) { openCircuitDetail(channel.key, btn); },
    trail: readingEl(meter, channel),
  });
  row.dataset.channelKey = channel.key;
  if (channel.hidden) row.classList.add('is-hidden-circuit');
  return row;
}

// The meter itself, after its clamps: its name (rename it here, so three
// meters read as "main board" rather than a model and a serial) and its
// reference data in the dialog.
function buildMeterRow(meter) {
  const count = (meter.channels || []).length;
  const row = rowEl({
    className: 'circuit-meter',
    glyph: 'gauge',
    badge: meter.reachable ? 'up' : 'down',
    title: meterLabel(meter),
    meta: meter.reachable ? 'Meter · ' + count + (count === 1 ? ' clamp' : ' clamps') : 'Meter',
    chip: meter.reachable ? null : chipEl('Offline', 'attention', 'circuit-meter-offline'),
    chevron: true,
    onOpen: function (btn) { openCircuitDetail(meter.meter_id, btn); },
  });
  row.dataset.meterId = meter.meter_id;
  return row;
}

// --------------------------------------------------------- rename modal
// Staged like the plug modal (#203 pattern): the label and the sign flip are
// held locally and written only on Save.
// One dialog key resolves to either a channel entry ({meter, channel}) or a
// whole meter — the shared detailModal() shell (issue #699) treats both as
// "the entity", tagged by kind so populate/buildOps below can branch.
function circuitEntity(key) {
  const entry = channelByKey(key);
  if (entry) return { kind: 'channel', entry: entry };
  const meter = meterByKey(key);
  if (meter) return { kind: 'meter', meter: meter };
  return null;
}

function renderToggle(btn, on, label) {
  if (!btn) return;
  btn.className = 'toggle' + (on ? ' on' : ' off');
  btn.setAttribute('aria-checked', on ? 'true' : 'false');
  btn.innerHTML = toggleMarkup(on);
  if (label) btn.setAttribute('aria-label', label);
}

// "—" rather than a blank cell: an absent reading is a fact worth showing.
function setReading(el, value, unit, digits) {
  if (!el) return;
  el.textContent = value == null ? '—' : value.toFixed(digits) + ' ' + unit;
}

function patchChannel(key, patch) {
  state.circuits = state.circuits.map(function (meter) {
    return Object.assign({}, meter, {
      channels: (meter.channels || []).map(function (channel) {
        return channel.key === key ? Object.assign({}, channel, patch) : channel;
      }),
    });
  });
}

function patchMeter(key, patch) {
  state.circuits = state.circuits.map(function (meter) {
    return meter.meter_id === key ? Object.assign({}, meter, patch) : meter;
  });
}

// One dialog serves both a channel and a whole meter: the key tells them apart
// (a channel key is its meter's id plus ":<channel>"). A meter has no clamp
// direction to correct, and hiding one would hide every circuit under it, so
// both toggle sections are for a channel only — and each gets the read-only
// block that suits it.
const circuitModal = detailModal({
  dialog: els.circuitDialog,
  closeButton: els.circuitDetailClose,
  onClose: function () { state.selectedCircuitKey = null; },
  saveButton: els.circuitSave,
  focusEl: els.circuitDisplayName,
  getEntity: circuitEntity,
  stage: function (entity) {
    if (entity.kind === 'meter') return { invert: false, hidden: false };
    return { invert: !!entity.entry.channel.inverted, hidden: !!entity.entry.channel.hidden };
  },
  populate: function (staged, entity) {
    const entry = entity.kind === 'channel' ? entity.entry : null;
    const meter = entity.kind === 'meter' ? entity.meter : null;

    if (els.circuitInvertSection) els.circuitInvertSection.hidden = !entry;
    if (els.circuitHiddenSection) els.circuitHiddenSection.hidden = !entry;
    if (els.circuitReadings) els.circuitReadings.hidden = !entry;
    if (els.circuitMeterInfo) els.circuitMeterInfo.hidden = !meter;

    if (meter) {
      els.circuitDetailName.textContent = meterLabel(meter);
      els.circuitDisplayName.value = meter.display_name || '';
      els.circuitDisplayName.placeholder = meter.name || 'Custom label…';
      if (els.circuitOriginalName) {
        els.circuitOriginalName.textContent =
          (meter.name || meter.meter_id) + (meter.host ? ' · ' + meter.host : '');
      }
      setReading(els.circuitMeterVoltage, meter.voltage_v, 'V', 0);
      // fmtW already renders a missing value as the same em dash setReading uses.
      if (els.circuitMeterTotal) els.circuitMeterTotal.textContent = fmtW(meter.total_power_w);
      setReading(els.circuitMeterSignal, meter.wifi_rssi_dbm, 'dBm', 0);
      if (els.circuitMeterMac) {
        // Statically-configured meters have no MAC until read — the server sends
        // null rather than dressing "host:<ip>" up as one.
        els.circuitMeterMac.textContent = meter.mac || '—';
      }
    } else {
      els.circuitDetailName.textContent = channelLabel(entry.channel);
      els.circuitDisplayName.value = entry.channel.display_name || '';
      els.circuitDisplayName.placeholder = 'Clamp ' + entry.channel.channel;
      if (els.circuitOriginalName) {
        // Which physical terminal this is, so an unlabelled clamp stays traceable.
        els.circuitOriginalName.textContent =
          meterLabel(entry.meter) + ' · channel ' + entry.channel.channel;
      }
      if (els.circuitReadingPower) {
        els.circuitReadingPower.textContent = fmtW(entry.channel.power_w);
      }
      setReading(els.circuitReadingCurrent, entry.channel.current_a, 'A', 2);
      setReading(els.circuitReadingEnergy, entry.channel.energy_kwh, 'kWh', 2);
      renderToggle(els.circuitInvertToggle, staged.invert);
      renderToggle(els.circuitHiddenToggle, staged.hidden);
    }
  },
  buildOps: function (key, staged, entity) {
    const entry = entity.kind === 'channel' ? entity.entry : null;
    const meter = entity.kind === 'meter' ? entity.meter : null;
    const ops = [];
    const newName = els.circuitDisplayName.value.trim();
    const currentName = (entry ? entry.channel.display_name : meter.display_name) || '';
    if (currentName !== newName) {
      ops.push(jsonApi('/api/circuits/' + encodeURIComponent(key) + '/display_name', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ display_name: newName }),
      }).then(function () {
        const patch = { display_name: newName || null };
        if (entry) patchChannel(key, patch); else patchMeter(key, patch);
      }));
    }
    // Only a channel has a clamp direction or a hidden flag; a meter never
    // sends either of these.
    if (!!entry && !!entry.channel.inverted !== staged.invert) {
      ops.push(jsonApi('/api/circuits/' + encodeURIComponent(key) + '/invert', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ invert: staged.invert }),
      }).then(function () { patchChannel(key, { inverted: staged.invert }); }));
    }
    if (!!entry && !!entry.channel.hidden !== staged.hidden) {
      ops.push(jsonApi('/api/circuits/' + encodeURIComponent(key) + '/hidden', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hidden: staged.hidden }),
      }).then(function () { patchChannel(key, { hidden: staged.hidden }); }));
    }
    return ops;
  },
  afterSave: function (key, staged, entity) {
    const updatedChannel = channelByKey(key);
    const updatedMeter = meterByKey(key);
    if (updatedChannel) els.circuitDetailName.textContent = channelLabel(updatedChannel.channel);
    else if (updatedMeter) els.circuitDetailName.textContent = meterLabel(updatedMeter);
    // A sign flip changes what the server reports, so pull a fresh read
    // rather than leaving the old sign on screen until the next poll.
    const entry = entity.kind === 'channel' ? entity.entry : null;
    const signChanged = !!entry && !!entry.channel.inverted !== staged.invert;
    if (signChanged) loadCircuits();
  },
  render: renderCircuits,
});

function openCircuitDetail(key, trigger) {
  if (!circuitEntity(key)) return;
  state.selectedCircuitKey = key;
  circuitModal.open(key, trigger);
}

function toggleCircuitInvert() {
  if (!circuitModal.staged) return;
  circuitModal.staged.invert = !circuitModal.staged.invert;
  renderToggle(els.circuitInvertToggle, circuitModal.staged.invert);
  circuitModal.markDirty();
}

function toggleCircuitHidden() {
  if (!circuitModal.staged) return;
  circuitModal.staged.hidden = !circuitModal.staged.hidden;
  renderToggle(els.circuitHiddenToggle, circuitModal.staged.hidden);
  circuitModal.markDirty();
}

// ------------------------------------------------------------- render
function setNote(message) {
  if (!els.circuitsNote) return;
  els.circuitsNote.textContent = message || '';
  els.circuitsNote.hidden = !message;
}

// Show hidden only exists once something is actually put away — the same
// group-foot verb as the Plugs group.
function renderHiddenToggle() {
  const n = state.circuitsHiddenCount || 0;
  const btn = els.circuitsHiddenToggle;
  if (!btn) return;
  btn.hidden = n === 0;
  btn.textContent = state.circuitsShowHidden ? 'Hide hidden' : 'Show hidden (' + n + ')';
  btn.setAttribute('aria-pressed', state.circuitsShowHidden ? 'true' : 'false');
}

export function renderCircuits() {
  if (!els.circuitsList) return;
  els.circuitsList.innerHTML = '';

  const channels = allChannels();
  state.circuitsHiddenCount = channels.filter(function (entry) {
    return entry.channel.hidden;
  }).length;
  renderHiddenToggle();

  // The group's meta is how many meters there are.
  const meters = state.circuits.length;
  if (els.circuitsCount) {
    els.circuitsCount.textContent = meters === 1 ? '1 meter' : meters + ' meters';
    els.circuitsCount.hidden = meters === 0;
  }

  if (!meters) {
    // The empty-state block rather than a vanished group: "no meters found"
    // is a state worth seeing. A discovery problem is its own fact.
    setNote('');
    els.circuitsList.appendChild(emptyStateEl('gauge', state.circuitsError ||
      'No meters yet. They appear once powered and on the Wi-Fi.'));
    return;
  }
  setNote(state.circuitsError || '');

  // Meters A→Z by name; each meter's clamps in physical terminal order, then
  // the meter's own row. Sorted on a copy — state.circuits mirrors the server.
  const list = document.createElement('ul');
  list.className = 'action-rows';
  state.circuits.slice().sort(byMeterLabel).forEach(function (meter) {
    visibleChannels(meter).forEach(function (channel) {
      list.appendChild(buildChannelRow(meter, channel));
    });
  });
  state.circuits.slice().sort(byMeterLabel).forEach(function (meter) {
    list.appendChild(buildMeterRow(meter));
  });
  els.circuitsList.appendChild(list);
}

// --------------------------------------------------------------- load
export async function loadCircuits() {
  try {
    const body = await jsonApi('/api/circuits');
    reportFetchOk('circuits');
    state.circuits = (body && body.meters) || [];
    // A discovery problem is reported as its own fact, never folded into
    // "no meters" — the two need different actions from whoever is reading.
    state.circuitsError = body && body.error ? friendlyError(body.error, 'Meter discovery failed.') : '';
    renderCircuits();
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    state.circuitsError = 'Circuits unavailable: ' + friendlyError(exc);
    renderCircuits();
  }
}

// There is no Refresh button (issue #619): the card re-polls on its own while
// the IoT tab is open, and a meter that joins the Wi-Fi appears by itself once
// the server's mDNS discovery TTL lapses. POST /api/circuits/refresh still
// exists for a forced sweep from the command line.
export function wireCircuitsToggle() {
  state.circuitsShowHidden = showHiddenPref.read();

  if (!els.circuitsHiddenToggle) return;
  els.circuitsHiddenToggle.addEventListener('click', function () {
    state.circuitsShowHidden = !state.circuitsShowHidden;
    showHiddenPref.write(state.circuitsShowHidden);
    renderCircuits();
  });
}

// Wire the rename modal once at boot (mirrors the plug detail-modal wiring).
export function wireCircuitDetail() {
  if (!els.circuitDialog) return;
  els.circuitDisplayName.addEventListener('input', circuitModal.markDirty);
  els.circuitDisplayName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); circuitModal.save(); }
  });
  if (els.circuitInvertToggle) {
    els.circuitInvertToggle.addEventListener('click', toggleCircuitInvert);
  }
  if (els.circuitHiddenToggle) {
    els.circuitHiddenToggle.addEventListener('click', toggleCircuitHidden);
  }
  if (els.circuitSave) els.circuitSave.addEventListener('click', circuitModal.save);
}

// --------------------------------------------------------- cadence + tabs
const schedule = createPoller(loadCircuits);

export function onCircuitsTab(tab) {
  if (tab === 'iot') {
    loadCircuits();
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}
