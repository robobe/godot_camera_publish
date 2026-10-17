#!/usr/bin/env python3
"""Measure four ZeroMQ/SHM viewers on NVIDIA; --tune compares signaling socket buffers."""
import argparse
import csv
import datetime
import json
import math
import os
from pathlib import Path
import re
import signal
import socket
import statistics
import subprocess
import sys
import time

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from viewer import buffer_bytes


def choose_buffer(results):
    qualified = [r for r in results if all(r[f'viewer{i}_fps']['mean'] >= 29 for i in range(1, 5))]
    if qualified:
        return min(qualified, key=lambda r: r['buffer_bytes'])['buffer_bytes']
    return max(results, key=lambda r: (
        round(min(r[f'viewer{i}_fps']['mean'] for i in range(1, 5)), 1), -r['buffer_bytes']))['buffer_bytes']


def measure(size, seconds):
    stamp = datetime.datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    out = ROOT / 'build' / f'benchmark-zmq-shm-{size}-{stamp}'
    out.mkdir(parents=True)
    env = {**os.environ, 'CAMERA_ZMQ_BUFFER_BYTES': str(size)}
    commands = {'godot': [str(Path.home() / '.local/bin/godot'), '--path', str(ROOT / 'demo'),
                          '--quit-after', str(math.ceil((seconds + 12) * 30))]}
    for i, camera in enumerate((1, 2, 3, 1), 1):
        commands[f'viewer{i}'] = [sys.executable, '-u', str(ROOT / 'viewer.py'), '--camera', str(camera)]
    viewers = [name for name in commands if name != 'godot']
    children, logs, rows = {}, {}, []
    gpu = None
    try:
        for name, command in commands.items():
            logs[name] = (out / f'{name}.log').open('w')
            child_env = dict(env)
            if name == 'godot':
                child_env.update(__NV_PRIME_RENDER_OFFLOAD='1', __GLX_VENDOR_LIBRARY_NAME='nvidia')
            children[name] = subprocess.Popen(command, cwd=ROOT, env=child_env,
                                              stdout=logs[name], stderr=subprocess.STDOUT)
        logs['gpu'] = (out / 'nvidia.csv').open('w')
        logs['gpu_errors'] = (out / 'nvidia-errors.log').open('w')
        gpu = subprocess.Popen(['stdbuf', '-oL', 'nvidia-smi', '-i', '0',
                                '--query-gpu=timestamp,utilization.gpu,memory.used',
                                '--format=csv,noheader,nounits', '--loop-ms=1000'],
                               stdout=logs['gpu'], stderr=logs['gpu_errors'])
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            assert all(p.poll() is None for p in children.values()), f'A demo process exited: {out}'
            assert gpu.poll() is None, 'NVIDIA monitoring exited'
            ready = 'Using Device: NVIDIA' in (out / 'godot.log').read_text()
            if (ready and all('Live (ZeroMQ SHM)' in (out / f'{v}.log').read_text() for v in viewers)
                    and (out / 'nvidia.csv').stat().st_size):
                break
            time.sleep(.1)
        else:
            raise AssertionError(f'Initialization timed out: {out}')
        time.sleep(3)
        processes = {name: psutil.Process(child.pid) for name, child in children.items()}
        for name, process in processes.items():
            connections = process.connections(kind='inet')
            assert connections and all(c.type == socket.SOCK_STREAM for c in connections), name
            mappings = Path(f'/proc/{process.pid}/maps').read_text()
            slot_names = set(re.findall(r'/dev/shm/(godot_camera_zmq_[^\s]+)', mappings))
            assert (len(slot_names) == 9 if name == 'godot' else 1 <= len(slot_names) <= 3), name
            assert '.zenoh' not in mappings, name
            process.cpu_percent()
        (out / 'tcp-sockets.log').write_text(subprocess.check_output(['ss', '-tmipn'], text=True))
        offsets = {name: (out / f'{name}.log').stat().st_size for name in viewers}
        started = time.monotonic()
        print(f'{size // 1024} KiB: measuring {seconds}s with four viewers: {out}', flush=True)
        for index in range(seconds):
            time.sleep(max(0, started + index + 1 - time.monotonic()))
            row = {'elapsed_s': time.monotonic() - started}
            for name, process in processes.items():
                assert children[name].poll() is None, f'{name} exited during measurement'
                row[f'{name}_cpu_pct'] = process.cpu_percent()
                row[f'{name}_rss_mib'] = process.memory_info().rss / 2**20
            assert gpu.poll() is None and time.time() - (out / 'nvidia.csv').stat().st_mtime < 3
            fields = (out / 'nvidia.csv').read_text().splitlines()[-1].split(',')
            row['gpu_pct'], row['vram_mib'] = float(fields[-2]), float(fields[-1])
            rows.append(row)
            if (index + 1) % 10 == 0:
                print(f'{size // 1024} KiB: {index + 1}/{seconds}s', flush=True)
        duration = time.monotonic() - started
        rates = {}
        for name in viewers:
            text = (out / f'{name}.log').read_bytes()[offsets[name]:].decode()
            values = [float(x) for x in re.findall(r'Received FPS: ([0-9.]+)', text)]
            assert len(values) >= seconds - 2 and 'Live (ZeroMQ SHM)' in text, name
            rates[name + '_fps'] = dict(mean=statistics.mean(values), min=min(values),
                                        max=max(values), samples=len(values))
        for name in viewers:
            children[name].send_signal(signal.SIGINT)
        for name in viewers:
            children[name].wait(timeout=5)
        children['godot'].wait(timeout=25)
        assert all(p.returncode == 0 for p in children.values()), 'Expected clean exits'
        godot_log = (out / 'godot.log').read_text()
        assert 'ERROR:' not in godot_log and 'SCRIPT ERROR' not in godot_log
        names = re.findall(r'SHM slot: /([^\s]+)', godot_log)
        assert len(names) == 9 and all(not (Path('/dev/shm') / n).exists() for n in names), 'SHM cleanup failed'
        assert len(rows) == seconds and duration >= seconds
        summary = dict(buffer_bytes=size, duration_s=duration, samples=len(rows),
                       logical_cpus=psutil.cpu_count(), output=str(out), **rates)
        for key in rows[0]:
            if key != 'elapsed_s':
                values = [r[key] for r in rows]
                summary[key] = dict(mean=statistics.mean(values), peak=max(values))
        with (out / 'samples.csv').open('w') as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0])
            writer.writeheader()
            writer.writerows(rows)
        (out / 'summary.json').write_text(json.dumps(summary, indent=2)+'\n')
        print(json.dumps({k: v for k, v in summary.items() if k.endswith('_fps')}, indent=2), flush=True)
        return summary
    finally:
        if gpu is not None and gpu.poll() is None:
            gpu.terminate()
            gpu.wait(timeout=5)
        for child in children.values():
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
        for handle in logs.values():
            handle.close()
        if 'godot' in children:
            for name in re.findall(r'SHM slot: /([^\s]+)', (out / 'godot.log').read_text()):
                if name.startswith(f'godot_camera_zmq_{children["godot"].pid}-'):
                    (Path('/dev/shm') / name).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tune', action='store_true', help='Measure 64, 256, 1024 KiB for 10s each')
    args = parser.parse_args()
    if args.tune:
        results = [measure(size, 10) for size in (65536, 262144, 1048576)]
        selection = dict(selected_buffer_bytes=choose_buffer(results), results=results)
        path = ROOT / 'build' / 'zmq-shm-buffer-tuning.json'
        path.write_text(json.dumps(selection, indent=2)+'\n')
        print(f'Selected {selection["selected_buffer_bytes"]} bytes; {path}', flush=True)
    else:
        result = measure(buffer_bytes(), 30)
        (ROOT / 'build' / 'zmq-shm-benchmark-latest.json').write_text(json.dumps(result, indent=2)+'\n')
        print('RESULT:', result['output'], flush=True)


if __name__ == '__main__':
    main()
