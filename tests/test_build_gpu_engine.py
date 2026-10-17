import io
from pathlib import Path
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools import build_gpu_engine as builder


class BuildHelperTest(unittest.TestCase):
    def test_extracts_pinned_archive_applies_patch_and_builds(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "fixture.tar.gz"
            prefix = f"godot-{builder.REVISION}"
            with tarfile.open(archive, "w:gz") as tar:
                entry = tarfile.TarInfo(prefix)
                entry.type = tarfile.DIRTYPE
                tar.addfile(entry)
                for name, content in [
                    ("SConstruct", b"# fixture"),
                    ("servers/rendering/rendering_device.cpp", b"// original"),
                ]:
                    entry = tarfile.TarInfo(prefix + "/" + name)
                    entry.size = len(content)
                    tar.addfile(entry, io.BytesIO(content))
            source = root / "build/godot-src"
            with (
                patch.object(builder, "ROOT", root),
                patch.object(builder, "SOURCE", source),
                patch.object(
                    builder.urllib.request,
                    "urlretrieve",
                    side_effect=lambda url, dest: shutil.copyfile(archive, dest),
                ),
                patch.object(builder.subprocess, "run") as run,
            ):
                builder.main(2)
                self.assertEqual((source / "SConstruct").read_text(), "# fixture")
                self.assertEqual(
                    (source / ".gpu-patched").read_text(), builder.REVISION + "\n"
                )
                self.assertEqual(run.call_count, 2)
                self.assertEqual(run.call_args_list[0].args[0][0], "patch")
                self.assertIn("-j2", run.call_args.args[0])
                builder.main(2)
                self.assertEqual(run.call_count, 3, "Second run must not reapply patch")


if __name__ == "__main__":
    unittest.main()
