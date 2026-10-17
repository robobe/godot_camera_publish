"""One-consumer Vulkan external-memory receiver, with CUDA stream ownership."""

from dataclasses import dataclass
import array
import ctypes
import os
import socket
import struct

import numpy as np

WIDTH, HEIGHT, SLOTS = 640, 480, 3
FRAME_BYTES = WIDTH * HEIGHT * 4
def address_for(channel="camera1"):
    if not channel or len(channel) > 48 or not all(
        c.isascii() and (c.isalnum() or c in "_-") for c in channel
    ):
        raise ValueError("GPU channel must contain 1-48 letters, digits, underscores or hyphens")
    return f"\0godot-camera-gpu-{os.getuid()}-{channel}"


ADDRESS = address_for("camera1_a")
SETUP = struct.Struct("<4s5I16s3Q")
READY = struct.Struct("<4sIIQQ")
RELEASE = struct.Struct("<4sIQ")


def receive_packet(sock, size):
    """Receive one bounded packet, closing any descriptors if decoding fails."""
    data, ancillary, flags, _ = sock.recvmsg(
        size, socket.CMSG_SPACE(6 * array.array("i").itemsize), socket.MSG_CMSG_CLOEXEC
    )
    fds = []
    try:
        for level, kind, content in ancillary:
            if level != socket.SOL_SOCKET or kind != socket.SCM_RIGHTS:
                raise ValueError("Unexpected control message")
            values = array.array("i")
            values.frombytes(content)
            fds.extend(values)
        if flags & (socket.MSG_TRUNC | socket.MSG_CTRUNC):
            raise ValueError("Truncated or oversized GPU packet")
        if not data:
            raise EOFError("GPU publisher disconnected")
        return data, fds
    except BaseException:
        for fd in fds:
            os.close(fd)
        raise


def decode_setup(data, fds):
    if data == b"BUSY":
        raise ValueError("GPU publisher already has a consumer")
    if len(data) != SETUP.size or len(fds) != SLOTS * 2:
        raise ValueError("Expected GPU setup and exactly six descriptors")
    magic, version, width, height, slots, size, uuid, *allocations = SETUP.unpack(data)
    if (magic, version, width, height, slots, size) != (
        b"GPG1",
        1,
        WIDTH,
        HEIGHT,
        SLOTS,
        FRAME_BYTES,
    ):
        raise ValueError("Unsupported GPU image layout")
    if not any(uuid) or any(
        size < FRAME_BYTES or size > 16 * 1024 * 1024 for size in allocations
    ):
        raise ValueError("Invalid GPU UUID or allocation sizes")
    return uuid, allocations


class CudaResources:
    def __init__(self, uuid, allocations, fds):
        from cuda.bindings import driver as cu

        self.cu = cu
        self.context = self.stream = self.host = None
        self.memories, self.pointers, self.semaphores = [], [], []
        try:
            self.call(cu.cuInit(0))
            devices = self.call(cu.cuDeviceGetCount())
            device = next(
                (
                    i
                    for i in range(devices)
                    if bytes(self.call(cu.cuDeviceGetUuid(i)).bytes) == uuid
                ),
                None,
            )
            if device is None:
                raise ValueError("Vulkan GPU UUID does not match any CUDA device")
            self.context = self.call(cu.cuCtxCreate(0, device))
            self.stream = self.call(
                cu.cuStreamCreate(cu.CUstream_flags.CU_STREAM_NON_BLOCKING)
            )
            self.host = self.call(cu.cuMemHostAlloc(FRAME_BYTES, 0))
            for i, allocation in enumerate(allocations):
                desc = cu.CUDA_EXTERNAL_MEMORY_HANDLE_DESC()
                desc.type = cu.CUexternalMemoryHandleType.CU_EXTERNAL_MEMORY_HANDLE_TYPE_OPAQUE_FD
                desc.handle.fd = fds[2 * i]
                desc.size = allocation
                desc.flags = cu.CUDA_EXTERNAL_MEMORY_DEDICATED
                memory = self.call(cu.cuImportExternalMemory(desc))
                fds[2 * i] = -1  # CUDA owns the FD after a successful opaque-FD import.
                self.memories.append(memory)
                mapping = cu.CUDA_EXTERNAL_MEMORY_BUFFER_DESC()
                mapping.size = FRAME_BYTES
                self.pointers.append(
                    self.call(cu.cuExternalMemoryGetMappedBuffer(memory, mapping))
                )
                sem_desc = cu.CUDA_EXTERNAL_SEMAPHORE_HANDLE_DESC()
                sem_desc.type = cu.CUexternalSemaphoreHandleType.CU_EXTERNAL_SEMAPHORE_HANDLE_TYPE_OPAQUE_FD
                sem_desc.handle.fd = fds[2 * i + 1]
                self.semaphores.append(
                    self.call(cu.cuImportExternalSemaphore(sem_desc))
                )
                fds[2 * i + 1] = -1
            print(f"CUDA imported 3 GPU slots | Device UUID: {uuid.hex()}", flush=True)
        except BaseException:
            self.close()
            raise

    @staticmethod
    def call(result):
        status, *values = result
        if int(status) != 0:
            raise RuntimeError(f"CUDA operation failed: {status}")
        return values[0] if len(values) == 1 else values

    def wait(self, slot):
        params = self.cu.CUDA_EXTERNAL_SEMAPHORE_WAIT_PARAMS()
        self.call(
            self.cu.cuWaitExternalSemaphoresAsync(
                [self.semaphores[slot]], [params], 1, self.stream
            )
        )

    def synchronize(self):
        self.call(self.cu.cuStreamSynchronize(self.stream))

    def download(self, slot):
        self.call(
            self.cu.cuMemcpyDtoHAsync(
                self.host, self.pointers[slot], FRAME_BYTES, self.stream
            )
        )
        self.synchronize()
        view = np.ctypeslib.as_array(
            (ctypes.c_uint8 * FRAME_BYTES).from_address(int(self.host))
        )
        return view.reshape(HEIGHT, WIDTH, 4).copy()

    def close(self):
        if self.context is None:
            return
        # Context destruction also reclaims partially imported resources on initialization failure.
        try:
            if self.stream is not None:
                self.synchronize()
            for pointer in self.pointers:
                self.call(self.cu.cuMemFree(pointer))
            for semaphore in self.semaphores:
                self.call(self.cu.cuDestroyExternalSemaphore(semaphore))
            for memory in self.memories:
                self.call(self.cu.cuDestroyExternalMemory(memory))
            if self.host is not None:
                self.call(self.cu.cuMemFreeHost(self.host))
            if self.stream is not None:
                self.call(self.cu.cuStreamDestroy(self.stream))
        finally:
            self.call(self.cu.cuCtxDestroy(self.context))
            self.context = None


@dataclass(frozen=True)
class GpuFrame:
    slot: int
    frame_id: int
    capture_usec: int
    pattern: bool
    device_pointer: int


class GpuReceiver:
    """Pointers remain valid until release. Enqueue consumer work on receiver.cuda.stream."""

    def __init__(self, sock, cuda_factory=CudaResources):
        self.sock, self.cuda = sock, None
        self.held = {}
        self.last_frame = 0
        fds = []
        try:
            uid = struct.unpack(
                "3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )[1]
            if uid != os.getuid():
                raise ValueError("GPU publisher belongs to another user")
            data, fds = receive_packet(sock, SETUP.size)
            uuid, allocations = decode_setup(data, fds)
            self.cuda = cuda_factory(uuid, allocations, fds)
        except BaseException:
            sock.close()
            raise
        finally:
            for fd in fds:
                if fd >= 0:
                    os.close(fd)

    @classmethod
    def connect(cls, channel="camera1_a"):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        sock.settimeout(3.0)
        try:
            sock.connect(address_for(channel))
            receiver = cls(sock)
            sock.setblocking(False)
            return receiver
        except BaseException:
            sock.close()
            raise

    def acquire(self):
        try:
            data, fds = receive_packet(self.sock, READY.size)
        except BlockingIOError:
            return None
        if fds:
            for fd in fds:
                os.close(fd)
            raise ValueError("Frame notifications must not contain descriptors")
        if len(data) != READY.size:
            raise ValueError("Invalid GPU frame notification length")
        magic, slot, pattern, frame_id, timestamp = READY.unpack(data)
        if (
            magic != b"RDY1"
            or slot >= SLOTS
            or pattern not in (0, 1)
            or frame_id <= self.last_frame
            or timestamp == 0
            or slot in self.held
        ):
            raise ValueError("Invalid, stale or busy GPU frame slot")
        self.cuda.wait(slot)
        frame = GpuFrame(
            slot, frame_id, timestamp, bool(pattern), int(self.cuda.pointers[slot])
        )
        self.held[slot] = frame
        self.last_frame = frame_id
        return frame

    def download(self, frame):
        if self.held.get(frame.slot) is not frame:
            raise ValueError("GPU frame is no longer held")
        return self.cuda.download(frame.slot)

    def release(self, frame):
        if self.held.get(frame.slot) is not frame:
            raise ValueError("GPU frame is no longer held")
        self.cuda.synchronize()  # Includes any CUDA work queued by the caller on this stream.
        message = RELEASE.pack(b"REL1", frame.slot, frame.frame_id)
        if self.sock.send(message) != len(message):
            raise OSError("Could not acknowledge GPU frame release")
        del self.held[frame.slot]

    def close(self):
        try:
            if self.cuda is not None:
                self.cuda.close()
                self.cuda = None
        finally:
            self.sock.close()
            self.held.clear()


def verify_pattern(image):
    """Validate every byte of the startup GPU pattern, including channel order and orientation."""
    expected = np.empty((HEIGHT, WIDTH, 4), dtype=np.uint8)
    expected[:, :, 0] = np.arange(WIDTH, dtype=np.uint16) % 256
    expected[:, :, 1] = (np.arange(HEIGHT, dtype=np.uint16) % 256)[:, None]
    expected[:, :, 2] = 73
    expected[:, :, 3] = 255
    if image.shape != expected.shape or not np.array_equal(image, expected):
        raise ValueError("GPU startup pattern does not match expected RGBA pixels")
