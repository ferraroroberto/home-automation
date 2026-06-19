/* Chart.js wrappers for the Energy tab.
 *
 * Two charts, both reading their colors from the page's CSS custom properties
 * so dark/light themes Just Work (restyle() re-reads them on toggle):
 *
 *   • live line chart  — Production / Consumption / Net (W), recent samples.
 *     spanGaps:false so an asleep inverter (null PV) draws a gap, never a 0.
 *   • aggregate bars    — Production / Consumption / Net grid (Wh) per bucket;
 *     Net is signed (− importing, + exporting).
 *
 * Chart.js is loaded as a vendored UMD global (window.Chart) by index.html. */

'use strict';

const PROD = '#f5a623';   // warm — production stands out
const CONS = '#2f7df6';   // accent blue — consumption
const NET = '#2ecc71';    // green — net (paired with red via per-bar coloring)
const NEG = '#e2574c';    // red — importing (negative net)

function cssVar(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}

function palette() {
  return {
    ink: cssVar('--ink', '#16202e'),
    muted: cssVar('--muted', '#6b7785'),
    line: cssVar('--line', '#e2e7ef'),
  };
}

function baseScales(pal, unit) {
  return {
    x: {
      ticks: { color: pal.muted, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 },
      grid: { display: false },
    },
    y: {
      title: { display: true, text: unit, color: pal.muted },
      ticks: { color: pal.muted },
      grid: { color: pal.line },
    },
  };
}

function legend(pal) {
  return { labels: { color: pal.ink, boxWidth: 12, usePointStyle: true } };
}

// ----------------------------------------------------------------- live
export function createLiveChart(canvas) {
  const pal = palette();
  return new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        ds('Production', PROD),
        ds('Consumption', CONS),
        ds('Net', NET),
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      spanGaps: false,
      interaction: { mode: 'index', intersect: false },
      elements: { point: { radius: 0 }, line: { tension: 0.25, borderWidth: 2 } },
      plugins: { legend: legend(pal) },
      scales: baseScales(pal, 'W'),
    },
  });
}

function ds(label, color) {
  return {
    label: label,
    data: [],
    borderColor: color,
    backgroundColor: color,
    fill: false,
  };
}

export function setLiveData(chart, samples) {
  chart.data.labels = samples.map(function (s) { return timeLabel(s.ts); });
  chart.data.datasets[0].data = samples.map(function (s) { return s.pv_power_w; });
  chart.data.datasets[1].data = samples.map(function (s) { return s.house_consumption_w; });
  chart.data.datasets[2].data = samples.map(function (s) { return s.pv_surplus_w; });
  chart.update('none');
}

export function pushLivePoint(chart, ts, prod, cons, net, maxPoints) {
  chart.data.labels.push(timeLabel(ts));
  chart.data.datasets[0].data.push(prod);
  chart.data.datasets[1].data.push(cons);
  chart.data.datasets[2].data.push(net);
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

// ------------------------------------------------------------ aggregate
export function createAggChart(canvas) {
  const pal = palette();
  return new Chart(canvas.getContext('2d'), {
    type: 'bar',
    data: {
      labels: [],
      datasets: [
        { label: 'Production', data: [], backgroundColor: PROD },
        { label: 'Consumption', data: [], backgroundColor: CONS },
        { label: 'Net grid', data: [], backgroundColor: [] },
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      plugins: { legend: legend(pal) },
      scales: baseScales(pal, 'Wh'),
    },
  });
}

export function setAggData(chart, buckets) {
  chart.data.labels = buckets.map(function (b) { return b.label; });
  // Production: null (not 0) when the inverter was asleep all bucket, so the
  // bar is absent rather than a misleading zero.
  chart.data.datasets[0].data = buckets.map(function (b) {
    return b.pv_missing ? null : b.pv_wh;
  });
  chart.data.datasets[1].data = buckets.map(function (b) { return b.house_wh; });
  const net = buckets.map(function (b) { return round1(b.export_wh - b.import_wh); });
  chart.data.datasets[2].data = net;
  chart.data.datasets[2].backgroundColor = net.map(function (v) {
    return v >= 0 ? NET : NEG;
  });
  chart.update('none');
}

function round1(v) { return Math.round(v * 10) / 10; }

// --------------------------------------------------------------- theming
export function restyle(chart, unit) {
  if (!chart) return;
  const pal = palette();
  chart.options.plugins.legend.labels.color = pal.ink;
  Object.assign(chart.options.scales, baseScales(pal, unit));
  chart.update('none');
}
