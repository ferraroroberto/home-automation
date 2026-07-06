/* Network tab — DHCP reservation planner + apply flow (#170/#176).
 *
 * Split out of network.js (issue #197). Lazy-loads GET /api/network/dhcp-plan on
 * first open, then on demand via Refresh. The F6600P caps the static-binding table
 * at 10 rows, so this is a small *staged* reservation manager, not a one-shot
 * apply: mark router rows to remove, tick suggested/manual rows to add, then one
 * "Apply changes" runs the removes then the adds in a single router session (POST
 * /api/network/dhcp-reservations/apply). Every write is confirm-gated (via the boot
 * module's confirmAction) and never runs on a poll.
 */

'use strict';

import { els, toast } from './state.js';
import { jsonApi } from './api.js';
import { confirmAction } from './network.js';
import { buildToggle } from './toggle.js';

let dhcpPlanLoading = false;
let dhcpPlanLoaded = false;
let dhcpApplying = false;
let lastDhcpPlan = null;

// Staged changes, reconciled against each fresh plan load (the server is the truth).
const stagedRemove = new Set();   // inst_ids to delete
const stagedAdd = new Set();      // normalised MACs (suggested create/change) to write
let manualAdds = [];              // [{mac, ip, name}] manual rows staged for add

function normMac(m) { return (m || '').trim().toUpperCase(); }
function isPendingRow(a) { return a.status === 'create' || a.status === 'change'; }

// create/change rows across all categories — the "suggestions" you can add.
function planPendingAssignments(plan) {
  const out = [];
  ((plan && plan.categories) || []).forEach(function (c) {
    c.assignments.forEach(function (a) { if (isPendingRow(a)) out.push(a); });
  });
  return out;
}

// A create consumes a slot; a change re-writes one the device already owns. Manual
// rows are counted as new (worst case) for the live budget estimate.
function stagedCreateCount() {
  let creates = 0;
  planPendingAssignments(lastDhcpPlan).forEach(function (a) {
    if (a.status === 'create' && stagedAdd.has(normMac(a.mac))) creates += 1;
  });
  return creates + manualAdds.length;
}

function stagedCount() { return stagedRemove.size + stagedAdd.size + manualAdds.length; }

// ---- On the router now (each row's trash toggle stages a removal) ----
function existingRow(e) {
  const staged = !!e.inst_id && stagedRemove.has(e.inst_id);
  const row = document.createElement('div');
  row.className = 'net-dhcp-existing-row' + (e.online ? '' : ' is-offline')
    + (staged ? ' is-removing' : '');

  const name = document.createElement('span');
  name.className = 'net-dhcp-ex-name';
  name.textContent = e.display_name || e.name || '(unnamed)';
  if (!e.online) {
    const badge = document.createElement('span');
    badge.className = 'net-dhcp-ex-offline';
    badge.textContent = 'offline';
    name.appendChild(document.createTextNode(' '));
    name.appendChild(badge);
  }

  const mac = document.createElement('span');
  mac.className = 'net-dhcp-ex-mac mono';
  mac.textContent = e.mac || '—';

  const ip = document.createElement('span');
  ip.className = 'net-dhcp-ex-ip';
  ip.textContent = e.ip || '—';

  const del = document.createElement('button');
  del.type = 'button';
  del.className = 'net-dhcp-ex-del' + (staged ? ' is-active' : '');
  del.title = staged ? 'Keep this reservation' : 'Delete this reservation (frees a slot)';
  del.setAttribute('aria-label', staged ? 'Keep this reservation' : 'Delete this reservation');
  del.disabled = !e.inst_id;
  del.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-' +
    (staged ? 'rotate-cw' : 'trash-2') + '"></use></svg>';
  del.addEventListener('click', function () {
    if (!e.inst_id) return;
    if (stagedRemove.has(e.inst_id)) stagedRemove.delete(e.inst_id);
    else stagedRemove.add(e.inst_id);
    renderDhcpPlan(lastDhcpPlan);
  });

  row.appendChild(name);
  row.appendChild(mac);
  row.appendChild(ip);
  row.appendChild(del);
  return row;
}

function renderDhcpExisting(plan) {
  if (!els.netDhcpExistingWrap) return;
  const known = !!(plan && plan.bindings_known);
  const existing = (plan && plan.existing) || [];
  els.netDhcpExisting.innerHTML = '';
  if (!known || !existing.length) {
    els.netDhcpExistingWrap.hidden = true;
    return;
  }
  els.netDhcpExistingWrap.hidden = false;
  const cap = typeof plan.capacity === 'number' ? plan.capacity : '?';
  if (els.netDhcpExistingHead) {
    els.netDhcpExistingHead.textContent = 'On the router now · ' + existing.length + '/' + cap;
  }
  existing.forEach(function (e) { els.netDhcpExisting.appendChild(existingRow(e)); });
}

// ---- Suggested adds + unassigned (assignable) + randomised (un-reservable) ----
function suggestedRow(a) {
  const key = normMac(a.mac);
  const row = document.createElement('div');
  row.className = 'net-dhcp-row';

  const cb = buildToggle('net-dhcp-check', stagedAdd.has(key), function (on) {
    if (on) stagedAdd.add(key); else stagedAdd.delete(key);
    renderDhcpPlan(lastDhcpPlan);
  });
  cb.title = 'Add this reservation';
  cb.setAttribute('aria-label', 'Add this reservation');
  row.appendChild(cb);

  const name = document.createElement('span');
  name.className = 'net-dhcp-name';
  name.textContent = a.label || '(unnamed)';

  const mac = document.createElement('span');
  mac.className = 'net-dhcp-mac mono';
  mac.textContent = a.mac || '??';

  const move = document.createElement('span');
  move.className = 'net-dhcp-move';
  const current = a.current_ip || '—';
  if (a.planned_ip === a.current_ip) {
    move.textContent = a.planned_ip;
    move.classList.add('net-dhcp-stable');
  } else {
    move.textContent = current + ' → ' + a.planned_ip;
    move.classList.add('net-dhcp-change');
  }

  row.appendChild(name);
  row.appendChild(mac);
  row.appendChild(move);

  const tag = a.status === 'change'
    ? ['Change', 'net-dhcp-pill-change']
    : ['New', 'net-dhcp-pill-create'];
  const pill = document.createElement('span');
  pill.className = 'net-dhcp-pill ' + tag[1];
  pill.textContent = tag[0];
  row.appendChild(pill);
  return row;
}

function unassignedRow(a) {
  const row = document.createElement('div');
  row.className = 'net-dhcp-row';

  const spacer = document.createElement('span');
  spacer.className = 'net-dhcp-check net-dhcp-check-spacer';
  row.appendChild(spacer);

  const name = document.createElement('span');
  name.className = 'net-dhcp-name';
  name.textContent = a.label || '(unnamed)';

  const mac = document.createElement('span');
  mac.className = 'net-dhcp-mac mono';
  mac.textContent = a.mac || '??';

  const move = document.createElement('span');
  move.className = 'net-dhcp-move net-dhcp-unplaced';
  move.textContent = (a.current_ip || '—') + ' → —';

  row.appendChild(name);
  row.appendChild(mac);
  row.appendChild(move);
  row.appendChild(dhcpGroupSelect(a));
  return row;
}

function randomisedRow(a) {
  const row = document.createElement('div');
  row.className = 'net-dhcp-row net-dhcp-row-random';

  const spacer = document.createElement('span');
  spacer.className = 'net-dhcp-check net-dhcp-check-spacer';
  row.appendChild(spacer);

  const name = document.createElement('span');
  name.className = 'net-dhcp-name';
  name.textContent = a.label || '(unnamed)';

  const mac = document.createElement('span');
  mac.className = 'net-dhcp-mac mono';
  mac.textContent = a.mac || '??';

  const note = document.createElement('span');
  note.className = 'net-dhcp-move net-dhcp-random-note';
  note.textContent = "can't reserve";

  row.appendChild(name);
  row.appendChild(mac);
  row.appendChild(note);
  return row;
}

function dhcpGroupSelect(a) {
  const sel = document.createElement('select');
  sel.className = 'net-dhcp-group';
  sel.title = 'Assign a category — the planner then gives it an IP in that range';
  const labels = (lastDhcpPlan && lastDhcpPlan.category_labels) || [];
  const blank = document.createElement('option');
  blank.value = '';
  blank.textContent = '— group —';
  sel.appendChild(blank);
  labels.forEach(function (label) {
    const opt = document.createElement('option');
    opt.value = label;
    opt.textContent = label;
    if (a.category === label) opt.selected = true;
    sel.appendChild(opt);
  });
  sel.addEventListener('change', function () { setDhcpOverride(a.mac, sel.value); });
  return sel;
}

function dhcpHead(text) {
  const head = document.createElement('h4');
  head.className = 'net-group-head';
  head.textContent = text;
  els.netDhcpPlan.appendChild(head);
}

function renderDhcpSuggestions(plan) {
  els.netDhcpPlan.innerHTML = '';
  const pending = planPendingAssignments(plan);
  if (pending.length) {
    const head = document.createElement('h4');
    head.className = 'net-group-head net-dhcp-suggest-head';
    head.appendChild(document.createTextNode('Suggested to add · ' + pending.length + ' '));
    const allOn = pending.every(function (a) { return stagedAdd.has(normMac(a.mac)); });
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'net-dhcp-selectall';
    toggle.textContent = allOn ? 'none' : 'all';
    toggle.addEventListener('click', function () {
      pending.forEach(function (a) {
        if (allOn) stagedAdd.delete(normMac(a.mac)); else stagedAdd.add(normMac(a.mac));
      });
      renderDhcpPlan(lastDhcpPlan);
    });
    head.appendChild(toggle);
    els.netDhcpPlan.appendChild(head);
    pending.forEach(function (a) { els.netDhcpPlan.appendChild(suggestedRow(a)); });
  }

  const unassigned = (plan && plan.unassigned) || [];
  const assignable = unassigned.filter(function (a) { return !a.randomized; });
  const randomised = unassigned.filter(function (a) { return a.randomized; });
  if (assignable.length) {
    dhcpHead('Unassigned · ' + assignable.length);
    assignable.forEach(function (a) { els.netDhcpPlan.appendChild(unassignedRow(a)); });
  }
  if (randomised.length) {
    dhcpHead('Randomised — private MAC, not reservable · ' + randomised.length);
    randomised.forEach(function (a) { els.netDhcpPlan.appendChild(randomisedRow(a)); });
  }
}

// ---- manual staged rows (chips) ----
function renderManualStaged() {
  if (!els.netDhcpManualStaged) return;
  els.netDhcpManualStaged.innerHTML = '';
  if (!manualAdds.length) { els.netDhcpManualStaged.hidden = true; return; }
  els.netDhcpManualStaged.hidden = false;
  manualAdds.forEach(function (m, i) {
    const chip = document.createElement('span');
    chip.className = 'net-dhcp-chip';
    chip.appendChild(document.createTextNode(
      (m.name ? m.name + ' · ' : '') + m.mac + ' → ' + m.ip + ' '));
    const x = document.createElement('button');
    x.type = 'button';
    x.className = 'net-dhcp-chip-x';
    x.title = 'Delete this staged add';
    x.setAttribute('aria-label', 'Delete this staged add');
    x.innerHTML = '<svg class="icon" aria-hidden="true"><use href="#i-x"></use></svg>';
    x.addEventListener('click', function () {
      manualAdds.splice(i, 1);
      renderDhcpPlan(lastDhcpPlan);
    });
    chip.appendChild(x);
    els.netDhcpManualStaged.appendChild(chip);
  });
}

// ---- apply bar + live slot budget ----
function updateDhcpApplyBar(plan) {
  const has = stagedCount() > 0;
  if (els.netDhcpApplyBar) els.netDhcpApplyBar.hidden = !has;
  if (!has) return;
  const cap = plan && typeof plan.capacity === 'number' ? plan.capacity : 10;
  const used = plan && typeof plan.reservations_used === 'number' ? plan.reservations_used : 0;
  const usedAfter = used - stagedRemove.size + stagedCreateCount();
  if (els.netDhcpBudget) {
    els.netDhcpBudget.textContent = 'After: ' + usedAfter + '/' + cap + ' used';
    els.netDhcpBudget.classList.toggle('is-over', usedAfter > cap);
  }
  if (els.netDhcpApply) {
    els.netDhcpApply.textContent = '';
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    svg.setAttribute('class', 'icon');
    svg.setAttribute('aria-hidden', 'true');
    const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
    use.setAttribute('href', '#i-router');
    svg.appendChild(use);
    els.netDhcpApply.appendChild(svg);
    const adds = stagedAdd.size + manualAdds.length;
    const parts = [];
    if (stagedRemove.size) parts.push('remove ' + stagedRemove.size);
    if (adds) parts.push('add ' + adds);
    els.netDhcpApply.appendChild(
      document.createTextNode(' Apply changes (' + parts.join(' · ') + ')'));
  }
}

function renderDhcpPlan(plan) {
  lastDhcpPlan = plan || null;

  // A failed binding read means we can't tell what's reserved — say so plainly
  // rather than the old misleading "all already applied".
  if (plan && plan.bindings_known === false) {
    if (els.netDhcpExistingWrap) els.netDhcpExistingWrap.hidden = true;
    els.netDhcpPlan.innerHTML = '';
    if (els.netDhcpApplyBar) els.netDhcpApplyBar.hidden = true;
    if (els.netDhcpManualStaged) els.netDhcpManualStaged.hidden = true;
    els.netDhcpWarnings.hidden = true;
    els.netDhcpNote.hidden = false;
    els.netDhcpNote.textContent =
      "Couldn't read the router's current reservations — tap Refresh to try again.";
    return;
  }

  // Drop staged items that no longer exist in this fresh plan (keep state honest).
  const existIds = new Set(((plan && plan.existing) || [])
    .map(function (e) { return e.inst_id; }).filter(Boolean));
  Array.from(stagedRemove).forEach(function (id) {
    if (!existIds.has(id)) stagedRemove.delete(id);
  });
  const pendingMacs = new Set(
    planPendingAssignments(plan).map(function (a) { return normMac(a.mac); }));
  Array.from(stagedAdd).forEach(function (m) {
    if (!pendingMacs.has(m)) stagedAdd.delete(m);
  });

  renderDhcpExisting(plan);
  renderDhcpSuggestions(plan);
  renderManualStaged();

  const warnings = (plan && plan.warnings) || [];
  els.netDhcpWarnings.innerHTML = '';
  els.netDhcpWarnings.hidden = warnings.length === 0;
  warnings.forEach(function (w) {
    const p = document.createElement('p');
    p.className = 'net-dhcp-warning';
    p.textContent = '⚠️ ' + w;
    els.netDhcpWarnings.appendChild(p);
  });

  updateDhcpApplyBar(plan);

  const cap = plan && typeof plan.capacity === 'number' ? plan.capacity : null;
  const used = plan && typeof plan.reservations_used === 'number' ? plan.reservations_used : null;
  const free = plan && typeof plan.slots_free === 'number' ? plan.slots_free : null;
  const pendingCount = planPendingAssignments(plan).length;
  els.netDhcpNote.hidden = false;
  if (cap != null && used != null) {
    let note = used + ' of ' + cap + ' reservations used · ' + (free || 0) + ' free.';
    note += pendingCount
      ? ' ' + pendingCount + ' suggestion(s) available to add.'
      : ' Everything suggested is already reserved. ✅';
    els.netDhcpNote.textContent = note;
  } else {
    els.netDhcpNote.textContent = pendingCount + ' suggestion(s) to add.';
  }
}

async function loadDhcpPlan() {
  if (dhcpPlanLoading) return;
  dhcpPlanLoading = true;
  els.netDhcpNote.hidden = false;
  els.netDhcpNote.textContent = 'Computing plan…';
  try {
    const plan = await jsonApi('/api/network/dhcp-plan');
    renderDhcpPlan(plan);
    dhcpPlanLoaded = true;
  } catch (exc) {
    els.netDhcpPlan.innerHTML = '';
    els.netDhcpWarnings.hidden = true;
    if (els.netDhcpExistingWrap) els.netDhcpExistingWrap.hidden = true;
    if (els.netDhcpApplyBar) els.netDhcpApplyBar.hidden = true;
    els.netDhcpNote.hidden = false;
    els.netDhcpNote.textContent = 'Could not compute plan: ' + (exc && exc.message ? exc.message : exc);
  } finally {
    dhcpPlanLoading = false;
  }
}

// Persist a device's category override (#176), then recompute so it becomes a
// suggested add — and auto-stage it, so "pick a group" → staged add is one step.
async function setDhcpOverride(mac, category) {
  try {
    await jsonApi('/api/network/dhcp-overrides/' + encodeURIComponent(mac), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ category: category || '' }),
    });
    if (category) stagedAdd.add(normMac(mac));
    toast(category ? 'Assigned to ' + category : 'Group cleared', 'success');
    await loadDhcpPlan();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to assign group: ' + (exc.message || exc), 'error');
    }
  }
}

// Stage (not write) a manual reservation — applied with everything else on Apply.
function stageManualBinding() {
  if (!els.netDhcpManualMac || !els.netDhcpManualIp) return;
  const mac = normMac((els.netDhcpManualMac.value || '').trim());
  const ip = (els.netDhcpManualIp.value || '').trim();
  const name = (els.netDhcpManualName.value || '').trim();
  if (!/^[0-9A-F]{2}(:[0-9A-F]{2}){5}$/.test(mac)) {
    toast('Enter a valid MAC (AA:BB:CC:DD:EE:FF)', 'error');
    return;
  }
  if (!/^\d{1,3}(\.\d{1,3}){3}$/.test(ip)) {
    toast('Enter a valid IP (192.168.0.x)', 'error');
    return;
  }
  manualAdds.push({ mac: mac, ip: ip, name: name });
  els.netDhcpManualMac.value = '';
  els.netDhcpManualIp.value = '';
  els.netDhcpManualName.value = '';
  renderDhcpPlan(lastDhcpPlan);
}

function clearDhcpStaged() {
  stagedRemove.clear();
  stagedAdd.clear();
  manualAdds = [];
  renderDhcpPlan(lastDhcpPlan);
}

async function applyDhcpChanges() {
  if (dhcpApplying) return;
  const removes = Array.from(stagedRemove);
  const addMacs = Array.from(stagedAdd);
  const manual = manualAdds.slice();
  const total = removes.length + addMacs.length + manual.length;
  if (!total) return;

  const cap = lastDhcpPlan && typeof lastDhcpPlan.capacity === 'number' ? lastDhcpPlan.capacity : 10;
  const used = lastDhcpPlan && typeof lastDhcpPlan.reservations_used === 'number'
    ? lastDhcpPlan.reservations_used : 0;
  const usedAfter = used - removes.length + stagedCreateCount();
  const overflowNote = usedAfter > cap
    ? ' ⚠️ That needs ' + usedAfter + ' slots but the router only has ' + cap
      + ' — remove more, or untick some adds (the rest will be skipped).'
    : '';
  const parts = [];
  if (removes.length) parts.push('remove ' + removes.length);
  if (addMacs.length + manual.length) parts.push('add ' + (addMacs.length + manual.length));
  const ok = await confirmAction({
    title: 'Apply reservation changes?',
    message: 'This will ' + parts.join(' and ') + ' on the router in one sequence '
      + '(removals first, then adds). Devices pick up a new address on their next '
      + 'lease renewal.' + overflowNote,
    okLabel: 'Apply to router',
    danger: true,
  });
  if (!ok) return;
  dhcpApplying = true;
  if (els.netDhcpApply) els.netDhcpApply.disabled = true;
  els.netDhcpNote.hidden = false;
  els.netDhcpNote.textContent = 'Applying ' + total + ' change(s)…';
  try {
    const res = await jsonApi('/api/network/dhcp-reservations/apply', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ remove: removes, add_macs: addMacs, add_manual: manual }),
    });
    const removed = (res && res.removed) || 0;
    const added = (res && res.added) || 0;
    const failed = (res && res.failed) || 0;
    const cap2 = (res && res.capacity) || cap;
    const summary = [];
    if (removed) summary.push(removed + ' removed');
    if (added) summary.push(added + ' added');
    if (res && res.table_full) {
      toast((summary.join(', ') || 'No room') + " — the router holds only " + cap2
        + ' reservations; remove more to add the rest.', 'error');
    } else if (failed) {
      toast((summary.join(', ') || '0 applied') + ', ' + failed + ' failed — see the list', 'error');
    } else {
      toast(summary.join(', ') || 'No changes', 'success');
    }
    stagedRemove.clear();
    stagedAdd.clear();
    manualAdds = [];
    await loadDhcpPlan();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Apply failed: ' + (exc.message || exc), 'error');
    }
    els.netDhcpNote.textContent = 'Apply failed: ' + (exc && exc.message ? exc.message : exc);
  } finally {
    dhcpApplying = false;
    if (els.netDhcpApply) els.netDhcpApply.disabled = false;
  }
}

export function wireDhcpPlan() {
  if (els.netDhcpCard) {
    els.netDhcpCard.addEventListener('toggle', function () {
      if (els.netDhcpCard.open && !dhcpPlanLoaded) loadDhcpPlan();
    });
  }
  if (els.netDhcpRefresh) {
    els.netDhcpRefresh.addEventListener('click', function (e) {
      e.preventDefault();
      loadDhcpPlan();
    });
  }
  if (els.netDhcpApply) {
    els.netDhcpApply.addEventListener('click', function (e) {
      e.preventDefault();
      applyDhcpChanges();
    });
  }
  if (els.netDhcpClear) {
    els.netDhcpClear.addEventListener('click', function (e) {
      e.preventDefault();
      clearDhcpStaged();
    });
  }
  if (els.netDhcpManualAdd) {
    els.netDhcpManualAdd.addEventListener('click', function (e) {
      e.preventDefault();
      stageManualBinding();
    });
  }
}
