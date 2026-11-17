/* The trigger of a view's one Filters & sort sheet.
 *
 * The badge counts the active non-default filters and the accessible name
 * spells them out, so content a filter hides never disappears silently
 * (design.md "one sheet, named Filters & sort"). With none active the badge is
 * hidden and the name is the plain "Filters & sort".
 *
 * `active` is the list of human labels of the filters that differ from their
 * default ("Period: Week", "Showing money"); the caller owns what counts.
 */

'use strict';

const BASE_NAME = 'Filters & sort';

export function renderFilterTrigger(button, badge, active) {
  if (!button) return;
  const n = active.length;
  const name = n ? BASE_NAME + ', ' + n + ' active: ' + active.join(', ') : BASE_NAME;
  button.setAttribute('aria-label', name);
  button.title = name;
  if (badge) {
    badge.textContent = n ? String(n) : '';
    badge.hidden = n === 0;
  }
}
