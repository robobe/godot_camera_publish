"""Real NVIDIA Godot -> ZeroMQ SHM integration. Run with .venv/bin/python."""
import os
import fcntl
import re
from pathlib import Path
import subprocess
import sys
import time

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from viewer import FrameReceiver

ROOT = Path(__file__).resolve().parents[1]
ENV = {**os.environ, '__NV_PRIME_RENDER_OFFLOAD': '1', '__GLX_VENDOR_LIBRARY_NAME': 'nvidia'}
LOGS = []


def start(name, frames):
    path = ROOT / 'build' / name
    LOGS.append(path)
    log = path.open('w')
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
        process, log = start('integration-zmq-shm.log', 600)
        results = collect(receivers, 12)
        assert all(len(r) >= 30 for r in results), [len(r) for r in results]
        common = set(results[0]).intersection(*results[1:])
        assert common, 'No common render frame across cameras/subscribers'
        frame_id = max(common)
        assert np.array_equal(results[0][frame_id], results[1][frame_id]), 'Fanout changed pixels'
        assert all(not np.array_equal(results[0][frame_id], results[i][frame_id]) for i in (2, 3)), 'Offsets must produce distinct views'
        old_stream = receivers[0].stream_id
        own_paths = [Path('/dev/shm') / name for name in re.findall(
            r'SHM slot: /([^\s]+)', (ROOT / 'build/integration-zmq-shm.log').read_text())]
        assert len(own_paths) == 9
        # Two pinned slots must leave the third usable for this camera.
        held = [os.open(f'/dev/shm/godot_camera_zmq_{old_stream}_c1_s{i}', os.O_RDONLY) for i in (0, 1)]
        try:
            for fd in held:
                fcntl.flock(fd, fcntl.LOCK_SH)
            rotation = collect(receivers, .5)
            assert len(rotation[0]) >= 5, 'Publisher did not rotate around locked slots'
        finally:
            for fd in held:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
        # A reader holding all camera-1 slots must not stall other cameras or Godot.
        code = """import fcntl,os,sys,time
fds=[os.open(path,os.O_RDONLY) for path in sys.argv[1:]]
for fd in fds: fcntl.flock(fd,fcntl.LOCK_SH)
print('locked',flush=True)
time.sleep(20)
"""
        locked = subprocess.Popen([sys.executable, '-c', code,
            *[f'/dev/shm/godot_camera_zmq_{old_stream}_c1_s{i}' for i in range(3)]],
            stdout=subprocess.PIPE, text=True)
        try:
            assert locked.stdout.readline().strip() == 'locked'
            collect(receivers, .1)
            before_locked = receivers[0].frame_id
            active = collect(receivers, .4)
            assert receivers[0].frame_id == before_locked, 'Locked camera slot was overwritten'
            assert all(len(result) >= 5 for result in active[2:]), 'Other cameras stalled'
            locked.kill()
            locked.wait(timeout=5)
            recovered = collect(receivers, .5)
            assert len(recovered[0]) >= 5, 'Reader crash did not release locks'
        finally:
            if locked.poll() is None:
                locked.kill()
                locked.wait()
            locked.stdout.close()
        before = receivers[1].frame_id
        collect(receivers[1:], 1)
        assert receivers[1].frame_id > before, 'Unpolled subscriber blocked the others'
        collect([receivers[0], receivers[1]], .2)
        assert receivers[0].frame_id >= receivers[1].frame_id - 3, 'Reader retained a stale backlog'
        receivers[0].close()
        before = receivers[1].frame_id
        collect(receivers[1:], 1)
        assert receivers[1].frame_id > before, 'Closed subscriber blocked the others'
        process.wait(timeout=10)
        log.close()
        assert process.returncode == 0
        assert all(not p.exists() for p in own_paths), 'Normal shutdown left SHM slots'
        process, log = start('integration-zmq-shm-restart.log', 330)
        collect(receivers[1:], 6)
        assert all(r.stream_id != old_stream for r in receivers[1:]), 'Restart did not reconnect'
        interrupted_stream = receivers[1].stream_id
        old_mappings = list(receivers[1].mappings.values())
        process.kill()
        process.wait(timeout=5)
        log.close()
        # Reap only this test publisher's slots after its process has stopped.
        for name in re.findall(r'SHM slot: /([^\s]+)', LOGS[-1].read_text()):
            (Path('/dev/shm') / name).unlink(missing_ok=True)
        process, log = start('integration-zmq-shm-after-crash.log', 180)
        collect(receivers[1:], 6)
        assert all(r.stream_id != interrupted_stream for r in receivers[1:]), 'Crash restart did not reconnect'
        assert all(mapping.memory.closed for mapping in old_mappings), 'Old session mapping leaked'
        assert all(len(r.mappings) <= 3 for r in receivers[1:])
        process.wait(timeout=10)
        log.close()
        assert process.returncode == 0
        print('PASS: ZeroMQ images, subscriber-first, three camera offsets, duplicate subscriber pixels, slow/closed subscriber, slot rotation/exhaustion, crashed reader, publisher restart, cleanup')
    finally:
        for receiver in receivers[1:]:
            receiver.close()
        if not receivers[0].sock.closed:
            receivers[0].close()
        if process is not None and process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        if log is not None:
            log.close()
        for path in LOGS:
            for name in re.findall(r'SHM slot: /([^\s]+)', path.read_text()):
                (Path('/dev/shm') / name).unlink(missing_ok=True)


if __name__ == '__main__':
    main()
