import { useEffect, useState } from "react";
import { endpoint } from "../data/live";
import { advanceTrend, emptyTrend, parseDiagnostics } from "../lib/diagnostics";
import type { Diagnostics, Trend } from "../lib/diagnostics";

type Poll = {
  status: "loading" | "online" | "offline";
  data: Diagnostics | null;
  trend: Trend;
};

type BackendPoll = Poll & { backend: string };

const initial: Poll = { status: "loading", data: null, trend: emptyTrend };

export function pollDiagnostics(
  backend: string,
  publish: (status: "online" | "offline", data: Diagnostics | null) => void,
) {
  let stopped = false;
  let scheduled: ReturnType<typeof setTimeout>;
  let request: AbortController | undefined;
  let timeout: ReturnType<typeof setTimeout>;

  async function refresh() {
    const start = performance.now();
    const controller = new AbortController();
    request = controller;
    timeout = setTimeout(() => controller.abort(), 3500);
    try {
      const response = await fetch(endpoint(backend, "api/diagnostics"), {
        signal: controller.signal,
        cache: "no-store",
      });
      if (!response.ok) throw new Error("Diagnostics request failed");
      const payload: unknown = await response.json();
      if (stopped) return;
      const data = parseDiagnostics(payload);
      publish("online", data);
    } catch {
      if (!stopped) publish("offline", null);
    } finally {
      clearTimeout(timeout);
      if (!stopped)
        scheduled = setTimeout(
          refresh,
          Math.max(0, 1000 - (performance.now() - start)),
        );
    }
  }

  void refresh();
  return () => {
    stopped = true;
    clearTimeout(scheduled);
    clearTimeout(timeout);
    request?.abort();
  };
}

export function pollForBackend(
  poll: BackendPoll,
  backend: string,
): BackendPoll {
  return poll.backend === backend ? poll : { ...initial, backend };
}

export function updatePoll(
  poll: BackendPoll,
  backend: string,
  status: "online" | "offline",
  data: Diagnostics | null,
): BackendPoll {
  const previous = pollForBackend(poll, backend);
  return data
    ? { backend, status, data, trend: advanceTrend(previous.trend, data) }
    : { ...previous, status };
}

export function useDiagnostics(backend: string) {
  const [poll, setPoll] = useState<BackendPoll>(() => ({
    ...initial,
    backend,
  }));
  useEffect(() => {
    setPoll((previous) => pollForBackend(previous, backend));
    return pollDiagnostics(backend, (status, data) => {
      setPoll((previous) => updatePoll(previous, backend, status, data));
    });
  }, [backend]);
  return pollForBackend(poll, backend);
}
