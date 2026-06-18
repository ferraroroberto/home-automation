/* Home Automation — fetch helpers and the login overlay.
 *
 * `api()` attaches the bearer token and routes a 401 to the login
 * overlay; `jsonApi()` adds JSON parsing + error shaping on top.
 */

'use strict';

import { els, readToken } from './state.js';

const DEFAULT_TIMEOUT_MS = 30000;

export function showLogin() {
  if (!els.loginOverlay) return;
  els.loginOverlay.hidden = false;
  els.loginPassword.value = '';
  els.loginPassword.focus();
}
export function hideLogin() {
  if (els.loginOverlay) els.loginOverlay.hidden = true;
}

export async function api(path, opts) {
  opts = opts || {};
  const headers = new Headers(opts.headers || {});
  const token = readToken();
  if (token) headers.set('Authorization', 'Bearer ' + token);

  const timeoutMs = opts.timeoutMs == null ? DEFAULT_TIMEOUT_MS : opts.timeoutMs;
  const controller = timeoutMs > 0 ? new AbortController() : null;
  let timeoutId = null;
  if (controller) {
    timeoutId = setTimeout(function () { controller.abort(); }, timeoutMs);
  }
  let res;
  try {
    res = await fetch(
      path,
      Object.assign({}, opts, {
        headers,
        signal: controller ? controller.signal : opts.signal,
      })
    );
  } catch (exc) {
    if (exc && exc.name === 'AbortError') {
      throw new Error('request timed out after ' + Math.round(timeoutMs / 1000) + ' s');
    }
    throw exc;
  } finally {
    if (timeoutId != null) clearTimeout(timeoutId);
  }
  if (res.status === 401) {
    showLogin();
    throw new Error('auth required');
  }
  return res;
}

export async function jsonApi(path, opts) {
  const res = await api(path, opts);
  let body = null;
  try {
    body = await res.json();
  } catch (_) {
    body = null;
  }
  if (!res.ok) {
    const detail = (body && body.detail) || ('HTTP ' + res.status);
    const err = new Error(detail);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return body;
}
