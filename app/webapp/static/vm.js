/* Home Assistant Hyper-V VM control (Home tab — issue #240; #461 folded the
 * old body tile into the HA card's summary).
 *
 * Reads GET /api/hyperv and controls the VM via POST /api/hyperv/{start|stop}.
 * The backend shells out to Hyper-V on the host PC and addresses the VM by
 * name, so this is independent of the VM's LAN IP. Stop is confirm-gated (it
 * drops Home Assistant); Start is not. Polled only while the Home tab is
 * active.
 *
 * The whole surface is the HA card's summary row: the status text
 * (#homeAssistantSummaryState) and the power switch (#homeVmToggle) — there is
 * no body tile any more. The card element carries data-vm-state as the
 * machine-readable state hook (tests); IP·MAC and the stale-snapshot detail
 * live in the status text's hover tooltip.
 */

'use strict';

import { state, els, toast, reportFetchFailure, reportFetchOk } from './state.js';
import { jsonApi } from './api.js';
import { restoreSnapshot, saveSnapshot, snapshotLabel } from './snapshots.js';
import { confirmAction } from './network.js';
import { createPoller } from './poll.js';
import { createViewState } from './view-state.js';
import { setToggleState } from './toggle.js';

const POLL_MS = 30_000;

let busy = false;       // a start/stop is in flight — disable the toggle, skip overlap
let pending = null;     // 'start' | 'stop' while that action is in flight
const vmView = createViewState('hyperv');

function viewStateFor(vm) {
  if (!vm || vm.state === 'not_found') return 'empty';
  return vm.available === true ? 'ready' : 'error';
}

function renderSummaryState(text, modifier, title) {
  if (!els.homeAssistantSummaryState) return;
  els.homeAssistantSummaryState.textContent = text;
  els.homeAssistantSummaryState.title = title || '';
  els.homeAssistantSummaryState.className =
    'muted small ha-summary-state ha-summary-' + modifier;
}

function renderToggle(on, disabled) {
  if (!els.homeVmToggle) return;
  setToggleState(els.homeVmToggle, on);
  els.homeVmToggle.disabled = !!disabled;
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

// Status text + the CSS modifier driving its colour.
function statusBadge(vm) {
  if (pending) {
    return { mod: 'transition', text: pending === 'start' ? 'starting…' : 'stopping…' };
  }
  if (!vm || vm.available !== true) {
    return { mod: 'unavailable', text: vm && vm.state === 'not_found' ? 'not found' : 'unavailable' };
  }
  if (vm.state === 'running') {
    const up = fmtUptime(vm.uptime_seconds);
    return { mod: 'running', text: up ? 'online · up ' + up : 'online' };
  }
  if (vm.state === 'off') return { mod: 'off', text: 'off' };
  // Transient Hyper-V states (saved/paused/starting/stopping).
  return { mod: 'transition', text: vm.state };
}

function render(vm) {
  if (els.homeAssistantCard) els.homeAssistantCard.dataset.vmState = vmView.state;
  if (vmView.state === 'loading') {
    renderSummaryState('Reading status…', 'transition');
    renderToggle(false, true);
    return;
  }
  if (vmView.state === 'empty') {
    renderSummaryState('VM not found', 'unavailable');
    renderToggle(false, true);
    return;
  }
  if (vmView.state === 'error') {
    renderSummaryState('status unavailable', 'unavailable');
    // An unreachable-but-identified VM can still be started — keep the switch
    // usable for that (the old tile's "Start Home Assistant" action).
    const canStart = !!(vm && vm.name && vm.state !== 'not_found');
    renderToggle(pending === 'start', busy || !canStart);
    return;
  }

  const available = !!(vm && vm.available === true);
  const running = !!(vm && vm.state === 'running');
  const badge = statusBadge(vm);
  const stale = vmView.state === 'stale';

  // IP·MAC are hidden per design but kept one hover away; a stale snapshot
  // says so in the text and carries its age in the same tooltip.
  const tip = [];
  if (vm && vm.ip_address) tip.push(vm.ip_address);
  if (vm && vm.mac_address) tip.push(vm.mac_address);
  if (stale) {
    tip.push(vmView.liveUnavailable
      ? vmView.lastUpdatedLabel() + ' · live data unavailable'
      : snapshotLabel('hyperv'));
  }
  renderSummaryState(badge.text + (stale ? ' · cached' : ''), badge.mod, tip.join(' · '));

  // Same on/off switch as AC power / plugs: on = running. Toggling starts
  // (off→on) or stops (on→off, confirm-gated). Disabled while a change is in
  // flight, or when the VM isn't in a settled, actionable state. While
  // pending it slides to the target.
  const actionable = available && (vm.state === 'running' || vm.state === 'off');
  renderToggle(pending ? pending === 'start' : running, busy || !actionable || stale);
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
    vmView.set(viewStateFor(state.vm), {
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

export function wireVm() {
  if (!els.homeVmToggle) return;
  els.homeVmToggle.addEventListener('click', function (ev) {
    // The switch lives inside the card's <summary> — never fold the card.
    ev.preventDefault();
    ev.stopPropagation();
    onToggle();
  });
}

export function renderVm() {
  render(state.vm);
}

export async function loadVm() {
  if (!state.vm) {
    vmView.set('loading', { liveUnavailable: false });
    renderVm();
  }
  try {
    const body = await jsonApi('/api/hyperv');
    reportFetchOk('hyperv');
    saveSnapshot('hyperv', body);
    state.vm = (body && body.hyperv) || null;
    vmView.set(viewStateFor(state.vm), {
      updatedAt: new Date(),
      liveUnavailable: false,
    });
    renderVm();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    vmView.set(state.vm && state.vm.available === true ? 'stale' : 'error', {
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
  vmView.set(state.vm && state.vm.available === true ? 'stale' : viewStateFor(state.vm), {
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
