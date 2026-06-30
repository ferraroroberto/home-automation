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

function el(id) {
  return document.getElementById(id);
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

async function loadDomains() {
  const select = el('activityDomain');
  try {
    const data = await jsonApi('/api/activity/domains');
    const domains = (data && data.domains) || [];
    const current = select.value;
    select.innerHTML = '<option value="">All</option>';
    for (const d of domains) {
      const opt = document.createElement('option');
      opt.value = d;
      opt.textContent = d;
      select.appendChild(opt);
    }
    select.value = current; // preserve selection across reopen
  } catch (exc) {
    // Non-fatal: the dropdown just stays "All".
  }
}

function openActivity() {
  const dlg = el('activityDialog');
  if (!dlg) return;
  if (typeof dlg.showModal === 'function') dlg.showModal();
  else dlg.setAttribute('open', '');
  loadDomains();
  loadEvents();
}

function closeActivity() {
  const dlg = el('activityDialog');
  if (!dlg) return;
  if (typeof dlg.close === 'function') dlg.close();
  else dlg.removeAttribute('open');
}

// Wire the Home button + the overlay's controls. Called once at boot.
export function wireActivity() {
  const open = el('activityOpen');
  if (open) open.addEventListener('click', openActivity);
  const close = el('activityClose');
  if (close) close.addEventListener('click', closeActivity);
  const domain = el('activityDomain');
  if (domain) domain.addEventListener('change', loadEvents);
  const type = el('activityType');
  if (type) {
    let t = null;
    type.addEventListener('input', function () {
      if (t) clearTimeout(t);
      t = setTimeout(loadEvents, 250); // debounce free-text typing
    });
  }
}
