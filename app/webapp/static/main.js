/* Home Automation — entry module: boots the dashboard and wires events.
 *
 * Loaded by index.html as <script type="module">. Thin orchestrator
 * (the same pattern as security.js/network.js): owns boot wiring, the theme toggle, nav-debug init, and the sign-in hook.
 * The AC rows (Home and the AC tab, one shared row) and the unit sheet
 * (setpoint, power, mode, fan, vanes, temperature rule, schedules, name)
 * live in ./units.js — each command there hits POST /api/units/{id} and
 * re-renders the rows from the read-back response.
 */

'use strict';

import {
  els,
  readToken,
  tokenFromUrl,
  writeToken,
  THEME_KEY,
} from './state.js';
import { icon } from './_vendored/icons/icons.js';
import { bindTextSize } from './_vendored/text-size/text-size.js';
import { buildReadoutText } from './_vendored/page-foot/page-foot.js';
import { initUsageLog } from './_vendored/usage-log/usage-log.js';
import { jsonApi, setSignInHandler } from './api.js';
import { wireTabs } from './tabs.js';
import { wireSettings } from './settings.js';
import { installNavDebug, isNavDebugEnabled, setNavDebugEnabled } from './nav-debug.js';
import { setToggleState, wireToggle } from './toggle.js';
import {
  loadUnits,
  restoreUnitsSnapshot,
  onUnitsTab,
  wireUnitsControls,
} from './devices/units.js';
import {
  loadEnergy,
  wireEnergyControls,
  onEnergyTab,
  restyleEnergyCharts,
  restoreEnergySnapshots,
} from './energy/energy.js';
import { onPlugsTab, wirePlugsPair, wireBlindsGroup, wirePlugsToggle, wirePlugDetail, restorePlugsSnapshot } from './devices/plugs.js';
import { onCircuitsTab, wireCircuitsToggle, wireCircuitDetail } from './energy/circuits.js';
import { onUpsTab, restoreUpsSnapshot } from './energy/ups.js';
import { wirePowerNotify } from './energy/ups-notify.js';
import { wirePcFleet, onPcFleetTab } from './devices/pc-fleet.js';
import { onVmTab, restoreVmSnapshot, wireVm } from './devices/vm.js';
import { onTeslaTab } from './devices/tesla.js';
import { restyleTeslaHistoryCharts, wireTeslaHistory } from './devices/tesla-history.js';
import { onSearxngTab, wireSearxng } from './searxng.js';
import { onHaTab, wireHa } from './ha.js';
import { onLightsTab, wireLightControls, restoreLightsSnapshot } from './devices/lights.js';
import { onBlindSchedulesTab, wireBlindSchedules } from './devices/blind-schedules.js';
import { onSecurityTab, wireSecuritySheets, wireZoneDetail, wireSecurityHiddenToggle, wireSecuritySchedules, wireScenePairings, wireSecurityOverrides, wirePresenceControls, wirePresencePlaces, wireSecurityNotify } from './security/security.js';
import { onWakeAlarmsTab, wireWakeAlarms } from './wake-alarms.js';
import { onRemindersTab, wireReminders } from './reminders.js';
import { wireVoiceCommands } from './voice-commands.js';
import { onCamerasTab, wireCameras } from './devices/cameras.js';
import { onNetworkTab, wireNetworkControls, restyleNetworkCharts, restoreNetworkSnapshot } from './network/network.js';
import { startWeatherPolling } from './energy/weather.js';
import { wireActivity } from './activity.js';
import { installDialogScrollLock } from './scroll-lock.js';
import { wireConfirmDialog } from './confirm.js';

// --------------------------------------------------- build identity
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
    els.buildReadout.textContent = buildReadoutText(sha, body.built_at || '');
  } catch (_) {
    els.buildReadout.textContent = '';
  }
}

// --------------------------------------------------------------- theme toggle
function applyTheme(dark) {
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  // Show the glyph for the action: sun to switch to light, moon to switch to dark.
  const mark = icon(dark ? 'sun' : 'moon');
  // One toggle per page header, so the glyph is the same on every tab.
  els.themeToggleBtns.forEach(function (btn) { btn.innerHTML = mark; });
  localStorage.setItem(THEME_KEY, dark ? 'dark' : 'light');
  restyleEnergyCharts();
  restyleNetworkCharts();
  restyleTeslaHistoryCharts();
}

function toggleTheme() {
  applyTheme(document.documentElement.dataset.theme !== 'dark');
}

(function initTheme() {
  const stored = localStorage.getItem(THEME_KEY);
  const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  applyTheme(stored ? stored === 'dark' : prefersDark);
})();

els.themeToggleBtns.forEach(function (btn) { btn.addEventListener('click', toggleTheme); });

// ------------------------------------------------------- text size
// The pre-paint stamp is the inline boot script in index.html; this wires the
// Settings control (vendored, prefix = the same one the theme key uses).
bindTextSize(document.getElementById('textSizeControl'), 'home-automation');

// ----------------------------------------------------------- nav debug
(function initNavDebug() {
  installNavDebug();
  if (!els.navDebugBtn) return;
  // A Settings switch: Diagnostics › Record navigation log.
  setToggleState(els.navDebugBtn, isNavDebugEnabled());
  wireToggle(els.navDebugBtn, setNavDebugEnabled);
})();

// A successful sign-in (the vendored sheet stores the token via api.js) reloads
// the dashboard's units.
setSignInHandler(loadUnits);

(function boot() {
  const fromUrl = tokenFromUrl();
  if (fromUrl) writeToken(fromUrl);

  // Local-only usage log: which tabs and
  // [data-track] controls get used, posted to this app's own /api/usage. The
  // token is read per send, like api(), since a phone off loopback needs it.
  const usage = initUsageLog({
    headers: function () {
      const token = readToken();
      return token ? { Authorization: 'Bearer ' + token } : {};
    },
  });

  // Lock background scroll whenever a modal opens — iOS Safari scrolls the page
  // behind a <dialog> despite the CSS overflow lock. Patch before any
  // dialog can open.
  installDialogScrollLock();

  // The shared confirm dialog is used from every tab and from dense-editor.js,
  // so it is wired at boot rather than by whichever feature tab happens to own
  // it — the layering confirm.js exists for.
  wireConfirmDialog();
  wireSettings();
  wireUnitsControls();
  wireEnergyControls();
  wirePlugsToggle();
  wirePlugsPair();
  wireBlindsGroup();
  wireBlindSchedules();
  wirePlugDetail();
  wireCircuitsToggle();
  wireCircuitDetail();
  wirePowerNotify();
  wirePcFleet();
  wireTeslaHistory();
  wireLightControls();
  wireZoneDetail();
  wireSecurityHiddenToggle();
  wireSecuritySheets();
  wireSecuritySchedules();
  wireScenePairings();
  wireSecurityOverrides();
  wirePresenceControls();
  wirePresencePlaces();
  wireSecurityNotify();
  wireWakeAlarms();
  wireReminders();
  wireVm();
  wireSearxng();
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
  // Tabs: wireTabs (nav-tabs.js) restores the remembered tab and
  // fires this fan-out callback immediately for it, so it must run last —
  // after every wireXControls()/restoreXSnapshot() call above — replacing
  // the old three-step wireTabs() + onTabChange() + setTab(initialTab()).
  // Energy, Plugs, Lights, Network, and Security each adjust their own
  // polling cadence on tab change.
  wireTabs(function (tab) {
    // dynamic-id-exempt: `tab` is one of the fixed nav tab keys, never user content
    usage.trackView(tab);
    onUnitsTab(tab); onEnergyTab(tab); onPlugsTab(tab); onCircuitsTab(tab); onUpsTab(tab); onPcFleetTab(tab); onVmTab(tab); onTeslaTab(tab); onSearxngTab(tab); onHaTab(tab); onLightsTab(tab); onBlindSchedulesTab(tab); onNetworkTab(tab); onSecurityTab(tab); onCamerasTab(tab); onWakeAlarmsTab(tab); onRemindersTab(tab);
  });

  // AC units only matter on Home and AC (their rows), so poll them
  // only while one of those tabs is active rather than every 30s everywhere.
  // wireTabs() above fires the fan-out callback for the default
  // 'home'/'ac' tab immediately, which calls units.js's onUnitsTab ->
  // loadUnits() — so there is no separate boot fetch for units here (unlike
  // loadEnergy() / startWeatherPolling() below, whose tabs' onChange hooks
  // don't fetch on every tab, hence the explicit boot call).
  loadEnergy();
  startWeatherPolling();
  fetchVersion();
  setInterval(fetchVersion, 300_000);
})();
