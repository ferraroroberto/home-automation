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
  // Athom CT-clamp meters (one entry per meter, each with all its channels)
  // from GET /api/circuits (issue #25).
  circuits: [],
  // Discovery/read problem text for the Circuits card, kept separate from an
  // empty meter list: "mDNS could not run" and "no meters here" differ (#25).
  circuitsError: '',
  // Channel key ("<meter_id>:<channel>") whose rename modal is open, or null.
  selectedCircuitKey: null,
  // Local USB UPS state from GET /api/ups.
  ups: null,
  // Home Assistant Hyper-V VM state from GET /api/hyperv (issue #240).
  vm: null,
  // HA-owned Voice PE rooms + recent interactions from GET /api/ha (#239).
  ha: null,
  // SearXNG container status from GET /api/searxng (issue #321).
  searxng: null,
  // Elgato light cards from GET /api/lights.
  lights: [],
  // device_id whose rename modal is open (or null).
  selectedPlugId: null,
  // When true (default), all source-visible devices render, including no-IP
  // adapters. When false, only cards with has_valid_ip===true are shown.
  plugsShowAll: true,
  // When false (default), user-hidden plugs/blinds are filtered out; the
  // "Show hidden" toggle reveals them (mirrors the Network device list).
  plugsShowHidden: false,
  // RISCO alarm state and event log from GET /api/security.
  security: null,
  securityEvents: [],
  securitySchedules: [],
  // Detector→camera+preset alarm-scene capture pairings (issue #162). The
  // camera list for the pairing editor's dropdowns reuses the existing
  // `cameras` state above (managed by cameras.js).
  scenePairings: [],
  // Per-detector "auto-bypass after N repeats this armed session" rules
  // (issue #341) from GET /api/security/overrides.
  securityOverrides: [],
  // When false (default), detectors marked hidden are filtered out of the list.
  // When true, they render (dimmed) so they can be un-hidden (issue #104).
  securityShowHidden: false,
  // iCloud Find My presence spike from GET /api/presence.
  presence: null,
  thisDevicePresence: null,
  presencePlaces: {},
  // Configured named places for the voice locator (issue #438), from
  // GET/PUT /api/presence/places. Distinct from presencePlaces above (that's
  // the reverse-geocode cache keyed by coordinates).
  presencePlacesList: [],
  location: null,
  presenceAutomation: null,
  presenceShowHidden: false,
  selectedPresenceId: null,
  // Elgato light id whose detail/rename modal is open (or null).
  selectedLightId: null,
  // zone id whose detector detail/rename modal is open (or null).
  selectedZoneId: null,
  // Camera cards from GET /api/cameras (issue #161).
  cameras: [],
  // camera id whose detail/live modal is open (or null).
  selectedCameraId: null,
  // PTZ d-pad mode (issue #190): 'step' = one click → one fixed nudge (precise),
  // 'hold' = press-and-hold continuous move.
  cameraPtzMode: 'step',
  // Saved presets for the camera in the open live modal.
  cameraPresets: [],
  // Home-network (LAN) snapshot from GET /api/network (issue #129).
  network: null,
  // Browser-restored API snapshots keyed by allowlisted scope (issue #148).
  snapshotRestored: {},
  snapshotUpdatedAt: {},
  // MAC of the device whose detail/rename modal is open (or null).
  selectedNetDeviceMac: null,
  // Wi-Fi identity whose detail/rename modal is open (or null).
  selectedNetWifiId: null,
  // Hidden Network rows render dimmed only when these filters are active.
  networkShowHiddenDevices: false,
  networkShowHiddenWifi: false,
  // When false (default), offline (known-but-absent) devices are hidden; when
  // true they render dimmed in a trailing "Offline" group (issue #129 Phase 4).
  networkShowOffline: false,
  // Wi-Fi walk test (issue #547): GET /api/network/survey payload, and the MAC
  // of the device being surveyed (the phone you are walking around with).
  networkSurvey: null,
  networkSurveyMac: null,
  // Device row sort inside each Network group: A-Z by default, or weakest signal.
  networkDeviceSort: 'az',
  // How the Network device list is grouped: by radio band (the original view)
  // or by the user's own device groups (issue #513).
  networkDeviceGrouping: 'band',
  // Wi-Fi diagnostics channel charts on the Network tab.
  wifiChart24: null,
  wifiChart5: null,
  // Active top-level tab: 'home' | 'ac' | 'energy' | 'iot' | 'network' | 'security'.
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
  // Sun-position diagnostic (#590): the day on screen (ISO, local) and its
  // chart. The card is folded away by default and loads on first open, so both
  // stay null/empty until someone actually asks the question.
  sunOverlayDate: '',
  sunOverlayChart: null,
  // PV array the forecast is computed from, from GET/PUT /api/energy/pv-system
  // (issue #561). Rows carry a positional client-side id for the dense-editor;
  // the file itself stores a plain list.
  pvArrays: [],
  pvPerformanceRatio: 0.8,
  // Horizon/shading profile points (issue #578 part b) — same GET/PUT
  // endpoint, own dense-editor bodyKey. Not yet applied to the forecast; the
  // switch that arms it has no editor control.
  pvHorizonProfile: [],
  // Fleet solar-boost sequencing knobs, from GET/PUT
  // /api/hvac/boost-coordinator (issue #562). Seconds/watts on the wire exactly
  // as stored; only the settle interval is *rendered* in minutes.
  boostCoord: null,
  // Wake alarms (recurring/one-shot) from GET /api/wake-alarms, and app-native
  // countdown timers from GET /api/wake-timers (issue #304). Distinct from the
  // RISCO `security` alarm above — these ring/notify, they don't arm/disarm.
  wakeAlarms: [],
  wakeTimers: [],
  // Free-text reminders from GET /api/reminders (issue #314) — bidirectional
  // voice/app sync, distinct from wakeAlarms above (these are a checklist,
  // not a fire-at-a-time alert).
  reminders: [],
  voiceCommands: [],
  // Cheat-sheet language filter (#466): 'all' | 'en' | 'es'.
  voiceLang: 'all',
};

// ----------------------------------------------------------------- DOM
export const THEME_KEY = 'home-automation.theme';
export const TAB_KEY = 'home-automation.tab';
export const PLUGS_SHOW_ALL_KEY = 'home-automation.plugsShowAll';
export const PLUGS_SHOW_HIDDEN_KEY = 'home-automation.plugsShowHidden';
export const SECURITY_SHOW_HIDDEN_KEY = 'home-automation.securityShowHidden';
export const PRESENCE_SHOW_HIDDEN_KEY = 'home-automation.presenceShowHidden';
export const NETWORK_SHOW_OFFLINE_KEY = 'home-automation.networkShowOffline';
export const NETWORK_DEVICE_SORT_KEY = 'home-automation.networkDeviceSort';
export const NETWORK_DEVICE_GROUPING_KEY = 'home-automation.networkDeviceGrouping';
export const NETWORK_SHOW_HIDDEN_DEVICES_KEY = 'home-automation.networkShowHiddenDevices';
export const NETWORK_SHOW_HIDDEN_WIFI_KEY = 'home-automation.networkShowHiddenWifi';
// Which attached device the walk test profiles (issue #547). Per-browser rather
// than server-side on purpose: over Tailscale every client arrives from a 100.x
// address, so the server cannot map a browser to a LAN MAC — each device that
// runs a walk test picks itself once, and that pick belongs to that device.
export const NETWORK_SURVEY_MAC_KEY = 'home-automation.networkSurveyMac';
export const THIS_DEVICE_PRESENCE_KEY = 'home-automation.thisDevicePresence';
export const THIS_DEVICE_LOCATION_KEY = 'home-automation.thisDeviceLocation';

export const els = {
  paneAc: document.getElementById('paneAc'),
  acFeedback: document.getElementById('acFeedback'),
  grid: document.getElementById('unitsGrid'),
  toast: document.getElementById('toast'),
  buildReadout: document.getElementById('buildReadout'),
  // Security (RISCO alarm) tab
  paneSecurity: document.getElementById('paneSecurity'),
  securityFeedback: document.getElementById('securityFeedback'),
  homeSecurityFeedback: document.getElementById('homeSecurityFeedback'),
  securityState: document.getElementById('securityState'),
  securityActions: document.getElementById('securityActions'),
  // Alarm controls mirrored onto the Home tab (actionable).
  homeSecurityState: document.getElementById('homeSecurityState'),
  homeSecurityActions: document.getElementById('homeSecurityActions'),
  securityEvents: document.getElementById('securityEvents'),
  securityEventsNote: document.getElementById('securityEventsNote'),
  securitySchedules: document.getElementById('securitySchedules'),
  securitySchedulesNote: document.getElementById('securitySchedulesNote'),
  securitySchedulesCount: document.getElementById('securitySchedulesCount'),
  securityScheduleAdd: document.getElementById('securityScheduleAdd'),
  securityScheduleDialog: document.getElementById('securityScheduleDialog'),
  securityScheduleEditorTitle: document.getElementById('securityScheduleEditorTitle'),
  securityScheduleEditorClose: document.getElementById('securityScheduleEditorClose'),
  securityScheduleEnabled: document.getElementById('securityScheduleEnabled'),
  securityScheduleTime: document.getElementById('securityScheduleTime'),
  securityScheduleAction: document.getElementById('securityScheduleAction'),
  securityScheduleDays: document.getElementById('securityScheduleDays'),
  securityScheduleDelete: document.getElementById('securityScheduleDelete'),
  securityScheduleSave: document.getElementById('securityScheduleSave'),
  scenePairings: document.getElementById('scenePairings'),
  scenePairingsNote: document.getElementById('scenePairingsNote'),
  scenePairingsCount: document.getElementById('scenePairingsCount'),
  scenePairingAdd: document.getElementById('scenePairingAdd'),
  scenePairingDialog: document.getElementById('scenePairingDialog'),
  scenePairingEditorTitle: document.getElementById('scenePairingEditorTitle'),
  scenePairingEditorClose: document.getElementById('scenePairingEditorClose'),
  scenePairingEnabled: document.getElementById('scenePairingEnabled'),
  scenePairingZone: document.getElementById('scenePairingZone'),
  scenePairingCamera: document.getElementById('scenePairingCamera'),
  scenePairingPreset: document.getElementById('scenePairingPreset'),
  scenePairingDelete: document.getElementById('scenePairingDelete'),
  scenePairingSave: document.getElementById('scenePairingSave'),
  securityOverrides: document.getElementById('securityOverrides'),
  securityOverridesNote: document.getElementById('securityOverridesNote'),
  securityOverridesCount: document.getElementById('securityOverridesCount'),
  securityOverrideAdd: document.getElementById('securityOverrideAdd'),
  securityOverrideDialog: document.getElementById('securityOverrideDialog'),
  securityOverrideEditorTitle: document.getElementById('securityOverrideEditorTitle'),
  securityOverrideEditorClose: document.getElementById('securityOverrideEditorClose'),
  securityOverrideEnabled: document.getElementById('securityOverrideEnabled'),
  securityOverrideZone: document.getElementById('securityOverrideZone'),
  securityOverrideRetries: document.getElementById('securityOverrideRetries'),
  securityOverrideDelete: document.getElementById('securityOverrideDelete'),
  securityOverrideSave: document.getElementById('securityOverrideSave'),
  securityZones: document.getElementById('securityZones'),
  securityZonesNote: document.getElementById('securityZonesNote'),
  securityHiddenCount: document.getElementById('securityHiddenCount'),
  securityHiddenToggle: document.getElementById('securityHiddenToggle'),
  notifyScheduleArm: document.getElementById('notifyScheduleArm'),
  notifyScheduleDisarm: document.getElementById('notifyScheduleDisarm'),
  notifyPresenceArm: document.getElementById('notifyPresenceArm'),
  notifyPresenceDisarm: document.getElementById('notifyPresenceDisarm'),
  notifyError: document.getElementById('notifyError'),
  notifyIntrusion: document.getElementById('notifyIntrusion'),
  notifyAcLost: document.getElementById('notifyAcLost'),
  notifyConfiguredNote: document.getElementById('notifyConfiguredNote'),
  presenceSummary: document.getElementById('presenceSummary'),
  presenceHiddenCount: document.getElementById('presenceHiddenCount'),
  presenceHiddenToggle: document.getElementById('presenceHiddenToggle'),
  presenceList: document.getElementById('presenceList'),
  presenceNote: document.getElementById('presenceNote'),
  presenceKidsHome: document.getElementById('presenceKidsHome'),
  presenceRefreshNote: document.getElementById('presenceRefreshNote'),
  locationLabel: document.getElementById('locationLabel'),
  locationLat: document.getElementById('locationLat'),
  locationLon: document.getElementById('locationLon'),
  locationUseBrowser: document.getElementById('locationUseBrowser'),
  presenceAutoEnabled: document.getElementById('presenceAutoEnabled'),
  presenceAutomationNote: document.getElementById('presenceAutomationNote'),
  presenceArmMinutes: document.getElementById('presenceArmMinutes'),
  presenceStaleMinutes: document.getElementById('presenceStaleMinutes'),
  presenceDisarmOnArrival: document.getElementById('presenceDisarmOnArrival'),
  pushSubscribe: document.getElementById('pushSubscribe'),
  presenceDialog: document.getElementById('presenceDialog'),
  presenceDetailName: document.getElementById('presenceDetailName'),
  presenceDetailClose: document.getElementById('presenceDetailClose'),
  presenceDetailStatus: document.getElementById('presenceDetailStatus'),
  presenceDetailSource: document.getElementById('presenceDetailSource'),
  presenceDetailLastSeen: document.getElementById('presenceDetailLastSeen'),
  presenceDetailDistance: document.getElementById('presenceDetailDistance'),
  presenceDetailPlace: document.getElementById('presenceDetailPlace'),
  presenceMapLink: document.getElementById('presenceMapLink'),
  presenceMapFrame: document.getElementById('presenceMapFrame'),
  presenceDisplayName: document.getElementById('presenceDisplayName'),
  presenceOriginalName: document.getElementById('presenceOriginalName'),
  presenceRole: document.getElementById('presenceRole'),
  presenceHiddenDetailToggle: document.getElementById('presenceHiddenDetailToggle'),
  // Named-places dense-collection card (issue #438).
  presencePlacesList: document.getElementById('presencePlacesList'),
  presencePlacesNote: document.getElementById('presencePlacesNote'),
  presencePlaceAdd: document.getElementById('presencePlaceAdd'),
  presencePlaceDialog: document.getElementById('presencePlaceDialog'),
  presencePlaceEditorTitle: document.getElementById('presencePlaceEditorTitle'),
  presencePlaceEditorClose: document.getElementById('presencePlaceEditorClose'),
  presencePlaceLabel: document.getElementById('presencePlaceLabel'),
  presencePlaceLat: document.getElementById('presencePlaceLat'),
  presencePlaceLon: document.getElementById('presencePlaceLon'),
  presencePlaceRadius: document.getElementById('presencePlaceRadius'),
  presencePlaceUseBrowser: document.getElementById('presencePlaceUseBrowser'),
  presencePlacePickMap: document.getElementById('presencePlacePickMap'),
  presencePlaceDelete: document.getElementById('presencePlaceDelete'),
  presencePlaceSave: document.getElementById('presencePlaceSave'),
  // Map-pin picker dialog (issue #438).
  presenceMapPickerDialog: document.getElementById('presenceMapPickerDialog'),
  presenceMapPickerClose: document.getElementById('presenceMapPickerClose'),
  presenceMapPicker: document.getElementById('presenceMapPicker'),
  presenceMapPickerCoords: document.getElementById('presenceMapPickerCoords'),
  presenceMapPickerConfirm: document.getElementById('presenceMapPickerConfirm'),
  // Home-tab "Mom & Dad locator" card (issue #438).
  locatorList: document.getElementById('locatorList'),
  locatorSourceNote: document.getElementById('locatorSourceNote'),
  presenceDetailSave: document.getElementById('presenceDetailSave'),
  // Detector (zone) detail + rename modal
  zoneDialog: document.getElementById('zoneDialog'),
  zoneDetailName: document.getElementById('zoneDetailName'),
  zoneDetailClose: document.getElementById('zoneDetailClose'),
  zoneDetailType: document.getElementById('zoneDetailType'),
  zoneDetailStatus: document.getElementById('zoneDetailStatus'),
  zoneDetailTrouble: document.getElementById('zoneDetailTrouble'),
  zoneDisplayName: document.getElementById('zoneDisplayName'),
  zoneOriginalName: document.getElementById('zoneOriginalName'),
  zoneHiddenToggle: document.getElementById('zoneHiddenToggle'),
  zoneTroubleIgnoreToggle: document.getElementById('zoneTroubleIgnoreToggle'),
  zoneSave: document.getElementById('zoneSave'),
  // Cameras tile (Security tab) + detail / live-view modals (issue #161)
  camerasList: document.getElementById('camerasList'),
  camerasNote: document.getElementById('camerasNote'),
  cameraDialog: document.getElementById('cameraDialog'),
  cameraDetailName: document.getElementById('cameraDetailName'),
  cameraDetailClose: document.getElementById('cameraDetailClose'),
  cameraSnapshot: document.getElementById('cameraSnapshot'),
  cameraSnapshotEmpty: document.getElementById('cameraSnapshotEmpty'),
  cameraDetailStatus: document.getElementById('cameraDetailStatus'),
  cameraDisplayName: document.getElementById('cameraDisplayName'),
  cameraSave: document.getElementById('cameraSave'),
  cameraLiveBtn: document.getElementById('cameraLiveBtn'),
  cameraLiveDialog: document.getElementById('cameraLiveDialog'),
  cameraLiveName: document.getElementById('cameraLiveName'),
  cameraLiveClose: document.getElementById('cameraLiveClose'),
  cameraLiveImg: document.getElementById('cameraLiveImg'),
  cameraSnapBtn: document.getElementById('cameraSnapBtn'),
  cameraRecBtn: document.getElementById('cameraRecBtn'),
  cameraPtzUp: document.getElementById('cameraPtzUp'),
  cameraPtzDown: document.getElementById('cameraPtzDown'),
  cameraPtzLeft: document.getElementById('cameraPtzLeft'),
  cameraPtzRight: document.getElementById('cameraPtzRight'),
  cameraZoomIn: document.getElementById('cameraZoomIn'),
  cameraZoomOut: document.getElementById('cameraZoomOut'),
  // Precise-PTZ + presets + snapshot zoom (issue #190)
  cameraPtzModeBtn: document.getElementById('cameraPtzModeBtn'),
  cameraPresetsRow: document.getElementById('cameraPresetsRow'),
  cameraPresetsList: document.getElementById('cameraPresetsList'),
  cameraPresetSave: document.getElementById('cameraPresetSave'),
  cameraCoordsRow: document.getElementById('cameraCoordsRow'),
  cameraPanInput: document.getElementById('cameraPanInput'),
  cameraTiltInput: document.getElementById('cameraTiltInput'),
  cameraZoomInput: document.getElementById('cameraZoomInput'),
  cameraCoordsRefresh: document.getElementById('cameraCoordsRefresh'),
  cameraCoordsGo: document.getElementById('cameraCoordsGo'),
  cameraZoomDialog: document.getElementById('cameraZoomDialog'),
  cameraZoomName: document.getElementById('cameraZoomName'),
  cameraZoomClose: document.getElementById('cameraZoomClose'),
  cameraZoomImg: document.getElementById('cameraZoomImg'),
  // IoT tab — Plugs / Lights / Blinds, each a collapsible row-list card (#136).
  plugsFeedback: document.getElementById('plugsFeedback'),
  plugsCard: document.getElementById('plugsCard'),
  plugsList: document.getElementById('plugsList'),
  plugsCount: document.getElementById('plugsCount'),
  blindsCard: document.getElementById('blindsCard'),
  blindsList: document.getElementById('blindsList'),
  blindsCount: document.getElementById('blindsCount'),
  // Circuits — per-breaker CT-clamp meters, the IoT card after Plugs (#25).
  circuitsCard: document.getElementById('circuitsCard'),
  circuitsList: document.getElementById('circuitsList'),
  circuitsCount: document.getElementById('circuitsCount'),
  circuitsRefresh: document.getElementById('circuitsRefresh'),
  circuitsNote: document.getElementById('circuitsNote'),
  plugsNote: document.getElementById('plugsNote'),
  plugsRefresh: document.getElementById('plugsRefresh'),
  plugsToggleBtn: document.getElementById('plugsToggleBtn'),
  plugsHiddenToggle: document.getElementById('plugsHiddenToggle'),
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
  homeUpsTile: document.getElementById('homeUpsTile'),
  upsTile: document.getElementById('upsTile'),
  notifyPowerLost: document.getElementById('notifyPowerLost'),
  notifyPowerRestored: document.getElementById('notifyPowerRestored'),
  powerNotifyConfiguredNote: document.getElementById('powerNotifyConfiguredNote'),
  // PC fleet — UPS-triggered graceful fleet shutdown card (issue #498).
  pcFleetEnabled: document.getElementById('pcFleetEnabled'),
  pcFleetThreshold: document.getElementById('pcFleetThreshold'),
  pcFleetCaption: document.getElementById('pcFleetCaption'),
  pcFleetMachines: document.getElementById('pcFleetMachines'),
  pcFleetNote: document.getElementById('pcFleetNote'),
  // Home Assistant Hyper-V VM tile (Home tab, last card — issue #240).
  homeVmToggle: document.getElementById('homeVmToggle'),
  homeAssistantCard: document.getElementById('homeAssistantCard'),
  homeAssistantSummaryState: document.getElementById('homeAssistantSummaryState'),
  haSatellitesList: document.getElementById('haSatellitesList'),
  haSatellitesNote: document.getElementById('haSatellitesNote'),
  haInteractionsList: document.getElementById('haInteractionsList'),
  haInteractionsNote: document.getElementById('haInteractionsNote'),
  // Search-engine (SearXNG) status sub-card (issue #321).
  searxngCard: document.getElementById('searxngCard'),
  searxngSummaryState: document.getElementById('searxngSummaryState'),
  searxngNote: document.getElementById('searxngNote'),
  searxngStartBtn: document.getElementById('searxngStartBtn'),
  // Plug rename modal
  plugDialog: document.getElementById('plugDialog'),
  plugDetailName: document.getElementById('plugDetailName'),
  plugDisplayName: document.getElementById('plugDisplayName'),
  plugOriginalName: document.getElementById('plugOriginalName'),
  plugHiddenToggle: document.getElementById('plugHiddenToggle'),
  plugDetailClose: document.getElementById('plugDetailClose'),
  plugSave: document.getElementById('plugSave'),
  // Circuit channel rename + sign-flip modal (#25)
  circuitDialog: document.getElementById('circuitDialog'),
  circuitDetailName: document.getElementById('circuitDetailName'),
  circuitDisplayName: document.getElementById('circuitDisplayName'),
  circuitOriginalName: document.getElementById('circuitOriginalName'),
  circuitInvertSection: document.getElementById('circuitInvertSection'),
  circuitInvertToggle: document.getElementById('circuitInvertToggle'),
  circuitDetailClose: document.getElementById('circuitDetailClose'),
  circuitSave: document.getElementById('circuitSave'),
  // Elgato lights — the IoT tab's middle row-list card (#136).
  lightsCard: document.getElementById('lightsCard'),
  lightsCount: document.getElementById('lightsCount'),
  lightsAllOn: document.getElementById('lightsAllOn'),
  lightsAllOff: document.getElementById('lightsAllOff'),
  lightsRefresh: document.getElementById('lightsRefresh'),
  lightsList: document.getElementById('lightsList'),
  lightsNote: document.getElementById('lightsNote'),
  lightDialog: document.getElementById('lightDialog'),
  lightDetailName: document.getElementById('lightDetailName'),
  lightDetailClose: document.getElementById('lightDetailClose'),
  lightSave: document.getElementById('lightSave'),
  lightDisplayName: document.getElementById('lightDisplayName'),
  lightOriginalName: document.getElementById('lightOriginalName'),
  lightProduct: document.getElementById('lightProduct'),
  lightHost: document.getElementById('lightHost'),
  lightPort: document.getElementById('lightPort'),
  lightMac: document.getElementById('lightMac'),
  lightFirmware: document.getElementById('lightFirmware'),
  lightTemperatureMeta: document.getElementById('lightTemperatureMeta'),
  lightIdentifier: document.getElementById('lightIdentifier'),
  // Network (LAN) tab
  paneNetwork: document.getElementById('paneNetwork'),
  netFeedback: document.getElementById('netFeedback'),
  netInternetStatus: document.getElementById('netInternetStatus'),
  netInternetMeta: document.getElementById('netInternetMeta'),
  netSpeedResult: document.getElementById('netSpeedResult'),
  netSpeedBtn: document.getElementById('netSpeedBtn'),
  netAlerts: document.getElementById('netAlerts'),
  netApCard: document.getElementById('netApCard'),
  netApName: document.getElementById('netApName'),
  netApMeta: document.getElementById('netApMeta'),
  netApReboot: document.getElementById('netApReboot'),
  netRouterCard: document.getElementById('netRouterCard'),
  netRouterName: document.getElementById('netRouterName'),
  netRouterMeta: document.getElementById('netRouterMeta'),
  netRouterReboot: document.getElementById('netRouterReboot'),
  netWifiStatus: document.getElementById('netWifiStatus'),
  netWifiSummary: document.getElementById('netWifiSummary'),
  netWifiMeta: document.getElementById('netWifiMeta'),
  netWifiRecommendations: document.getElementById('netWifiRecommendations'),
  netWifiChart24: document.getElementById('netWifiChart24'),
  netWifiChart5: document.getElementById('netWifiChart5'),
  netWifiList: document.getElementById('netWifiList'),
  netWifiNote: document.getElementById('netWifiNote'),
  netWifiHiddenCount: document.getElementById('netWifiHiddenCount'),
  netWifiHiddenToggle: document.getElementById('netWifiHiddenToggle'),
  // Wi-Fi walk test (issue #547) — per-room coverage survey card + device picker.
  netSurveyCard: document.getElementById('netSurveyCard'),
  netSurveyStatus: document.getElementById('netSurveyStatus'),
  netSurveyBody: document.getElementById('netSurveyBody'),
  netSurveyDeviceRow: document.getElementById('netSurveyDeviceRow'),
  netSurveyDeviceName: document.getElementById('netSurveyDeviceName'),
  netSurveyDevicePick: document.getElementById('netSurveyDevicePick'),
  netSurveyForm: document.getElementById('netSurveyForm'),
  netSurveyRoom: document.getElementById('netSurveyRoom'),
  netSurveyRoomList: document.getElementById('netSurveyRoomList'),
  netSurveyRecord: document.getElementById('netSurveyRecord'),
  netSurveyProgress: document.getElementById('netSurveyProgress'),
  netSurveyRooms: document.getElementById('netSurveyRooms'),
  netSurveyNote: document.getElementById('netSurveyNote'),
  netSurveyDialog: document.getElementById('netSurveyDialog'),
  netSurveyDialogClose: document.getElementById('netSurveyDialogClose'),
  netSurveyDialogList: document.getElementById('netSurveyDialogList'),
  netStats: document.getElementById('netStats'),
  netSortAlpha: document.getElementById('netSortAlpha'),
  netSortSignal: document.getElementById('netSortSignal'),
  netGroupByBand: document.getElementById('netGroupByBand'),
  netGroupByGroup: document.getElementById('netGroupByGroup'),
  netOfflineToggle: document.getElementById('netOfflineToggle'),
  netHiddenCount: document.getElementById('netHiddenCount'),
  netHiddenToggle: document.getElementById('netHiddenToggle'),
  netDevices: document.getElementById('netDevices'),
  netDevicesNote: document.getElementById('netDevicesNote'),
  // DHCP reservation plan (issue #170 + #176) — lazy-loaded; "Apply" writes
  netDhcpCard: document.getElementById('netDhcpCard'),
  netDhcpRefresh: document.getElementById('netDhcpRefresh'),
  netDhcpApply: document.getElementById('netDhcpApply'),
  netDhcpWarnings: document.getElementById('netDhcpWarnings'),
  netDhcpPlan: document.getElementById('netDhcpPlan'),
  netDhcpNote: document.getElementById('netDhcpNote'),
  // Staged reservation manager (#176): existing rows, manual staging, apply bar.
  netDhcpExistingWrap: document.getElementById('netDhcpExistingWrap'),
  netDhcpExistingHead: document.getElementById('netDhcpExistingHead'),
  netDhcpExisting: document.getElementById('netDhcpExisting'),
  netDhcpManual: document.getElementById('netDhcpManual'),
  netDhcpManualMac: document.getElementById('netDhcpManualMac'),
  netDhcpManualIp: document.getElementById('netDhcpManualIp'),
  netDhcpManualName: document.getElementById('netDhcpManualName'),
  netDhcpManualAdd: document.getElementById('netDhcpManualAdd'),
  netDhcpManualStaged: document.getElementById('netDhcpManualStaged'),
  netDhcpApplyBar: document.getElementById('netDhcpApplyBar'),
  netDhcpBudget: document.getElementById('netDhcpBudget'),
  netDhcpClear: document.getElementById('netDhcpClear'),
  // Per-device detail + rename modal
  netDeviceDialog: document.getElementById('netDeviceDialog'),
  netDeviceDetailName: document.getElementById('netDeviceDetailName'),
  netDeviceDetailClose: document.getElementById('netDeviceDetailClose'),
  netDeviceStatus: document.getElementById('netDeviceStatus'),
  netDeviceVendor: document.getElementById('netDeviceVendor'),
  netDeviceIp: document.getElementById('netDeviceIp'),
  netDeviceConn: document.getElementById('netDeviceConn'),
  netDeviceSignal: document.getElementById('netDeviceSignal'),
  netDeviceSsid: document.getElementById('netDeviceSsid'),
  netDeviceSource: document.getElementById('netDeviceSource'),
  netDeviceHostname: document.getElementById('netDeviceHostname'),
  netDeviceSeen: document.getElementById('netDeviceSeen'),
  netDeviceSeenRow: document.getElementById('netDeviceSeenRow'),
  netDeviceDisplayName: document.getElementById('netDeviceDisplayName'),
  netDeviceImportant: document.getElementById('netDeviceImportant'),
  netDeviceImportantRow: document.getElementById('netDeviceImportantRow'),
  netDeviceHiddenToggle: document.getElementById('netDeviceHiddenToggle'),
  netDeviceSave: document.getElementById('netDeviceSave'),
  netDeviceMac: document.getElementById('netDeviceMac'),
  netDeviceGroup: document.getElementById('netDeviceGroup'),
  netDeviceGroupNew: document.getElementById('netDeviceGroupNew'),
  netDeviceGroupNewRow: document.getElementById('netDeviceGroupNewRow'),
  // Rename/delete dialog for one device group (issue #513).
  netGroupDialog: document.getElementById('netGroupDialog'),
  netGroupDialogTitle: document.getElementById('netGroupDialogTitle'),
  netGroupDialogClose: document.getElementById('netGroupDialogClose'),
  netGroupName: document.getElementById('netGroupName'),
  netGroupMembers: document.getElementById('netGroupMembers'),
  netGroupDelete: document.getElementById('netGroupDelete'),
  netGroupSave: document.getElementById('netGroupSave'),
  // Per-Wi-Fi-radio detail + rename modal
  netWifiDialog: document.getElementById('netWifiDialog'),
  netWifiDetailName: document.getElementById('netWifiDetailName'),
  netWifiDetailClose: document.getElementById('netWifiDetailClose'),
  netWifiDetailStatus: document.getElementById('netWifiDetailStatus'),
  netWifiDetailBand: document.getElementById('netWifiDetailBand'),
  netWifiDetailChannel: document.getElementById('netWifiDetailChannel'),
  netWifiDetailSignal: document.getElementById('netWifiDetailSignal'),
  netWifiDetailSecurity: document.getElementById('netWifiDetailSecurity'),
  netWifiDisplayName: document.getElementById('netWifiDisplayName'),
  netWifiOriginalName: document.getElementById('netWifiOriginalName'),
  netWifiHiddenDetailToggle: document.getElementById('netWifiHiddenDetailToggle'),
  // Reusable confirm modal
  confirmDialog: document.getElementById('confirmDialog'),
  confirmTitle: document.getElementById('confirmTitle'),
  confirmMessage: document.getElementById('confirmMessage'),
  confirmClose: document.getElementById('confirmClose'),
  confirmCancel: document.getElementById('confirmCancel'),
  confirmOk: document.getElementById('confirmOk'),
  // Read-only AC summary (Home tab)
  acSummary: document.getElementById('acSummary'),
  // Voice cheat sheet (Home tab, issue #437)
  voiceCommandsCard: document.getElementById('voiceCommandsCard'),
  voiceLangToggle: document.getElementById('voiceLangToggle'),
  voiceCommandsList: document.getElementById('voiceCommandsList'),
  voiceCommandsNote: document.getElementById('voiceCommandsNote'),
  // Wake alarms + timers (Home tab, issue #304)
  wakeRingingBanner: document.getElementById('wakeRingingBanner'),
  wakeAlarmsList: document.getElementById('wakeAlarmsList'),
  wakeAlarmsNote: document.getElementById('wakeAlarmsNote'),
  wakeAlarmsCount: document.getElementById('wakeAlarmsCount'),
  wakeAlarmAdd: document.getElementById('wakeAlarmAdd'),
  wakeTimersList: document.getElementById('wakeTimersList'),
  wakeTimersNote: document.getElementById('wakeTimersNote'),
  wakeTimerCustomMinutes: document.getElementById('wakeTimerCustomMinutes'),
  wakeTimerCustomAdd: document.getElementById('wakeTimerCustomAdd'),
  // Reminders (Home tab, issue #314)
  remindersList: document.getElementById('remindersList'),
  remindersNote: document.getElementById('remindersNote'),
  remindersCount: document.getElementById('remindersCount'),
  reminderAdd: document.getElementById('reminderAdd'),
  reminderDialog: document.getElementById('reminderDialog'),
  reminderEditorTitle: document.getElementById('reminderEditorTitle'),
  reminderEditorClose: document.getElementById('reminderEditorClose'),
  reminderText: document.getElementById('reminderText'),
  reminderDueToggle: document.getElementById('reminderDueToggle'),
  reminderDueFields: document.getElementById('reminderDueFields'),
  reminderDate: document.getElementById('reminderDate'),
  reminderTime: document.getElementById('reminderTime'),
  reminderDelete: document.getElementById('reminderDelete'),
  reminderSave: document.getElementById('reminderSave'),
  // Energy-flow card (GET /api/energy), Home tab — same view as the Energy tab.
  homeEnergyFlow: document.getElementById('homeEnergyFlow'),
  homeFlowPv: document.getElementById('homeFlowPv'),
  homeFlowGrid: document.getElementById('homeFlowGrid'),
  homeFlowHouse: document.getElementById('homeFlowHouse'),
  homeFlowNodePv: document.getElementById('homeFlowNodePv'),
  homeWirePv: document.getElementById('homeWirePv'),
  homeWireGrid: document.getElementById('homeWireGrid'),
  // Home-tab weather tile (GET /api/weather) + its inline theme toggle
  weatherTile: document.getElementById('weatherTile'),
  weatherThemeBtn: document.getElementById('weatherThemeBtn'),
  navDebugBtn: document.getElementById('navDebugBtn'),
  wxLocation: document.getElementById('wxLocation'),
  wxLocationLabel: document.getElementById('wxLocationLabel'),
  wxNowIcon: document.getElementById('wxNowIcon'),
  wxNowTemp: document.getElementById('wxNowTemp'),
  wxFcIcon: document.getElementById('wxFcIcon'),
  wxFcMin: document.getElementById('wxFcMin'),
  wxFcMax: document.getElementById('wxFcMax'),
  // Energy tab: flow diagram (live)
  paneEnergy: document.getElementById('paneEnergy'),
  energyFeedback: document.getElementById('energyFeedback'),
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
  genGap: document.getElementById('genGap'),
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
  // Energy tab: sun-position diagnostic card (issue #590)
  sunOverlayCard: document.getElementById('sunOverlayCard'),
  sunOverlayDate: document.getElementById('sunOverlayDate'),
  sunOverlayChart: document.getElementById('sunOverlayChart'),
  sunOverlayCount: document.getElementById('sunOverlayCount'),
  sunOverlayNote: document.getElementById('sunOverlayNote'),
  sunOverlayEmpty: document.getElementById('sunOverlayEmpty'),
  // Energy tab: PV-system editor card + its row dialog (issue #561)
  pvSystemTotal: document.getElementById('pvSystemTotal'),
  pvArrayList: document.getElementById('pvArrayList'),
  pvArrayAdd: document.getElementById('pvArrayAdd'),
  pvPerformanceRatio: document.getElementById('pvPerformanceRatio'),
  pvLat: document.getElementById('pvLat'),
  pvLon: document.getElementById('pvLon'),
  pvArrayDialog: document.getElementById('pvArrayDialog'),
  pvArrayEditorTitle: document.getElementById('pvArrayEditorTitle'),
  pvArrayEditorClose: document.getElementById('pvArrayEditorClose'),
  pvArrayKwp: document.getElementById('pvArrayKwp'),
  pvArrayKwpError: document.getElementById('pvArrayKwpError'),
  pvArrayTilt: document.getElementById('pvArrayTilt'),
  pvArrayTiltError: document.getElementById('pvArrayTiltError'),
  pvArrayAzimuth: document.getElementById('pvArrayAzimuth'),
  pvArrayAzimuthError: document.getElementById('pvArrayAzimuthError'),
  pvArrayAzimuthEcho: document.getElementById('pvArrayAzimuthEcho'),
  pvArrayDelete: document.getElementById('pvArrayDelete'),
  pvArraySave: document.getElementById('pvArraySave'),
  // Energy tab: horizon/shading profile editor (issue #578 part b)
  pvHorizonList: document.getElementById('pvHorizonList'),
  pvHorizonAdd: document.getElementById('pvHorizonAdd'),
  pvHorizonDialog: document.getElementById('pvHorizonDialog'),
  pvHorizonEditorTitle: document.getElementById('pvHorizonEditorTitle'),
  pvHorizonEditorClose: document.getElementById('pvHorizonEditorClose'),
  pvHorizonAzimuth: document.getElementById('pvHorizonAzimuth'),
  pvHorizonAzimuthError: document.getElementById('pvHorizonAzimuthError'),
  pvHorizonAzimuthEcho: document.getElementById('pvHorizonAzimuthEcho'),
  pvHorizonElevation: document.getElementById('pvHorizonElevation'),
  pvHorizonElevationError: document.getElementById('pvHorizonElevationError'),
  pvHorizonDelete: document.getElementById('pvHorizonDelete'),
  pvHorizonSave: document.getElementById('pvHorizonSave'),
  // Energy tab: fleet solar-boost sequencing knobs (issue #562)
  boostCoordSummary: document.getElementById('boostCoordSummary'),
  boostSettleMin: document.getElementById('boostSettleMin'),
  boostAdmissionMargin: document.getElementById('boostAdmissionMargin'),
  boostHardDeficit: document.getElementById('boostHardDeficit'),
  boostOrderingPolicy: document.getElementById('boostOrderingPolicy'),
  // Detail modal
  detail: document.getElementById('detailDialog'),
  detailName: document.getElementById('detailName'),
  detailOffline: document.getElementById('detailOffline'),
  detailDisplayName: document.getElementById('detailDisplayName'),
  detailMode: document.getElementById('detailMode'),
  detailFanSpeed: document.getElementById('detailFanSpeed'),
  detailFanSpeedRow: document.getElementById('detailFanSpeedRow'),
  detailVaneVertical: document.getElementById('detailVaneVertical'),
  detailVaneHorizontal: document.getElementById('detailVaneHorizontal'),
  detailVaneVerticalRow: document.getElementById('detailVaneVerticalRow'),
  detailVaneHorizontalRow: document.getElementById('detailVaneHorizontalRow'),
  detailClose: document.getElementById('detailClose'),
  detailSave: document.getElementById('detailSave'),
  // Detail modal — temperature rule (dynamic setpoint) section
  ruleEnabled: document.getElementById('ruleEnabled'),
  ruleCoolTarget: document.getElementById('ruleCoolTarget'),
  ruleHeatTarget: document.getElementById('ruleHeatTarget'),
  ruleBoostEnabled: document.getElementById('ruleBoostEnabled'),
  ruleBoostOffset: document.getElementById('ruleBoostOffset'),
  // Detail modal — schedule-entry list
  schedList: document.getElementById('schedList'),
  schedAdd: document.getElementById('schedAdd'),
  // Login overlay
  loginOverlay: document.getElementById('loginOverlay'),
  loginForm: document.getElementById('loginForm'),
  loginPassword: document.getElementById('loginPassword'),
  loginError: document.getElementById('loginError'),
};

// ------------------------------------------------- persisted UI preferences
// Every list toggle/sort/pick that survives a reload is a localStorage string
// wrapped in a try/catch (private mode throws, and the pref must then just stay
// in memory for the session). That read-with-catch / write-with-catch pair used
// to be hand-rolled six times across network-devices / network-wifi /
// network-survey (issue #571). These own the storage concern only — value
// semantics (what an absent or unrecognised string means) stay at the call site.

/** `{read, write}` over one localStorage key. `read()` yields the raw stored
 *  string, or `fallback` when absent or unreadable; `write(value)` removes the
 *  key for a null/empty value and stores `String(value)` otherwise. */
export function persistedPref(key, fallback) {
  const missing = fallback === undefined ? null : fallback;
  return {
    read() {
      try {
        const raw = localStorage.getItem(key);
        return raw === null ? missing : raw;
      } catch (_e) {
        return missing;  // private mode — in-memory only
      }
    },
    write(value) {
      try {
        if (value == null || value === '') localStorage.removeItem(key);
        else localStorage.setItem(key, String(value));
      } catch (_e) { /* private mode — in-memory only */ }
    },
  };
}

/** `persistedPref` for an on/off flag stored as `'1'`/`'0'`. `read()` is a
 *  boolean (absent or unreadable → `fallback`, default `false`). */
export function persistedFlag(key, fallback) {
  const pref = persistedPref(key, null);
  const missing = fallback === undefined ? false : fallback;
  return {
    read() {
      const raw = pref.read();
      return raw === null ? missing : raw === '1';
    },
    write(on) { pref.write(on ? '1' : '0'); },
  };
}

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

// ------------------------------------------------ fetch-failure surfacing
// Surface a failed data fetch as a single error toast per failure *transition*.
// The tabs poll every few seconds, so toasting on every cycle while a source
// stays down would spam — instead we track the last-known health per scope and
// toast only when it goes healthy → failing, staying quiet until it recovers.
// `auth required` is never surfaced (it routes to the login overlay instead).
const fetchFailing = {};  // scope -> currently in a failed state
export function reportFetchFailure(scope, exc, label) {
  if (exc && String(exc.message) === 'auth required') return;
  if (fetchFailing[scope]) return;  // already toasted for this outage
  fetchFailing[scope] = true;
  const reason = (exc && (exc.message || exc)) || 'unknown error';
  toast("Couldn't load " + (label || scope) + ': ' + reason, 'error');
}
export function reportFetchOk(scope) {
  fetchFailing[scope] = false;  // re-arm so the next outage toasts again
}

// ----------------------------------------------------------- toasts
let toastTimer = null;
export function toast(msg, kind) {
  els.toast.textContent = msg;
  els.toast.className = 'toast ' + (kind || '');
  // Errors interrupt (aria-live="assertive"); everything else stays polite so
  // routine save/toggle confirmations don't cut off other screen-reader
  // speech (issue #370).
  els.toast.setAttribute('aria-live', kind === 'error' ? 'assertive' : 'polite');
  els.toast.hidden = false;
  if (toastTimer) { clearTimeout(toastTimer); toastTimer = null; }
  // 'pending' (e.g. "Sending…") stays up until a follow-up toast replaces it
  // with the result — it has no fixed lifetime (#204).
  if (kind === 'pending') return;
  toastTimer = setTimeout(function () {
    els.toast.hidden = true;
  }, kind === 'error' ? 4500 : 2000);
}

// ------------------------------------------------------ mode presentation
// Mode → Lucide glyph name (rendered through icon() from the vendored
// _vendored/icons/icons.js at the call sites; this returns the bare name, not
// markup).
const MODE_ICONS = {
  Heat: 'flame',
  Cool: 'snowflake',
  Automatic: 'refresh-cw',
  Dry: 'droplets',
  Fan: 'fan',
};
export function modeIcon(mode) {
  return MODE_ICONS[mode] || 'thermometer';
}
