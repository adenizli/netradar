import { useEffect, useState } from "react";
import {
  ArrowDownLeft,
  ArrowLeft,
  ArrowUpRight,
  ChevronRight,
  Crosshair,
  FileCode2,
  Plane,
  Search,
  X,
} from "lucide-react";
import { positionAgeText, positionStatusText } from "../lib/freshness";
import type { Contact, Coordinate, DecoderEvent } from "../types";
import {
  bearingText,
  distanceKm,
  flightName,
  hasPosition,
  numeric,
} from "../lib/geo";
interface Props {
  contacts: Contact[];
  selected: string | null;
  onSelect: (id: string | null) => void;
  center: Coordinate;
  onLocate: (id: string) => void;
  query: string;
  setQuery: (query: string) => void;
}
function Metric({
  label,
  value,
  unit,
}: {
  label: string;
  value: string;
  unit?: string;
}) {
  return (
    <div className="flight-metric">
      <span>{label}</span>
      <strong>
        {value} <small>{unit}</small>
      </strong>
    </div>
  );
}
function EventCard({ event, slot }: { event: DecoderEvent; slot: string }) {
  return (
    <details className="event-card">
      <summary>
        <FileCode2 size={15} />
        <span>{slot.replaceAll("_", " ")}</span>
        <ChevronRight size={14} />
      </summary>
      <div className="event-body">
        <span className="eyebrow">HAM MESAJ · 112 BİT</span>
        <code className="raw-frame">{event.raw_frame ?? "Mevcut değil"}</code>
        <pre>{JSON.stringify(event, null, 2)}</pre>
      </div>
    </details>
  );
}
export function FlightPanel({
  contacts,
  selected,
  onSelect,
  center,
  onLocate,
  query,
  setQuery,
}: Props) {
  const [tab, setTab] = useState<"summary" | "messages">("summary");
  useEffect(() => setTab("summary"), [selected]);
  const contact = contacts.find((c) => c.aircraft.icao === selected);
  const sorted = [...contacts]
    .filter((c) =>
      `${c.aircraft.icao} ${c.aircraft.callsign ?? ""}`
        .toLowerCase()
        .includes(query.toLowerCase()),
    )
    .sort(
      (a, b) =>
        Number(hasPosition(b.aircraft)) - Number(hasPosition(a.aircraft)) ||
        flightName(a.aircraft).localeCompare(flightName(b.aircraft)),
    );
  if (contact) {
    const a = contact.aircraft;
    const events = { ...a.latest_events };
    if (
      a.position_event &&
      !Object.values(events).some(
        (e) => JSON.stringify(e) === JSON.stringify(a.position_event),
      )
    )
      events["kabul edilen konum"] = a.position_event;
    if (
      contact.event &&
      !Object.values(events).some(
        (e) => JSON.stringify(e) === JSON.stringify(contact.event),
      )
    )
      events["son mesaj"] = contact.event;
    return (
      <aside className="flight-panel panel" aria-label="Uçak detayları">
        <div className="panel-heading">
          <button className="text-button" onClick={() => onSelect(null)}>
            <ArrowLeft size={15} /> Uçak listesi
          </button>
          <button
            className="icon-button"
            title="Detayları kapat"
            aria-label="Detayları kapat"
            onClick={() => onSelect(null)}
          >
            <X size={16} />
          </button>
        </div>
        <div className="selected-flight">
          <div className="flight-symbol">
            <Plane size={26} />
          </div>
          <span className="eyebrow">SEÇİLİ UÇAK</span>
          <h2>{flightName(a)}</h2>
          <p>
            ICAO <span className="mono">{a.icao}</span>{" "}
          </p>
          <p className={`position-health ${a.position_status ?? "unknown"}`}>
            {positionStatusText(a)}
          </p>
        </div>
        <div className="tabs" role="tablist" aria-label="Uçak bilgileri">
          <button
            role="tab"
            aria-selected={tab === "summary"}
            onClick={() => setTab("summary")}
          >
            Genel bilgiler
          </button>
          <button
            role="tab"
            aria-selected={tab === "messages"}
            onClick={() => setTab("messages")}
          >
            Mesajlar <span>{Object.keys(events).length}</span>
          </button>
        </div>
        <div className="panel-scroll" role="tabpanel">
          {tab === "summary" ? (
            <>
              <div className="metric-grid">
                <Metric
                  label="Barometrik irtifa"
                  value={numeric(a.altitude_ft)}
                  unit="ft"
                />
                <Metric
                  label="Yer hızı"
                  value={numeric(a.speed_knots)}
                  unit="kt"
                />
                <Metric
                  label="Gerçek yer izi"
                  value={bearingText(a.track_deg)}
                />
                <Metric
                  label="Dikey hız"
                  value={numeric(a.vertical_rate_fpm)}
                  unit="ft/dk"
                />
              </div>
              <section className="detail-section">
                <h3>Konum</h3>
                <dl>
                  <div>
                    <dt>Konum yaşı</dt>
                    <dd>{positionAgeText(a)}</dd>
                  </div>
                  <div>
                    <dt>Konum ölçüm zamanı</dt>
                    <dd>
                      {a.position_observed_at_unix_ms != null
                        ? new Date(
                            a.position_observed_at_unix_ms,
                          ).toLocaleTimeString("tr-TR", { timeZone: "UTC" }) +
                          " UTC"
                        : "Bilinmiyor"}
                    </dd>
                  </div>
                  <div>
                    <dt>Enlem</dt>
                    <dd>
                      {numeric(a.latitude, 5)}
                      {a.latitude !== null ? "°" : ""}
                    </dd>
                  </div>
                  <div>
                    <dt>Boylam</dt>
                    <dd>
                      {numeric(a.longitude, 5)}
                      {a.longitude !== null ? "°" : ""}
                    </dd>
                  </div>
                  <div>
                    <dt>İstasyona uzaklık</dt>
                    <dd>
                      {hasPosition(a)
                        ? `${numeric(distanceKm(center, [a.latitude!, a.longitude!]), 1)} km`
                        : "—"}
                    </dd>
                  </div>
                  <div>
                    <dt>Konum kaynağı</dt>
                    <dd>
                      {a.position_source === "gnss"
                        ? "GNSS"
                        : a.position_source === "barometric"
                          ? "Barometrik CPR"
                          : "—"}
                    </dd>
                  </div>
                </dl>
                <button
                  className="outline-button full"
                  disabled={!hasPosition(a)}
                  onClick={() => onLocate(a.icao)}
                >
                  <Crosshair size={15} /> Haritada odaklan
                </button>
              </section>
              <section className="detail-section">
                <h3>Diğer ölçümler</h3>
                <dl>
                  <div>
                    <dt>GNSS yüksekliği</dt>
                    <dd>{numeric(a.gnss_height_ft)} ft</dd>
                  </div>
                  <div>
                    <dt>Hava hızı {a.airspeed_type?.toUpperCase()}</dt>
                    <dd>{numeric(a.airspeed_knots)} kt</dd>
                  </div>
                  <div>
                    <dt>Manyetik baş açısı</dt>
                    <dd>{bearingText(a.heading_deg)}</dd>
                  </div>
                  <div>
                    <dt>Dikey hız kaynağı</dt>
                    <dd>
                      {a.vertical_rate_source === "gnss"
                        ? "GNSS"
                        : a.vertical_rate_source === "barometric"
                          ? "Barometrik"
                          : "—"}
                    </dd>
                  </div>
                </dl>
              </section>
              <section className="detail-section">
                <h3>Zaman bilgisi</h3>
                <dl>
                  <div>
                    <dt>Son mesajın ölçümü</dt>
                    <dd>
                      {a.last_seen
                        ? new Date(a.last_seen).toLocaleTimeString("tr-TR", {
                            timeZone: "UTC",
                          }) + " UTC"
                        : "Bilinmiyor"}
                    </dd>
                  </div>
                  <div>
                    <dt>Son mesaj alımı</dt>
                    <dd>
                      {new Date(contact.receivedAt).toLocaleTimeString("tr-TR")}
                    </dd>
                  </div>
                </dl>
                <p className="helper">
                  Mesaj alım saati, RF ölçüm zamanı yerine kullanılmaz. Konum
                  yaşı, konuma ait ölçüm zamanından hesaplanır. Ölçüm zamanı
                  yoksa yeni konumun alımı ve kayıt içindeki örnek farkı esas
                  alınır. Çağrı adı mesajı konumu güncellemez.
                </p>
              </section>
              <details className="state-json">
                <summary>Tüm uçak verisi</summary>
                <pre>{JSON.stringify(a, null, 2)}</pre>
              </details>
            </>
          ) : (
            <>
              <div className="message-intro">
                <span className="eyebrow">DECODER → BACKEND → ARAYÜZ</span>
                <p>
                  Ham çerçeve, CPR, metadata ve ek alanlar eksiksiz korunur.
                </p>
              </div>
              {Object.entries(events).map(([slot, event]) => (
                <EventCard key={slot} event={event} slot={slot} />
              ))}
              {!Object.keys(events).length && (
                <div className="empty-state">
                  <FileCode2 size={26} />
                  <h3>Ham mesaj yok</h3>
                  <p>
                    Demo verileri kurgusaldır. Gerçek decoder mesajları
                    alındığında burada görünecek.
                  </p>
                </div>
              )}
            </>
          )}
        </div>
      </aside>
    );
  }
  return (
    <aside className="flight-panel panel" aria-label="Uçak listesi">
      <div className="panel-heading">
        <h2>
          Uçaklar{" "}
          <span className="count-badge">
            {contacts.length.toString().padStart(2, "0")}
          </span>
        </h2>
        <span className="eyebrow">TÜM TRAFİK</span>
      </div>
      <label className="search-field">
        <Search size={16} />
        <input
          aria-label="Uçak ara"
          placeholder="Çağrı adı veya ICAO ara"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
        {query && (
          <button
            className="icon-button"
            aria-label="Aramayı temizle"
            onClick={() => setQuery("")}
          >
            <X size={14} />
          </button>
        )}
      </label>
      <div className="list-column-labels">
        <span>ÇAĞRI ADI / ICAO</span>
        <span>İRTİFA · HIZ</span>
      </div>
      <div className="panel-scroll flight-list">
        {sorted.map(({ aircraft: a }) => (
          <button
            className="flight-row"
            key={a.icao}
            onClick={() => {
              setTab("summary");
              onSelect(a.icao);
            }}
          >
            <div
              className={`row-plane ${!hasPosition(a) ? "unlocated" : ""} ${a.position_status !== "fresh" ? "outdated" : ""}`}
            >
              <Plane size={19} />
            </div>
            <div className="flight-identity">
              <strong>{flightName(a)}</strong>
              <span className="mono" title={positionStatusText(a)}>
                {a.icao}
                {!hasPosition(a)
                  ? " · Konum yok"
                  : a.position_status === "stale"
                    ? " · Eski konum"
                    : a.position_status !== "fresh"
                      ? " · Zaman yok"
                      : ""}
              </span>
            </div>
            <div className="flight-values">
              <strong>
                {numeric(a.altitude_ft)} <small>ft</small>
              </strong>
              <span>
                {numeric(a.speed_knots)} kt{" "}
                {a.vertical_rate_fpm != null && a.vertical_rate_fpm !== 0 ? (
                  a.vertical_rate_fpm > 0 ? (
                    <ArrowUpRight size={12} />
                  ) : (
                    <ArrowDownLeft size={12} />
                  )
                ) : null}
              </span>
            </div>
            <ChevronRight className="row-chevron" size={14} />
          </button>
        ))}
        {!sorted.length && (
          <div className="empty-state">
            <Search size={25} />
            <h3>{query ? "Uçak bulunamadı" : "Trafik bekleniyor"}</h3>
            <p>
              {query
                ? "Başka bir çağrı adı veya ICAO deneyin."
                : "Konumu çözülmeyen uçaklar da bu listede görünür."}
            </p>
          </div>
        )}
      </div>
      <div className="panel-footnote">
        <span className="tiny-dot" />{" "}
        {contacts.filter((c) => !hasPosition(c.aircraft)).length} uçak konum
        bekliyor
      </div>
    </aside>
  );
}
