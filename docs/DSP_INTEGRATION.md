# DSP → decoder → FastAPI bağlantısı

## 1. Önceden eksik olan neydi?

`detect_frames(chunks)` gelen `IqChunk` akışını kullanmıyor, çalışma dizinindeki
sabit `adsb.iq` dosyasını okuyup grafik/istatistik üretiyordu. Iterator sözleşmesine
rağmen `ModeSFrame` döndürmediği için decoder'a bağlanamıyordu.
`replay_cu8()` da `NotImplementedError` veriyordu. Bu iki bağlantı tamamlandı.

## 2. Yeni veri yolu

```text
CU8 dosyası → replay_cu8 → IqChunk → detect_frames → ModeSFrame
                                                        ↓
                                              DecoderPipeline / CRC
                                                        ↓
                                             DecodedEvent / JSONL
                                                        ↓
                                           forward → POST /api/events
                                                        ↓
                                              Store → WebSocket → UI
```

FastAPI ve IQ üreticisi bu kullanımda ayrı süreçlerdir. Dosya okuma/DSP işlemi,
sunucunun event loop'unu meşgul etmez. Mevcut `--fixture` ve `--input` yolları da
korundu; yeni `--iq` bunlardan bağımsızdır.

## 3. Çalıştırma

Repo kökünde:

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m uvicorn netradar.backend.server:app \
  --host 127.0.0.1 --port 8000 --workers 1
```

Başka terminalde:

```bash
.venv/bin/python -m netradar.pipeline \
  --iq fixtures/synthetic_adsb_2msps.cu8 --source synthetic \
  | .venv/bin/python -m netradar.backend.forward
```

Sadece DSP/decoder çıktısını görmek için son pipe kaldırılabilir. Her stdout
satırı tam bir `aircraft_update` olur; grafik/istatistik çıktısı JSONL'e karışmaz.
Bu sentetik IQ testidir; antene erişmez ve fixture metadata'sındaki hazır
hex/örnek indekslerini okumaz. Dosyanın tüm baytları parça parça taranır.

Gerçek kaydedilmiş 2 MS/s CU8 için:

```bash
.venv/bin/python -m netradar.pipeline --iq kayit.cu8 --source replay \
  | .venv/bin/python -m netradar.backend.forward
```

Arayüzde `http://localhost:8000` backend adresiyle **Canlı alıcı** seçilir.
Fixture uçakları İstanbul dışında bulunabilir; uçak listesinden seçip haritada
odaklanılabilir. Dosya bittiğinde yeni ölçüm gelmez; konumların eskimesi doğrudur.

## 4. Adım adım DSP

1. **Parçalama:** `replay_cu8()` varsayılan olarak 8192 kompleks örnek, yani 16384
   bayt okur. İndeksler bayt değil I/Q çifti cinsindendir: `0, 8192, 16384, …`.
   Son kısa parça da teslim edilir. Tek kalan I/Q baytı bozuk dosya olarak reddedilir.
2. **Güç hesabı:** I/Q baytları float'a çevrilir, 127.5 ofseti çıkarılır.
   `power = (I - 127.5)² + (Q - 127.5)²` hesaplanır. Matplotlib gerekmez.
3. **Preamble:** Mevcut 2 MS/s modeli, `[0, 2, 7, 9]` darbe konumları ve sessiz
   örneklerle karşılaştırma korunur. Yakın önceki bir yanlış aday gerçek başlangıcı
   bastırmaz; adayın doğruluğu CRC aşamasında kesinleşir.
4. **PPM:** 16 örneklik preamble sonrası 112 bit için ikişer örnek karşılaştırılır.
   İlk yarı güçlüyse 1, ikinci yarı güçlüyse 0 olur. Eşit/kararsız bitli aday
   atlanır. Bitler MSB-first 14 bayta paketlenir.
5. **Sözleşme:** DF17 adayından uppercase hex, preamble'ın mutlak örnek indeksi,
   kaynak ve varsa gerçek zamanla `ModeSFrame` üretilir.
6. **Decoder:** CRC, ICAO, çağrı adı, hız ve CPR çözümü mevcut decoder'da kalır.
   DSP'den geçen bozuk CRC'li aday backend'e yayınlanmaz.

NumPy işlemleri için referanslar:
[frombuffer](https://numpy.org/doc/2.2/reference/generated/numpy.frombuffer.html),
[packbits](https://numpy.org/doc/2.2/reference/generated/numpy.packbits.html).

## 5. Parça sınırı neden önemli?

Bir uzun mesaj 240 kompleks örnektir. Mesajın ilk 100 örneği bir parçada, kalan
140 örneği sonraki parçada olabilir. Her parçayı tek başına tararsak mesaj kaybolur.

Bu nedenle son **239 örnek** tutulup sonraki parçanın önüne eklenir. Önceki
taramada tamamlanmış başlangıçlar bu kuyrukta kalmaz; aynı mesaj iki kere çıkmaz.
1 örneklik çok küçük parçalarla bölünmüş mesajlar da tamamlanana kadar birikir.

Örnek indeksinde boşluk varsa eski kuyruk silinir; arada eksik veri varken iki
yarım mesaj birleştirilmez. Örtüşen/geriye giden parçalar açık hatadır. Üretici
kapatıldığında kaynak iterator'ü de kapatılıp açık dosya bırakılmaz.

## 6. Zaman ve kayıt kimliği

CU8 baytları tarih taşımaz. Varsayılan replay'de `observed_at_unix_ms=None` kalır;
dosyanın değiştirilme zamanı veya çalıştırma saati gözlem zamanı yapılmaz.
Örnek sırası decoder'ın mevcut `capture_id` bilgisiyle birlikte taşınır.

Kaydın sıfırıncı örneğinin zamanı gerçekten ölçülmüşse
`--capture-start-unix-ms` verilebilir. Hesap:

```text
frame_time_ms = capture_start_unix_ms + sample_index × 1000 // 2_000_000
```

Parça sınırını geçen mesajda da zaman, preamble başlangıcına aittir. Birbirinden
bağımsız iki tarihsiz kaydın örnekleri karşılaştırılamaz. Böyle kayıtlar arasında
backend yeniden başlatılıp yeni store kullanılmalıdır; replay'i döndürüp eski
örnekleri tekrar güncel konum gibi sunmuyoruz.

## 7. Doğrulama ve mevcut sınır

`tests/test_dsp.py` gerçek fixture baytlarından yedi mesajı ve tam örnek indekslerini
doğrular. Ek kontroller: 239 olası bölünme, tek örneklik parçalar, ardışık mesajlar,
kesik son mesaj, boş/gürültülü giriş, boşluk/örtüşme, farklı çalışma dizini,
yanlış örnekleme hızı, CRC reddi, kaynak kapatma ve zamanın korunması.

FastAPI entegrasyon testi gerçek DSP çıktısını `TrafficService.handle_frame()`
üzerinden WebSocket'e aktarır. Ayrıca Uvicorn üzerinde gerçek HTTP/WS bağlantısıyla
`CU8 → DSP → decoder → forward → FastAPI → WS` zinciri doğrulandı: yedi tam olay,
beş uçak özeti, iki çözülmüş CPR konumu; ölçüm zamanı uydurulmadı.

```bash
.venv/bin/python -m unittest discover -s tests -q
```

Bu temel DSP **2 MS/s, 1090 MHz, CU8 ve 112-bit DF17** ile sınırlıdır. Farklı
örnekleme hızları sessizce yorumlanmaz; resampling yoktur. Preamble eşikleri
deneyseldir; çakışma, zamanlama/frekans kayması ve zayıf gerçek RF için gelişmiş
iyileştirme yapılmadı. `stream_rtlsdr()` hâlâ donanım adaptörü olarak eksiktir;
canlı anten alımı doğrulanmış değildir. DSP mevcut `Iterable[IqChunk]` sözleşmesi
sayesinde bu kaynak hazır olduğunda aynı decoder hattını kullanabilir.

Repodaki kaynağı belirsiz 20 MB `adsb.iq` dosyası da yerel olarak tarandı; mevcut
eşiklerle DF17 adayı/CRC-geçerli olay çıkmadı. Bu sonuç dosyada kesinlikle uçak
olmadığını veya gerçek RF'de başarı elde edildiğini kanıtlamaz. Dosya değiştirilmedi.
