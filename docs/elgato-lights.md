# Elgato lights

Issue #124 proved that Elgato lighting can be controlled locally from this app without cloud credentials. The implementation uses the devices' direct LAN HTTP API on port `9123` plus Bonjour/mDNS discovery for `_elg._tcp.local.`; `ELGATO_LIGHT_HOSTS` is the fallback when discovery is blocked.

## Spike result

- mDNS discovery found three Elgato accessories on the home LAN.
- A no-visible-change write/read-back against a Key Light Air succeeded: `off`, brightness `15`, temperature `203` mired (`4926 K`) read back exactly.
- Key Light Air and Key Light MK.2 report color temperature.
- Light Strip Pro reports `temperature=0`, so the app treats it as brightness/power only and does not send `temperature: 0` on writes.

## Recommendation

Keep the minimal direct client in `src/elgato_client.py` rather than adding `python-elgato` for now. The required surface is small (`GET`/`PUT /elgato/lights`, optional `GET /elgato/accessory-info`), the app already depends on `aiohttp`, and direct handling lets the UI distinguish color-temperature-capable lights from brightness-only lights. Revisit a wrapper only if future work adds RGB color, scenes, or broader Elgato accessory support.

## Verification

Read all discoverable lights:

```powershell
& .\.venv\Scripts\python.exe -m src.list_elgato_lights
```

Write a light and read back the accepted state:

```powershell
& .\.venv\Scripts\python.exe -m src.list_elgato_lights --id <host>:9123 --on off --brightness 15 --temperature 203
```
