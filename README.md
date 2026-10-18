# NetRadar -- Team Starter Kit (6-hour sprint)

An **educational** starter kit for a Raspberry Pi 4 + RTL2832U/R820T ADS-B receiver.
This kit provides **contracts, real example DF17 frames, deterministic synthetic IQ,
mock backend events, and tests**. It does NOT contain a complete decoder or a real RF recording.
Each teammate must implement their own module and demonstrate it against shared fixtures.

The decoder implementation now covers DF17 identification, airborne position
(barometric and GNSS height), and velocity subtypes 1-4. See
[decoder coverage and field meanings](docs/DECODER.md) for supported encodings,
integration details and remaining scope. Other pipeline modules are still starter
interfaces, except the aircraft state/delivery adapter described below.

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
receives every full update. HTTP/WebSocket hosting and live SDR acquisition are
not started by this adapter.

## 0. Quick start (Mac, Linux, Raspberry Pi; Python 3.10+)

```bash
python3 -m unittest discover -s tests -v
python3 tools/verify_fixtures.py
```

No `pip install` is needed to inspect the fixtures or run the baseline tests.
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
