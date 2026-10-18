interface Props {
  total: number;
  positioned: number;
  inZones: number;
  rate: number;
}

/** A compact summary leaves the map as the primary surface. */
export function TrafficSummary({ total, positioned, inZones, rate }: Props) {
  return (
    <section className="traffic-summary" aria-label="Trafik özeti">
      <div title="İzlenen toplam uçak">
        <strong>{total}</strong>
        <span>uçak</span>
      </div>
      <div title="Konumu çözülen uçak">
        <strong>{positioned}</strong>
        <span>konumlu</span>
      </div>
      <div title="Aktif bölgelerden en az birinin içerisindeki uçak">
        <strong>{inZones}</strong>
        <span>bölgede</span>
      </div>
      <div title="Saniyede alınan güncelleme">
        <strong>{rate}</strong>
        <span>mesaj/sn</span>
      </div>
    </section>
  );
}
