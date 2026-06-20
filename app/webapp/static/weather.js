/* Home Automation — Home-tab weather tile.
 *
 * Polls GET /api/weather at a slow cadence and fills the Home-tab weather
 * strip: current time · current weather (icon + temp) · today's forecast
 * (min/max + a forecast icon). Hidden until the first successful read; fails
 * quietly like the energy tile (weather is decorative, never load-bearing).
 * The clock ticks client-side once a minute, independent of the weather poll. */

'use strict';

import { els } from './state.js';
import { jsonApi } from './api.js';

const WEATHER_MS = 600_000;  // 10 min — weather barely moves
const CLOCK_MS = 30_000;     // re-stamp the clock twice a minute

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

function fmtTemp(v) {
  return v == null || v === '' ? '—' : Math.round(Number(v)) + '°';
}

// Local HH:MM in the browser's timezone (24-hour, zero-padded).
function nowHHMM() {
  const d = new Date();
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return hh + ':' + mm;
}

function tickClock() {
  els.wxTime.textContent = nowHHMM();
}

function render(w) {
  if (!w || !w.available) return;  // stay hidden, keep last value

  // Current weather — icon + temperature, plus the location label.
  els.wxNowIcon.textContent = weatherIcon(Number(w.weather_code), w.is_day !== false);
  els.wxNowTemp.textContent = fmtTemp(w.temperature_c);
  els.wxLoc.textContent = (w.label || '').trim();

  // Today's forecast — daytime icon (the forecast describes the day) + min/max.
  els.wxFcIcon.textContent =
    w.forecast_code == null ? '—' : weatherIcon(Number(w.forecast_code), true);
  els.wxFcMin.textContent = fmtTemp(w.temp_min_c);
  els.wxFcMax.textContent = fmtTemp(w.temp_max_c);

  tickClock();
  els.weatherTile.hidden = false;
}

async function loadWeather() {
  try {
    const body = await jsonApi('/api/weather');
    render(body);
  } catch (_) {
    // Weather is decorative — fail quietly, keep the tile as-is.
  }
}

export function startWeatherPolling() {
  loadWeather();
  setInterval(loadWeather, WEATHER_MS);
  setInterval(tickClock, CLOCK_MS);
}
