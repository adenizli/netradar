import { useRef, useState } from "react";
import { Activity, X } from "lucide-react";
import { useDiagnostics } from "../hooks/useDiagnostics";
import {
  activityAge,
  decoderNote,
  diagnosticsStatus,
  rate,
  TREND_SECONDS,
} from "../lib/diagnostics";
import type { Capture, Diagnostics as Report, Point } from "../lib/diagnostics";
import { displayCounts } from "../lib/displayDiagnostics";
import type { ConnectionState, Contact } from "../types";

type Value = [label: string, value: string];
const decimal = new Intl.NumberFormat("tr-TR", { maximumFractionDigits: 1 });
const precise = new Intl.NumberFormat("tr-TR", { maximumFractionDigits: 2 });
const metric = (value: number | null | undefined, unit = "") =>
  value == null ? "—" : decimal.format(value) + unit;
const memory = (bytes: number | null) =>
  metric(bytes === null ? null : bytes / 1024 ** 2, " MiB");
const stageStatus = {
  measured: "Ölçülüyor",
  unavailable: "Ölçüm yok",
  missing: "Veri eksik",
};

function Values({ values }: { values: Value[] }) {
  return (
    <dl className="diag-values">
      {values.map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd>
            {value === "—" ? (
              <abbr title="Veri yok" aria-label="Veri yok">
                —
              </abbr>
            ) : (
              value
            )}
          </dd>
        </div>
      ))}
    </dl>
  );
}

function Card({
  title,
  status,
  values,
  note,
}: {
  title: string;
  status?: string;
  values: Value[];
  note?: string | null;
}) {
  return (
    <section className="diag-card">
      <h3>{title}</h3>
      {status && <div className="diag-stage-status">{status}</div>}
      <Values values={values} />
      {note && <p>{note}</p>}
    </section>
  );
}

function Funnel({ capture, report }: { capture: Capture; report: Report }) {
  const { acquisition, dsp, decoder, delivery, display } = capture.stages;
  const { queue } = report;
  const avg = (counter: number | null | undefined) =>
    metric(rate(counter, capture.elapsed), " /sn");
  return (
    <section className="diag-section">
      <h3>Yakalama aşamaları</h3>
      <p>Hızlar yakalama başlangıcından bu yana ortalamadır.</p>
      <div className="diag-grid">
        <Card
          title="IQ ve kuyruk"
          status={stageStatus[acquisition.status]}
          values={[
            ["Karmaşık IQ örneği", avg(acquisition.counters.complex_samples)],
            [
              "Kuyruk kaybı",
              queue.drop === null
                ? "—"
                : "%" + precise.format(queue.drop * 100),
            ],
            [
              "Kuyruk derinliği / kapasite",
              queue.depth === null && queue.capacity === null
                ? "—"
                : `${metric(queue.depth)} / ${metric(queue.capacity)}`,
            ],
            ["En yüksek kuyruk derinliği", metric(queue.peak)],
          ]}
        />
        <Card
          title="Sinyal işleme"
          status={stageStatus[dsp.status]}
          values={[
            ["Preamble adayı", avg(dsp.counters.preamble_candidates)],
            ["DF17 öneki", avg(dsp.counters.df17_prefixes)],
          ]}
        />
        <Card
          title="CRC doğrulama"
          status={stageStatus[decoder.status]}
          values={[
            ["Temiz", metric(decoder.counters.crc_clean)],
            [
              "Tek bit düzeltildi",
              metric(decoder.counters.crc_corrected_one_bit),
            ],
            ["Reddedildi", metric(decoder.counters.crc_rejected)],
          ]}
        />
        <Card
          title="Konum çözümü"
          status={stageStatus[decoder.status]}
          values={[
            [
              "Ayrı ICAO · 5 dk",
              capture.distinct === null
                ? "—"
                : (capture.lowerBound ? "≥ " : "") + metric(capture.distinct),
            ],
            ["CPR çözüldü", metric(decoder.counters.cpr_successes)],
            ["CPR çözülemedi", metric(decoder.counters.cpr_failures)],
          ]}
          note={decoderNote(capture)}
        />
        <Card
          title="Üretici teslimatı"
          status={stageStatus[delivery.status]}
          values={[
            ["Kabul edilen gönderim", metric(delivery.counters.accepted)],
            ["Başarısız gönderim", metric(delivery.counters.rejected)],
          ]}
        />
        <Card
          title="Üretici ekran geçişleri"
          status={stageStatus[display.status]}
          values={[
            ["Güncel duruma geçiş", metric(display.counters.fresh)],
            ["Eski duruma geçiş", metric(display.counters.stale)],
            ["Süresi dolan geçiş", metric(display.counters.expired)],
          ]}
        />
      </div>
    </section>
  );
}

function Chart({
  points,
  field,
  label,
}: {
  points: Point[];
  field: "samples" | "valid";
  label: string;
}) {
  const end = points.at(-1)?.elapsed ?? TREND_SECONDS;
  const left = end - TREND_SECONDS;
  const peak = Math.max(1, ...points.map((point) => point[field] ?? 0));
  const latest = points.at(-1)?.[field] ?? null;
  const width = 450;
  const height = 62;
  let path = "";
  let previous = false;
  for (const point of points) {
    const value = point[field];
    if (value !== null) {
      const x = ((point.elapsed - left) / TREND_SECONDS) * width;
      const y = height - 4 - (value / peak) * (height - 8);
      path += `${previous ? "L" : "M"} ${x} ${y} `;
    }
    previous = value !== null;
  }
  return (
    <figure className="diag-chart">
      <figcaption>
        <span>{label}</span>
        <strong>{metric(latest, " /sn")}</strong>
      </figcaption>
      {points.filter((point) => point[field] !== null).length < 2 ? (
        <p>Grafik için ardışık ölçümler bekleniyor.</p>
      ) : (
        <svg
          viewBox={`0 0 ${width} ${height}`}
          preserveAspectRatio="none"
          role="img"
          aria-label={`${label}. Son ölçüm ${metric(latest, " /sn")}, ölçek 0–${metric(peak, " /sn")}`}
        >
          <path
            className="diag-gridline"
            d={`M 0 4 H ${width} M 0 ${height / 2} H ${width} M 0 ${height - 4} H ${width}`}
          />
          <path className="diag-series" d={path} />
        </svg>
      )}
      <div className="diag-chart-axis">
        <span>−5 dk</span>
        <span>Son örnek</span>
      </div>
    </figure>
  );
}

function ResourceCard({
  title,
  resource,
}: {
  title: string;
  resource: Report["resources"]["backend"];
}) {
  return (
    <Card
      title={title}
      values={[
        ["Toplam CPU süresi", metric(resource.cpu, " sn")],
        ["Bellek (RSS)", memory(resource.rss)],
        ["En yüksek RSS", memory(resource.peak)],
        ["Cihaz sıcaklığı", metric(resource.temperature, " °C")],
        [
          "Pi kısıtlama maskesi",
          resource.throttling === null
            ? "—"
            : `0x${resource.throttling.toString(16)}`,
        ],
      ]}
    />
  );
}

const sources = { rf: "RF", replay: "Replay", synthetic: "Synthetic" };
const streamStatuses: Record<ConnectionState, string> = {
  connected: "Açık",
  connecting: "Bağlanıyor",
  reconnecting: "Yeniden bağlanıyor",
  offline: "Kapalı",
};

export function Diagnostics({
  backend,
  stream,
  contacts,
  expirations,
}: {
  backend: string;
  stream: ConnectionState;
  contacts: Contact[];
  expirations: ReadonlyMap<string, number>;
}) {
  const { status, data, trend } = useDiagnostics(backend);
  const [open, setOpen] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const [label, explanation] = diagnosticsStatus(status, data);
  const capture = data?.capture;
  const healthy = status === "online" && data?.receiver === "receiving";
  const counts = open && capture ? displayCounts(contacts, capture.key) : null;

  return (
    <>
      <div
        className={`connection-pill ${healthy ? "" : "disconnected"}`}
        role="status"
      >
        <span className="live-dot" aria-hidden="true" />
        {label}
        {capture && (
          <span className="diag-source">{sources[capture.source]}</span>
        )}
      </div>
      <button
        className="diag-trigger outline-button"
        aria-haspopup="dialog"
        onClick={() => {
          dialog.current?.showModal();
          setOpen(true);
        }}
      >
        <Activity size={15} aria-hidden="true" /> Tanı
      </button>
      <dialog
        className="diag-dialog"
        ref={dialog}
        aria-labelledby="diag-title"
        onClose={() => setOpen(false)}
        onKeyDown={(event) => {
          if (event.key === "Escape") event.stopPropagation();
        }}
      >
        <div className="diag-titlebar">
          <h2 id="diag-title">Alıcı tanısı</h2>
          <button
            className="icon-button"
            aria-label="Tanıyı kapat"
            onClick={() => dialog.current?.close()}
          >
            <X size={20} />
          </button>
        </div>
        {open && (
          <div className="diag-body">
            <section className="diag-overview">
              <div role="status">
                <h3>{label}</h3>
                <p>{explanation}</p>
                {data?.partial && data.receiver && (
                  <p>Tanı yanıtı eksik. Bazı ölçümler gösterilemiyor.</p>
                )}
                {capture ? (
                  <p className="diag-capture">
                    <strong>{sources[capture.source]}</strong>
                    <span>{capture.id}</span>
                  </p>
                ) : (
                  status === "online" && <p>Yakalama kaydı yok.</p>
                )}
              </div>
              <Values
                values={[
                  ["Uçak veri akışı", streamStatuses[stream]],
                  [
                    "Son telemetri sinyali",
                    metric(data?.heartbeat, " sn önce"),
                  ],
                  [
                    "Son geçerli CRC",
                    metric(
                      capture
                        ? activityAge(
                            capture.stages.decoder,
                            capture.elapsed,
                            data?.heartbeat ?? null,
                          )
                        : null,
                      " sn önce",
                    ),
                  ],
                ]}
              />
            </section>
            {data && capture && <Funnel capture={capture} report={data} />}
            {capture && (
              <section className="diag-section">
                <h3>Son 5 dakika</h3>
                <div className="diag-charts">
                  <Chart
                    points={trend.points}
                    field="samples"
                    label="IQ örnek hızı"
                  />
                  <Chart
                    points={trend.points}
                    field="valid"
                    label="CRC geçerli çerçeve hızı"
                  />
                </div>
              </section>
            )}
            {data && (
              <section className="diag-section">
                <h3>Tarayıcı ve sunucu</h3>
                <div className="diag-grid diag-infrastructure">
                  {capture && counts && (
                    <Card
                      title="Bu yakalamanın ekranı"
                      values={[
                        ["Konumu olan uçak", metric(counts.positioned)],
                        ["Güncel konum", metric(counts.fresh)],
                        ["Eski konum", metric(counts.stale)],
                        ["Konum yaşı bilinmiyor", metric(counts.unknown)],
                        [
                          "Süresi dolan kayıt · oturum",
                          metric(expirations.get(capture.key) ?? 0),
                        ],
                      ]}
                    />
                  )}
                  <Card
                    title="Sunucu işlem ömrü"
                    values={[
                      ["Kabul edilen istek", metric(data.backend.accepted)],
                      ["Reddedilen istek", metric(data.backend.rejected)],
                      [
                        "Bağlı aboneler · şu an",
                        metric(data.backend.subscribers),
                      ],
                    ]}
                  />
                  <ResourceCard
                    title="Üretici kaynakları"
                    resource={data.resources.producer}
                  />
                  <ResourceCard
                    title="Sunucu kaynakları"
                    resource={data.resources.backend}
                  />
                </div>
              </section>
            )}
          </div>
        )}
      </dialog>
    </>
  );
}
