import json
import unittest
from types import SimpleNamespace

from viewer import FRAME_BYTES, FrameReceiver, ReceivedFPS


class Bytes:
    def __init__(self, data, shm=True):
        self.data, self.shm = data, shm
    def __len__(self):
        return len(self.data)
    def as_shm(self):
        return self if self.shm else None
    def to_bytes(self):
        return bytes(self.data)


class ReceiverTest(unittest.TestCase):
    def setUp(self):
        self.receiver = FrameReceiver.__new__(FrameReceiver)
        self.receiver.camera_id = 1
        self.receiver.frame_id = 0
        self.receiver.stream_id = None
        self.metadata = dict(version=1, camera_id=1, width=640, height=480,
                             format='RGB8', frame_id=10, capture_usec=123, stream_id='abc')

    def sample(self, **changes):
        metadata = {**self.metadata, **changes}
        return SimpleNamespace(payload=Bytes(bytearray([42]) * FRAME_BYTES),
                               attachment=Bytes(json.dumps(metadata).encode()))

    def test_private_copy_and_restart(self):
        sample = self.sample()
        frame = self.receiver.decode(sample)
        sample.payload.data[:] = b'\x00' * FRAME_BYTES
        self.assertEqual(frame.shape, (480, 640, 3))
        self.assertTrue((frame == 42).all())
        with self.assertRaisesRegex(ValueError, 'increase'):
            self.receiver.decode(self.sample())
        self.receiver.decode(self.sample(stream_id='new-session', frame_id=1))
        self.assertEqual(self.receiver.frame_id, 1)

    def test_rejects_raw_payload(self):
        sample = self.sample()
        sample.payload.shm = False
        with self.assertRaisesRegex(ValueError, 'SHM required'):
            self.receiver.decode(sample)

    def test_rejects_invalid_metadata(self):
        for changes in [dict(format='RGBA8'), dict(camera_id=2), dict(frame_id=True),
                        dict(capture_usec=-1), dict(stream_id=''), dict(version=2),
                        dict(version=True), dict(camera_id=True)]:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.receiver.decode(self.sample(**changes))

    def test_rejects_invalid_length_and_attachment(self):
        sample = self.sample()
        sample.payload = Bytes(b'short')
        with self.assertRaisesRegex(ValueError, 'length'):
            self.receiver.decode(sample)
        sample.attachment = None
        with self.assertRaisesRegex(ValueError, 'metadata'):
            self.receiver.decode(sample)


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
