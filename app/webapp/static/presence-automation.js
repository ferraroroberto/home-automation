/* Presence — alarm-automation knobs (split out of ./presence.js, issue #454
 * maintainability split).
 *
 * Owns GET/PUT /api/presence/automation (arm-away delay, stale threshold,
 * disarm-on-arrival) and the "kids home" override (PUT
 * /api/presence/kids_home_override) that makes the everyone-away webhook arm
 * perimeter instead of full. Calls back into ./presence.js's loadPresence()
 * after a kids-home write so the card reflects the new override immediately.
 */

'use strict';

import { state, els, toast } from './state.js';
import { jsonApi } from './api.js';
import { setToggleState, isToggleOn, wireToggle } from './toggle.js';
import { loadPresence } from './presence.js';

export function renderKidsHomeToggle(viewReady) {
  if (!els.presenceKidsHome) return;
  const on = !!(state.presence && state.presence.kids_home_override);
  els.presenceKidsHome.classList.toggle('active', on);
  els.presenceKidsHome.setAttribute('aria-pressed', on ? 'true' : 'false');
  els.presenceKidsHome.disabled = !viewReady;
}

export function renderPresenceAutomationNote() {
  if (!els.presenceAutomationNote || !els.presenceAutoEnabled) return;
  const entities = (state.presence && state.presence.entities) || [];
  const hasWebhookPerson = entities.some(function (entity) {
    return entity.source === 'webhook' && !entity.hidden;
  });
  if (isToggleOn(els.presenceAutoEnabled) && !hasWebhookPerson) {
    els.presenceAutomationNote.textContent = 'Configure iOS Shortcut arrive/leave webhooks before enabling alarm automation. Browser GPS and Find My diagnostics do not drive arm/disarm.';
    els.presenceAutomationNote.hidden = false;
  } else {
    els.presenceAutomationNote.hidden = true;
    els.presenceAutomationNote.textContent = '';
  }
}

// "Kids home" override: when on, the everyone-away webhook arms perimeter
// instead of full. Auto-resets server-side on the next disarm-on-arrival.
async function toggleKidsHome() {
  const next = !(state.presence && state.presence.kids_home_override);
  try {
    await jsonApi('/api/presence/kids_home_override', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ active: next }),
    });
    await loadPresence();
    toast(next ? 'Kids home on · perimeter when away' : 'Kids home off', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Kids home toggle failed: ' + (exc.message || exc), 'error');
    }
  }
}

export async function loadPresenceAutomation() {
  if (!els.presenceAutoEnabled) return;
  try {
    state.presenceAutomation = await jsonApi('/api/presence/automation');
    const cfg = state.presenceAutomation || {};
    setToggleState(els.presenceAutoEnabled, cfg.enabled === true);
    els.presenceArmMinutes.value = Math.round((Number(cfg.arm_away_after_s) || 0) / 60);
    els.presenceStaleMinutes.value = Math.round((Number(cfg.stale_after_s) || 3600) / 60);
    setToggleState(els.presenceDisarmOnArrival, cfg.disarm_on_arrival !== false);
    renderPresenceAutomationNote();
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Automation settings failed: ' + (exc.message || exc), 'error');
    }
  }
}

async function savePresenceAutomation() {
  const payload = {
    enabled: isToggleOn(els.presenceAutoEnabled),
    arm_away_after_s: Math.max(0, Number(els.presenceArmMinutes.value || 0)) * 60,
    stale_after_s: Math.max(1, Number(els.presenceStaleMinutes.value || 1)) * 60,
    disarm_on_arrival: isToggleOn(els.presenceDisarmOnArrival),
  };
  try {
    state.presenceAutomation = await jsonApi('/api/presence/automation', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    renderPresenceAutomationNote();
    toast('Automation saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Automation save failed: ' + (exc.message || exc), 'error');
    }
  }
}

export function wirePresenceAutomationControls() {
  if (els.presenceKidsHome) {
    // The button lives in the <summary>, so swallow the click to toggle the
    // override instead of collapsing the card.
    els.presenceKidsHome.addEventListener('click', function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      toggleKidsHome();
    });
  }
  [els.presenceArmMinutes, els.presenceStaleMinutes].forEach(function (el) {
    if (el) el.addEventListener('change', savePresenceAutomation);
  });
  wireToggle(els.presenceAutoEnabled, savePresenceAutomation);
  wireToggle(els.presenceDisarmOnArrival, savePresenceAutomation);
}
