/* Home Assistant Voice PE card (#239).
 *
 * The disclosure consumes the existing HA VM tile and adds HA-owned room names,
 * satellite state/volume, recent Assist interactions, and per-room push-to-talk.
 * Dictation mirrors App Launcher's proven flow: one-second MediaRecorder chunks,
 * ordered proxy uploads to Voice Transcriber, live SSE partials, then canonical
 * finish + assist_satellite.announce. Only one mic may record at a time.
 */

'use strict';

import { api, jsonApi } from './api.js';
import { icon } from './_vendored/icons/icons.js';
import { createPoller } from './poll.js';
import { els, readToken, state, toast } from './state.js';
import { esc } from './format.js';

const POLL_MS = 15_000;
const CHUNK_MS = 1_000;
const MAX_QUEUED_CHUNKS = 8;

let activeTab = state.tab;
let activeDictation = null;
let haViewState = 'idle';

function pickAudioMime() {
  const candidates = [
    'audio/webm;codecs=opus', 'audio/webm',
    'audio/mp4;codecs=mp4a.40.2', 'audio/mp4',
  ];
  if (!window.MediaRecorder || !MediaRecorder.isTypeSupported) return '';
  for (const mime of candidates) {
    if (MediaRecorder.isTypeSupported(mime)) return mime;
  }
  return '';
}

function fmtVolume(value) {
  if (value == null || Number.isNaN(Number(value))) return 'Volume —';
  return 'Volume ' + Math.round(Number(value) * 100) + '%';
}

function fmtInteractionTime(value) {
  if (!value) return '';
  const date = typeof value === 'number' ? new Date(value * 1000) : new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function renderInteractions(rows) {
  els.haInteractionsList.innerHTML = '';
  els.haInteractionsNote.hidden = rows.length > 0;
  for (const interaction of rows) {
    const item = document.createElement('div');
    item.className = 'ha-interaction-row';
    const title = interaction.room || interaction.satellite_id || 'Home Assistant';
    const transcript = interaction.transcript || 'No transcript';
    const response = interaction.spoken_response || '';
    item.innerHTML =
      '<div class="ha-interaction-main">' +
      '  <span class="ha-interaction-room">' + esc(title) + '</span>' +
      '  <span class="muted small">' + esc(fmtInteractionTime(interaction.timestamp)) + '</span>' +
      '</div>' +
      '<p class="ha-interaction-text"></p>' +
      (response && response !== transcript ? '<p class="ha-interaction-response muted small"></p>' : '') +
      '<p class="ha-interaction-meta muted small"></p>';
    item.querySelector('.ha-interaction-text').textContent = transcript;
    const responseEl = item.querySelector('.ha-interaction-response');
    if (responseEl) responseEl.textContent = 'Response: ' + response;
    item.querySelector('.ha-interaction-meta').textContent =
      [interaction.intent_kind, interaction.intent, interaction.action].filter(Boolean).join(' · ');
    els.haInteractionsList.appendChild(item);
  }
}

function micButtonHtml(satellite) {
  const transcriberReady = !!(state.ha && state.ha.voice_transcriber);
  const disabled = !satellite.online || !window.MediaRecorder || !transcriberReady;
  const label = !satellite.online
    ? 'Satellite offline'
    : (!transcriberReady
        ? 'Voice Transcriber is not configured'
        : (!window.MediaRecorder ? 'Microphone unsupported' : 'Start microphone in ' + satellite.room));
  return '<button type="button" class="ha-mic-btn" data-entity="' + esc(satellite.entity_id) + '"' +
    (disabled ? ' disabled' : '') + ' aria-pressed="false" aria-label="' + esc(label) +
    '" title="' + esc(label) + '">' + icon('mic') + '</button>';
}

function renderSatellites(rows) {
  els.haSatellitesList.innerHTML = '';
  els.haSatellitesNote.hidden = rows.length > 0;
  if (!rows.length) {
    els.haSatellitesNote.textContent = haViewState === 'error'
      ? 'Home Assistant is offline or unavailable.'
      : 'No Assist satellites found in Home Assistant.';
    return;
  }
  for (const satellite of rows) {
    const row = document.createElement('div');
    row.className = 'ha-satellite-row' + (satellite.online ? '' : ' is-offline');
    row.dataset.entity = satellite.entity_id;
    row.innerHTML =
      '<div class="ha-satellite-copy">' +
      '  <div class="ha-satellite-title"><strong>' + esc(satellite.room) + '</strong>' +
      '    <span class="ha-satellite-state ' + (satellite.online ? 'is-online' : 'is-offline') + '">' +
             esc(satellite.online ? satellite.state : 'offline') + '</span></div>' +
      '  <div class="muted small">' + esc(satellite.name) + ' · ' + esc(fmtVolume(satellite.volume)) + '</div>' +
      '  <p class="ha-live-transcript muted small" aria-live="polite"></p>' +
      '</div>' + micButtonHtml(satellite);
    const button = row.querySelector('.ha-mic-btn');
    if (button && !button.disabled) {
      button.addEventListener('click', function () { toggleDictation(satellite, row, button); });
    }
    els.haSatellitesList.appendChild(row);
  }
}

function renderHa() {
  const data = state.ha || { satellites: [], interactions: [] };
  renderSatellites(data.satellites || []);
  renderInteractions(data.interactions || []);
}

async function loadHa() {
  if (!els.homeAssistantCard || !els.homeAssistantCard.open || activeTab !== 'home') return;
  if (!state.ha) {
    haViewState = 'loading';
    els.haSatellitesNote.hidden = false;
    els.haSatellitesNote.textContent = 'Reading Home Assistant rooms…';
  }
  try {
    state.ha = await jsonApi('/api/ha');
    haViewState = 'ready';
    renderHa();
  } catch (exc) {
    if (String(exc.message) === 'auth required') return;
    haViewState = 'error';
    renderSatellites([]);
    toast((exc && exc.message) || 'Home Assistant unavailable', 'error');
  }
}

const schedule = createPoller(loadHa);

function updatePolling() {
  const enabled = activeTab === 'home' && els.homeAssistantCard && els.homeAssistantCard.open;
  if (enabled) {
    loadHa();
    schedule(POLL_MS);
  } else {
    schedule(0);
  }
}

export function onHaTab(tab) {
  activeTab = tab;
  updatePolling();
}

export function wireHa() {
  if (!els.homeAssistantCard) return;
  els.homeAssistantCard.addEventListener('toggle', updatePolling);
}

function voiceEventUrl(sessionId) {
  const token = readToken();
  const query = token ? '?token=' + encodeURIComponent(token) : '';
  return '/api/ha/transcribe/sessions/' + encodeURIComponent(sessionId) + '/events' + query;
}

async function announceFinal(satellite, transcript, transcriptEl) {
  const text = String(transcript || '').trim();
  if (!text) {
    transcriptEl.textContent = 'Nothing heard — silent recording.';
    toast('Nothing heard — silent recording', 'error');
    return;
  }
  transcriptEl.textContent = text + ' · Sending to ' + satellite.room + '…';
  await jsonApi('/api/ha/satellites/' + encodeURIComponent(satellite.entity_id) + '/announce', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text: text }),
  });
  transcriptEl.textContent = text + ' · Announced';
  toast('Announced in ' + satellite.room, 'success');
  // Reflect the successful interaction immediately without re-rendering the
  // satellite row (which would erase the visible final transcript). The next
  // 15-second poll replaces this optimistic row with SQLite's canonical event.
  if (state.ha) {
    state.ha.interactions = state.ha.interactions || [];
    state.ha.interactions.unshift({
      timestamp: new Date().toISOString(),
      room: satellite.room,
      satellite_id: satellite.entity_id,
      transcript: text,
      intent_kind: 'direct',
      intent: 'assist_satellite.announce',
      action: 'assist_satellite.announce',
      spoken_response: text,
    });
    renderInteractions(state.ha.interactions.slice(0, 12));
  }
}

async function toggleDictation(satellite, row, button) {
  if (activeDictation && activeDictation.button === button) {
    activeDictation.stop();
    return;
  }
  if (activeDictation) {
    toast('Another room microphone is already recording', 'error');
    return;
  }
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia || !window.MediaRecorder) {
    toast('Microphone recording is not supported in this browser', 'error');
    return;
  }

  const transcriptEl = row.querySelector('.ha-live-transcript');
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (exc) {
    toast('Microphone unavailable: ' + (exc.message || exc), 'error');
    return;
  }
  const mime = pickAudioMime();
  let recorder;
  try {
    recorder = mime ? new MediaRecorder(stream, { mimeType: mime }) : new MediaRecorder(stream);
  } catch (exc) {
    stream.getTracks().forEach(function (track) { track.stop(); });
    toast('Recorder failed: ' + (exc.message || exc), 'error');
    return;
  }

  let sessionId = null;
  let eventSource = null;
  let streaming = false;
  let queue = [];
  let drainPromise = null;
  let buffered = [];
  let overload = false;

  async function drain() {
    if (drainPromise) return drainPromise;
    drainPromise = (async function () {
      try {
        while (queue.length && sessionId) {
          const blob = queue.shift();
          const response = await api(
            '/api/ha/transcribe/sessions/' + encodeURIComponent(sessionId) + '/chunk',
            { method: 'POST', body: blob, timeoutMs: 30_000 }
          );
          if (!response.ok) throw new Error('Audio stream failed (HTTP ' + response.status + ')');
        }
      } finally {
        drainPromise = null;
      }
    })();
    return drainPromise;
  }

  try {
    const created = await jsonApi('/api/ha/transcribe/sessions', { method: 'POST' });
    sessionId = created && created.session_id;
    streaming = !!sessionId;
  } catch (_) {
    streaming = false;
  }

  if (streaming) {
    eventSource = new EventSource(voiceEventUrl(sessionId));
    eventSource.addEventListener('partial', function (event) {
      try {
        const data = JSON.parse(event.data);
        if (typeof data.transcript === 'string') transcriptEl.textContent = data.transcript;
      } catch (_) { /* malformed upstream event is ignored */ }
    });
  }

  recorder.addEventListener('dataavailable', function (event) {
    if (!event.data || !event.data.size) return;
    if (!streaming) {
      buffered.push(event.data);
      return;
    }
    if (queue.length >= MAX_QUEUED_CHUNKS) {
      overload = true;
      transcriptEl.textContent = 'Transcription could not keep up — stopping safely…';
      try { recorder.stop(); } catch (_) { /* stop handler still owns cleanup */ }
      return;
    }
    queue.push(event.data);
    drain().catch(function (exc) {
      overload = true;
      transcriptEl.textContent = exc.message || 'Audio stream failed';
      try { recorder.stop(); } catch (_) { /* cleanup below */ }
    });
  });

  recorder.addEventListener('stop', async function () {
    stream.getTracks().forEach(function (track) { track.stop(); });
    button.classList.remove('recording');
    button.setAttribute('aria-pressed', 'false');
    button.innerHTML = icon('mic');
    button.disabled = true;
    try {
      let result;
      if (streaming) {
        await drain();
        result = await jsonApi(
          '/api/ha/transcribe/sessions/' + encodeURIComponent(sessionId) + '/finish',
          { method: 'POST', timeoutMs: 90_000 }
        );
      } else {
        const blob = new Blob(buffered, { type: recorder.mimeType || mime || 'audio/webm' });
        const form = new FormData();
        const extension = blob.type.indexOf('mp4') >= 0 ? 'mp4' : 'webm';
        form.append('file', blob, 'recording.' + extension);
        const response = await api('/api/ha/transcribe', {
          method: 'POST', body: form, timeoutMs: 90_000,
        });
        result = await response.json().catch(function () { return null; });
        if (!response.ok) throw new Error((result && result.detail) || 'Transcription failed');
      }
      if (result && result.silent) {
        await announceFinal(satellite, '', transcriptEl);
      } else if (!overload) {
        await announceFinal(satellite, result && result.transcript, transcriptEl);
      }
    } catch (exc) {
      transcriptEl.textContent = exc.message || 'Transcription failed';
      toast(exc.message || 'Transcription failed', 'error');
    } finally {
      if (eventSource) eventSource.close();
      buffered = [];
      queue = [];
      activeDictation = null;
      button.disabled = !satellite.online;
    }
  });

  recorder.start(streaming ? CHUNK_MS : undefined);
  button.classList.add('recording');
  button.setAttribute('aria-pressed', 'true');
  button.setAttribute('aria-label', 'Stop microphone in ' + satellite.room);
  button.innerHTML = icon('square');
  transcriptEl.textContent = 'Listening…';
  activeDictation = {
    button: button,
    stop: function () {
      if (recorder.state !== 'inactive') recorder.stop();
    },
  };
}
