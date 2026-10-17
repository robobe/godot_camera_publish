#!/usr/bin/env python3
"""Fetch and build the pinned Godot with external GPU buffer support."""

import argparse
from pathlib import Path
import subprocess
import sys
import tarfile
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REVISION = (
    "876b290332ec6f2e6d173d08162a02aa7e6ca46d"  # Godot 4.5-stable, matches godot-cpp.
)
SOURCE = ROOT / "build/godot-src"
BINARY = SOURCE / "bin/godot.linuxbsd.template_debug.x86_64"
BUILD_OPTIONS = [
    "platform=linuxbsd",
    "target=template_debug",
    "dev_build=no",
    "debug_symbols=no",
    "optimize=speed",
    "lto=none",
    "opengl3=no",
    "wayland=no",
    "modules_enabled_by_default=no",
    "module_gdscript_enabled=yes",
    "module_glslang_enabled=yes",
    "module_text_server_fb_enabled=yes",
    "module_freetype_enabled=yes",
    "module_regex_enabled=yes",
]


def main(jobs):
    if not (SOURCE / "SConstruct").exists():
        SOURCE.mkdir(parents=True, exist_ok=True)
        archive = ROOT / "build/godot-source.tar.gz"
        urllib.request.urlretrieve(
            f"https://codeload.github.com/godotengine/godot/tar.gz/{REVISION}", archive
        )
        with tarfile.open(archive) as source:
            members = source.getmembers()
            prefix = f"godot-{REVISION}/"
            for member in members:
                if member.name == prefix[:-1]:
                    member.name = ""
                    continue
                if not member.name.startswith(prefix):
                    raise ValueError("Unexpected Godot archive root")
                member.name = member.name[len(prefix) :]
            source.extractall(
                SOURCE, members=[m for m in members if m.name], filter="data"
            )
    patch = ROOT / "patches/godot-gpu-sharing.patch"
    if not (SOURCE / ".gpu-patched").exists():
        if (
            "storage_buffer_create_from_extension"
            not in (SOURCE / "servers/rendering/rendering_device.cpp").read_text()
        ):
            subprocess.run(
                ["patch", "-p1", "--forward", "-i", str(patch)], cwd=SOURCE, check=True
            )
        (SOURCE / ".gpu-patched").write_text(REVISION + "\n")
    subprocess.run(
        [sys.executable, "-m", "SCons", *BUILD_OPTIONS, f"-j{jobs}"],
        cwd=SOURCE,
        check=True,
    )
    print(f"GPU-capable Godot: {BINARY}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=4)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error("--jobs must be positive")
    main(args.jobs)
