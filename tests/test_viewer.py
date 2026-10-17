import fcntl
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from viewer import (FRAME_BYTES, HEADER, MAGIC, SLOT_BYTES, FrameReceiver, ReceivedFPS,
                    buffer_bytes)


class ReceiverTest(unittest.TestCase):
    def setUp(self):
        self.receiver = FrameReceiver.__new__(FrameReceiver)
        self.receiver.camera_id = 1
        self.receiver.topic = b'godot/camera/1/image\0'
        self.receiver.frame_id = 0
        self.receiver.stream_id = None
        self.receiver.last_frame_time = None
        self.receiver.mappings = {}
        self.stream = f'{os.getpid()}-{time.monotonic_ns()}'
        self.name = f'/godot_camera_zmq_{self.stream}_c1_s0'
        self.path = Path('/dev/shm' + self.name)
        self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.ftruncate(self.fd, SLOT_BYTES)
        self.metadata = dict(version=2, camera_id=1, width=640, height=480, bytes=FRAME_BYTES,
                             format='RGB8', frame_id=10, capture_usec=123, stream_id=self.stream,
                             slot=0, shm_name=self.name)
        self.write_header()
        os.pwrite(self.fd, bytes([42])*FRAME_BYTES, HEADER.size)

    def tearDown(self):
        self.receiver._close_mappings()
        os.close(self.fd)
        self.path.unlink(missing_ok=True)

    def write_header(self, **changes):
        values = dict(magic=MAGIC, version=2, camera_id=1, slot=0, width=640, height=480,
                      fmt=1, size=FRAME_BYTES, frame_id=10, capture_usec=123, valid=1,
                      stream=self.stream.encode())
        values.update(changes)
        os.pwrite(self.fd, HEADER.pack(*values.values()), 0)

    def message(self, **changes):
        metadata = json.dumps({**self.metadata, **changes}).encode()
        return self.receiver.topic + struct.pack('<I', len(metadata)) + metadata

    def test_private_pixels_and_stale_notifications(self):
        frame = self.receiver.decode(self.message())
        os.pwrite(self.fd, bytes(FRAME_BYTES), HEADER.size)
        self.assertEqual(frame.shape, (480, 640, 3))
        self.assertTrue((frame == 42).all())
        last_time = self.receiver.last_frame_time
        for frame_id in (10, 9):
            self.assertIsNone(self.receiver.decode(self.message(frame_id=frame_id)))
        self.assertEqual(self.receiver.last_frame_time, last_time)
        self.assertEqual(self.receiver.frame_id, 10)

    def test_incomplete_and_reused_slots_are_skipped(self):
        self.write_header(valid=0)
        self.assertIsNone(self.receiver.decode(self.message()))
        self.write_header(frame_id=11)
        self.assertIsNone(self.receiver.decode(self.message()))
        self.assertEqual(self.receiver.frame_id, 0)
        self.assertIsNotNone(self.receiver.decode(self.message(frame_id=11)))

    def test_header_corruption_is_rejected(self):
        for changes in (dict(magic=b'bad'), dict(width=1), dict(camera_id=2), dict(slot=2),
                        dict(valid=2), dict(stream=b'other'), dict(capture_usec=999)):
            with self.subTest(changes=changes):
                self.write_header(**changes)
                with self.assertRaises(ValueError):
                    self.receiver.decode(self.message())

    def test_restart_closes_old_mappings(self):
        self.receiver.decode(self.message())
        mapping = self.receiver.mappings[self.name]
        new_stream = self.stream+'1'
        self.assertIsNone(self.receiver.decode(self.message(
            stream_id=new_stream, frame_id=1, shm_name=f'/godot_camera_zmq_{new_stream}_c1_s0')))
        self.assertTrue(mapping.memory.closed)
        self.assertEqual(self.receiver.mappings, {})

    def test_topic_metadata_and_old_payload_are_rejected(self):
        for changes in (dict(version=1), dict(camera_id=True), dict(slot=True),
                        dict(slot=3), dict(shm_name='/../../etc/passwd'), dict(bytes=1),
                        dict(stream_id='bad'), dict(frame_id=True), dict(capture_usec=-1)):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.receiver.decode(self.message(**changes))
        for message in (self.message().replace(b'/1/image\0', b'/2/image\0', 1),
                        self.message()+b'x', self.message()+bytes(FRAME_BYTES),
                        self.receiver.topic, self.receiver.topic+struct.pack('<I', 1025)):
            with self.subTest(length=len(message)), self.assertRaises(ValueError):
                self.receiver.decode(message)

    def test_invalid_shm_permissions_and_size(self):
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'permissions'):
            self.receiver.decode(self.message())
        self.path.chmod(0o600)
        os.ftruncate(self.fd, 10)
        with self.assertRaisesRegex(ValueError, 'size'):
            self.receiver.decode(self.message())

    def test_fifo_is_rejected_without_waiting(self):
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaisesRegex(ValueError, 'ownership, permissions, or size'):
            self.receiver.decode(self.message())

    def test_symlink_is_not_followed(self):
        self.path.unlink()
        self.path.symlink_to('/etc/passwd')
        with self.assertRaises(OSError):
            self.receiver.decode(self.message())

    def test_writer_crash_releases_lock_but_incomplete_image_is_not_read(self):
        code = '''import fcntl,os,sys,time
fd=os.open(sys.argv[1],os.O_RDWR)
fcntl.flock(fd,fcntl.LOCK_EX)
os.pwrite(fd,bytes(8),56)
os.pwrite(fd,b'partial',128)
print('locked',flush=True)
time.sleep(20)
'''
        child = subprocess.Popen([sys.executable, '-c', code, str(self.path)], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), 'locked')
            self.assertIsNone(self.receiver.decode(self.message()))
            child.kill()
            child.wait(timeout=5)
            self.assertIsNone(self.receiver.decode(self.message()))
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.write_header()
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            self.assertIsNotNone(self.receiver.decode(self.message()))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()

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
