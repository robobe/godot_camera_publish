# Camera transport performance summary

The historical custom SHM, Zenoh, and ZeroMQ TCP rows were summarized from results already reported in this conversation without rerunning them. The ZeroMQ-signaled SHM row was subsequently added from a new 30-second benchmark on branch `zmq_shm`.

All runs below measured 30 seconds after startup and warmup, using the NVIDIA GeForce RTX 5070 Laptop GPU, three 640×480 RGB8 cameras, a 30 FPS target, and 90° horizontal FOV. Cameras 2 and 3 were offset 1 m and 2 m to camera 1's right. Measurements were recorded on 2026-10-16 and 2026-10-17.

## Average performance

| Method | Viewers | Received FPS per viewer | Godot CPU | CPU per viewer | Godot RAM (RSS) | RAM per viewer (RSS) | NVIDIA GPU utilization | NVIDIA VRAM |
| --- | ---: | --- | ---: | --- | ---: | --- | ---: | ---: |
| Custom POSIX SHM + TCP READY/ACK | 3 | 30.0 / 29.9 / 29.9 | 44.4% | 49.2–49.6% | 199.9 MiB | 211.0–215.1 MiB | 7.1% | 114 MiB |
| Zenoh native SHM + TCP coordination | 3 | 30.0 / 30.0 / 30.0 | 44.1% | 44.8–45.5% | 246.8 MiB | 258.1–259.3 MiB | 10.6% | 116 MiB |
| Zenoh best-effort UDP, SHM disabled | 4 | 14.9 / 17.1 / 18.2 / 17.7 | 85.8% | 48.6–49.8% | 210.9 MiB | 223.2–224.2 MiB | 7.5% | 114 MiB |
| ZeroMQ PUB/SUB over TCP, no SHM | 4 | 30.02 / 30.03 / 29.98 / 29.99 | 48.4% | 46.1–46.7% | 207.4 MiB | 212.5–215.6 MiB | 10.9% | 114 MiB |
| ZeroMQ signaling + three SHM slots per camera | 4 | 30.01 / 30.01 / 30.00 / 30.03 | 46.1% | 46.0–46.6% | 211.3 MiB | 213.7–215.2 MiB | 9.5% | 114 MiB |

All table entries are averages. Viewer CPU and RAM ranges describe the averages across individual viewer processes, not fluctuations over time. The four-viewer runs subscribed to cameras **1, 2, 3, 1**, so two independent processes received camera 1.

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

## Comparison limits

The custom SHM and Zenoh SHM benchmarks used **three viewers**; ZeroMQ TCP, ZeroMQ-signaled SHM, and Zenoh UDP used **four**. These were separate desktop runs with background applications, not a controlled transport-only comparison. In particular, lower GPU utilization does not prove a more efficient transport.

The cameras were capped at 30 FPS. The results show whether that target was met in these runs, not the maximum supported FPS. No end-to-end latency benchmark, controlled packet-loss study, or long-duration stability test was performed. Small queues and PUB/SUB policies can discard publications under load; TCP does not make the complete application lossless.

## Existing local measurement artifacts

These directories contain the previously generated reports, summaries, samples, or logs. They are ignored build artifacts and are not included in Git:

- Custom SHM: `build/monitor-nvidia-three-cameras-20261016-233950/`
- Zenoh SHM: `build/monitor-zenoh-nvidia-20261017-072849/`
- Zenoh UDP: `build/monitor-udp-nvidia-20261017-080436/`
- ZeroMQ TCP: `build/benchmark-zmq-65536-20261017-082206-156146/`
- ZeroMQ tuning: `build/zmq-buffer-tuning.json`

- ZeroMQ-signaled SHM: `build/benchmark-zmq-shm-65536-20261017-102611-347592/`
