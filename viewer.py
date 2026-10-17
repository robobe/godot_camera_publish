#!/usr/bin/env python3
"""Display a local ZeroMQ TCP RGB8 camera stream."""
import argparse
import json
import os
import time

import cv2
import numpy as np
import struct
import zmq

WIDTH, HEIGHT = 640, 480
FRAME_BYTES = WIDTH * HEIGHT * 3
ENDPOINT = "tcp://127.0.0.1:5555"
DEFAULT_BUFFER_BYTES = 65536
MAX_MESSAGE_BYTES = 1024 * 1024


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


def buffer_bytes():
    value = os.environ.get("CAMERA_ZMQ_BUFFER_BYTES", str(DEFAULT_BUFFER_BYTES))
    if not value.isascii() or not value.isdecimal() or not 1024 <= int(value) <= 4194304:
        raise ValueError("CAMERA_ZMQ_BUFFER_BYTES must be an integer from 1024 through 4194304")
    return int(value)


class FrameReceiver:
    """One camera per SUB socket, retaining the latest complete message."""

    def __init__(self, camera_id=1):
        if type(camera_id) is not int or camera_id not in (1, 2, 3):
            raise ValueError("Camera ID must be 1, 2, or 3")
        size = buffer_bytes()
        self.camera_id = camera_id
        self.topic = f"godot/camera/{camera_id}/image".encode() + b"\0"
        self.frame_id = 0
        self.stream_id = None
        self.capture_usec = 0
        self.last_frame_time = None
        self.context = zmq.Context()
        self.sock = self.context.socket(zmq.SUB)
        try:
            self.sock.setsockopt(zmq.CONFLATE, 1)
            self.sock.setsockopt(zmq.LINGER, 0)
            self.sock.setsockopt(zmq.RCVBUF, size)
            self.sock.setsockopt(zmq.MAXMSGSIZE, MAX_MESSAGE_BYTES)
            self.sock.setsockopt(zmq.SUBSCRIBE, self.topic)
            self.sock.connect(ENDPOINT)
        except Exception:
            self.close()
            raise

    def decode(self, message):
        if len(message) > MAX_MESSAGE_BYTES or not message.startswith(self.topic):
            raise ValueError("Invalid camera topic or oversized message")
        offset = len(self.topic)
        if len(message) < offset + 4:
            raise ValueError("Truncated metadata length")
        length = struct.unpack_from("<I", message, offset)[0]
        start = offset + 4
        if not 1 <= length <= 1024 or len(message) < start + length:
            raise ValueError("Invalid metadata length")
        metadata = json.loads(message[start:start + length])
        expected = {"version": 1, "camera_id": self.camera_id,
                    "width": WIDTH, "height": HEIGHT, "format": "RGB8"}
        if not isinstance(metadata, dict) or any(type(metadata.get(k)) is not type(v) or metadata[k] != v
                                                for k, v in expected.items()):
            raise ValueError("Unsupported camera metadata")
        stream_id = metadata.get("stream_id")
        frame_id, capture_usec = metadata.get("frame_id"), metadata.get("capture_usec")
        if (not isinstance(stream_id, str) or not stream_id or len(stream_id) > 128
                or type(frame_id) is not int or frame_id <= 0
                or type(capture_usec) is not int or capture_usec < 0):
            raise ValueError("Invalid frame identity or timestamp")
        if len(message) - start - length != FRAME_BYTES:
            raise ValueError("Invalid RGB8 payload length")
        if stream_id == self.stream_id and frame_id <= self.frame_id:
            return None
        # recv() already owns immutable bytes, so NumPy can retain them without another pixel copy.
        message = bytes(message)
        self.stream_id, self.frame_id = stream_id, frame_id
        self.capture_usec = capture_usec
        self.last_frame_time = time.monotonic()
        return np.frombuffer(message, dtype=np.uint8, offset=start + length).reshape(HEIGHT, WIDTH, 3)

    def poll(self):
        try:
            message = self.sock.recv(flags=zmq.NOBLOCK)
        except zmq.Again:
            return None
        if self.sock.getsockopt(zmq.RCVMORE):
            raise ValueError("Multipart camera messages are unsupported")
        return self.decode(message)

    def close(self):
        self.sock.close(linger=0)
        self.context.term()


def draw_overlay(image, fps, status, frame_id):
    result = image.copy()
    cv2.rectangle(result, (0, 0), (WIDTH, 68), (20, 20, 20), -1)
    cv2.putText(result, f"Received FPS: {fps:.1f}", (12, 27),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (80, 240, 100), 2, cv2.LINE_AA)
    cv2.putText(result, f"{status} | Frame: {frame_id}", (12, 54),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (240, 240, 240), 1, cv2.LINE_AA)
    return result


def main(camera_id=1):
    window = f"Godot ZeroMQ camera {camera_id} ({os.getpid()}) - Q / Esc to quit"
    receiver = FrameReceiver(camera_id)
    fps = ReceivedFPS(time.monotonic())
    image = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    last_report = time.monotonic()
    cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
    print(f"Subscribed to godot/camera/{camera_id}/image; ZeroMQ TCP, no SHM", flush=True)
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
                status = "Live (ZeroMQ)" if now - receiver.last_frame_time < 1.0 else "No frames"
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
    except (ValueError, zmq.ZMQError) as error:
        raise SystemExit(f"Camera stream rejected: {error}") from error
    finally:
        receiver.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--camera", type=int, choices=(1, 2, 3), default=1)
    main(parser.parse_args().camera)
