import json
from pathlib import Path
import unittest

from netradar.contracts import IqChunk, ModeSFrame, DecodedEvent
from tools.verify_fixtures import crc24, verify

ROOT=Path(__file__).resolve().parents[1]


class BaselineTest(unittest.TestCase):
    def test_fixture_oracles(self):
        verify()

    def test_iq_chunk_samples(self):
        c=IqChunk(data=b'\x7f\x80\x80\x81',start_sample_index=4)
        self.assertEqual(c.sample_count,2)
        with self.assertRaises(ValueError):
            IqChunk(data=b'\x7f',start_sample_index=0)

    def test_frame(self):
        f=ModeSFrame(hex='8D40621D58C382D690C8AC2863A7',sample_index=10,source='synthetic')
        self.assertEqual(f.sample_index,10)
        with self.assertRaises(ValueError):
            ModeSFrame(hex='not-a-frame',sample_index=0)

    def test_events(self):
        d=DecodedEvent(icao='40621D',event_type='airborne_position',fields={'latitude':None})
        self.assertIsNone(d.fields['latitude'])

    def test_mock_event_format(self):
        lines=(ROOT/'fixtures'/'aircraft_updates.mock.jsonl').read_text().splitlines()
        self.assertEqual(len(lines),3)
        for line in lines:
            event=json.loads(line)
            self.assertEqual(event['type'],'aircraft_update')
            self.assertIn('icao',event['data'])
            self.assertIn('latitude',event['data'])
            self.assertEqual(event['schema_version'],'1.1')
            self.assertIsNone(event['event'])  # Fictional UI demo, no RF provenance.
            self.assertIsNotNone(event['data']['track_deg'])
            self.assertIsNone(event['data']['heading_deg'])


if __name__=='__main__':
    unittest.main()
