import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from orchestrator.packaging_overlay import (
    REPLACED_DEBIAN_PATCHES,
    changelog_replace_cmds,
    install_patches,
    replace_debian_patches,
    set_changelog_version_cmd,
)

CONTROL_PATCH = """--- a/debian/control
+++ b/debian/control
@@ -1,1 +1,1 @@
-Package: asterisk-modules
+Package: asterisk-modules-core
"""


DEBIAN_PATCH = """--- a/main/translate.c
+++ b/main/translate.c
@@ -1,1 +1,1 @@
-upstream
+debian
"""


OUR_PATCH = """--- a/main/translate.c
+++ b/main/translate.c
@@ -1,1 +1,1 @@
-upstream
+fixed
"""


class ReplaceDebianPatchesTests(unittest.TestCase):
    def _extracted_tree(self, tmp):
        # what dpkg-source -x leaves: Debian's patch applied and recorded in .pc/
        source_dir = Path(tmp) / "source"
        (source_dir / "main").mkdir(parents=True)
        (source_dir / "main" / "translate.c").write_text("debian\n")
        (source_dir / "debian" / "patches").mkdir(parents=True)
        (source_dir / "debian" / "patches" / "plc.patch").write_text(DEBIAN_PATCH)
        (source_dir / "debian" / "patches" / "series").write_text("other.patch\nplc.patch\n")
        (source_dir / ".pc" / "plc.patch").mkdir(parents=True)
        (source_dir / ".pc" / "applied-patches").write_text("other.patch\nplc.patch\n")
        ours = Path(tmp) / "ours"
        ours.mkdir()
        (ours / "plc.patch").write_text(OUR_PATCH)
        return source_dir, ours

    def test_applies_ours_instead_of_debians_and_keeps_series_and_pc(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_dir, ours = self._extracted_tree(tmp)
            pin = hashlib.sha256(DEBIAN_PATCH.encode()).hexdigest()

            replace_debian_patches(source_dir, ours, {"plc.patch": (pin, "why")})

            self.assertEqual((source_dir / "main" / "translate.c").read_text(), "fixed\n")
            self.assertEqual((source_dir / "debian" / "patches" / "plc.patch").read_text(), OUR_PATCH)
            self.assertEqual((source_dir / "debian" / "patches" / "series").read_text(), "other.patch\nplc.patch\n")
            self.assertEqual((source_dir / ".pc" / "applied-patches").read_text(), "other.patch\nplc.patch\n")
            self.assertTrue((source_dir / ".pc" / "plc.patch").is_dir())

    def test_a_patch_changed_in_debian_stops_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_dir, ours = self._extracted_tree(tmp)
            with self.assertRaisesRegex(RuntimeError, "changed in Debian"):
                replace_debian_patches(source_dir, ours, {"plc.patch": ("0" * 64, "why")})
            self.assertEqual((source_dir / "main" / "translate.c").read_text(), "debian\n")
            self.assertEqual((source_dir / "debian" / "patches" / "plc.patch").read_text(), DEBIAN_PATCH)

    def test_a_patch_gone_from_debian_stops_the_build(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_dir, ours = self._extracted_tree(tmp)
            with self.assertRaisesRegex(RuntimeError, "no longer in the source"):
                replace_debian_patches(source_dir, ours, {"gone.patch": ("0" * 64, "why")})

    def test_the_shipped_replacement_matches_the_pinned_patch_name(self):
        for name in REPLACED_DEBIAN_PATCHES:
            self.assertTrue((Path("packaging/debian-patches") / name).is_file(), name)

    def test_every_replaced_patch_is_stated_in_the_changelog(self):
        self.assertEqual(
            changelog_replace_cmds({"plc.patch": ("0" * 64, "it breaks audio.")}),
            [["dch", "--append", "Replace Debian's plc.patch with a fixed version: it breaks audio."]],
        )


class InstallPatchesTests(unittest.TestCase):
    def test_applies_overlay_patches_to_the_source_tree_in_sorted_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "source"
            (source_dir / "debian").mkdir(parents=True)
            (source_dir / "debian" / "control").write_text("Package: asterisk-modules\n")

            overlay_dir = Path(tmp) / "overlay"
            overlay_dir.mkdir()
            (overlay_dir / "0001-rename-modules-package.patch").write_text(CONTROL_PATCH)

            applied = install_patches(source_dir, overlay_dir)

            self.assertEqual(applied, ["0001-rename-modules-package.patch"])
            self.assertEqual(
                (source_dir / "debian" / "control").read_text(),
                "Package: asterisk-modules-core\n",
            )

    def test_applies_an_overlay_directory_named_relative_to_the_caller(self):
        # orchestrator/cli.py passes "packaging/patches", relative to the
        # repository root it runs from — never to the source tree.
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "source"
            (source_dir / "debian").mkdir(parents=True)
            (source_dir / "debian" / "control").write_text("Package: asterisk-modules\n")

            (Path(tmp) / "overlay").mkdir()
            (Path(tmp) / "overlay" / "0001-rename-modules-package.patch").write_text(CONTROL_PATCH)

            cwd = os.getcwd()
            os.chdir(tmp)
            try:
                install_patches(source_dir, "overlay")
            finally:
                os.chdir(cwd)

            self.assertEqual(
                (source_dir / "debian" / "control").read_text(),
                "Package: asterisk-modules-core\n",
            )

    def test_a_patch_that_does_not_apply_cleanly_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            source_dir = Path(tmp) / "source"
            (source_dir / "debian").mkdir(parents=True)
            (source_dir / "debian" / "control").write_text("Package: something-else\n")

            overlay_dir = Path(tmp) / "overlay"
            overlay_dir.mkdir()
            (overlay_dir / "0001-rename-modules-package.patch").write_text(CONTROL_PATCH)

            with self.assertRaises(subprocess.CalledProcessError):
                install_patches(source_dir, overlay_dir)


class SetChangelogVersionCmdTests(unittest.TestCase):
    def test_stamps_the_zamfono_version_into_debian_changelog(self):
        argv = set_changelog_version_cmd("1:20.9.0~dfsg-1+zamfono13.1")
        self.assertEqual(
            argv,
            [
                "dch",
                "--newversion", "1:20.9.0~dfsg-1+zamfono13.1",
                "--distribution", "trixie",
                "--force-distribution",
                "Zamfono rebuild for Debian 13",
            ],
        )


if __name__ == "__main__":
    unittest.main()
