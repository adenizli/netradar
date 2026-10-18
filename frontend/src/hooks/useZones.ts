import { useEffect, useRef, useState } from "react";
import type { Alert, Aircraft, Coordinate, Zone } from "../types";
import { GeofenceMonitor } from "../lib/geofence";
export const ZONE_COLORS = ["#a5efb7", "#e9c882", "#9bcddc", "#c1a6e7"];
function initialZones(): Zone[] {
  try {
    const raw: unknown = JSON.parse(
      localStorage.getItem("netradar.zones.v1") ?? "null",
    );
    if (
      Array.isArray(raw) &&
      raw.length <= 12 &&
      raw.every(
        (z) =>
          z &&
          typeof z.id === "string" &&
          typeof z.name === "string" &&
          z.name.length <= 40 &&
          typeof z.enabled === "boolean" &&
          typeof z.radiusKm === "number" &&
          z.radiusKm >= 1 &&
          z.radiusKm <= 250 &&
          ZONE_COLORS.includes(z.color),
      )
    )
      return raw;
  } catch {
    /* A disabled/full browser storage must not prevent startup. */
  }
  return [
    {
      id: "nearby",
      name: "Yakın hava sahası",
      radiusKm: 20,
      enabled: true,
      color: ZONE_COLORS[0],
    },
  ];
}
export function useZones(
  aircraft: Aircraft[],
  center: Coordinate,
  session: number,
  active: boolean,
) {
  const [zones, setZones] = useState<Zone[]>(initialZones);
  const [alerts, setAlerts] = useState<Alert[]>([]),
    [toasts, setToasts] = useState<Alert[]>([]);
  const monitor = useRef(new GeofenceMonitor());
  useEffect(() => {
    try {
      localStorage.setItem("netradar.zones.v1", JSON.stringify(zones));
    } catch {
      /* Session remains usable. */
    }
  }, [zones]);
  // A changed fence is a new baseline, not a flight transition.
  const signature = zones
    .map((z) => `${z.id}:${z.radiusKm}:${z.enabled}`)
    .join("|");
  useEffect(() => {
    monitor.current.reset();
  }, [session, center[0], center[1], signature, active]);
  useEffect(() => {
    if (!active) return;
    const next = monitor.current.update(aircraft, zones, center);
    if (next.length) {
      setAlerts((old) => [...next.reverse(), ...old].slice(0, 80));
      setToasts((old) => [...next, ...old].slice(0, 3));
    }
  }, [aircraft, zones, center, active]);
  useEffect(() => {
    const timer = setInterval(
      () => setToasts((old) => old.filter((a) => Date.now() - a.time < 7000)),
      1000,
    );
    return () => clearInterval(timer);
  }, []);
  useEffect(() => {
    setAlerts([]);
    setToasts([]);
  }, [session]);
  return {
    zones,
    setZones,
    alerts,
    toasts,
    dismiss: (id: string) => setToasts((old) => old.filter((a) => a.id !== id)),
  };
}
