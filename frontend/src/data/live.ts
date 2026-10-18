import { parseSnapshot, parseUpdate } from "../lib/contracts";
import type { Aircraft, AircraftUpdate, ConnectionState } from "../types";
export interface LiveSink {
  snapshot: (aircraft: Aircraft[]) => void;
  update: (event: AircraftUpdate) => void;
  status: (state: ConnectionState, message: string) => void;
}
export function endpoint(base: string, path: string, ws = false): string {
  const url = new URL(base);
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password
  )
    throw new Error("Sunucu adresi http/https olmalı ve parola içermemeli.");
  url.pathname = `${url.pathname.replace(/\/$/, "")}/${path}`;
  url.search = "";
  url.hash = "";
  if (ws) url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  return url.toString();
}
/** Subscribe before snapshot; buffer stream events until the snapshot is installed. */
export function connectLive(base: string, sink: LiveSink): () => void {
  let stopped = false,
    attempt = 0,
    socket: WebSocket | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout>,
    watchdog: ReturnType<typeof setTimeout>;
  let abort: AbortController | null = null;
  const httpUrl = endpoint(base, "api/aircraft");
  const wsUrl = endpoint(base, "ws/aircraft", true);
  function connect() {
    if (stopped) return;
    sink.status(
      attempt ? "reconnecting" : "connecting",
      attempt ? "Yeniden bağlanılıyor…" : "Alıcıya bağlanılıyor…",
    );
    let ready = false,
      failed = false;
    const buffer: AircraftUpdate[] = [];
    const current = new WebSocket(wsUrl);
    socket = current;
    const fail = (message: string) => {
      if (failed || stopped) return;
      failed = true;
      clearTimeout(watchdog);
      abort?.abort();
      current.close();
      sink.status("offline", message);
      reconnectTimer = setTimeout(
        connect,
        Math.min(15_000, 1000 * 2 ** Math.min(attempt++, 4)),
      );
    };
    watchdog = setTimeout(
      () => fail("Sunucu yanıt vermedi. Bağlantı tekrar denenecek."),
      10_000,
    );
    current.onopen = async () => {
      abort = new AbortController();
      try {
        const response = await fetch(httpUrl, {
          signal: abort.signal,
          cache: "no-store",
        });
        if (!response.ok)
          throw new Error(`Snapshot alınamadı (HTTP ${response.status}).`);
        const snapshot = parseSnapshot(await response.json());
        if (stopped || failed) return;
        sink.snapshot(snapshot);
        for (const update of buffer) sink.update(update);
        buffer.length = 0;
        ready = true;
        attempt = 0;
        clearTimeout(watchdog);
        sink.status("connected", "Alıcı bağlantısı açık");
      } catch (e) {
        if (!stopped && !failed) fail((e as Error).message);
      }
    };
    current.onmessage = (e) => {
      if (stopped || failed) return;
      try {
        const update = parseUpdate(JSON.parse(e.data));
        if (ready) sink.update(update);
        else {
          if (buffer.length >= 5000)
            throw new Error("Snapshot beklenirken mesaj tamponu doldu.");
          buffer.push(update);
        }
      } catch {
        fail("Uyumsuz veya bozuk veri alındı. Bağlantı tekrar kurulacak.");
      }
    };
    current.onclose = () =>
      fail("Alıcı bağlantısı kesildi. Son bilinen konumlar gösteriliyor.");
    current.onerror = () =>
      fail(
        "Alıcıya erişilemiyor. Sunucu adresini ve CORS ayarını kontrol edin.",
      );
  }
  connect();
  return () => {
    stopped = true;
    clearTimeout(reconnectTimer);
    clearTimeout(watchdog);
    abort?.abort();
    socket?.close();
  };
}
