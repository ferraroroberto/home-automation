# Home Assistant VM — how to interact with it

The procedure for interacting with the Home Assistant VM, plus the reference for the app's VM status/control feature and the VM's static MAC + DHCP reservation. `CLAUDE.md` keeps a one-line pointer here. Voice-PE specifics live in [`voice-pe-config/README.md`](voice-pe-config/README.md).

## Interacting with the VM — SSH first, browser only as fallback

The SSH shell is a full administrator shell (`root` in the add-on container, `ha` CLI and `/config` mounted). Connection settings in `.env` (`HA_SSH_*`, `HA_URL`, `HA_TOKEN`). One-time SSH bootstrap: [`voice-pe-config/README.md`](voice-pe-config/README.md). HAOS host SSH on `:22222` is break-glass only.

Reach each task via SSH in this order of preference:

- **Repo-owned voice-PE config** (`configuration.yaml` managed block, `custom_sentences/`) → [`scripts/ha_config_sync.py`](../scripts/ha_config_sync.py) `preflight / deploy [--dry-run] [--restart] / rollback / probe`. Idempotent; validates with `ha core check`; takes backups.
- **One-off config edits / `ha` CLI** → `ssh -p <HA_SSH_PORT> <HA_SSH_USER>@<HA_SSH_HOST>` then the `ha` CLI: `ha core check`, `ha core restart`, `ha core reload`, `ha addon list`, `ha addon install <slug>`, `ha addon start/stop/restart <slug>`, `ha addon options <slug>` (read/write add-on config JSON).
- **File deployment** (add-on model files, arbitrary `/config` edits not covered by `ha_config_sync.py`) → `scp -P <HA_SSH_PORT> <local_file> <HA_SSH_USER>@<HA_SSH_HOST>:<remote_path>`. Add-on data directories: `/addon_configs/<slug>/` and `/addon_local/<slug>/` inside the shell.
- **HA REST API** (read entity state, call a service, set a select entity) → `curl -H "Authorization: Bearer <HA_TOKEN>" http://<HA_HOST>:8123/api/...`. Set ESPHome select entities (e.g. wake word picker) without the browser: `POST /api/services/select/select_option` with `{"entity_id": "select.<name>", "option": "<value>"}`.

**When the browser IS genuinely needed** (rare — document in the issue): initial ESPHome device adoption, re-flashing ESPHome firmware from the ESPHome dashboard, any HA onboarding wizard step with no API/CLI equivalent. Do **not** drive the HA File editor, Developer Tools, or add-on store through Chrome automation when the above paths work — Chrome is the fallback only when the Terminal & SSH add-on is down or a key is unprovisioned.

## Hyper-V VM status + control

Home Assistant runs as a **Hyper-V VM** on this PC (TOWER), bridged onto the LAN so it has its own MAC and DHCP-reserved IP. The **Home Assistant** group on the Devices tab shows that VM as a row — `Online · up 3d 4h` (or `Off`) — with the Start/Stop switch as the row's trailing item. `GET /api/hyperv` reads the state; `POST /api/hyperv/{start|stop}` controls it. The VM is addressed **by name** (`HA_VM_NAME`), so the card is independent of its IP, and the backend only ever touches that one VM — never the host's other VMs (e.g. WSL2's hidden utility VM). **Stop is a graceful ACPI shutdown** (confirm-gated in the UI); there is no hard power-off.

**`HA_VM_NAME`** (in `.env`) is the exact VM name as it appears in Hyper-V Manager / `Get-VM` — e.g. `Home Assistant`. Nothing is hardcoded.

**Rights prerequisite — "Hyper-V Administrators".** The webapp runs under the tray user, which by default **cannot** run `Start-VM` / `Stop-VM` (and often not even `Get-VM`). Grant VM lifecycle control **without** full machine admin by adding that account to the local **Hyper-V Administrators** group, then sign out/in (or restart the tray) so the new token takes effect:

```powershell
# Run once, elevated. Replace with the account the tray/webapp runs as.
Add-LocalGroupMember -Group "Hyper-V Administrators" -Member "$env:COMPUTERNAME\YourUser"
```

Without that membership the tile shows a distinct `⚠ insufficient Hyper-V rights` state (read and act can fail independently — the card surfaces each cause: insufficient rights · VM not found · already in that state).

## Static MAC + DHCP reservation (`.4`)

The VM is pinned to **`192.168.0.4`** via a **static MAC + router DHCP reservation** so a VM re-import can't churn its address or leave a stale lease. One-time, manual (host + router — the app only *displays* MAC/IP so you can verify it landed on the reserved address):

1. **Pin the MAC static** (elevated PowerShell). Reuse the VM's *current* dynamic MAC so no new lease is generated — read it, then set it static:
   ```powershell
   (Get-VMNetworkAdapter -VMName "Home Assistant").MacAddress      # e.g. 00155D012A0B
   Set-VMNetworkAdapter -VMName "Home Assistant" -StaticMacAddress "00155D012A0B"
   ```
2. **Reserve `.4` to that MAC** on the router (Vodafone ZXHN F6600P → DHCP / static binding), and **delete the stale old-MAC `homeassistant` entry** so only one reservation remains.
3. **Reboot the VM** so it picks up the `.4` lease, then verify: the Home tile's sub-line should read `192.168.0.4 · 00:15:5D:01:2A:0B`.
4. **Re-point the deploy config**: set `HA_SSH_HOST=192.168.0.4` and `HA_URL=http://192.168.0.4:8123` in `.env`, drop the stale SSH host key (`ssh-keygen -R "[192.168.0.102]:2222"`), and re-run [`scripts/ha_config_sync.py`](../scripts/ha_config_sync.py) `preflight`.
