#!/usr/bin/env python3
"""Display a local Zenoh UDP RGB8 camera stream."""
import argparse
import json
import os
import time

import cv2
import numpy as np
import zenoh

WIDTH, HEIGHT = 640, 480
FRAME_BYTES = WIDTH * HEIGHT * 3


class ReceivedFPS:
    """Count completed frame copies per elapsed second, including idle intervals."""

    def __init__(self, now):
        self.started = now
        self.frames = 0
        self.value = 0.0

    def record(self):
        self.frames += 1

    def update(self, now):
        elapsed = now - self.started
        if elapsed >= 1.0:
            self.value = self.frames / elapsed
            self.frames = 0
            self.started = now


class FrameReceiver:
    """Latest-only subscriber for complete Zenoh UDP image samples."""

    def __init__(self, camera_id=1):
        if camera_id not in (1, 2, 3):
            raise ValueError("Camera ID must be 1, 2, or 3")
        self.camera_id = camera_id
        self.frame_id = 0
        self.stream_id = None
        self.capture_usec = 0
        self.last_frame_time = None
        config = zenoh.Config()
        for key, value in {
            "mode": '"peer"', "listen/endpoints": "[]",
            "connect/endpoints": '["udp/127.0.0.1:7447"]',
            "connect/timeout_ms": "-1", "connect/exit_on_failure": "false",
            "scouting/multicast/enabled": "false",
            "transport/shared_memory/enabled": "false",
            "transport/link/protocols": '["udp"]',
            "transport/link/tx/batch_size": "8192",
        }.items():
            config.insert_json5(key, value)
        self.session = zenoh.open(config)
        self.subscriber = self.session.declare_subscriber(
            f"godot/camera/{camera_id}/image", zenoh.handlers.RingChannel(1))

    def decode(self, sample):
        if sample.payload.as_shm() is not None:
            raise ValueError("UDP images required: received an unexpected SHM payload")
        if sample.attachment is None or len(sample.attachment) > 1024:
            raise ValueError("Missing or oversized camera metadata")
        metadata = json.loads(sample.attachment.to_bytes())
        expected = {"version": 1, "camera_id": self.camera_id,
                    "width": WIDTH, "height": HEIGHT, "format": "RGB8"}
        if not isinstance(metadata, dict) or any(type(metadata.get(k)) is not type(v) or metadata[k] != v
                                                   for k, v in expected.items()):
            raise ValueError("Unsupported camera metadata")
        stream_id = metadata.get("stream_id")
        frame_id, capture_usec = metadata.get("frame_id"), metadata.get("capture_usec")
        if (not isinstance(stream_id, str) or not stream_id or len(stream_id) > 64
                or type(frame_id) is not int or frame_id <= 0
                or type(capture_usec) is not int or capture_usec < 0):
            raise ValueError("Invalid frame identity or timestamp")
        if len(sample.payload) != FRAME_BYTES:
            raise ValueError("Invalid RGB8 payload length")
        if stream_id == self.stream_id and frame_id <= self.frame_id:
            return None
        # Own the image independently of Zenoh samples and their receive buffers.
        pixels = sample.payload.to_bytes()
        self.stream_id, self.frame_id = stream_id, frame_id
        self.capture_usec = capture_usec
        self.last_frame_time = time.monotonic()
        return np.frombuffer(pixels, dtype=np.uint8).reshape(HEIGHT, WIDTH, 3)

    def poll(self):
        sample = self.subscriber.try_recv()
        return None if sample is None else self.decode(sample)

    def close(self):
        self.subscriber.undeclare()
        self.session.close()


def draw_overlay(image, fps, status, frame_id):
    result = image.copy()
    cv2.rectangle(result, (0, 0), (WIDTH, 68), (20, 20, 20), -1)
    cv2.putText(result, f"Received FPS: {fps:.1f}", (12, 27),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (80, 240, 100), 2, cv2.LINE_AA)
    cv2.putText(result, f"{status} | Frame: {frame_id}", (12, 54),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (240, 240, 240), 1, cv2.LINE_AA)
    return result


def main(camera_id=1):
    window = f"Godot UDP camera {camera_id} ({os.getpid()}) - Q / Esc to quit"
    receiver = FrameReceiver(camera_id)
    fps = ReceivedFPS(time.monotonic())
    image = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    last_report = time.monotonic()
    cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
    print(f"Subscribed to godot/camera/{camera_id}/image; UDP best effort, SHM disabled", flush=True)
    try:
        while True:
            frame = receiver.poll()
            if frame is not None:
                fps.record()
                image = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            now = time.monotonic()
            fps.update(now)
            status = "Waiting for frames"
            if receiver.last_frame_time is not None:
                status = "Live (UDP)" if now - receiver.last_frame_time < 1.0 else "No frames"
            cv2.imshow(window, draw_overlay(image, fps.value, status, receiver.frame_id))
            if now - last_report >= 1.0:
                print(f"Received FPS: {fps.value:.1f} | {status} | Frame: {receiver.frame_id}", flush=True)
                last_report = now
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            try:
                if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                    break
            except cv2.error:
                break
            time.sleep(0.005)
    except KeyboardInterrupt:
        pass
    except (ValueError, zenoh.ZError) as error:
        raise SystemExit(f"Camera stream rejected: {error}") from error
    finally:
        receiver.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", type=int, choices=(1, 2, 3), default=1)
    main(parser.parse_args().camera)
