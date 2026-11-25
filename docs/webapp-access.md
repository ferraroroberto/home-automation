# Webapp access: HTTPS and auth

## HTTPS (Tailscale cert)

The webapp is reached over Tailscale, so HTTPS uses a **real Let's Encrypt
certificate** issued for the tailnet MagicDNS name via `tailscale cert`. Every
device already on the tailnet trusts Let's Encrypt, so there are **no
per-device trust steps** — no CA to install, no iOS profile, no Chrome restart.

**One-time setup (per tailnet):** enable HTTPS in the Tailscale admin console —
[**DNS → HTTPS Certificates**](https://login.tailscale.com/admin/dns). Then
provision the cert (auto-detects this machine's MagicDNS name):

```powershell
& .\.venv\Scripts\python.exe scripts\gen_tailscale_cert.py
```

This writes `cert.pem` / `key.pem` into `webapp/certificates/`; restart the
webapp (`tray.bat --restart`, or `scripts/webapp.bat`) and open
`https://<pc>.<tailnet>.ts.net:8447`.

> **Auto-renew (no calendar reminder needed).** A Let's Encrypt leaf is valid
> ~90 days, so renewal is automated rather than manual: both boot paths run
> `gen_tailscale_cert.py --check` on startup, which re-issues the cert only
> when it is a `.ts.net` cert expiring within 30 days (a no-op otherwise). The
> tray-owned webapp self-heals on its next restart; `scripts/webapp.bat` does the same.
> A cert file that cannot be read (corrupt PEM, `cryptography` missing) is
> reported as unknown, not as healthy: `--check` prints a `[WARN]` and exits 1,
> and the tray logs it as a failed renewal check.

Plain desktop access on the PC itself uses `http://localhost:8447`
(`https://localhost:8447` would warn — the cert is for the `.ts.net` name, not
loopback). The tray's **🏠 Open** action opens the trusted `.ts.net` URL
directly, so the lock is green with no certificate fuss.

### Phone install (PWA)

The webapp installs to the iPhone/Android home screen as a full-screen app:
open `https://<pc>.<tailnet>.ts.net:8447` in Safari/Chrome — the lock is solid
on first visit thanks to the Let's Encrypt cert (see
[HTTPS](#https-tailscale-cert)) — then **Share → Add to Home Screen**.

## Auth (token + password)

Both layers are optional. With nothing configured the API is open (fine on a
trusted tailnet). Loopback callers always bypass the gate.

The PWA keeps a browser-local, versioned last-good snapshot of selected read-only API responses (AC units, live/today energy, plugs, lights, and network) so a reload can paint the last useful state before fresh live reads finish. Successful live reads replace the snapshot; failed reads leave the last-good snapshot intact. Security state, presence/location diagnostics, event logs, auth responses, edit forms, and command responses are intentionally excluded.

```powershell
& .\.venv\Scripts\python.exe scripts\gen_token.py            # set a bearer token
& .\.venv\Scripts\python.exe scripts\gen_token.py --force    # rotate
& .\.venv\Scripts\python.exe scripts\gen_token.py --clear    # disable
& .\.venv\Scripts\python.exe -m src.password set            # optional login password (prompts twice)
```

The login password is stored only as a salted hash in `config/app_password.json` (git-ignored), through the vendored `src/password.py` every fleet app shares; any non-empty password works. Until it has been set, an older plaintext `auth_password` in `webapp_config.json` still signs in; after, that value is ignored and can be deleted.

- Remote (LAN / Tailscale) callers must present `Authorization: Bearer <token>` or `?token=…`.
- Open the webapp once with `?token=…`; the page stashes it in localStorage and strips it from the URL.
- A login **password** lets a fresh device (e.g. an iOS PWA whose storage is partitioned) type a secret into the overlay instead — the server hands the token back. Failed attempts log to `webapp/auth.log`.

## Restart from the build readout

Tapping the build readout in the page foot opens a sheet that restarts the app, from any client that can already use it, the phone over the tailnet included. It posts `POST /api/restart`, which runs this repo's own `tray.bat --restart` from the path spelling the tray was launched from, through a runner detached from the tray's process tree so the restart cannot kill itself. Only the declared `app` target runs; the client sends that id and nothing else.

- **Auth is the bearer gate every other `/api` route uses.** The route also depends on `require_authenticated` (`app/webapp/middleware.py`), which refuses a request the middleware did not admit, so it can never answer on an exempt path. Loopback bypasses the token like every other call. A `POST` whose `Origin` is not this host is refused with 403.
- **Every ending is recorded and shown.** `ok`, `failed`, `timeout`, `error`, and `unknown` (the runner stopped reporting) are written to `restart/last.json` and `restart/last.log` under the app's runtime-data directory, and the sheet shows each as its own message; a restart is confirmed only when `/api/version` reports a new process and the record says `ok`.
- **The component is vendored.** `src/self_restart.py` and `page-foot/restart-sheet.{js,css}` come verbatim from `project-scaffolding`; the app's part is the router mount in `app/webapp/server.py` and the two calls in `app/webapp/static/main.js`.
