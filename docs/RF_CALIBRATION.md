# RF audit and gain/PPM calibration

Field procedures for issue #10 (antenna, cable, line of sight) and issue #11 (gain and
oscillator PPM). They need the Raspberry Pi, the dongle and real air traffic. The tools
run anywhere, but only real-RF results count. Never decide on hardware from synthetic data.

Rules for every experiment:

- Change **one** factor at a time and record it in the site file (`docs/FIELD_BASELINE.md`).
- Compare equal windows in similar traffic hours, and repeat A/B/A/B. One pair is not proof.
- Rate a setting by CRC-valid frames, distinct and positioned ICAOs and measured range with
  **zero IQ loss**. Never rate it by raw preamble candidates.
- Keep exact coordinates, photos with addresses and raw IQ private (`.netradar-run/` is ignored).
- Stop the always-on service first if it is installed: `systemctl --user stop netradar.service`
  (the gain sweep can do this with `--stop-service`).

## 1. Station audit (#10)

Fill in what you can verify; write "unknown" rather than guessing.

| Item | Value |
|---|---|
| Pi model, power supply, USB port / hub | |
| Dongle model and tuner (`rtl_test -t`), serial | |
| Antenna model; is it made for 1090 MHz? element length (cm) | |
| Placement: indoor/window/roof; height above ground and above surrounding roofs (m) | |
| Obstructions by direction (N/E/S/W): buildings, hills, metal, glass type | |
| Cable type and length; connectors and adapters | |
| Filter / LNA (model, position in the chain) | |
| Nearby transmitters or noise (cell towers, USB 3 devices, switching supplies) | |
| Grounding and weatherproofing (outdoor) | |

Estimate geometry and feedline loss before buying anything:

```bash
.venv/bin/python -m tools.rf_link --antenna-height-m 6 --cable rg58 --cable-length-m 5
```

It prints the smooth-earth line-of-sight limit per altitude, the cable loss and its effect on
free-space range (each 6 dB halves it), and the 1090 MHz quarter/half-wave lengths (6.9/13.8 cm).
The loss table uses typical datasheet values near 1 GHz; pass `--loss-db-per-m` for your cable.

### A/B test of one change

1. Baseline **A** with the current setup and fixed gain/PPM:
   `.venv/bin/python -m tools.field_baseline --minutes 10 --gain-db 33.8 --site site-a.json --diagnostics`
2. Make one change (e.g. move the antenna to the window or roof) and record it in `site-b.json`.
3. Run **B** with the same duration, gain and PPM, as close in time of day as possible.
4. Compare:
   `.venv/bin/python -m tools.field_compare .netradar-run/field/<A> .netradar-run/field/<B> --output ab.md`
5. Repeat A and B at least once more. Trust the change only if B wins every time.

`field_compare` lists every recorded condition that differs and warns when the comparison is
confounded: more than one changed factor, different durations, more than 2 h apart in time of
day, incomplete runs, non-RF sources or IQ drops. It reports valid DF17/s, distinct and
positioned ICAOs, p50/p95/max range, CRC rejects, snippet clipping and per-30° sector range.

### Ranking hardware changes

Write the result as a ranked table. Only list a purchase when a measurement supports it.

| Candidate change | Cost | Evidence that it is the bottleneck | How to test |
|---|---|---|---|
| Move antenna higher / clear of walls | free | sectors with no fixes, short p95 vs line-of-sight limit | A/B placement, same gain |
| Antenna tuned for 1090 MHz | low | stock whip, element length far from 6.9/13.8 cm | A/B antenna, same place and gain |
| Shorter or lower-loss cable | low–medium | `rf_link` shows several dB of loss | A/B cable, same antenna |
| 1090 MHz band-pass filter | medium | high noise floor or clipping in snippets, strong nearby transmitters | A/B filter, compare clip % and CRC rejects |
| LNA at the antenna | medium | long cable loss **and** no clipping; an LNA can overload the dongle | A/B with gain re-checked (section 2) |

## 2. Gain and PPM sweep (#11)

```bash
.venv/bin/python -m tools.gain_sweep --dry-run          # shows the schedule and duration
.venv/bin/python -m tools.gain_sweep --stop-service     # default: auto + gains near 22–49.6 dB
.venv/bin/python -m tools.gain_sweep --gains 28 33.8 40.2 --rounds 3 --window-seconds 300
```

Without `--gains` the tool reads the tuner's supported gains from `rtl_test -t` and tests `auto`
plus the supported values nearest to 22, 29, 35, 40, 45 and 49.6 dB. Each window records a 1 s
raw snippet for clipping and noise (`tools/iq_health.py`), then runs the production pipeline for
the window length. Every round measures each setting once, in a new shuffled order (the seed
is in the report), to spread air-traffic changes over all settings.

The report in `.netradar-run/gain-sweep/<UTC time>/report.md` (plus `report.json`) ranks settings
by median CRC-valid frames per minute. A setting is **eligible** only with zero dropped IQ samples
and at most 1% clipping (`--max-clip-pct`). The tool recommends a new setting only if it is eligible
and beats the baseline (default 35 dB, snapped to a supported gain) by more than 10% in **every**
paired round, with at least two rounds. Otherwise it keeps the baseline and says why. There is no
automatic gain chasing during normal operation.

PPM: ADS-B uses on/off keying, so a few tens of ppm (±55 kHz at ±50 ppm) usually matter little.
Measure the dongle once with `rtl_test -p` (let it run a few minutes) and test that value against
0 with `--ppm 0 <measured>`. Keep 0 unless the sweep shows a consistent gain.

Apply the chosen gain to the service in `~/.config/netradar.env` (`NETRADAR_GAIN_DB=33.8`) and
restart it: `systemctl --user restart netradar.service`.
