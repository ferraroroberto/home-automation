/* Home Automation — entry module: boots the dashboard and wires events.
 *
 * Loaded by index.html as <script type="module">. Thin orchestrator (issue
 * #346 maintainability split, mirroring #197's security.js/network.js
 * pattern): owns boot wiring, the theme toggle, nav-debug init, and login.
 * The AC/units card grid, Home summary tile, and per-unit detail modal
 * (mode/fan/vanes + temperature-rule + schedule editor) live in
 * ./units.js — each write there hits POST /api/units/{id} and re-renders
 * only that card from the read-back response.
 */

'use strict';

import {
  els,
  tokenFromUrl,
  writeToken,
  THEME_KEY,
} from './state.js';
import { icon } from './_vendored/icons/icons.js';
import { jsonApi, hideLogin } from './api.js';
import { wireTabs } from './tabs.js';
import { installNavDebug, isNavDebugEnabled, setNavDebugEnabled } from './nav-debug.js';
import {
  loadUnits,
  restoreUnitsSnapshot,
  onUnitsTab,
  wireUnitsControls,
} from './units.js';
import {
  loadEnergy,
  wireEnergyControls,
  onEnergyTab,
  restyleEnergyCharts,
  restoreEnergySnapshots,
} from './energy.js';
import { onPlugsTab, wirePlugsRefresh, wirePlugsToggle, wirePlugDetail, restorePlugsSnapshot } from './plugs.js';
import { onUpsTab, restoreUpsSnapshot } from './ups.js';
import { wirePowerNotify } from './ups-notify.js';
import { wirePcFleet, onPcFleetTab } from './pc-fleet.js';
import { onVmTab, restoreVmSnapshot, wireVm } from './vm.js';
import { onHaTab, wireHa } from './ha.js';
import { onLightsTab, wireLightControls, restoreLightsSnapshot } from './lights.js';
import { onSecurityTab, wireZoneDetail, wireSecurityHiddenToggle, wireSecuritySchedules, wireScenePairings, wireSecurityOverrides, wirePresenceControls, wirePresencePlaces, wireSecurityNotify } from './security.js';
import { onWakeAlarmsTab, wireWakeAlarms } from './wake-alarms.js';
import { wireVoiceCommands } from './voice-commands.js';
import { onCamerasTab, wireCameras } from './cameras.js';
import { onNetworkTab, wireNetworkControls, restyleNetworkCharts, restoreNetworkSnapshot } from './network.js';
import { startWeatherPolling } from './weather.js';
import { wireActivity } from './activity.js';
import { installDialogScrollLock } from './scroll-lock.js';

// --------------------------------------------------- build identity
function fmtBuildTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return String(iso).replace('T', ' ').slice(0, 16);
  const pad = function (n) { return String(n).padStart(2, '0'); };
  return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) +
    ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
}

const ASSET_HASH_KEY = 'home-automation.assetHash';
const ASSET_RELOAD_KEY = 'home-automation.assetReloadedFor';

async function fetchVersion() {
  // Visible proof of which build the PWA is running — confirms a tray
  // restart actually picked up new code. Uses jsonApi so the bearer token
  // is attached (/api/version is auth-gated like the rest of the API).
  try {
    const body = await jsonApi('/api/version');
    const sha = body.git_sha || 'unknown';
    const assetHash = body.asset_hash || '';
    const previousHash = localStorage.getItem(ASSET_HASH_KEY) || '';
    if (
      assetHash && previousHash && previousHash !== assetHash &&
      sessionStorage.getItem(ASSET_RELOAD_KEY) !== assetHash
    ) {
      // iOS standalone PWAs can cling to an old shell even with stamped asset
      // URLs. Once a freshly-loaded JS has this guard, future deploys get one
      // automatic reload instead of needing a home-screen reinstall.
      localStorage.setItem(ASSET_HASH_KEY, assetHash);
      sessionStorage.setItem(ASSET_RELOAD_KEY, assetHash);
      window.location.reload();
      return;
    }
    if (assetHash) localStorage.setItem(ASSET_HASH_KEY, assetHash);
    const ts = fmtBuildTime(body.built_at || '');
    els.buildReadout.textContent = ts ? ('Build: ' + sha + ' · ' + ts) : ('Build: ' + sha);
  } catch (_) {
    els.buildReadout.textContent = '';
  }
}

// --------------------------------------------------------------- theme toggle
function applyTheme(dark) {
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  // Show the glyph for the action: sun to switch to light, moon to switch to dark.
  const mark = icon(dark ? 'sun' : 'moon');
  // The theme toggle lives only on the Home weather tile now (#186) — the
  // redundant Settings-card duplicate was removed.
  if (els.weatherThemeBtn) els.weatherThemeBtn.innerHTML = mark;
  localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light');
  restyleEnergyCharts();
  restyleNetworkCharts();
}

function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme !== 'dark');
}

(function initTheme() {
  const stored = localStorage.getItem(THEME_KEY);
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  applyTheme(stored ? stored === 'dark' : prefersDark);
})();

els.weatherThemeBtn.addEventListener('click', toggleTheme);

// ----------------------------------------------------------- nav debug (#300)
(function initNavDebug() {
  installNavDebug();
  if (!els.navDebugBtn) return;
  els.navDebugBtn.setAttribute('aria-pressed', isNavDebugEnabled() ? 'true' : 'false');
  els.navDebugBtn.addEventListener('click', function () {
    const next = !isNavDebugEnabled();
    setNavDebugEnabled(next);
    els.navDebugBtn.setAttribute('aria-pressed', next ? 'true' : 'false');
  });
})();

els.loginForm.addEventListener('submit', async function (ev) {
  ev.preventDefault();
  els.loginError.hidden = true;
  const password = els.loginPassword.value;
  try {
    const res = await fetch('/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password }),
    });
    const body = await res.json().catch(function () { return null; });
    if (!res.ok || !body || !body.token) {
      els.loginError.textContent = (body && body.detail) || 'Login failed';
      els.loginError.hidden = false;
      return;
    }
    writeToken(body.token);
    hideLogin();
    loadUnits();
  } catch (exc) {
    els.loginError.textContent = String(exc.message || exc);
    els.loginError.hidden = false;
  }
});

(function boot() {
  const fromUrl = tokenFromUrl();
  if (fromUrl) writeToken(fromUrl);

  // Lock background scroll whenever a modal opens — iOS Safari scrolls the page
  // behind a <dialog> despite the CSS overflow lock (#214). Patch before any
  // dialog can open.
  installDialogScrollLock();

  wireUnitsControls();
  wireEnergyControls();
  wirePlugsToggle();
  wirePlugsRefresh();
  wirePlugDetail();
  wirePowerNotify();
  wirePcFleet();
  wireLightControls();
  wireZoneDetail();
  wireSecurityHiddenToggle();
  wireSecuritySchedules();
  wireScenePairings();
  wireSecurityOverrides();
  wirePresenceControls();
  wirePresencePlaces();
  wireSecurityNotify();
  wireWakeAlarms();
  wireVm();
  wireVoiceCommands();
  wireCameras();
  wireNetworkControls();
  wireActivity();
  wireHa();
  restoreUnitsSnapshot();
  restoreEnergySnapshots();
  restorePlugsSnapshot();
  restoreUpsSnapshot();
  restoreVmSnapshot();
  restoreLightsSnapshot();
  restoreNetworkSnapshot();
  // Tabs: wireTabs (nav-tabs.js, issue #184) restores the remembered tab and
  // fires this fan-out callback immediately for it, so it must run last —
  // after every wireXControls()/restoreXSnapshot() call above — replacing
  // the old three-step wireTabs() + onTabChange() + setTab(initialTab()).
  // Energy, Plugs, Lights, Network, and Security each adjust their own
  // polling cadence on tab change.
  wireTabs(function (tab) {
    onUnitsTab(tab); onEnergyTab(tab); onPlugsTab(tab); onUpsTab(tab); onPcFleetTab(tab); onVmTab(tab); onHaTab(tab); onLightsTab(tab); onNetworkTab(tab); onSecurityTab(tab); onCamerasTab(tab); onWakeAlarmsTab(tab);
  });

  // AC units only matter on Home (summary tile) and AC (cards), so poll them
  // only while one of those tabs is active rather than every 30s everywhere
  // (#209). wireTabs() above fires the fan-out callback for the default
  // 'home'/'ac' tab immediately, which calls units.js's onUnitsTab ->
  // loadUnits() — so there is no separate boot fetch for units here (unlike
  // loadEnergy() / startWeatherPolling() below, whose tabs' onChange hooks
  // don't fetch on every tab, hence the explicit boot call).
  loadEnergy();
  startWeatherPolling();
  fetchVersion();
  setInterval(fetchVersion, 300_000);
})();
