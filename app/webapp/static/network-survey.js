/* Network tab — Wi-Fi walk test (site survey), issue #547.
 *
 * The Wi-Fi diagnostics card next door scans from the *server PC*, so it only
 * ever describes coverage where that PC sits. This card answers the question the
 * house actually poses — where is Wi-Fi bad — by measuring coverage where you
 * are standing.
 *
 * It cannot do that by scanning from the phone, and that is a platform limit
 * rather than a gap to close later: there is no `navigator.wifi` in any browser,
 * `navigator.connection` is unimplemented in WebKit (so an iPhone reports
 * nothing at all), and even a native iOS app needs the `NEHotspotHelper`
 * entitlement Apple reserves for hotspot vendors to read RSSI. So the phone is
 * the *probe* and the AP/router is the *meter*: you pick this device once, and
 * each "Record here" asks the server how well the infrastructure currently hears
 * that MAC, while the browser times its own round-trip to the same server.
 *
 * The device pick lives in localStorage rather than on the server because over
 * Tailscale every client arrives from a 100.x address — there is nothing for the
 * server to map back to a LAN MAC. Sibling of network-devices/-wifi/-dhcp under
 * the issue-#197 split; the boot module (network.js) calls renderSurvey here.
 */

'use strict';

import { state, els, toast, NETWORK_SURVEY_MAC_KEY } from './state.js';
import { api, jsonApi } from './api.js';
import { emptyStateEl } from './empty-state.js';
import { icon } from './_vendored/icons/icons.js';
// Same identity precedence the device list uses (custom label → vendor →
// hostname → MAC), imported rather than restated so a rename shows up here too.
import { deviceLabel } from './network-devices.js';

// One warm-up round-trip (discarded — it pays for connection setup) plus this
// many timed ones. Ten keeps a sample under ~2 s on a healthy link while still
// giving the median something to be robust about.
const RTT_SAMPLES = 10;
const RTT_TIMEOUT_MS = 5_000;
// 2 MiB: long enough to time meaningfully on a fast link, short enough not to
// stall the sample on a weak one. Must stay within the server's 8 MiB cap.
const PAYLOAD_BYTES = 2 * 1024 * 1024;
const PAYLOAD_TIMEOUT_MS = 20_000;
// The AP SOAP read and the router login run concurrently but are individually
// slow, so the recording POST gets a longer budget than the 30 s api() default.
const RECORD_TIMEOUT_MS = 45_000;

const BAND_LABELS = { '2.4GHz': '2.4 GHz', '5GHz': '5 GHz', '6GHz': '6 GHz', wired: 'wired' };
// Which box actually measured the sample — the load-bearing column for an
// "where should the next access point go" decision.
const SOURCE_LABELS = { ap: 'via AP', router: 'via router', both: 'via AP + router' };

let surveyLoaded = false;
let recording = false;

// --------------------------------------------------------------- formatting
function bandLabel(band) {
  return BAND_LABELS[band] || band || '—';
}

function round1(v) {
  return v == null ? null : Math.round(v * 10) / 10;
}

function agoLabel(ts) {
  if (!ts) return '—';
  const secs = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (secs < 60) return 'just now';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return mins + ' min ago';
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + ' h ago';
  return Math.floor(hours / 24) + ' d ago';
}

function signalClass(signal) {
  if (signal == null) return '';
  if (signal < 60) return ' is-weak';
  if (signal >= 80) return ' is-strong';
  return '';
}

// --------------------------------------------------------------- device pick
function readSurveyMac() {
  try {
    return localStorage.getItem(NETWORK_SURVEY_MAC_KEY) || null;
  } catch (_e) {
    return null; // private mode — the pick stays in memory for this session
  }
}

function writeSurveyMac(mac) {
  state.networkSurveyMac = mac || null;
  try {
    if (mac) localStorage.setItem(NETWORK_SURVEY_MAC_KEY, mac);
    else localStorage.removeItem(NETWORK_SURVEY_MAC_KEY);
  } catch (_e) { /* private mode — in-memory only */ }
}

export function initSurveyMacPref() {
  state.networkSurveyMac = readSurveyMac();
}

function wirelessDevices() {
  const list = (state.network && state.network.devices) || [];
  return list.filter(function (d) {
    return d.online !== false && d.conn_type && d.conn_type !== 'wired';
  }).slice().sort(function (a, b) {
    return (b.signal || 0) - (a.signal || 0);
  });
}

function deviceByMac(mac) {
  const list = (state.network && state.network.devices) || [];
  const target = String(mac || '').toUpperCase();
  return list.find(function (d) { return String(d.mac || '').toUpperCase() === target; }) || null;
}

function pickedLabel() {
  const mac = state.networkSurveyMac;
  if (!mac) return null;
  const dev = deviceByMac(mac);
  return dev ? deviceLabel(dev) : mac + ' (offline)';
}

// --------------------------------------------------------------- probes
// A probe that fails reports null rather than a zero: "we could not measure
// this" and "this measured zero" are different facts, and only one of them is
// something the walk test should draw a bar for.
async function measureRtt() {
  const times = [];
  let failures = 0;
  for (let i = 0; i <= RTT_SAMPLES; i++) {
    const t0 = performance.now();
    try {
      await api('/api/version', { cache: 'no-store', timeoutMs: RTT_TIMEOUT_MS });
      if (i > 0) times.push(performance.now() - t0);
    } catch (exc) {
      if (String(exc.message) === 'auth required') throw exc;
      if (i > 0) failures++;
    }
  }
  if (!times.length) return { rtt_ms: null, jitter_ms: null, loss_pct: 100 };
  const sorted = times.slice().sort(function (a, b) { return a - b; });
  const median = sorted[Math.floor(sorted.length / 2)];
  const jitter = times.reduce(function (sum, t) {
    return sum + Math.abs(t - median);
  }, 0) / times.length;
  return {
    rtt_ms: round1(median),
    jitter_ms: round1(jitter),
    loss_pct: round1((failures / RTT_SAMPLES) * 100),
  };
}

async function measureThroughput() {
  const t0 = performance.now();
  try {
    const res = await api('/api/network/survey/payload?bytes=' + PAYLOAD_BYTES, {
      cache: 'no-store',
      timeoutMs: PAYLOAD_TIMEOUT_MS,
    });
    if (!res.ok) return null;
    const buf = await res.arrayBuffer();
    const seconds = (performance.now() - t0) / 1000;
    if (!(seconds > 0) || !buf.byteLength) return null;
    return round1((buf.byteLength * 8) / seconds / 1e6);
  } catch (exc) {
    if (String(exc.message) === 'auth required') throw exc;
    return null;
  }
}

// --------------------------------------------------------------- data
async function loadSurvey() {
  state.networkSurvey = await jsonApi('/api/network/survey');
}

function setProgress(text) {
  if (!els.netSurveyProgress) return;
  els.netSurveyProgress.hidden = !text;
  els.netSurveyProgress.textContent = text || '';
}

async function recordHere() {
  if (recording) return;
  const mac = state.networkSurveyMac;
  if (!mac) {
    toast('Pick which device you are holding first', 'error');
    openDevicePicker();
    return;
  }
  const room = (els.netSurveyRoom.value || '').trim();
  if (!room) {
    toast('Name the room first', 'error');
    els.netSurveyRoom.focus();
    return;
  }

  recording = true;
  els.netSurveyRecord.disabled = true;
  try {
    setProgress('Measuring latency…');
    const rtt = await measureRtt();
    setProgress('Measuring throughput…');
    const throughput = await measureThroughput();
    setProgress('Asking the access point how it hears this device…');
    const sample = await jsonApi('/api/network/survey', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      timeoutMs: RECORD_TIMEOUT_MS,
      body: JSON.stringify({
        room: room,
        mac: mac,
        rtt_ms: rtt.rtt_ms,
        jitter_ms: rtt.jitter_ms,
        loss_pct: rtt.loss_pct,
        throughput_mbps: throughput,
      }),
    });
    await loadSurvey();
    renderSurvey();
    toast(
      sample.found
        ? 'Recorded ' + room + ' at ' + (sample.signal == null ? 'unknown signal' : sample.signal + '%')
        : 'Recorded ' + room + ' — not seen on either radio',
      // Not an error: standing somewhere with no coverage is a real, useful
      // result, so it gets the neutral toast rather than the red one.
      sample.found ? 'success' : ''
    );
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to record: ' + (exc.message || exc), 'error');
    }
  } finally {
    recording = false;
    els.netSurveyRecord.disabled = false;
    setProgress('');
  }
}

async function deleteRoom(room) {
  try {
    await jsonApi('/api/network/survey/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ room: room }),
    });
    await loadSurvey();
    renderSurvey();
    toast('Deleted ' + room, 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to delete: ' + (exc.message || exc), 'error');
    }
  }
}

// --------------------------------------------------------------- rendering
function roomRow(entry) {
  const row = document.createElement('div');
  row.className = 'net-survey-row' + signalClass(entry.last_signal) +
    (entry.last_found ? '' : ' is-missing');

  const main = document.createElement('div');
  main.className = 'net-survey-row-main';
  const name = document.createElement('span');
  name.className = 'net-survey-row-name';
  name.textContent = entry.room;
  main.appendChild(name);
  row.appendChild(main);

  const sig = document.createElement('span');
  sig.className = 'net-device-signal net-survey-row-signal';
  if (!entry.last_found) {
    sig.textContent = 'not seen';
  } else if (entry.last_signal != null) {
    const bar = document.createElement('span');
    bar.className = 'net-signal-bar';
    const fill = document.createElement('span');
    fill.className = 'net-signal-fill';
    fill.style.width = Math.max(0, Math.min(100, entry.last_signal)) + '%';
    bar.appendChild(fill);
    sig.appendChild(bar);
    const pct = document.createElement('span');
    pct.className = 'net-signal-pct';
    pct.textContent = entry.last_signal + '%';
    sig.appendChild(pct);
  } else {
    sig.textContent = '—';
  }
  row.appendChild(sig);

  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'net-survey-row-delete hit-target';
  del.title = 'Delete this room’s samples';
  del.setAttribute('aria-label', 'Delete samples for ' + entry.room);
  del.innerHTML = icon('trash-2');
  del.addEventListener('click', function () { deleteRoom(entry.room); });
  row.appendChild(del);

  const meta = document.createElement('span');
  meta.className = 'net-survey-row-meta';
  const bits = [];
  if (entry.last_found) {
    bits.push(bandLabel(entry.last_band));
    bits.push(SOURCE_LABELS[entry.last_source] || entry.last_source || 'source unknown');
  } else {
    bits.push('on neither radio');
  }
  if (entry.last_rtt_ms != null) bits.push(entry.last_rtt_ms + ' ms');
  if (entry.last_throughput_mbps != null) bits.push(entry.last_throughput_mbps + ' Mbps');
  if (entry.count > 1 && entry.best_signal != null && entry.worst_signal != null) {
    bits.push(entry.count + ' samples · ' + entry.worst_signal + '–' + entry.best_signal + '%');
  }
  bits.push(agoLabel(entry.last_recorded_at));
  meta.textContent = bits.filter(Boolean).join(' · ');
  row.appendChild(meta);

  return row;
}

function renderRoomList() {
  const rooms = (state.networkSurvey && state.networkSurvey.rooms) || [];
  els.netSurveyRooms.innerHTML = '';
  if (!rooms.length) {
    els.netSurveyRooms.appendChild(emptyStateEl(
      'map-pin',
      state.networkSurveyMac
        ? 'No samples yet — name a room and tap Record here.'
        : 'No samples yet — pick this device, then record a room.'
    ));
    return;
  }
  rooms.forEach(function (entry) { els.netSurveyRooms.appendChild(roomRow(entry)); });
}

function renderRoomSuggestions() {
  const known = (state.networkSurvey && state.networkSurvey.known_rooms) || [];
  els.netSurveyRoomList.innerHTML = '';
  known.forEach(function (room) {
    const opt = document.createElement('option');
    opt.value = room;
    els.netSurveyRoomList.appendChild(opt);
  });
}

function renderSummary() {
  const rooms = (state.networkSurvey && state.networkSurvey.rooms) || [];
  if (!rooms.length) {
    els.netSurveyStatus.textContent = '—';
    els.netSurveyStatus.className = 'muted small';
    return;
  }
  // room_summary sorts weakest-first, so the head of the list is the headline.
  const weakest = rooms[0];
  const label = weakest.last_found
    ? (weakest.last_signal == null ? 'signal unknown' : weakest.last_signal + '%')
    : 'no signal';
  els.netSurveyStatus.textContent = rooms.length + ' rooms · ' + weakest.room + ' ' + label;
  els.netSurveyStatus.className = 'muted small net-survey-status' +
    (weakest.last_found ? signalClass(weakest.last_signal) : ' is-weak');
}

export function renderSurvey() {
  if (!els.netSurveyCard) return;

  // Lazy first load: the samples only change when the user records one, so this
  // rides the Network tab's first render rather than its 15 s poll.
  if (!surveyLoaded) {
    surveyLoaded = true;
    loadSurvey().then(function () {
      // Clear any note a previous failed attempt left behind — a retry that
      // succeeded must not keep reading as unavailable.
      els.netSurveyNote.hidden = true;
      els.netSurveyNote.textContent = '';
      renderSurvey();
    }).catch(function (exc) {
      // Reset the latch so the next poll retries rather than leaving the card
      // permanently empty after one transient failure.
      surveyLoaded = false;
      if (String(exc.message) !== 'auth required') {
        els.netSurveyNote.hidden = false;
        els.netSurveyNote.textContent = 'Walk-test history unavailable.';
      }
    });
  }

  const label = pickedLabel();
  els.netSurveyDeviceName.textContent = label || 'No device picked';
  els.netSurveyDeviceName.classList.toggle('is-unset', !label);
  els.netSurveyDevicePick.textContent = label ? 'Change' : 'Pick device';
  els.netSurveyRecord.disabled = recording || !state.networkSurveyMac;

  renderSummary();
  renderRoomSuggestions();
  renderRoomList();
}

// --------------------------------------------------------------- device picker
function pickerRow(d) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'net-survey-picker-row' +
    (String(d.mac || '').toUpperCase() === String(state.networkSurveyMac || '').toUpperCase()
      ? ' is-current' : '');

  const name = document.createElement('span');
  name.className = 'net-survey-picker-name';
  name.textContent = deviceLabel(d);
  btn.appendChild(name);

  const meta = document.createElement('span');
  meta.className = 'net-survey-picker-meta';
  meta.textContent = [
    bandLabel(d.conn_type),
    d.signal == null ? null : d.signal + '%',
    d.ip,
    d.mac,
  ].filter(Boolean).join(' · ');
  btn.appendChild(meta);

  btn.addEventListener('click', function () {
    writeSurveyMac(d.mac);
    closeDevicePicker();
    renderSurvey();
    toast('Walk test will profile ' + deviceLabel(d), 'success');
  });
  return btn;
}

function openDevicePicker() {
  const devices = wirelessDevices();
  els.netSurveyDialogList.innerHTML = '';
  if (!devices.length) {
    els.netSurveyDialogList.appendChild(emptyStateEl(
      'smartphone',
      'No wireless devices in the current read — open the Network tab and wait for a poll.'
    ));
  } else {
    devices.forEach(function (d) { els.netSurveyDialogList.appendChild(pickerRow(d)); });
  }
  if (typeof els.netSurveyDialog.showModal === 'function') els.netSurveyDialog.showModal();
  else els.netSurveyDialog.setAttribute('open', '');
}

function closeDevicePicker() {
  if (typeof els.netSurveyDialog.close === 'function') els.netSurveyDialog.close();
  else els.netSurveyDialog.removeAttribute('open');
}

export function wireSurvey() {
  if (!els.netSurveyCard) return;
  els.netSurveyDevicePick.addEventListener('click', openDevicePicker);
  els.netSurveyRecord.addEventListener('click', recordHere);
  els.netSurveyRoom.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); recordHere(); }
  });
  if (els.netSurveyDialog) {
    els.netSurveyDialogClose.addEventListener('click', closeDevicePicker);
    els.netSurveyDialog.addEventListener('click', function (ev) {
      if (ev.target === els.netSurveyDialog) closeDevicePicker();
    });
  }
}
