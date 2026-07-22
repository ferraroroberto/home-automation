/* PC-fleet UPS-triggered shutdown card (IoT tab, issue #498).
 *
 * A folded-by-default card, peer of the UPS Notifications card. Owns the
 * fleet's desired-state shutdown prefs (GET/PUT /api/pc-fleet/prefs) plus a
 * live machine roster read from the hub (GET /api/pc-fleet/machines, polled
 * every ~15s while the tab is open). Each backend PUT sends the whole prefs
 * object — master enable, the runtime-remaining threshold, and the `excluded`
 * id list (a machine's include-toggle OFF = its id in `excluded`). The hub
 * host row always participates (shut down last over its local path) and has no
 * include toggle. A per-machine Wake button appears only for a down/dormant
 * machine whose `actions.wake` is true, and posts /api/pc-fleet/wake/{id}.
 */

'use strict';

import { els, toast } from './state.js';
import { jsonApi } from './api.js';
import { setToggleState, isToggleOn, wireToggle, buildToggle } from './toggle.js';
import { esc } from './format.js';
import { createPoller } from './poll.js';

const POLL_MS = 15_000;
const THRESHOLD_MIN = 1;
const THRESHOLD_MAX = 240;

// Module state: the last-known prefs and machine roster. `excluded` is kept
// here (not recomputed from the DOM) so a master-toggle/threshold save while
// the hub is unreachable — no machine rows rendered — never wipes it.
let prefs = { enabled: false, threshold_minutes: 15, excluded: [] };
let machines = [];
let hubUnreachable = false;

const STATE_LABELS = {
  self: 'this host',
  up: 'up',
  down: 'down',
  dormant: 'dormant',
};

function clampThreshold(value) {
  const n = Math.round(Number(value));
  if (!Number.isFinite(n)) return prefs.threshold_minutes || 15;
  return Math.min(THRESHOLD_MAX, Math.max(THRESHOLD_MIN, n));
}

function renderPrefs() {
  if (els.pcFleetEnabled) setToggleState(els.pcFleetEnabled, prefs.enabled === true);
  if (els.pcFleetThreshold) els.pcFleetThreshold.value = prefs.threshold_minutes;
  if (els.pcFleetCaption) {
    els.pcFleetCaption.textContent = 'Trigger when UPS runtime-remaining ≤ ' +
      prefs.threshold_minutes + ' min.';
  }
}

function renderNote() {
  if (!els.pcFleetNote) return;
  if (hubUnreachable) {
    els.pcFleetNote.hidden = false;
    els.pcFleetNote.textContent =
      'Machine list unavailable — the fleet hub is not reachable. Shutdown settings can still be edited.';
  } else {
    els.pcFleetNote.hidden = true;
    els.pcFleetNote.textContent = '';
  }
}

function machineRow(machine) {
  const row = document.createElement('div');
  row.className = 'pc-fleet-machine';
  row.dataset.machineId = machine.id;

  const name = document.createElement('span');
  name.className = 'pc-fleet-machine-name';
  name.textContent = machine.display_name || machine.id;
  row.appendChild(name);

  const rail = document.createElement('div');
  rail.className = 'pc-fleet-machine-rail';

  const chip = document.createElement('span');
  const rawState = String(machine.state || '');
  chip.className = 'pc-fleet-chip pc-fleet-chip--' + esc(rawState || 'unknown');
  chip.textContent = STATE_LABELS[rawState] || (rawState || 'unknown');
  rail.appendChild(chip);

  const actions = machine.actions || {};
  if ((rawState === 'down' || rawState === 'dormant') && actions.wake === true) {
    const wake = document.createElement('button');
    wake.type = 'button';
    wake.className = 'range-tab pc-fleet-wake';
    wake.dataset.machineId = machine.id;
    wake.textContent = 'Wake';
    rail.appendChild(wake);
  }

  if (machine.is_host === true) {
    const note = document.createElement('span');
    note.className = 'muted small pc-fleet-host-note';
    note.textContent = 'always last · local path';
    rail.appendChild(note);
  } else {
    const included = !(prefs.excluded || []).includes(machine.id);
    const toggle = buildToggle('pc-fleet-include', included, function (next) {
      const set = new Set(prefs.excluded || []);
      if (next) set.delete(machine.id);
      else set.add(machine.id);
      prefs.excluded = Array.from(set);
      savePrefs();
    });
    toggle.setAttribute('aria-label', 'Include ' + (machine.display_name || machine.id) + ' in fleet shutdown');
    rail.appendChild(toggle);
  }

  row.appendChild(rail);
  return row;
}

function renderMachines() {
  if (!els.pcFleetMachines) return;
  els.pcFleetMachines.innerHTML = '';
  machines.forEach(function (machine) {
    els.pcFleetMachines.appendChild(machineRow(machine));
  });
  renderNote();
}

function applyPrefs(payload) {
  if (!payload) return;
  prefs = {
    enabled: payload.enabled === true,
    threshold_minutes: clampThreshold(payload.threshold_minutes),
    excluded: Array.isArray(payload.excluded) ? payload.excluded.slice() : [],
  };
  renderPrefs();
  renderMachines();
}

async function savePrefs() {
  const payload = {
    enabled: els.pcFleetEnabled ? isToggleOn(els.pcFleetEnabled) : prefs.enabled,
    threshold_minutes: els.pcFleetThreshold
      ? clampThreshold(els.pcFleetThreshold.value)
      : prefs.threshold_minutes,
    excluded: prefs.excluded || [],
  };
  try {
    applyPrefs(await jsonApi('/api/pc-fleet/prefs', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    }));
    toast('Fleet shutdown saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Fleet shutdown save failed: ' + (exc.message || exc), 'error');
    }
  }
}

async function loadMachines() {
  if (!els.pcFleetMachines) return;
  try {
    const body = await jsonApi('/api/pc-fleet/machines');
    machines = (body && Array.isArray(body.machines)) ? body.machines : [];
    hubUnreachable = false;
    renderMachines();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    machines = [];
    hubUnreachable = true;
    renderMachines();
  }
}

export async function loadPcFleet() {
  if (!els.pcFleetEnabled) return;
  try {
    applyPrefs(await jsonApi('/api/pc-fleet/prefs'));
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Fleet shutdown settings failed: ' + (exc.message || exc), 'error');
    }
  }
  await loadMachines();
}

async function wakeMachine(id) {
  toast('Waking machine…', 'pending');
  try {
    await jsonApi('/api/pc-fleet/wake/' + encodeURIComponent(id), { method: 'POST' });
    toast('Wake signal sent', 'success');
    loadMachines();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Wake failed: ' + (exc.message || exc), 'error');
    }
  }
}

export function wirePcFleet() {
  wireToggle(els.pcFleetEnabled, savePrefs);
  if (els.pcFleetThreshold) {
    els.pcFleetThreshold.addEventListener('change', savePrefs);
  }
  // Wake buttons are re-rendered on every machine refresh — delegate off the
  // stable container so a single listener survives re-renders. (Include
  // toggles carry their own handler via buildToggle.)
  if (els.pcFleetMachines) {
    els.pcFleetMachines.addEventListener('click', function (ev) {
      const btn = ev.target.closest('.pc-fleet-wake');
      if (!btn) return;
      wakeMachine(btn.dataset.machineId);
    });
  }
}

const schedule = createPoller(loadMachines);

export function onPcFleetTab(tab) {
  if (tab === 'iot') {
    loadPcFleet();
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}
