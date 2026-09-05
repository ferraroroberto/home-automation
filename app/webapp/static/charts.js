/* Chart.js wrappers for the Energy tab.
 *
 * Both charts use the same three all-positive series — nothing dips below zero,
 * so every line "goes up" and the translucent fills stack visually:
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

function energyTickBudget(width) {
  return Number(width) <= 480 ? 4 : 8;
}

function baseScales(pal, unit, width) {
  return {
    x: {
      ticks: {
        color: pal.muted,
        maxRotation: 0,
        autoSkip: true,
        autoSkipPadding: 12,
        maxTicksLimit: energyTickBudget(width || window.innerWidth),
      },
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

function energyDatasets(pal) {
  const generation = area('Generation', pal.gen);
  generation.borderDash = [];
  generation.pointStyle = 'circle';

  const grid = area('Grid-supplied', pal.grid);
  grid.borderDash = [8, 4];
  grid.pointStyle = 'rectRot';

  const consumption = envelope('Consumption', pal.muted);
  consumption.borderDash = [2, 4];
  consumption.pointStyle = 'triangle';

  return [generation, grid, consumption];
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
    onResize: function (chart, size) {
      chart.options.scales.x.ticks.maxTicksLimit = energyTickBudget(size.width);
    },
  };
}

// ----------------------------------------------------------------- live
export function createLiveChart(canvas) {
  const pal = palette();
  return new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels: [],
      datasets: energyDatasets(pal),
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
  const solarUsed = envelope('Solar consumed', pal.accent);
  solarUsed.borderDash = [6, 3];
  solarUsed.pointStyle = 'rectRot';
  const exported = envelope('Solar exported', pal.attention);
  exported.borderDash = [2, 3];
  exported.pointStyle = 'triangle';
  return new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: {
      labels: [],
      datasets: [
        area('Production', pal.gen),
        envelope('Consumption', pal.muted),
        envelope('Grid imported', pal.grid),
        solarUsed,
        exported,
      ],
    },
    options: commonOptions(pal, 'kWh', true),
  });
}

function kwh(wh) { return (Number(wh) || 0) / 1000; }

// The slot containing *now* is still filling, so it is drawn as an explicit
// in-progress marker instead of a settled value: a hollow point, with the
// segment leading into it dashed (#557). Without this the half-finished slot
// reads as a collapse — the array is still producing, the hour just isn't over.
// pointBorderColor is scriptable so it tracks the dataset's colour through a
// light/dark theme switch, which restyle() applies to borderColor only.
// Drop any in-progress styling a previous render left behind, so switching
// range (Day → Σ Total) can't leave a stale hollow marker on a settled value.
function clearInProgress(ds) {
  ds.pointBackgroundColor = undefined;
  ds.pointBorderColor = undefined;
  ds.pointBorderWidth = undefined;
  ds.segment = {};
}

// Takes a *list* of indices: the history chart marks at most one slot (the one
// containing now), but the forecast overlay can have several — every hour the
// feed only partly covered is an inference too, not just the in-progress one
// (#579).
function markInProgress(ds, indices) {
  clearInProgress(ds);
  const marked = {};
  (indices || []).forEach(function (i) { if (i >= 0) marked[i] = true; });
  if (!Object.keys(marked).length) {
    ds.pointRadius = 0;
    return;
  }
  ds.pointRadius = ds.data.map(function (_, i) { return marked[i] ? 4 : 0; });
  ds.pointBackgroundColor = 'transparent';   // hollow — reads as "not settled"
  ds.pointBorderColor = function (ctx) { return ctx.dataset.borderColor; };
  ds.pointBorderWidth = 2;
  ds.segment = {
    borderDash: function (ctx) {
      return marked[ctx.p1DataIndex] ? [4, 3] : undefined;
    },
  };
}

export function setAggData(chart, buckets) {
  chart.data.labels = buckets.map(function (b) { return b.label; });
  chart.data.datasets[0].data = buckets.map(function (b) { return kwh(b.pv_wh); });
  chart.data.datasets[1].data = buckets.map(function (b) { return kwh(b.house_wh); });
  chart.data.datasets[2].data = buckets.map(function (b) { return kwh(b.import_wh); });
  chart.data.datasets[3].data = buckets.map(function (b) {
    return Math.max(0, kwh(b.house_wh) - kwh(b.import_wh));
  });
  chart.data.datasets[4].data = buckets.map(function (b) { return kwh(b.export_wh); });
  // A line through a single point is invisible (pointRadius is 0 everywhere
  // else), so the Σ Total — and any range that resolves to one bucket — would
  // read as empty. Show the markers only in that case so the value is visible.
  const single = buckets.length <= 1;
  if (single) {
    chart.data.datasets.forEach(function (d) { clearInProgress(d); d.pointRadius = 4; });
  } else {
    // Values stay exactly as measured here: unlike the forecast card there is
    // no expected curve to be comparable to, and extrapolating a part-finished
    // day or month would be far more speculative than a part-finished hour.
    // Buckets map 1:1 onto plotted points, so the array index is the chart index.
    const idx = buckets.findIndex(function (b) { return b && b.partial; });
    chart.data.datasets.forEach(function (d) { markInProgress(d, [idx]); });
  }
  chart.update('none');
}

// Export compensation is money, not energy, so it gets its own small chart
// rather than a misleading second unit on the generation chart.
export function createExportCreditChart(canvas) {
  const pal = palette();
  const cost = envelope('Grid cost', pal.grid);
  cost.pointStyle = 'circle';
  const saved = envelope('Avoided cost', pal.gen);
  saved.borderDash = [8, 4];
  saved.pointStyle = 'rectRot';
  const credit = envelope('Export income', pal.accent);
  credit.borderDash = [2, 4];
  credit.pointStyle = 'triangle';
  return new Chart(canvas.getContext('2d'), {
    type: 'line',
    data: { labels: [], datasets: [cost, saved, credit] },
    options: commonOptions(pal, '€', true),
  });
}

export function setExportCreditData(chart, points) {
  chart.data.labels = (points || []).map(function (point) { return point.label; });
  chart.data.datasets[0].data = (points || []).map(function (point) { return Number(point.grid_cost) || 0; });
  chart.data.datasets[1].data = (points || []).map(function (point) { return Number(point.savings) || 0; });
  chart.data.datasets[2].data = (points || []).map(function (point) { return Number(point.export_credit) || 0; });
  chart.data.datasets.forEach(function (dataset) {
    dataset.pointRadius = points && points.length <= 1 ? 4 : 0;
  });
  chart.update('none');
}

export function restyleExportCredit(chart) {
  if (!chart) return;
  const pal = palette();
  chart.options.plugins.legend.labels.color = pal.ink;
  chart.data.datasets[0].borderColor = pal.grid;
  chart.data.datasets[0].backgroundColor = pal.grid;
  chart.data.datasets[1].borderColor = pal.gen;
  chart.data.datasets[1].backgroundColor = pal.gen;
  chart.data.datasets[2].borderColor = pal.accent;
  chart.data.datasets[2].backgroundColor = pal.accent;
  Object.assign(chart.options.scales, baseScales(pal, '€'));
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
  // Two kinds of hour arrive projected to a full-hour rate so they stay
  // comparable to the expected curve: the one still in progress (#557), and any
  // the feed only partly covered (#579). Both are `estimated`, and both are
  // drawn hollow + dashed so they read as the inference they are, never as a
  // measurement — which is the whole point: an under-measured hour plotted
  // solid is indistinguishable from a real production collapse. The series is
  // plotted on a fixed 24-hour axis, so a point's own `hour` is its chart
  // index; an hour with nothing to plot yet (too little data to project) is
  // left unmarked.
  const estimatedIdx = [];
  if (hasActual) {
    actual.forEach(function (p) {
      if (p && p.estimated && p.wh != null) estimatedIdx.push(Number(p.hour));
    });
  }
  markInProgress(chart.data.datasets[1], estimatedIdx);
  chart.update('none');
}

// ------------------------------------------------- sun-position diagnostic
// Effective performance ratio (what the array actually delivered per unit of
// plane-of-array irradiance) plotted against where the sun *was*, not against
// the clock — issue #590. Read on this axis, a drop that repeats at the same
// azimuth day after day is fixed obstruction geometry; one that wanders with
// the time of day is weather.
//
// Deliberately a scatter, not a line: the points are independent hourly
// observations, and joining them would imply a continuity between 09:00 and
// 15:00 that an excluded 12:00 does not have. The modelled PR is the flat
// dashed --muted reference the measured points are read against, matching how
// the forecast card already draws its estimate.
function sunOverlayOptions(pal) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: false,
    interaction: { mode: 'nearest', intersect: false },
    plugins: {
      legend: legend(pal),
      tooltip: {
        callbacks: {
          title: function (items) {
            const p = items.length ? items[0].raw : null;
            return p && p.hour != null
              ? (p.hour < 10 ? '0' + p.hour : '' + p.hour) + ':00'
              : '';
          },
          label: function (item) {
            const p = item.raw || {};
            if (p.hour == null) return 'Modelled PR ' + Number(item.parsed.y).toFixed(2);
            return [
              'Effective PR ' + Number(p.y).toFixed(2),
              'Azimuth ' + Math.round(p.x) + '° · elevation ' + Math.round(p.elevation) + '°',
              (p.actualWh / 1000).toFixed(2) + ' of ' + (p.expectedWh / 1000).toFixed(2) + ' kWh modelled',
            ];
          },
        },
      },
    },
    scales: {
      x: {
        type: 'linear',
        title: { display: true, text: 'Sun azimuth (° from north)', color: pal.muted },
        ticks: { color: pal.muted, maxRotation: 0 },
        grid: { display: false },
      },
      y: {
        beginAtZero: true,
        suggestedMax: 1,
        title: { display: true, text: 'Effective PR', color: pal.muted },
        ticks: { color: pal.muted },
        grid: { color: pal.line },
      },
    },
  };
}

export function createSunOverlayChart(canvas) {
  const pal = palette();
  return new Chart(canvas.getContext('2d'), {
    type: 'scatter',
    data: {
      datasets: [
        {
          label: 'Measured',
          data: [],
          borderColor: pal.gen,
          backgroundColor: alphaFill(pal.gen, 0.55),
          pointRadius: 5,
          pointHoverRadius: 7,
          showLine: false,
        },
        {
          label: 'Modelled',
          data: [],
          borderColor: pal.muted,
          backgroundColor: pal.muted,
          borderDash: [6, 4],
          borderWidth: 2,
          pointRadius: 0,
          showLine: true,
          fill: false,
        },
      ],
    },
    options: sunOverlayOptions(pal),
  });
}

export function setSunOverlayData(chart, points, modelledPr) {
  const pts = (points || []).map(function (p) {
    return {
      x: p.azimuth_deg,
      y: p.effective_pr,
      hour: p.hour,
      elevation: p.elevation_deg,
      actualWh: p.actual_wh,
      expectedWh: p.expected_wh,
    };
  });
  chart.data.datasets[0].data = pts;
  // The reference is one configured number, so it spans whatever azimuth range
  // the day actually produced — two points, no invented resolution. With
  // nothing measured there is no span to draw it across, so it stays empty
  // rather than floating over a blank axis.
  const pr = Number(modelledPr);
  chart.data.datasets[1].data = (pts.length && Number.isFinite(pr))
    ? [
        { x: Math.min.apply(null, pts.map(function (p) { return p.x; })), y: pr },
        { x: Math.max.apply(null, pts.map(function (p) { return p.x; })), y: pr },
      ]
    : [];
  chart.update('none');
}

export function restyleSunOverlay(chart) {
  if (!chart) return;
  const pal = palette();
  chart.options.plugins.legend.labels.color = pal.ink;
  chart.data.datasets[0].borderColor = pal.gen;
  chart.data.datasets[0].backgroundColor = alphaFill(pal.gen, 0.55);
  chart.data.datasets[1].borderColor = pal.muted;
  chart.data.datasets[1].backgroundColor = pal.muted;
  Object.assign(chart.options.scales, sunOverlayOptions(pal).scales);
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
  const colors = chart.data.datasets.length === 5
    ? [pal.gen, pal.muted, pal.grid, pal.accent, pal.attention]
    : [pal.gen, pal.grid, pal.muted];
  chart.data.datasets.forEach(function (dataset, index) {
    dataset.borderColor = colors[index];
    dataset.backgroundColor = index === 0 && chart.data.datasets.length === 5
      ? alphaFill(colors[index], 0.18)
      : colors[index];
  });
  Object.assign(chart.options.scales, baseScales(pal, unit));
  chart.update('none');
}
