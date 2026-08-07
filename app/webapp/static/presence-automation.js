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
import { jsonApi, reportActionFailure } from './api.js';
import { setToggleState, isToggleOn, wireToggle } from './toggle.js';
import { loadPresence, presenceById, presenceEntityLabel } from './presence.js';

export function renderKidsHomeToggle(viewReady) {
  if (!els.presenceKidsHome) return;
  const on = !!(state.presence && state.presence.kids_home_override);
  els.presenceKidsHome.classList.toggle('active', on);
  els.presenceKidsHome.setAttribute('aria-pressed', on ? 'true' : 'false');
  els.presenceKidsHome.disabled = !viewReady;
}

export function renderPresenceAutomationNote() {
  if (!els.presenceAutomationNote || !els.presenceAutoEnabled || !els.presenceDisarmOnArrival) return;
  const entities = (state.presence && state.presence.entities) || [];
  const hasWebhookPerson = entities.some(function (entity) {
    return entity.source === 'webhook' && !entity.hidden;
  });
  const anyAutomationOn = isToggleOn(els.presenceAutoEnabled) || isToggleOn(els.presenceDisarmOnArrival);
  const auto = state.presenceAutomation || {};
  const blockedIds = Array.isArray(auto.arm_blocked_person_ids) ? auto.arm_blocked_person_ids : [];
  if (anyAutomationOn && !hasWebhookPerson) {
    els.presenceAutomationNote.textContent = 'Configure iOS Shortcut arrive/leave webhooks before enabling alarm automation. Browser GPS and Find My diagnostics do not drive arm/disarm.';
    els.presenceAutomationNote.hidden = false;
  } else if (auto.arm_blocked && blockedIds.length) {
    // #531: someone else left, but auto-arm is waiting on these people's
    // presence to flip to "away" - surfaces the block instead of it looking
    // like the feature silently isn't working.
    const names = blockedIds.map(function (id) {
      const entity = presenceById(id);
      return entity ? presenceEntityLabel(entity) : id;
    }).join(', ');
    els.presenceAutomationNote.textContent = 'Auto-arm not active: ' + names + ' still reported home.';
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
    reportActionFailure(exc, 'Kids home toggle failed');
  }
}

export async function loadPresenceAutomation() {
  if (!els.presenceAutoEnabled) return;
  try {
    state.presenceAutomation = await jsonApi('/api/presence/automation');
    const cfg = state.presenceAutomation || {};
    setToggleState(els.presenceAutoEnabled, cfg.auto_arm_enabled === true);
    els.presenceArmMinutes.value = Math.round((Number(cfg.arm_away_after_s) || 0) / 60);
    els.presenceStaleMinutes.value = Math.round((Number(cfg.stale_after_s) || 3600) / 60);
    setToggleState(els.presenceDisarmOnArrival, cfg.auto_disarm_enabled === true);
    renderPresenceAutomationNote();
  } catch (exc) {
    reportActionFailure(exc, 'Automation settings failed');
  }
}

async function savePresenceAutomation() {
  const payload = {
    auto_arm_enabled: isToggleOn(els.presenceAutoEnabled),
    arm_away_after_s: Math.max(0, Number(els.presenceArmMinutes.value || 0)) * 60,
    stale_after_s: Math.max(1, Number(els.presenceStaleMinutes.value || 1)) * 60,
    auto_disarm_enabled: isToggleOn(els.presenceDisarmOnArrival),
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
    reportActionFailure(exc, 'Automation save failed');
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
