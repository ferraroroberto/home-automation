/* Chart.js wrappers for the Energy tab.
 *
 * Both charts use the same three all-positive series — nothing dips below zero,
 * so every line "goes up" and the translucent fills stack visually (SMA style):
 *
 *   • live line chart  — Generation / Grid-supplied / Consumption (W), recent
 *     samples. spanGaps:false so an asleep inverter (null generation) draws a
 *     gap, never a 0.
 *   • history area chart — the same three in energy (kWh) per calendar slot,
 *     for a fill-up Day / Week / Month / Year / Total window.
 *
 * All colours read from the design-system CSS custom properties (theme-aware via
 * restyle()): axes/legend from --ink/--muted/--line, and the series palette from
 * the status tokens so it matches the flow + cards:
 *   Generation = --on (success/green), Grid-supplied = --deficit (danger/red),
 *   Consumption = --muted (grey line).
 * Series colours are resolved gamut-safely via alphaFill() (issue #65), so the
 * P3 oklch layer in styles.css feeds --on/--deficit straight through — any CSS
 * color syntax (hex, oklch, rgb, named) works.
 *
 * Chart.js is loaded as a vendored UMD global (window.Chart) by index.html. */

'use strict';

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function palette() {
  return {
    ink: cssVar('--ink', '#1f2328'),
    muted: cssVar('--muted', '#656d76'),
    line: cssVar('--line', '#d1d9e0'),
    accent: cssVar('--accent', '#0969da'),
    attention: cssVar('--attention', '#9a6700'),
    gen: cssVar('--on', '#1a7f37'),
    grid: cssVar('--deficit', '#cf222e'),
  };
}

// Any CSS color string → rgba() at the given alpha, for translucent fills.
// Painting onto a throwaway 1×1 canvas lets the browser normalise any input
// syntax (hex, oklch, rgb, named) to sRGB bytes, so an oklch P3 token is safe
// here: the line itself renders the true wide-gamut color via CSS; only this
// alpha fill is the sRGB approximation (getImageData clamps to sRGB bytes).
let _fillCtx = null;
function alphaFill(color, a) {
  if (!_fillCtx) {
    const c = (typeof OffscreenCanvas !== 'undefined')
      ? new OffscreenCanvas(1, 1)
      : document.createElement('canvas');
    _fillCtx = c.getContext('2d', { willReadFrequently: true });
  }
  _fillCtx.clearRect(0, 0, 1, 1);
  _fillCtx.fillStyle = color;
  _fillCtx.fillRect(0, 0, 1, 1);
  const d = _fillCtx.getImageData(0, 0, 1, 1).data;
  return 'rgba(' + d[0] + ',' + d[1] + ',' + d[2] + ',' + a + ')';
}

function baseScales(pal, unit) {
  return {
    x: {
      ticks: { color: pal.muted, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
      grid: { display: false },
    },
    y: {
      beginAtZero: true,
      title: { display: true, text: unit, color: pal.muted },
      ticks: { color: pal.muted },
      grid: { color: pal.line },
    },
  };
}

function legend(pal) {
  return { labels: { color: pal.ink, boxWidth: 12, usePointStyle: true } };
}

// A translucent filled area (Generation, Grid-supplied).
function area(label, color) {
  return {
    label: label,
    data: [],
    borderColor: color,
    backgroundColor: alphaFill(color, 0.18),
    fill: 'origin',
  };
}

// A plain envelope line (Consumption) — colour passed in so it can be theme grey.
function envelope(label, color) {
  return {
    label: label,
    data: [],
    borderColor: color,
    backgroundColor: color,
    fill: false,
  };
}

function commonOptions(pal, unit, spanGaps) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    spanGaps: spanGaps,
    interaction: { mode: 'index', intersect: false },
    elements: { point: { radius: 0 }, line: { tension: 0.3, borderWidth: 2 } },
    plugins: { legend: legend(pal) },
    scales: baseScales(pal, unit),
  };
}

// ----------------------------------------------------------------- live
export function createLiveChart(canvas) {
  const pal = palette();
  return new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        area('Generation', pal.gen),
        area('Grid-supplied', pal.grid),
        envelope('Consumption', pal.muted),
      ],
    },
    // spanGaps:false — asleep generation should read as a gap, not a 0.
    options: commonOptions(pal, 'W', false),
  });
}

export function setLiveData(chart, samples) {
  chart.data.labels = samples.map(function (s) { return timeLabel(s.ts); });
  chart.data.datasets[0].data = samples.map(function (s) { return s.pv_power_w; });
  chart.data.datasets[1].data = samples.map(function (s) { return s.grid_import_w; });
  chart.data.datasets[2].data = samples.map(function (s) { return s.house_consumption_w; });
  chart.update('none');
}

export function pushLivePoint(chart, ts, gen, grid, cons, maxPoints) {
  chart.data.labels.push(timeLabel(ts));
  chart.data.datasets[0].data.push(gen);
  chart.data.datasets[1].data.push(grid);
  chart.data.datasets[2].data.push(cons);
  const cap = maxPoints || 360;
  while (chart.data.labels.length > cap) {
    chart.data.labels.shift();
    chart.data.datasets.forEach(function (d) { d.data.shift(); });
  }
  chart.update('none');
}

function timeLabel(tsSeconds) {
  return new Date(tsSeconds * 1000).toLocaleTimeString([], {
    hour: '2-digit', minute: '2-digit',
  });
}

// ------------------------------------------------------------ history
// The same filled Generation / Grid-supplied areas + a Consumption envelope as
// the live chart, but per calendar slot in kWh (#74 — reverted from the #72 bar
// experiment, which read as cluttered hourly bars on the Day view). A single-
// bucket range (the Σ Total, or Year with <1y of history) would draw an
// invisible 1-point line, so setAggData() turns the point markers on in that
// one case — see there.
export function createAggChart(canvas) {
  const pal = palette();
  return new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        area('Generation', pal.gen),
        area('Grid-supplied', pal.grid),
        envelope('Consumption', pal.muted),
      ],
    },
    options: commonOptions(pal, 'kWh', true),
  });
}

function kwh(wh) { return (Number(wh) || 0) / 1000; }

export function setAggData(chart, buckets) {
  chart.data.labels = buckets.map(function (b) { return b.label; });
  chart.data.datasets[0].data = buckets.map(function (b) { return kwh(b.pv_wh); });
  chart.data.datasets[1].data = buckets.map(function (b) { return kwh(b.import_wh); });
  chart.data.datasets[2].data = buckets.map(function (b) { return kwh(b.house_wh); });
  // A line through a single point is invisible (pointRadius is 0 everywhere
  // else), so the Σ Total — and any range that resolves to one bucket — would
  // read as empty. Show the markers only in that case so the value is visible.
  const single = buckets.length <= 1;
  chart.data.datasets.forEach(function (d) { d.pointRadius = single ? 4 : 0; });
  chart.update('none');
}

// ------------------------------------------------------------ forecast
// Expected generation is a dashed --muted line: it is an *estimate*, not a
// measured state, so it stays neutral grey (status colours signal state only).
// The day's actual generation is overlaid as the usual filled --on area, so the
// two read distinctly. spanGaps:false so an asleep / not-yet-sampled hour in the
// actual series draws a gap, never a misleading 0.
export function createForecastChart(canvas) {
  const pal = palette();
  const expected = envelope('Expected', pal.muted);
  expected.borderDash = [6, 4];
  return new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        expected,
        area('Actual', pal.gen),
      ],
    },
    options: commonOptions(pal, 'kWh', false),
  });
}

// Fixed 24-hour x axis ("00".."23"), so expected and actual align by hour.
function hourLabels() {
  const out = [];
  for (let h = 0; h < 24; h++) out.push(h < 10 ? '0' + h : '' + h);
  return out;
}

export function setForecastData(chart, expected, actual) {
  const expMap = {};
  (expected || []).forEach(function (p) { expMap[p.hour] = p.wh; });
  const hasActual = Array.isArray(actual);
  const actMap = {};
  if (hasActual) actual.forEach(function (p) { actMap[p.hour] = p.wh; });

  const labels = hourLabels();
  chart.data.labels = labels;
  chart.data.datasets[0].data = labels.map(function (_, h) {
    return h in expMap ? kwh(expMap[h]) : 0;
  });
  // No actuals (tomorrow) → empty series, so only the dashed forecast draws.
  chart.data.datasets[1].data = hasActual
    ? labels.map(function (_, h) {
        const v = actMap[h];
        return v == null ? null : kwh(v);   // null hour → gap (asleep / no sample)
      })
    : [];
  chart.update('none');
}

// ---------------------------------------------------------- Wi-Fi channels
function wifiColor(pal, i) {
  return [pal.accent, pal.gen, pal.attention, pal.grid, pal.muted][i % 5];
}

function wifiSpanChannels(b) {
  const width = Number(b.channel_width_mhz || 0);
  if (b.band === '2.4GHz') return 4.5;      // roughly one 20/22 MHz channel footprint
  if (width >= 80) return 16;
  if (width >= 40) return 8;
  return 4;
}

function wifiCurvePoints(b) {
  const center = Number(b.channel);
  const signal = Number(b.signal || 0);
  const span = wifiSpanChannels(b);
  const sigma = span / 2.6;
  const points = [];
  for (let i = 0; i <= 24; i++) {
    const x = center - span + (2 * span * i / 24);
    const y = signal * Math.exp(-0.5 * Math.pow((x - center) / sigma, 2));
    points.push({ x: Number(x.toFixed(2)), y: Number(y.toFixed(1)) });
  }
  return points;
}

function wifiChartOptions(pal, band) {
  const suggested = band === '2.4GHz' ? { min: 1, max: 14 } : { min: 32, max: 180 };
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    parsing: false,
    interaction: { mode: 'nearest', intersect: false },
    elements: { point: { radius: 0 }, line: { tension: 0.35, borderWidth: 2 } },
    plugins: {
      legend: { display: false },
      tooltip: {
        callbacks: {
          title: function (items) {
            return items.length ? items[0].dataset.label : '';
          },
          label: function (item) {
            const raw = item.dataset._wifi || {};
            const bits = [];
            if (raw.signal != null) bits.push(raw.signal + '%');
            if (raw.channel != null) bits.push('ch ' + raw.channel);
            if (raw.bssid) bits.push(raw.bssid);
            return bits.join(' · ');
          },
        },
      },
    },
    scales: {
      x: {
        type: 'linear',
        min: suggested.min,
        max: suggested.max,
        title: { display: true, text: 'Channel', color: pal.muted },
        ticks: { color: pal.muted, precision: 0, maxRotation: 0 },
        grid: { display: false },
      },
      y: {
        beginAtZero: true,
        suggestedMax: 100,
        max: 100,
        title: { display: true, text: 'Signal %', color: pal.muted },
        ticks: { color: pal.muted },
        grid: { color: pal.line },
      },
    },
  };
}

export function createWifiChannelChart(canvas, band) {
  const pal = palette();
  return new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { datasets: [] },
    options: wifiChartOptions(pal, band),
  });
}

export function setWifiChannelData(chart, bssids) {
  const pal = palette();
  chart.data.datasets = (bssids || [])
    .filter(function (b) { return b.channel != null && b.signal != null; })
    .slice()
    .sort(function (a, b) { return (b.signal || 0) - (a.signal || 0); })
    .map(function (b, i) {
      const color = b.connected ? pal.accent : wifiColor(pal, i);
      return {
        label: (b.connected ? 'Current · ' : '') + (b.display_name || b.ssid || '(hidden)'),
        data: wifiCurvePoints(b),
        borderColor: color,
        backgroundColor: alphaFill(color, b.connected ? 0.24 : 0.14),
        fill: 'origin',
        _wifi: b,
      };
    });
  chart.update('none');
}

export function restyleWifiChannelChart(chart) {
  if (!chart) return;
  const pal = palette();
  chart.options.scales.x.title.color = pal.muted;
  chart.options.scales.x.ticks.color = pal.muted;
  chart.options.scales.y.title.color = pal.muted;
  chart.options.scales.y.ticks.color = pal.muted;
  chart.options.scales.y.grid.color = pal.line;
  chart.data.datasets.forEach(function (d, i) {
    const color = d._wifi && d._wifi.connected ? pal.accent : wifiColor(pal, i);
    d.borderColor = color;
    d.backgroundColor = alphaFill(color, d._wifi && d._wifi.connected ? 0.24 : 0.14);
  });
  chart.update('none');
}

// --------------------------------------------------------------- theming
export function restyleForecast(chart) {
  if (!chart) return;
  const pal = palette();
  chart.options.plugins.legend.labels.color = pal.ink;
  chart.data.datasets[0].borderColor = pal.muted;   // expected (dashed estimate)
  chart.data.datasets[0].backgroundColor = pal.muted;
  chart.data.datasets[1].borderColor = pal.gen;      // actual (filled area)
  chart.data.datasets[1].backgroundColor = alphaFill(pal.gen, 0.18);
  Object.assign(chart.options.scales, baseScales(pal, 'kWh'));
  chart.update('none');
}

export function restyle(chart, unit) {
  if (!chart) return;
  const pal = palette();
  chart.options.plugins.legend.labels.color = pal.ink;
  // Series colours track the theme's status tokens (--on / --deficit / --muted).
  // Both history and live charts are areas (translucent fills) + a solid
  // Consumption line.
  chart.data.datasets[0].borderColor = pal.gen;
  chart.data.datasets[0].backgroundColor = alphaFill(pal.gen, 0.18);
  chart.data.datasets[1].borderColor = pal.grid;
  chart.data.datasets[1].backgroundColor = alphaFill(pal.grid, 0.18);
  chart.data.datasets[2].borderColor = pal.muted;
  chart.data.datasets[2].backgroundColor = pal.muted;
  Object.assign(chart.options.scales, baseScales(pal, unit));
  chart.update('none');
}
