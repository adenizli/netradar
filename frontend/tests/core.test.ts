import { describe, expect, it } from "vitest";
import {
  parseAircraft,
  parseEvent,
  parseJsonl,
  parseSnapshot,
  parseUpdate,
} from "../src/lib/contracts";
import { demoSeeds, demoUpdates } from "../src/data/demo";
import {
  bearingBetween,
  destination,
  distanceKm,
  hasPosition,
  interpolateCoordinate,
} from "../src/lib/geo";
import { GeofenceMonitor, membersInZone } from "../src/lib/geofence";
import { clusterPoints, placeLabels } from "../src/lib/layout";
import { mergeContact } from "../src/hooks/useTraffic";
import type { Aircraft, AircraftUpdate, Coordinate, Zone } from "../src/types";
const center: Coordinate = [41.085, 29.051];
const zone: Zone = {
  id: "one",
  name: "Yakın alan",
  radiusKm: 20,
  enabled: true,
  color: "#a5efb7",
};
const aircraft = (km: number, icao = "ABC123"): Aircraft => {
  const [latitude, longitude] = destination(center, 90, km);
  return parseAircraft({
    icao,
    latitude,
    longitude,
    speed_knots: 0,
    track_deg: 0,
    position_status: "fresh",
  });
};
const update = (a: Aircraft): AircraftUpdate => ({
  schema_version: "1.1",
  type: "aircraft_update",
  data: a,
  event: null,
});
describe("lossless frontend contract", () => {
  it("preserves unknown nested data, zero, false and null", () => {
    const event = {
      icao: "ABC123",
      event_type: "identification",
      fields: {
        callsign: "TEST",
        future: { n: 0, flag: false, missing: null },
      },
      raw_frame: null,
      observed_at_unix_ms: null,
      metadata: { sample_index: 0, crc_valid: false, future: [0, null, false] },
    };
    const input = {
      schema_version: "1.1",
      type: "aircraft_update",
      data: {
        ...aircraft(0),
        future: { x: false },
        latest_events: { identification: event },
      },
      event,
    };
    expect(parseUpdate(input)).toEqual(input);
    const parsed = parseUpdate(input);
    parsed.event!.fields.callsign = "CHANGE";
    expect(event.fields.callsign).toBe("TEST");
  });
  it("accepts legacy snapshots and unknown position without inventing time", () => {
    const a = parseSnapshot([{ icao: "ABC123" }])[0];
    expect(a.latitude).toBeNull();
    expect(a.last_seen).toBeNull();
    expect(hasPosition(a)).toBe(false);
  });
  it("keeps real zero coordinates and measurements", () => {
    const a = parseAircraft({
      icao: "ABC123",
      latitude: 0,
      longitude: 0,
      speed_knots: 0,
      altitude_ft: 0,
      track_deg: 0,
    });
    expect(hasPosition(a)).toBe(true);
    expect(a.altitude_ft).toBe(0);
    expect(a.track_deg).toBe(0);
  });
  it("does not mix magnetic heading, airspeed or height categories", () => {
    const a = parseAircraft({
      icao: "ABC123",
      heading_deg: 123,
      heading_reference: "magnetic",
      airspeed_knots: 345,
      gnss_height_ft: 38000,
    });
    expect(a.track_deg).toBeNull();
    expect(a.speed_knots).toBeNull();
    expect(a.altitude_ft).toBeNull();
  });
  it.each(["2.0", 1.1, false])("rejects unknown schema %s", (schema_version) =>
    expect(() =>
      parseUpdate({ ...update(aircraft(0)), schema_version }),
    ).toThrow(),
  );
  it.each([
    { latitude: 91 },
    { longitude: -181 },
    { speed_knots: "12" },
    { altitude_ft: Infinity },
    { track_deg: 360 },
    { callsign: 123 },
    { latest_events: [] },
  ])("rejects bad measurements %s", (field) =>
    expect(() => parseAircraft({ ...aircraft(0), ...field })).toThrow(),
  );
  it("rejects nonfinite values in extensions", () =>
    expect(() =>
      parseUpdate({
        ...update(aircraft(0)),
        event: {
          icao: "ABC123",
          event_type: "identification",
          fields: { future: Infinity },
        },
      }),
    ).toThrow());
  it("rejects mismatched event ICAO", () =>
    expect(() =>
      parseUpdate({
        ...update(aircraft(0)),
        event: { icao: "ABC124", event_type: "identification", fields: {} },
      }),
    ).toThrow());
  it("rejects malformed event metadata and raw frames", () => {
    expect(() =>
      parseEvent({
        icao: "ABC123",
        event_type: "identification",
        fields: {},
        metadata: [],
      }),
    ).toThrow();
    expect(() =>
      parseEvent({
        icao: "ABC123",
        event_type: "identification",
        fields: {},
        raw_frame: "BAD",
      }),
    ).toThrow();
  });
  it("rejects an invalid JSONL import atomically with line number", () => {
    expect(() =>
      parseJsonl(JSON.stringify(update(aircraft(0))) + "\n{}"),
    ).toThrow("Satır 2");
    expect(() => parseJsonl("  ")).toThrow();
  });
  it("parses the actual repository UI fixture", () => {
    const seeds = demoSeeds();
    expect(seeds).toHaveLength(13);
    expect(seeds.filter(hasPosition)).toHaveLength(12);
    expect(seeds.every((a) => a.last_seen === null)).toBe(true);
  });
  it("produces valid updates with correctly separate optional measurements", () => {
    for (const t of [0, 1, 1000])
      for (const u of demoUpdates(t)) {
        expect(parseUpdate(u)).toEqual(u);
        expect(u.data.heading_deg).toBeNull();
      }
  });
});
describe("geodesic calculations", () => {
  it.each([0, 1, 20, 250])("distance/destination agree at %s km", (km) =>
    expect(distanceKm(center, destination(center, 130, km))).toBeCloseTo(km, 7),
  );
  it("uses the short path across the date line", () =>
    expect(Math.abs(interpolateCoordinate([0, 179], [0, -179], 0.5)[1])).toBe(
      180,
    ));
  it("computes inclusion for known positions only", () =>
    expect(
      membersInZone(
        [
          aircraft(19),
          aircraft(21, "ABC124"),
          { ...aircraft(1, "ABC125"), latitude: null },
        ],
        zone,
        center,
      ).map((a) => a.icao),
    ).toEqual(["ABC123"]));
});
describe("zone transition semantics", () => {
  it("initial state is a silent baseline", () =>
    expect(
      new GeofenceMonitor().update([aircraft(10)], [zone], center),
    ).toEqual([]));
  it("reports one entry then no duplicates", () => {
    const m = new GeofenceMonitor();
    m.update([aircraft(21)], [zone], center);
    expect(
      m.update([aircraft(19)], [zone], center, 1).map((a) => a.kind),
    ).toEqual(["enter"]);
    expect(m.update([aircraft(18)], [zone], center)).toEqual([]);
  });
  it("debounces noisy boundary fixes with 150 m exit hysteresis", () => {
    const m = new GeofenceMonitor();
    m.update([aircraft(19.99)], [zone], center);
    expect(m.update([aircraft(20.1)], [zone], center)).toEqual([]);
    expect(
      m.update([aircraft(20.2)], [zone], center).map((a) => a.kind),
    ).toEqual(["exit"]);
    expect(m.update([aircraft(20.1)], [zone], center)).toEqual([]);
  });
  it("newly observed aircraft inside an existing zone is an entry", () => {
    const m = new GeofenceMonitor();
    m.update([], [zone], center);
    expect(m.update([aircraft(10)], [zone], center)[0].kind).toBe("enter");
  });
  it("loss of position or expiration does not invent an exit", () => {
    const m = new GeofenceMonitor();
    m.update([aircraft(10)], [zone], center);
    expect(
      m.update([{ ...aircraft(10), latitude: null }], [zone], center),
    ).toEqual([]);
    expect(m.update([], [zone], center)).toEqual([]);
  });
  it("each overlapping zone keeps its own membership", () => {
    const m = new GeofenceMonitor();
    const zones = [zone, { ...zone, id: "two", radiusKm: 40 }];
    m.update([aircraft(45)], zones, center);
    const entries = m.update([aircraft(10)], zones, center);
    expect(entries.map((a) => a.zoneId)).toEqual(["one", "two"]);
  });
  it("adding a zone does not trigger entry for every current aircraft", () => {
    const m = new GeofenceMonitor();
    m.update([aircraft(10)], [zone], center);
    expect(
      m.update([aircraft(10)], [zone, { ...zone, id: "new" }], center),
    ).toEqual([]);
  });
  it("disabled/deleted zones cannot produce alerts", () => {
    const m = new GeofenceMonitor();
    m.update([aircraft(10)], [zone], center);
    expect(
      m.update([aircraft(22)], [{ ...zone, enabled: false }], center),
    ).toEqual([]);
    expect(m.update([aircraft(22)], [], center)).toEqual([]);
  });
  it("reconnect/reset starts a silent baseline", () => {
    const m = new GeofenceMonitor();
    m.update([aircraft(10)], [zone], center);
    m.reset();
    expect(m.update([aircraft(30)], [zone], center)).toEqual([]);
  });
});
describe("dense map layout", () => {
  it("clusters coincident aircraft without losing access to any member", () => {
    const points = Array.from({ length: 500 }, (_, i) => ({
      id: String(i),
      x: i % 40,
      y: i % 25,
    }));
    const clusters = clusterPoints(points);
    expect(clusters.flatMap((c) => c.members)).toHaveLength(500);
    for (const [i, a] of clusters.entries())
      for (const b of clusters.slice(i + 1))
        expect(Math.hypot(a.x - b.x, a.y - b.y)).toBeGreaterThanOrEqual(36);
  });
  it("places labels within bounds and without overlapping markers or reserved controls", () => {
    const points = Array.from({ length: 20 }, (_, i) => ({
      id: String(i),
      x: 50 + (i % 5) * 100,
      y: 100 + Math.floor(i / 5) * 100,
    }));
    const reserved = { x: 0, y: 0, width: 600, height: 80 };
    const labels = placeLabels(points, 600, 600, "3", [reserved]);
    expect(labels.size).toBeGreaterThan(0);
    const rectangles = [...labels.values()];
    for (const [i, a] of rectangles.entries()) {
      expect(a.y).toBeGreaterThanOrEqual(80);
      expect(a.x + a.width).toBeLessThanOrEqual(588);
      for (const b of rectangles.slice(i + 1))
        expect(
          a.x + a.width <= b.x ||
            b.x + b.width <= a.x ||
            a.y + a.height <= b.y ||
            b.y + b.height <= a.y,
        ).toBe(true);
    }
  });
});
describe("contact retention", () => {
  it("bounds trails at 60 measured fixes", () => {
    let c;
    for (let i = 0; i < 90; i++)
      c = mergeContact(c, update(aircraft(i / 10)), i);
    expect(c!.trail).toHaveLength(60);
    expect(c!.receivedAt).toBe(89);
    expect(c!.aircraft.last_seen).toBeNull();
  });
  it("does not grow trails on velocity-only updates or identical fixes", () => {
    const c = mergeContact(undefined, update(aircraft(10)), 1);
    expect(mergeContact(c, update(aircraft(10)), 2).trail).toHaveLength(1);
  });
});

describe("continuous demo routes", () => {
  it("keeps fictional aircraft in the local airspace during long sessions", () => {
    for (const time of [0, 300, 3600, 86400])
      for (const { data: a } of demoUpdates(time))
        if (hasPosition(a))
          expect(distanceKm(center, [a.latitude!, a.longitude!])).toBeLessThan(
            65,
          );
  });
  it("points aircraft along their simulated movement", () => {
    const before = demoUpdates(100),
      after = demoUpdates(101);
    for (let i = 0; i < before.length; i++) {
      const a = before[i].data,
        b = after[i].data;
      if (!hasPosition(a)) continue;
      const actual = bearingBetween(
        [a.latitude!, a.longitude!],
        [b.latitude!, b.longitude!],
      );
      const difference = Math.abs(((a.track_deg! - actual + 540) % 360) - 180);
      expect(difference).toBeLessThan(3);
    }
  });
});
