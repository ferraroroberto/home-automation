/* Presence — Web Push enrolment (split out of ./presence.js, issue #454
 * maintainability split).
 *
 * Owns the browser-side VAPID subscribe flow: GET /api/push/config for the
 * public key, service-worker registration, PushManager subscribe, and POSTing
 * the subscription to /api/push/subscriptions.
 */

'use strict';

import { els, toast } from './state.js';
import { jsonApi } from './api.js';

function base64UrlToUint8Array(value) {
  const padding = '='.repeat((4 - value.length % 4) % 4);
  const base64 = (value + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = window.atob(base64);
  const out = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) out[i] = raw.charCodeAt(i);
  return out;
}

async function subscribePush() {
  if (!('serviceWorker' in navigator) || !('PushManager' in window)) {
    toast('Notifications unavailable in this browser', 'error');
    return;
  }
  try {
    const cfg = await jsonApi('/api/push/config');
    if (!cfg.available || !cfg.public_key) {
      toast('Web Push keys are not configured', 'error');
      return;
    }
    const registration = await navigator.serviceWorker.register('/static/sw.js');
    const existing = await registration.pushManager.getSubscription();
    const sub = existing || await registration.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: base64UrlToUint8Array(cfg.public_key),
    });
    await jsonApi('/api/push/subscriptions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(sub.toJSON()),
    });
    toast('Notifications enabled', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Notifications failed: ' + (exc.message || exc), 'error');
    }
  }
}

export function wirePresencePushControls() {
  if (els.pushSubscribe) els.pushSubscribe.addEventListener('click', subscribePush);
}
