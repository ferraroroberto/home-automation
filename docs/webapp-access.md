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
webapp (`tray.bat --restart`, or `webapp.bat`) and open
`https://<pc>.<tailnet>.ts.net:8447`.

> **Auto-renew (no calendar reminder needed).** A Let's Encrypt leaf is valid
> ~90 days, so renewal is automated rather than manual: both boot paths run
> `gen_tailscale_cert.py --check` on startup, which re-issues the cert only
> when it is a `.ts.net` cert expiring within 30 days (a no-op otherwise). The
> tray-owned webapp self-heals on its next restart; `webapp.bat` does the same.

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
& .\.venv\Scripts\python.exe scripts\set_password.py <pw>    # optional login password
```

- Remote (LAN / Tailscale) callers must present `Authorization: Bearer <token>` or `?token=…`.
- Open the webapp once with `?token=…`; the page stashes it in localStorage and strips it from the URL.
- A login **password** lets a fresh device (e.g. an iOS PWA whose storage is partitioned) type a secret into the overlay instead — the server hands the token back. Failed attempts log to `webapp/auth.log`.
