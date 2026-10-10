/* Home Automation — the Home header's weather line.
 *
 * Polls GET /api/weather at a slow cadence. Since #885 (decision 2 of #872)
 * the weather is no card: it is the Home page header's live line, the one
 * header where the quiet line may be a plain fact rather than a count. The
 * line is the sky now and today's range ("Clear 21° · 9° / 21°"); the
 * accessible name adds today's forecast sky and the place it is for (the
 * home location, set in Settings). Fails quietly and keeps the last value:
 * weather is decorative, never load-bearing. The clock was dropped long ago:
 * it just duplicated the phone's status-bar clock (issue #72). */

'use strict';

import { jsonApi } from './api.js';
import { setHeadPart } from './head-status.js';

const WEATHER_MS = 600_000;  // 10 min — weather barely moves

// WMO weather code → one short word, short enough for the header line at
// 390px. https://open-meteo.com/en/docs (WMO Weather interpretation codes)
function weatherWord(code) {
  if (code === 0) return 'Clear';
  if (code === 1 || code === 2) return 'Clouds';
  if (code === 3) return 'Overcast';
  if (code === 45 || code === 48) return 'Fog';
  if (code >= 51 && code <= 57) return 'Drizzle';
  if (code >= 61 && code <= 67) return 'Rain';
  if ((code >= 71 && code <= 77) || code === 85 || code === 86) return 'Snow';
  if (code >= 80 && code <= 82) return 'Showers';
  if (code >= 95) return 'Storm';
  return '';
}

function fmtTemp(v) {
  return v == null || v === '' ? '—' : Math.round(Number(v)) + '°';
}

function render(w) {
  if (!w || !w.available) return;  // keep the last value
  const now = [weatherWord(Number(w.weather_code)), fmtTemp(w.temperature_c)].filter(Boolean).join(' ');
  const range = fmtTemp(w.temp_min_c) + ' / ' + fmtTemp(w.temp_max_c);
  setHeadPart('home', 'weather', { fact: now + ' · ' + range });

  const el = document.querySelector('.home-head .status[data-head="home"]');
  if (!el) return;
  const today = w.forecast_code == null ? '' : weatherWord(Number(w.forecast_code));
  const label = 'Weather' + (w.label ? ' at ' + w.label : '') + ': now ' + now +
    '; today ' + (today ? today.toLowerCase() + ', ' : '') + fmtTemp(w.temp_min_c) + ' to ' + fmtTemp(w.temp_max_c);
  el.setAttribute('aria-label', label);
  el.title = label;
}

async function loadWeather() {
  try {
    const body = await jsonApi('/api/weather');
    render(body);
  } catch (_) {
    // Weather is decorative — fail quietly, keep the line as-is.
  }
}

export function startWeatherPolling() {
  loadWeather();
  setInterval(loadWeather, WEATHER_MS);
}
