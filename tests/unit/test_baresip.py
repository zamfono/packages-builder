import subprocess
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator import baresip
from orchestrator.cli import _next_baresip_build, _sign_and_upload


class ReleaseTagTests(unittest.TestCase):
    def test_plain_release_tag(self):
        self.assertEqual(baresip.parse_release_tag("v4.12.0"), "4.12.0")

    def test_refuses_anything_but_a_release_number(self):
        # the tag ends up in file names, URLs and the package version
        for tag in ("4.12.0", "v4.12", "v4.12.0-rc1", "v4.12.0/../x", "v4.12.0\n"):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                baresip.parse_release_tag(tag)


class ModuleLoadFailureTests(unittest.TestCase):
    def test_reports_link_failures_only(self):
        output = (
            "baresip v4.12.0 Copyright (C) 2010 - 2026 Alfred E. Heggestad et al.\n"
            "Local network addresses:\n"
            "dl: mod: ./turn.so (./turn.so: cannot open shared object file: No such file or directory)\n"
            "module turn.so: No such file or directory [2]\n"
            "Populated 1 audio codec\n"
            "baresip is ready.\n"
        )
        self.assertEqual(
            baresip.module_load_failures(output),
            ["dl: mod: ./turn.so (./turn.so: cannot open shared object file: No such file or directory)"],
        )

    def test_a_module_that_declines_to_start_is_no_failure(self):
        # evdev without an input device, stdio without a terminal
        output = (
            "module evdev.so: No such file or directory [2]\n"
            "module stdio.so: Operation not permitted [1]\n"
        )
        self.assertEqual(baresip.module_load_failures(output), [])

    def test_clean_start_has_no_failures(self):
        self.assertEqual(baresip.module_load_failures("Populated 3 audio codecs\n"), [])


class PurgeScriptTests(unittest.TestCase):
    def test_fails_when_files_survive_the_purge(self):
        # `! cmd` would not trip set -e; the leftover check must fail the script
        script = baresip.PURGE_SCRIPT.replace("apt-get purge -y baresip\n", "")
        script = "set -e\n" + script.replace("ls -d /usr/lib/*/baresip", "true")
        self.assertNotEqual(subprocess.run(["sh", "-c", script]).returncode, 0)

    def test_gate_script_parses(self):
        # the gate joins these into one sh -c script; a join that leaves a
        # dangling `&&` once broke it
        from orchestrator.cli import CANDIDATE_REPO_SETUP
        script = baresip.gate_script(CANDIDATE_REPO_SETUP)
        self.assertEqual(subprocess.run(["sh", "-n", "-c", script]).returncode, 0)


class NextBaresipBuildTests(unittest.TestCase):
    def _patches(self, upstream, sources, binaries=()):
        return (
            patch("orchestrator.cli.baresip.latest_upstream_version", return_value=upstream),
            patch("orchestrator.cli.published_source_versions", return_value=list(sources)),
            patch("orchestrator.cli.published_binary_versions", return_value=list(binaries)),
        )

    def test_primary_builds_a_newer_upstream_release(self):
        a, b, c = self._patches("4.13.0", ["4.12.0-1+zamfono13.1"])
        with a, b, c:
            self.assertEqual(
                _next_baresip_build("amd64"),
                ("4.13.0-1+zamfono13.1", "4.12.0-1+zamfono13.1", "4.13.0"),
            )

    def test_primary_builds_the_first_release(self):
        a, b, c = self._patches("4.12.0", [])
        with a, b, c:
            self.assertEqual(_next_baresip_build("amd64"), ("4.12.0-1+zamfono13.1", None, "4.12.0"))

    def test_primary_is_idle_when_the_release_is_published(self):
        a, b, c = self._patches("4.12.0", ["4.12.0-1+zamfono13.1"])
        with a, b, c:
            self.assertIsNone(_next_baresip_build("amd64"))

    def test_follower_builds_the_published_source_without_asking_upstream(self):
        a, b, c = self._patches("9.9.9", ["4.12.0-1+zamfono13.1"], ["4.11.0-1+zamfono13.1"])
        with a as upstream, b as source_index, c as binary_index:
            self.assertEqual(
                _next_baresip_build("arm64"),
                ("4.12.0-1+zamfono13.1", "4.11.0-1+zamfono13.1", None),
            )
        upstream.assert_not_called()
        source_index.assert_called_once_with(baresip.INDEX_BASE_URL, "baresip")
        binary_index.assert_called_once_with("arm64", baresip.INDEX_BASE_URL, "baresip")

    def test_follower_is_idle_when_its_binaries_are_current(self):
        a, b, c = self._patches("4.12.0", ["4.12.0-1+zamfono13.1"], ["4.12.0-1+zamfono13.1"])
        with a, b, c:
            self.assertIsNone(_next_baresip_build("arm64"))


class UploadDirectoryTests(unittest.TestCase):
    def test_uploads_into_the_baresip_incoming_directory(self):
        fake_run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
        workspace = Path("/srv/ws")
        with patch("orchestrator.cli.subprocess.run", fake_run), \
                patch("orchestrator.cli.Path.read_text", return_value="KEYID\n"), \
                patch("orchestrator.cli.Path.glob", return_value=[workspace / "baresip_4.12.0-1+zamfono13.1_amd64.changes"]), \
                patch("orchestrator.cli._changes_referenced_files", return_value=["baresip.deb"]):
            _sign_and_upload(workspace, baresip.UPLOAD_DIR)
        batch = fake_run.call_args_list[-1].kwargs["input"]
        self.assertTrue(batch.startswith("cd /incoming-baresip\n"))
        self.assertTrue(batch.rstrip().endswith("_amd64.changes"))


if __name__ == "__main__":
    unittest.main()
