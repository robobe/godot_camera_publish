"""Run the three-camera/four-viewer NVIDIA GPU-sharing smoke check."""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "build/godot-src/bin/godot.linuxbsd.template_debug.x86_64"
CHANNELS = ("camera1_a", "camera1_b", "camera2", "camera3")


def main(seconds):
    (ROOT / "build").mkdir(exist_ok=True)
    env = dict(os.environ, __NV_PRIME_RENDER_OFFLOAD="1", __VK_LAYER_NV_optimus="NVIDIA_only")
    godot_log = (ROOT / "build/gpu-multi-camera-godot.log").open("w")
    godot = subprocess.Popen(
        [str(BINARY), "--path", str(ROOT / "demo"), "--disable-vsync", "--print-fps", "--", f"--gpu-frames={(seconds + 10) * 30}"],
        cwd=ROOT,
        env=env,
        stdout=godot_log,
        stderr=subprocess.STDOUT,
    )
    viewers = []
    logs = []
    try:
        for channel in CHANNELS:
            log = (ROOT / "build" / f"gpu-viewer-{channel}.log").open("w+")
            logs.append(log)
            viewers.append(
                subprocess.Popen(
                    [sys.executable, "-u", str(ROOT / "viewer.py"), "--channel", channel, "--seconds", str(seconds)],
                    cwd=ROOT,
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            )
        for channel, viewer, log in zip(CHANNELS, viewers, logs):
            code = viewer.wait(timeout=seconds + 20)
            log.flush()
            log.seek(0)
            output = log.read()
            frame_ids = [int(value) for value in re.findall(r"Frame: (\d+)", output)]
            fps = [float(value) for value in re.findall(r"Received FPS: ([\d.]+)", output)]
            if code or not frame_ids or max(frame_ids) <= 3 or not fps or max(fps) == 0:
                raise RuntimeError(f"{channel} viewer failed (exit {code}):\n{output}")
            print(f"{channel}: {max(fps):.1f} FPS, last frame {max(frame_ids)}")
    finally:
        for viewer in viewers:
            if viewer.poll() is None:
                viewer.terminate()
                viewer.wait(timeout=5)
        if godot.poll() is None:
            godot.terminate()
            try:
                godot.wait(timeout=5)
            except subprocess.TimeoutExpired:
                godot.kill()
                godot.wait()
        godot_log.close()
        for log in logs:
            log.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=10)
    main(parser.parse_args().seconds)
