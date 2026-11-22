#!/usr/bin/env python3
"""1090 MHz station geometry and feedline estimates for the RF audit (issue #10).

Prints, for the given antenna height and cable:
- radio line-of-sight limit to aircraft at common altitudes (smooth earth, 4/3 radius),
- estimated feedline loss and what it does to free-space range,
- 1090 MHz quarter- and half-wave element lengths to check an antenna.

These are estimates for planning A/B tests, not measurements. Terrain, buildings and
indoor walls cut range further; check the measured range with tools/field_baseline.py.
"""

from __future__ import annotations

import argparse
import json
import math

FREQ_HZ = 1_090_000_000
C = 299_792_458
# Approximate matched loss near 1 GHz in dB per metre, from typical datasheets.
# Check your cable's datasheet; pass --loss-db-per-m to override.
CABLE_LOSS_DB_PER_M = {
    "rg174": 1.00,
    "rg58": 0.70,
    "rg213": 0.27,
    "rg6": 0.22,
    "lmr240": 0.30,
    "lmr400": 0.14,
}
ALTITUDES_FT = (5_000, 10_000, 20_000, 30_000, 40_000)
EFFECTIVE_EARTH_RADIUS_KM = 6371.0088 * 4 / 3


def horizon_km(height_m: float) -> float:
    """Distance to the radio horizon over smooth earth with standard refraction."""
    return math.sqrt(2 * EFFECTIVE_EARTH_RADIUS_KM * max(0.0, height_m) / 1000)


def line_of_sight_km(antenna_m: float, aircraft_ft: float) -> float:
    return horizon_km(antenna_m) + horizon_km(aircraft_ft * 0.3048)


def feedline_loss_db(cable: str | None, length_m: float, connectors: int,
                     connector_loss_db: float, loss_db_per_m: float | None) -> float | None:
    per_m = loss_db_per_m if loss_db_per_m is not None else CABLE_LOSS_DB_PER_M.get((cable or "").lower())
    if per_m is None:
        return None
    return per_m * length_m + connectors * connector_loss_db


def range_factor(loss_db: float) -> float:
    """Free-space range scales with 10^(-loss/20): every 6 dB halves it."""
    return 10 ** (-loss_db / 20)


def element_lengths_cm(velocity_factor: float = 1.0) -> dict:
    wavelength_cm = C / FREQ_HZ * 100 * velocity_factor
    return {"wavelength_cm": wavelength_cm, "half_wave_cm": wavelength_cm / 2,
            "quarter_wave_cm": wavelength_cm / 4}


def estimate(antenna_height_m, cable, cable_length_m, connectors, connector_loss_db, loss_db_per_m):
    loss = feedline_loss_db(cable, cable_length_m, connectors, connector_loss_db, loss_db_per_m)
    return {
        "antenna_height_m": antenna_height_m,
        "line_of_sight_km": {str(alt): line_of_sight_km(antenna_height_m, alt) for alt in ALTITUDES_FT},
        "feedline": {
            "cable": cable, "length_m": cable_length_m, "connectors": connectors,
            "loss_db": loss, "free_space_range_factor": None if loss is None else range_factor(loss),
        },
        "elements_cm": element_lengths_cm(),
    }


def markdown(result: dict) -> str:
    lines = [f"# 1090 MHz station estimate (antenna {result['antenna_height_m']:g} m above surroundings)", "",
             "| Aircraft altitude | Line-of-sight limit |", "|---:|---:|"]
    for alt, km in result["line_of_sight_km"].items():
        lines.append(f"| {int(alt):,} ft | {km:.0f} km |")
    feed = result["feedline"]
    lines += ["", "## Feedline", ""]
    if feed["loss_db"] is None:
        lines.append(f"Unknown cable `{feed['cable']}`; pass --loss-db-per-m from its datasheet.")
    else:
        lines.append(f"{feed['length_m']:g} m of {feed['cable'] or 'cable'} + {feed['connectors']} connector(s): "
                     f"about **{feed['loss_db']:.1f} dB** loss, which alone limits free-space range to "
                     f"**{feed['free_space_range_factor'] * 100:.0f}%** of a lossless feed.")
    el = result["elements_cm"]
    lines += ["", "## Antenna elements at 1090 MHz", "",
              f"Wavelength {el['wavelength_cm']:.1f} cm · half-wave {el['half_wave_cm']:.2f} cm · "
              f"quarter-wave {el['quarter_wave_cm']:.2f} cm (free space, before velocity factor).",
              "A whip much longer than the quarter-wave is not tuned for 1090 MHz.", "",
              "Estimates only: terrain, buildings and walls reduce range further. Measure with",
              "tools/field_baseline.py and compare A/B runs with tools/field_compare.py.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--antenna-height-m", type=float, required=True,
                        help="Antenna height above the surrounding terrain/rooftops")
    parser.add_argument("--cable", choices=sorted(CABLE_LOSS_DB_PER_M), help="Cable type")
    parser.add_argument("--cable-length-m", type=float, default=0.0)
    parser.add_argument("--loss-db-per-m", type=float, help="Datasheet loss at ~1090 MHz (overrides --cable)")
    parser.add_argument("--connectors", type=int, default=2)
    parser.add_argument("--connector-loss-db", type=float, default=0.1)
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown")
    args = parser.parse_args(argv)
    if args.antenna_height_m < 0 or args.cable_length_m < 0 or args.connectors < 0:
        parser.error("heights, lengths and connector counts must not be negative")
    result = estimate(args.antenna_height_m, args.cable, args.cable_length_m, args.connectors,
                      args.connector_loss_db, args.loss_db_per_m)
    print(json.dumps(result, indent=2) if args.json else markdown(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
