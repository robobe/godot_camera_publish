"""Real NVIDIA Godot -> Zenoh SHM integration. Run with .venv/bin/python."""
import os
from pathlib import Path
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from viewer import FrameReceiver

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, '__NV_PRIME_RENDER_OFFLOAD': '1', '__GLX_VENDOR_LIBRARY_NAME': 'nvidia'}


def start(name, frames):
    log = (ROOT / 'build' / name).open('w')
    process = subprocess.Popen([str(Path.home() / '.local/bin/godot'), '--path', str(ROOT / 'demo'),
                                '--disable-vsync', '--quit-after', str(frames)],
                               env=ENV, stdout=log, stderr=subprocess.STDOUT)
    return process, log


def collect(receivers, seconds):
    end = time.monotonic() + seconds
    frames = [{} for _ in receivers]
    while time.monotonic() < end:
        for receiver, result in zip(receivers, frames):
            frame = receiver.poll()
            if frame is not None:
                result[receiver.frame_id] = frame
                if len(result) > 60:
                    del result[min(result)]
        time.sleep(.003)
    return frames


def main():
    receivers = [FrameReceiver(i) for i in (1, 1, 2, 3)]
    process = log = None
    try:
        assert all(r.poll() is None for r in receivers), 'Subscriber-first must be nonblocking'
        process, log = start('integration-zenoh.log', 600)
        results = collect(receivers, 12)
        assert all(len(r) >= 30 for r in results), [len(r) for r in results]
        common = set(results[0]).intersection(*results[1:])
        assert common, 'No common render frame across cameras/subscribers'
        frame_id = max(common)
        assert np.array_equal(results[0][frame_id], results[1][frame_id]), 'Fanout changed pixels'
        assert all(not np.array_equal(results[0][frame_id], results[i][frame_id]) for i in (2, 3)), 'Offsets must produce distinct views'
        old_stream = receivers[0].stream_id
        before = receivers[1].frame_id
        collect(receivers[1:], 1)
        assert receivers[1].frame_id > before, 'Unpolled subscriber blocked the others'
        receivers[0].close()
        before = receivers[1].frame_id
        collect(receivers[1:], 1)
        assert receivers[1].frame_id > before, 'Closed subscriber blocked the others'
        # Retain actual SHM samples deliberately until the bounded publisher pool fills.
        held = []
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            sample = receivers[1].subscriber.try_recv()
            if sample is not None:
                assert sample.payload.as_shm() is not None
                held.append(sample)
            time.sleep(.003)
        assert 20 <= len(held) <= 36, f'Expected bounded pool, retained {len(held)} frames'
        assert any(
            int(line.split('Dropped: ')[1].split(' |')[0]) > 0
            for line in (ROOT / 'build/integration-zenoh.log').read_text().splitlines() if 'Dropped:' in line)
        held.clear()
        sample = None
        before = receivers[1].frame_id
        collect(receivers[1:], 1)
        assert receivers[1].frame_id > before, 'Pool did not recover after samples were released'
        process.wait(timeout=10)
        log.close()
        assert process.returncode == 0
        process, log = start('integration-zenoh-restart.log', 330)
        collect(receivers[1:], 10)
        assert all(r.stream_id != old_stream for r in receivers[1:]), 'Restart did not reconnect'
        process.wait(timeout=5)
        log.close()
        assert process.returncode == 0
        print('PASS: native SHM, subscriber-first, three camera offsets, duplicate subscriber pixels, slow/closed subscriber, bounded pool exhaustion/recovery, publisher restart')
    finally:
        for receiver in receivers[1:]:
            receiver.close()
        if receivers[0].session.is_closed() is False:
            receivers[0].close()
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        if log is not None:
            log.close()


if __name__ == '__main__':
    main()
