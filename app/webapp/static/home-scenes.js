/* The Home tab's Scenes card: leave, arrive and night, one row each.
 *
 * A scene moves several device families at once (the blinds, the AC, the
 * alarm), so a tap asks once, naming every step, before it runs. The rows
 * are in the markup at first paint; this module fills each row's steps line
 * from GET /api/scenes and runs a scene with POST /api/scenes/{id}.
 *
 * The answer reports each step. A step that only partly took (one blind out
 * of five did not answer) is still a success with a warning, named in the
 * toast; a scene where nothing took is a 502 and an error toast.
 */

'use strict';

import { toast } from './state.js';
import { jsonApi, reportActionFailure } from './api.js';
import { confirmAction } from './confirm.js';

const STEP_NAMES = { blinds: 'blinds', ac: 'AC', alarm: 'alarm' };

// id -> { label, steps: [phrase] }, from the last GET /api/scenes.
const scenes = new Map();
let running = false;

function sceneButtons() {
  return Array.prototype.slice.call(document.querySelectorAll('#scenesCard .scene-run'));
}

function renderScenes() {
  sceneButtons().forEach(function (btn) {
    const scene = scenes.get(btn.dataset.scene);
    if (!scene) return;
    btn.querySelector('.scene-label').textContent = scene.label;
    btn.querySelector('.scene-steps').textContent = scene.steps.length ? scene.steps.join(' · ') : 'Does nothing yet';
  });
}

async function loadScenes() {
  try {
    const body = await jsonApi('/api/scenes');
    ((body && body.scenes) || []).forEach(function (s) { scenes.set(s.id, s); });
    renderScenes();
  } catch (exc) {
    // The rows keep their first-paint text; a run still works without it.
    console.warn('scenes failed to load', exc);
  }
}

function resultMessage(label, body) {
  const partial = ((body && body.steps) || []).filter(function (s) { return s.error; });
  if (!partial.length) return { text: label + ' done', tone: 'success' };
  const names = partial.map(function (s) { return STEP_NAMES[s.kind] || s.kind; });
  return { text: label + ' done, except ' + names.join(' and ') + ' — check them', tone: 'error' };
}

async function runScene(btn) {
  if (running) return;
  const id = btn.dataset.scene;
  const scene = scenes.get(id);
  const label = scene ? scene.label : btn.querySelector('.scene-label').textContent;
  const steps = scene ? scene.steps : [];
  const ok = await confirmAction({
    title: 'Run ' + label + '?',
    message: steps.length ? steps.join(' · ') : 'This scene has no steps.',
    okLabel: 'Run',
  });
  if (!ok) return;
  running = true;
  sceneButtons().forEach(function (b) { b.disabled = true; });
  try {
    toast('Running ' + label + '…', 'pending');
    const body = await jsonApi('/api/scenes/' + encodeURIComponent(id), { method: 'POST', timeoutMs: 60000 });
    const msg = resultMessage(label, body);
    toast(msg.text, msg.tone);
  } catch (exc) {
    reportActionFailure(exc, label + ' failed');
  } finally {
    running = false;
    sceneButtons().forEach(function (b) { b.disabled = false; });
  }
}

export function wireHomeScenes() {
  const buttons = sceneButtons();
  if (!buttons.length) return;
  buttons.forEach(function (btn) {
    btn.addEventListener('click', function () { runScene(btn); });
  });
  loadScenes();
}
