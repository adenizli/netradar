import { useEffect, useMemo, useState } from "react";
import {
  Check,
  ChevronDown,
  ExternalLink,
  Pause,
  Play,
  RadioTower,
  Settings2,
} from "lucide-react";
import type { Settings, SourceMode } from "./types";
import { TrafficSummary } from "./components/TrafficSummary";
import { Notifications } from "./components/Notifications";
import { RadarGlyph } from "./components/Brand";
import { FlightPanel } from "./components/FlightPanel";
import { RadarMap } from "./components/RadarMap";
import { ZonePanel } from "./components/ZonePanel";
import { SettingsDialog } from "./components/SettingsDialog";
import { useTraffic } from "./hooks/useTraffic";
import { useZones } from "./hooks/useZones";
import { hasPosition } from "./lib/geo";
import { membersInZone } from "./lib/geofence";
const DEFAULT_SETTINGS: Settings = {
  receiver: [41.085, 29.051],
  stationName: "İstanbul · Boğaziçi",
  backendUrl: import.meta.env.VITE_BACKEND_URL || "http://localhost:8000",
};
function readSettings(): Settings {
  try {
    const s = JSON.parse(
      localStorage.getItem("netradar.settings.v1") ?? "null",
    );
    if (
      s &&
      typeof s.stationName === "string" &&
      s.stationName.length <= 45 &&
      typeof s.backendUrl === "string" &&
      /^https?:\/\//.test(s.backendUrl) &&
      Array.isArray(s.receiver) &&
      s.receiver.length === 2 &&
      s.receiver.every(
        (n: unknown) => typeof n === "number" && Number.isFinite(n),
      ) &&
      Math.abs(s.receiver[0]) <= 85 &&
      Math.abs(s.receiver[1]) <= 180
    )
      return s;
  } catch {
    /* Defaults work with unavailable storage. */
  }
  return DEFAULT_SETTINGS;
}
function Clock() {
  const [now, setNow] = useState(new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <div className="clock">
      <strong>
        {now.toLocaleTimeString("en-GB", { timeZone: "UTC" })}
        <small> UTC</small>
      </strong>
    </div>
  );
}
export default function App() {
  const [settings, setSettings] = useState<Settings>(readSettings);
  const [mode, setMode] = useState<SourceMode>("demo"),
    [paused, setPaused] = useState(false),
    [speed, setSpeed] = useState(4);
  const [selected, setSelected] = useState<string | null>(null),
    [query, setQuery] = useState("");
  const [settingsOpen, setSettingsOpen] = useState(false),
    [trails, setTrails] = useState(true);
  const [composing, setComposing] = useState(false),
    [preview, setPreview] = useState<number | null>(null);
  const [focus, setFocus] = useState<{ id: string; key: number } | null>(null);
  const { contacts, connection, rate, session } = useTraffic(
    mode,
    settings.backendUrl,
    paused,
    speed,
  );
  const aircraft = useMemo(() => contacts.map((c) => c.aircraft), [contacts]);
  const { zones, setZones, alerts, toasts, dismiss } = useZones(
    aircraft,
    settings.receiver,
    session,
    connection.state === "connected",
  );
  const mapped = aircraft.filter(hasPosition).length;
  const inZones = new Set(
    zones
      .filter((z) => z.enabled)
      .flatMap((z) =>
        membersInZone(aircraft, z, settings.receiver).map((a) => a.icao),
      ),
  ).size;
  const connected =
    connection.state === "connected" || connection.state === "paused";
  const changeMode = (next: SourceMode) => {
    setMode(next);
    setSelected(null);
    setQuery("");
    setPaused(false);
  };
  const select = (id: string | null) => {
    setSelected(id);
  };
  useEffect(() => {
    if (selected && !contacts.some((c) => c.aircraft.icao === selected))
      setSelected(null);
  }, [contacts, selected]);
  useEffect(() => {
    try {
      localStorage.setItem("netradar.settings.v1", JSON.stringify(settings));
    } catch {
      /* Keep settings in this session. */
    }
  }, [settings]);
  useEffect(() => {
    const escape = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !settingsOpen) {
        setSelected(null);
        setComposing(false);
        setPreview(null);
      }
    };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [settingsOpen]);
  return (
    <div className="app-shell">
      <header className="app-header">
        <a className="brand" href="/" aria-label="NetRadar ana ekran">
          <RadarGlyph size={34} />
          <span>
            NET<span>RADAR</span>
            <small>ADS-B RECEIVER</small>
          </span>
        </a>
        <div className="header-right">
          <div
            className={`connection-pill ${!connected ? "disconnected" : ""}`}
            title={connection.message}
          >
            <span className="live-dot" />
            {mode === "demo"
              ? paused
                ? "Demo duraklatıldı"
                : "Kurgusal demo"
              : connected
                ? "Alıcı bağlı"
                : "Bağlantı bekleniyor"}
          </div>
          <Clock />
          <button
            className="header-settings icon-button"
            title="Alıcı ayarları"
            aria-label="Alıcı ayarları"
            onClick={() => setSettingsOpen(true)}
          >
            <Settings2 size={20} />
          </button>
        </div>
      </header>
      <main className="workspace">
        <div className="workspace-heading">
          <div>
            <h1>Hava sahası</h1>
            <p>
              <RadioTower size={13} />
              {settings.stationName}
            </p>
          </div>
          <div className="workspace-actions">
            <div className="source-toggle" aria-label="Veri kaynağı">
              <button
                aria-pressed={mode === "demo"}
                className={mode === "demo" ? "active" : ""}
                onClick={() => changeMode("demo")}
              >
                Demo
              </button>
              <button
                aria-pressed={mode === "live"}
                className={mode === "live" ? "active" : ""}
                onClick={() => changeMode("live")}
              >
                Canlı alıcı{mode === "live" && <span className="tiny-dot" />}
              </button>
            </div>
          </div>
        </div>
        <div className="radar-toolbar">
          <TrafficSummary
            total={aircraft.length}
            positioned={mapped}
            inZones={inZones}
            rate={rate}
          />
          <div className="playback-tools">
            {mode === "demo" && (
              <>
                <button
                  className="text-button pause-button"
                  onClick={() => setPaused((p) => !p)}
                  aria-label={
                    paused ? "Simülasyonu sürdür" : "Simülasyonu duraklat"
                  }
                >
                  {paused ? <Play size={13} /> : <Pause size={13} />}{" "}
                  {paused ? "Sürdür" : "Duraklat"}
                </button>
                <label className="speed-select">
                  <select
                    aria-label="Demo hızı"
                    value={speed}
                    onChange={(e) => setSpeed(+e.target.value)}
                  >
                    <option value="1">1× hız</option>
                    <option value="4">4× hız</option>
                    <option value="12">12× hız</option>
                  </select>
                  <ChevronDown size={12} />
                </label>
              </>
            )}
            <button
              className={`trail-toggle ${trails ? "active" : ""}`}
              aria-pressed={trails}
              onClick={() => setTrails((t) => !t)}
            >
              <Check size={13} /> Uçuş izleri
            </button>
          </div>
        </div>
        {mode === "live" && !connected && (
          <div className="connection-banner" role="status">
            <RadioTower size={15} />
            <span>{connection.message}</span>
            <button onClick={() => setSettingsOpen(true)}>
              Bağlantı ayarları <ExternalLink size={12} />
            </button>
          </div>
        )}
        <div className="radar-workspace">
          <FlightPanel
            contacts={contacts}
            selected={selected}
            onSelect={select}
            center={settings.receiver}
            onLocate={(id) => setFocus({ id, key: Date.now() })}
            query={query}
            setQuery={setQuery}
          />
          <div className="map-frame">
            <RadarMap
              contacts={contacts}
              center={settings.receiver}
              zones={zones}
              selected={selected}
              onSelect={select}
              preview={preview}
              focus={focus}
              trails={trails}
              connected={connected}
              scanning={!paused}
              animate={mode === "demo" && !paused}
            />
            <Notifications
              alerts={toasts}
              onSelect={select}
              onDismiss={dismiss}
            />
          </div>
          <ZonePanel
            zones={zones}
            aircraft={aircraft}
            center={settings.receiver}
            alerts={alerts}
            onChange={setZones}
            onSelect={select}
            onPreview={setPreview}
            composing={composing}
            setComposing={setComposing}
          />
        </div>
      </main>
      {settingsOpen && (
        <SettingsDialog
          settings={settings}
          onSave={(s) => {
            setSettings(s);
            setComposing(false);
            setPreview(null);
          }}
          onClose={() => setSettingsOpen(false)}
        />
      )}
    </div>
  );
}
