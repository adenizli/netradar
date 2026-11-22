export const receiverStates = {
  unknown: [
    "Alıcı durumu bilinmiyor",
    "Henüz bir üretici veya IQ ölçümü bildirilmedi.",
  ],
  starting: [
    "Alıcı başlatılıyor",
    "Üreticinin ilk sinyali veya başlatma işleminin bitmesi bekleniyor.",
  ],
  receiver_down: [
    "Alıcı devre dışı",
    "Üretici durdu, kesintiye uğradı veya başlatılamadı.",
  ],
  telemetry_stale: [
    "Telemetri gecikmiş",
    "Üreticiden en az 5 saniyedir güncelleme alınmadı. Son bilinen değerler gösteriliyor.",
  ],
  receiver_silent: [
    "IQ sinyali yok",
    "Üretici çalışıyor ancak yakın zamanda IQ örneği alınmadı.",
  ],
  no_valid_traffic: [
    "Geçerli çerçeve yok",
    "IQ örnekleri alınıyor ancak yakın zamanda CRC doğrulamasını geçen bir çerçeve yok.",
  ],
  receiving: [
    "Geçerli trafik alınıyor",
    "Yakın zamanda IQ ve CRC doğrulamasını geçen çerçeveler alındı.",
  ],
  completed: [
    "Yakalama tamamlandı",
    "Üretici yakalamayı tamamladı. Son yakalamanın değerleri gösteriliyor.",
  ],
} as const;

export type ReceiverState = keyof typeof receiverStates;
type Source = "rf" | "replay" | "synthetic";
type Fields = Record<string, unknown>;

const record = (value: unknown): Fields =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Fields)
    : {};

const number = (value: unknown): number | null =>
  typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : null;

export function captureKey(id: unknown, source: unknown): string | null {
  return typeof id === "string" &&
    id.length > 0 &&
    (source === "rf" || source === "replay" || source === "synthetic")
    ? JSON.stringify([source, id])
    : null;
}

export function activityAge(
  stage: Stage,
  elapsed: number | null,
  heartbeat: number | null,
) {
  return stage.lastActivity === null || elapsed === null || heartbeat === null
    ? null
    : Math.max(0, elapsed - stage.lastActivity) + heartbeat;
}

export const stageCounters = {
  acquisition: ["complex_samples", "queue_dropped_samples", "queue_drop_events"],
  dsp: ["preamble_candidates", "df17_prefixes"],
  decoder: [
    "crc_clean",
    "crc_corrected_one_bit",
    "crc_rejected",
    "cpr_successes",
    "cpr_failures",
  ],
  delivery: ["accepted", "rejected", "undelivered", "unconfirmed"],
  display: ["fresh", "stale", "expired"],
} as const;

// Added with the decoupled live delivery (#9). Older producers do not send them,
// so their absence must not mark the snapshot as partial.
const optionalCounters: ReadonlySet<string> = new Set([
  "queue_drop_events",
  "undelivered",
  "unconfirmed",
]);

type StageName = keyof typeof stageCounters;
type CounterName = (typeof stageCounters)[StageName][number];
type Stage = {
  status: "measured" | "unavailable" | "missing";
  counters: Partial<Record<CounterName, number | null>>;
  lastActivity: number | null;
};
type Resources = ReturnType<typeof resources>;
export type Capture = {
  key: string;
  id: string;
  source: Source;
  elapsed: number | null;
  stages: Record<StageName, Stage>;
  distinct: number | null;
  lowerBound: boolean;
};
export type Diagnostics = {
  receiver: ReceiverState | null;
  heartbeat: number | null;
  partial: boolean;
  capture: Capture | null;
  queue: {
    depth: number | null;
    capacity: number | null;
    peak: number | null;
    drop: number | null;
  };
  backend: {
    accepted: number | null;
    rejected: number | null;
    subscribers: number | null;
  };
  resources: { producer: Resources; backend: Resources };
};

function resources(value: unknown) {
  const data = record(value);
  return {
    cpu: number(data.cpu_seconds),
    rss: number(data.rss_bytes),
    peak: number(data.peak_rss_bytes),
    temperature:
      typeof data.temperature_celsius === "number" &&
      Number.isFinite(data.temperature_celsius)
        ? data.temperature_celsius
        : null,
    throttling: number(data.throttling_mask),
  };
}

export function parseDiagnostics(value: unknown): Diagnostics {
  const data = record(value);
  const state = data.receiver_state;
  const receiver =
    data.schema_version === "1.0" &&
    typeof state === "string" &&
    Object.hasOwn(receiverStates, state)
      ? (state as ReceiverState)
      : null;
  const input = record(data.capture);
  const key = captureKey(input.capture_id, input.source);
  let partial =
    receiver === null ||
    !Object.hasOwn(data, "capture") ||
    !Object.hasOwn(data, "heartbeat_age_seconds") ||
    !Object.hasOwn(data, "resources") ||
    !Object.hasOwn(data, "queue") ||
    !Object.hasOwn(data, "backend");
  const allStages = record(input.stages);
  const readStage = (name: StageName): Stage => {
    const stage = record(allStages[name]);
    if (stage.status === "unavailable")
      return { status: "unavailable", counters: {}, lastActivity: null };
    if (stage.status !== "measured") {
      partial = true;
      return { status: "missing", counters: {}, lastActivity: null };
    }
    const counters = record(stage.counters);
    const entries = stageCounters[name].map((metric) => {
      const count = number(counters[metric]);
      if (count === null && !optionalCounters.has(metric)) partial = true;
      return [metric, count];
    });
    return {
      status: "measured",
      counters: Object.fromEntries(entries),
      lastActivity: number(stage.last_activity_elapsed_seconds),
    };
  };
  const window = record(
    record(record(input.distinct_icao_windows).windows_seconds)["300"],
  );
  let capture: Capture | null = null;
  if (key && data.schema_version === "1.0") {
    capture = {
      key,
      id: input.capture_id as string,
      source: input.source as Source,
      elapsed: number(input.elapsed_seconds),
      stages: {
        acquisition: readStage("acquisition"),
        dsp: readStage("dsp"),
        decoder: readStage("decoder"),
        delivery: readStage("delivery"),
        display: readStage("display"),
      },
      distinct: number(window.count),
      lowerBound: window.lower_bound === true,
    };
    partial ||= capture.elapsed === null;
  } else if (data.capture !== null) {
    partial = true;
  }
  const queue = record(data.queue);
  const backend = record(data.backend);
  const usage = record(data.resources);
  return {
    receiver,
    partial,
    capture,
    heartbeat: number(data.heartbeat_age_seconds),
    queue: {
      depth: number(queue.depth),
      capacity: number(queue.capacity),
      peak: number(queue.high_watermark),
      drop: number(queue.drop_fraction),
    },
    backend: {
      accepted: number(backend.accepted),
      rejected: number(backend.rejected),
      subscribers: number(backend.subscriber_count),
    },
    resources: {
      producer: resources(usage.producer),
      backend: resources(usage.backend),
    },
  };
}

const samples = (capture: Capture) =>
  capture.stages.acquisition.counters.complex_samples ?? null;
export function rate(
  count: number | null | undefined,
  elapsed: number | null | undefined,
) {
  return count == null || elapsed == null || elapsed <= 0
    ? null
    : count / elapsed;
}

const valid = (capture: Capture) => {
  const { crc_clean, crc_corrected_one_bit } = capture.stages.decoder.counters;
  return crc_clean == null || crc_corrected_one_bit == null
    ? null
    : crc_clean + crc_corrected_one_bit;
};
export function decoderNote(capture: Capture): string | null {
  const total = valid(capture);
  const { crc_rejected, cpr_successes } = capture.stages.decoder.counters;
  if (total === 0 && (crc_rejected ?? 0) > 0)
    return "Çerçeveler alındı, ancak hiçbiri CRC doğrulamasını geçmedi.";
  if (total !== null && total > 0 && cpr_successes === 0)
    return "CRC doğrulaması başarılı. Çağrı kodu gibi mesajlar gelebilir. Henüz CPR ile konum çözülemedi.";
  return null;
}

export const TREND_SECONDS = 300;
export type Point = {
  elapsed: number;
  samples: number | null;
  valid: number | null;
};
export type Trend = { capture: Capture | null; points: Point[] };
export const emptyTrend: Trend = { capture: null, points: [] };

function interval(
  previous: number | null,
  next: number | null,
  elapsed: number,
) {
  return previous === null || next === null || next < previous
    ? null
    : (next - previous) / elapsed;
}

export function advanceTrend(history: Trend, data: Diagnostics): Trend {
  const next = data.capture;
  if (!next) return emptyTrend;
  const previous = history.capture;
  if (!previous || previous.key !== next.key)
    return { capture: next, points: [] };
  if (
    data.receiver === "telemetry_stale" ||
    data.receiver === "receiver_down" ||
    next.elapsed === null ||
    previous.elapsed === null ||
    next.elapsed === previous.elapsed
  )
    return history;
  if (next.elapsed < previous.elapsed) return { capture: next, points: [] };
  const elapsed = next.elapsed - previous.elapsed;
  const point = {
    elapsed: next.elapsed,
    samples: interval(samples(previous), samples(next), elapsed),
    valid: interval(valid(previous), valid(next), elapsed),
  };
  return {
    capture: next,
    points: [...history.points, point]
      .filter((entry) => entry.elapsed >= point.elapsed - TREND_SECONDS)
      .slice(-TREND_SECONDS),
  };
}

export function diagnosticsStatus(
  status: "loading" | "online" | "offline",
  data: Diagnostics | null,
) {
  if (status === "loading")
    return [
      "Tanı bekleniyor",
      "Sunucudan ilk tanı yanıtı bekleniyor.",
    ] as const;
  if (status === "offline")
    return [
      "Tanı API’si çevrimdışı",
      "Sunucu tanı isteğine yanıt veremedi. Varsa aşağıda son alınan değerler gösteriliyor.",
    ] as const;
  if (!data?.receiver)
    return [
      "Tanı verisi eksik",
      "Sunucu yanıt verdi ancak alıcının durumunu doğrulamak için gerekli veriler eksik veya uyumsuz.",
    ] as const;
  return receiverStates[data.receiver];
}
