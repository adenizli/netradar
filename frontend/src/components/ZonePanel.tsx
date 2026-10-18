import { useState } from "react";
import {
  ArrowDownLeft,
  ArrowUpRight,
  Bell,
  Check,
  CircleDashed,
  Plus,
  Radio,
  Trash2,
  X,
} from "lucide-react";
import type { Aircraft, Alert, Coordinate, Zone } from "../types";
import { membersInZone } from "../lib/geofence";
import { flightName } from "../lib/geo";
import { ZONE_COLORS } from "../hooks/useZones";
interface Props {
  zones: Zone[];
  aircraft: Aircraft[];
  center: Coordinate;
  alerts: Alert[];
  onChange: (zones: Zone[]) => void;
  onSelect: (id: string) => void;
  onPreview: (radius: number | null) => void;
  composing: boolean;
  setComposing: (open: boolean) => void;
}
export function ZonePanel({
  zones,
  aircraft,
  center,
  alerts,
  onChange,
  onSelect,
  onPreview,
  composing,
  setComposing,
}: Props) {
  const [radius, setRadius] = useState(25),
    [name, setName] = useState(""),
    [expanded, setExpanded] = useState<string | null>("nearby");
  const close = () => {
    setComposing(false);
    onPreview(null);
  };
  const open = () => {
    setComposing(true);
    setRadius(25);
    setName("");
    onPreview(25);
  };
  const add = () => {
    if (zones.length >= 12) return;
    const id = crypto.randomUUID();
    onChange([
      ...zones,
      {
        id,
        name: name.trim() || `İzleme bölgesi ${zones.length + 1}`,
        radiusKm: radius,
        enabled: true,
        color: ZONE_COLORS[zones.length % ZONE_COLORS.length],
      },
    ]);
    setExpanded(id);
    close();
  };
  return (
    <aside className="zone-panel panel" aria-label="İzleme bölgeleri">
      <div className="panel-heading">
        <h2>İzleme bölgeleri</h2>
        <span className="count-badge">
          {zones.length.toString().padStart(2, "0")}
        </span>
      </div>
      <div className="zone-scroll">
        {zones.map((zone) => {
          const members = membersInZone(aircraft, zone, center);
          return (
            <section
              key={zone.id}
              className={`zone-card ${!zone.enabled ? "zone-disabled" : ""}`}
              style={{ "--zone-color": zone.color } as React.CSSProperties}
            >
              <div className="zone-card-top">
                <span className="zone-dot" />
                <button
                  className="zone-title"
                  aria-expanded={expanded === zone.id}
                  onClick={() =>
                    setExpanded(expanded === zone.id ? null : zone.id)
                  }
                >
                  {zone.name}
                </button>
                <button
                  className="icon-button"
                  aria-label={`${zone.name} sil`}
                  title="Bölgeyi sil"
                  onClick={() =>
                    onChange(zones.filter((z) => z.id !== zone.id))
                  }
                >
                  <Trash2 size={14} />
                </button>
              </div>
              <div className="zone-radius">
                <strong>
                  {zone.radiusKm}
                  <small>km yarıçap</small>
                </strong>
                <button
                  className={`switch ${zone.enabled ? "on" : ""}`}
                  role="switch"
                  aria-checked={zone.enabled}
                  aria-label={`${zone.name} izleme`}
                  onClick={() =>
                    onChange(
                      zones.map((z) =>
                        z.id === zone.id ? { ...z, enabled: !z.enabled } : z,
                      ),
                    )
                  }
                >
                  <span />
                </button>
              </div>
              <div className="zone-count">
                <span className="tiny-dot" />
                {members.length} uçak alan içerisinde{" "}
                {!zone.enabled && <span>· İzleme kapalı</span>}
              </div>
              {expanded === zone.id && (
                <div className="zone-members">
                  {members.length ? (
                    members.map((a) => (
                      <button key={a.icao} onClick={() => onSelect(a.icao)}>
                        <span>{flightName(a)}</span>
                        <span className="mono">
                          {a.icao}
                          <ArrowUpRight size={12} />
                        </span>
                      </button>
                    ))
                  ) : (
                    <p>Bu alanın içinde konumu bilinen uçak yok.</p>
                  )}
                </div>
              )}
            </section>
          );
        })}
        {!zones.length && !composing && (
          <div className="empty-state compact">
            <CircleDashed size={28} />
            <h3>Henüz bölge yok</h3>
            <p>Bir yarıçap seçerek uçak giriş ve çıkışlarını takip et.</p>
          </div>
        )}
        {composing && (
          <form
            className="zone-composer"
            onSubmit={(e) => {
              e.preventDefault();
              add();
            }}
          >
            <div className="composer-heading">
              <strong>Yeni izleme bölgesi</strong>
              <button
                type="button"
                className="icon-button"
                aria-label="Bölge eklemeyi iptal et"
                onClick={close}
              >
                <X size={16} />
              </button>
            </div>
            <label>
              Bölge adı
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                maxLength={40}
                placeholder={`İzleme bölgesi ${zones.length + 1}`}
                autoFocus
              />
            </label>
            <div className="radius-input">
              <label htmlFor="radius">Yarıçap</label>
              <span>
                <input
                  id="radius"
                  type="number"
                  min="1"
                  max="250"
                  step="1"
                  value={radius}
                  onChange={(e) => {
                    const v = Math.max(
                      1,
                      Math.min(250, Number(e.target.value) || 1),
                    );
                    setRadius(v);
                    onPreview(v);
                  }}
                />{" "}
                km
              </span>
            </div>
            <input
              type="range"
              aria-label="Bölge yarıçapı"
              min="1"
              max="150"
              value={Math.min(150, radius)}
              onChange={(e) => {
                setRadius(+e.target.value);
                onPreview(+e.target.value);
              }}
            />
            <div className="range-labels">
              <span>1 km</span>
              <span>150 km</span>
            </div>
            <p className="helper">
              Merkez: yer istasyonu. Kesik çizgi haritada önizlemedir.
            </p>
            <button className="primary-button full" type="submit">
              <Check size={16} /> Bölgeyi oluştur
            </button>
          </form>
        )}
      </div>
      <div className="activity-section">
        <div className="section-heading">
          <h3>
            <Bell size={14} /> Hareket günlüğü
          </h3>
          <span>{alerts.length}</span>
        </div>
        <div className="activity-list">
          {alerts.slice(0, 30).map((a) => (
            <button
              className="activity-item"
              key={a.id}
              onClick={() => onSelect(a.icao)}
            >
              <span className={`activity-icon ${a.kind}`}>
                {a.kind === "enter" ? (
                  <ArrowDownLeft size={15} />
                ) : (
                  <ArrowUpRight size={15} />
                )}
              </span>
              <span>
                <strong>
                  {a.callsign}{" "}
                  <small>
                    {a.kind === "enter" ? "giriş yaptı" : "çıkış yaptı"}
                  </small>
                </strong>
                <span>{a.zoneName}</span>
              </span>
              <time>
                {new Date(a.time).toLocaleTimeString("tr-TR", {
                  hour: "2-digit",
                  minute: "2-digit",
                })}
              </time>
            </button>
          ))}
          {!alerts.length && (
            <div className="activity-empty">
              <Radio size={19} />
              <p>
                Sınır geçişleri burada görünür.
                <br />
                <span>Henüz bir sınır geçişi yok.</span>
              </p>
            </div>
          )}
        </div>
      </div>
      <div className="zone-add">
        <button
          className="add-zone-button"
          aria-label="İzleme bölgesi ekle"
          onClick={composing ? close : open}
          disabled={!composing && zones.length >= 12}
        >
          <span>
            {composing
              ? "Eklemeyi iptal et"
              : zones.length >= 12
                ? "Bölge sınırına ulaşıldı"
                : "İzleme bölgesi ekle"}
          </span>
          <span className="add-zone-icon">
            {composing ? <X size={21} /> : <Plus size={22} />}
          </span>
        </button>
      </div>
    </aside>
  );
}
