"""Measure the three-camera/four-viewer setup on NVIDIA for 30 seconds."""

import argparse
import csv
import datetime
import json
import os
from pathlib import Path
import re
import signal
import statistics
import subprocess
import sys
import time

import psutil

ROOT = Path(__file__).resolve().parents[1]
BINARY = ROOT / "build/godot-src/bin/godot.linuxbsd.template_debug.x86_64"
CHANNELS = ("camera1_a", "camera1_b", "camera2", "camera3")


def mean_peak(values):
    return {"mean": statistics.mean(values), "peak": max(values)}


def main(seconds=30, warmup=3):
    output = ROOT / "build" / ("gpu-sharing-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S"))
    output.mkdir(parents=True)
    env = dict(os.environ, __NV_PRIME_RENDER_OFFLOAD="1", __VK_LAYER_NV_optimus="NVIDIA_only")
    handles = {}
    children = {}
    gpu_monitor = None
    rows = []
    try:
        handles["godot"] = (output / "godot.log").open("w+")
        children["godot"] = subprocess.Popen(
            [
                str(BINARY), "--path", str(ROOT / "demo"), "--disable-vsync", "--print-fps",
                "--", f"--gpu-frames={(seconds + warmup + 15) * 30}",
            ],
            cwd=ROOT,
            env=env,
            stdout=handles["godot"],
            stderr=subprocess.STDOUT,
        )
        for index, channel in enumerate(CHANNELS, 1):
            name = f"viewer{index}"
            handles[name] = (output / f"{name}.log").open("w+")
            children[name] = subprocess.Popen(
                [sys.executable, "-u", str(ROOT / "viewer.py"), "--channel", channel],
                cwd=ROOT,
                env=env,
                stdout=handles[name],
                stderr=subprocess.STDOUT,
            )
        gpu_log_path = output / "nvidia.csv"
        gpu_log = gpu_log_path.open("w+")
        handles["nvidia"] = gpu_log
        gpu_monitor = subprocess.Popen(
            [
                "stdbuf", "-oL", "nvidia-smi", "-i", "0",
                "--query-gpu=timestamp,utilization.gpu,memory.used",
                "--format=csv,noheader,nounits", "--loop-ms=1000",
            ],
            stdout=gpu_log,
            stderr=subprocess.STDOUT,
        )

        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if any(process.poll() is not None for process in children.values()):
                raise RuntimeError("A publisher or viewer exited during startup")
            if all("CUDA imported 3 GPU slots" in (output / f"viewer{i}.log").read_text() for i in range(1, 5)):
                break
            time.sleep(0.1)
        else:
            raise RuntimeError("Timed out waiting for all four CUDA viewers to connect")

        time.sleep(warmup)
        processes = {name: psutil.Process(process.pid) for name, process in children.items()}
        for process in processes.values():
            process.cpu_percent()
        psutil.cpu_percent()
        offsets = {f"viewer{i}": (output / f"viewer{i}.log").stat().st_size for i in range(1, 5)}
        started = time.monotonic()
        print(f"Measuring {seconds}s after {warmup}s warmup: {output}", flush=True)

        for index in range(seconds):
            target = started + index + 1
            time.sleep(max(0, target - time.monotonic()))
            now = time.monotonic()
            row = {"elapsed_s": now - started, "system_cpu_pct": psutil.cpu_percent()}
            for name, process in processes.items():
                if not process.is_running() or process.status() == psutil.STATUS_ZOMBIE:
                    raise RuntimeError(f"{name} exited during measurement")
                row[f"{name}_cpu_pct"] = process.cpu_percent()
                row[f"{name}_rss_mib"] = process.memory_info().rss / 2**20
            gpu_log.flush()
            gpu_rows = gpu_log_path.read_text().splitlines()
            if not gpu_rows or gpu_monitor.poll() is not None:
                raise RuntimeError("nvidia-smi monitoring stopped")
            fields = gpu_rows[-1].split(",")
            row["nvidia_gpu_pct"] = float(fields[-2].strip())
            row["nvidia_memory_mib"] = float(fields[-1].strip())
            rows.append(row)
            if (index + 1) % 10 == 0:
                print(f"{index + 1}/{seconds}s | GPU {row['nvidia_gpu_pct']:.0f}% | Godot CPU {row['godot_cpu_pct']:.1f}%", flush=True)

        duration = time.monotonic() - started
        end_offsets = {name: (output / f"{name}.log").stat().st_size for name in offsets}
        for name, process in children.items():
            if name != "godot":
                process.send_signal(signal.SIGINT)
        for name, process in children.items():
            process.wait(timeout=20 if name == "godot" else 5)
        if any(process.returncode != 0 for name, process in children.items() if name != "godot"):
            raise RuntimeError("A viewer did not exit cleanly")

        summary = {
            "date": datetime.date.today().isoformat(),
            "duration_s": duration,
            "warmup_s": warmup,
            "samples": len(rows),
            "logical_cpus": psutil.cpu_count(),
            "channels": list(CHANNELS),
        }
        for key in rows[0]:
            if key == "elapsed_s":
                continue
            values = [row[key] for row in rows]
            summary[key] = mean_peak(values)
        viewer_fps = {}
        for index, channel in enumerate(CHANNELS, 1):
            name = f"viewer{index}"
            with (output / f"{name}.log").open() as log:
                log.seek(offsets[name])
                text = log.read()
            rates = [float(value) for value in re.findall(r"Received FPS: ([0-9.]+)", text)]
            if not rates:
                raise RuntimeError(f"{name} produced no FPS measurements")
            viewer_fps[name] = {"channel": channel, "mean": statistics.mean(rates), "min": min(rates), "max": max(rates)}
        summary["viewers"] = viewer_fps
        with (output / "samples.csv").open("w") as sample_file:
            writer = csv.DictWriter(sample_file, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
        print(json.dumps(summary, indent=2), flush=True)
        print(f"Artifacts: {output}", flush=True)
    finally:
        for process in children.values():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        if gpu_monitor is not None and gpu_monitor.poll() is None:
            gpu_monitor.terminate()
            gpu_monitor.wait(timeout=5)
        for handle in handles.values():
            handle.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=30)
    parser.add_argument("--warmup", type=int, default=3)
    args = parser.parse_args()
    main(args.seconds, args.warmup)
