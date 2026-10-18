import type { Aircraft, AircraftUpdate, DecoderEvent } from "../types";
export const object = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);
function assertJson(value: unknown) {
  const stack: unknown[] = [value];
  while (stack.length) {
    const item = stack.pop();
    if (typeof item === "number" && !Number.isFinite(item))
      throw new Error("Sonlu olmayan JSON sayısı.");
    if (item && typeof item === "object") stack.push(...Object.values(item));
  }
}
const numberFields = [
  "latitude",
  "longitude",
  "altitude_ft",
  "gnss_height_ft",
  "speed_knots",
  "track_deg",
  "airspeed_knots",
  "heading_deg",
  "vertical_rate_fpm",
  "last_seen_unix_ms",
  "position_observed_at_unix_ms",
  "position_received_at_unix_ms",
  "position_age_ms",
];
const stringFields = [
  "callsign",
  "airspeed_type",
  "heading_reference",
  "vertical_rate_source",
  "last_seen",
  "position_source",
];
function version(v: unknown) {
  if (v !== undefined && v !== "1.0" && v !== "1.1")
    throw new Error("Desteklenmeyen veri şeması.");
}
export function parseEvent(v: unknown): DecoderEvent {
  if (
    !object(v) ||
    typeof v.icao !== "string" ||
    !/^[0-9A-F]{6}$/.test(v.icao) ||
    typeof v.event_type !== "string" ||
    !object(v.fields)
  )
    throw new Error("Geçersiz decoder mesajı.");
  version(v.schema_version);
  assertJson(v);
  if (v.metadata !== undefined && !object(v.metadata))
    throw new Error("Geçersiz mesaj metadatası.");
  if (
    v.raw_frame != null &&
    (typeof v.raw_frame !== "string" || !/^[0-9A-F]{28}$/.test(v.raw_frame))
  )
    throw new Error("Geçersiz ham mesaj.");
  if (
    v.observed_at_unix_ms != null &&
    (typeof v.observed_at_unix_ms !== "number" ||
      !Number.isFinite(v.observed_at_unix_ms))
  )
    throw new Error("Geçersiz ölçüm zamanı.");
  return structuredClone(v) as unknown as DecoderEvent;
}
export function parseAircraft(v: unknown): Aircraft {
  assertJson(v);
  if (!object(v) || typeof v.icao !== "string" || !/^[0-9A-F]{6}$/.test(v.icao))
    throw new Error("Geçersiz ICAO adresi.");
  for (const key of numberFields)
    if (
      v[key] != null &&
      (typeof v[key] !== "number" || !Number.isFinite(v[key]))
    )
      throw new Error(`Geçersiz sayısal alan: ${key}`);
  for (const key of stringFields)
    if (v[key] != null && typeof v[key] !== "string")
      throw new Error(`Geçersiz metin alanı: ${key}`);
  if (typeof v.latitude === "number" && Math.abs(v.latitude) > 90)
    throw new Error("Geçersiz enlem.");
  if (typeof v.longitude === "number" && Math.abs(v.longitude) > 180)
    throw new Error("Geçersiz boylam.");
  if (
    v.track_deg != null &&
    ((v.track_deg as number) < 0 || (v.track_deg as number) >= 360)
  )
    throw new Error("Geçersiz yer izi.");
  if (v.position_age_ms != null && (v.position_age_ms as number) < 0)
    throw new Error("Geçersiz konum yaşı.");
  if (
    v.position_revision != null &&
    (!Number.isSafeInteger(v.position_revision) ||
      (v.position_revision as number) < 0)
  )
    throw new Error("Geçersiz konum sürümü.");
  const positionEvent =
    v.position_event == null ? v.position_event : parseEvent(v.position_event);
  if (
    positionEvent &&
    (positionEvent.icao !== v.icao ||
      positionEvent.event_type !== "airborne_position" ||
      positionEvent.fields.latitude !== v.latitude ||
      positionEvent.fields.longitude !== v.longitude)
  )
    throw new Error("Konum ve kaynak mesaj uyuşmuyor.");
  const latest = v.latest_events ?? {};
  if (!object(latest)) throw new Error("Geçersiz mesaj arşivi.");
  const latestEvents: Record<string, DecoderEvent> = Object.create(null);
  for (const [key, event] of Object.entries(latest)) {
    const parsed = parseEvent(event);
    if (parsed.icao !== v.icao) throw new Error("Mesaj ICAO adresi uyuşmuyor.");
    latestEvents[key] = parsed;
  }
  // Defaults only fill absent optional fields. Unknown extensions survive unchanged.
  return {
    ...Object.fromEntries(
      [...numberFields, ...stringFields].map((k) => [k, null]),
    ),
    ...structuredClone(v),
    latest_events: latestEvents,
    ...(positionEvent !== undefined ? { position_event: positionEvent } : {}),
  } as unknown as Aircraft;
}
export function parseUpdate(v: unknown): AircraftUpdate {
  if (!object(v) || v.type !== "aircraft_update")
    throw new Error("Geçersiz uçak güncellemesi.");
  version(v.schema_version);
  assertJson(v);
  const data = parseAircraft(v.data);
  const event = v.event == null ? null : parseEvent(v.event);
  if (event && event.icao !== data.icao)
    throw new Error("Güncelleme ICAO adresi uyuşmuyor.");
  return {
    ...structuredClone(v),
    type: "aircraft_update",
    data,
    event,
  } as AircraftUpdate;
}
export function parseSnapshot(v: unknown): Aircraft[] {
  if (!Array.isArray(v)) throw new Error("Snapshot bir uçak dizisi olmalı.");
  return v.map(parseAircraft);
}
export function parseJsonl(text: string): AircraftUpdate[] {
  const lines = text.split(/\r?\n/).filter((line) => line.trim());
  if (!lines.length) throw new Error("Dosyada güncelleme bulunamadı.");
  if (lines.length > 100_000)
    throw new Error("Bir dosyada en fazla 100.000 mesaj açılabilir.");
  return lines.map((line, i) => {
    try {
      return parseUpdate(JSON.parse(line));
    } catch (e) {
      throw new Error(`Satır ${i + 1}: ${(e as Error).message}`);
    }
  });
}
