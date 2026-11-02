/**
 * Internet-health tile trends: a latency sparkline (last 24 h) and a
 * speed-test sparkline (download solid, upload dashed, last 30 days), read from
 * `GET /api/network/internet-history`.
 *
 * Passive by design: a failed read keeps whatever was last drawn (the trends
 * are context under the live figures, not a surface that can be "stale and
 * unsafe to act on"), and the tile stays hidden until the first good read.
 * Strokes use tokens via CSS classes, so a theme flip needs no redraw.
 */

import { els } from '../state.js';
import { jsonApi } from '../api.js';
import { icon } from '../_vendored/icons/icons.js';

const W = 300;          // viewBox width — the SVG stretches to its cell
const H = 40;           // viewBox height (CSS pins the rendered height)
const PAD = 4;          // vertical inset so the stroke never clips
const LATENCY_MIN_SPAN_MS = 10;   // a steady link must not read as a swing
const SPEED_MIN_TESTS = 3;        // fewer tests than this is not a trend

function fmtMs(v) { return Math.round(Number(v)) + ' ms'; }
function fmtMbps(v) { return Math.round(Number(v)) + ' Mbps'; }

/** Map `[[ts, value], ...]` to SVG coordinates on a shared y scale `scale = {lo, hi}`. */
function project(points, scale, t0, t1) {
  const span = t1 - t0;
  const yRange = scale.hi - scale.lo;
  return points.map(function (p) {
    const x = span > 0 ? ((p[0] - t0) / span) * W : W;
    const y = H - PAD - ((p[1] - scale.lo) / yRange) * (H - 2 * PAD);
    return [x, y];
  });
}

/**
 * Latency's own range, centred and widened to at least `LATENCY_MIN_SPAN_MS`,
 * so a steady 14-15 ms link plots mid-chart with its jitter visible instead
 * of pinned to the top of a zero-based axis. Never dips below 0 ms.
 */
function latencyScale(values) {
  const lo = Math.min.apply(null, values);
  const hi = Math.max.apply(null, values);
  const span = Math.max(hi - lo, LATENCY_MIN_SPAN_MS);
  const mid = (hi + lo) / 2;
  const floor = Math.max(0, mid - span / 2);
  return { lo: floor, hi: floor + span, flat: hi - lo < LATENCY_MIN_SPAN_MS };
}

function pathOf(coords) {
  // A lone point is a zero-length round-capped segment — a dot that survives
  // the non-uniform viewBox stretch (a <circle> would squash to an ellipse).
  if (coords.length === 1) return 'M' + coords[0][0].toFixed(1) + ' ' + coords[0][1].toFixed(1) + 'h0.01';
  return coords.map(function (c, i) {
    return (i ? 'L' : 'M') + c[0].toFixed(1) + ' ' + c[1].toFixed(1);
  }).join('');
}

function sparkline(series, label, scale) {
  const all = series.reduce(function (acc, s) { return acc.concat(s.points); }, []);
  const t0 = Math.min.apply(null, all.map(function (p) { return p[0]; }));
  const t1 = Math.max.apply(null, all.map(function (p) { return p[0]; }));
  const parts = series.map(function (s) {
    const coords = project(s.points, scale, t0, t1);
    const line = '<path class="net-spark-line ' + s.cls + '" d="' + pathOf(coords) + '"/>';
    if (!s.area || coords.length < 2) return line;
    const first = coords[0];
    const last = coords[coords.length - 1];
    const fill = pathOf(coords) + 'L' + last[0].toFixed(1) + ' ' + H + 'L' + first[0].toFixed(1) + ' ' + H + 'Z';
    return '<path class="net-spark-area" d="' + fill + '"/>' + line;
  });
  return '<svg class="net-spark" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none" ' +
    'role="img" aria-label="' + label + '">' + parts.join('') + '</svg>';
}

function row(name, value, body) {
  return '<div class="net-trend">' +
    '<div class="net-trend-text"><span class="net-trend-name">' + name + '</span>' +
    '<span class="net-trend-value muted small">' + value + '</span></div>' +
    '<div class="net-trend-chart">' + body + '</div></div>';
}

function empty(text) {
  return '<span class="net-trend-empty muted small">' + text + '</span>';
}

function latencyRow(points) {
  if (!points.length) {
    return row('Latency', '24 h', empty('Fills in while this tab is open.'));
  }
  const vals = points.map(function (p) { return p[1]; });
  const lo = Math.min.apply(null, vals);
  const hi = Math.max.apply(null, vals);
  const label = 'Latency, last 24 hours: ' + fmtMs(lo) + ' to ' + fmtMs(hi) + ', latest ' +
    fmtMs(points[points.length - 1][1]);
  // The fill reads as "amount above zero", which a floating range is not: a
  // flat series keeps just the line.
  const scale = latencyScale(vals);
  return row('Latency', fmtMs(points[points.length - 1][1]),
    sparkline([{ points: points, cls: 'is-latency', area: !scale.flat }], label, scale));
}

function speedRow(down, up) {
  const name = 'Speed <span class="net-trend-key" aria-hidden="true">' +
    icon('arrow-down') + icon('arrow-up') + '</span>';
  if (!down.length && !up.length) {
    return row(name, '30 d', empty('Run a speed test to start this trend.'));
  }
  const count = Math.max(down.length, up.length);
  if (count < SPEED_MIN_TESTS) {
    return row(name, count + (count === 1 ? ' test' : ' tests'),
      empty('A trend needs ' + SPEED_MIN_TESTS + ' tests; ' + count + ' so far.'));
  }
  const series = [];
  if (down.length) series.push({ points: down, cls: 'is-down' });
  if (up.length) series.push({ points: up, cls: 'is-up' });
  const label = 'Download and upload speed, last 30 days, ' + count + ' test' + (count === 1 ? '' : 's') +
    (down.length ? ', latest download ' + fmtMbps(down[down.length - 1][1]) : '') +
    (up.length ? ', latest upload ' + fmtMbps(up[up.length - 1][1]) : '');
  // Throughput keeps a zero baseline: the gap between tests is the signal.
  const top = Math.max.apply(null, down.concat(up).map(function (p) { return p[1]; })) || 1;
  return row(name, count + ' tests', sparkline(series, label, { lo: 0, hi: top }));
}

export function renderTrends(history) {
  const host = els.netTrends;
  if (!host || !history) return;
  host.innerHTML = latencyRow(history.latency || []) +
    speedRow(history.download || [], history.upload || []);
  host.hidden = false;
}

/** Fetch + draw; never throws (the tile's own read owns error feedback). */
export async function loadInternetTrends() {
  try {
    renderTrends(await jsonApi('/api/network/internet-history'));
  } catch (exc) {
    console.warn('internet trends unavailable', exc);
  }
}
