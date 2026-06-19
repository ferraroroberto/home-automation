/* Home Automation — header weather readout.
 *
 * Polls GET /api/weather at a slow cadence and shows a compact "🌤 21°C" in
 * the header. Hidden until the first successful read; fails quietly like the
 * energy tile (weather is decorative, never load-bearing). */

'use strict';

import { els } from './state.js';
import { jsonApi } from './api.js';

const WEATHER_MS = 600_000;  // 10 min — weather barely moves

// WMO weather-code → emoji. Day/night split only where it reads differently.
// https://open-meteo.com/en/docs (WMO Weather interpretation codes)
function weatherIcon(code, isDay) {
  if (code === 0) return isDay ? '☀️' : '🌙';            // clear
  if (code === 1 || code === 2) return isDay ? '🌤' : '☁️'; // mainly/partly clear
  if (code === 3) return '☁️';                            // overcast
  if (code === 45 || code === 48) return '🌫';            // fog
  if (code >= 51 && code <= 57) return '🌦';              // drizzle
  if (code >= 61 && code <= 67) return '🌧';              // rain
  if (code >= 71 && code <= 77) return '🌨';              // snow
  if (code >= 80 && code <= 82) return '🌧';              // rain showers
  if (code === 85 || code === 86) return '🌨';            // snow showers
  if (code >= 95) return '⛈';                             // thunderstorm
  return '🌡';                                            // fallback
}

function render(w) {
  if (!w || !w.available) return;  // stay hidden, keep last value
  const icon = weatherIcon(Number(w.weather_code), w.is_day !== false);
  const temp = Math.round(Number(w.temperature_c));
  els.weather.textContent = icon + ' ' + temp + '°';
  els.weather.hidden = false;
}

async function loadWeather() {
  try {
    const body = await jsonApi('/api/weather');
    render(body);
  } catch (_) {
    // Weather is decorative — fail quietly, keep the readout as-is.
  }
}

export function startWeatherPolling() {
  loadWeather();
  setInterval(loadWeather, WEATHER_MS);
}
