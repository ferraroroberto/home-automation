/* Load a classic (UMD) script, or its stylesheet, on first use (#760, #777).
 *
 * Chart.js and Leaflet used to load as classic <script> tags ahead of the boot
 * module, so every cold launch downloaded and ran ~350 KB for two tabs. Each
 * library now loads the first time a view needs it; Leaflet's stylesheet too,
 * since a <link> in <head> is render-blocking. One promise per URL, so
 * concurrent callers share the download; a failed load is forgotten, so the
 * next use retries instead of staying broken until a reload. */

'use strict';

const pending = new Map();

function loadOnce(url, makeElement) {
  if (!pending.has(url)) {
    pending.set(url, new Promise(function (resolve, reject) {
      const el = makeElement(url);
      el.onload = function () { resolve(); };
      el.onerror = function () {
        pending.delete(url);
        el.remove();
        reject(new Error('failed to load ' + url));
      };
      document.head.appendChild(el);
    }));
  }
  return pending.get(url);
}

export function loadScript(src) {
  return loadOnce(src, function (url) {
    const el = document.createElement('script');
    el.src = url;
    return el;
  });
}

export function loadStyle(href) {
  return loadOnce(href, function (url) {
    const el = document.createElement('link');
    el.rel = 'stylesheet';
    el.href = url;
    return el;
  });
}
