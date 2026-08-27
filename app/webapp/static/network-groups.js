/* Network tab — device-group rename/delete dialog.
 *
 * Split out of network-devices.js (issue #702): the "My groups" view's rename
 * and delete dialog for a real (persisted) group, opened from the pencil icon
 * on a group header network-devices.js renders. The dialog only ever edits a
 * real group; the synthetic "Unclassified" catch-all has no pencil, so there
 * is no path here for it. network-devices.js still owns the group *list*
 * (groupsOf / membersOf) and the render/edit-pencil wiring; this module calls
 * back into renderNetwork() (network.js) after a write, same pattern as the
 * device detail modal.
 */

'use strict';

import { state, els, toast } from './state.js';
import { jsonApi, reportActionFailure } from './api.js';
import { renderNetwork } from './network.js';
import { confirmAction } from './confirm.js';
import { closeDialog, openDialog } from './dialog.js';
import { membersOf } from './network-devices.js';

let selectedGroup = null;

export function openGroupDialog(name) {
  if (!els.netGroupDialog) return;
  selectedGroup = name;
  const members = membersOf((state.network && state.network.devices) || [], name);
  els.netGroupDialogTitle.textContent = name;
  els.netGroupMembers.textContent = members.length +
    (members.length === 1 ? ' device' : ' devices');
  els.netGroupName.value = name;
  if (els.netGroupSave) els.netGroupSave.disabled = true;
  openDialog(els.netGroupDialog);
  els.netGroupName.focus();
}

function closeGroupDialog() {
  selectedGroup = null;
  closeDialog(els.netGroupDialog);
}

// Rewrite the group on every local device row so the list re-renders without
// waiting for the next poll — the same optimistic pattern as the device modal.
function patchGroupLocally(from, to) {
  if (!(state.network && Array.isArray(state.network.devices))) return;
  state.network.devices = state.network.devices.map(function (d) {
    return (d.group || '') === from ? Object.assign({}, d, { group: to || null }) : d;
  });
}

async function saveGroupName() {
  const name = selectedGroup;
  if (!name) return;
  const next = els.netGroupName.value.trim();
  if (!next || next === name) { closeGroupDialog(); return; }
  if (els.netGroupSave) els.netGroupSave.disabled = true;
  try {
    await jsonApi('/api/network/groups/rename', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name, new_name: next }),
    });
    patchGroupLocally(name, next);
    closeGroupDialog();
    renderNetwork();
    toast('Group renamed', 'success');
  } catch (exc) {
    reportActionFailure(exc, 'Failed to rename group');
    if (els.netGroupSave) els.netGroupSave.disabled = false;
  }
}

async function deleteGroup() {
  const name = selectedGroup;
  if (!name) return;
  const members = membersOf((state.network && state.network.devices) || [], name);
  const ok = await confirmAction({
    title: 'Delete group?',
    message: '"' + name + '" is removed. Its ' + members.length +
      (members.length === 1 ? ' device moves' : ' devices move') +
      ' to Unclassified — nothing is deleted from the network.',
    okLabel: 'Delete',
    danger: true,
  });
  if (!ok) return;
  try {
    await jsonApi('/api/network/groups/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name }),
    });
    patchGroupLocally(name, '');
    closeGroupDialog();
    renderNetwork();
    toast('Group deleted', 'success');
  } catch (exc) {
    reportActionFailure(exc, 'Failed to delete group');
  }
}

export function wireNetGroupDialog() {
  if (!els.netGroupDialog) return;
  els.netGroupDialogClose.addEventListener('click', closeGroupDialog);
  els.netGroupDialog.addEventListener('click', function (ev) {
    if (ev.target === els.netGroupDialog) closeGroupDialog();  // backdrop
  });
  els.netGroupName.addEventListener('input', function () {
    const next = els.netGroupName.value.trim();
    if (els.netGroupSave) els.netGroupSave.disabled = !next || next === selectedGroup;
  });
  els.netGroupName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); saveGroupName(); }
  });
  if (els.netGroupSave) els.netGroupSave.addEventListener('click', saveGroupName);
  if (els.netGroupDelete) els.netGroupDelete.addEventListener('click', deleteGroup);
}
