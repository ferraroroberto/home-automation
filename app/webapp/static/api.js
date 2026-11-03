/* Home Automation — fetch helpers and the sign-in sheet trigger.
 *
 * `api()` attaches the bearer token and routes a 401 to the vendored
 * sign-in sheet; `jsonApi()` adds JSON parsing + error shaping on top.
 *
 * A 401 reaches callers as `Error('auth required')`, which the sign-in sheet has
 * already handled — so no call site may surface it as a failure.
 * `isAuthRequired()` is that test (never hand-roll the string compare) and
 * `reportActionFailure()` is the whole mutation-path idiom in one line: silent
 * on auth, otherwise one "<label>: <reason>" error toast.
 */

'use strict';

import { readToken, toast, writeToken } from './state.js';
import { requestSignIn } from './_vendored/sign-in/sign-in.js';
import { friendlyError } from './format.js';

const DEFAULT_TIMEOUT_MS = 30000;

// Opens the vendored sign-in sheet on a 401. The sheet hands every caller the
// same promise while it is open, so the follow-up runs once per sign-in, not
// once per failing request.
let onSignedIn = null;
let signInOpen = false;

export function showLogin() {
  if (signInOpen) return;
  signInOpen = true;
  requestSignIn({
    endpoint: '/api/login',
    hint: 'Enter the password to control your units.',
    onToken: writeToken,
  }).then(function () {
    signInOpen = false;
    if (onSignedIn) onSignedIn();
  }, function (exc) {
    signInOpen = false;
    console.warn('sign-in failed to open', exc);
  });
}

// Boot hook called from main.js, so this module stays independent of what a
// successful sign-in reloads.
export function setSignInHandler(handler) {
  onSignedIn = handler;
}

// True when `exc` is the sentinel `api()` throws on a 401. The sign-in sheet is
// already up, so the caller stays quiet instead of toasting a failure.
export function isAuthRequired(exc) {
  return String(exc && exc.message) === 'auth required';
}

// The mutation-path failure idiom: quiet on auth, one error toast otherwise.
// `label` names the action that failed ('Rename failed', 'Failed to save', …);
// the sanitized reason is appended (no hosts, addresses or exception
// text in the UI). A call site that also has cleanup to skip on auth keeps its
// own `if (!isAuthRequired(exc)) { … }` block instead.
export function reportActionFailure(exc, label) {
  if (isAuthRequired(exc)) return;
  toast(label + ': ' + friendlyError(exc), 'error');
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
