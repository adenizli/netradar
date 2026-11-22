import { useEffect, useState } from "react";
import type {
  Aircraft,
  AircraftUpdate,
  ConnectionState,
  Contact,
} from "../types";
import { connectLive } from "../data/live";
import { mergeContact, refreshContact } from "../lib/freshness";
export { mergeContact } from "../lib/freshness";
export function useTraffic(backend: string) {
  const [contacts, setContacts] = useState<Contact[]>([]);
  const [connection, setConnection] = useState<{
    state: ConnectionState;
    message: string;
  }>({ state: "connecting", message: "Başlatılıyor…" });
  const [rate, setRate] = useState(0),
    [session, setSession] = useState(0);
  useEffect(() => {
    let stopped = false,
      dirty = true,
      count = 0,
      connected = false;
    const store = new Map<string, Contact>();
    setContacts([]);
    setRate(0);
    setSession((s) => s + 1);
    const status = (state: ConnectionState, message: string) => {
      if (!stopped) {
        connected = state === "connected";
        setConnection({ state, message });
      }
    };
    const update = (u: AircraftUpdate) => {
      if (store.size >= 10_000 && !store.has(u.data.icao)) return;
      store.set(
        u.data.icao,
        mergeContact(store.get(u.data.icao), u, Date.now()),
      );
      dirty = true;
      count++;
    };
    const snapshot = (aircraft: Aircraft[]) => {
      store.clear();
      aircraft
        .slice(0, 10_000)
        .forEach((a) =>
          update({ type: "aircraft_update", data: a, event: null }),
        );
      dirty = true;
      setContacts([...store.values()]);
      setSession((s) => s + 1);
    };
    let cleanup = () => {};
    try {
      cleanup = connectLive(backend, { snapshot, update, status });
    } catch (e) {
      status("offline", (e as Error).message);
    }
    const flush = setInterval(() => {
      for (const [id, contact] of store) {
        const refreshed = refreshContact(contact, performance.now());
        if (refreshed !== contact) {
          store.set(id, refreshed);
          dirty = true;
        }
      }
      if (connected)
        for (const [id, contact] of store)
          if (Date.now() - contact.receivedAt > 60_000) {
            store.delete(id);
            dirty = true;
          }
      if (dirty) {
        setContacts([...store.values()]);
        dirty = false;
      }
    }, 200);
    const meter = setInterval(() => {
      setRate(count);
      count = 0;
    }, 1000);
    return () => {
      stopped = true;
      cleanup();
      clearInterval(flush);
      clearInterval(meter);
    };
  }, [backend]);
  return { contacts, connection, rate, session };
}
