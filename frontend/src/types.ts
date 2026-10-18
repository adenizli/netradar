export type Json =
  null | boolean | number | string | Json[] | { [key: string]: Json };
export type JsonObject = { [key: string]: Json };
export interface DecoderEvent {
  schema_version?: string;
  icao: string;
  event_type: string;
  fields: JsonObject;
  raw_frame: string | null;
  observed_at_unix_ms: number | null;
  metadata?: JsonObject;
  [key: string]: unknown;
}
export interface Aircraft {
  icao: string;
  callsign: string | null;
  latitude: number | null;
  longitude: number | null;
  altitude_ft: number | null;
  gnss_height_ft: number | null;
  speed_knots: number | null;
  track_deg: number | null;
  airspeed_knots: number | null;
  airspeed_type: string | null;
  heading_deg: number | null;
  heading_reference: string | null;
  vertical_rate_fpm: number | null;
  vertical_rate_source: string | null;
  last_seen: string | null;
  last_seen_unix_ms?: number | null;
  position_source?: string | null;
  position_revision?: number;
  position_event?: DecoderEvent | null;
  position_observed_at_unix_ms?: number | null;
  position_received_at_unix_ms?: number | null;
  position_age_ms?: number | null;
  position_status?: "fresh" | "stale" | "unknown";
  latest_events: Record<string, DecoderEvent>;
  [key: string]: unknown;
}
export interface AircraftUpdate {
  schema_version?: string;
  type: "aircraft_update";
  data: Aircraft;
  event: DecoderEvent | null;
}
export interface Contact {
  aircraft: Aircraft;
  event: DecoderEvent | null;
  receivedAt: number;
  trail: Coordinate[];
  positionKey: string | null;
  positionAgeAtReceiptMs: number | null;
  positionClockAt: number;
}
export type Coordinate = [number, number];
export interface Zone {
  id: string;
  name: string;
  radiusKm: number;
  enabled: boolean;
  color: string;
}
export interface Alert {
  id: string;
  kind: "enter" | "exit";
  icao: string;
  callsign: string;
  zoneId: string;
  zoneName: string;
  time: number;
}
export type SourceMode = "demo" | "live";
export type ConnectionState =
  "connecting" | "connected" | "reconnecting" | "offline" | "paused";
export interface Settings {
  receiver: Coordinate;
  stationName: string;
  backendUrl: string;
}
