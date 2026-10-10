/* Presence card controller — boot/core (split out of security.js, issue #197;
 * further split, issue #454).
 *
 * Owns the people list (render, hide, rename), the detail modal, and the
 * House card's who-is-home row on Home (#885) — this is a leaf-ish module: it depends only
 * on ./state.js and ./api.js plus its three feature sibling modules (issue
 * #454 maintainability split, mirroring the network.js → network-devices/
 * wifi/dhcp.js boot + feature-module pattern):
 *   ./presence-location.js    home-location editor + "this device" browser GPS
 *   ./presence-automation.js  alarm-automation knobs (arm delay, kids-home)
 *   ./presence-push.js        Web Push enrolment
 * Reads through GET /api/presence, /api/location and /api/presence/automation
 * (the latter two owned by the sub-modules); writes are PUT/POST calls that
 * re-render from the returned live state. Other security sub-modules may
 * import the shared formatter (fmtTime) without creating a cycle.
 */

'use strict';

import {
  state,
  els,
  toast,
  reportFetchFailure,
  reportFetchOk,
  persistedFlag,
  PRESENCE_SHOW_HIDDEN_KEY,
} from './state.js';
import { jsonApi, isAuthRequired, reportActionFailure } from './api.js';
import { emptyStateEl } from './empty-state.js';
import { toggleMarkup } from './toggle.js';
import { createViewState } from './view-state.js';
import { hydrateThisDeviceLocation, refreshThisDeviceLocation, wirePresenceLocationControls } from './presence-location.js';
import { renderKidsHomeToggle, renderPresenceAutomationNote, wirePresenceAutomationControls } from './presence-automation.js';
import { wirePresencePushControls } from './presence-push.js';
import { closeDialog, openDialog } from './dialog.js';
import { confirmAction } from './confirm.js';
import { friendlyError } from './format.js';
import { rowEl } from './row.js';
import { chipEl } from './chip.js';
import { showTab } from './tabs.js';

// Re-export so callers (security.js, main.js) keep a single import surface —
// same convention security.js itself uses for its own sub-modules.
export { loadLocation } from './presence-location.js';
export { loadPresenceAutomation } from './presence-automation.js';

// The "show hidden people" filter, on the shared localStorage wrapper.
const showHiddenPref = persistedFlag(PRESENCE_SHOW_HIDDEN_KEY, false);

const presenceView = createViewState();

export function fmtTime(value) {
  if (!value) return '-';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString([], {
    month: 'short',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

function fmtDistance(value) {
  if (value === null || value === undefined) return 'unknown';
  const n = Number(value);
  if (!Number.isFinite(n)) return 'unknown';
  if (n >= 1000) return (n / 1000).toFixed(1) + ' km';
  return Math.round(n) + ' m';
}

function coordsKey(entity) {
  if (entity.latitude === null || entity.latitude === undefined ||
      entity.longitude === null || entity.longitude === undefined) return '';
  const lat = Number(entity.latitude);
  const lon = Number(entity.longitude);
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return '';
  return lat.toFixed(4) + ',' + lon.toFixed(4);
}

function placeLabel(entity) {
  const key = coordsKey(entity);
  if (!key) return '';
  const value = state.presencePlaces[key];
  return typeof value === 'string' ? value : '';
}

function ensurePlaceLabel(entity) {
  const key = coordsKey(entity);
  if (!key || state.presencePlaces[key] !== undefined) return;
  state.presencePlaces[key] = null;
  const url = '/api/location/reverse?lat=' + encodeURIComponent(entity.latitude) +
    '&lon=' + encodeURIComponent(entity.longitude);
  jsonApi(url).then(function (body) {
    state.presencePlaces[key] = body && body.available ? (body.label || '') : '';
    const selected = state.selectedPresenceId ? presenceById(state.selectedPresenceId) : null;
    if (selected && coordsKey(selected) === key && els.presenceDetailPlace) {
      els.presenceDetailPlace.textContent = state.presencePlaces[key] || '—';
    }
    renderPresence();
  }).catch(function () {
    state.presencePlaces[key] = '';
  });
}

function presenceLabel(entity) {
  if (entity.at_home === true) return 'Home';
  if (entity.at_home === false) return 'Away';
  return 'Unknown';
}

export function presenceEntityLabel(entity) {
  return entity.display_name || entity.name || entity.entity_id || 'Unknown';
}

function isThisDevice(entity) {
  return entity && entity.entity_id === '__this_device__';
}

export function presenceById(entityId) {
  const entities = (state.presence && state.presence.entities) || [];
  if (state.thisDevicePresence && state.thisDevicePresence.entity_id === entityId) {
    return state.thisDevicePresence;
  }
  return entities.find(function (e) { return e.entity_id === entityId; }) || null;
}

function sourceLabel(entity) {
  if (entity.source === 'webhook') return 'Shortcut';
  if (entity.source === 'icloud') return 'Find My';
  if (entity.source === 'browser') return 'Browser GPS · diagnostic only';
  return entity.source || 'Unknown';
}

function showPresenceState(message, retry) {
  const options = retry ? {
    actionLabel: 'Retry',
    onAction: function () { loadPresence(); },
  } : null;
  els.presenceList.appendChild(emptyStateEl('smartphone', message, options));
}

function hidePresenceRefreshNote() {
  if (els.presenceRefreshNote) els.presenceRefreshNote.hidden = true;
}

function markPresenceFailure() {
  const hasLastGood = !!(state.presence && state.presence.available !== false);
  presenceView.set(hasLastGood ? 'stale' : 'error', {
    liveUnavailable: true,
  });
  reportFetchFailure(
    'presence',
    { message: 'live data unavailable' },
    'presence'
  );
  renderPresence();
}

// Fail loud when the Find My diagnostics source itself is broken (#442) —
// distinct from a person simply being "away, unknown exact location".
const BROKEN_SOURCE_REASONS = ['error', '2fa_required', 'terms_required', 'not_configured'];
function brokenSourceText(reason) {
  if (reason === '2fa_required') return 'Find My needs iCloud sign-in again';
  if (reason === 'terms_required') return 'Find My needs Apple’s new terms accepted';
  return 'Find My location tracking is down';
}

// Who is home, as one row of Home's House card (#885; it replaces the Home
// locator card of #438): "2 home · 1 away" and who is out, counted over the
// people the People group lists (not hidden, not this device). The row opens
// Security › People, where each person's sheet holds their place, last seen
// and the role the voice locator answers to ("where's dad?"). It derives from
// the same state.presence the People group polls: no new fetch.
function renderHousePeople() {
  const list = els.housePeople;
  if (!list) return;
  list.innerHTML = '';
  const presence = state.presence;
  const diag = (presence && presence.diagnostics) || {};
  const entities = presence && presence.available !== false
    ? (presence.entities || []).filter(function (e) { return !e.hidden; })
    : [];
  let title;
  let meta;
  let chip = null;
  if (!presence) {
    list.hidden = true;
    return;
  }
  if (presence.available === false) {
    title = 'Presence unavailable';
    meta = BROKEN_SOURCE_REASONS.indexOf(presence.reason) !== -1
      ? brokenSourceText(presence.reason) : 'Not configured';
  } else if (!entities.length) {
    list.hidden = true;
    return;
  } else {
    const home = entities.filter(function (e) { return e.at_home === true; });
    const away = entities.filter(function (e) { return e.at_home === false; });
    const unknown = entities.length - home.length - away.length;
    title = home.length + ' home · ' + away.length + ' away' + (unknown ? ' · ' + unknown + ' unknown' : '');
    if (away.length === 1) {
      const dist = fmtDistance(away[0].distance_from_home_m);
      meta = [presenceEntityLabel(away[0]) + ' away', dist !== 'unknown' ? dist : '', placeLabel(away[0])]
        .filter(Boolean).join(' · ');
      ensurePlaceLabel(away[0]);
    } else if (away.length) {
      meta = away.map(presenceEntityLabel).join(', ') + ' away';
    } else {
      meta = home.length ? 'Everyone home' : '';
    }
    // A broken Find My source leaves the counts frozen: say so (#442).
    if (diag.available === false && BROKEN_SOURCE_REASONS.indexOf(diag.reason) !== -1) {
      chip = chipEl(diag.reason === '2fa_required' ? 'Sign in' : 'Find My down', 'attention', 'house-people-chip');
      chip.title = brokenSourceText(diag.reason);
    } else if (presenceView.state === 'stale') {
      chip = chipEl('Stale', null, 'house-people-chip');
    }
  }
  list.appendChild(rowEl({
    className: 'house-people-row',
    glyph: 'users',
    title: title,
    meta: meta || null,
    chip: chip,
    openLabel: title + (meta ? ': ' + meta : '') + '. Open People',
    onOpen: function () { showTab('security', els.presenceCard); },
    chevron: true,
  }));
  list.hidden = false;
}

export function renderPresence() {
  renderHousePeople();
  renderKidsHomeToggle(presenceView.state === 'ready');
  if (!els.presenceSummary || !els.presenceList || !els.presenceNote) return;
  els.presenceList.innerHTML = '';
  els.presenceList.dataset.state = presenceView.state;
  els.presenceList.setAttribute(
    'aria-busy',
    presenceView.state === 'loading' ? 'true' : 'false'
  );
  const presence = state.presence;
  // The per-account iCloud rows (#659) live on the diagnostics, which survive
  // a stale poll — render them from whatever snapshot is held.
  renderPresenceAccounts(presence);
  if (presenceView.state === 'loading') {
    els.presenceSummary.textContent = 'Loading';
    showPresenceState('Reading presence…', false);
    els.presenceNote.hidden = true;
    hidePresenceRefreshNote();
    return;
  }
  if (presenceView.state === 'error' && presenceView.liveUnavailable) {
    els.presenceSummary.textContent = 'Unavailable';
    showPresenceState('Presence unavailable', true);
    els.presenceNote.hidden = false;
    els.presenceNote.textContent =
      'Live presence data is unavailable. Check the connection, then retry.';
    hidePresenceRefreshNote();
    return;
  }
  if (!presence) {
    els.presenceSummary.textContent = '—';
    els.presenceNote.hidden = true;
    return;
  }
  if (presence.available === false) {
    els.presenceSummary.textContent = 'Unavailable';
    showPresenceState('Presence unavailable', false);
    els.presenceNote.hidden = false;
    els.presenceNote.textContent = presence.reason === '2fa_required'
      ? 'iCloud needs re-authentication — use Renew trust in Accounts below.'
      : presence.reason === 'terms_required'
        ? 'An iCloud account must accept Apple’s updated terms — see Accounts below.'
        : friendlyError(presence.detail, 'Presence is not configured.');
    hidePresenceRefreshNote();
    return;
  }

  const entities = (presence.entities || []).concat(state.thisDevicePresence ? [state.thisDevicePresence] : []);
  const sorted = entities.slice().sort(function (a, b) {
    if (a.entity_id === '__this_device__') return -1;
    if (b.entity_id === '__this_device__') return 1;
    return presenceEntityLabel(a).localeCompare(presenceEntityLabel(b), undefined, { sensitivity: 'base' });
  });
  const hiddenCount = sorted.filter(function (e) { return e.hidden && !isThisDevice(e); }).length;
  const visible = state.presenceShowHidden
    ? sorted
    : sorted.filter(function (e) { return !e.hidden || isThisDevice(e); });
  const counted = visible.filter(function (e) { return !isThisDevice(e); });

  const homeCount = counted.filter(function (e) { return e.at_home === true; }).length;
  const awayCount = counted.filter(function (e) { return e.at_home === false; }).length;
  const unknownCount = counted.filter(function (e) { return e.at_home !== true && e.at_home !== false; }).length;
  els.presenceSummary.textContent = homeCount + ' home · ' + awayCount + ' away' +
    (unknownCount ? ' · ' + unknownCount + ' unknown' : '');
  if (els.presenceHiddenToggle) {
    els.presenceHiddenToggle.hidden = hiddenCount === 0;
    els.presenceHiddenToggle.textContent = (state.presenceShowHidden ? 'Hide ' : 'Show ') + hiddenCount + ' hidden';
    els.presenceHiddenToggle.setAttribute('aria-pressed', state.presenceShowHidden ? 'true' : 'false');
  }

  if (!entities.length) {
    showPresenceState('No presence entities configured', false);
    els.presenceNote.hidden = true;
    renderPresenceRefreshNote();
    renderPresenceAutomationNote();
    return;
  }

  if (presenceView.state === 'stale') {
    els.presenceNote.hidden = false;
    els.presenceNote.textContent = presenceView.lastUpdatedLabel() + ' · live data unavailable';
  } else {
    els.presenceNote.hidden = visible.length > 0;
    els.presenceNote.textContent = visible.length ? '' : 'No presence entities shown.';
  }

  if (!visible.length) showPresenceState('No presence entities shown', false);

  const list = document.createElement('ul');
  list.className = 'action-rows';
  visible.forEach(function (entity) {
    list.appendChild(personRow(entity));
    ensurePlaceLabel(entity);
  });
  if (visible.length) els.presenceList.appendChild(list);

  renderPresenceRefreshNote();
  renderPresenceAutomationNote();
}

// Where a person is, in one muted line (#882; four lines before, J-10):
// home, or away with the distance and the place; who, how and when sit in the
// person sheet. This device's line says what it is instead: browser GPS that
// never drives the alarm.
function personMeta(entity) {
  if (isThisDevice(entity)) return sourceLabel(entity);
  if (entity.at_home === true) return 'Home';
  if (entity.at_home !== false) return 'Unknown';
  const dist = fmtDistance(entity.distance_from_home_m);
  const place = placeLabel(entity);
  return ['Away', dist !== 'unknown' ? dist : '', place].filter(Boolean).join(' · ');
}

// One person on the shared row (row.js): the row opens the person sheet.
function personRow(entity) {
  const classes = ['presence-row',
    entity.at_home === true ? 'is-home' : (entity.at_home === false ? 'is-away' : 'is-unknown')];
  if (entity.hidden) classes.push('is-hidden');
  if (entity.stale) classes.push('is-stale');
  const row = rowEl({
    className: classes.join(' '),
    glyph: isThisDevice(entity) ? 'smartphone' : 'user',
    title: presenceEntityLabel(entity),
    meta: personMeta(entity),
    chip: entity.stale ? chipEl('Stale', null, 'presence-stale-chip') : null,
    openLabel: presenceEntityLabel(entity) + ': ' + personMeta(entity),
    onOpen: function (btn) { openPresenceDetail(entity.entity_id, btn); },
    chevron: true,
  });
  row.dataset.entityId = entity.entity_id;
  return row;
}

function renderPresenceRefreshNote() {
  if (!els.presenceRefreshNote) return;
  els.presenceRefreshNote.hidden = false;
  const diag = (state.presence && state.presence.diagnostics) || {};
  const interval = Math.max(1, Math.round((Number(diag.refresh_interval_s) || 300) / 60));
  const last = diag.refreshed_at ? fmtTime(diag.refreshed_at) : 'not yet';
  els.presenceRefreshNote.textContent =
    'The open tab reloads the local snapshot every 10 s. Find My refreshes in the background about every ' + interval +
    ' min. This device is browser GPS only: it updates only while this tab/PWA is open and is not used for alarm automation. Alarm automation uses Shortcut webhook people. Last Find My refresh: ' + last + '.';
}

// ------------------------------------------- iCloud accounts + trust renewal
// (issue #659) One row per configured Apple ID from diagnostics.accounts[]:
// who it is, whether its cached session still holds Apple's ~30-day browser
// trust, and a Renew trust action. Renewal is two POSTs on the SAME live
// pyicloud session server-side: begin (Apple pushes a 6-digit code) → the code
// dialog → complete. Untrusted is not broken — Find My keeps serving; only
// fresh sign-ins get expensive — so the row states it plainly rather than as
// an error, and the button is merely emphasised. An account Apple holds until
// updated terms are accepted (#736) gets no button at all: renewal re-signs in
// and hits the same refusal, and accepting is the account holder's own step.
const accountNotes = {};   // label → inline result line, survives the 10 s re-render
const accountBusy = {};    // label → true while begin is in flight
let trustAccount = null;   // the account the code dialog is open for

function accountLabel(acct) {
  return acct.display_name || ('account ' + acct.label);
}

function accountTrustState(acct) {
  if (acct.available === false && acct.reason === 'terms_required') {
    return { cls: 'is-broken', text: 'needs Apple’s updated terms accepted' };
  }
  if (acct.available === false) {
    return { cls: 'is-broken', text: 'broken: ' + friendlyError(acct.reason, 'sign-in failed') };
  }
  if (acct.trusted === true) return { cls: 'is-trusted', text: 'trusted' };
  if (acct.trusted === false) {
    return { cls: 'is-untrusted', text: 'untrusted — password login on each fresh sign-in' };
  }
  return { cls: 'is-unknown', text: 'trust unknown — no session yet' };
}

// The People group's Accounts row value (#882): how many Apple IDs, and a
// chip when one needs you: a broken sign-in in danger, a lapsed trust or
// Apple's terms in attention.
function renderAccountsMeta(accounts) {
  const meta = els.presenceAccountsMeta;
  if (!meta) return;
  const line = meta.parentNode;
  line.querySelectorAll('.chip').forEach(function (chip) { chip.remove(); });
  meta.textContent = accounts.length
    ? accounts.length + (accounts.length === 1 ? ' account' : ' accounts')
    : 'None';
  const states = accounts.map(function (acct) { return { acct: acct, trust: accountTrustState(acct) }; });
  const needing = states.filter(function (s) { return s.trust.cls === 'is-broken' || s.trust.cls === 'is-untrusted'; });
  if (!needing.length) return;
  const broken = needing.some(function (s) {
    return s.trust.cls === 'is-broken' && s.acct.reason !== 'terms_required';
  });
  line.appendChild(chipEl(needing.length === 1 ? '1 needs you' : needing.length + ' need you',
    broken ? 'danger' : 'attention', 'presence-accounts-chip'));
}

function renderPresenceAccounts(presence) {
  if (!els.presenceAccounts || !els.presenceAccountsList) return;
  const diag = (presence && presence.diagnostics) || {};
  const accounts = Array.isArray(diag.accounts) ? diag.accounts : [];
  renderAccountsMeta(accounts);
  if (!accounts.length) {
    els.presenceAccounts.hidden = true;
    els.presenceAccountsList.innerHTML = '';
    return;
  }
  els.presenceAccounts.hidden = false;
  els.presenceAccountsList.innerHTML = '';
  accounts.forEach(function (acct) {
    const label = String(acct.label);
    const trust = accountTrustState(acct);
    const row = document.createElement('div');
    row.className = 'presence-account-row ' + trust.cls;
    row.dataset.account = label;
    row.dataset.testid = 'presence-account-row';

    const termsRequired = acct.available === false && acct.reason === 'terms_required';
    const main = document.createElement('div');
    main.className = 'presence-main';
    const name = document.createElement('span');
    name.className = 'presence-name';
    name.textContent = accountLabel(acct);
    main.appendChild(name);
    const stateLine = document.createElement('span');
    stateLine.className = 'presence-account-state';
    stateLine.textContent = trust.text;
    main.appendChild(stateLine);
    const noteText = termsRequired ? (acct.detail || accountNotes[label]) : accountNotes[label];
    if (noteText) {
      const note = document.createElement('span');
      note.className = 'presence-account-note';
      note.setAttribute('role', 'status');
      note.textContent = noteText;
      main.appendChild(note);
    }
    row.appendChild(main);
    if (termsRequired) {
      els.presenceAccountsList.appendChild(row);
      return;
    }

    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'range-tab presence-account-renew';
    if (trust.cls !== 'is-trusted') btn.classList.add('active');
    btn.dataset.testid = 'presence-account-renew';
    btn.textContent = accountBusy[label] ? 'Sending…' : 'Renew trust';
    btn.disabled = !!accountBusy[label];
    btn.setAttribute('aria-label', 'Renew iCloud trust for ' + accountLabel(acct));
    btn.addEventListener('click', function () { renewAccountTrust(acct); });
    row.appendChild(btn);
    els.presenceAccountsList.appendChild(row);
  });
}

function trustUrl(label, step) {
  return '/api/presence/icloud/' + encodeURIComponent(label) + '/trust/' + step;
}

async function renewAccountTrust(acct) {
  const label = String(acct.label);
  if (accountBusy[label]) return;
  const who = accountLabel(acct);
  const ok = await confirmAction({
    title: 'Renew iCloud trust',
    message: 'Apple will push a 6-digit code to ' + who + '\u2019s trusted devices. Have one at hand, then continue.',
    okLabel: 'Send code',
  });
  if (!ok) return;
  accountBusy[label] = true;
  delete accountNotes[label];
  renderPresenceAccounts(state.presence);
  try {
    const res = await jsonApi(trustUrl(label, 'begin'), { method: 'POST', timeoutMs: 60000 });
    if (res && res.status === 'code_sent') {
      openTrustCodeDialog(acct, res.detail);
    } else if (res && res.status === 'terms_required') {
      accountNotes[label] = res.detail || 'Apple requires this account to accept updated iCloud terms first.';
      toast('Accept Apple’s updated terms first', 'error');
      loadPresence();
    } else if (res && res.status === 'already_trusted') {
      accountNotes[label] = res.detail || 'Already trusted.';
      toast('Already trusted', 'success');
      loadPresence();
    } else {
      accountNotes[label] = (res && res.detail) || 'Trust renewal could not start.';
      toast('Trust renewal failed', 'error');
    }
  } catch (exc) {
    reportActionFailure(exc, 'Failed to start trust renewal');
  } finally {
    delete accountBusy[label];
    renderPresenceAccounts(state.presence);
  }
}

function openTrustCodeDialog(acct, hint) {
  if (!els.presenceTrustDialog) return;
  trustAccount = acct;
  els.presenceTrustTitle.textContent = 'Renew iCloud trust · ' + accountLabel(acct);
  els.presenceTrustHint.textContent = hint || 'Apple pushed a 6-digit code to the account\u2019s trusted devices.';
  els.presenceTrustCode.value = '';
  els.presenceTrustNote.hidden = true;
  els.presenceTrustNote.textContent = '';
  els.presenceTrustVerify.disabled = false;
  els.presenceTrustVerify.textContent = 'Verify';
  openDialog(els.presenceTrustDialog);
  els.presenceTrustCode.focus();
}

function closeTrustCodeDialog() {
  trustAccount = null;
  closeDialog(els.presenceTrustDialog);
}

function showTrustNote(text) {
  els.presenceTrustNote.textContent = text;
  els.presenceTrustNote.hidden = !text;
}

async function verifyTrustCode() {
  if (!trustAccount) return;
  const label = String(trustAccount.label);
  const code = (els.presenceTrustCode.value || '').replace(/\s+/g, '');
  if (!/^\d{6}$/.test(code)) {
    showTrustNote('Enter the 6-digit code.');
    els.presenceTrustCode.focus();
    return;
  }
  els.presenceTrustVerify.disabled = true;
  els.presenceTrustVerify.textContent = 'Verifying…';
  showTrustNote('');
  try {
    const res = await jsonApi(trustUrl(label, 'complete'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code: code }),
      timeoutMs: 60000,
    });
    const status = res && res.status;
    if (status === 'trusted') {
      accountNotes[label] = res.detail || 'Browser trust renewed.';
      toast('iCloud trust renewed', 'success');
      closeTrustCodeDialog();
      loadPresence();
      return;
    }
    // invalid_code / expired / failed — Apple's own words, inline; the dialog
    // stays open so a mistyped code can be retried without a second push.
    showTrustNote((res && res.detail) || 'Verification failed.');
    if (status !== 'invalid_code') accountNotes[label] = (res && res.detail) || 'Verification failed.';
    els.presenceTrustCode.focus();
    els.presenceTrustCode.select();
  } catch (exc) {
    reportActionFailure(exc, 'Failed to verify the code');
  } finally {
    els.presenceTrustVerify.disabled = false;
    els.presenceTrustVerify.textContent = 'Verify';
    renderPresenceAccounts(state.presence);
  }
}

function wirePresenceTrustDialog() {
  if (!els.presenceTrustDialog) return;
  els.presenceTrustClose.addEventListener('click', closeTrustCodeDialog);
  els.presenceTrustDialog.addEventListener('click', function (ev) {
    if (ev.target === els.presenceTrustDialog) closeTrustCodeDialog();
  });
  els.presenceTrustDialog.addEventListener('cancel', function () { trustAccount = null; });
  els.presenceTrustVerify.addEventListener('click', verifyTrustCode);
  els.presenceTrustCode.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); verifyTrustCode(); }
  });
}

export async function loadPresence() {
  if (!state.presence) {
    presenceView.set('loading', { liveUnavailable: false });
    renderPresence();
  }
  try {
    state.presence = await jsonApi('/api/presence');
    reportFetchOk('presence');
    const entities = (state.presence && state.presence.entities) || [];
    const hasEntities = entities.length > 0 || !!state.thisDevicePresence;
    presenceView.set(
      state.presence && state.presence.available === false
        ? 'error'
        : (hasEntities ? 'ready' : 'empty'),
      { updatedAt: new Date(), liveUnavailable: false }
    );
    refreshThisDeviceLocation();
  } catch (exc) {
    if (isAuthRequired(exc)) return;
    markPresenceFailure();
    return;
  }
  renderPresence();
}

// --------------------------------------------------- presence detail + config
let presenceDetailOpener = null;

function openPresenceDetail(entityId, trigger) {
  const entity = presenceById(entityId);
  if (!entity) return;
  presenceDetailOpener = trigger || null;
  state.selectedPresenceId = entityId;
  els.presenceDetailName.textContent = presenceEntityLabel(entity);
  els.presenceDetailStatus.textContent = presenceLabel(entity) + (entity.stale ? ' · stale' : '');
  els.presenceDetailSource.textContent = sourceLabel(entity);
  els.presenceDetailLastSeen.textContent = entity.last_seen ? fmtTime(entity.last_seen) : '—';
  els.presenceDetailDistance.textContent = fmtDistance(entity.distance_from_home_m);
  els.presenceDetailPlace.textContent = placeLabel(entity) || '—';
  renderPresenceMap(entity);
  els.presenceDisplayName.value = entity.display_name || '';
  els.presenceDisplayName.placeholder = entity.name || entity.entity_id || 'Custom label…';
  els.presenceOriginalName.textContent = 'System name: ' + (entity.name || entity.entity_id || 'Unknown');
  if (els.presenceRole) {
    els.presenceRole.value = entity.role || '';
    els.presenceRole.disabled = isThisDevice(entity);
  }
  renderPresenceHiddenToggle(entity);
  if (els.presenceDetailSave) els.presenceDetailSave.disabled = true;
  openDialog(els.presenceDialog);
  els.presenceDisplayName.focus();
}

function mapUrl(entity) {
  if (!coordsKey(entity)) return '';
  const lat = Number(entity.latitude);
  const lon = Number(entity.longitude);
  return 'https://www.openstreetmap.org/?mlat=' + encodeURIComponent(lat) +
    '&mlon=' + encodeURIComponent(lon) + '#map=16/' + encodeURIComponent(lat) +
    '/' + encodeURIComponent(lon);
}

function mapEmbedUrl(entity) {
  if (!coordsKey(entity)) return '';
  const lat = Number(entity.latitude);
  const lon = Number(entity.longitude);
  const delta = 0.01;
  const bbox = [lon - delta, lat - delta, lon + delta, lat + delta].join(',');
  return 'https://www.openstreetmap.org/export/embed.html?bbox=' +
    encodeURIComponent(bbox) + '&layer=mapnik&marker=' +
    encodeURIComponent(lat + ',' + lon);
}

function renderPresenceMap(entity) {
  const href = mapUrl(entity);
  if (!href) {
    els.presenceMapLink.hidden = true;
    els.presenceMapFrame.hidden = true;
    els.presenceMapFrame.removeAttribute('src');
    return;
  }
  els.presenceMapLink.hidden = false;
  els.presenceMapLink.href = href;
  els.presenceMapFrame.hidden = false;
  els.presenceMapFrame.src = mapEmbedUrl(entity);
  ensurePlaceLabel(entity);
}

function renderPresenceHiddenToggle(entity) {
  const btn = els.presenceHiddenDetailToggle;
  if (!btn) return;
  const isThisDevice = entity.entity_id === '__this_device__';
  const hidden = !!entity.hidden;
  btn.disabled = isThisDevice;
  btn.title = isThisDevice ? 'This device is always shown when browser location is enabled' : '';
  btn.className = 'toggle' + (hidden ? ' on' : ' off');
  btn.setAttribute('aria-checked', hidden ? 'true' : 'false');
  btn.innerHTML = toggleMarkup(hidden);
}

function closePresenceDetail() {
  state.selectedPresenceId = null;
  closeDialog(els.presenceDialog);
}

async function savePresenceDetail() {
  if (!state.selectedPresenceId) return;
  const id = state.selectedPresenceId;
  const newName = els.presenceDisplayName.value.trim();
  if (id === '__this_device__') {
    state.thisDevicePresence = Object.assign({}, state.thisDevicePresence, { display_name: newName || null });
    els.presenceDetailName.textContent = presenceEntityLabel(state.thisDevicePresence);
    renderPresence();
    if (els.presenceDetailSave) els.presenceDetailSave.disabled = true;
    return;
  }
  const newRole = els.presenceRole ? els.presenceRole.value.trim() : '';
  try {
    await Promise.all([
      jsonApi('/api/presence/entity-display-name', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entity_id: id, display_name: newName }),
      }),
      jsonApi('/api/presence/role', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entity_id: id, role: newRole }),
      }),
    ]);
    if (state.presence && Array.isArray(state.presence.entities)) {
      state.presence.entities = state.presence.entities.map(function (e) {
        return e.entity_id === id
          ? Object.assign({}, e, { display_name: newName || null, role: newRole || null })
          : e;
      });
    }
    const entity = presenceById(id);
    if (entity) els.presenceDetailName.textContent = presenceEntityLabel(entity);
    renderPresence();
    if (els.presenceDetailSave) els.presenceDetailSave.disabled = true;
    toast('Saved', 'success');
  } catch (exc) {
    reportActionFailure(exc, 'Failed to save');
  }
}

async function togglePresenceHidden() {
  const id = state.selectedPresenceId;
  if (!id) return;
  if (id === '__this_device__') return;
  const entity = presenceById(id);
  if (!entity) return;
  const next = !entity.hidden;
  try {
    await jsonApi('/api/presence/entity-hidden', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entity_id: id, hidden: next }),
    });
    if (state.presence && Array.isArray(state.presence.entities)) {
      state.presence.entities = state.presence.entities.map(function (e) {
        return e.entity_id === id ? Object.assign({}, e, { hidden: next }) : e;
      });
    }
    renderPresenceHiddenToggle(presenceById(id) || entity);
    renderPresence();
    toast(next ? 'Presence hidden' : 'Presence shown', 'success');
  } catch (exc) {
    reportActionFailure(exc, 'Failed to update presence');
  }
}

export function wirePresenceControls() {
  state.presenceShowHidden = showHiddenPref.read();

  if (hydrateThisDeviceLocation()) renderPresence();
  refreshThisDeviceLocation();

  if (els.presenceHiddenToggle) {
    els.presenceHiddenToggle.addEventListener('click', function () {
      state.presenceShowHidden = !state.presenceShowHidden;
      showHiddenPref.write(state.presenceShowHidden);
      renderPresence();
    });
  }
  wirePresenceAutomationControls();
  wirePresenceLocationControls();
  wirePresencePushControls();
  wirePresenceTrustDialog();

  if (els.presenceDetailClose) els.presenceDetailClose.addEventListener('click', closePresenceDetail);
  if (els.presenceDialog) {
    els.presenceDialog.addEventListener('click', function (ev) {
      if (ev.target === els.presenceDialog) closePresenceDetail();
    });
    // However it closed (×, Esc, backdrop), focus goes back to the row that
    // opened it, or its re-rendered replacement.
    els.presenceDialog.addEventListener('close', function () {
      const id = state.selectedPresenceId;
      state.selectedPresenceId = null;
      let target = presenceDetailOpener;
      presenceDetailOpener = null;
      if ((!target || !target.isConnected) && id && els.presenceList) {
        target = els.presenceList.querySelector('.presence-row[data-entity-id="' + CSS.escape(id) + '"] .action-row-main');
      }
      if (target && target.isConnected) target.focus();
    });
  }
  [els.presenceDisplayName, els.presenceRole].forEach(function (el) {
    if (!el) return;
    el.addEventListener('input', function () {
      if (els.presenceDetailSave) els.presenceDetailSave.disabled = false;
    });
    el.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); savePresenceDetail(); }
    });
  });
  if (els.presenceDetailSave) els.presenceDetailSave.addEventListener('click', savePresenceDetail);
  if (els.presenceHiddenDetailToggle) {
    els.presenceHiddenDetailToggle.addEventListener('click', togglePresenceHidden);
  }
}
