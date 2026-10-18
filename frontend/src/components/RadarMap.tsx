import { useEffect, useMemo, useRef, useState } from "react";
import L from "leaflet";
import { mapTiles } from "../config";
import { Crosshair, Layers, Minus, Plus, X } from "lucide-react";
import type { Contact, Coordinate, Zone } from "../types";
import {
  bearingText,
  destination,
  flightName,
  hasPosition,
  hasFreshPosition,
  numeric,
} from "../lib/geo";
import { positionAgeText, positionStatusText } from "../lib/freshness";
import { clusterPoints, placeLabels } from "../lib/layout";
import planeSvg from "../../public/icons/plane.svg?raw";
const planePath = /<path d="([^"]+)"/.exec(planeSvg)![1];
interface Props {
  contacts: Contact[];
  center: Coordinate;
  zones: Zone[];
  selected: string | null;
  onSelect: (id: string) => void;
  preview: number | null;
  focus: { id: string; key: number } | null;
  trails: boolean;
  connected: boolean;
  animate: boolean;
  scanning: boolean;
}
export function RadarMap({
  contacts,
  center,
  zones,
  selected,
  onSelect,
  preview,
  focus,
  trails,
  connected,
  animate,
  scanning,
}: Props) {
  const host = useRef<HTMLDivElement>(null),
    mapRef = useRef<L.Map | null>(null);
  const [view, setView] = useState({ width: 1, height: 1, version: 0 });
  const [tileError, setTileError] = useState(false),
    [clusterIds, setClusterIds] = useState<string[]>([]);
  const [baseVisible, setBaseVisible] = useState(true);
  const [moving, setMoving] = useState(false);
  useEffect(() => {
    if (!host.current) return;
    const map = L.map(host.current, {
      center,
      zoom: 9,
      zoomControl: false,
      attributionControl: true,
      minZoom: 5,
      maxZoom: 13,
      zoomSnap: 0.5,
    });
    mapRef.current = map;
    map.attributionControl.setPrefix(false);
    const tiles = L.tileLayer(mapTiles.url, {
      attribution: mapTiles.attribution,
      maxZoom: 19,
    }).addTo(map);
    tiles.on("tileerror", () => setTileError(true));
    tiles.on("tileload", () => setTileError(false));
    const update = () => {
      const size = map.getSize();
      setView((v) => ({
        width: size.x,
        height: size.y,
        version: v.version + 1,
      }));
    };
    const start = () => setMoving(true),
      end = () => {
        setMoving(false);
        update();
      };
    map.on("move zoom resize", update);
    map.on("movestart zoomstart", start);
    map.on("moveend zoomend", end);
    const observer = new ResizeObserver(() => {
      map.invalidateSize({ pan: false });
      update();
    });
    observer.observe(host.current);
    update();
    return () => {
      observer.disconnect();
      map.remove();
      mapRef.current = null;
    };
    // A Leaflet instance owns the map until unmount. Center changes handled below.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  useEffect(() => {
    mapRef.current?.setView(center, mapRef.current.getZoom(), {
      animate: false,
    });
  }, [center[0], center[1]]);
  useEffect(() => {
    if (!focus) return;
    const contact = contacts.find((c) => c.aircraft.icao === focus.id);
    if (contact && hasPosition(contact.aircraft))
      mapRef.current?.setView(
        [contact.aircraft.latitude!, contact.aircraft.longitude!],
        Math.max(mapRef.current.getZoom(), 10),
        { animate: false },
      );
  }, [focus]);
  const map = mapRef.current;
  const project = (c: Coordinate) =>
    map ? map.latLngToContainerPoint(c) : L.point(0, 0);
  const origin = project(center);
  const points = contacts
    .filter((c) => hasPosition(c.aircraft))
    .map((c) => ({
      id: c.aircraft.icao,
      ...project([c.aircraft.latitude!, c.aircraft.longitude!]),
    }))
    .filter(
      (p) =>
        p.x > -30 &&
        p.x < view.width + 30 &&
        p.y > 65 &&
        p.y < view.height - 45,
    );
  const clusters = useMemo(() => clusterPoints(points), [contacts, view]);
  const singles = clusters.filter((c) => c.members.length === 1);
  const labels = placeLabels(singles, view.width, view.height, selected, [
    { x: 0, y: view.height - 235, width: 60, height: 235 },
    { x: 0, y: 0, width: view.width, height: 78 },
    { x: 0, y: view.height - 65, width: view.width, height: 65 },
    { x: origin.x - 75, y: origin.y - 15, width: 150, height: 65 },
    ...clusters
      .filter((c) => c.members.length > 1)
      .map((c) => ({ x: c.x - 22, y: c.y - 22, width: 44, height: 44 })),
  ]);
  const byId = new Map(contacts.map((c) => [c.aircraft.icao, c]));
  const pathForRadius = (radius: number) =>
    Array.from({ length: 73 }, (_, i) => {
      const p = project(destination(center, i * 5, radius));
      return `${i ? "L" : "M"}${p.x},${p.y}`;
    }).join(" ") + "Z";
  const radius = Math.abs(project(destination(center, 90, 60)).x - origin.x);
  const selectedContact = selected ? byId.get(selected) : null;
  const visibleCluster = clusterIds
    .map((id) => byId.get(id))
    .filter((c): c is Contact => !!c);
  return (
    <section
      className={`radar-map ${!baseVisible ? "hide-basemap" : ""}`}
      aria-label="Radar haritası"
    >
      <div ref={host} className="leaflet-host" />
      <div className="map-grid" aria-hidden="true" />
      <div className="map-vignette" aria-hidden="true" />
      {map && (
        <>
          <div
            className={`radar-sweep ${!connected || !scanning ? "stopped" : ""}`}
            style={{
              left: origin.x - radius,
              top: origin.y - radius,
              width: radius * 2,
              height: radius * 2,
            }}
            aria-hidden="true"
          >
            <div className="sweep-beam" />
            <div className="radar-ripple" />
            <div className="radar-ripple delayed" />
          </div>
          <svg
            className="radar-overlay"
            width={view.width}
            height={view.height}
            aria-label="Uçak konumları"
          >
            <g className="range-rings" fill="none">
              {[10, 20, 40, 60].map((km) => (
                <g key={km}>
                  <path d={pathForRadius(km)} />
                  <text
                    x={origin.x + 8}
                    y={project(destination(center, 0, km)).y - 7}
                  >
                    {km} KM
                  </text>
                </g>
              ))}
              <path
                className="cross-lines"
                d={`M${origin.x - radius},${origin.y}H${origin.x + radius}M${origin.x},${origin.y - radius}V${origin.y + radius}`}
              />
            </g>
            {zones
              .filter((z) => z.enabled)
              .map((z) => (
                <path
                  key={z.id}
                  className="zone-shape"
                  d={pathForRadius(z.radiusKm)}
                  style={{ stroke: z.color, fill: z.color }}
                />
              ))}
            {preview !== null && (
              <path className="zone-preview" d={pathForRadius(preview)} />
            )}
            {trails &&
              contacts
                .filter(
                  (c) => c.trail.length > 1 && hasFreshPosition(c.aircraft),
                )
                .map((c) => (
                  <polyline
                    key={c.aircraft.icao}
                    className={`aircraft-trail ${selected === c.aircraft.icao ? "selected" : ""}`}
                    points={c.trail
                      .map((p) => {
                        const xy = project(p);
                        return `${xy.x},${xy.y}`;
                      })
                      .join(" ")}
                  />
                ))}
            <g
              className="receiver"
              transform={`translate(${origin.x},${origin.y})`}
            >
              <circle r="17" />
              <circle r="7" />
              <circle r="2.5" className="receiver-core" />
              <path d="M-25 0h6M19 0h6M0-25v6M0 19v6" />
              <rect x="-70" y="28" width="140" height="24" rx="4" />
              <text y="44" textAnchor="middle">
                NR-01 · YER İSTASYONU
              </text>
            </g>
            {clusters.map((cluster) => {
              const contact = byId.get(cluster.id)!;
              const a = contact.aircraft;
              const grouped = cluster.members.length > 1,
                isSelected = cluster.members.some((p) => p.id === selected),
                label = labels.get(cluster.id);
              const groupFresh = cluster.members.every((p) =>
                hasFreshPosition(byId.get(p.id)!.aircraft),
              );
              const positionClass = groupFresh
                ? ""
                : a.position_status === "stale"
                  ? "position-stale"
                  : "position-unknown";
              const act = () =>
                grouped
                  ? setClusterIds(cluster.members.map((p) => p.id))
                  : onSelect(a.icao);
              return (
                <g
                  key={cluster.id}
                  className={`aircraft-marker ${isSelected ? "is-selected" : ""} ${!connected ? "is-stale" : ""} ${positionClass}`}
                >
                  {label && !grouped && (
                    <g
                      className="aircraft-label"
                      onClick={act}
                      aria-hidden="true"
                    >
                      <path
                        className="label-leader"
                        d={`M${cluster.x},${cluster.y}L${label.x < cluster.x ? label.x + label.width : label.x},${label.y + 17}`}
                      />
                      <rect
                        x={label.x}
                        y={label.y}
                        width={label.width}
                        height={label.height}
                        rx="3"
                      />
                      <text
                        x={label.x + 8}
                        y={label.y + 16}
                        className="label-callsign"
                      >
                        {flightName(a).slice(0, 12)}
                      </text>
                      <text
                        x={label.x + 8}
                        y={label.y + 33}
                        className="label-detail"
                      >
                        {a.position_status === "fresh"
                          ? `${numeric(a.altitude_ft)} FT · ${numeric(a.speed_knots)} KT`
                          : a.position_status === "stale"
                            ? `KONUM ESKİ · ${positionAgeText(a)}`
                            : "ZAMAN BİLİNMİYOR"}
                      </text>
                    </g>
                  )}
                  <g
                    className={`plane-position ${animate && !moving ? "smooth" : ""}`}
                    style={{
                      transform: `translate(${cluster.x}px, ${cluster.y}px)`,
                    }}
                    role="button"
                    tabIndex={0}
                    aria-label={
                      grouped
                        ? `${cluster.members.length} uçak grubu`
                        : `${flightName(a)} uçak detayları`
                    }
                    onClick={act}
                    onKeyDown={(e) => {
                      if (e.key === "Enter" || e.key === " ") {
                        e.preventDefault();
                        act();
                      }
                    }}
                  >
                    <title>
                      {grouped
                        ? cluster.members
                            .map(
                              (p) =>
                                `${flightName(byId.get(p.id)!.aircraft)} · ${positionStatusText(byId.get(p.id)!.aircraft)}`,
                            )
                            .join(", ")
                        : `${flightName(a)} · ${positionStatusText(a)} · ${numeric(a.altitude_ft)} ft · Yer izi ${bearingText(a.track_deg)}`}
                    </title>
                    <circle className="plane-hit-target" r="18" />
                    {grouped ? (
                      <>
                        <circle className="cluster-disc" r="18" />
                        <text
                          className="cluster-count"
                          textAnchor="middle"
                          y="5"
                        >
                          {cluster.members.length}
                        </text>
                      </>
                    ) : (
                      <>
                        {isSelected && (
                          <circle className="selection-ring" r="22" />
                        )}
                        {a.track_deg !== null ? (
                          <path
                            className="plane-shape"
                            d={planePath}
                            transform={`rotate(${a.track_deg - 90}) translate(-14,-14) scale(1.1667)`}
                          />
                        ) : (
                          <>
                            <path
                              className="unknown-track"
                              d="M0-10 10 0 0 10-10 0Z"
                            />
                            <circle r="2" fill="currentColor" />
                          </>
                        )}
                      </>
                    )}
                  </g>
                </g>
              );
            })}
          </svg>
        </>
      )}
      <div className="map-topbar">
        <div className="map-heading">
          <span className="live-dot" />
          <span>RADAR GÖRÜNÜMÜ</span>
          <span className="map-heading-divider" />
          <span className="mono">1090 MHz</span>
        </div>
        <div className="north-compass">
          <span>K</span>
          <svg width="16" height="23" viewBox="0 0 16 23" aria-hidden="true">
            <path d="m8 1 6 18-6-4-6 4Z" fill="currentColor" />
            <path d="M8 1v14l-6 4Z" fill="#345345" />
          </svg>
        </div>
      </div>
      <div className="map-tools">
        <button
          className="map-tool"
          aria-label="Haritayı yakınlaştır"
          title="Yakınlaştır"
          onClick={() => map?.zoomIn()}
        >
          <Plus size={18} />
        </button>
        <button
          className="map-tool"
          aria-label="Haritayı uzaklaştır"
          title="Uzaklaştır"
          onClick={() => map?.zoomOut()}
        >
          <Minus size={18} />
        </button>
        <span />
        <button
          className="map-tool"
          aria-label="İstasyona dön"
          title="İstasyona dön"
          onClick={() => map?.setView(center, 9)}
        >
          <Crosshair size={18} />
        </button>
        <button
          className={`map-tool ${!baseVisible ? "active" : ""}`}
          aria-label="Harita altlığını aç/kapat"
          title="Harita altlığı"
          aria-pressed={baseVisible}
          onClick={() => setBaseVisible((v) => !v)}
        >
          <Layers size={18} />
        </button>
      </div>
      <div className="map-bottom">
        <span className="map-coordinate mono">
          {Math.abs(center[0]).toFixed(3)}° {center[0] >= 0 ? "K" : "G"}{" "}
          <span>/</span> {Math.abs(center[1]).toFixed(3)}°{" "}
          {center[1] >= 0 ? "D" : "B"}
        </span>
        <div className="map-legend">
          <i />
          Uçak <i className="legend-station" />
          İstasyon{" "}
          {selectedContact && (
            <span className="selected-label">
              {flightName(selectedContact.aircraft)} seçili
            </span>
          )}
        </div>
      </div>
      {tileError && (
        <div className="tile-warning">
          Harita altlığı yüklenemedi · radar katmanı çalışıyor
        </div>
      )}
      {visibleCluster.length > 0 && (
        <div className="cluster-picker">
          <div>
            <strong>Yakın konumdaki uçaklar</strong>
            <button
              className="icon-button"
              aria-label="Uçak grubunu kapat"
              onClick={() => setClusterIds([])}
            >
              <X size={16} />
            </button>
          </div>
          <p>Gerçek konumlar korunur. İncelemek için bir uçak seçin.</p>
          {visibleCluster.map((c) => (
            <button
              key={c.aircraft.icao}
              onClick={() => {
                onSelect(c.aircraft.icao);
                setClusterIds([]);
              }}
            >
              <strong>{flightName(c.aircraft)}</strong>
              <span>
                {c.aircraft.icao} · {positionStatusText(c.aircraft)}
              </span>
            </button>
          ))}
        </div>
      )}
    </section>
  );
}
