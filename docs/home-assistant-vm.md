# Home Assistant VM — how to interact with it

Durable reference moved verbatim from `CLAUDE.md` (#920, R-26): the multi-step procedure for touching the Home Assistant VM. `CLAUDE.md` keeps a one-line pointer here. Voice-PE specifics live in [`voice-pe-config/README.md`](voice-pe-config/README.md).

**Interacting with the Home Assistant VM — SSH first, browser only as fallback (#243).** The SSH shell is a full administrator shell (`root` in the add-on container, `ha` CLI and `/config` mounted). Connection settings in `.env` (`HA_SSH_*`, `HA_URL`, `HA_TOKEN`). One-time SSH bootstrap: `docs/voice-pe-config/README.md`. HAOS host SSH on `:22222` is break-glass only.

Reach each task via SSH in this order of preference:

- **Repo-owned voice-PE config** (`configuration.yaml` managed block, `custom_sentences/`) → `scripts/ha_config_sync.py preflight / deploy [--dry-run] [--restart] / rollback / probe`. Idempotent; validates with `ha core check`; takes backups.
- **One-off config edits / `ha` CLI** → `ssh -p <HA_SSH_PORT> <HA_SSH_USER>@<HA_SSH_HOST>` then the `ha` CLI: `ha core check`, `ha core restart`, `ha core reload`, `ha addon list`, `ha addon install <slug>`, `ha addon start/stop/restart <slug>`, `ha addon options <slug>` (read/write add-on config JSON).
- **File deployment** (add-on model files, arbitrary `/config` edits not covered by `ha_config_sync.py`) → `scp -P <HA_SSH_PORT> <local_file> <HA_SSH_USER>@<HA_SSH_HOST>:<remote_path>`. Add-on data directories: `/addon_configs/<slug>/` and `/addon_local/<slug>/` inside the shell.
- **HA REST API** (read entity state, call a service, set a select entity) → `curl -H "Authorization: Bearer <HA_TOKEN>" http://<HA_HOST>:8123/api/...`. Set ESPHome select entities (e.g. wake word picker) without the browser: `POST /api/services/select/select_option` with `{"entity_id": "select.<name>", "option": "<value>"}`.

**When the browser IS genuinely needed** (rare — document in the issue): initial ESPHome device adoption, re-flashing ESPHome firmware from the ESPHome dashboard, any HA onboarding wizard step with no API/CLI equivalent. Do **not** drive the HA File editor, Developer Tools, or add-on store through Chrome automation when the above paths work — Chrome is the fallback only when the Terminal & SSH add-on is down or a key is unprovisioned.
