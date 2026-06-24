/* Browser-side last-good API snapshots.
 *
 * This is deliberately allowlisted and read-only-response-only: command
 * responses, auth failures, edit state, and security/presence/event payloads
 * are not persisted here. A versioned envelope lets future incompatible shapes
 * be ignored cleanly instead of rendering stale garbage.
 */

'use strict';

import { state } from './state.js';

const SNAPSHOT_VERSION = 1;
const SNAPSHOT_KEY = 'home-automation.apiSnapshots.v1';
const ALLOWED = {
  units: true,
  energyLive: true,
  energyToday: true,
  plugs: true,
  lights: true,
  network: true,
};

function emptyStore() {
  return { version: SNAPSHOT_VERSION, snapshots: {} };
}

function loadStore() {
  try {
    const raw = localStorage.getItem(SNAPSHOT_KEY);
    if (!raw) return emptyStore();
    const store = JSON.parse(raw);
    if (!store || store.version !== SNAPSHOT_VERSION || typeof store.snapshots !== 'object') {
      return emptyStore();
    }
    return store;
  } catch (_) {
    return emptyStore();
  }
}

function writeStore(store) {
  try {
    localStorage.setItem(SNAPSHOT_KEY, JSON.stringify(store));
  } catch (_) {
    // Private mode / quota pressure: snapshots are an enhancement only.
  }
}

function cloneJson(value) {
  return JSON.parse(JSON.stringify(value));
}

export function saveSnapshot(name, body) {
  if (!ALLOWED[name] || body == null) return;
  const store = loadStore();
  try {
    store.snapshots[name] = {
      saved_at: new Date().toISOString(),
      body: cloneJson(body),
    };
    writeStore(store);
    state.snapshotRestored[name] = false;
    state.snapshotUpdatedAt[name] = null;
  } catch (_) {
    // Non-serialisable response shapes are ignored rather than risking storage.
  }
}

export function restoreSnapshot(name) {
  if (!ALLOWED[name]) return null;
  const store = loadStore();
  const snap = store.snapshots && store.snapshots[name];
  if (!snap || snap.body == null) return null;
  try {
    state.snapshotRestored[name] = true;
    state.snapshotUpdatedAt[name] = snap.saved_at || null;
    return cloneJson(snap.body);
  } catch (_) {
    return null;
  }
}

export function snapshotLabel(name) {
  const raw = state.snapshotUpdatedAt[name];
  if (!raw) return 'Last saved; refreshing...';
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return 'Last saved; refreshing...';
  return 'Last saved ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + '; refreshing...';
}

export function isSnapshotRestored(name) {
  return state.snapshotRestored[name] === true;
}
