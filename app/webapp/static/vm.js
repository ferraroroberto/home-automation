/* Home Assistant Hyper-V VM tile (Home tab, last card — issue #240).
 *
 * Reads GET /api/hyperv and controls the VM via POST /api/hyperv/{start|stop}.
 * The backend shells out to Hyper-V on the host PC and addresses the VM by name,
 * so this tile is independent of the VM's LAN IP. Stop is confirm-gated (it
 * drops Home Assistant); Start is not. Polled only while the Home tab is active. */

'use strict';

import { state, els, toast, reportFetchFailure, reportFetchOk } from './state.js';
import { jsonApi } from './api.js';
import { emptyStateEl } from './icons.js';
import { esc } from './format.js';
import { restoreSnapshot, saveSnapshot, snapshotLabel } from './snapshots.js';
import { confirmAction } from './network.js';
import { createPoller } from './poll.js';
import { toggleMarkup } from './toggle.js';

const POLL_MS = 30_000;

let busy = false;       // a start/stop is in flight — disable the toggle, skip overlap
let pending = null;     // 'start' | 'stop' while that action is in flight
let vmViewState = 'idle';
let vmUpdatedAt = null;
let vmLiveUnavailable = false;

function setVmViewState(next, opts) {
  vmViewState = next;
  if (opts && opts.updatedAt) vmUpdatedAt = opts.updatedAt;
  if (opts && Object.prototype.hasOwnProperty.call(opts, 'liveUnavailable')) {
    vmLiveUnavailable = opts.liveUnavailable;
  }
}

function viewStateFor(vm) {
  if (!vm || vm.state === 'not_found') return 'empty';
  return vm.available === true ? 'ready' : 'error';
}

function lastUpdatedLabel() {
  const raw = vmUpdatedAt || state.snapshotUpdatedAt.hyperv;
  const updated = raw instanceof Date ? raw : new Date(raw || '');
  if (Number.isNaN(updated.getTime())) return 'Last updated earlier';
  return 'Last updated ' + updated.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
  });
}

function renderVmState(tile, iconName, message, action) {
  tile.hidden = false;
  tile.title = '';
  tile.classList.remove('is-unavailable', 'is-running');
  tile.innerHTML = '';
  const stateEl = emptyStateEl(iconName, message, action ? {
    actionLabel: action.label,
    onAction: action.onAction,
  } : null);
  tile.appendChild(stateEl);
  const button = stateEl.querySelector('.empty-state-action');
  if (button && action && action.disabled) button.disabled = true;
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
  tile.dataset.state = vmViewState;
  tile.setAttribute('aria-busy', vmViewState === 'loading' ? 'true' : 'false');
  if (vmViewState === 'loading') {
    renderVmState(tile, 'refresh-cw', 'Reading Home Assistant status…', null);
    return;
  }
  if (vmViewState === 'empty') {
    renderVmState(tile, 'cpu', 'Home Assistant VM not found', {
      label: 'Retry',
      onAction: function () { loadVm(); },
    });
    return;
  }
  if (vmViewState === 'error') {
    const canStart = !!(vm && vm.name && vm.state !== 'not_found');
    renderVmState(tile, 'cpu', 'Home Assistant status unavailable', canStart ? {
      label: busy ? 'Starting…' : 'Start Home Assistant',
      onAction: onToggle,
      disabled: busy,
    } : {
      label: 'Retry',
      onAction: function () { loadVm(); },
    });
    return;
  }
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
  const disabled = busy || !actionable || vmViewState === 'stale';
  const toggle =
    '<button type="button" class="toggle vm-toggle' + (toggleOn ? ' on' : '') + '"' +
    ' role="switch" aria-checked="' + (toggleOn ? 'true' : 'false') + '"' +
    (disabled ? ' disabled' : '') + ' aria-label="Home Assistant VM power">' +
    toggleMarkup(toggleOn) + '</button>';

  // IP·MAC are hidden per design, but kept as a hover tooltip so the "is it on
  // the reserved address?" check is still one mouse-over away.
  const meta = [];
  if (vm && vm.ip_address) meta.push(vm.ip_address);
  if (vm && vm.mac_address) meta.push(vm.mac_address);
  tile.title = meta.length ? meta.join(' · ') : '';

  tile.innerHTML =
    '<div class="vm-main">' +
    '  <div class="vm-title"><svg class="icon title-icon" aria-hidden="true"><use href="#i-cpu"></use></svg><span>HA</span></div>' +
    '  <span class="vm-status vm-status-' + badge.mod + '">' + esc(badge.dot) + ' ' + esc(badge.text) + '</span>' +
    '  ' + toggle +
    '</div>';

  if (vmViewState === 'stale') {
    const note = document.createElement('p');
    note.className = 'muted small vm-stale-note';
    note.textContent = vmLiveUnavailable
      ? lastUpdatedLabel() + ' · live data unavailable'
      : snapshotLabel('hyperv');
    tile.appendChild(note);
  }

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
    reportFetchOk('hyperv');
    setVmViewState(viewStateFor(state.vm), {
      updatedAt: new Date(),
      liveUnavailable: false,
    });
    toast(action === 'stop' ? 'Home Assistant is shutting down' : 'Home Assistant is starting', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast(
        action === 'stop'
          ? "Couldn't stop Home Assistant"
          : "Couldn't start Home Assistant",
        'error'
      );
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
  if (!state.vm) {
    setVmViewState('loading', { liveUnavailable: false });
    renderVm();
  }
  try {
    const body = await jsonApi('/api/hyperv');
    reportFetchOk('hyperv');
    saveSnapshot('hyperv', body);
    state.vm = (body && body.hyperv) || null;
    setVmViewState(viewStateFor(state.vm), {
      updatedAt: new Date(),
      liveUnavailable: false,
    });
    renderVm();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    setVmViewState(state.vm && state.vm.available === true ? 'stale' : 'error', {
      liveUnavailable: true,
    });
    reportFetchFailure(
      'hyperv',
      { message: 'live data unavailable' },
      'Home Assistant VM'
    );
    renderVm();
  }
}

export function restoreVmSnapshot() {
  const body = restoreSnapshot('hyperv');
  if (!body) return;
  state.vm = (body && body.hyperv) || null;
  setVmViewState(state.vm && state.vm.available === true ? 'stale' : viewStateFor(state.vm), {
    updatedAt: state.snapshotUpdatedAt.hyperv,
    liveUnavailable: false,
  });
  renderVm();
}

const schedule = createPoller(loadVm);

export function onVmTab(tab) {
  if (tab === 'home') {
    loadVm();
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}
