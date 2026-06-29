/* Home Assistant Hyper-V VM tile (Home tab, last card — issue #240).
 *
 * Reads GET /api/hyperv and controls the VM via POST /api/hyperv/{start|stop}.
 * The backend shells out to Hyper-V on the host PC and addresses the VM by name,
 * so this tile is independent of the VM's LAN IP. Stop is confirm-gated (it
 * drops Home Assistant); Start is not. Polled only while the Home tab is active. */

'use strict';

import { state, els, toast, reportFetchFailure, reportFetchOk } from './state.js';
import { jsonApi } from './api.js';
import { isSnapshotRestored, restoreSnapshot, saveSnapshot, snapshotLabel } from './snapshots.js';
import { confirmAction } from './network.js';

const POLL_MS = 30_000;

let vmTimer = null;
let busy = false;       // a start/stop is in flight — disable the toggle, skip overlap
let pending = null;     // 'start' | 'stop' while that action is in flight

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function fmtUptime(seconds) {
  if (seconds == null) return '';
  const total = Math.max(0, Math.round(Number(seconds)));
  const mins = Math.floor(total / 60);
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + ' min';
  const h = Math.floor(mins / 60);
  const d = Math.floor(h / 24);
  if (d >= 1) return d + 'd ' + (h % 24) + 'h';
  return h + 'h ' + (mins % 60) + 'm';
}

// Status badge: dot + text + a CSS modifier driving the colour.
function statusBadge(vm) {
  if (pending) {
    return { mod: 'transition', dot: '◐', text: pending === 'start' ? 'starting…' : 'stopping…' };
  }
  if (!vm || vm.available !== true) {
    const text = vm && vm.state === 'not_found' ? 'not found' : 'unavailable';
    return { mod: 'unavailable', dot: '⚠', text: text };
  }
  if (vm.state === 'running') {
    const up = fmtUptime(vm.uptime_seconds);
    return { mod: 'running', dot: '●', text: up ? 'online · up ' + up : 'online' };
  }
  if (vm.state === 'off') return { mod: 'off', dot: '○', text: 'off' };
  // Transient Hyper-V states (saved/paused/starting/stopping).
  return { mod: 'transition', dot: '◐', text: vm.state };
}

function render(tile, vm) {
  if (!tile) return;
  tile.hidden = false;
  const available = !!(vm && vm.available === true);
  const running = !!(vm && vm.state === 'running');
  tile.classList.toggle('is-unavailable', !available);
  tile.classList.toggle('is-running', running);

  const badge = statusBadge(vm);

  // The same on/off switch as AC power / plugs: on = running. Toggling it
  // starts (off→on) or stops (on→off, confirm-gated). Disabled while a change
  // is in flight, or when the VM isn't in a settled, actionable state (e.g.
  // insufficient rights → can't control). While pending it slides to the target.
  const actionable = available && (vm.state === 'running' || vm.state === 'off');
  const toggleOn = pending ? (pending === 'start') : running;
  const disabled = busy || !actionable;
  const toggle =
    '<button type="button" class="toggle vm-toggle' + (toggleOn ? ' on' : '') + '"' +
    ' role="switch" aria-checked="' + (toggleOn ? 'true' : 'false') + '"' +
    (disabled ? ' disabled' : '') + ' aria-label="Home Assistant VM power">' +
    '<span class="knob"></span><span class="toggle-label">' + (toggleOn ? 'ON' : 'OFF') + '</span></button>';

  // IP·MAC are hidden per design, but kept as a hover tooltip so the "is it on
  // the reserved address?" check is still one mouse-over away.
  const meta = [];
  if (vm && vm.ip_address) meta.push(vm.ip_address);
  if (vm && vm.mac_address) meta.push(vm.mac_address);
  tile.title = meta.length ? meta.join(' · ') : '';

  // A sub-line only when the read failed (e.g. insufficient rights) — the normal
  // online/off tile is a single row, equal height to the weather tile.
  let sub = '';
  if (!available && vm && vm.error) {
    sub = '<div class="vm-meta muted small">' + esc(vm.error) + '</div>';
  }

  tile.innerHTML =
    '<div class="vm-main">' +
    '  <div class="vm-title"><svg class="icon title-icon" aria-hidden="true"><use href="#i-cpu"></use></svg><span>HA</span></div>' +
    '  <span class="vm-status vm-status-' + badge.mod + '">' + esc(badge.dot) + ' ' + esc(badge.text) + '</span>' +
    '  ' + toggle +
    '</div>' +
    sub;

  const btn = tile.querySelector('.vm-toggle');
  if (btn && !disabled) btn.addEventListener('click', onToggle);
}

async function onToggle() {
  if (busy) return;
  const running = !!(state.vm && state.vm.state === 'running');
  const action = running ? 'stop' : 'start';
  if (action === 'stop') {
    const ok = await confirmAction({
      title: 'Shut down Home Assistant?',
      message: 'The VM shuts down gracefully. Voice control and the HA dashboard go offline until you start it again.',
      okLabel: 'Shut down',
      danger: true,
    });
    if (!ok) return;
  }
  busy = true;
  pending = action;
  renderVm();
  toast(action === 'stop' ? 'Shutting down Home Assistant…' : 'Starting Home Assistant…', 'pending');
  try {
    const body = await jsonApi('/api/hyperv/' + action, { method: 'POST' });
    state.vm = (body && body.hyperv) || state.vm;
    saveSnapshot('hyperv', body);
    toast(action === 'stop' ? 'Home Assistant is shutting down' : 'Home Assistant is starting', 'good');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed: ' + (exc.message || exc), 'error');
    }
  } finally {
    busy = false;
    pending = null;
    renderVm();
    // A transition was kicked off; re-poll shortly to catch the settled state.
    setTimeout(loadVm, 4_000);
  }
}

export function renderVm() {
  render(els.homeVmTile, state.vm);
}

export async function loadVm() {
  try {
    const body = await jsonApi('/api/hyperv');
    reportFetchOk('hyperv');
    saveSnapshot('hyperv', body);
    state.vm = (body && body.hyperv) || null;
    renderVm();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    reportFetchFailure('hyperv', exc, 'Home Assistant VM');
    renderVm();
  }
}

export function restoreVmSnapshot() {
  const body = restoreSnapshot('hyperv');
  if (!body) return;
  state.vm = (body && body.hyperv) || null;
  renderVm();
}

function schedule(ms) {
  if (vmTimer) clearInterval(vmTimer);
  vmTimer = ms > 0 ? setInterval(loadVm, ms) : null;
}

export function onVmTab(tab) {
  if (tab === 'home') {
    loadVm();
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}
