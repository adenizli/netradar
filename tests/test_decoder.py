import json
from pathlib import Path
import unittest

from netradar.contracts import ModeSFrame
from netradar.decoder.decode import parse_header


ROOT = Path(__file__).resolve().parents[1]


class HeaderTest(unittest.TestCase):
    def test_published_example_headers(self):
        examples = json.loads((ROOT / "fixtures" / "frames.json").read_text())
        for example in examples["frames"]:
            with self.subTest(frame=example["hex"]):
                frame = ModeSFrame(hex=example["hex"], sample_index=0)
                header = parse_header(frame)
                self.assertEqual(header.df, example["df"])
                self.assertEqual(header.icao, example["icao"])
                self.assertEqual(header.type_code, example["type_code"])

    def test_unsupported_format_preserves_leading_zero_bits(self):
        # A parsing-only input, not a CRC-valid ADS-B example.
        frame = ModeSFrame(hex="0D4840D6202CC371C32CE0576098", sample_index=0)
        header = parse_header(frame)
        self.assertEqual(header.df, 1)
        self.assertIsNone(header.icao)
        self.assertIsNone(header.type_code)


if __name__ == "__main__":
    unittest.main()
