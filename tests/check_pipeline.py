"""Rendered integration check. Run from the repo root; no OpenCV window is opened."""

from pathlib import Path
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from viewer import ADDRESS, FrameReceiver  # noqa: E402

ADDRESSES = (ADDRESS, (ADDRESS[0], 5002), (ADDRESS[0], 5003))


def connect(process, address=ADDRESS):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"Godot exited early: {process.returncode}")
        try:
            sock = socket.create_connection(address, timeout=0.2)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return FrameReceiver(sock)
        except OSError:
            time.sleep(0.05)
    raise AssertionError("Timed out connecting to Godot")


def next_frame(receiver):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        frame = receiver.poll()
        if frame is not None:
            return frame
        select.select([receiver.sock], [receiver.sock] if receiver.outgoing else [], [], 0.005)
    raise AssertionError("Timed out waiting for frame")


def matching_frames(receivers):
    frames = [None] * len(receivers)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        for index, receiver in enumerate(receivers):
            frame = receiver.poll()
            if frame is not None:
                frames[index] = frame
        if all(frame is not None for frame in frames) and len({reader.frame_id for reader in receivers}) == 1:
            return frames
        select.select([receiver.sock for receiver in receivers], [], [], 0.005)
    raise AssertionError("Timed out matching the cameras' rendered frame IDs")


def main():
    # Refuse to consume an unrelated publisher or mistake it for our child process.
    for address in ADDRESSES:
        with socket.socket() as probe:
            probe.bind(address)
    receiver = receiver2 = receiver3 = None
    with tempfile.TemporaryFile(mode="w+") as log:
        process = subprocess.Popen(
            ["godot", "--path", str(ROOT / "demo"), "--disable-vsync", "--quit-after", "330"],
            stdout=log, stderr=subprocess.STDOUT,
        )
        try:
            receiver = connect(process)
            # Delay ACK to exercise a reader still copying the shared slot.
            with patch.object(receiver, "_flush_ack"):
                first = next_frame(receiver)
                first_id = receiver.frame_id
                assert first.shape == (480, 640, 3)
                assert first.nbytes == 921600, "Expected tightly packed RGB8"
                assert first.std() > 10, "Expected rendered scene, not a blank image"
                snapshot = bytes(receiver.memory)
                time.sleep(0.3)
                assert bytes(receiver.memory) == snapshot, "Producer overwrote the slot before ACK"
            second = next_frame(receiver)
            assert receiver.frame_id > first_id + 1, "Slow consumer should skip rendered frames"
            assert not np.array_equal(first, second), "Expected animated images"

            # SIGSTOP pauses rendering without running the application exit cleanup.
            process.send_signal(signal.SIGSTOP)
            time.sleep(0.05)
            # Drain at most one already published frame, then the stopped producer must be idle.
            receiver.poll()
            assert receiver.poll() is None
            process.send_signal(signal.SIGCONT)
            next_frame(receiver)

            receiver.close()
            receiver = connect(process)
            next_frame(receiver)
            receiver2 = connect(process, ADDRESSES[1])
            receiver3 = connect(process, ADDRESSES[2])
            frames = matching_frames([receiver, receiver2, receiver3])
            for index, frame in enumerate(frames):
                assert frame.shape == (480, 640, 3) and frame.nbytes == 921600
                for other in frames[:index]:
                    assert np.abs(frame.astype(np.int16) - other.astype(np.int16)).mean() > 1, \
                        "Offset cameras must show different views of the same rendered frame"

            with patch.object(receiver2, "_flush_ack"), patch.object(receiver3, "_flush_ack"):
                next_frame(receiver2)
                next_frame(receiver3)
                snapshot2 = bytes(receiver2.memory)
                snapshot3 = bytes(receiver3.memory)
                first_id = receiver.frame_id
                for _ in range(5):
                    next_frame(receiver)
                assert receiver.frame_id > first_id, "A stalled camera 2 viewer blocked camera 1"
                assert bytes(receiver2.memory) == snapshot2, "Camera 2 buffer changed without its ACK"
                assert bytes(receiver3.memory) == snapshot3, "Camera 3 buffer changed without its ACK"
            next_frame(receiver2)
            next_frame(receiver3)
            receiver.close()
            next_frame(receiver2)  # Closing camera 1 must not stop camera 2.
            next_frame(receiver3)
            receiver = connect(process)
            next_frame(receiver)

            counts = [0, 0, 0]
            start = time.monotonic()
            while time.monotonic() - start < 3:
                for index, reader in enumerate([receiver, receiver2, receiver3]):
                    if reader.poll() is not None:
                        counts[index] += 1
                select.select([receiver.sock, receiver2.sock, receiver3.sock], [], [], 0.005)
            duration = time.monotonic() - start
            for address, count in zip(ADDRESSES, counts):
                fps = count / duration
                print(f"Camera {address[1]} rendered delivery: {fps:.1f} FPS ({count} frames)")
                assert 0 < fps <= 32, "Expected frames within the 30 FPS cap"
            receiver.close()
            receiver2.close()
            receiver3.close()
            receiver = None
            receiver2 = None
            receiver3 = None
            # --quit-after goes through SceneTree shutdown; SIGINT may bypass scene cleanup.
            process.wait(timeout=20)
            log.seek(0)
            output = log.read()
            print(output)
            assert process.returncode == 0, f"Godot exited with {process.returncode}"
            assert "SCRIPT ERROR" not in output and "ERROR:" not in output, "Godot reported errors"
            paths = set()
            for line in output.splitlines():
                if "shared memory: " in line:
                    paths.add(Path("/dev/shm") / line.split("shared memory: ", 1)[1].lstrip("/"))
            assert len(paths) == 3, "Expected three distinct shared-memory segments"
            assert all(not path.exists() for path in paths), f"Shared-memory segment leaked: {paths}"
            print("PASS: offset views, independent readers, ownership, pause/resume, reconnect, and cleanup")
        except BaseException:
            log.seek(0)
            print(log.read(), file=sys.stderr)
            raise
        finally:
            for reader in [receiver, receiver2, receiver3]:
                if reader is not None:
                    reader.close()
            if process.poll() is None:
                process.send_signal(signal.SIGCONT)
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            # Failed checks may terminate Godot before its scene cleanup can run.
            log.seek(0)
            for line in log.read().splitlines():
                prefix = f"shared memory: /godot_camera_{process.pid}_"
                if prefix in line:
                    orphan = Path("/dev/shm") / line.split("shared memory: ", 1)[1].lstrip("/")
                    orphan.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
