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
 * NOTE: tokens are consumed as #rrggbb here (hexA parses hex). The P3 oklch layer
 * (issue #65) must add a gamut-safe resolver before --on/--deficit can be oklch.
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
    gen: cssVar('--on', '#1a7f37'),
    grid: cssVar('--deficit', '#cf222e'),
  };
}

// Hex (#rrggbb) → rgba string at the given alpha — for translucent fills.
function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return 'rgba(' + ((n >> 16) & 255) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')';
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
    backgroundColor: hexA(color, 0.18),
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
    // spanGaps:true — empty future slots are real 0 kWh, draw a continuous frame.
    options: commonOptions(pal, 'kWh', true),
  });
}

function kwh(wh) { return (Number(wh) || 0) / 1000; }

export function setAggData(chart, buckets) {
  chart.data.labels = buckets.map(function (b) { return b.label; });
  chart.data.datasets[0].data = buckets.map(function (b) { return kwh(b.pv_wh); });
  chart.data.datasets[1].data = buckets.map(function (b) { return kwh(b.import_wh); });
  chart.data.datasets[2].data = buckets.map(function (b) { return kwh(b.house_wh); });
  chart.update('none');
}

// --------------------------------------------------------------- theming
export function restyle(chart, unit) {
  if (!chart) return;
  const pal = palette();
  chart.options.plugins.legend.labels.color = pal.ink;
  // Series colours track the theme's status tokens (--on / --deficit / --muted).
  chart.data.datasets[0].borderColor = pal.gen;
  chart.data.datasets[0].backgroundColor = hexA(pal.gen, 0.18);
  chart.data.datasets[1].borderColor = pal.grid;
  chart.data.datasets[1].backgroundColor = hexA(pal.grid, 0.18);
  chart.data.datasets[2].borderColor = pal.muted;
  chart.data.datasets[2].backgroundColor = pal.muted;
  Object.assign(chart.options.scales, baseScales(pal, unit));
  chart.update('none');
}
