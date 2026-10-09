/* One row renderer (#880, Step 2/8 of #872).
 *
 * design.md `action-row` + `avatar`: a flat row whose tap opens the item's
 * sheet, with exactly one leading slot, a 600-weight title on one line, at
 * most one muted meta line (which a status chip may close, for an exception
 * only), and exactly one trailing item: a switch, a value, or a segmented
 * verb. Rows of different tabs that show the same kind of thing use this one
 * renderer; a tab that needs a variation adds an optional field here.
 *
 * The anatomy is the vendored action-row (_vendored/action-row/, verbatim):
 * `.action-row-main` is the stretched tap target and the trailing item is its
 * sibling, so a tap on the switch never also opens the sheet. Two parts are
 * this app's until project-scaffolding#341 takes them upstream: the leading
 * avatar inside the tap target, and a trailing item that is not a kebab.
 *
 * Avatar badge = connected (design.md: "alive and nothing else"): 'up' while
 * the device is connected and running, 'down' when it should be reachable and
 * is not, no badge otherwise (plainly off). It is decoration: whatever it
 * says, the meta line or the switch says too.
 *
 * rowEl(opts):
 *   glyph, badge     the avatar's icon name and 'up' | 'down' | null
 *   title            the row's name
 *   meta             optional muted line: text, or a node when it carries a
 *                    glyph (an arrow is a Lucide icon, never a character)
 *   chip             optional exception chip element closing the meta line
 *   openLabel        optional aria-label for the tap target (default: its
 *                    own text, title then meta)
 *   onOpen(btn)      tap handler; the row is inert text without one
 *   chevron          optional: a chevron closes the tap target, for a row
 *                    that only opens its sheet (no trailing item)
 *   lead             optional leading element *outside* the tap target, in
 *                    place of the avatar, for a lead with its own action (a
 *                    camera's last-frame thumbnail zooms, #882)
 *   trail            optional trailing element (a switch, a value)
 *   className        extra class on the row
 * Returns the <li>; the caller appends it to a `<ul class="action-rows">`
 * inside a `.card.action-list`.
 */

'use strict';

import { icon } from './_vendored/icons/icons.js';

export function avatarEl(glyph, badge) {
  const av = document.createElement('span');
  av.className = 'row-avatar';
  av.setAttribute('aria-hidden', 'true');
  av.innerHTML = icon(glyph);
  if (badge === 'up' || badge === 'down') av.dataset.badge = badge;
  return av;
}

export function rowEl(opts) {
  const li = document.createElement('li');
  li.className = 'action-row' + (opts.className ? ' ' + opts.className : '');

  const main = document.createElement(opts.onOpen ? 'button' : 'div');
  main.className = 'action-row-main';
  if (opts.onOpen) {
    main.type = 'button';
    if (opts.openLabel) main.setAttribute('aria-label', opts.openLabel);
    main.addEventListener('click', function () { opts.onOpen(main); });
  }
  if (opts.glyph) {
    main.classList.add('has-avatar');
    main.appendChild(avatarEl(opts.glyph, opts.badge));
  }

  const text = document.createElement('span');
  text.className = 'action-row-text';
  const title = document.createElement('span');
  title.className = 'action-row-title';
  title.textContent = opts.title;
  text.appendChild(title);
  if (opts.meta || opts.chip) {
    const meta = document.createElement('span');
    meta.className = 'action-row-meta';
    if (opts.meta) {
      const words = document.createElement('span');
      words.className = 'action-row-meta-text';
      if (typeof opts.meta === 'string') words.textContent = opts.meta;
      else words.appendChild(opts.meta);
      meta.appendChild(words);
    }
    // Outside the truncating span, so an ellipsis never cuts the exception.
    if (opts.chip) meta.appendChild(opts.chip);
    text.appendChild(meta);
  }
  main.appendChild(text);
  if (opts.chevron) {
    main.insertAdjacentHTML('beforeend', icon('chevron-right', 'action-row-chevron'));
  }
  if (opts.lead) {
    opts.lead.classList.add('action-row-lead');
    li.appendChild(opts.lead);
  }
  li.appendChild(main);

  if (opts.trail) {
    opts.trail.classList.add('action-row-trail');
    li.appendChild(opts.trail);
  }
  return li;
}
