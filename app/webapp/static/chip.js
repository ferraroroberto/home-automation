/* Home Automation — the one status chip (issue #879, design.md "status chip").
 *
 * A chip marks an exception only; a normal state gets none. Tones are the one
 * map in styles.css (.chip[data-tone]): neutral (omit the tone) for a plain
 * fact, 'accent' for selected / in progress, 'attention' for needs-you-soon,
 * 'danger' for broken. `success` is never a chip. `cls` is an optional
 * component hook for layout or tests, never a colour.
 */

'use strict';

import { esc } from './format.js';

export function chipEl(text, tone, cls) {
  const el = document.createElement('span');
  el.className = 'chip' + (cls ? ' ' + cls : '');
  if (tone) el.dataset.tone = tone;
  el.textContent = text;
  return el;
}

// The same markup for template strings. `text` is escaped here.
export function chipHtml(text, tone, cls) {
  return '<span class="chip' + (cls ? ' ' + esc(cls) : '') + '"' +
    (tone ? ' data-tone="' + esc(tone) + '"' : '') + '>' + esc(text) + '</span>';
}
