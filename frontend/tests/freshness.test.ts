import { describe, expect, it } from "vitest";
import {
  mergeContact,
  refreshContact,
  POSITION_STALE_AFTER_MS,
} from "../src/lib/freshness";
import { parseAircraft, parseUpdate } from "../src/lib/contracts";
import { GeofenceMonitor, membersInZone } from "../src/lib/geofence";
import { destination } from "../src/lib/geo";
import type {
  AircraftUpdate,
  Coordinate,
  DecoderEvent,
  Zone,
} from "../src/types";
const center: Coordinate = [41.085, 29.051];
const zone: Zone = {
  id: "zone",
  name: "Alan",
  radiusKm: 20,
  enabled: true,
  color: "#a5efb7",
};
function update(sample = 100, km = 10, age = 0): AircraftUpdate {
  const [latitude, longitude] = destination(center, 90, km);
  const event: DecoderEvent = {
    icao: "ABC123",
    event_type: "airborne_position",
    fields: { latitude, longitude },
    raw_frame: null,
    observed_at_unix_ms: null,
    metadata: {
      capture_id: "one",
      source: "rf",
      sample_index: sample,
      sample_rate_hz: 1000,
    },
  };
  return {
    type: "aircraft_update",
    event,
    data: parseAircraft({
      icao: "ABC123",
      latitude,
      longitude,
      position_revision: sample,
      position_age_ms: age,
      position_event: event,
    }),
  };
}
describe("independent position freshness", () => {
  it("buffered stream summaries cannot roll a newer snapshot back", () => {
    const snapshot = update();
    Object.assign(snapshot.data, {
      stream_id: "server-one",
      stream_sequence: 20,
      callsign: "NEWNAME",
      speed_knots: 400,
    });
    const first = mergeContact(undefined, snapshot, 1000, false, 0);
    const buffered = update();
    Object.assign(buffered.data, {
      stream_id: "server-one",
      stream_sequence: 10,
      callsign: "OLDNAME",
      speed_knots: 100,
    });
    const next = mergeContact(first, buffered, 2000, false, 1000);
    expect(next.aircraft.callsign).toBe("NEWNAME");
    expect(next.aircraft.speed_knots).toBe(400);
    expect(next.aircraft.stream_sequence).toBe(20);
    expect(next.event).toEqual(buffered.event);
    expect(next.aircraft.position_age_ms).toBe(1000);
    const later = update(200);
    Object.assign(later.data, {
      stream_id: "server-one",
      stream_sequence: 21,
      callsign: "LATER",
    });
    expect(mergeContact(next, later, 3000, false, 2000).aircraft.callsign).toBe(
      "LATER",
    );
  });
  it("stream sequence restarts are isolated by server session identity", () => {
    const firstUpdate = update();
    Object.assign(firstUpdate.data, {
      stream_id: "server-one",
      stream_sequence: 500,
      callsign: "OLD",
    });
    const first = mergeContact(undefined, firstUpdate, 1000, false, 0);
    const restarted = update(200);
    Object.assign(restarted.data, {
      stream_id: "server-two",
      stream_sequence: 1,
      callsign: "NEW",
    });
    expect(
      mergeContact(first, restarted, 2000, false, 1000).aircraft.callsign,
    ).toBe("NEW");
  });
  it("validates paired stream identities on envelopes and aircraft", () => {
    const value = update();
    Object.assign(value.data, { stream_id: "server", stream_sequence: 2 });
    expect(() => parseUpdate(value)).toThrow();
    Object.assign(value, { stream_id: "server", stream_sequence: 2 });
    expect(parseUpdate(value).stream_sequence).toBe(2);
    expect(() =>
      parseAircraft({ ...value.data, stream_sequence: -1 }),
    ).toThrow();
    expect(() => parseAircraft({ ...value.data, stream_id: "" })).toThrow();
  });
  it("callsign messages never reset the position age", () => {
    const first = mergeContact(undefined, update(), 1000, false, 0);
    const identity = {
      ...update(),
      event: {
        icao: "ABC123",
        event_type: "identification",
        fields: { callsign: "TEST" },
        raw_frame: null,
        observed_at_unix_ms: null,
      } as DecoderEvent,
    };
    const next = mergeContact(first, identity, 41000, false, 40000);
    expect(next.aircraft.position_age_ms).toBe(40000);
    expect(next.aircraft.position_status).toBe("stale");
    expect(next.receivedAt).toBe(41000);
  });
  it("ages without any incoming messages", () => {
    const first = mergeContact(undefined, update(), 1000, false, 0);
    expect(
      refreshContact(first, POSITION_STALE_AFTER_MS).aircraft.position_status,
    ).toBe("stale");
  });
  it("same coordinates with a genuinely newer fix become fresh again", () => {
    const first = mergeContact(undefined, update(), 1000, false, 0);
    const stale = refreshContact(first, 40000);
    const next = mergeContact(stale, update(200), 41000, false, 40000);
    expect(next.aircraft.position_status).toBe("fresh");
    expect(next.aircraft.position_age_ms).toBe(0);
    expect(next.trail).toHaveLength(1);
  });
  it("repeated or retransmitted fix cannot become young again", () => {
    const first = mergeContact(undefined, update(), 1000, false, 0);
    const duplicate = mergeContact(first, update(), 41000, false, 40000);
    expect(duplicate.aircraft.position_status).toBe("stale");
  });
  it("old snapshot is stale immediately on reconnect", () => {
    const snapshot = { ...update(100, 10, 90000), event: null };
    expect(
      mergeContact(undefined, snapshot, 100000, false, 0).aircraft
        .position_status,
    ).toBe("stale");
  });
  it("timestamp-free legacy snapshot is unknown rather than fresh", () => {
    const legacy = {
      type: "aircraft_update" as const,
      data: parseAircraft({ icao: "ABC123", latitude: 41, longitude: 29 }),
      event: null,
    };
    const c = mergeContact(undefined, legacy, 1000, false, 0);
    expect(c.aircraft.position_status).toBe("unknown");
    expect(membersInZone([c.aircraft], zone, center)).toHaveLength(0);
  });
  it("late old update cannot roll a newer map position backward", () => {
    const first = mergeContact(undefined, update(200, 10), 1000, false, 0);
    const old = mergeContact(first, update(100, 40), 2000, false, 1000);
    expect(old.aircraft.longitude).toBe(first.aircraft.longitude);
    expect(old.trail).toHaveLength(1);
    expect(old.event!.metadata!.sample_index).toBe(100);
  });
  it("client clock adjustments do not drive position age", () => {
    const first = mergeContact(undefined, update(), 100000, false, 0);
    const next = mergeContact(first, update(), 1000, false, 40000);
    expect(next.aircraft.position_status).toBe("stale");
    expect(next.aircraft.position_age_ms).toBe(40000);
  });
  it("demo positions remain fresh while the simulation is paused", () => {
    const first = mergeContact(undefined, update(), 1000, true, 0);
    expect(refreshContact(first, 600000, true).aircraft.position_status).toBe(
      "fresh",
    );
  });
});
describe("zones exclude uncertain fixes without inventing crossings", () => {
  it("stale positions are not counted and expiring them creates no exit", () => {
    const first = mergeContact(undefined, update(), 1000, false, 0);
    const m = new GeofenceMonitor();
    m.update([first.aircraft], [zone], center);
    const stale = refreshContact(first, 40000);
    expect(membersInZone([stale.aircraft], zone, center)).toHaveLength(0);
    expect(m.update([stale.aircraft], [zone], center)).toEqual([]);
  });
  it("recovery after an unknown interval establishes a baseline, not an exit", () => {
    const first = mergeContact(undefined, update(), 1000, false, 0);
    const m = new GeofenceMonitor();
    m.update([first.aircraft], [zone], center);
    m.update([refreshContact(first, 40000).aircraft], [zone], center);
    const recovered = mergeContact(first, update(200, 40), 41000, false, 40000);
    expect(m.update([recovered.aircraft], [zone], center)).toEqual([]);
    const backInside = mergeContact(
      recovered,
      update(300, 10),
      42000,
      false,
      41000,
    );
    expect(
      m.update([backInside.aircraft], [zone], center).map((a) => a.kind),
    ).toEqual(["enter"]);
  });
});

describe("freshness clock edge cases", () => {
  it("new observation time refreshes stationary fix even with unchanged legacy sample index", () => {
    const first = update();
    first.data.position_observed_at_unix_ms = 1000;
    first.data.position_event!.observed_at_unix_ms = 1000;
    const old = mergeContact(undefined, first, 1000, false, 0);
    const next = update();
    next.data.position_observed_at_unix_ms = 41000;
    next.data.position_event!.observed_at_unix_ms = 41000;
    expect(
      mergeContact(old, next, 41000, false, 40000).aircraft.position_status,
    ).toBe("fresh");
  });
  it("age without identifiable fix cannot manufacture freshness", () => {
    const u = {
      type: "aircraft_update" as const,
      event: null,
      data: parseAircraft({
        icao: "ABC123",
        latitude: 41,
        longitude: 29,
        position_age_ms: 0,
      }),
    };
    expect(
      mergeContact(undefined, u, 1000, false, 0).aircraft.position_status,
    ).toBe("unknown");
  });
});
