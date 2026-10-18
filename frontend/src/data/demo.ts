import fixture from "../../../fixtures/aircraft_updates.mock.jsonl?raw";
import type { Aircraft, AircraftUpdate } from "../types";
import { parseJsonl } from "../lib/contracts";
import { bearingBetween, destination, distanceKm } from "../lib/geo";
const center: [number, number] = [41.085, 29.051];
const extras = [
  [325, 34, 145, 410, 32000],
  [60, 37, 240, 440, 36000],
  [165, 31, 345, 310, 18000],
  [275, 39, 95, 465, 38000],
  [215, 49, 35, 390, 29000],
  [20, 52, 200, 420, 34000],
  [118, 48, 300, 460, 37000],
  [240, 15, 75, 280, 12000],
  [100, 19.4, 280, 300, 15000],
];
export function demoSeeds(): Aircraft[] {
  const seeds = parseJsonl(fixture).map((u) => ({
    ...u.data,
    last_seen: null,
    last_seen_unix_ms: null,
  }));
  for (const [
    i,
    [bearing, distance, track, speed, altitude],
  ] of extras.entries()) {
    const [latitude, longitude] = destination(center, bearing, distance);
    seeds.push({
      ...seeds[0],
      icao: `D000${(i + 4).toString(16).padStart(2, "0").toUpperCase()}`,
      callsign: `DEMO${(i + 4) * 101}`,
      latitude,
      longitude,
      track_deg: track,
      speed_knots: speed,
      altitude_ft: altitude,
      vertical_rate_fpm: i % 3 === 0 ? 640 : i % 3 === 1 ? -832 : 0,
    });
  }
  seeds.push({
    ...seeds[0],
    icao: "D0000D",
    callsign: "DEMO1313",
    latitude: null,
    longitude: null,
    altitude_ft: null,
    speed_knots: null,
    track_deg: null,
  });
  return seeds;
}
export function demoUpdates(seconds: number): AircraftUpdate[] {
  return demoSeeds().map((seed) => {
    const moving =
      seed.latitude !== null &&
      seed.longitude !== null &&
      seed.track_deg !== null &&
      seed.speed_knots !== null;
    if (!moving)
      return {
        schema_version: "1.1",
        type: "aircraft_update",
        data: { ...seed },
        event: null,
      };
    // Closed fictional routes remain in view during a long demo. Ground track is
    // the bearing of the actual route tangent, never a decorative fixed rotation.
    const start: [number, number] = [seed.latitude!, seed.longitude!];
    const orbitCenter = destination(center, bearingBetween(center, start), 24);
    const radius = Math.max(2, distanceKm(orbitCenter, start));
    const initialBearing = bearingBetween(orbitCenter, start);
    const angle =
      initialBearing +
      (((seed.speed_knots! * 1.852 * seconds) / 3600 / radius) * 180) / Math.PI;
    const position =
      seconds === 0 ? start : destination(orbitCenter, angle, radius);
    const next = destination(orbitCenter, angle + 0.001, radius);
    return {
      schema_version: "1.1",
      type: "aircraft_update",
      data: {
        ...seed,
        latitude: position[0],
        longitude: position[1],
        track_deg: bearingBetween(position, next),
      },
      event: null,
    };
  });
}
