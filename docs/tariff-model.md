# Electricity tariff model

How the Energy-tab **cost & savings breakdown** turns monitored energy into money, and how the default per-period prices were derived from a real Spanish PVPC 2.0TD invoice. The code lives in `src/tariff.py`; the rates live in `config/tariff.json` (gitignored; copy `config/tariff.sample.json`).

This is a **household-monitoring estimate**, not a billing-grade meter read. It is good enough to compare periods, track self-consumption value, and size the eventual solar load-balancing automation — but it will not match your utility bill to the cent.

## What it computes

For each hour in the selected window (Day / Week / Month / Year / Σ Total), the model:

1. Assigns the hour to a **time-of-use period** from its local date/time (see the 2.0TD calendar below).
2. Prices the **grid import** for that hour at the period's all-in €/kWh.
3. Values the **self-consumed PV** — the consumption covered by solar, i.e. `house − grid_import` (≥ 0) — at that same avoided rate. That is the **savings**: the money you did *not* spend because solar covered the load.

Per period and in total it reports: consumption, grid-import, solar-covered kWh, generation, export, grid cost €, and savings €. A summary adds the prorated fixed standing charge, an estimated bill, the "without solar" cost (every consumed kWh bought from the grid), and any export credit.

## The 2.0TD time-of-use calendar

Peninsular Spain, local time. Weekends and configured holidays are entirely valle (P3).

| Period | When (Mon–Fri) | Weekend / holidays |
|--------|----------------|--------------------|
| **P1 punta** (Peak) | 10:00–14:00 and 18:00–22:00 | — |
| **P2 llano** (Standard) | 08:00–10:00, 14:00–18:00, 22:00–24:00 | — |
| **P3 valle** (Off-peak) | 00:00–08:00 | all hours |

Set `"calendar": "2.0TD"` to use this. Any other value (e.g. `"flat"`) treats the first configured period as a single round-the-clock rate.

## All-in price of a kWh

Each period's `price_eur_kwh` in the config is **pre-tax**: the energy commodity + access tolls + system charges. The app then adds the per-kWh electricity tax and VAT:

```
all_in[P] = (price_eur_kwh[P] + electricity_tax_eur_kwh) × (1 + vat_pct / 100)
```

Grid energy costs `all_in[P]` per kWh; a self-consumed PV kWh saves exactly the same (you avoid the commodity, the tolls/charges, the electricity tax, and the VAT on all of it).

## Deriving the default prices from the invoice

The committed defaults were derived from the Energía XXI / Endesa **PVPC 2.0TD** invoice for 19 Apr–19 May 2026 (446.084 kWh over 30 days). The bill's "DESGLOSE" splits the variable term into per-period regulated **tolls + charges** plus a single lumped **energy commodity** cost:

| Component | Value |
|-----------|-------|
| Energy commodity ("Costes de la energía") | €45.44 for 446.084 kWh |
| P1 tolls + charges | €0.097553 /kWh |
| P2 tolls + charges | €0.029267 /kWh |
| P3 tolls + charges | €0.003292 /kWh |
| Electricity tax (impuesto especial) | €1.00 /MWh = €0.001 /kWh |
| VAT (IVA) | 10 % |

**PVPC is hourly-indexed**, so the bill cannot give a clean per-period commodity price — it is one lump sum. We approximate it as a flat average:

```
commodity ≈ 45.44 / 446.084 = 0.101863 €/kWh
```

and add each period's regulated toll+charge to get the pre-tax `price_eur_kwh`:

| Period | commodity + toll/charge = pre-tax | all-in (×1.001 tax, ×1.10 VAT) |
|--------|-----------------------------------|--------------------------------|
| P1 | 0.101863 + 0.097553 = **0.199416** | ≈ **0.2205** |
| P2 | 0.101863 + 0.029267 = **0.131130** | ≈ **0.1453** |
| P3 | 0.101863 + 0.003292 = **0.105155** | ≈ **0.1168** |

The flat-commodity approximation is the main source of inaccuracy: in reality the commodity is higher during P1 hours and lower during P3, so this *understates* the P1/P3 spread. Refine the per-period prices in `config/tariff.json` whenever a new invoice arrives.

## Bono social

The invoice carries a 42.5 % bono-social discount on the energy term of the bonified consumption tranche. The defaults above are **gross** (no bono social) — i.e. the honest "value of solar": what a kWh would cost at the undiscounted regulated price. If you would rather see savings net of your bono-social discount, lower the per-period `price_eur_kwh` accordingly. The model deliberately does **not** implement the bono-social tranche cap — that is billing-engine territory, out of scope for a monitoring estimate.

## Fixed standing charge

`daily_fixed = contracted_power_kw × (power_term_p1 + power_term_p3 + marketing_margin) / 365 + meter_rental_eur_day`

prorated by the window's day count and grossed up by VAT. It feeds the summary's "Fixed" and "Estimated bill" figures only — it is not part of the per-kWh savings (you pay the standing charge whether or not solar covers your load).

## Limitations

- Flat-average commodity price (see above) — replace per-invoice for accuracy.
- Holidays must be listed manually in `config/tariff.json` (`holidays: ["YYYY-MM-DD", …]`); unlisted national/local holidays are billed as their weekday period instead of valle.
- Long windows (Year, Σ Total) only fill as the local history DB accrues data — see the data-retention note in the README.
- Export is a single flat `export_eur_kwh` credit; real surplus-compensation schemes (e.g. *compensación de excedentes*) net against the same bill and are not modelled.
