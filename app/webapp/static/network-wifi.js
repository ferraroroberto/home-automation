/* Network tab — Wi-Fi diagnostics + the channel charts + the Wi-Fi rename modal.
 *
 * Split out of network.js (issue #197): the visible-radio list (grouped by band,
 * strongest first), the 2.4/5 GHz channel-occupancy charts, the recommendations
 * strip, the show-hidden toggle with its localStorage pref, and the per-radio
 * rename / hide detail modal. The boot module (network.js) owns renderNetwork and
 * calls renderWifi here; this module calls back into renderNetwork after a write.
 */

'use strict';

import {
  state,
  els,
  toast,
  NETWORK_SHOW_HIDDEN_WIFI_KEY,
} from './state.js';
import { jsonApi } from './api.js';
import {
  createWifiChannelChart,
  setWifiChannelData,
} from './charts.js';
import { renderNetwork } from './network.js';
import { toggleMarkup } from './toggle.js';

const WIFI_BAND_LABELS = { '2.4GHz': '2.4 GHz', '5GHz': '5 GHz', '6GHz': '6 GHz' };

function bandLabel(band) {
  return WIFI_BAND_LABELS[band] || band || '—';
}

function wifiLabel(b) {
  return b.display_name || b.ssid || '(hidden)';
}

function wifiId(b) {
  if (!b) return '';
  return b.wifi_id || b.bssid || (b.ssid ? 'SSID:' + b.ssid : '');
}

function wifiSignalClass(signal) {
  if (signal == null) return '';
  if (signal < 60) return ' is-weak';
  if (signal >= 80) return ' is-strong';
  return '';
}

function wifiSummary(wifi) {
  if (!wifi || !wifi.available) return '';
  const parts = [];
  if (wifi.current_ssid) parts.push(wifi.current_ssid);
  if (wifi.current_band) parts.push(bandLabel(wifi.current_band));
  if (wifi.current_channel != null) parts.push('ch ' + wifi.current_channel);
  return parts.join(' · ');
}

function ensureWifiCharts() {
  if (!els.netWifiChart24 || !els.netWifiChart5) return;
  if (!state.wifiChart24) state.wifiChart24 = createWifiChannelChart(els.netWifiChart24, '2.4GHz');
  if (!state.wifiChart5) state.wifiChart5 = createWifiChannelChart(els.netWifiChart5, '5GHz');
}

function renderWifiRecommendations(items) {
  const list = items || [];
  els.netWifiRecommendations.innerHTML = '';
  if (!list.length) {
    els.netWifiRecommendations.hidden = true;
    return;
  }
  list.forEach(function (text) {
    const row = document.createElement('div');
    row.className = 'net-wifi-tip';
    row.textContent = text;
    els.netWifiRecommendations.appendChild(row);
  });
  els.netWifiRecommendations.hidden = false;
}

function wifiRow(b) {
  const hidden = !!b.hidden;
  const row = document.createElement('div');
  row.className = 'net-wifi-row' + (b.connected ? ' is-current' : '') +
    wifiSignalClass(b.signal);
  if (hidden) row.classList.add('is-hidden');

  const main = document.createElement('div');
  main.className = 'net-wifi-row-main';
  const name = document.createElement('button');
  name.type = 'button';
  name.className = 'net-wifi-row-name';
  name.title = 'Wi-Fi details · rename';
  name.textContent = wifiLabel(b);
  name.addEventListener('click', function () { openNetWifiDetail(wifiId(b)); });
  main.appendChild(name);
  if (b.connected) {
    const pill = document.createElement('span');
    pill.className = 'net-wifi-current';
    pill.textContent = 'current';
    main.appendChild(pill);
  }
  const meta = document.createElement('span');
  meta.className = 'net-wifi-row-meta';
  const metaBits = [bandLabel(b.band)];
  if (b.channel != null) metaBits.push('ch ' + b.channel);
  if (b.radio_type) metaBits.push(b.radio_type);
  if (b.authentication) metaBits.push(b.authentication);
  meta.textContent = metaBits.filter(Boolean).join(' · ');
  main.appendChild(meta);
  row.appendChild(main);

  const sig = document.createElement('span');
  sig.className = 'net-device-signal net-wifi-row-signal';
  if (b.signal != null) {
    const bar = document.createElement('span');
    bar.className = 'net-signal-bar';
    const fill = document.createElement('span');
    fill.className = 'net-signal-fill';
    fill.style.width = Math.max(0, Math.min(100, b.signal)) + '%';
    bar.appendChild(fill);
    sig.appendChild(bar);
    const pct = document.createElement('span');
    pct.className = 'net-signal-pct';
    pct.textContent = b.signal + '%';
    sig.appendChild(pct);
  } else {
    sig.textContent = '—';
  }
  row.appendChild(sig);

  const mac = document.createElement('span');
  mac.className = 'net-wifi-row-mac';
  mac.textContent = b.bssid || '—';
  row.appendChild(mac);
  return row;
}

function renderWifiHiddenToggle(hiddenCount) {
  if (els.netWifiHiddenCount) {
    els.netWifiHiddenCount.hidden = hiddenCount === 0;
    els.netWifiHiddenCount.textContent = hiddenCount + ' hidden';
  }
  if (!els.netWifiHiddenToggle) return;
  els.netWifiHiddenToggle.hidden = hiddenCount === 0;
  els.netWifiHiddenToggle.textContent = state.networkShowHiddenWifi ? 'Hide' : 'Show hidden';
  els.netWifiHiddenToggle.classList.toggle('active', state.networkShowHiddenWifi);
  els.netWifiHiddenToggle.setAttribute('aria-pressed', state.networkShowHiddenWifi ? 'true' : 'false');
}

function renderWifiList(bssids) {
  const all = bssids || [];
  const hiddenCount = all.filter(function (b) { return !!b.hidden; }).length;
  renderWifiHiddenToggle(hiddenCount);
  const list = all.filter(function (b) {
    return state.networkShowHiddenWifi || !b.hidden;
  }).slice().sort(function (a, b) {
    const band = bandLabel(a.band).localeCompare(bandLabel(b.band));
    if (band !== 0) return band;
    return (b.signal || 0) - (a.signal || 0);
  });
  els.netWifiList.innerHTML = '';
  if (!list.length) {
    els.netWifiNote.hidden = false;
    els.netWifiNote.textContent = hiddenCount
      ? 'All Wi-Fi radios are hidden.'
      : 'No visible Wi-Fi radios reported by this PC.';
    return;
  }
  els.netWifiNote.hidden = true;
  list.forEach(function (b) { els.netWifiList.appendChild(wifiRow(b)); });
}

export function renderWifi(wifi) {
  const available = !!(wifi && wifi.available);
  const signal = wifi ? wifi.current_signal : null;
  els.netWifiStatus.textContent = signal != null ? signal + '%' : '';
  els.netWifiStatus.className = 'net-wifi-status' + wifiSignalClass(signal);
  els.netWifiSummary.textContent = wifiSummary(wifi);

  if (!wifi) {
    els.netWifiMeta.textContent = '—';
    els.netWifiRecommendations.hidden = true;
    els.netWifiList.innerHTML = '';
    renderWifiHiddenToggle(0);
    return;
  }

  const meta = [];
  if (wifi.interface_name) meta.push(wifi.interface_name);
  if (wifi.adapter_description && wifi.adapter_description !== wifi.interface_name) {
    meta.push(wifi.adapter_description);
  }
  if (wifi.current_radio_type) meta.push(wifi.current_radio_type);
  els.netWifiMeta.textContent = meta.length ? meta.join(' · ') : (wifi.error || 'Wi-Fi scan unavailable.');

  if (!available) {
    renderWifiRecommendations([]);
    els.netWifiList.innerHTML = '';
    renderWifiHiddenToggle(0);
    els.netWifiNote.hidden = false;
    els.netWifiNote.textContent = wifi.error || 'Wi-Fi diagnostics are unavailable on this PC.';
    if (state.wifiChart24) setWifiChannelData(state.wifiChart24, []);
    if (state.wifiChart5) setWifiChannelData(state.wifiChart5, []);
    return;
  }

  ensureWifiCharts();
  const bssids = wifi.bssids || [];
  const chartBssids = bssids.filter(function (b) {
    return state.networkShowHiddenWifi || !b.hidden;
  });
  if (state.wifiChart24) {
    setWifiChannelData(
      state.wifiChart24,
      chartBssids.filter(function (b) { return b.band === '2.4GHz'; })
    );
  }
  if (state.wifiChart5) {
    setWifiChannelData(
      state.wifiChart5,
      chartBssids.filter(function (b) { return b.band === '5GHz'; })
    );
  }
  renderWifiRecommendations(wifi.recommendations || []);
  renderWifiList(bssids);
}

// ------------------------------------------------- Wi-Fi detail + rename
function wifiById(targetWifiId) {
  const list = (state.network && state.network.wifi && state.network.wifi.bssids) || [];
  return list.find(function (b) { return targetWifiId === wifiId(b); }) || null;
}

function renderNetWifiHiddenToggle(b) {
  const btn = els.netWifiHiddenDetailToggle;
  if (!btn) return;
  const on = !!b.hidden;
  btn.className = 'toggle' + (on ? ' on' : ' off');
  btn.setAttribute('aria-checked', on ? 'true' : 'false');
  btn.innerHTML = toggleMarkup(on);
}

function openNetWifiDetail(wifiId) {
  const b = wifiById(wifiId);
  if (!b) return;
  state.selectedNetWifiId = wifiId;
  els.netWifiDetailName.textContent = wifiLabel(b);
  els.netWifiDetailStatus.textContent = b.connected ? 'Current network' : 'Visible';
  els.netWifiDetailBand.textContent = bandLabel(b.band);
  els.netWifiDetailChannel.textContent = b.channel != null ? 'ch ' + b.channel : '—';
  els.netWifiDetailSignal.textContent = b.signal != null ? b.signal + '%' : '—';
  els.netWifiDetailSecurity.textContent = [b.authentication, b.encryption].filter(Boolean).join(' · ') || '—';
  els.netWifiDisplayName.value = b.display_name || '';
  els.netWifiDisplayName.placeholder = b.ssid || 'Custom label…';
  els.netWifiOriginalName.textContent = 'Original SSID: ' + (b.original_name || b.ssid || '—') +
    ' · BSSID: ' + (b.bssid || '—') + (b.bssid ? '' : ' · key ' + (b.wifi_id || '—'));
  renderNetWifiHiddenToggle(b);
  if (typeof els.netWifiDialog.showModal === 'function') els.netWifiDialog.showModal();
  else els.netWifiDialog.setAttribute('open', '');
  els.netWifiDisplayName.focus();
}

function closeNetWifiDetail() {
  state.selectedNetWifiId = null;
  if (typeof els.netWifiDialog.close === 'function') els.netWifiDialog.close();
  else els.netWifiDialog.removeAttribute('open');
}

async function saveNetWifiName() {
  const targetWifiId = state.selectedNetWifiId;
  if (!targetWifiId) return;
  const newName = els.netWifiDisplayName.value.trim();
  try {
    await jsonApi('/api/network/wifi/display_name', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ wifi_id: targetWifiId, display_name: newName }),
    });
    if (state.network && state.network.wifi && Array.isArray(state.network.wifi.bssids)) {
      state.network.wifi.bssids = state.network.wifi.bssids.map(function (b) {
        return targetWifiId === wifiId(b) ? Object.assign({}, b, { display_name: newName || null }) : b;
      });
    }
    const b = wifiById(targetWifiId);
    if (b) els.netWifiDetailName.textContent = wifiLabel(b);
    renderNetwork();
    toast('Name saved', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to save name: ' + (exc.message || exc), 'error');
    }
  }
}

async function toggleWifiHidden() {
  const targetWifiId = state.selectedNetWifiId;
  if (!targetWifiId) return;
  const b = wifiById(targetWifiId);
  if (!b) return;
  const next = !b.hidden;
  try {
    await jsonApi('/api/network/wifi/hidden', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ wifi_id: targetWifiId, hidden: next }),
    });
    if (state.network && state.network.wifi && Array.isArray(state.network.wifi.bssids)) {
      state.network.wifi.bssids = state.network.wifi.bssids.map(function (x) {
        return targetWifiId === wifiId(x) ? Object.assign({}, x, { hidden: next }) : x;
      });
    }
    renderNetWifiHiddenToggle(wifiById(targetWifiId) || b);
    renderNetwork();
    toast(next ? 'Hidden' : 'Restored', 'success');
  } catch (exc) {
    if (String(exc.message) !== 'auth required') {
      toast('Failed to update: ' + (exc.message || exc), 'error');
    }
  }
}

export function wireNetWifiDetail() {
  if (!els.netWifiDialog) return;
  els.netWifiDetailClose.addEventListener('click', closeNetWifiDetail);
  els.netWifiDialog.addEventListener('click', function (ev) {
    if (ev.target === els.netWifiDialog) closeNetWifiDetail();
  });
  els.netWifiDisplayName.addEventListener('blur', saveNetWifiName);
  els.netWifiDisplayName.addEventListener('keydown', function (ev) {
    if (ev.key === 'Enter') { ev.preventDefault(); els.netWifiDisplayName.blur(); }
  });
  if (els.netWifiHiddenDetailToggle) {
    els.netWifiHiddenDetailToggle.addEventListener('click', toggleWifiHidden);
  }
}

// ------------------------------------------------- prefs + toggles
export function toggleShowHiddenWifi(ev) {
  if (ev) {
    ev.preventDefault();
    ev.stopPropagation();
  }
  state.networkShowHiddenWifi = !state.networkShowHiddenWifi;
  try {
    localStorage.setItem(NETWORK_SHOW_HIDDEN_WIFI_KEY, state.networkShowHiddenWifi ? '1' : '0');
  } catch (_e) { /* private mode — in-memory only */ }
  renderNetwork();
}

export function initShowHiddenWifiPref() {
  try {
    state.networkShowHiddenWifi = localStorage.getItem(NETWORK_SHOW_HIDDEN_WIFI_KEY) === '1';
  } catch (_e) {
    state.networkShowHiddenWifi = false;
  }
}
