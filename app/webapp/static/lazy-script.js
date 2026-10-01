/* Load a classic (UMD) script on first use (#760).
 *
 * Chart.js and Leaflet used to load as classic <script> tags ahead of the boot
 * module, so every cold launch downloaded and ran ~350 KB for two tabs. Each
 * library now loads the first time a view needs it. One promise per URL, so
 * concurrent callers share the download; a failed load is forgotten, so the
 * next use retries instead of staying broken until a reload. */

'use strict';

const pending = new Map();

export function loadScript(src) {
  if (!pending.has(src)) {
    pending.set(src, new Promise(function (resolve, reject) {
      const el = document.createElement('script');
      el.src = src;
      el.onload = function () { resolve(); };
      el.onerror = function () {
        pending.delete(src);
        el.remove();
        reject(new Error('failed to load ' + src));
      };
      document.head.appendChild(el);
    }));
  }
  return pending.get(src);
}
