import type { Aircraft, Alert, Coordinate, Zone } from "../types";
import { distanceKm, flightName, hasFreshPosition } from "./geo";
/** Silent baseline, 150 m exit hysteresis, and no invented exits on loss of data. */
export class GeofenceMonitor {
  private membership = new Map<string, Set<string>>();
  private uncertain = new Map<string, Set<string>>();
  reset() {
    this.membership.clear();
    this.uncertain.clear();
  }
  update(
    aircraft: Aircraft[],
    zones: Zone[],
    center: Coordinate,
    now = Date.now(),
  ): Alert[] {
    const alerts: Alert[] = [];
    const activeIds = new Set(zones.filter((z) => z.enabled).map((z) => z.id));
    for (const id of this.membership.keys())
      if (!activeIds.has(id)) {
        this.membership.delete(id);
        this.uncertain.delete(id);
      }
    for (const zone of zones.filter((z) => z.enabled)) {
      const previous = this.membership.get(zone.id);
      const next = new Set<string>();
      const unknown = this.uncertain.get(zone.id) ?? new Set<string>();
      const present = new Set(aircraft.map((a) => a.icao));
      for (const id of unknown) if (!present.has(id)) unknown.delete(id);
      for (const a of aircraft) {
        if (!hasFreshPosition(a)) {
          unknown.add(a.icao);
          if (previous?.has(a.icao)) next.add(a.icao);
          continue;
        }
        const d = distanceKm(center, [a.latitude!, a.longitude!]);
        const recovered = unknown.delete(a.icao);
        const wasInside = !recovered && (previous?.has(a.icao) ?? false);
        const inside = d <= zone.radiusKm + (wasInside ? 0.15 : 0);
        if (inside) next.add(a.icao);
        if (previous && !recovered && inside !== wasInside)
          alerts.push({
            id: `${zone.id}:${a.icao}:${now}:${inside}`,
            kind: inside ? "enter" : "exit",
            icao: a.icao,
            callsign: flightName(a),
            zoneId: zone.id,
            zoneName: zone.name,
            time: now,
          });
      }
      // Missing aircraft silently leave the baseline: expiration/disconnect isn't a crossing.
      this.membership.set(zone.id, next);
      this.uncertain.set(zone.id, unknown);
    }
    return alerts;
  }
}
export function membersInZone(
  aircraft: Aircraft[],
  zone: Zone,
  center: Coordinate,
): Aircraft[] {
  return aircraft
    .filter(
      (a) =>
        hasFreshPosition(a) &&
        distanceKm(center, [a.latitude!, a.longitude!]) <= zone.radiusKm,
    )
    .sort((a, b) => flightName(a).localeCompare(flightName(b)));
}
