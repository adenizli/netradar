import { ArrowDownLeft, ArrowUpRight, X } from "lucide-react";
import type { Alert } from "../types";
export function Notifications({
  alerts,
  onSelect,
  onDismiss,
}: {
  alerts: Alert[];
  onSelect: (id: string) => void;
  onDismiss: (id: string) => void;
}) {
  return (
    <div
      className="toast-stack"
      aria-live="polite"
      aria-label="Bölge bildirimleri"
    >
      {alerts.slice(0, 1).map((a) => (
        <div className={`toast ${a.kind}`} key={a.id}>
          <span className="toast-icon">
            {a.kind === "enter" ? (
              <ArrowDownLeft size={20} />
            ) : (
              <ArrowUpRight size={20} />
            )}
          </span>
          <button className="toast-content" onClick={() => onSelect(a.icao)}>
            <strong>
              {a.callsign}{" "}
              {a.kind === "enter" ? "bölgeye girdi" : "bölgeden çıktı"}
            </strong>
            <span>
              {a.zoneName} · {new Date(a.time).toLocaleTimeString("tr-TR")}
              {alerts.length > 1
                ? ` · +${alerts.length - 1} hareket günlükte`
                : ""}
            </span>
          </button>
          <button
            className="icon-button"
            aria-label="Bildirimi kapat"
            onClick={() => onDismiss(a.id)}
          >
            <X size={15} />
          </button>
        </div>
      ))}
    </div>
  );
}
