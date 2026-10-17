#!/usr/bin/env python3
"""View the local Godot camera; pixels use shared memory, notifications use TCP."""

import argparse
import json
import mmap
import os
import re
import select
import socket
import stat
import struct
import time

import cv2
import numpy as np

ADDRESS = ("127.0.0.1", 5001)
WIDTH, HEIGHT = 640, 480
FRAME_BYTES = WIDTH * HEIGHT * 3
READY = struct.Struct("<QQ")
ACK = struct.Struct("<Q")


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
    """Nonblocking, single-slot READY / copy / ACK consumer."""

    def __init__(self, sock):
        self.sock = sock
        self.sock.setblocking(False)
        self.incoming = bytearray()
        self.outgoing = bytearray()
        self.memory = None
        self.frame_id = 0
        self.capture_usec = 0
        self.last_frame_time = None

    def _open_memory(self, payload):
        metadata = json.loads(payload)
        if not isinstance(metadata, dict) or any(
            metadata.get(key) != expected
            for key, expected in {
                "version": 1, "width": WIDTH, "height": HEIGHT,
                "format": "RGB8", "bytes": FRAME_BYTES,
            }.items()
        ):
            raise ValueError("Unsupported camera metadata")
        name = metadata.get("shm")
        if not isinstance(name, str) or not re.fullmatch(r"/godot_camera_[0-9]+_[0-9]+", name):
            raise ValueError("Invalid shared-memory name")
        fd = os.open("/dev/shm" + name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size != FRAME_BYTES):
                raise ValueError("Shared-memory ownership, permissions, or size mismatch")
            self.memory = mmap.mmap(fd, FRAME_BYTES, access=mmap.ACCESS_READ)
        finally:
            os.close(fd)

    def _flush_ack(self):
        if self.outgoing:
            try:
                sent = self.sock.send(self.outgoing)
                if sent == 0:
                    raise ConnectionError("Publisher disconnected during ACK")
                del self.outgoing[:sent]
            except BlockingIOError:
                pass

    def poll(self):
        """Return a private RGB frame when READY arrives, otherwise None."""
        self._flush_ack()
        try:
            data = self.sock.recv(4096)
        except BlockingIOError:
            data = None
        if data == b"":
            raise ConnectionError("Publisher disconnected")
        if data:
            self.incoming.extend(data)
        if self.memory is None:
            if len(self.incoming) < 4:
                return None
            length = struct.unpack_from("<I", self.incoming)[0]
            if not 1 <= length <= 1024:
                raise ValueError("Invalid handshake length")
            if len(self.incoming) < 4 + length:
                return None
            self._open_memory(bytes(self.incoming[4:4 + length]))
            del self.incoming[:4 + length]
        if len(self.incoming) < READY.size:
            return None
        if len(self.incoming) != READY.size or self.outgoing:
            raise ValueError("Publisher sent multiple frames without ACK")
        frame_id, capture_usec = READY.unpack(self.incoming)
        if frame_id <= self.frame_id:
            raise ValueError("Frame IDs must increase")
        # The copy must finish before ACK; a NumPy view alone would keep reading the shared slot.
        frame = np.frombuffer(self.memory, dtype=np.uint8).reshape(HEIGHT, WIDTH, 3).copy()
        self.incoming.clear()
        self.frame_id = frame_id
        self.capture_usec = capture_usec
        self.last_frame_time = time.monotonic()
        self.outgoing.extend(ACK.pack(frame_id))
        self._flush_ack()
        return frame

    def close(self):
        self.sock.close()
        if self.memory is not None:
            self.memory.close()
            self.memory = None


def draw_overlay(image, fps, status, frame_id):
    result = image.copy()
    cv2.rectangle(result, (0, 0), (WIDTH, 68), (20, 20, 20), -1)
    cv2.putText(result, f"Received FPS: {fps:.1f}", (12, 27),
                cv2.FONT_HERSHEY_SIMPLEX, 0.7, (80, 240, 100), 2, cv2.LINE_AA)
    cv2.putText(result, f"{status} | Frame: {frame_id}", (12, 54),
                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (240, 240, 240), 1, cv2.LINE_AA)
    return result


def main(port=ADDRESS[1]):
    address = (ADDRESS[0], port)
    window = f"Godot camera {port} - Q / Esc to quit"
    receiver = None
    fps = ReceivedFPS(time.monotonic())
    image = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    next_connect = 0.0
    last_report = time.monotonic()
    cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
    print(f"Waiting for Godot on {address[0]}:{address[1]}; Q / Esc to quit", flush=True)
    try:
        while True:
            now = time.monotonic()
            if receiver is None and now >= next_connect:
                try:
                    sock = socket.create_connection(address, timeout=0.1)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                    receiver = FrameReceiver(sock)
                    fps = ReceivedFPS(time.monotonic())
                    print("Connected to Godot", flush=True)
                except OSError:
                    next_connect = now + 1.0
            if receiver is not None:
                try:
                    frame = receiver.poll()
                    if frame is not None:
                        fps.record()
                        image = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                except (OSError, ValueError, UnicodeError) as error:
                    print(f"Stream closed: {error}; retrying", flush=True)
                    receiver.close()
                    receiver = None
                    next_connect = now + 1.0
            now = time.monotonic()
            fps.update(now)
            status = "Waiting for frames"
            frame_id = 0
            if receiver is not None:
                frame_id = receiver.frame_id
                if receiver.last_frame_time is not None:
                    status = "Live" if now - receiver.last_frame_time < 1.0 else "No frames"
            cv2.imshow(window, draw_overlay(image, fps.value, status, frame_id))
            if now - last_report >= 1.0:
                print(f"Received FPS: {fps.value:.1f} | {status} | Frame: {frame_id}", flush=True)
                last_report = now
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), 27):
                break
            try:
                visible = cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE)
            except cv2.error:
                # The Qt backend raises instead of returning zero after its last window closes.
                break
            if visible < 1:
                break
            if receiver is not None:
                select.select([receiver.sock], [receiver.sock] if receiver.outgoing else [], [], 0.005)
            else:
                time.sleep(0.005)
    except KeyboardInterrupt:
        pass
    finally:
        if receiver is not None:
            receiver.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, choices=(5001, 5002, 5003), default=5001,
                        help="Camera publisher port (default: 5001)")
    main(parser.parse_args().port)
