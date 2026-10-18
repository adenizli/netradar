import contextlib
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from netradar.acquisition.receiver import StreamStats, _RateMonitor, replay_cu8, stream_rtlsdr

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'fixtures' / 'synthetic_adsb_2msps.cu8'
FIXTURE_META = json.loads((ROOT / 'fixtures' / 'synthetic_adsb_2msps.json').read_text())
TOTAL_SAMPLES = FIXTURE_META['total_complex_samples']

# Stand-in for the rtl_sdr CLI. Its own options come before "--", the rtl_sdr
# arguments after it; those are saved to --args-out so tests can check them.
FAKE_RTL_SDR = r'''
import argparse, sys, time
p = argparse.ArgumentParser()
p.add_argument('--fixture')
p.add_argument('--write-size', type=int, default=4096)
p.add_argument('--delay', type=float, default=0.0)
p.add_argument('--forever', action='store_true')
p.add_argument('--fail', action='store_true')
p.add_argument('--args-out')
argv = sys.argv[1:]
split = argv.index('--')
opts = p.parse_args(argv[:split])
rtl_args = argv[split + 1:]
if opts.args_out:
    with open(opts.args_out, 'w') as f:
        f.write('\n'.join(rtl_args))
if opts.fail:
    sys.stderr.write('Found 1 device(s):\nusb_claim_interface error -6\nFailed to open rtlsdr device #0.\n')
    sys.exit(1)
sys.stderr.write('Found 1 device(s):\nReading samples in async mode...\n')
data = open(opts.fixture, 'rb').read()
out = sys.stdout.buffer
try:
    while True:
        for i in range(0, len(data), opts.write_size):
            out.write(data[i:i + opts.write_size])
            out.flush()
            if opts.delay:
                time.sleep(opts.delay)
        if not opts.forever:
            break
except (BrokenPipeError, OSError):
    pass
'''


def collect(chunks):
    return [(c.start_sample_index, c.data) for c in chunks]


class ReplayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def assert_contiguous(self, chunks, total):
        expected = 0
        for c in chunks:
            self.assertEqual(c.start_sample_index, expected)
            self.assertEqual(len(c.data) % 2, 0)
            expected += c.sample_count
        self.assertEqual(expected, total)

    def test_fixture_byte_identical_for_many_chunk_sizes(self):
        raw = FIXTURE.read_bytes()
        for size in (1, 1000, 8191, 8192, 1_000_000):
            with self.subTest(chunk_samples=size):
                chunks = list(replay_cu8(FIXTURE, chunk_samples=size))
                self.assert_contiguous(chunks, TOTAL_SAMPLES)
                self.assertEqual(b''.join(c.data for c in chunks), raw)
                self.assertTrue(all(c.sample_count == size for c in chunks[:-1]))

    def test_first_preamble_sample(self):
        index = FIXTURE_META['preamble_start_sample_indices'][0]
        self.assertEqual(index, 10000)
        for size in (1000, 8192, 7):
            with self.subTest(chunk_samples=size):
                for c in replay_cu8(FIXTURE, chunk_samples=size):
                    if c.start_sample_index <= index < c.start_sample_index + c.sample_count:
                        offset = 2 * (index - c.start_sample_index)
                        self.assertEqual((c.data[offset], c.data[offset + 1]), (222, 180))
                        break
                else:
                    self.fail('sample 10000 not found')

    def test_empty_file(self):
        path = self.dir / 'empty.cu8'
        path.write_bytes(b'')
        self.assertEqual(list(replay_cu8(path)), [])

    def test_odd_length_file_drops_last_byte(self):
        path = self.dir / 'odd.cu8'
        path.write_bytes(bytes(range(11)))
        with self.assertWarns(RuntimeWarning):
            chunks = list(replay_cu8(path, chunk_samples=2))
        self.assert_contiguous(chunks, 5)
        self.assertEqual(b''.join(c.data for c in chunks), bytes(range(10)))

    def test_single_byte_file(self):
        path = self.dir / 'one.cu8'
        path.write_bytes(b'\x7f')
        with self.assertWarns(RuntimeWarning):
            self.assertEqual(list(replay_cu8(path)), [])

    def test_invalid_arguments(self):
        for bad in (0, -1):
            with self.assertRaises(ValueError):
                replay_cu8(FIXTURE, chunk_samples=bad)
        with self.assertRaises(FileNotFoundError):
            replay_cu8(self.dir / 'missing.cu8')

    def test_metadata_defaults_sidecar_and_override(self):
        path = self.dir / 'cap.cu8'
        path.write_bytes(b'\x7f\x80' * 4)
        c = next(replay_cu8(path))
        self.assertEqual((c.sample_rate_hz, c.center_frequency_hz), (2_000_000, 1_090_000_000))

        path.with_suffix('.json').write_text(
            json.dumps({'sample_rate_hz': 2_400_000, 'center_frequency_hz': 1_089_000_000}))
        c = next(replay_cu8(path))
        self.assertEqual((c.sample_rate_hz, c.center_frequency_hz), (2_400_000, 1_089_000_000))

        c = next(replay_cu8(path, sample_rate_hz=2_000_000))
        self.assertEqual((c.sample_rate_hz, c.center_frequency_hz), (2_000_000, 1_089_000_000))
        c = next(replay_cu8(path, sample_rate_hz=1_000_000, center_frequency_hz=1_090_000_000))
        self.assertEqual((c.sample_rate_hz, c.center_frequency_hz), (1_000_000, 1_090_000_000))

    def test_fixture_sidecar(self):
        c = next(replay_cu8(FIXTURE))
        self.assertEqual(c.sample_rate_hz, FIXTURE_META['sample_rate_hz'])
        self.assertEqual(c.center_frequency_hz, FIXTURE_META['center_frequency_hz'])


class StreamTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.fake = self.dir / 'fake_rtl_sdr.py'
        self.fake.write_text(FAKE_RTL_SDR)
        self.args_out = self.dir / 'args.txt'

    def tearDown(self):
        self.tmp.cleanup()

    def fake_cmd(self, *opts):
        return [sys.executable, str(self.fake), '--fixture', str(FIXTURE),
                '--args-out', str(self.args_out), *opts, '--']

    def assert_no_reader_threads(self):
        names = [t.name for t in threading.enumerate()]
        self.assertFalse([n for n in names if n.startswith('netradar-rtl')], names)

    def test_stream_matches_replay(self):
        stats = StreamStats()
        # Odd-sized writes force short reads on the pipe.
        streamed = collect(stream_rtlsdr(rtl_sdr_cmd=self.fake_cmd('--write-size', '999'),
                                         chunk_samples=8192, stats=stats))
        self.assertEqual(streamed, collect(replay_cu8(FIXTURE, chunk_samples=8192)))
        self.assertEqual(stats.samples_emitted, TOTAL_SAMPLES)
        self.assertEqual(stats.samples_received, TOTAL_SAMPLES)
        self.assertEqual(stats.dropped_samples, 0)
        self.assertEqual(stats.chunks_emitted, len(streamed))
        self.assertEqual(stats.returncode, 0)
        self.assertEqual(self.args_out.read_text().split('\n'),
                         ['-f', '1090000000', '-s', '2000000', '-g', '35', '-p', '0', '-d', '0', '-'])
        self.assert_no_reader_threads()

    def test_command_line_options(self):
        chunks = list(stream_rtlsdr(rtl_sdr_cmd=self.fake_cmd(), gain_db=None, ppm=-3,
                                    device_index=1, sample_rate_hz=2_400_000,
                                    max_samples=100_001, chunk_samples=4096))
        self.assertEqual(self.args_out.read_text().split('\n'),
                         ['-f', '1090000000', '-s', '2400000', '-g', '0', '-p', '-3', '-d', '1',
                          '-n', '100001', '-'])
        self.assertEqual(sum(c.sample_count for c in chunks), 100_001)
        self.assertTrue(all(c.sample_rate_hz == 2_400_000 for c in chunks))
        self.assert_no_reader_threads()

    def test_startup_failure_raises_with_stderr(self):
        with self.assertRaises(RuntimeError) as ctx, \
                self.assertLogs('netradar.acquisition.receiver', level='WARNING') as logs:
            list(stream_rtlsdr(rtl_sdr_cmd=self.fake_cmd('--fail')))
        self.assertIn('usb_claim_interface', '\n'.join(logs.output))
        message = str(ctx.exception)
        self.assertIn('usb_claim_interface error -6', message)
        self.assertIn('TROUBLESHOOTING', message)
        self.assert_no_reader_threads()

    def test_missing_executable(self):
        with self.assertRaises(RuntimeError):
            next(stream_rtlsdr(rtl_sdr_cmd='netradar-no-such-rtl-sdr-binary'))

    def test_invalid_arguments(self):
        for kwargs in ({'chunk_samples': 0}, {'max_samples': 0}, {'queue_max_chunks': 0}):
            with self.subTest(**kwargs), self.assertRaises(ValueError):
                stream_rtlsdr(**kwargs)

    def test_close_early_terminates_process(self):
        stats = StreamStats()
        gen = stream_rtlsdr(rtl_sdr_cmd=self.fake_cmd('--forever', '--delay', '0.001'), stats=stats)
        with contextlib.closing(gen):
            for _ in range(3):
                next(gen)
            self.assertIsNotNone(stats.pid)
            self.assertIsNone(stats.returncode)
        self.assertIsNotNone(stats.returncode)  # process exited and was reaped
        self.assert_no_reader_threads()

    def test_overflow_drops_and_reports_gap(self):
        stats = StreamStats()
        raw = FIXTURE.read_bytes()
        chunks = []
        # The fake writes ~16k samples every 10 ms; the consumer takes 50 ms per chunk.
        with self.assertLogs('netradar.acquisition.receiver', level='WARNING'):
            for c in stream_rtlsdr(rtl_sdr_cmd=self.fake_cmd('--write-size', '32768', '--delay', '0.01'),
                                   chunk_samples=16384, queue_max_chunks=2, stats=stats):
                chunks.append(c)
                time.sleep(0.05)
        self.assertGreater(stats.dropped_samples, 0)
        self.assertEqual(stats.samples_received, TOTAL_SAMPLES)

        gaps = 0
        expected = 0
        for c in chunks:
            self.assertGreaterEqual(c.start_sample_index, expected)
            gaps += c.start_sample_index - expected
            expected = c.start_sample_index + c.sample_count
            # Indices still point at the right place in the original stream.
            self.assertEqual(c.data, raw[2 * c.start_sample_index:2 * expected])
        self.assertGreater(gaps, 0)
        trailing = stats.samples_received - expected
        self.assertEqual(gaps + trailing, stats.dropped_samples)
        self.assertEqual(stats.samples_emitted + stats.dropped_samples, stats.samples_received)


class RateMonitorTest(unittest.TestCase):
    def test_reports_deficit_after_warmup_only(self):
        now = [0.0]
        mon = _RateMonitor(2_000_000, warmup_s=1.0, slack_samples=0, interval_s=5.0, clock=lambda: now[0])
        self.assertIsNone(mon.update(0))
        now[0] = 0.5
        self.assertIsNone(mon.update(10))  # still warming up
        now[0] = 2.0
        self.assertIsNone(mon.update(4_000_000))  # on time
        now[0] = 3.0
        self.assertAlmostEqual(mon.update(5_400_000), 0.1)  # 10 % short
        now[0] = 4.0
        self.assertIsNone(mon.update(5_500_000))  # rate-limited
        now[0] = 9.0
        self.assertIsNotNone(mon.update(5_500_000))


if __name__ == '__main__':
    unittest.main()
