import type { Aircraft, AircraftUpdate, Contact, DecoderEvent } from "../types";
import { hasPosition } from "./geo";

// Application policy, not an ADS-B protocol limit.
export const POSITION_STALE_AFTER_MS = 30_000;
const finite = (v: unknown): v is number =>
  typeof v === "number" && Number.isFinite(v);

function fixEvent(
  a: Aircraft,
  event: DecoderEvent | null,
): DecoderEvent | null {
  if (a.position_event) return a.position_event;
  const matches = (e: DecoderEvent) =>
    e.event_type === "airborne_position" &&
    e.fields.latitude === a.latitude &&
    e.fields.longitude === a.longitude;
  if (event && matches(event)) return event;
  return Object.values(a.latest_events).find(matches) ?? null;
}
function sampleClock(
  event: DecoderEvent | null,
): [string, string, number, number] | null {
  const m = event?.metadata;
  return m &&
    typeof m.capture_id === "string" &&
    m.capture_id &&
    typeof m.source === "string" &&
    finite(m.sample_index) &&
    Number.isSafeInteger(m.sample_index) &&
    m.sample_index >= 0 &&
    finite(m.sample_rate_hz) &&
    Number.isSafeInteger(m.sample_rate_hz) &&
    m.sample_rate_hz > 0
    ? [m.capture_id, m.source, m.sample_rate_hz, m.sample_index]
    : null;
}
function observed(a: Aircraft, e: DecoderEvent | null): number | null {
  return a.position_observed_at_unix_ms ?? e?.observed_at_unix_ms ?? null;
}
function keyFor(a: Aircraft, event: DecoderEvent | null): string | null {
  if (!hasPosition(a)) return null;
  const samples = sampleClock(event),
    time = observed(a, event);
  if (samples)
    return JSON.stringify(["sample", samples, time, a.latitude, a.longitude]);
  if (finite(time))
    return JSON.stringify(["observed", time, a.latitude, a.longitude]);
  if (a.position_revision && a.position_revision > 0)
    return JSON.stringify([
      "revision",
      a.position_revision,
      a.position_received_at_unix_ms,
      a.latitude,
      a.longitude,
    ]);
  return null;
}
function compareFix(
  a: Aircraft,
  e: DecoderEvent | null,
  b: Aircraft,
  oldEvent: DecoderEvent | null,
): number | null {
  const time = observed(a, e),
    oldTime = observed(b, oldEvent);
  if (finite(time) && finite(oldTime) && time !== oldTime)
    return Math.sign(time - oldTime);
  const s = sampleClock(e),
    old = sampleClock(oldEvent);
  if (s && old && s.slice(0, 3).every((v, i) => v === old[i]))
    return Math.sign(s[3] - old[3]);
  if (finite(time) && finite(oldTime)) return 0;
  if (!finite(time) && finite(oldTime)) return -1;
  if (finite(time) && !finite(oldTime)) return 1;
  return null;
}
function ageOf(c: Contact, tick: number): number | null {
  return c.positionAgeAtReceiptMs === null
    ? null
    : c.positionAgeAtReceiptMs + Math.max(0, tick - c.positionClockAt);
}
function status(age: number | null, a: Aircraft) {
  return !hasPosition(a) || age === null
    ? ("unknown" as const)
    : age >= POSITION_STALE_AFTER_MS
      ? ("stale" as const)
      : ("fresh" as const);
}
export function refreshContact(
  c: Contact,
  tick: number,
  demo = false,
): Contact {
  const age = demo && hasPosition(c.aircraft) ? 0 : ageOf(c, tick);
  const nextStatus = status(age, c.aircraft);
  // One-second display precision; the fresh/stale boundary is still exact.
  const displayAge = age === null ? null : Math.floor(age / 1000) * 1000;
  if (
    c.aircraft.position_status === nextStatus &&
    c.aircraft.position_age_ms === displayAge
  )
    return c;
  return {
    ...c,
    aircraft: {
      ...c.aircraft,
      position_status: nextStatus,
      position_age_ms: displayAge,
    },
  };
}
export function mergeContact(
  old: Contact | undefined,
  update: AircraftUpdate,
  now: number,
  demo = false,
  tick = performance.now(),
): Contact {
  let a = { ...update.data };
  // Buffered events may precede the HTTP snapshot. Keep its newer full summary,
  // including callsign/velocity, while retaining the incoming raw event below.
  if (
    !demo &&
    old &&
    a.stream_id &&
    a.stream_id === old.aircraft.stream_id &&
    typeof a.stream_sequence === "number" &&
    typeof old.aircraft.stream_sequence === "number" &&
    a.stream_sequence <= old.aircraft.stream_sequence
  ) {
    a = { ...old.aircraft };
  }
  let evidence = fixEvent(a, update.event);
  let key = keyFor(a, evidence);
  let changed = !old || key !== old.positionKey;
  if (
    old &&
    old.positionKey &&
    changed &&
    !demo &&
    compareFix(
      a,
      evidence,
      old.aircraft,
      old.aircraft.position_event ?? null,
    ) !== 1
  ) {
    // Keep the full incoming event for inspection, but do not roll the map back.
    for (const field of [
      "latitude",
      "longitude",
      "position_source",
      "position_revision",
      "position_event",
      "position_observed_at_unix_ms",
      "position_received_at_unix_ms",
      "position_age_ms",
    ] as const) {
      (a as Record<string, unknown>)[field] = old.aircraft[field];
    }
    evidence = a.position_event ?? null;
    key = old.positionKey;
    changed = false;
  }
  if (evidence) a.position_event = evidence;
  const measured = observed(a, evidence);
  const reportedAge =
    key === null
      ? null
      : finite(a.position_age_ms)
        ? Math.max(0, a.position_age_ms)
        : finite(measured)
          ? Math.max(0, now - measured)
          : null;
  const previousAge = old ? ageOf(old, tick) : null;
  const age =
    demo && hasPosition(a)
      ? 0
      : !changed && old
        ? previousAge === null
          ? reportedAge
          : Math.max(previousAge, reportedAge ?? 0)
        : reportedAge;
  const trail = old?.trail.slice() ?? [];
  const last = trail.at(-1);
  if (
    hasPosition(a) &&
    (!last || last[0] !== a.latitude || last[1] !== a.longitude)
  )
    trail.push([a.latitude!, a.longitude!]);
  const contact: Contact = {
    aircraft: a,
    event: update.event ?? old?.event ?? null,
    receivedAt: now,
    trail: trail.slice(-60),
    positionKey: key,
    positionAgeAtReceiptMs: age,
    positionClockAt: tick,
  };
  return refreshContact(contact, tick, demo);
}
export const positionAgeText = (a: Aircraft) =>
  typeof a.position_age_ms === "number"
    ? `${Math.floor(a.position_age_ms / 1000)} sn`
    : "Bilinmiyor";
export function positionStatusText(a: Aircraft): string {
  if (!hasPosition(a)) return "Konum bekleniyor";
  if (a.position_status === "fresh") return "Konum güncel";
  if (a.position_status === "stale")
    return `Konum eski · ${positionAgeText(a)}`;
  return "Konum zamanı bilinmiyor";
}
