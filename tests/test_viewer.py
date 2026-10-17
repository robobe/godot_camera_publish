import json
import os
import struct
import unittest
from unittest.mock import patch

from viewer import FRAME_BYTES, FrameReceiver, ReceivedFPS, buffer_bytes


class ReceiverTest(unittest.TestCase):
    def setUp(self):
        self.receiver = FrameReceiver.__new__(FrameReceiver)
        self.receiver.camera_id = 1
        self.receiver.topic = b'godot/camera/1/image\0'
        self.receiver.frame_id = 0
        self.receiver.stream_id = None
        self.metadata = dict(version=1, camera_id=1, width=640, height=480,
                             format='RGB8', frame_id=10, capture_usec=123, stream_id='abc')

    def message(self, **changes):
        metadata = json.dumps({**self.metadata, **changes}).encode()
        return self.receiver.topic + struct.pack('<I', len(metadata)) + metadata + bytes([42]) * FRAME_BYTES

    def test_private_pixels_stale_frames_and_restart(self):
        message = bytearray(self.message())
        frame = self.receiver.decode(message)
        message[:] = bytes(len(message))
        self.assertEqual(frame.shape, (480, 640, 3))
        self.assertTrue((frame == 42).all())
        last_time = self.receiver.last_frame_time
        for frame_id in (10, 9):
            self.assertIsNone(self.receiver.decode(self.message(frame_id=frame_id)))
        self.assertEqual(self.receiver.last_frame_time, last_time)
        self.assertEqual(self.receiver.frame_id, 10)
        self.assertIsNotNone(self.receiver.decode(self.message(stream_id='new', frame_id=1)))

    def test_topic_isolation(self):
        with self.assertRaisesRegex(ValueError, 'topic'):
            self.receiver.decode(self.message().replace(b'/1/image\0', b'/2/image\0', 1))
        with self.assertRaisesRegex(ValueError, 'topic'):
            self.receiver.decode(self.message().replace(b'/1/image\0', b'/1/image-extra\0', 1))

    def test_invalid_metadata(self):
        for changes in [dict(format='RGBA8'), dict(camera_id=2), dict(frame_id=True),
                        dict(capture_usec=-1), dict(stream_id=''), dict(version=True)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.receiver.decode(self.message(**changes))

    def test_malformed_framing(self):
        topic = self.receiver.topic
        for message in [topic, topic+b'\x01', topic+struct.pack('<I', 1025),
                        topic+struct.pack('<I', 0), topic+struct.pack('<I', 10)+b'{}',
                        topic+struct.pack('<I', 2)+b'[]'+bytes(FRAME_BYTES),
                        self.message()[:-1], self.message()+b'x', self.message()*2]:
            with self.subTest(length=len(message)), self.assertRaises(ValueError):
                self.receiver.decode(message)

    def test_invalid_buffer_settings(self):
        for value in ('', 'abc', '1', '4194305', '-1024', '+1024', ' 1024', '1024.0', '１２３４'):
            with self.subTest(value=value), patch.dict(os.environ, CAMERA_ZMQ_BUFFER_BYTES=value):
                with self.assertRaises(ValueError):
                    buffer_bytes()
        for value in ('1024', '65536', '262144', '1048576', '4194304'):
            with patch.dict(os.environ, CAMERA_ZMQ_BUFFER_BYTES=value):
                self.assertEqual(buffer_bytes(), int(value))


class FPSTest(unittest.TestCase):
    def test_elapsed_time_and_idle(self):
        fps = ReceivedFPS(10)
        for _ in range(60):
            fps.record()
        fps.update(11.2)
        self.assertAlmostEqual(fps.value, 50)
        fps.update(12.3)
        self.assertEqual(fps.value, 0)


if __name__ == '__main__':
    unittest.main()
