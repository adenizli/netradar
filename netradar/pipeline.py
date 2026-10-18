"""IQ/DSP or supplied frames -> decoder -> backend, with JSONL delivery."""
import argparse
from collections.abc import Callable
import json
from pathlib import Path
import sys

from netradar.backend.state import AircraftStore
from netradar.contracts import ModeSFrame
from netradar.decoder.decode import ADSBDecoder


class DecoderPipeline:
    def __init__(
        self, *, decoder: ADSBDecoder | None = None,
        store: AircraftStore | None = None, publish: Callable[[dict], None] | None = None,
    ):
        self.decoder = decoder if decoder is not None else ADSBDecoder()
        self.store = store if store is not None else AircraftStore()
        self.publish = publish

    def handle_frame(self, frame: ModeSFrame) -> list[dict]:
        """Deliver every decoded event to the backend and then the publisher."""
        updates = []
        for event in self.decoder.decode(frame):
            update = self.store.apply(event)
            if self.publish is not None:
                self.publish(update)
            updates.append(update)
        return updates


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    inputs = parser.add_mutually_exclusive_group()
    inputs.add_argument("--fixture", type=Path, help="Synthetic metadata JSON with expected_frames; no IQ detection")
    inputs.add_argument("--input", type=Path, help="Frame JSONL file; defaults to stdin")
    inputs.add_argument("--iq", type=Path, help="Detect DF17 from CU8 bytes; no expected-frame metadata")
    parser.add_argument("--sample-rate-hz", type=int, default=None)
    parser.add_argument("--chunk-samples", type=int, default=8192, help="CU8 replay chunk size")
    parser.add_argument("--source", default="replay", help="Frame source label for --iq; use synthetic for fixtures")
    parser.add_argument("--capture-start-unix-ms", type=int, help="Measured time of IQ sample zero, only if known")
    args = parser.parse_args()
    if args.capture_start_unix_ms is not None and args.iq is None:
        parser.error("--capture-start-unix-ms applies only to --iq")

    try:
        if args.iq is not None:
            try:
                from netradar.acquisition.receiver import replay_cu8
                from netradar.dsp.detector import detect_frames
            except ModuleNotFoundError as exc:
                if exc.name != "numpy":
                    raise
                raise ValueError("IQ detection needs NumPy: install requirements-dsp.txt") from None
            sample_rate = args.sample_rate_hz if args.sample_rate_hz is not None else 2_000_000
            frames = detect_frames(
                replay_cu8(args.iq, chunk_samples=args.chunk_samples, sample_rate_hz=sample_rate),
                source=args.source, capture_start_unix_ms=args.capture_start_unix_ms,
            )
            stream = None
        elif args.fixture is not None:
            metadata = json.loads(args.fixture.read_text())
            if metadata.get("kind") != "synthetic_baseband_iq":
                raise ValueError("--fixture expects explicitly synthetic fixture metadata")
            sample_rate = metadata["sample_rate_hz"]
            if args.sample_rate_hz is not None and args.sample_rate_hz != sample_rate:
                raise ValueError("Sample rate does not match fixture metadata")
            frames = (
                ModeSFrame(hex=item["hex"], sample_index=item["sample_index"], source="synthetic")
                for item in metadata["expected_frames"]
            )
            stream = None
        else:
            sample_rate = args.sample_rate_hz if args.sample_rate_hz is not None else 2_000_000
            stream = args.input.open() if args.input is not None else sys.stdin
            frames = (ModeSFrame(**json.loads(line)) for line in stream if line.strip())

        def publish(update: dict) -> None:
            print(json.dumps(update, ensure_ascii=False, allow_nan=False), flush=True)

        pipeline = DecoderPipeline(
            decoder=ADSBDecoder(sample_rate_hz=sample_rate), publish=publish,
        )
        try:
            for frame in frames:
                pipeline.handle_frame(frame)
        finally:
            if hasattr(frames, "close"):
                frames.close()
            if stream is not None and stream is not sys.stdin:
                stream.close()
    except (ValueError, TypeError, KeyError, OSError) as exc:
        parser.exit(2, f"netradar.pipeline: {exc}\n")


if __name__ == "__main__":
    main()
