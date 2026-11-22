"""Compare queue configurations by replaying the same RF snippets at 2 MS/s."""
import argparse
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import resource
import subprocess
import sys
import tempfile
from threading import Thread
from time import monotonic, sleep

if len(sys.argv) > 1 and sys.argv[1] == 'emit':
    files = json.loads(os.environ['NETRADAR_BENCH_SNIPPETS'])
    began = monotonic()
    emitted = 0
    for name in files:
        with open(name, 'rb') as source:
            while data := source.read(262144):
                deadline = began + emitted / 4_000_000
                sleep(max(0, deadline - monotonic()))
                sys.stdout.buffer.write(data)
                sys.stdout.buffer.flush()
                emitted += len(data)
    sys.exit(0)

@contextmanager
def mock_api(delay):
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers['content-length']))
            sleep(delay)
            self.send_response(200)
            self.send_header('Content-Length', '2')
            self.end_headers()
            self.wfile.write(b'{}')
            self.server.posts += 1

        def log_message(self, *_):
            pass

    with HTTPServer(('127.0.0.1', 0), Handler) as api:
        api.posts = 0
        worker = Thread(target=api.serve_forever, kwargs={'poll_interval': 0.05}, daemon=True)
        worker.start()
        try:
            yield (f'http://127.0.0.1:{api.server_port}', api)
        finally:
            api.shutdown()
            worker.join()


def cpu():
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_stime + usage.ru_utime


def run(args, chunk, files, delay):
    # A fixed IQ queue memory budget for all chunk sizes.
    capacity = 524288 // chunk
    samples = sum(path.stat().st_size for path in files) // 2
    with tempfile.TemporaryDirectory() as dirname, mock_api(delay) as (url, api):
        tmp = Path(dirname)
        receiver = tmp / 'rtl-benchmark'
        receiver.write_text('#!/bin/sh\nexec "$NETRADAR_BENCH_PYTHON" "$NETRADAR_BENCH_DRIVER" emit\n')
        receiver.chmod(0o700)
        metrics = tmp / 'metrics.json'
        env = dict(os.environ, NETRADAR_BENCH_PYTHON=sys.executable,
                   NETRADAR_BENCH_DRIVER=str(Path(__file__).resolve()),
                   NETRADAR_BENCH_SNIPPETS=json.dumps([str(path) for path in files]))
        argv = [sys.executable, '-m', 'netradar.pipeline', '--rtl-sdr', '--rtl-sdr-command', str(receiver),
                '--source', 'synthetic', '--max-samples', str(samples), '--chunk-samples', str(chunk),
                '--queue-max-chunks', str(capacity), '--backend-url', url, '--metrics-json', str(metrics)]
        started = monotonic()
        cpu_start = cpu()
        process = subprocess.run(argv, env=env, cwd=args.root, text=True, capture_output=True,
                                 timeout=max(40, 60 + samples / 2_000_000))
        capture = json.loads(metrics.read_text())
        print(json.dumps({'code': args.code, 'chunk_samples': chunk, 'queue_capacity': capacity,
                          'mock_http_delay_ms': delay * 1000, 'returncode': process.returncode,
                          'wall_seconds': monotonic() - started, 'cpu_seconds': cpu() - cpu_start,
                          'backend_posts': api.posts, 'input_samples': samples,
                          'stages': {name: {'counters': stage['counters'], 'queue': stage.get('queue'),
                                           **{f'{key}_p95_ms': stage[key]['p95_ms']
                                              for key in stage if isinstance(stage[key], dict) and 'p95_ms' in stage[key]}}
                                     for name, stage in capture['stages'].items() if name != 'display'},
                          'producer_output_tail': process.stderr.splitlines()[-3:]}), flush=True)


parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('root', type=Path)
parser.add_argument('code')
parser.add_argument('snippets', type=Path)
parser.add_argument('--count', type=int, default=6)
parser.add_argument('--delays', type=float, nargs='+', default=[0, .3])
parser.add_argument('--chunks', type=int, nargs='+', default=[8192, 16384, 32768])
args = parser.parse_args()
files = sorted(args.snippets.glob('*.cu8'))[:args.count]
if not files:
    parser.error('No CU8 snippets found')
for delay in args.delays:
    for chunk in args.chunks:
        run(args, chunk, files, delay)
