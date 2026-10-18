import type { Aircraft, Coordinate } from "../types";
const R = 6371.0088;
const rad = (v: number) => (v * Math.PI) / 180;
const deg = (v: number) => (v * 180) / Math.PI;
export function hasPosition(a: Aircraft): boolean {
  return (
    typeof a.latitude === "number" &&
    Number.isFinite(a.latitude) &&
    typeof a.longitude === "number" &&
    Number.isFinite(a.longitude)
  );
}
export function hasFreshPosition(a: Aircraft): boolean {
  return hasPosition(a) && a.position_status === "fresh";
}
export function distanceKm(a: Coordinate, b: Coordinate): number {
  const h =
    Math.sin(rad(b[0] - a[0]) / 2) ** 2 +
    Math.cos(rad(a[0])) *
      Math.cos(rad(b[0])) *
      Math.sin(rad(b[1] - a[1]) / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(Math.min(1, Math.max(0, h))));
}
export function destination(
  start: Coordinate,
  bearing: number,
  distance: number,
): Coordinate {
  const d = distance / R,
    b = rad(bearing),
    lat = rad(start[0]),
    lon = rad(start[1]);
  const nextLat = Math.asin(
    Math.sin(lat) * Math.cos(d) + Math.cos(lat) * Math.sin(d) * Math.cos(b),
  );
  const nextLon =
    lon +
    Math.atan2(
      Math.sin(b) * Math.sin(d) * Math.cos(lat),
      Math.cos(d) - Math.sin(lat) * Math.sin(nextLat),
    );
  return [deg(nextLat), ((deg(nextLon) + 540) % 360) - 180];
}
export function interpolateCoordinate(
  a: Coordinate,
  b: Coordinate,
  t: number,
): Coordinate {
  const deltaLon = ((b[1] - a[1] + 540) % 360) - 180;
  return [a[0] + (b[0] - a[0]) * t, ((a[1] + deltaLon * t + 540) % 360) - 180];
}
export const numeric = (v: unknown, digits = 0) =>
  typeof v === "number" && Number.isFinite(v)
    ? v.toLocaleString("tr-TR", { maximumFractionDigits: digits })
    : "—";
export const bearingText = (v: unknown) =>
  typeof v === "number" ? `${Math.round(v).toString().padStart(3, "0")}°` : "—";
export const flightName = (a: Aircraft) => a.callsign?.trim() || a.icao;

export function bearingBetween(a: Coordinate, b: Coordinate): number {
  const d = rad(b[1] - a[1]);
  return (
    (deg(
      Math.atan2(
        Math.sin(d) * Math.cos(rad(b[0])),
        Math.cos(rad(a[0])) * Math.sin(rad(b[0])) -
          Math.sin(rad(a[0])) * Math.cos(rad(b[0])) * Math.cos(d),
      ),
    ) +
      360) %
    360
  );
}
