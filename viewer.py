#!/usr/bin/env python3
"""Preview one GPU-shared Godot camera, downloading to CPU only for OpenCV."""

import argparse
import select
import time

import cv2
import numpy as np

from gpu_receiver import GpuReceiver, WIDTH, HEIGHT, verify_pattern


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


def draw_overlay(image, fps, status, frame_id):
    result = image.copy()
    cv2.rectangle(result, (0, 0), (WIDTH, 68), (20, 20, 20), -1)
    cv2.putText(
        result,
        f"Received FPS: {fps:.1f}",
        (12, 27),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (80, 240, 100),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        result,
        f"{status} | Frame: {frame_id}",
        (12, 54),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.5,
        (240, 240, 240),
        1,
        cv2.LINE_AA,
    )
    return result


def main(seconds=None, channel="camera1"):
    receiver = None
    fps = ReceivedFPS(time.monotonic())
    image = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    window = f"Godot GPU {channel}"
    started = last_report = time.monotonic()
    retry = 0.0
    frame_id = 0
    last_frame_time = None
    cv2.namedWindow(window, cv2.WINDOW_AUTOSIZE)
    try:
        while seconds is None or time.monotonic() - started < seconds:
            now = time.monotonic()
            if receiver is None and now >= retry:
                try:
                    receiver = GpuReceiver.connect(channel)
                except (ConnectionRefusedError, FileNotFoundError):
                    retry = now + 0.5
            if receiver is not None:
                try:
                    frame = receiver.acquire()
                    if frame is not None:
                        rgba = receiver.download(frame)
                        receiver.release(frame)
                        if frame.pattern:
                            verify_pattern(rgba)
                            print(
                                f"GPU startup pattern verified | Frame: {frame.frame_id}",
                                flush=True,
                            )
                        image = cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)
                        frame_id = frame.frame_id
                        last_frame_time = time.monotonic()
                        fps.record()
                except (EOFError, ConnectionResetError, BrokenPipeError):
                    receiver.close()
                    receiver = None
                    last_frame_time = None
                    retry = now + 0.5
                    print("GPU publisher disconnected, waiting for restart", flush=True)
            fps.update(now)
            status = "Waiting for frames"
            if receiver is not None and last_frame_time is not None:
                status = (
                    "Live (Vulkan/CUDA)" if now - last_frame_time < 1.0 else "No frames"
                )
            cv2.imshow(window, draw_overlay(image, fps.value, status, frame_id))
            if now - last_report >= 1.0:
                print(
                    f"Received FPS: {fps.value:.1f} | {status} | Frame: {frame_id}",
                    flush=True,
                )
                last_report = now
            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                break
            try:
                if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                    break
            except cv2.error:
                break
            if receiver is not None:
                select.select([receiver.sock], [], [], 0.005)
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
    parser.add_argument(
        "--seconds", type=float, help="Close the preview after this many seconds"
    )
    parser.add_argument("--channel", default="camera1", help="Publisher channel")
    args = parser.parse_args()
    main(args.seconds, args.channel)
