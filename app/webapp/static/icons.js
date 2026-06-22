/* Home Automation — Lucide icon helper.
 *
 * One icon source for the whole PWA: the inline <svg> sprite at the top of
 * index.html — a set of <symbol id="i-NAME"> Lucide glyphs (vendored from
 * lucide-static v1.21.0, ISC). Reference a glyph from JS with icon(name); in
 * static markup write <svg class="icon"><use href="#i-NAME"></use></svg>.
 *
 * The sprite is inline (not an external /static/icons.svg) on purpose: iOS
 * Safari does not resolve external <use href="file.svg#id"> references, so the
 * symbols must ship in-document. Stroke styling lives on the `.icon` CSS class
 * and is inherited into the <use> shadow tree; fill:none lives on each <symbol>
 * so the few filled glyphs (e.g. the palette dots) keep their fill.
 */

'use strict';

export function icon(name, extraClass) {
  const cls = 'icon' + (extraClass ? ' ' + extraClass : '');
  return '<svg class="' + cls + '" aria-hidden="true"><use href="#i-' + name + '"></use></svg>';
}
