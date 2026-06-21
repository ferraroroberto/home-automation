/* Home Automation — shared state, DOM handles, and small primitives.
 *
 * State (single source of truth):
 *   state.units        — [unit dict] from GET /api/units
 *   state.selectedId   — unit_id whose detail modal is open (or null)
 *
 * Auth: a bearer token is stored in localStorage under TOKEN_KEY. The
 * page extracts it from ?token=… on first load (then strips it from the
 * visible URL). On 401, api.js shows the login overlay; the password is
 * swapped for the token via POST /api/login.
 */

'use strict';

export const TOKEN_KEY = 'home-automation.token';

export const state = {
  units: [],
  selectedId: null,
  // Local Tuya / Smart Life device cards from GET /api/tuya.
  plugs: [],
  // device_id whose rename modal is open (or null).
  selectedPlugId: null,
  // When false (default), only cards with has_valid_ip===true are shown.
  // When true, all cards render (including unregistered/no-IP adapters).
  plugsShowAll: false,
  // RISCO alarm state and event log from GET /api/security.
  security: null,
  securityEvents: [],
  // Active top-level tab: 'home' | 'ac' | 'energy' | 'plugs' | 'security'.
  tab: 'home',
  // Active history range on the Energy tab: 'day'|'week'|'month'|'year'|'total'.
  range: 'day',
  // Active range for the Energy-tab cost & savings breakdown table.
  costRange: 'day',
  // Active day for the Energy-tab solar-forecast card: 'yesterday'|'today'|'tomorrow'.
  forecastDay: 'today',
  // Live Chart.js instances (created lazily on the Energy tab); kept so the
  // theme toggle can restyle and the live poller can push points.
  liveChart: null,
  aggChart: null,
  forecastChart: null,
};

// ----------------------------------------------------------------- DOM
export const THEME_KEY = 'home-automation.theme';
export const TAB_KEY = 'home-automation.tab';
export const PLUGS_SHOW_ALL_KEY = 'home-automation.plugsShowAll';

export const els = {
  grid: document.getElementById('unitsGrid'),
  themeBtn: document.getElementById('themeBtn'),
  settingsCard: document.getElementById('settingsCard'),
  toast: document.getElementById('toast'),
  buildReadout: document.getElementById('buildReadout'),
  // Tabs + panes
  tabHome: document.getElementById('tabHome'),
  tabAc: document.getElementById('tabAc'),
  tabEnergy: document.getElementById('tabEnergy'),
  tabPlugs: document.getElementById('tabPlugs'),
  tabSecurity: document.getElementById('tabSecurity'),
  paneHome: document.getElementById('paneHome'),
  paneAc: document.getElementById('paneAc'),
  paneEnergy: document.getElementById('paneEnergy'),
  panePlugs: document.getElementById('panePlugs'),
  paneSecurity: document.getElementById('paneSecurity'),
  // Security (RISCO alarm) tab
  securityState: document.getElementById('securityState'),
  securityActions: document.getElementById('securityActions'),
  // Alarm controls mirrored onto the Home tab (actionable).
  homeSecurityState: document.getElementById('homeSecurityState'),
  homeSecurityActions: document.getElementById('homeSecurityActions'),
  securityEvents: document.getElementById('securityEvents'),
  securityEventsNote: document.getElementById('securityEventsNote'),
  securityZones: document.getElementById('securityZones'),
  securityZonesNote: document.getElementById('securityZonesNote'),
  // Plugs (Smart Life) tab
  plugsGrid: document.getElementById('plugsGrid'),
  plugsNote: document.getElementById('plugsNote'),
  plugsToggleBtn: document.getElementById('plugsToggleBtn'),
  plugsHiddenCount: document.getElementById('plugsHiddenCount'),
  // Plugs summary stats
  plugsStats: document.getElementById('plugsStats'),
  plugStatTotal: document.getElementById('plugStatTotal'),
  plugStatOn: document.getElementById('plugStatOn'),
  plugStatOff: document.getElementById('plugStatOff'),
  plugStatWatts: document.getElementById('plugStatWatts'),
  // Plug summary mirrored onto the Home tab (informative).
  homePlugsStats: document.getElementById('homePlugsStats'),
  homePlugStatTotal: document.getElementById('homePlugStatTotal'),
  homePlugStatOn: document.getElementById('homePlugStatOn'),
  homePlugStatOff: document.getElementById('homePlugStatOff'),
  homePlugStatWatts: document.getElementById('homePlugStatWatts'),
  // Plug rename modal
  plugDialog: document.getElementById('plugDialog'),
  plugDetailName: document.getElementById('plugDetailName'),
  plugDisplayName: document.getElementById('plugDisplayName'),
  plugDetailClose: document.getElementById('plugDetailClose'),
  // Read-only AC summary (Home tab)
  acSummary: document.getElementById('acSummary'),
  // Energy-flow card (GET /api/energy), Home tab — same view as the Energy tab.
  homeEnergyFlow: document.getElementById('homeEnergyFlow'),
  homeFlowTime: document.getElementById('homeFlowTime'),
  homeFlowPv: document.getElementById('homeFlowPv'),
  homeFlowGrid: document.getElementById('homeFlowGrid'),
  homeFlowHouse: document.getElementById('homeFlowHouse'),
  homeFlowNodePv: document.getElementById('homeFlowNodePv'),
  homeWirePv: document.getElementById('homeWirePv'),
  homeWireGrid: document.getElementById('homeWireGrid'),
  // Home-tab weather tile (GET /api/weather) + its inline theme toggle
  weatherTile: document.getElementById('weatherTile'),
  weatherThemeBtn: document.getElementById('weatherThemeBtn'),
  wxNowIcon: document.getElementById('wxNowIcon'),
  wxNowTemp: document.getElementById('wxNowTemp'),
  wxFcIcon: document.getElementById('wxFcIcon'),
  wxFcMin: document.getElementById('wxFcMin'),
  wxFcMax: document.getElementById('wxFcMax'),
  // Energy tab: flow diagram (live)
  flowTime: document.getElementById('flowTime'),
  flowPv: document.getElementById('flowPv'),
  flowGrid: document.getElementById('flowGrid'),
  flowHouse: document.getElementById('flowHouse'),
  flowNodePv: document.getElementById('flowNodePv'),
  flowNodeGrid: document.getElementById('flowNodeGrid'),
  flowNodeHouse: document.getElementById('flowNodeHouse'),
  wirePv: document.getElementById('wirePv'),
  wireGrid: document.getElementById('wireGrid'),
  // Energy tab: live efficiency tiles
  liveSelfSuff: document.getElementById('liveSelfSuff'),
  liveSelfCons: document.getElementById('liveSelfCons'),
  // Energy tab: today's split cards
  genTotal: document.getElementById('genTotal'),
  genSelf: document.getElementById('genSelf'),
  genFeed: document.getElementById('genFeed'),
  genBar: document.getElementById('genBar'),
  genPct: document.getElementById('genPct'),
  consTotal: document.getElementById('consTotal'),
  consSelf: document.getElementById('consSelf'),
  consGrid: document.getElementById('consGrid'),
  consBar: document.getElementById('consBar'),
  consPct: document.getElementById('consPct'),
  // Energy tab: savings
  savEur: document.getElementById('savEur'),
  savCo2: document.getElementById('savCo2'),
  savTrees: document.getElementById('savTrees'),
  // Energy tab: charts, range switcher
  liveMeta: document.getElementById('liveMeta'),
  liveChart: document.getElementById('liveChart'),
  aggChart: document.getElementById('aggChart'),
  aggEmpty: document.getElementById('aggEmpty'),
  // History range buttons (Day / Week / Month / Year / Σ) — driven by data-range.
  rangeBtns: Array.from(document.querySelectorAll('#aggRange .range-tab')),
  // Energy tab: cost & savings breakdown
  costMeta: document.getElementById('costMeta'),
  costBody: document.getElementById('costBody'),
  costFoot: document.getElementById('costFoot'),
  costSummary: document.getElementById('costSummary'),
  costEmpty: document.getElementById('costEmpty'),
  costNote: document.getElementById('costNote'),
  costRangeBtns: Array.from(document.querySelectorAll('#costRange .range-tab')),
  // Energy tab: solar-forecast card
  forecastMeta: document.getElementById('forecastMeta'),
  forecastHeadline: document.getElementById('forecastHeadline'),
  forecastParams: document.getElementById('forecastParams'),
  forecastChart: document.getElementById('forecastChart'),
  forecastEmpty: document.getElementById('forecastEmpty'),
  forecastDayBtns: Array.from(document.querySelectorAll('#forecastDay .range-tab')),
  // Detail modal
  detail: document.getElementById('detailDialog'),
  detailName: document.getElementById('detailName'),
  detailDisplayName: document.getElementById('detailDisplayName'),
  detailMode: document.getElementById('detailMode'),
  detailVaneVertical: document.getElementById('detailVaneVertical'),
  detailVaneHorizontal: document.getElementById('detailVaneHorizontal'),
  detailVaneVerticalRow: document.getElementById('detailVaneVerticalRow'),
  detailVaneHorizontalRow: document.getElementById('detailVaneHorizontalRow'),
  detailClose: document.getElementById('detailClose'),
  // Login overlay
  loginOverlay: document.getElementById('loginOverlay'),
  loginForm: document.getElementById('loginForm'),
  loginPassword: document.getElementById('loginPassword'),
  loginError: document.getElementById('loginError'),
};

// ----------------------------------------------------------- auth utils
export function tokenFromUrl() {
  const params = new URLSearchParams(window.location.search);
  const t = (params.get('token') || '').trim();
  if (!t) return null;
  params.delete('token');
  const q = params.toString();
  const newUrl =
    window.location.pathname + (q ? '?' + q : '') + window.location.hash;
  window.history.replaceState({}, '', newUrl);
  return t;
}
export function readToken() {
  return localStorage.getItem(TOKEN_KEY) || '';
}
export function writeToken(t) {
  if (t) localStorage.setItem(TOKEN_KEY, t);
}
export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

// ----------------------------------------------------------- toasts
let toastTimer = null;
export function toast(msg, kind) {
  els.toast.textContent = msg;
  els.toast.className = 'toast ' + (kind || '');
  els.toast.hidden = false;
  if (toastTimer) clearTimeout(toastTimer);
  toastTimer = setTimeout(function () {
    els.toast.hidden = true;
  }, kind === 'error' ? 4500 : 2000);
}

// ------------------------------------------------------ mode presentation
const MODE_ICONS = {
  Heat: '🔥',
  Cool: '❄️',
  Automatic: '🔄',
  Dry: '💧',
  Fan: '🌀',
};
export function modeIcon(mode) {
  return MODE_ICONS[mode] || '🌡';
}
