"""Hardware integration check for one NVIDIA Vulkan publisher and CUDA consumer."""

from pathlib import Path
import os
import re
import select
import socket
import subprocess
import sys
import time

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from gpu_receiver import address_for, GpuReceiver, RELEASE, verify_pattern  # noqa: E402

BINARY = ROOT / "build/godot-src/bin/godot.linuxbsd.template_debug.x86_64"


def connect(process):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"Godot exited early: {process.returncode}, see build/gpu-integration-godot.log"
            )
        try:
            return GpuReceiver.connect("camera1_a")
        except (ConnectionRefusedError, FileNotFoundError):
            time.sleep(0.05)
    raise AssertionError("Timed out waiting for GPU publisher")


def next_frame(receiver, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        frame = receiver.acquire()
        if frame is not None:
            return frame
        select.select([receiver.sock], [], [], 0.02)
    raise AssertionError("Timed out waiting for GPU frame")


def main():
    # Do not connect to an unrelated publisher already using this user's address.
    with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as probe:
        probe.bind(address_for("camera1_a"))
    env = dict(
        os.environ, __NV_PRIME_RENDER_OFFLOAD="1", __VK_LAYER_NV_optimus="NVIDIA_only"
    )
    log_path = ROOT / "build/gpu-integration-godot.log"
    receiver = None
    with log_path.open("w") as log:
        process = subprocess.Popen(
            [
                str(BINARY),
                "--path",
                str(ROOT / "demo"),
                "--disable-vsync",
                "--print-fps",
                "--",
                "--gpu-frames=540",
            ],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            receiver = connect(process)
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as second:
                second.settimeout(3)
                second.connect(address_for("camera1_a"))
                assert second.recv(100) == b"BUSY", "Second consumer must be rejected"
            held = []
            snapshots = []
            for _ in range(3):
                frame = next_frame(receiver)
                assert frame.pattern, "First three frames must be GPU diagnostics"
                pixels = receiver.download(frame)
                verify_pattern(pixels)
                held.append(frame)
                snapshots.append(pixels)
            assert len({f.slot for f in held}) == 3
            time.sleep(0.25)
            assert receiver.acquire() is None, "All held slots must stop publication"
            for frame, snapshot in zip(held, snapshots):
                assert np.array_equal(receiver.download(frame), snapshot), (
                    "Publisher overwrote a held slot"
                )
            receiver.release(held[1])
            recovered = next_frame(receiver)
            assert recovered.slot == held[1].slot and not recovered.pattern
            pixels = receiver.download(recovered)
            assert pixels.shape == (480, 640, 4) and pixels[:, :, :3].std() > 10
            assert np.all(pixels[:, :, 3] == 255)
            receiver.release(recovered)
            for frame in [held[0], held[2]]:
                receiver.release(frame)
            first_scene = pixels.copy()
            for _ in range(12):
                frame = next_frame(receiver)
                pixels = receiver.download(frame)
                receiver.release(frame)
            assert not np.array_equal(first_scene, pixels), (
                "Animated camera pixels must change"
            )
            import cv2

            cv2.imwrite(
                str(ROOT / "build/gpu-camera.png"),
                cv2.cvtColor(pixels, cv2.COLOR_RGBA2BGR),
            )
            receiver.close()
            receiver = None
            time.sleep(0.1)
            receiver = connect(process)
            frame = next_frame(receiver)
            assert receiver.download(frame)[:, :, :3].std() > 10
            receiver.release(frame)
            # Malformed frame release must close this connection without terminating Godot.
            receiver.sock.send(RELEASE.pack(b"REL1", 99, frame.frame_id))
            deadline = time.monotonic() + 2
            disconnected = False
            while time.monotonic() < deadline:
                try:
                    pending = receiver.acquire()
                    if pending is not None:
                        receiver.download(pending)
                except (EOFError, ConnectionResetError):
                    disconnected = True
                    break
                time.sleep(0.02)
            assert disconnected, "Malformed release must disconnect the consumer"
            receiver.close()
            receiver = None
            # A killed CUDA consumer must not pin the next connection's slots.
            child_code = """
import select, time
from gpu_receiver import GpuReceiver
r = GpuReceiver.connect("camera1_a")
held = []
while len(held) < 3:
    f = r.acquire()
    if f is not None:
        r.download(f)
        held.append(f)
    select.select([r.sock], [], [], 0.01)
print('HELD', flush=True)
time.sleep(20)
"""
            with (ROOT / "build/gpu-killed-consumer.log").open("w+") as child_log:
                child = subprocess.Popen(
                    [sys.executable, "-u", "-c", child_code],
                    cwd=ROOT,
                    stdout=child_log,
                    stderr=subprocess.STDOUT,
                )
                try:
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if (
                            "HELD"
                            in (ROOT / "build/gpu-killed-consumer.log").read_text()
                        ):
                            break
                        assert child.poll() is None, (
                            "CUDA child exited before acquiring slots"
                        )
                        time.sleep(0.05)
                    else:
                        raise AssertionError("CUDA child did not acquire three slots")
                finally:
                    if child.poll() is None:
                        child.kill()
                    child.wait(timeout=5)
            time.sleep(0.1)
            receiver = connect(process)
            frame = next_frame(receiver)
            receiver.download(frame)
            receiver.release(frame)
            receiver.close()
            receiver = None
            assert process.wait(timeout=30) == 0, "Godot must exit normally"
            content = log_path.read_text()
            assert "NVIDIA" in content and "Vulkan" in content
            assert any(int(n) > 0 for n in re.findall(r"Dropped: (\d+)", content))
            assert any(
                int(n) >= 25 for n in re.findall(r"Project FPS: (\d+)", content)
            ), content
            assert content.count("Invalid GPU release packet") == 1, content
            assert "ERROR:" not in content and "VUID-" not in content, content
            print(
                "PASS: NVIDIA GPU import, exact pattern, 3 held slots, recovery, camera animation, single consumer per channel, reconnect, malformed release, killed CUDA consumer and normal shutdown"
            )
        finally:
            if receiver is not None:
                receiver.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
