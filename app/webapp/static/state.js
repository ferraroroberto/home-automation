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
  // Active top-level tab: 'home' | 'ac' | 'energy' | 'plugs'.
  tab: 'home',
  // Active aggregate range on the Energy tab: 'hourly' | 'daily' | 'monthly'.
  range: 'hourly',
  // Live Chart.js instances (created lazily on the Energy tab); kept so the
  // theme toggle can restyle and the live poller can push points.
  liveChart: null,
  aggChart: null,
};

// ----------------------------------------------------------------- DOM
export const THEME_KEY = 'home-automation.theme';
export const TAB_KEY = 'home-automation.tab';

export const els = {
  grid: document.getElementById('unitsGrid'),
  themeBtn: document.getElementById('themeBtn'),
  status: document.getElementById('status'),
  toast: document.getElementById('toast'),
  buildReadout: document.getElementById('buildReadout'),
  // Tabs + panes
  tabHome: document.getElementById('tabHome'),
  tabAc: document.getElementById('tabAc'),
  tabEnergy: document.getElementById('tabEnergy'),
  tabPlugs: document.getElementById('tabPlugs'),
  paneHome: document.getElementById('paneHome'),
  paneAc: document.getElementById('paneAc'),
  paneEnergy: document.getElementById('paneEnergy'),
  panePlugs: document.getElementById('panePlugs'),
  // Plugs (Smart Life) tab
  plugsGrid: document.getElementById('plugsGrid'),
  plugsNote: document.getElementById('plugsNote'),
  // Read-only AC summary (Home tab)
  acSummary: document.getElementById('acSummary'),
  // Energy-flow tile (GET /api/energy), Home tab
  energyFlow: document.getElementById('energyFlow'),
  enPv: document.getElementById('enPv'),
  enHouse: document.getElementById('enHouse'),
  enGrid: document.getElementById('enGrid'),
  enSurplus: document.getElementById('enSurplus'),
  enUpdated: document.getElementById('enUpdated'),
  // Energy tab: hero numbers, charts, range switcher
  heroProd: document.getElementById('heroProd'),
  heroCons: document.getElementById('heroCons'),
  heroNet: document.getElementById('heroNet'),
  liveMeta: document.getElementById('liveMeta'),
  liveChart: document.getElementById('liveChart'),
  aggChart: document.getElementById('aggChart'),
  aggEmpty: document.getElementById('aggEmpty'),
  rangeHourly: document.getElementById('rangeHourly'),
  rangeDaily: document.getElementById('rangeDaily'),
  rangeMonthly: document.getElementById('rangeMonthly'),
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
