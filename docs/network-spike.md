# Network view — spike findings (issue #125)

Proof-of-concept results for a future **Network** tab: internet/WiFi/LAN health,
the attached-device inventory named by MAC, network-quality alerts, and
router/AP reboot — so the network can be watched and managed without logging
into the vendor web UIs by hand.

This documents what the spike **proved against the live hardware**, what to
**adopt**, and what is **left for the follow-up implementation issue**. The
prototype core lives in `src/network_client.py` with the CLI `src/list_network.py`.

## TL;DR

Doable. The access-point side is effectively production-ready; the router side
has the hard part (headless login) proven, with one well-understood detail left.

| Capability | Source | Status |
| --- | --- | --- |
| Attached-device inventory (MAC, IP, name, signal %, band, SSID) | NETGEAR AP | ✅ working, 35 devices live |
| AP health (model, firmware, mode) | NETGEAR AP | ✅ working |
| AP reboot | NETGEAR AP | ✅ working (`reboot_access_point()`) |
| Internet health (up/down, latency, packet loss) | host-side ping | ✅ working |
| WAN speed test (down/up Mbps) | host-side `speedtest-cli` | ✅ working (~13 s) |
| Router headless login | ZTE web (SHA256) | ✅ working (`RouterClient.login`) |
| Router WAN-status data read | ZTE web | ✅ working (`RouterClient.read_wan`, issue #129 Phase 3) |
| Router reboot | ZTE web | ✅ working (`RouterClient.reboot`, issue #129 Phase 3) |

## Devices

- **Access point — NETGEAR R9000 (Nighthawk X10)**, running in **AP mode**
  (`get_info().DeviceMode == 1`). Despite AP mode it reports the **whole LAN**
  (wired + wireless clients), so it carries the device inventory on its own —
  the router's DHCP list is *not* required for a usable inventory.
- **Router — Vodafone ZXHN F6600P (ZTE)**, the gateway. Web UI only; no SNMP/SSH
  exposed by default.

## What to adopt

### Access point → `pynetgear`

The mature [`pynetgear`](https://pypi.org/project/pynetgear/) library drives the
Netgear SOAP API and needs only one non-default: **this R9000 serves SOAP on
port 80**, while `pynetgear` defaults to 5000 — so construct it with `port=80`.

```python
from pynetgear import Netgear
ng = Netgear(password=..., host=..., user=..., port=80)
ng.get_info()                 # ModelName, Firmwareversion, DeviceMode, ...
ng.get_attached_devices_2()   # list of namedtuples (see fields below)
ng.reboot()                   # returns True on accept
```

`get_attached_devices_2()` fields actually populated on this unit:
`name, ip, mac, type (wired|2.4GHz|5GHz), signal (percent), ssid`. `link_rate`
comes back `0` for wireless / `None` for wired here, so don't rely on it.
`name` is the client hostname and is **often `n/a`** — which is exactly why the
display-name store (below) matters.

### Router → thin custom client (no library fits)

There is **no** usable pip library for the F6600P's live web UI. The known ZTE
repos (`mkst/zte-config-utility`, `douniwan5788/zte_modem_tools`) do config
backup/decrypt and telnet-enable, not live status/reboot. So the router client
is hand-rolled (stdlib `hashlib` + `requests`), implementing the login observed
in the live page's JS (`g_loginToken`):

1. `GET /?_type=loginData&_tag=login_entry` → JSON `sess_token` (sets `SID` cookie)
2. `GET /?_type=loginData&_tag=login_token` → XML challenge token
3. `POST /?_type=loginData&_tag=login_entry` with
   `Password = sha256(password + challenge)`, `Username`, `_sessionTOKEN=sess_token`
   → `{"login_need_refresh": true}` on success

This is implemented and **verified working** in `RouterClient.login()`.

**The data-read + reboot scheme (solved in issue #129 Phase 3).** Reverse-engineered
off the live page JS and verified against the hardware:

- **Page-gated feeds.** Each `menuData` feed is gated on the *current* menu page,
  so a feed read returns `IF_ERRORSTR=SessionTimeout` unless its page was loaded
  first. Sequence: `GET /?_type=menuView&_tag=<pageId>` (e.g. `ethWanStatus`), then
  `GET /?_type=menuData&_tag=<feed>` with `Referer` = that page. The home-dashboard
  widget feeds (`*_homepage_lua.lua`) are the exception — they read with the `SID`
  cookie alone.
- **WAN status.** Feed `wan_internetstatus_lua.lua&TypeUplink=2&pageType=1` returns
  all WAN instances as `<Instance>` ParaName/ParaValue blocks; the live internet
  connection is the one that is `Connected` with a non-`0.0.0.0` IPv4 (prefer
  `IsDefGW=1`). Fields used: `ConnStatus`, `IPAddress`, `GateWay`, `DNS1/2`,
  `WANCName`, `UpTime`, `Addressingtype`.
- **Reboot (writes).** `commConf.IntegCheck` is **true**, so each POST carries the
  rolling `_sessionTOKEN` (embedded in the page as `_sessionTmpToken`, `\xNN`
  escaped) in the body *and* a `Check` header = `asyEncode(sha256(body))`, where
  `asyEncode` is JSEncrypt RSA/PKCS1-v1.5 under the page's embedded PEM public key,
  base64-encoded. Reboot body = `IF_ACTION=Restart&_sessionTOKEN=<token>` POSTed to
  `devmgr_restartmgr_lua.lua` (after loading the `rebootAndReset` page). The device
  resets the connection as it restarts, so a post-accept transport error counts as
  success.

All of this lives in `src/network_client.py` (`RouterClient.read_wan` /
`RouterClient.reboot` + the `_parse_instances` / `_pick_internet_wan` /
`_extract_token` / `_extract_pubkey` / `_asy_encode` helpers).

Note: the router's **built-in** speed test is disabled in firmware
(`commConf.diagnose.speedtest == 0`), so throughput is measured host-side
regardless.

## Internet health (host-side)

Independent of both devices, so "is the internet up" never depends on the router
API: OS `ping` to an external anchor (`1.1.1.1`) for latency + packet loss, plus
the gateway ping for the local hop, plus an **opt-in** `speedtest-cli` run
(~13 s, saturates the link — off by default).

## Proposed `NetworkState` shape

Already prototyped in `src/network_client.py` as frozen dataclasses, mirroring
`SecurityState` / `EnergyState`:

- `InternetHealth` — `online, gateway_ms, external_ms, packet_loss_pct, download_mbps, upload_mbps, speedtest_server`
- `AccessPointHealth` — `reachable, model, firmware, mode, device_count, error`
- `RouterHealth` — `reachable, authenticated, model, error`
- `NetDevice` — `mac, ip, name, conn_type, signal, link_rate, ssid, source`
- `NetworkState` — `internet, access_point, router, devices[], alerts[]`

`fetch_network_state(include_speedtest=False)` runs the three sources
concurrently (`asyncio.gather`); the blocking `pynetgear`/`speedtest` calls go
through `asyncio.to_thread`.

## Device inventory & the DHCP/wireless merge

The spike's open design question — "in AP mode, does the inventory need to merge
the router's DHCP leases with the AP's wireless data?" — resolved **simpler than
expected**: the R9000 already returns the full wired+wireless list with IPs, so
the AP alone is a complete-enough inventory for v1. The router's DHCP table is
only worth merging later for **better hostnames** (many AP `name`s are `n/a`);
that is an enhancement, not a blocker, and depends on the router data-read work
above. The merge key is the MAC.

**Done (#169).** `RouterClient.read_dhcp_leases()` reads the router's LAN
allocated-address table — `menuData` feed `accessdev_landevs_lua.lua`, page-gated
behind the `localNetStatus` `menuView` exactly like the WAN read — returning
`{mac, ip, hostname}` per client. `_merge_router_leases()` folds it into the AP
inventory keyed by normalized MAC: a missing AP `name` is filled from the router
hostname (never overwriting an AP-reported name) and the device is marked
`source="both"`; a lease with no AP match is added as a router-only `NetDevice`
(`source="router"` — the wired clients the AP can't see). The read is
best-effort, so a router failure leaves the AP inventory intact. The `source`
attribution surfaces in the device detail modal (`Seen by`), not the list. This
firmware exposes **no** DHCP port / lease-time feed (every candidate 404s), so
only host/ip/mac are available.

## Naming devices by MAC (follow-up)

The user wants devices recognizable by MAC. Reuse the existing display-name
module verbatim, as `tuya_display_names.py` / `security_display_names.py` do:
a new `src/network_display_names.py` over `config/network_display_names.json`
(gitignored, with a committed sample), keyed by **MAC**. Deferred out of the
spike to keep it to the three proofs.

## Eventual tab (follow-up)

A `GET /api/network` router under `app/webapp/routers/` returning `NetworkState`,
and a Network card view: an internet-health tile (up/down, latency, speed), an
AP/router health row with reboot buttons, and the device list (MAC → friendly
name, signal, band) with the weak-signal/offline alerts surfaced. Reboot is a
deliberate user action with a confirm — never automatic.

## Follow-up checklist

- [x] ZTE `menuData` session-token scheme → WAN/internet status read (#129 Phase 3)
- [x] ZTE reboot POST → `reboot_router()` (#129 Phase 3)
- [x] MAC history store + online/offline, last-seen, new-device / important-offline alerts, mark-important, show-offline toggle (`src/network_history.py`, #129 Phase 4)
- [x] Optional router-DHCP merge for better device hostnames (#169)
- [ ] Optional latency/throughput sparklines + scheduled nightly speed test (deferred from #129 Phase 4)
- [x] `src/network_display_names.py` (MAC-keyed) + sample JSON (#129 Phase 2)
- [x] `GET /api/network` router + the Network tab UI (#129 Phase 1)
