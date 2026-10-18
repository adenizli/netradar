import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { connectLive, endpoint } from "../src/data/live";
import type { AircraftUpdate } from "../src/types";
import { parseAircraft } from "../src/lib/contracts";
class FakeSocket {
  static instances: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(public url: string) {
    FakeSocket.instances.push(this);
  }
  close() {
    this.closed = true;
  }
  message(value: unknown) {
    this.onmessage?.({ data: JSON.stringify(value) });
  }
}
const data = (name: string) =>
  parseAircraft({ icao: "ABC123", callsign: name });
const update = (name: string): AircraftUpdate => ({
  type: "aircraft_update",
  data: data(name),
  event: null,
});
const sink = () => ({ snapshot: vi.fn(), update: vi.fn(), status: vi.fn() });
beforeEach(() => {
  vi.useFakeTimers();
  FakeSocket.instances = [];
  vi.stubGlobal("WebSocket", FakeSocket);
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});
describe("live transport", () => {
  it("buffers messages until snapshot and replays them afterward", async () => {
    let resolve!: (r: unknown) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise((r) => {
            resolve = r;
          }),
      ),
    );
    const s = sink(),
      stop = connectLive("http://localhost:8000", s),
      ws = FakeSocket.instances[0];
    ws.onopen?.();
    ws.message(update("NEW"));
    expect(s.update).not.toHaveBeenCalled();
    resolve({ ok: true, json: async () => [data("OLD")] });
    await vi.advanceTimersByTimeAsync(0);
    expect(s.snapshot).toHaveBeenCalledWith([data("OLD")]);
    expect(s.update).toHaveBeenCalledWith(update("NEW"));
    expect(s.snapshot.mock.invocationCallOrder[0]).toBeLessThan(
      s.update.mock.invocationCallOrder[0],
    );
    expect(s.status).toHaveBeenLastCalledWith("connected", expect.any(String));
    stop();
  });
  it("fetches a fresh snapshot after reconnect", async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue({ ok: true, json: async () => [] });
    vi.stubGlobal("fetch", fetcher);
    const s = sink(),
      stop = connectLive("http://localhost:8000", s);
    FakeSocket.instances[0].onopen?.();
    await vi.advanceTimersByTimeAsync(0);
    FakeSocket.instances[0].onclose?.();
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeSocket.instances).toHaveLength(2);
    FakeSocket.instances[1].onopen?.();
    await vi.advanceTimersByTimeAsync(0);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(s.snapshot).toHaveBeenCalledTimes(2);
    stop();
  });
  it("cleanup cancels retry and suppresses late snapshot replies", async () => {
    let resolve!: (r: unknown) => void;
    vi.stubGlobal(
      "fetch",
      vi.fn(
        () =>
          new Promise((r) => {
            resolve = r;
          }),
      ),
    );
    const s = sink(),
      stop = connectLive("http://localhost:8000", s);
    FakeSocket.instances[0].onopen?.();
    stop();
    resolve({ ok: true, json: async () => [data("LATE")] });
    await vi.advanceTimersByTimeAsync(30_000);
    expect(s.snapshot).not.toHaveBeenCalled();
    expect(FakeSocket.instances).toHaveLength(1);
    expect(FakeSocket.instances[0].closed).toBe(true);
  });
  it("marks bad data offline without substituting demo aircraft", () => {
    const s = sink(),
      stop = connectLive("http://localhost:8000", s);
    FakeSocket.instances[0].message({
      schema_version: "9.0",
      type: "aircraft_update",
      data: {},
    });
    expect(s.status).toHaveBeenLastCalledWith("offline", expect.any(String));
    expect(s.update).not.toHaveBeenCalled();
    stop();
  });
  it("times out a server that never opens", async () => {
    const s = sink(),
      stop = connectLive("http://localhost:8000", s);
    await vi.advanceTimersByTimeAsync(10_000);
    expect(s.status).toHaveBeenLastCalledWith("offline", expect.any(String));
    stop();
  });
  it("rejects invalid HTTP snapshots", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue({
        ok: true,
        json: async () => ({ wrong: "envelope" }),
      }),
    );
    const s = sink(),
      stop = connectLive("http://localhost:8000", s);
    FakeSocket.instances[0].onopen?.();
    await vi.advanceTimersByTimeAsync(0);
    expect(s.status).toHaveBeenLastCalledWith("offline", expect.any(String));
    expect(s.snapshot).not.toHaveBeenCalled();
    stop();
  });
});
describe("endpoint URLs", () => {
  it("supports Pi addresses and path prefixes", () => {
    expect(endpoint("http://raspberrypi.local:8000/", "api/aircraft")).toBe(
      "http://raspberrypi.local:8000/api/aircraft",
    );
    expect(endpoint("https://example.com/radar/", "ws/aircraft", true)).toBe(
      "wss://example.com/radar/ws/aircraft",
    );
  });
  it.each([
    "file:///etc/passwd",
    "javascript:alert(1)",
    "https://user:password@example.com",
  ])("rejects unsafe endpoint %s", (url) =>
    expect(() => endpoint(url, "api/aircraft")).toThrow(),
  );
});
