import { useEffect, useRef, useState } from "react";
import { Check, RadioTower, X } from "lucide-react";
import type { Settings } from "../types";
import { endpoint } from "../data/live";
export function SettingsDialog({
  settings,
  onSave,
  onClose,
}: {
  settings: Settings;
  onSave: (s: Settings) => void;
  onClose: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [draft, setDraft] = useState(settings),
    [lat, setLat] = useState(String(settings.receiver[0])),
    [lon, setLon] = useState(String(settings.receiver[1])),
    [error, setError] = useState("");
  useEffect(() => {
    dialog.current?.showModal();
    return () => dialog.current?.close();
  }, []);
  return (
    <dialog
      ref={dialog}
      className="settings-dialog"
      onCancel={onClose}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <form
        onSubmit={(e) => {
          e.preventDefault();
          try {
            endpoint(draft.backendUrl, "api/aircraft");
            const latitude = Number(lat),
              longitude = Number(lon);
            if (
              !lat.trim() ||
              !lon.trim() ||
              !Number.isFinite(latitude) ||
              Math.abs(latitude) > 85 ||
              !Number.isFinite(longitude) ||
              Math.abs(longitude) > 180
            )
              throw new Error(
                "Enlem −85…85, boylam −180…180 aralığında olmalı.",
              );
            onSave({
              ...draft,
              stationName: draft.stationName.trim() || "Yer istasyonu",
              receiver: [latitude, longitude],
            });
            onClose();
          } catch (e) {
            setError((e as Error).message);
          }
        }}
      >
        <header>
          <span className="eyebrow">
            <RadioTower size={14} /> YER İSTASYONU
          </span>
          <button
            type="button"
            className="icon-button"
            aria-label="Ayarları kapat"
            onClick={onClose}
          >
            <X size={19} />
          </button>
        </header>
        <h2>Alıcı ayarları</h2>
        <p>Konum, radar merkezi ve tüm izleme bölgeleri için kullanılır.</p>
        <label>
          İstasyon adı
          <input
            value={draft.stationName}
            maxLength={45}
            onChange={(e) =>
              setDraft({ ...draft, stationName: e.target.value })
            }
          />
        </label>
        <div className="form-grid">
          <label>
            Enlem (°)
            <input
              type="number"
              step="any"
              min="-85"
              max="85"
              required
              value={lat}
              onChange={(e) => setLat(e.target.value)}
            />
          </label>
          <label>
            Boylam (°)
            <input
              type="number"
              step="any"
              min="-180"
              max="180"
              required
              value={lon}
              onChange={(e) => setLon(e.target.value)}
            />
          </label>
        </div>
        <label>
          Backend adresi
          <input
            type="url"
            required
            value={draft.backendUrl}
            onChange={(e) => setDraft({ ...draft, backendUrl: e.target.value })}
            placeholder="http://raspberrypi.local:8000"
          />
        </label>
        <div className="endpoint-note">
          <code>GET /api/aircraft</code>
          <code>WS /ws/aircraft</code>
          <p>
            Canlı mod, bu servisleri sağlayan backend’e bağlanır. Demo modunda
            bağlantı kurulmaz.
          </p>
        </div>
        {error && (
          <p className="form-error" role="alert">
            {error}
          </p>
        )}
        <footer>
          <button type="button" className="outline-button" onClick={onClose}>
            Vazgeç
          </button>
          <button className="primary-button" type="submit">
            <Check size={16} /> Ayarları kaydet
          </button>
        </footer>
      </form>
    </dialog>
  );
}
