import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator.cli import (
    INDEX_BASE_URL,
    _changes_referenced_files,
    _container_check,
    _fetch_and_patch,
    _retry_pending,
    _next_build,
    _wait_until_published,
    _run_module_load_and_upgrade_gates,
    _safe_name,
    _sign_and_upload,
    main,
)


class ContainerCheckTests(unittest.TestCase):
    def test_uses_the_fully_qualified_base_image(self):
        # A bare "debian:13" relies on Docker's implicit docker.io
        # expansion; podman defines no unqualified-search-registries on
        # Debian 13 and fails closed on a short name instead.
        fake_run = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
        with patch("subprocess.run", fake_run):
            _container_check("zamfono-test", Path("/srv/workspace"), "true")
        argv = fake_run.call_args.args[0]
        self.assertIn("docker.io/library/debian:13", argv)


class RunModuleLoadAndUpgradeGatesTests(unittest.TestCase):
    CANDIDATE_VERSION = "1:20.9.0-1+zamfono13.1"

    def _container_name(self):
        return f"zamfono-lifecycle-{_safe_name(self.CANDIDATE_VERSION)}"

    def test_pre_cleans_the_lifecycle_container_name_before_creating_it(self):
        # A reboot or kill between creation and the `finally` cleanup can
        # leave the fixed name behind; without this pre-clean, every retry
        # of this candidate would fail on the name collision.
        fake_run = MagicMock(return_value=subprocess.CompletedProcess([], 0, stdout="", stderr=""))
        with patch("orchestrator.cli.subprocess.run", fake_run):
            _run_module_load_and_upgrade_gates(Path("/srv/workspace/x"), self.CANDIDATE_VERSION, None)
        argvs = [call.args[0] for call in fake_run.call_args_list]
        container_name = self._container_name()
        run_d_index = argvs.index(
            ["podman", "run", "-d", "--name", container_name,
             "-v", "/srv/workspace/x:/out", "-w", "/out",
             "docker.io/library/debian:13", "sleep", "infinity"]
        )
        self.assertIn(["podman", "rm", "-f", container_name], argvs[:run_d_index])

    def test_fails_without_crashing_when_service_asterisk_start_fails(self):
        container_name = self._container_name()

        def fake_run(argv, **kwargs):
            if argv[:3] == ["podman", "exec", container_name] and argv[3:] == ["service", "asterisk", "start"]:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="start failed")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch("orchestrator.cli.subprocess.run", fake_run):
            errors = _run_module_load_and_upgrade_gates(Path("/srv/workspace/x"), self.CANDIDATE_VERSION, None)

        self.assertEqual(len(errors), 1)
        self.assertIn("service asterisk start failed", errors[0])

    def test_fails_when_the_module_show_probe_exits_nonzero(self):
        # Asterisk exiting or crashing right after `service start` leaves
        # the probe with a nonzero exit and no module list — the broken
        # candidate must not pass the module-load gate on an empty parse.
        container_name = self._container_name()

        def fake_run(argv, **kwargs):
            if argv[:3] == ["podman", "exec", container_name] and "module show" in argv:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="Unable to connect")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch("orchestrator.cli.subprocess.run", fake_run):
            errors = _run_module_load_and_upgrade_gates(Path("/srv/workspace/x"), self.CANDIDATE_VERSION, None)

        self.assertEqual(len(errors), 1)
        self.assertIn("module show probe failed", errors[0])

    def test_fails_when_the_module_show_probe_returns_empty_output(self):
        container_name = self._container_name()

        def fake_run(argv, **kwargs):
            if argv[:3] == ["podman", "exec", container_name] and "module show" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout="   \n", stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch("orchestrator.cli.subprocess.run", fake_run):
            errors = _run_module_load_and_upgrade_gates(Path("/srv/workspace/x"), self.CANDIDATE_VERSION, None)

        self.assertEqual(len(errors), 1)
        self.assertIn("module show probe failed", errors[0])

    def test_parses_module_show_failures_when_the_probe_runs_cleanly(self):
        container_name = self._container_name()
        module_show_output = (
            "Module                         Description   Use Count  Status   Support Level\n"
            "chan_pjsip.so                  PJSIP Channel  0          Failed  extended\n"
            "1 modules loaded\n"
        )

        def fake_run(argv, **kwargs):
            if argv[:3] == ["podman", "exec", container_name] and "module show" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout=module_show_output, stderr="")
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        with patch("orchestrator.cli.subprocess.run", fake_run):
            errors = _run_module_load_and_upgrade_gates(Path("/srv/workspace/x"), self.CANDIDATE_VERSION, None)

        self.assertEqual(len(errors), 1)
        self.assertIn("chan_pjsip.so", errors[0])


class RunSubcommandTests(unittest.TestCase):
    @patch("orchestrator.cli.acquire_lock", return_value=None)
    def test_run_exits_zero_without_side_effects_when_lock_is_held(self, _acquire_lock):
        exit_code = main(["run"])
        self.assertEqual(exit_code, 0)


class PrimaryRebuildTests(unittest.TestCase):
    PUBLISHED = ["1:22.10.1-1+zamfono13.1"]

    def _next_build(self, rebuild):
        with patch("orchestrator.cli.sid_asterisk_version", return_value="1:22.10.1-1"), \
                patch("orchestrator.cli.published_source_versions", return_value=self.PUBLISHED):
            return _next_build("amd64", rebuild)

    def test_an_already_published_sid_version_is_not_built(self):
        self.assertIsNone(self._next_build(rebuild=False))

    def test_rebuild_builds_it_again_as_the_next_counter(self):
        self.assertEqual(
            self._next_build(rebuild=True), ("1:22.10.1-1+zamfono13.2", "1:22.10.1-1+zamfono13.1"),
        )


@patch("orchestrator.cli.sid_asterisk_version", side_effect=AssertionError("followers never read sid"))
class FollowerNextBuildTests(unittest.TestCase):
    SOURCE = "1:22.10.1-1+zamfono13.2"

    def _next_build(self, sources, arm64):
        with patch("orchestrator.cli.published_source_versions", return_value=sources) as source_index, \
                patch("orchestrator.cli.published_binary_versions", return_value=arm64) as binary_index:
            result = _next_build("arm64")
        source_index.assert_called_once_with(INDEX_BASE_URL, "asterisk")
        binary_index.assert_called_once_with("arm64", INDEX_BASE_URL, "asterisk")
        return result

    def test_builds_the_published_source_when_the_architecture_has_nothing_yet(self, _sid):
        self.assertEqual(self._next_build([self.SOURCE], []), (self.SOURCE, None))

    def test_builds_the_published_source_when_the_architecture_lags_behind(self, _sid):
        self.assertEqual(
            self._next_build([self.SOURCE], ["1:22.10.1-1+zamfono13.1"]),
            (self.SOURCE, "1:22.10.1-1+zamfono13.1"),
        )

    def test_nothing_to_build_when_the_architecture_is_current(self, _sid):
        self.assertIsNone(self._next_build([self.SOURCE], [self.SOURCE]))

    def test_nothing_to_build_before_the_primary_published_any_source(self, _sid):
        self.assertIsNone(self._next_build([], []))


class FollowerRetryPendingTests(unittest.TestCase):
    def test_holds_off_only_while_a_failed_workspace_is_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "candidate"
            self.assertFalse(_retry_pending(workspace))
            workspace.mkdir()
            self.assertTrue(_retry_pending(workspace))
            seven_hours_ago = time.time() - 7 * 3600
            os.utime(workspace, (seven_hours_ago, seven_hours_ago))
            self.assertFalse(_retry_pending(workspace))


class WaitUntilPublishedTests(unittest.TestCase):
    V = "1:22.10.1-1+zamfono13.1"

    @patch("orchestrator.cli.time.sleep")
    def test_returns_once_the_index_lists_the_upload(self, sleep):
        with patch("orchestrator.cli.published_binary_versions", side_effect=[[], ["older"], [self.V]]):
            self.assertTrue(_wait_until_published("arm64", self.V, INDEX_BASE_URL, "asterisk"))
        self.assertEqual(sleep.call_count, 2)

    @patch("orchestrator.cli.time.sleep")
    def test_gives_up_when_the_upload_never_appears(self, _sleep):
        with patch("orchestrator.cli.published_binary_versions", return_value=[]):
            self.assertFalse(_wait_until_published("arm64", self.V, INDEX_BASE_URL, "asterisk", timeout=0))


class UploadDirectoryTests(unittest.TestCase):
    def test_uploads_into_the_asterisk_incoming_directory(self):
        fake_run = MagicMock(return_value=subprocess.CompletedProcess([], 0))
        workspace = Path("/srv/ws")
        with patch("orchestrator.cli.subprocess.run", fake_run), \
                patch("orchestrator.cli.Path.read_text", return_value="KEYID\n"), \
                patch("orchestrator.cli.Path.glob", return_value=[workspace / "asterisk_22.5.2-1+zamfono13.1_amd64.changes"]), \
                patch("orchestrator.cli._changes_referenced_files", return_value=["asterisk.deb"]):
            _sign_and_upload(workspace, "/incoming")
        self.assertTrue(fake_run.call_args_list[-1].kwargs["input"].startswith("cd /incoming\n"))


class FetchAndPatchTests(unittest.TestCase):
    @patch("orchestrator.cli.replace_debian_patches")
    @patch("orchestrator.cli.install_patches")
    @patch("orchestrator.cli.set_changelog_version_cmd", return_value=["true"])
    @patch("orchestrator.cli._write_sid_sourcelist")
    def test_discards_a_stale_workspace_left_by_a_previous_failed_attempt(
        self, _write_sid_sourcelist, _set_changelog_version_cmd, install_patches, _replace_debian_patches
    ):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "candidate"
            stale_source = workspace / "source"
            stale_source.mkdir(parents=True)
            (stale_source / "already-patched-marker").write_text("x")

            def fake_run(argv, **_kwargs):
                if "source" in argv:
                    # simulates `apt-get source` extracting a fresh tree
                    (workspace / "asterisk-20.9.0").mkdir()
                return subprocess.CompletedProcess(argv, 0)

            with patch("orchestrator.cli.subprocess.run", side_effect=fake_run):
                _fetch_and_patch(workspace, "1:20.9.0-1", "1:20.9.0-1+zamfono13.1")

            # A retry must not nest the fresh tree inside the stale one, nor
            # patch on top of the stale, already-patched source.
            self.assertFalse((workspace / "source" / "already-patched-marker").exists())
            self.assertFalse((workspace / "source" / "asterisk-20.9.0").exists())
            install_patches.assert_called_once()


if __name__ == "__main__":
    unittest.main()


class ChangesReferencedFilesTest(unittest.TestCase):
    def test_reads_filenames_from_the_files_stanza_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            changes = Path(tmp) / "x.changes"
            changes.write_text(
                "Format: 1.8\n"
                "Checksums-Sha256:\n"
                " abc 1 not-a-files-entry.deb\n"
                "Files:\n"
                " deadbeef 123 comm optional asterisk_1.0_amd64.deb\n"
                " cafebabe 456 comm optional asterisk_1.0.dsc\n"
            )
            self.assertEqual(
                _changes_referenced_files(changes),
                ["asterisk_1.0_amd64.deb", "asterisk_1.0.dsc"],
            )
