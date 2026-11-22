import { describe, expect, it, vi } from "vitest";
import {
  pollDiagnostics,
  pollForBackend,
  updatePoll,
} from "../src/hooks/useDiagnostics";

import {
  activityAge,
  advanceTrend,
  captureKey,
  decoderNote,
  diagnosticsStatus,
  emptyTrend,
  parseDiagnostics,
  rate,
} from "../src/lib/diagnostics";
import { displayCounts } from "../src/lib/displayDiagnostics";
import { mergeContact } from "../src/lib/freshness";
import type { Aircraft, DecoderEvent } from "../src/types";

const stage = (
  counters: object,
  last_activity_elapsed_seconds: number | null = null,
) => ({
  status: "measured",
  counters,
  last_activity_elapsed_seconds,
});

function report({
  receiver = "receiving",
  id = "run-1",
  source = "rf",
  elapsed = 10,
  samples = 1000,
  clean = 1,
  rejected = 0,
  heartbeat = 0.5,
  drops = 0,
} = {}) {
  return {
    schema_version: "1.0",
    receiver_state: receiver,
    heartbeat_age_seconds: heartbeat,
    capture: {
      capture_id: id,
      source,
      elapsed_seconds: elapsed,
      distinct_icao_windows: {
        windows_seconds: { "300": { count: clean, lower_bound: false } },
      },
      stages: {
        acquisition: stage({
          complex_samples: samples,
          queue_dropped_samples: drops,
        }),
        dsp: stage({
          preamble_candidates: clean + rejected,
          df17_prefixes: clean + rejected,
        }),
        decoder: stage(
          {
            crc_clean: clean,
            crc_corrected_one_bit: 0,
            crc_rejected: rejected,
            cpr_successes: 0,
            cpr_failures: 0,
          },
          clean ? elapsed - 2 : null,
        ),
        delivery: stage({ accepted: clean, rejected: 0 }),
        display: { status: "unavailable" },
      },
    },
    queue: {
      depth: 2,
      capacity: 10,
      high_watermark: 10,
      drop_fraction: drops / (samples || 1),
    },
    backend: { accepted: 123, rejected: 8, subscriber_count: 1 },
    resources: {
      producer: { cpu_seconds: 1.2, temperature_celsius: 29 },
      backend: {},
    },
  };
}

describe("operator diagnostics", () => {
  it("distinguishes a running producer with idle RF from a stopped receiver", () => {
    const idle = parseDiagnostics(
      report({ receiver: "receiver_silent", samples: 0, clean: 0 }),
    );
    expect(diagnosticsStatus("online", idle)[0]).toBe("IQ sinyali yok");
    expect(
      rate(idle.capture!.stages.acquisition.counters.complex_samples, 10),
    ).toBe(0);
    const stopped = parseDiagnostics(report({ receiver: "receiver_down" }));
    expect(diagnosticsStatus("online", stopped)[0]).toBe("Alıcı devre dışı");
    expect(stopped.backend.subscribers).toBe(1);
  });

  it("reads queue drops and keeps the server request totals separate", () => {
    const data = parseDiagnostics(report({ drops: 12 }));
    expect(data.queue).toEqual({
      depth: 2,
      capacity: 10,
      peak: 10,
      drop: 0.012,
    });
    expect(data.backend).toEqual({
      accepted: 123,
      rejected: 8,
      subscribers: 1,
    });
    expect(data.capture!.stages.delivery.counters.accepted).toBe(1);
    expect(data.resources.producer).toMatchObject({
      cpu: 1.2,
      rss: null,
      temperature: 29,
    });
  });

  it("shows the live delivery backlog and overflow episodes when the producer sends them", () => {
    const payload = report();
    const stages = payload.capture.stages;
    Object.assign(stages.acquisition.counters, { queue_drop_events: 3 });
    Object.assign(stages.delivery.counters, { undelivered: 4, unconfirmed: 1 });
    const data = parseDiagnostics(payload);
    expect(data.partial).toBe(false);
    expect(data.capture!.stages.acquisition.counters.queue_drop_events).toBe(3);
    expect(data.capture!.stages.delivery.counters).toMatchObject({
      undelivered: 4,
      unconfirmed: 1,
    });
  });

  it("does not mark an older producer without backlog counters as partial", () => {
    const data = parseDiagnostics(report());
    expect(data.partial).toBe(false);
    expect(data.capture!.stages.delivery.counters.undelivered).toBeNull();
    expect(data.capture!.stages.acquisition.counters.queue_drop_events).toBeNull();
  });

  it("separates CRC rejection from callsign-only traffic with no CPR fix", () => {
    const rejected = parseDiagnostics(
      report({ receiver: "no_valid_traffic", clean: 0, rejected: 9 }),
    );
    expect(decoderNote(rejected.capture!)).toMatch(/hiçbiri CRC/);
    expect(diagnosticsStatus("online", rejected)[0]).toBe(
      "Geçerli çerçeve yok",
    );
    const callsignOnly = parseDiagnostics(report());
    expect(decoderNote(callsignOnly.capture!)).toMatch(/Henüz CPR/);
    expect(
      activityAge(
        callsignOnly.capture!.stages.decoder,
        10,
        callsignOnly.heartbeat,
      ),
    ).toBe(2.5);
  });

  it("identifies offline, missing, partial, and stale data independently", () => {
    const partial = parseDiagnostics({
      schema_version: "1.0",
      receiver_state: "starting",
    });
    expect(partial.partial).toBe(true);
    expect(partial.capture).toBeNull();
    expect(diagnosticsStatus("online", partial)[0]).toBe("Alıcı başlatılıyor");
    expect(diagnosticsStatus("online", parseDiagnostics({}))[0]).toBe(
      "Tanı verisi eksik",
    );
    expect(diagnosticsStatus("offline", partial)[0]).toBe(
      "Tanı API’si çevrimdışı",
    );
    const stale = parseDiagnostics(
      report({ receiver: "telemetry_stale", heartbeat: 6 }),
    );
    expect(diagnosticsStatus("online", stale)[0]).toBe("Telemetri gecikmiş");
    expect(stale.heartbeat).toBe(6);
    const empty = parseDiagnostics({
      ...report({ receiver: "unknown" }),
      capture: null,
      heartbeat_age_seconds: null,
    });
    expect(empty.partial).toBe(false);
    expect(diagnosticsStatus("online", empty)[0]).toBe(
      "Alıcı durumu bilinmiyor",
    );
  });

  it("calculates interval rates, ignores stale snapshots, and resets on new session or source", () => {
    const first = advanceTrend(emptyTrend, parseDiagnostics(report()));
    const next = advanceTrend(
      first,
      parseDiagnostics(report({ elapsed: 11, samples: 2500, clean: 3 })),
    );
    expect(next.points).toEqual([{ elapsed: 11, samples: 1500, valid: 2 }]);
    expect(
      advanceTrend(
        next,
        parseDiagnostics(report({ receiver: "telemetry_stale" })),
      ),
    ).toBe(next);
    expect(
      advanceTrend(next, parseDiagnostics(report({ id: "run-2" }))).points,
    ).toEqual([]);
    expect(
      advanceTrend(next, parseDiagnostics(report({ source: "replay" }))).points,
    ).toEqual([]);
    let history = next;
    for (let elapsed = 12; elapsed <= 910; elapsed++)
      history = advanceTrend(
        history,
        parseDiagnostics(
          report({ elapsed, samples: elapsed * 1000, clean: elapsed }),
        ),
      );
    expect(history.points).toHaveLength(300);
    expect(history.points[0].elapsed).toBe(611);
  });

  it("counts positions using fix provenance after another source reports a callsign", () => {
    const position: DecoderEvent = {
      icao: "abcdef",
      event_type: "airborne_position",
      fields: { latitude: 1, longitude: 2 },
      raw_frame: null,
      observed_at_unix_ms: 100_000,
      metadata: {
        source: "rf",
        capture_id: "run-1",
        sample_rate_hz: 1,
        sample_index: 0,
      },
    };
    const callsign: DecoderEvent = {
      ...position,
      event_type: "identification",
      metadata: { source: "replay", capture_id: "run-2" },
    };
    const aircraft = {
      icao: "abcdef",
      latitude: 1,
      longitude: 2,
      position_event: position,
      position_age_ms: 30_000,
      latest_events: {},
    } as Aircraft;
    const contact = mergeContact(
      undefined,
      { type: "aircraft_update", data: aircraft, event: callsign },
      130_000,
      0,
    );
    expect(displayCounts([contact], captureKey("run-1", "rf")!)).toEqual({
      positioned: 1,
      stale: 1,
      fresh: 0,
      unknown: 0,
    });
    expect(
      displayCounts([contact], captureKey("run-2", "replay")!).positioned,
    ).toBe(0);
  });
});

describe("diagnostics polling", () => {
  it("clears metrics and trend before the new backend responds, including on initial failure", () => {
    const oldBackend = "http://old.test";
    const newBackend = "http://new.test";
    const capture = parseDiagnostics(report());
    const oldPoll = updatePoll(
      {
        backend: oldBackend,
        status: "online",
        data: capture,
        trend: advanceTrend(emptyTrend, capture),
      },
      oldBackend,
      "online",
      parseDiagnostics(report({ elapsed: 11, samples: 2000 })),
    );
    expect(oldPoll.trend.points).toHaveLength(1);

    expect(pollForBackend(oldPoll, newBackend)).toEqual({
      backend: newBackend,
      status: "loading",
      data: null,
      trend: emptyTrend,
    });
    expect(updatePoll(oldPoll, newBackend, "offline", null)).toEqual({
      backend: newBackend,
      status: "offline",
      data: null,
      trend: emptyTrend,
    });
    const newPoll = updatePoll(
      oldPoll,
      newBackend,
      "online",
      parseDiagnostics(report({ elapsed: 12, samples: 3000 })),
    );
    expect(newPoll.trend.capture?.elapsed).toBe(12);
    expect(newPoll.trend.points).toEqual([]);
  });

  it("aborts the old backend and ignores a late response during a backend switch", async () => {
    const oldUrl = "http://old.test";
    const newUrl = "http://new.test";
    let respondToOld!: (response: Response) => void;
    let oldSignal: AbortSignal | null | undefined;
    const fetchStub = vi.fn((url: string, options: RequestInit) => {
      if (url.startsWith(oldUrl)) {
        oldSignal = options.signal;
        return new Promise<Response>((resolve) => {
          respondToOld = resolve;
        });
      }
      return Promise.resolve(
        Response.json(report({ id: "new", source: "synthetic" })),
      );
    });
    vi.stubGlobal("fetch", fetchStub);
    const publish = vi.fn();
    const stopOld = pollDiagnostics(oldUrl, publish);
    stopOld();
    const stopNew = pollDiagnostics(newUrl, publish);

    try {
      expect(oldSignal?.aborted).toBe(true);
      respondToOld(Response.json(report({ id: "old" })));
      await vi.waitFor(() => expect(publish).toHaveBeenCalledTimes(1));
      expect(publish).toHaveBeenCalledWith(
        "online",
        expect.objectContaining({
          capture: expect.objectContaining({ id: "new", source: "synthetic" }),
        }),
      );
    } finally {
      stopNew();
      vi.unstubAllGlobals();
    }
  });

  it("reports an unsuccessful HTTP request as offline", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(null, { status: 503 })),
    );
    const publish = vi.fn();
    const stop = pollDiagnostics("http://backend.test", publish);
    try {
      await vi.waitFor(() =>
        expect(publish).toHaveBeenCalledWith("offline", null),
      );
    } finally {
      stop();
      vi.unstubAllGlobals();
    }
  });
});
