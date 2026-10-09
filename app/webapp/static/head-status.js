/* The page header's live line (#880, Step 2/8 of #872).
 *
 * design.md `page-header`: the context line beside each tab's title is live
 * and names only the exceptions, each in its tone's *-text, at most two parts
 * (about 22 characters at 390px), and falls back to a plain fact in muted text
 * when nothing is wrong. Never a fixed slogan, never a count another tab owns.
 * The same rule as app-launcher's home-head.js (renderHeadStatus); it moves
 * upstream into the vendored home-head in project-scaffolding#343.
 *
 * A tab's line can be fed by more than one module (Devices: the plugs poll
 * and the UPS poll), so each module reports its own part with setHeadPart and
 * the line is rebuilt from every part the tab has: exceptions merged danger
 * first, plain facts joined in report order. A module that knows nothing yet
 * reports null, which removes its part, and an unknown tab shows nothing
 * rather than a reassuring fact it can't back.
 *
 * Markup: `<span class="status" data-head="TAB" aria-live="off">` inside the
 * pane's vendored home-head (the slot home-head.css already lays out).
 */

'use strict';

// The most exceptions one header line shows (about 22 characters at 390px).
export const HEAD_MAX_PARTS = 2;

const TONE_ORDER = { danger: 0, attention: 1 };

function toneRank(tone) {
  return tone in TONE_ORDER ? TONE_ORDER[tone] : 2;
}

// tab -> Map(source -> { exceptions: [{text, tone}], fact: string })
const parts = new Map();

// Render one line: `exceptions` win (danger first, two at most, each its own
// span in its tone); otherwise `fallback` in the slot's muted text.
export function renderHeadStatus(el, exceptions, fallback) {
  if (!el) return;
  const shown = (exceptions || [])
    .filter(function (x) { return x && x.text; })
    .sort(function (a, b) { return toneRank(a.tone) - toneRank(b.tone); })
    .slice(0, HEAD_MAX_PARTS);
  if (!shown.length) {
    el.textContent = fallback || '';
    delete el.dataset.tone;
    return;
  }
  el.textContent = '';
  shown.forEach(function (x, i) {
    if (i) el.appendChild(document.createTextNode(' · '));
    const part = document.createElement('span');
    part.className = 'head-exception';
    part.dataset.tone = x.tone;
    part.textContent = x.text;
    el.appendChild(part);
  });
  el.dataset.tone = shown[0].tone;
}

// Report (or, with null, withdraw) one module's part of a tab's line, then
// rebuild that line.
export function setHeadPart(tab, source, part) {
  let tabParts = parts.get(tab);
  if (!tabParts) {
    tabParts = new Map();
    parts.set(tab, tabParts);
  }
  if (part) tabParts.set(source, part);
  else tabParts.delete(source);

  const exceptions = [];
  const facts = [];
  tabParts.forEach(function (p) {
    (p.exceptions || []).forEach(function (x) { exceptions.push(x); });
    if (p.fact) facts.push(p.fact);
  });
  renderHeadStatus(
    document.querySelector('.home-head .status[data-head="' + tab + '"]'),
    exceptions,
    facts.join(' · ')
  );
}
