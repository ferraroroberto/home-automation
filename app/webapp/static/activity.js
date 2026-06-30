/* Home Automation — Activity log overlay (#289).
 *
 * A read-only admin/telemetry panel (a <dialog>, not a tab) opened from the
 * Home "Activity log" button. Lists recent events from GET /api/activity, with
 * a domain dropdown + a free-text type filter — both applied server-side, so
 * the browser never holds or filters the whole store.
 */

'use strict';

import { jsonApi } from './api.js';
import { toast } from './state.js';

const PAGE_LIMIT = 100;
let mode = 'events'; // 'events' | 'readings'

function el(id) {
  return document.getElementById(id);
}

// Compact numeric format for a reading value (keeps small decimals, drops noise).
function fmtNum(n) {
  if (n === null || n === undefined) return '—';
  const abs = Math.abs(n);
  if (abs !== 0 && abs < 1) return n.toFixed(3);
  return Math.round(n * 100) / 100;
}

// Human label for an epoch-second timestamp: today → time only, else date+time.
function fmtTime(ts) {
  if (!ts) return '—';
  const d = new Date(ts * 1000);
  const now = new Date();
  const sameDay =
    d.getFullYear() === now.getFullYear() &&
    d.getMonth() === now.getMonth() &&
    d.getDate() === now.getDate();
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return sameDay ? time : d.toLocaleDateString([], { month: 'short', day: '2-digit' }) + ' ' + time;
}

// A short, human one-liner for the event's payload (best-effort, never throws).
function detailText(ev) {
  const p = ev.payload || {};
  const bits = [];
  if (p.detail) bits.push(String(p.detail));
  else if (p.text) bits.push(String(p.text));
  else if (p.reason) bits.push(String(p.reason));
  if (ev.source && !bits.length) bits.push(ev.source);
  if (ev.outcome === 'error' && p.error) bits.push(String(p.error));
  return bits.join(' · ');
}

function renderRows(events) {
  const list = el('activityList');
  const note = el('activityNote');
  list.innerHTML = '';
  if (!events.length) {
    note.hidden = false;
    note.textContent = 'No events match — they accrue as the home acts (arming, plug toggles, power, presence).';
    return;
  }
  note.hidden = true;
  for (const ev of events) {
    const li = document.createElement('li');
    li.className = 'activity-row activity-sev-' + (ev.severity || 'info');
    const detail = detailText(ev);
    li.innerHTML =
      '<span class="activity-time">' + fmtTime(ev.ts) + '</span>' +
      '<span class="activity-domain">' + (ev.domain || '—') + '</span>' +
      '<span class="activity-type">' + (ev.event_type || '—') + '</span>' +
      '<span class="activity-detail muted small"></span>';
    // Text-only assignment for the untrusted detail string (no HTML injection).
    li.querySelector('.activity-detail').textContent = detail;
    list.appendChild(li);
  }
}

function renderReadings(readings) {
  const list = el('activityList');
  const note = el('activityNote');
  list.innerHTML = '';
  if (!readings.length) {
    note.hidden = false;
    note.textContent = 'No readings yet — the sampler records device telemetry (HVAC temps, plug watts, UPS load) every few minutes while the app runs.';
    return;
  }
  note.hidden = true;
  for (const r of readings) {
    const li = document.createElement('li');
    const unreachable = r.quality && r.quality !== 'ok';
    li.className = 'activity-row' + (unreachable ? ' activity-sev-warning' : '');
    const value = r.value_txt != null ? r.value_txt : fmtNum(r.value_num);
    const unit = r.unit && r.value_num != null ? ' ' + r.unit : '';
    li.innerHTML =
      '<span class="activity-time">' + fmtTime(r.ts) + '</span>' +
      '<span class="activity-domain">' + (r.domain || '—') + '</span>' +
      '<span class="activity-type">' + (r.metric || '—') + '</span>' +
      '<span class="activity-detail muted small"></span>';
    li.querySelector('.activity-detail').textContent =
      (r.entity_id ? r.entity_id + ' · ' : '') + value + unit;
    list.appendChild(li);
  }
}

async function loadEvents() {
  const domain = el('activityDomain').value;
  const type = (el('activityType').value || '').trim();
  const params = new URLSearchParams();
  if (domain) params.set('domain', domain);
  if (type) params.set('type', type);
  params.set('limit', String(PAGE_LIMIT));
  try {
    const data = await jsonApi('/api/activity?' + params.toString());
    renderRows((data && data.events) || []);
  } catch (exc) {
    toast((exc && exc.message) || 'Failed to load activity');
  }
}

async function loadReadings() {
  const domain = el('activityDomain').value;
  const metric = (el('activityMetric').value || '').trim();
  const params = new URLSearchParams();
  if (domain) params.set('domain', domain);
  if (metric) params.set('metric', metric);
  params.set('limit', String(PAGE_LIMIT));
  try {
    const data = await jsonApi('/api/activity/readings?' + params.toString());
    renderReadings((data && data.readings) || []);
  } catch (exc) {
    toast((exc && exc.message) || 'Failed to load readings');
  }
}

// Reload whichever view is active.
function reload() {
  return mode === 'readings' ? loadReadings() : loadEvents();
}

// Domains shown in the dropdown depend on the view: events come from the store,
// readings come from the fixed sampler domain set (no extra round-trip).
const READING_DOMAINS = ['hvac', 'plug', 'ups', 'light', 'presence'];

function fillDomains(domains) {
  const select = el('activityDomain');
  const current = select.value;
  select.innerHTML = '<option value="">All</option>';
  for (const d of domains) {
    const opt = document.createElement('option');
    opt.value = d;
    opt.textContent = d;
    select.appendChild(opt);
  }
  // Preserve the selection only if still valid for this view.
  select.value = domains.indexOf(current) >= 0 ? current : '';
}

async function refreshDomains() {
  if (mode === 'readings') {
    fillDomains(READING_DOMAINS);
    return;
  }
  try {
    const data = await jsonApi('/api/activity/domains');
    fillDomains((data && data.domains) || []);
  } catch (exc) {
    // Non-fatal: the dropdown just stays "All".
  }
}

function applyMode(next) {
  mode = next === 'readings' ? 'readings' : 'events';
  const isReadings = mode === 'readings';
  el('activityModeEvents').classList.toggle('is-active', !isReadings);
  el('activityModeEvents').setAttribute('aria-selected', String(!isReadings));
  el('activityModeReadings').classList.toggle('is-active', isReadings);
  el('activityModeReadings').setAttribute('aria-selected', String(isReadings));
  el('activityTypeRow').hidden = isReadings;
  el('activityMetricRow').hidden = !isReadings;
  refreshDomains();
  reload();
}

function openActivity() {
  const dlg = el('activityDialog');
  if (!dlg) return;
  if (typeof dlg.showModal === 'function') dlg.showModal();
  else dlg.setAttribute('open', '');
  refreshDomains();
  reload();
}

function closeActivity() {
  const dlg = el('activityDialog');
  if (!dlg) return;
  if (typeof dlg.close === 'function') dlg.close();
  else dlg.removeAttribute('open');
}

// Debounce a free-text input so each keystroke doesn't fire a request.
function wireDebounced(input, fn) {
  if (!input) return;
  let t = null;
  input.addEventListener('input', function () {
    if (t) clearTimeout(t);
    t = setTimeout(fn, 250);
  });
}

// Wire the Home button + the overlay's controls. Called once at boot.
export function wireActivity() {
  const open = el('activityOpen');
  if (open) open.addEventListener('click', openActivity);
  const close = el('activityClose');
  if (close) close.addEventListener('click', closeActivity);
  const modeEvents = el('activityModeEvents');
  if (modeEvents) modeEvents.addEventListener('click', function () { applyMode('events'); });
  const modeReadings = el('activityModeReadings');
  if (modeReadings) modeReadings.addEventListener('click', function () { applyMode('readings'); });
  const domain = el('activityDomain');
  if (domain) domain.addEventListener('change', reload);
  wireDebounced(el('activityType'), reload);
  wireDebounced(el('activityMetric'), reload);
}
