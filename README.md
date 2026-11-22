# NetRadar -- Team Starter Kit (6-hour sprint)

An **educational** starter kit for a Raspberry Pi 4 + RTL2832U/R820T ADS-B receiver.
This kit provides **contracts, real example DF17 frames, deterministic synthetic IQ,
mock backend events, and tests**. It does not yet provide a complete live hardware
receiver or a verified real RF recording. Modules are demonstrated against shared fixtures.

The decoder implementation now covers DF17 identification, airborne position
(barometric and GNSS height), and velocity subtypes 1-4. See
[decoder coverage and field meanings](docs/DECODER.md) for supported encodings,
integration details and remaining scope. CU8 file replay, a basic streaming 2 MS/s
DSP detector, aircraft state/delivery, FastAPI transport and desktop UI are now
implemented. Live RTL-SDR acquisition is connected to the same pipeline;
on-device real-air validation still requires the Raspberry Pi and dongle.

## Tek komutla başlatma (Mac + Raspberry Pi)

Mac üzerinde NetRadar repo klasöründe:

```bash
./start.sh
```

Mac kopyası `main` branch'indeyse, yerel değişiklik yoksa ve GitHub'dan ayrışmamışsa başlatıcı önce onu GitHub'daki `main`'e otomatik günceller (`git pull --ff-only`). Başka bir branch'te veya yerel değişiklik varken hiçbir şeye dokunmaz.

Başlatıcı Pi'ye SSH ile bağlanır (varsayılan `netradar@netradar.local`); gerekirse Pi'deki repoyu klonlar/günceller ve Python bağımlılıklarını kurar. Pi üzerinde **RTL-SDR + FastAPI** başlar. Mac'te **SSH port tüneli** (`127.0.0.1:8000`) ve Vite (`127.0.0.1:5173`) açılır. Tarayıcı otomatik açılır ve arayüz doğrudan canlı alıcıya bağlanır (demo modu yoktur). **Ctrl+C** yalnızca bu oturumda başlatılan servisleri kapatır.

İlk çalıştırma için Mac'te Node.js **22.12+ veya 24+**, npm, Python 3, SSH ve Pi'ye erişim; Pi'de Python 3, `rtl_sdr`, `curl`, `setsid` ve bağlı dongle gerekir. Backend Pi üzerinde yalnızca localhost'a bağlanır; LAN'a açık API portu gerekmiyor. SSH ve frontend işlemleri Mac üzerinde çalışır.

```bash
NETRADAR_PI_TARGET=netradar@192.168.0.120 ./start.sh  # IP ile SSH
NETRADAR_GAIN_DB=33.8 ./start.sh                      # Alternatif alıcı kazancı
```

Pi repo konumu varsayılan `~/netradar` (`NETRADAR_PI_DIR` ile değiştirilebilir). Mac'te 8000 ve 5173 portları boş olmalı. Pi'deki mevcut değişiklikler `git pull --ff-only` işlemini engellerse silinmez; önce ilgili değişiklikleri düzenleyin. Daha ayrıntılı yöntem için [DSP entegrasyon](docs/DSP_INTEGRATION.md) belgesine bakın.

### Pi'de sürekli çalışma ve GitHub'dan otomatik güncelleme

Pi'ye **bir kez** SSH ile girip repo klasöründe (`~/netradar`, `main` branch'i):

```bash
bash scripts/pi_install_service.sh
```

Bu komut iki systemd *user* servisi kurar ve açılışta başlatır (gerekirse `sudo loginctl enable-linger` için şifre sorar):

- `netradar.service`: RTL-SDR + FastAPI sürekli çalışır. Çökerse veya dongle takılı değilse 15 saniyede bir yeniden dener.
- `netradar-update.timer`: 2 dakikada bir GitHub'daki `main`'e bakar. Yeni commit varsa `git pull --ff-only` yapar, `requirements*.txt` değiştiyse bağımlılıkları kurar ve servisi yeniden başlatır. Sadece `docs/`, `frontend/`, `tests/`, `fixtures/` veya `.md` değiştiyse alıcı yeniden başlatılmaz.
- Yeni sürüm 2 dakika içinde `/healthz`'e cevap vermezse (veya hemen çökerse) **önceki commit'e geri döner** ve o commit'i tekrar denemez. Bir sonraki commit normal şekilde denenir. Güncellemeden önce de sistem sağlıklı değilse (ör. dongle takılı değil) geri dönüş yapılmaz.
- Pi'de commit edilmemiş değişiklik varsa, Pi başka bir branch'teyse veya `main` ayrışmışsa güncelleme atlanır; hiçbir şey silinmez.

Servis kuruluyken Mac'te `./start.sh` Pi'deki servise bağlanır, Pi'de ikinci bir alıcı başlatmaz. Ctrl+C sadece Mac tarafını kapatır, Pi çalışmaya devam eder. Servis modunda gain gibi ayarlar Pi'deki `~/.config/netradar.env` dosyasından okunur (ör. `NETRADAR_GAIN_DB=33.8`).

Kurulu systemd servis tanımına yeni kapanış sırasını uygulamak için Pi'de güncel
repodan `bash scripts/pi_install_service.sh` komutunu tekrar çalıştırın. Bir
sonraki servis durdurmasında önce bekleyen uçak olayları işlenir, ardından FastAPI kapanır.

```bash
bash scripts/pi_install_service.sh --status     # servis, timer, son güncelleme logları
journalctl --user -u netradar -f                # alıcı logları
journalctl --user -u netradar-update            # güncelleme geçmişi
systemctl --user start netradar-update.service  # beklemeden şimdi güncelle
bash scripts/pi_install_service.sh --uninstall  # eski (sadece start.sh) moda dön
```

> Not: `main`'e push edilen her şey birkaç dakika içinde Pi'de çalışır. Bu yüzden `main`'e sadece PR ile ve testler geçtikten sonra merge edin.

### Elektrik kesintisinden sonra kendiliğinden açılma ve olay kaydı

Aynı kurulum komutu (`bash scripts/pi_install_service.sh`, sudo şifresini bir kez sorar) Pi'yi elektrik gidip gelince kendi kendine ayağa kalkacak hale getirir:

- Elektrik geri gelince Pi 4 kendiliğinden açılır. Linger sayesinde alıcı ve otomatik güncelleme kimse giriş yapmadan başlar. Dongle hazır değilse alıcı 15 saniyede bir yeniden dener.
- **Donanım watchdog'u**: sistem donarsa Pi ~15 saniye içinde kendiliğinden yeniden başlar (`NETRADAR_NO_WATCHDOG=1` ile atlanır).
- **Kalıcı journal** (en fazla 100 MB): sistem logları elektrik kesintisinde silinmez.
- Kurulum sonunda açılış güvenliği kontrol edilir ve raporlanır: `fsck.repair=yes` (kesintiden sonra dosya sistemi onarımı), saat senkronizasyonu, ağ profillerinin otomatik bağlanması. Bunlar otomatik değiştirilmez, eksikse ne yapılacağı yazılır.
- **`netradar-monitor.service`** (sistem servisi, sadece standart Python): olayları `~/netradar-logs/` klasörüne yazar.

| Dosya | İçerik |
|---|---|
| `olaylar.log` | Her olay bir satır, Türkçe: `2026-11-22 20:14:22  ⚡ ELEKTRİK KESİNTİSİ ... kapalı kalma ≈ 13 dk` |
| `rapor.md` | Son 30 günün özeti (kesinti sayıları, toplam süreler) ve son 40 olay |
| `events.jsonl` | Aynı olaylar, makine için |

Kaydedilen olaylar:

- **⚡ Elektrik kesintisi veya zorla kapanma**: Pi düzgün kapanmadan durduysa (elektrik gitti, fiş çekildi, adaptör sorunu veya donma; yazılım bunları birbirinden ayıramaz), açıldıktan sonra son sinyal saati, açılış saati ve kapalı kalma süresi yazılır. Açılışta modem çalışıyorsa "muhtemelen sadece Pi'nin elektriği kesildi", internet ancak birkaç dakika sonra geldiyse "büyük ihtimalle binanın elektriği kesildi" notu eklenir.
- **🔌 Düzgün kapatma / yeniden başlatma**: kim yaptı (`sudo reboot` çalıştıran kullanıcı, güç düğmesi, otomatik sistem güncellemesi) ve ne kadar kapalı kaldı.
- **🌐 İnternet**: ağ bağlantısı yok / modeme ulaşılamıyor / modem açık ama internet yok / DNS çalışmıyor; ne zaman gitti, ne zaman geldi, ne kadar sürdü. Bir kesinti için art arda iki kötü kontrol (≈1 dk) gerekir.
- **🪫 Düşük voltaj** ve **🌡️ işlemci yavaşlatması** (Pi firmware'inin bildirdiği): zayıf adaptörün habercisi.
- **📡 Alıcı**: servis durdu, IQ gelmiyor (dongle takılı mı?), yazılım yanıt vermiyor; ve geri gelmesi.

Pi'de pil/RTC olmadığı için elektrik kesintisinden sonra saat, internetten senkronize olana kadar yanlıştır. İzleyici bu süredeki olayları bekletir ve saat düzeldiğinde doğru zamanla yazar. 15 dakika içinde senkron olmazsa "saat doğrulanamadı" notuyla yazar.

```bash
bash scripts/pi_install_service.sh --events   # Pi'de rapor
./start.sh --events                           # Mac'ten rapor (SSH ile)
tail -f ~/netradar-logs/olaylar.log           # Pi'de canlı izle
```

## Decoder to backend delivery

Every decoder event is now delivered to `AircraftStore` by `DecoderPipeline`.
Updates retain all decoded fields, raw hex and capture metadata; ground/air speed
and barometric/GNSS height remain separate. See [contracts v1.1](docs/CONTRACTS.md).

Run a decoder/backend demo using the known frames and offsets in the synthetic
fixture metadata (this bypasses IQ acquisition and detection):

```bash
python3 -m netradar.pipeline --fixture fixtures/synthetic_adsb_2msps.json
```

Each stdout line is a complete `aircraft_update` JSON object suitable for piping
to another process. For real/replayed DSP frames, pass a JSONL file or pipe stdin:

```bash
python3 -m netradar.pipeline --input frames.jsonl --sample-rate-hz 2000000
```

Each input line must contain `hex`, `sample_index`, and optionally `source` from
`ModeSFrame`. Keep one capture per invocation. In Python, use
`DecoderPipeline(publish=callback).handle_frame(frame)`; a separate backend process
can receive `event.to_json()` via `AircraftStore.apply_json()`. The optional callback
receives every full update. This adapter itself does not start HTTP/WS or SDR
acquisition. The separate FastAPI service below hosts the live transport.

## FastAPI live backend

`GET /api/aircraft` serves the current snapshot; `WS /ws/aircraft` streams full
updates. Producers send decoded events to `POST /api/events`. Position freshness
and all decoder fields are preserved. Run from the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m uvicorn netradar.backend.server:app --host 127.0.0.1 --port 8000 --workers 1
```

In another terminal, send the seven known synthetic-fixture events:

```bash
.venv/bin/python -m netradar.pipeline --fixture fixtures/synthetic_adsb_2msps.json \
  | .venv/bin/python -m netradar.backend.forward
```

Open the UI; it connects to the backend at `http://localhost:8000` by default.
This in-memory first step uses one worker and starts empty; run the IQ producer
below in a separate process to feed DSP output. It does not start live SDR acquisition.
See the [step-by-step backend guide](docs/LIVE_BACKEND.md) for architecture,
same-process integration, origin configuration, tests and current limits.

## IQ through DSP to the live backend

The `--iq` path reads actual CU8 bytes, finds preambles, demodulates 112 PPM bits,
and hands DF17 candidates to the CRC-validating decoder. It does not read the
fixture's expected frame list. With the FastAPI service running:

```bash
.venv/bin/python -m pip install -r requirements-dsp.txt
.venv/bin/python -m netradar.pipeline \
  --iq fixtures/synthetic_adsb_2msps.cu8 --source synthetic \
  | .venv/bin/python -m netradar.backend.forward
```

This detects the fixture's seven messages at their exact sample indices, then
publishes them through HTTP/WS. The detector preserves frames crossing chunk
boundaries and currently supports exactly 2 MS/s, 1090 MHz CU8 input. Raw replay
does not acquire an invented observation timestamp. See the
[DSP integration walkthrough](docs/DSP_INTEGRATION.md) for details and limits.

## Live RTL-SDR through DSP to FastAPI

With `rtl-sdr` installed and the dongle available, start the backend as above,
then run one live producer from the repository root:

```bash
.venv/bin/python -m netradar.pipeline --rtl-sdr \
  --gain-db 35 --backend-url http://127.0.0.1:8000
```

This connects `rtl_sdr` -> bounded IQ acquisition -> streaming DSP -> CRC/decoder
-> FastAPI -> WebSocket. Use `--gain-db auto`, `--ppm`, or `--device-index` as
needed; `--max-samples 10000000` limits a hardware check to five seconds at 2 MS/s.
Ctrl+C closes the producer and releases the dongle. Errors and received/emitted/
dropped sample counters go to stderr. Without `--backend-url`, stdout stays JSONL.
See [live setup, replay metadata and failure behavior](docs/DSP_INTEGRATION.md).
For stage counters, sampled latency and ICAO windows, enable
`--metrics-json /tmp/netradar-rf.json`. See the [loss-funnel report](docs/METRICS.md).

## Desktop radar interface

The React + TypeScript + Leaflet interface is now implemented in `frontend/`.
It includes a green receiver-centered radar map, directional aircraft markers,
full decoder-event details, multiple radius zones and entry/exit notifications.
It opens directly on the live adapter, which connects to the FastAPI HTTP/WS
backend; there is no demo traffic. The compact desktop layout keeps
aircraft, map and zone controls together without a navigation rail or file uploader. See [frontend setup and integration](frontend/README.md).

```bash
cd frontend
npm ci
npm run dev
# http://127.0.0.1:5173
```

## 0. Quick start (Mac, Linux, Raspberry Pi; Python 3.10+)

```bash
python3 -m unittest discover -s tests -v
python3 tools/verify_fixtures.py
```

No `pip install` is needed to inspect the fixtures or run the baseline tests.
HTTP/WebSocket and IQ/DSP integration tests require `requirements-dev.txt`;
without optional dependencies the corresponding tests are explicitly skipped.
Use `.venv/bin/python` for the full suite after installing development dependencies.
To regenerate the synthetic RF-like baseband capture:

```bash
python3 tools/generate_synthetic_iq.py
```

The generated file `fixtures/synthetic_adsb_2msps.cu8` is a **baseband IQ simulation**,
NOT a recording from the air, NOT a raw 1090 MHz RF waveform, and NOT a transmitter input.
The payload frame bytes were taken from documented real ADS-B example messages.

## 1. Signal path

```
RTL-SDR   ->   CU8 IQ   ->   DSP   ->   ModeSFrame  -> Protocol decoder
                                                              |
                                                         DecodedEvent
                                                              v
                                                           Backend
                                                              |
                                                       WebSocket JSON
                                                              v
                                                           Frontend
```

First deploy as modular components on the same Pi; only split into separate processes
or hosts when useful. The frontend can run on a teammate's laptop during development.

## 3. Fixtures

- `fixtures/frames.json`: documented real example ADS-B messages, CRC checked; includes two even/odd pairs.
- `fixtures/frames_bad.json`: deliberately CRC-corrupted negative fixture.
- `fixtures/synthetic_adsb_2msps.cu8`: 0.28s (560000 complex samples, 1120000 bytes) of idealized noisy unsigned IQ.
- `fixtures/synthetic_adsb_2msps.json`: capture parameters, ground-truth preamble sample indices.
- `fixtures/aircraft_updates.mock.jsonl`: **entirely fictional** Istanbul radar UI updates.

The synthetic file has simple, sample-aligned PPM pulses and additive artificial noise.
It does NOT simulate clock drift, multipath, collisions, difficult amplitude fading,
receiver gain dynamics, or overlapping messages. Passing this fixture alone is not
proof of a working real-air receiver.

## 4. Capture your own REAL RF samples on the Raspberry Pi

Requirements: RTL-SDR connected; `rtl_test` can access it, and no other process owns it.

```bash
sudo apt update
sudo apt install -y rtl-sdr
rtl_test                  # Ctrl+C to end the stream
bash scripts/capture_real.sh
```

The capture script creates `fixtures/real_1090_2msps.cu8` plus a matching JSON sidecar.
It records 5 seconds = 10 million complex samples = ~20 MB. SDR gain ~35 dB is a
starting value, not universally optimal. If the DVB kernel driver claimed the dongle,
see `docs/TROUBLESHOOTING.md` before changing any driver settings.

**Do not** claim this test RF recording has aircraft frames unless a verified decoder
actually detects CRC-valid messages. A live 5-second recording may have none.

## 5. Integration gate after ~2 hours

1. Acquisition produces IqChunk with correct metadata and reads from a saved file.
2. DSP returns ModeSFrame from fixture; correct hex AND correct sample index.
3. Decoder can reject the CRC-bad fixture and parse the positive fixtures.
4. Backend accepts a mock DecodedEvent and broadcasts an aircraft update.
5. Frontend renders `aircraft_updates.mock.jsonl` independently of backend.

Integrate continuously rather than doing one giant merge at hour six.

## 6. Reference material

- pyModeS documented message examples: https://github.com/junzis/pyModeS
- The 1090 MHz Riddle (protocol explanations): https://mode-s.org/1090mhz/
- Original dump1090 (cross-checking only): https://github.com/antirez/dump1090

The team writes its own acquisition/DSP/decoder modules. External decoders are useful
as **oracles**, but should not become the implementation of your decoder.
