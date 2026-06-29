/* Home Automation — Home-tab weather tile.
 *
 * Polls GET /api/weather at a slow cadence and fills the Home-tab weather
 * strip: current weather (icon + temp) · today's forecast (min/max + a forecast
 * icon). Hidden until the first successful read; fails quietly like the energy
 * tile (weather is decorative, never load-bearing). The clock was dropped — it
 * just duplicated the phone's status-bar clock (issue #72). */

'use strict';

import { els } from './state.js';
import { jsonApi } from './api.js';
import { icon } from './icons.js';

const WEATHER_MS = 600_000;  // 10 min — weather barely moves

// WMO weather-code → Lucide glyph name. Day/night split only where it reads
// differently. https://open-meteo.com/en/docs (WMO Weather interpretation codes)
function weatherIcon(code, isDay) {
  if (code === 0) return isDay ? 'sun' : 'moon';                 // clear
  if (code === 1 || code === 2) return isDay ? 'cloud-sun' : 'cloud-moon'; // mainly/partly clear
  if (code === 3) return 'cloud';                                // overcast
  if (code === 45 || code === 48) return 'cloud-fog';            // fog
  if (code >= 51 && code <= 57) return 'cloud-drizzle';          // drizzle
  if (code >= 61 && code <= 67) return 'cloud-rain';             // rain
  if (code >= 71 && code <= 77) return 'cloud-snow';             // snow
  if (code >= 80 && code <= 82) return 'cloud-rain';             // rain showers
  if (code === 85 || code === 86) return 'cloud-snow';           // snow showers
  if (code >= 95) return 'cloud-lightning';                      // thunderstorm
  return 'thermometer';                                          // fallback
}

function fmtTemp(v) {
  return v == null || v === '' ? '—' : Math.round(Number(v)) + '°';
}

function render(w) {
  if (!w || !w.available) return;  // stay hidden, keep last value

  // Location label — shown when the API returns a non-empty label string.
  if (w.label) {
    els.wxLocation.textContent = w.label + ':';
    els.wxLocation.hidden = false;
  }

  els.wxNowIcon.innerHTML = icon(weatherIcon(Number(w.weather_code), w.is_day !== false));
  els.wxNowTemp.textContent = fmtTemp(w.temperature_c);

  // Today's forecast — daytime icon (the forecast describes the day) + min/max.
  els.wxFcIcon.innerHTML =
    w.forecast_code == null ? '—' : icon(weatherIcon(Number(w.forecast_code), true));
  els.wxFcMin.textContent = fmtTemp(w.temp_min_c);
  els.wxFcMax.textContent = fmtTemp(w.temp_max_c);

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
}
