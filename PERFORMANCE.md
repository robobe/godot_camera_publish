# Camera transport performance summary

The custom SHM, Zenoh, and ZeroMQ TCP rows are historical measurements transcribed from prior reports in this conversation. The ZeroMQ-signaled SHM row came from its own 30-second benchmark on branch `zmq_shm`. The Vulkan/CUDA external-memory row is a new 30-second run of the current `gpu_sharing` branch.

The current GPU-sharing run measured 30.002 seconds after a 3-second warmup on 2026-10-17. It used the NVIDIA GeForce RTX 5070 Laptop GPU (driver 580.178.04), three 640×480 RGBA8 cameras, a 30 FPS target, and 90° horizontal FOV. Cameras 2 and 3 were offset 1 m and 2 m to camera 1's right. Four OpenCV viewer processes subscribed to camera views 1, 1, 2, and 3. Historical rows used RGB8 and their original recorded conditions.

## Average performance

| Method | Viewers | Received FPS per viewer | Godot CPU | CPU per viewer | Godot RAM (RSS) | RAM per viewer (RSS) | NVIDIA GPU utilization | NVIDIA VRAM |
| --- | ---: | --- | ---: | --- | ---: | --- | ---: | ---: |
| Custom POSIX SHM + TCP READY/ACK | 3 | 30.0 / 29.9 / 29.9 | 44.4% | 49.2–49.6% | 199.9 MiB | 211.0–215.1 MiB | 7.1% | 114 MiB |
| Zenoh native SHM + TCP coordination | 3 | 30.0 / 30.0 / 30.0 | 44.1% | 44.8–45.5% | 246.8 MiB | 258.1–259.3 MiB | 10.6% | 116 MiB |
| Zenoh best-effort UDP, SHM disabled | 4 | 14.9 / 17.1 / 18.2 / 17.7 | 85.8% | 48.6–49.8% | 210.9 MiB | 223.2–224.2 MiB | 7.5% | 114 MiB |
| ZeroMQ PUB/SUB over TCP, no SHM | 4 | 30.02 / 30.03 / 29.98 / 29.99 | 48.4% | 46.1–46.7% | 207.4 MiB | 212.5–215.6 MiB | 10.9% | 114 MiB |
| ZeroMQ signaling + three SHM slots per camera | 4 | 30.01 / 30.01 / 30.00 / 30.03 | 46.1% | 46.0–46.6% | 211.3 MiB | 213.7–215.2 MiB | 9.5% | 114 MiB |
| Vulkan external GPU buffers + CUDA | 4 | 30.01 / 29.99 / 30.01 / 30.00 | 10.7% | 81.2–83.8% | 352.0 MiB | 197.8–198.3 MiB | 4.1% | 737 MiB |

All table entries are averages. Viewer CPU and RAM ranges describe averages across individual viewer processes, not fluctuations over time. The new GPU row used two separate GPU channels for camera 1, each with its own exportable slots and CUDA imports. This duplicates the camera-1 GPU copy to give each viewer independent ownership.

The measured per-viewer FPS intervals were:

| Viewer | Camera channel | Average FPS | Minimum interval | Maximum interval |
|---|---|---:|---:|---:|
| 1 | camera1_a | 30.01 | 29.6 | 31.0 |
| 2 | camera1_b | 29.99 | 29.6 | 30.9 |
| 3 | camera2 | 30.01 | 29.7 | 30.9 |
| 4 | camera3 | 30.00 | 29.6 | 30.9 |

Resource samples were taken once per second during the same interval:

| Process/device | Average CPU | Peak CPU | Average RSS | Peak RSS | Average GPU use | Peak GPU use | Average VRAM | Peak VRAM |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| Godot | 10.7% | 12.0% | 352.0 MiB | 352.0 MiB | — | — | — | — |
| Viewer 1 | 83.3% | 95.0% | 198.0 MiB | 198.1 MiB | — | — | — | — |
| Viewer 2 | 81.2% | 87.0% | 198.2 MiB | 198.2 MiB | — | — | — | — |
| Viewer 3 | 82.7% | 94.0% | 197.8 MiB | 197.8 MiB | — | — | — | — |
| Viewer 4 | 83.8% | 93.0% | 198.3 MiB | 198.3 MiB | — | — | — | — |
| Whole NVIDIA device | — | — | — | — | 4.1% | 5.0% | 737 MiB | 737 MiB |

Whole-system CPU averaged 30.7% and peaked at 34.1% across the 16 logical CPUs, including the desktop and background applications.

- **CPU:** 100% means one logical core; the machine has 16 logical cores.
- **RAM:** RSS includes shared pages. Do not sum process RSS as unique physical memory.
- **GPU:** utilization and VRAM cover the whole NVIDIA device, including any other clients. They are not per-process measurements.
- **FPS:** received/displayed frame counts over each viewer's reporting interval. Values slightly above 30 reflect interval timing, not a higher configured camera rate.

## Buffer settings and observed FPS

| Method | Buffer/queue policy | Observed result |
| --- | --- | --- |
| Custom SHM | One RGB slot per camera, protected by READY/copy/ACK ownership | Approximately 30 FPS with one viewer per camera; a slow viewer holds its camera's slot |
| Zenoh SHM | 32 MiB publisher pool, nonblocking allocation, DROP congestion policy, RingChannel(1) per subscriber | Approximately 30 FPS in the three-viewer benchmark; separate integration checks verified duplicate subscribers and pool exhaustion/recovery |
| Zenoh UDP | Best-effort publications, DROP congestion policy, 8 KiB transmit batches, RingChannel(1) | Four-viewer average rates remained below 30 FPS; frame loss and latest-only queue replacement were not measured separately |
| ZeroMQ TCP | SNDHWM=1, nonblocking PUB sends, CONFLATE=1 per SUB, 64 KiB requested kernel send/receive buffers | Approximately 30 FPS for all four viewers; complete single-message images allow conflation without separating metadata from pixels |
| ZeroMQ + SHM | Three 921,728-byte slots per camera, nonblocking exclusive writer/shared reader locks, conflated descriptor notifications | Approximately 30 FPS for all four viewers; duplicate readers, slot exhaustion, killed readers and publisher restart passed integration checks |
| Vulkan external GPU buffers + CUDA | Three exportable slots per viewer channel, ready semaphore plus explicit release acknowledgement | Approximately 30 FPS for all four viewers. This avoids image-byte transport and the CUDA consumer can process the device pointer directly. The OpenCV viewer downloads each frame to host memory. |

ZeroMQ buffer tuning used four real viewers, a 3-second warmup, and a 10-second measurement for each candidate:

| Requested buffer size | Viewer 1 FPS | Viewer 2 FPS | Viewer 3 FPS | Viewer 4 FPS |
| --- | ---: | ---: | ---: | ---: |
| 64 KiB | 29.98 | 30.01 | 30.05 | 30.02 |
| 256 KiB | 29.98 | 30.06 | 30.06 | 30.02 |
| 1 MiB | 29.97 | 30.04 | 29.93 | 29.96 |

**64 KiB was selected** because it was the smallest tested request for which every viewer averaged at least 29 FPS. Linux may adjust the actual kernel buffer sizes; this is not a 64 KiB total-memory limit.

## Recommendation so far

**Use ZeroMQ PUB/SUB over TCP as the default for the current demo.** It met the three-camera/four-viewer requirement at approximately 30 received FPS per viewer, including two subscribers to the same topic, without application shared memory. Its measured CPU and RSS were practical, and the configuration is already implemented and tested on branch `zmq` at commit `b2356e3`.

**Use ZeroMQ signaling plus SHM when you specifically want a local SHM image transport while retaining the existing ZeroMQ topics.** It also achieved approximately 30 FPS for four viewers. Godot CPU averaged 46.1% versus 48.4% in the earlier TCP run, while Godot RSS averaged 211.3 MiB versus 207.4 MiB. These small differences come from separate runs and do not establish a universal speed advantage. This implementation adds slot locking, stale-frame validation, and orphan cleanup responsibilities; Python still copies pixels for display. ZeroMQ TCP remains the simpler default when SHM is not required.

**Use Zenoh native SHM when you want middleware-managed shared-buffer lifetime and fanout.** It achieved the 30 FPS target in the three-viewer benchmark and separately passed duplicate-subscriber, restart, and buffer-lifetime checks. It manages shared-buffer lifetime and fanout, although this implementation's Python viewer still copies image bytes before display. Its measured process RSS was higher than the other implementations shown here.

The custom SHM version remains a useful small local example, but its measured implementation supports one viewer per camera and uses application ACKs to protect each reusable slot. Extending it to multiple independent readers would require additional ownership/signaling work.

**Keep the current Zenoh UDP version as an experimental comparison.** In its four-viewer run it averaged only 14.9–18.2 received FPS per viewer and used substantially more Godot CPU. These results do not establish that UDP or Zenoh is inherently slower; they describe the tested best-effort configuration.

**Use Vulkan external GPU buffers + CUDA when downstream work should stay on the NVIDIA GPU.** This run met the 30 FPS target for all four viewers and used 10.7% of one logical CPU for Godot. The OpenCV viewers each used about 81–84% of one logical CPU because they copy frames to host memory and display them. For an inference or CUDA pipeline, consume `GpuFrame.device_pointer` on the receiver stream and avoid that preview download. The channel-per-viewer setup duplicates output buffers and copies for camera 1, so it is less elegant for fanout than Zenoh SHM or ZeroMQ PUB/SUB.

The benchmark uses 1 Hz sampling after a 3-second warmup. CPU is process CPU time as a percentage of one logical CPU (16 are available). RSS includes shared pages, and NVIDIA utilization/VRAM are whole-device readings. The desktop and other processes were active during the run. The first startup pattern is not part of the steady-state interval after warmup.

## Comparison limits

The historical custom SHM and Zenoh SHM benchmarks used **three viewers**; the other rows used **four**. These were separate desktop runs with background applications, not a controlled transport-only comparison. The new GPU-sharing run used RGBA8 while the historical image transports used RGB8. NVIDIA utilization and memory readings cover the whole GPU, including the desktop and other clients. In particular, lower GPU utilization does not prove a more efficient transport.

The cameras were capped at 30 FPS. The results show whether that target was met in these runs, not the maximum supported FPS. No end-to-end latency benchmark, controlled packet-loss study, or long-duration stability test was performed. Small queues and PUB/SUB policies can discard publications under load; TCP does not make the complete application lossless.

## Existing local measurement artifacts

These directories contain the previously generated reports, summaries, samples, or logs. They are ignored build artifacts and are not included in Git:

- Custom SHM: `build/monitor-nvidia-three-cameras-20261016-233950/`
- Zenoh SHM: `build/monitor-zenoh-nvidia-20261017-072849/`
- Zenoh UDP: `build/monitor-udp-nvidia-20261017-080436/`
- ZeroMQ TCP: `build/benchmark-zmq-65536-20261017-082206-156146/`
- ZeroMQ tuning: `build/zmq-buffer-tuning.json`
- Vulkan/CUDA external GPU memory: `build/gpu-sharing-20261017-144118/` (30 one-second samples, process logs, NVIDIA telemetry and JSON summary)

The run can be repeated with `.venv/bin/python tests/benchmark_gpu_sharing.py --seconds 30 --warmup 3`. It starts the custom Godot runtime and four real OpenCV viewers, then writes the raw process/GPU samples and logs under ignored `build/`.

- ZeroMQ-signaled SHM: `build/benchmark-zmq-shm-65536-20261017-102611-347592/`
