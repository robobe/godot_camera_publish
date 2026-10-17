import json
import os
from pathlib import Path
import socket
import struct
import time
import unittest

import numpy as np

from viewer import ACK, FRAME_BYTES, HEIGHT, READY, WIDTH, FrameReceiver, ReceivedFPS


class FPSTest(unittest.TestCase):
    def test_counts_frames_over_actual_elapsed_time_and_resets_when_idle(self):
        fps = ReceivedFPS(10.0)
        for _ in range(60):
            fps.record()
        fps.update(10.5)
        self.assertEqual(fps.value, 0.0)
        fps.update(11.2)
        self.assertAlmostEqual(fps.value, 50.0)
        fps.update(12.3)
        self.assertEqual(fps.value, 0.0)


class ReceiverTest(unittest.TestCase):
    def setUp(self):
        self.producer, consumer = socket.socketpair()
        self.producer.settimeout(1)
        self.receiver = FrameReceiver(consumer)
        self.path = Path("/dev/shm") / f"godot_camera_{os.getpid()}_{time.monotonic_ns()}"
        self.file = self.path.open("xb")
        self.file.truncate(FRAME_BYTES)
        self.file.close()
        self.path.chmod(0o600)
        self.metadata = {
            "version": 1, "shm": "/" + self.path.name,
            "width": WIDTH, "height": HEIGHT, "format": "RGB8", "bytes": FRAME_BYTES,
        }

    def tearDown(self):
        self.receiver.close()
        self.producer.close()
        self.path.unlink()

    def handshake(self, metadata=None):
        payload = json.dumps(self.metadata if metadata is None else metadata).encode()
        return struct.pack("<I", len(payload)) + payload

    def test_fragmented_messages_copy_before_ack_and_single_count(self):
        message = self.handshake() + READY.pack(7, 123456)
        for byte in message[:-1]:
            self.producer.sendall(bytes([byte]))
            self.assertIsNone(self.receiver.poll())
        pixel = np.array([23, 77, 149], dtype=np.uint8)
        self.path.write_bytes(pixel.tobytes() * (WIDTH * HEIGHT))
        self.producer.sendall(message[-1:])
        frame = self.receiver.poll()
        self.assertEqual(frame.shape, (HEIGHT, WIDTH, 3))
        self.assertEqual(frame.nbytes, 921600)
        self.assertTrue(np.all(frame == pixel))
        self.assertEqual(self.producer.recv(8), ACK.pack(7))
        self.path.write_bytes(bytes([99]) * FRAME_BYTES)
        self.assertTrue(np.all(frame == pixel), "Returned frame must own its pixels")
        self.assertIsNone(self.receiver.poll())
        self.assertEqual(self.receiver.frame_id, 7)

    def test_invalid_metadata_and_path_are_rejected(self):
        self.producer.sendall(self.handshake({**self.metadata, "shm": "/../../etc/passwd"}))
        with self.assertRaisesRegex(ValueError, "name"):
            self.receiver.poll()

    def test_old_rgba_metadata_is_rejected(self):
        self.producer.sendall(self.handshake({
            **self.metadata, "format": "RGBA8", "bytes": WIDTH * HEIGHT * 4,
        }))
        with self.assertRaisesRegex(ValueError, "Unsupported camera metadata"):
            self.receiver.poll()

    def test_incorrect_buffer_size_is_rejected(self):
        self.path.write_bytes(b"short")
        self.producer.sendall(self.handshake())
        with self.assertRaisesRegex(ValueError, "size"):
            self.receiver.poll()

    def test_oversized_handshake_is_rejected(self):
        self.producer.sendall(struct.pack("<I", 1025))
        with self.assertRaisesRegex(ValueError, "length"):
            self.receiver.poll()

    def test_duplicate_frame_is_rejected(self):
        self.producer.sendall(self.handshake() + READY.pack(1, 100))
        self.assertIsNotNone(self.receiver.poll())
        self.receiver.poll()
        self.producer.recv(8)
        self.producer.sendall(READY.pack(1, 101))
        with self.assertRaisesRegex(ValueError, "increase"):
            self.receiver.poll()

    def test_disconnect_is_reported(self):
        self.producer.close()
        with self.assertRaises(ConnectionError):
            self.receiver.poll()


if __name__ == "__main__":
    unittest.main()
