import array
import os
import socket
import unittest

import numpy as np

from gpu_receiver import (
    FRAME_BYTES,
    HEIGHT,
    READY,
    RELEASE,
    SETUP,
    WIDTH,
    GpuReceiver,
    address_for,
    decode_setup,
    receive_packet,
    verify_pattern,
)
from viewer import ReceivedFPS


def setup_packet():
    return SETUP.pack(
        b"GPG1", 1, WIDTH, HEIGHT, 3, FRAME_BYTES, b"1" * 16, *([FRAME_BYTES] * 3)
    )


class FakeCuda:
    def __init__(self, uuid, sizes, fds):
        self.pointers = [100, 200, 300]
        self.waits = []
        self.synced = 0
        self.closed = False

    def wait(self, slot):
        self.waits.append(slot)

    def synchronize(self):
        self.synced += 1

    def close(self):
        self.closed = True


class ReceiverTest(unittest.TestCase):
    def setUp(self):
        self.server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        fds = [os.open("/dev/null", os.O_RDONLY) for _ in range(6)]
        try:
            self.server.sendmsg(
                [setup_packet()],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))],
            )
        finally:
            for fd in fds:
                os.close(fd)
        self.receiver = GpuReceiver(client, FakeCuda)
        self.receiver.sock.setblocking(False)

    def tearDown(self):
        self.receiver.close()
        self.server.close()

    def ready(self, slot=0, frame=1):
        self.server.send(READY.pack(b"RDY1", slot, 0, frame, 123))

    def test_release_synchronizes_before_ack_and_prevents_duplicate_release(self):
        self.assertIsNone(self.receiver.acquire())
        self.ready()
        frame = self.receiver.acquire()
        self.assertEqual(frame.device_pointer, 100)
        self.assertEqual(self.receiver.cuda.waits, [0])
        self.receiver.release(frame)
        self.assertEqual(self.receiver.cuda.synced, 1)
        self.assertEqual(self.server.recv(100), RELEASE.pack(b"REL1", 0, 1))
        with self.assertRaises(ValueError):
            self.receiver.release(frame)

    def test_all_three_slots_can_be_held_independently(self):
        frames = []
        for slot in range(3):
            self.ready(slot, slot + 1)
            frames.append(self.receiver.acquire())
        self.assertEqual(len(self.receiver.held), 3)
        for frame in reversed(frames):
            self.receiver.release(frame)
        self.assertEqual(self.receiver.held, {})

    def test_rejects_overwrite_of_held_slot(self):
        self.ready()
        self.receiver.acquire()
        self.ready(frame=2)
        with self.assertRaises(ValueError):
            self.receiver.acquire()

    def test_rejects_stale_and_oversized_notifications(self):
        self.ready()
        self.receiver.release(self.receiver.acquire())
        self.ready()
        with self.assertRaises(ValueError):
            self.receiver.acquire()
        self.server.send(b"x" * 100)
        with self.assertRaises(ValueError):
            self.receiver.acquire()

    def test_rejects_unexpected_descriptors_without_leaking(self):
        before = len(os.listdir("/proc/self/fd"))
        fd = os.open("/dev/null", os.O_RDONLY)
        try:
            self.server.sendmsg(
                [READY.pack(b"RDY1", 0, 0, 1, 123)],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))],
            )
        finally:
            os.close(fd)
        with self.assertRaises(ValueError):
            self.receiver.acquire()
        self.assertEqual(len(os.listdir("/proc/self/fd")), before)

    def test_disconnect_is_detected(self):
        self.server.close()
        with self.assertRaises(EOFError):
            self.receiver.acquire()


class ProtocolTest(unittest.TestCase):
    def test_channels_have_distinct_validated_addresses(self):
        self.assertNotEqual(address_for("camera1_a"), address_for("camera1_b"))
        with self.assertRaises(ValueError):
            address_for("camera/1")

    def test_setup_validates_layout_fd_count_uuid_and_allocation(self):
        decode_setup(setup_packet(), list(range(6)))
        for packet, fds in [
            (setup_packet(), []),
            (b"BUSY", []),
            (
                SETUP.pack(
                    b"GPG1",
                    1,
                    WIDTH,
                    HEIGHT,
                    3,
                    FRAME_BYTES,
                    bytes(16),
                    *([FRAME_BYTES] * 3),
                ),
                list(range(6)),
            ),
            (
                SETUP.pack(
                    b"GPG1",
                    1,
                    WIDTH,
                    HEIGHT,
                    3,
                    FRAME_BYTES,
                    b"1" * 16,
                    1,
                    FRAME_BYTES,
                    FRAME_BYTES,
                ),
                list(range(6)),
            ),
        ]:
            with self.assertRaises(ValueError):
                decode_setup(packet, fds)

    def test_oversized_setup_closes_received_fds(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        fd = os.open("/dev/null", os.O_RDONLY)
        try:
            before = len(os.listdir("/proc/self/fd"))
            a.sendmsg(
                [b"x" * 100],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))],
            )
            with self.assertRaises(ValueError):
                receive_packet(b, SETUP.size)
            self.assertEqual(len(os.listdir("/proc/self/fd")), before)
        finally:
            a.close()
            b.close()
            os.close(fd)

    def test_pattern_detects_channel_order_and_orientation(self):
        image = np.zeros((HEIGHT, WIDTH, 4), dtype=np.uint8)
        image[:, :, 0] = np.arange(WIDTH, dtype=np.uint16) % 256
        image[:, :, 1] = (np.arange(HEIGHT, dtype=np.uint16) % 256)[:, None]
        image[:, :, 2:] = [73, 255]
        verify_pattern(image)
        with self.assertRaises(ValueError):
            verify_pattern(image[::-1])
        with self.assertRaises(ValueError):
            verify_pattern(image[:, :, [2, 1, 0, 3]])

    def test_received_fps_uses_actual_interval(self):
        fps = ReceivedFPS(10)
        for _ in range(60):
            fps.record()
        fps.update(11.2)
        self.assertAlmostEqual(fps.value, 50)
        fps.update(12.3)
        self.assertEqual(fps.value, 0)


if __name__ == "__main__":
    unittest.main()
