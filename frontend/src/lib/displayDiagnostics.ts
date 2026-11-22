import type { Contact } from "../types";
import { captureKey } from "./diagnostics";
import { hasPosition } from "./geo";

export function contactCapture(contact: Contact) {
  const metadata =
    contact.event?.metadata ??
    Object.values(contact.aircraft.latest_events)
      .filter((event) => event.observed_at_unix_ms !== null)
      .reduce<typeof contact.event>(
        (latest, event) =>
          !latest || event.observed_at_unix_ms! > latest.observed_at_unix_ms!
            ? event
            : latest,
        null,
      )?.metadata;
  return captureKey(metadata?.capture_id, metadata?.source);
}

export function displayCounts(contacts: Contact[], key: string) {
  const counts = { positioned: 0, fresh: 0, stale: 0, unknown: 0 };
  for (const { aircraft } of contacts) {
    const metadata = aircraft.position_event?.metadata;
    if (
      !hasPosition(aircraft) ||
      captureKey(metadata?.capture_id, metadata?.source) !== key
    )
      continue;
    counts.positioned++;
    counts[aircraft.position_status ?? "unknown"]++;
  }
  return counts;
}
