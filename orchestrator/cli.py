"""packages-builder's entry point. `check` only reports whether there is
something to build (no side effects). `run` is what the systemd timer
invokes: check, and only if needed, fetch/build/gate/sign/upload; always
under the host lock. Gates that only need the already-produced build
artifacts and the containerized install gates run in one non-fail-fast
pass so a candidate's full gate report is visible before it is discarded;
the lifecycle gates (module load, upgrade, purge) run afterwards, only
once that pass is clean. Publishing is debsign + an sftp batch upload;
ingest and serving are the packages host's job.

The host's own architecture decides its role. The amd64 builder is the
primary: it imports newer sid sources, patches them, and publishes the
source, the Architecture: all packages and its amd64 binaries. Every other
architecture follows: once the primary has published a source version its
own binaries lag behind, it downloads exactly that source from the archive
and uploads only its architecture's binaries."""
import argparse
import concurrent.futures
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

from orchestrator import baresip

from orchestrator.gates_deps import check_cross_group_dependencies, check_unresolved_shlibs
from orchestrator.gates_lint import check_dh_missing, lintian_gate_errors, parse_lintian
from orchestrator.gates_meta import check_asterisk_core_only_dependency, check_meta_package
from orchestrator.gates_upgrade import ARCHIVE_APT_LINE, purge_check_script, upgrade_check_script
from orchestrator.lock import acquire_lock
from orchestrator.manifest import (
    built_modules_from_debs,
    load_manifest,
    validate_full_coverage,
    validate_structure,
)
from orchestrator.packaging_overlay import (
    changelog_drop_cmds,
    drop_debian_patches,
    install_patches,
    set_changelog_version_cmd,
)
from orchestrator.pbuilder import ARCH_BUILD_OPTS, FULL_BUILD_OPTS, run_build
from orchestrator.published import published_binary_versions, published_source_versions
from orchestrator.sid_source import (
    SID_SOURCELIST_CONTENT,
    parse_showsrc_version,
    prepare_apt_state,
    scoped_apt_opts,
)
from orchestrator.version import compute_next_zamfono_version
from orchestrator.versioncheck import dpkg_compare_versions, should_build, strip_zamfono_suffix

RUNTIME_DIR = "/var/lib/zamfono/packages-builder/run"
# `.list`, not `.sources` — apt selects its parser by file extension.
SID_SOURCELIST_PATH = f"{RUNTIME_DIR}/sid.list"
ARCHIVE_SOURCELIST_PATH = f"{RUNTIME_DIR}/archive.list"
# Pinned in this repository (install/files), not fetched from the archive it
# authenticates: a follower signs and uploads whatever source it downloads,
# so that source must verify against the archive key, not just HTTPS.
ARCHIVE_SOURCELIST_CONTENT = (
    "deb-src [signed-by=/etc/zamfono/zamfono-archive-keyring.gpg]"
    " https://packages.zamfono.com/debian/asterisk/22 trixie main\n"
)
# asterisk's archive; baresip's lives in orchestrator/baresip.py. The shared
# helpers take these explicitly, so no product falls back to another's.
PACKAGE = "asterisk"
INDEX_BASE_URL = "https://packages.zamfono.com/debian/asterisk/22/dists/trixie/main/"
UPLOAD_DIR = "/incoming"
APT_STATE_DIR = f"{RUNTIME_DIR}/apt"
LOCK_PATH = f"{RUNTIME_DIR}/lock"
PRIMARY_ARCH = "amd64"
# Every builder polls every five minutes; a failed attempt's workspace is
# kept for diagnosis and holds off the next attempt for that version this
# long, instead of a back-to-back rebuild discarding the evidence minutes
# after the failure. A newer version gets its own workspace and builds at once.
RETRY_SECONDS = 6 * 3600
# Ingest takes seconds; this only bounds a refused or stuck upload.
INGEST_TIMEOUT_SECONDS = 600
WORKSPACE_BASE = "/var/lib/zamfono/packages-builder/workspace"
MANIFEST_PATH = "manifest/modules.json"
# One line: the key id debsign signs .changes with. Host-local, never in Git.
UPLOAD_KEYID_FILE = "/etc/zamfono/packages-builder/upload-keyid"
# The chrooted upload account on the packages host; its forced
# internal-sftp session starts in /incoming (asterisk's UPLOAD_DIR).
UPLOAD_TARGET = "zamfono-upload@10.250.0.2"
CHANGELOG_NAME = "Zamfono Packaging"
CHANGELOG_EMAIL = "packages@zamfono.com"
# [trusted=yes]: the candidate is unsigned by design (the .changes is
# signed for upload; the archive Release is signed on the packages host).
CANDIDATE_REPO_SETUP = (
    "echo 'deb [trusted=yes] file:/out ./' > /etc/apt/sources.list.d/zamfono-candidate.list"
    " && apt-get update"
)


def _safe_name(version: str) -> str:
    """Maps a Debian version to a filesystem- and container-name-safe string."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", version)


def _native_arch() -> str:
    return subprocess.run(
        ["dpkg", "--print-architecture"], capture_output=True, text=True, check=True
    ).stdout.strip()


def _write_sid_sourcelist() -> None:
    prepare_apt_state(APT_STATE_DIR)
    Path(SID_SOURCELIST_PATH).write_text(SID_SOURCELIST_CONTENT)


def _index_candidate_repo(workspace: Path) -> None:
    """dpkg-scanpackages (dpkg-dev) writes the flat Packages index the gate
    containers' candidate apt source reads."""
    result = subprocess.run(
        ["dpkg-scanpackages", "--multiversion", "."],
        cwd=workspace, check=True, capture_output=True, text=True,
    )
    (workspace / "Packages").write_text(result.stdout)


def find_extracted_source_dir(dest_dir: "str | Path", package: str) -> Path:
    """Locates the single directory `apt-get source` extracted."""
    dest_dir = Path(dest_dir)
    matches = [p for p in dest_dir.glob(f"{package}-*") if p.is_dir()]
    if len(matches) != 1:
        raise LookupError(
            f"expected exactly one {package}-* directory in {dest_dir}, found {len(matches)}"
        )
    return matches[0]


def parse_module_show(output: str) -> "list[str]":
    """Module-load gate: failure lines from `asterisk -rx "module show"` —
    every packaged module must load without an unresolved hard module or
    shared-library dependency in a running Asterisk instance."""
    return [
        line
        for line in output.splitlines()
        if "Failed" in line and not line.lower().startswith("module")
    ]


def sid_asterisk_version() -> str:
    _write_sid_sourcelist()
    # scoped_apt_opts pins Dir::Etc::sourcelist to the throwaway sid-only
    # list above, so neither command ever touches the host's real (trixie)
    # apt configuration.
    apt_opts = scoped_apt_opts(SID_SOURCELIST_PATH, APT_STATE_DIR)
    subprocess.run(["apt-get", *apt_opts, "update"], check=True)
    result = subprocess.run(
        ["apt-cache", *apt_opts, "showsrc", "asterisk"],
        capture_output=True, text=True, check=True,
    )
    return parse_showsrc_version(result.stdout)


def _next_build(arch: str, rebuild: bool = False) -> "tuple[str, str | None] | None":
    """The version to build on this architecture and the latest version
    already published for it, or None when there is nothing to build.
    `rebuild` makes the primary build sid's version even when it is already
    published, as the next +zamfono13.N: the timer only notices new Debian
    uploads, not packaging changes in this repository."""
    if arch == PRIMARY_ARCH:
        sid_version = sid_asterisk_version()
        published = published_source_versions(INDEX_BASE_URL, PACKAGE)
        latest_published = published[0] if published else None
        if not rebuild and not should_build(sid_version, latest_published):
            print(f"up to date: sid={sid_version} published={latest_published}")
            return None
        print(f"newer sid asterisk source available: {sid_version} (published: {latest_published})")
        return compute_next_zamfono_version(sid_version, published), latest_published

    sources = published_source_versions(INDEX_BASE_URL, PACKAGE)
    binaries = published_binary_versions(arch, INDEX_BASE_URL, PACKAGE)
    source_version = sources[0] if sources else None
    latest_published = binaries[0] if binaries else None
    if source_version is None or (
        latest_published is not None
        and not dpkg_compare_versions(source_version, "gt", latest_published)
    ):
        print(f"up to date: source={source_version} {arch}={latest_published}")
        return None
    print(f"published source not yet built for {arch}: {source_version} ({arch}: {latest_published})")
    return source_version, latest_published


def _retry_pending(workspace: Path) -> bool:
    """True while a failed attempt's kept workspace is younger than
    RETRY_SECONDS (successful runs remove theirs)."""
    return workspace.exists() and time.time() - workspace.stat().st_mtime < RETRY_SECONDS


def _wait_until_published(
    arch: str, version: str, index_url: str, package: str,
    timeout: float = INGEST_TIMEOUT_SECONDS,
) -> bool:
    """True once the archive's index for `arch` lists `version`."""
    deadline = time.monotonic() + timeout
    while version not in published_binary_versions(arch, index_url, package):
        if time.monotonic() > deadline:
            return False
        time.sleep(10)
    return True


def _cmd_check(_args) -> int:
    arch = _native_arch()
    _next_build(arch)
    _next_baresip_build(arch)
    return 0


def _fetch_and_patch(workspace: Path, sid_version: str, zamfono_version: str) -> None:
    _write_sid_sourcelist()
    # A failed attempt's workspace is kept for diagnosis, so a retry for the
    # same candidate can find workspace/source already populated and patched:
    # shutil.move below would then nest the freshly extracted tree inside it
    # instead of replacing it, and install_patches would reapply onto the
    # stale tree. Discarding it here, at the start of the next attempt,
    # keeps it inspectable right up until a new attempt needs the space.
    shutil.rmtree(workspace, ignore_errors=True)
    # Downloaded into the workspace root, not a subdirectory: a `3.0
    # (quilt)` source build reads the .orig tarball from the source tree's
    # parent, which is where the build also writes its products.
    workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["apt-get", *scoped_apt_opts(SID_SOURCELIST_PATH, APT_STATE_DIR), "source", f"asterisk={sid_version}"],
        cwd=workspace, check=True,
    )
    extracted = find_extracted_source_dir(workspace, "asterisk")
    source_dir = workspace / "source"
    shutil.move(str(extracted), str(source_dir))
    # apt-get source leaves the sid .dsc next to the tarballs; run_build
    # later generates the Zamfono .dsc with dpkg-source -b and must find
    # exactly one, so the fetched one is junk once the tree is extracted.
    for fetched_dsc in workspace.glob("*.dsc"):
        fetched_dsc.unlink()
    drop_debian_patches(source_dir)
    install_patches(source_dir, "packaging/patches")
    # Stamps zamfono_version into debian/changelog's top entry — without
    # this, the build runs under the raw sid version and every
    # ${binary:Version}-based gate check fails. DEBEMAIL and DEBFULLNAME
    # name the author of that entry; left unset, dch composes one from the
    # build account and the host name, which lintian rejects as a bogus
    # mail host.
    for cmd in [set_changelog_version_cmd(zamfono_version), *changelog_drop_cmds()]:
        subprocess.run(
            cmd, cwd=source_dir, check=True,
            env={**os.environ, "DEBEMAIL": CHANGELOG_EMAIL, "DEBFULLNAME": CHANGELOG_NAME},
        )


def _fetch_published_source(workspace: Path, version: str, sourcelist: str, package: str) -> None:
    """A follower's fetch: the primary's published .dsc and tarballs,
    verified by apt against the archive's signed Sources index and built
    unmodified, so every architecture ships binaries of the same source."""
    prepare_apt_state(APT_STATE_DIR)
    Path(ARCHIVE_SOURCELIST_PATH).write_text(sourcelist)
    apt_opts = scoped_apt_opts(ARCHIVE_SOURCELIST_PATH, APT_STATE_DIR)
    # same stale-workspace reasoning as _fetch_and_patch
    shutil.rmtree(workspace, ignore_errors=True)
    workspace.mkdir(parents=True, exist_ok=True)
    subprocess.run(["apt-get", *apt_opts, "update"], check=True)
    subprocess.run(
        ["apt-get", *apt_opts, "source", "--download-only", f"{package}={version}"],
        cwd=workspace, check=True,
    )


def _candidate_repo_setup(primary: bool) -> str:
    """A follower's candidate lacks the Architecture: all packages
    (asterisk-config, the asterisk-modules meta package, ...); the gate
    containers take those from the archive, where the primary published
    them at this same version. The debian:13 image ships no TLS trust
    store, hence ca-certificates first."""
    if primary:
        return CANDIDATE_REPO_SETUP
    return (
        "apt-get update && apt-get install -y ca-certificates"
        f" && echo '{ARCHIVE_APT_LINE}' > /etc/apt/sources.list.d/zamfono-archive.list"
        f" && {CANDIDATE_REPO_SETUP}"
    )


def _deb_paths_by_package(workspace: Path) -> "dict[str, str]":
    return {p.name.split("_", 1)[0]: str(p) for p in workspace.glob("*.deb")}


def _container_check(name: str, workspace: Path, script: str) -> bool:
    """Runs `script` in a throwaway docker.io/library/debian:13 container
    with the candidate directory mounted at /out; True when it exits 0.
    The image reference is fully qualified: Debian defines no
    unqualified-search-registries, so podman will not expand a bare
    "debian:13" on its own. --rm removes the container on exit; the
    pre-clean covers only a prior run killed hard enough to skip that."""
    subprocess.run(["podman", "rm", "-f", name], capture_output=True)
    result = subprocess.run(
        [
            "podman", "run", "--rm", "--name", name,
            "-v", f"{workspace}:/out", "-w", "/out",
            "docker.io/library/debian:13", "sh", "-c", script,
        ],
        capture_output=True, text=True,
    )
    return result.returncode == 0


def _run_lintian(workspace: Path) -> "list[str]":
    """The lintian gate's failures for the candidate's .changes."""
    changes_paths = list(workspace.glob("*.changes"))
    lintian_output = ""
    lintian_returncode = 0
    if changes_paths:
        lintian_result = subprocess.run(
            ["lintian", "--fail-on", "error", str(changes_paths[0])],
            capture_output=True, text=True,
        )
        lintian_output = lintian_result.stdout + lintian_result.stderr
        lintian_returncode = lintian_result.returncode
    # Warnings are not gate failures, but the spec requires them retained.
    # They are printed to this process's stdout, which systemd captures into
    # the journal — the durable build log for a run whose workspace does not
    # survive success.
    _lintian_errors, lintian_warnings = parse_lintian(lintian_output)
    for warning in lintian_warnings:
        print(f"lintian warning: {warning}")
    return lintian_gate_errors(lintian_returncode, lintian_output)


def _run_gates_for_candidate(
    workspace: Path, candidate_version: str, build_log: str, primary: bool = True
) -> "list[tuple[str, list[str]]]":
    manifest = load_manifest(MANIFEST_PATH)
    repo_setup = _candidate_repo_setup(primary)
    deb_paths = _deb_paths_by_package(workspace)
    built_modules = built_modules_from_debs(list(deb_paths.values()))

    lintian_errors = _run_lintian(workspace)
    gates = [
        (
            "manifest coverage",
            lambda: validate_structure(manifest) + validate_full_coverage(manifest, built_modules),
        ),
        ("dh_missing", lambda: check_dh_missing(build_log)),
        (
            "dependency derivation",
            lambda: check_unresolved_shlibs(build_log)
            + check_cross_group_dependencies(manifest, deb_paths, candidate_version)
            + check_asterisk_core_only_dependency(deb_paths),
        ),
        ("lintian", lambda: lintian_errors),
        (
            "minimal install",
            lambda: []
            if _container_check(
                f"zamfono-minimal-{_safe_name(candidate_version)}",
                workspace,
                f"{repo_setup} && apt-get install -y asterisk asterisk-modules-core",
            )
            else ["minimal asterisk + asterisk-modules-core install failed"],
        ),
    ]
    # The meta package is Architecture: all, built and gated by the primary
    # only; a follower's full install in the lifecycle gate still proves
    # every group it depends on exists for the follower's architecture.
    if primary:
        gates.append(
            (
                "meta-package coverage",
                lambda: check_meta_package(manifest, built_modules, deb_paths["asterisk-modules"], candidate_version)
                if "asterisk-modules" in deb_paths
                else ["asterisk-modules package was not built"],
            )
        )
    for package in deb_paths:
        if package in ("asterisk", "asterisk-config", "asterisk-modules-core", "asterisk-modules"):
            continue
        # dbgsym packages are debhelper's automatic detached-symbol
        # companions, not module groups: they carry no module and are
        # installable only alongside the package they shadow.
        if package.endswith("-dbgsym"):
            continue
        gates.append(
            (
                f"optional install: {package}",
                lambda pkg=package: []
                if _container_check(
                    f"zamfono-install-{pkg}",
                    workspace,
                    f"{repo_setup} && apt-get install -y {pkg}",
                )
                else [f"install failed for {pkg}"],
            )
        )

    # Not fail-fast: every gate runs so a candidate's full gate report is
    # visible in one pass before it is discarded. The gates are independent
    # (each container gate uses its own throwaway container), and the
    # install gates dominate wall-clock, so they run concurrently, one
    # worker per core.
    with concurrent.futures.ThreadPoolExecutor(max_workers=os.cpu_count()) as pool:
        error_lists = list(pool.map(lambda gate: gate[1](), gates))
    return [
        (name, errors)
        for (name, _check), errors in zip(gates, error_lists)
        if errors
    ]


def _run_module_load_and_upgrade_gates(
    workspace: Path, candidate_version: str, published: "str | None", primary: bool = True
) -> "list[str]":
    container_name = f"zamfono-lifecycle-{_safe_name(candidate_version)}"
    subprocess.run(["podman", "rm", "-f", container_name], capture_output=True)
    subprocess.run(
        [
            "podman", "run", "-d", "--name", container_name,
            "-v", f"{workspace}:/out", "-w", "/out",
            "docker.io/library/debian:13", "sleep", "infinity",
        ],
        check=True,
    )
    try:
        # asterisk-modules depends on every emitted group, so these two
        # names pull in the complete candidate.
        install = subprocess.run(
            ["podman", "exec", container_name, "sh", "-c",
             f"{_candidate_repo_setup(primary)} && apt-get install -y asterisk asterisk-modules"],
            capture_output=True, text=True,
        )
        if install.returncode != 0:
            return [f"full install for module-load check failed: {install.stdout + install.stderr}"]

        start_result = subprocess.run(
            ["podman", "exec", container_name, "service", "asterisk", "start"],
            capture_output=True, text=True,
        )
        if start_result.returncode != 0:
            return [f"service asterisk start failed: {start_result.stdout + start_result.stderr}"]

        # `service asterisk start` returns before the control socket exists;
        # probing immediately hits "Unable to connect to remote asterisk".
        # `core waitfullybooted` blocks until Asterisk is up, and the retry
        # covers the moments before the socket itself appears.
        boot_wait = subprocess.run(
            ["podman", "exec", container_name, "sh", "-c",
             'for i in $(seq 1 60); do asterisk -rx "core waitfullybooted" 2>/dev/null && exit 0; sleep 1; done; exit 1'],
            capture_output=True, text=True,
        )
        if boot_wait.returncode != 0:
            return [f"asterisk did not become fully booted: {boot_wait.stdout + boot_wait.stderr}"]

        module_show = subprocess.run(
            ["podman", "exec", container_name, "asterisk", "-rx", "module show"],
            capture_output=True, text=True,
        )
        # A running Asterisk instance always lists at least the modules it
        # loaded; a nonzero exit or empty stdout means Asterisk exited
        # before or during the probe rather than reporting no failures.
        if module_show.returncode != 0 or not module_show.stdout.strip():
            errors = [
                f"module show probe failed: returncode {module_show.returncode}: "
                f"{module_show.stdout + module_show.stderr}"
            ]
        else:
            errors = list(parse_module_show(module_show.stdout))

        if published is not None:
            # A fresh container, not the lifecycle one: the upgrade check
            # starts from the previous version, and the candidate already
            # installed above could only be downgraded.
            upgrade_name = f"zamfono-upgrade-{_safe_name(candidate_version)}"
            subprocess.run(["podman", "rm", "-f", upgrade_name], capture_output=True)
            upgrade_result = subprocess.run(
                [
                    "podman", "run", "--rm", "--name", upgrade_name,
                    "-v", f"{workspace}:/out",
                    "-w", "/out", "docker.io/library/debian:13", "sh", "-c",
                    upgrade_check_script(published, candidate_version),
                ],
                capture_output=True, text=True,
            )
            if upgrade_result.returncode != 0:
                errors.append(f"upgrade check failed: {upgrade_result.stdout + upgrade_result.stderr}")

        purge_result = subprocess.run(
            ["podman", "exec", container_name, "sh", "-c", purge_check_script()],
            capture_output=True, text=True,
        )
        if purge_result.returncode != 0:
            errors.append(f"purge check failed: {purge_result.stdout + purge_result.stderr}")

        return errors
    finally:
        subprocess.run(["podman", "rm", "-f", container_name])


def _changes_referenced_files(changes_path: Path) -> "list[str]":
    """Filenames from the .changes Files: stanza (5th field per line)."""
    names = []
    in_files = False
    for line in changes_path.read_text().splitlines():
        if in_files and line.startswith(" "):
            names.append(line.split()[4])
        else:
            in_files = line.startswith("Files:")
    return names


def _sign_and_upload(workspace: Path, remote_dir: str) -> None:
    keyid = Path(UPLOAD_KEYID_FILE).read_text().strip()
    # A primary build leaves a companion *_source.changes next to the full
    # *_amd64.changes; the full one already carries source and binaries,
    # and it is the only one uploaded.
    changes_paths = [
        p for p in workspace.glob("*.changes")
        if not p.name.endswith("_source.changes")
    ]
    if len(changes_paths) != 1:
        raise LookupError(
            f"expected exactly one .changes in {workspace}, found {len(changes_paths)}"
        )
    changes = changes_paths[0]
    subprocess.run(["debsign", "--no-re-sign", "-k", keyid, str(changes)], check=True)
    # Plain OpenSSH sftp in batch mode (-b aborts on the first error): the
    # upload account's chroot forces internal-sftp and holds one incoming
    # directory per archive; remote_dir names this product's. The .changes
    # itself is transferred last — ingestion on the packages host triggers
    # on *.changes, and everything it references must already be present.
    batch = f"cd {remote_dir}\n" + "".join(
        f"put {workspace / name}\n" for name in _changes_referenced_files(changes)
    ) + f"put {changes}\n"
    subprocess.run(
        ["sftp", "-b", "-", UPLOAD_TARGET],
        input=batch, text=True, check=True,
    )


def _run_asterisk(arch: str, rebuild: bool = False) -> int:
    primary = arch == PRIMARY_ARCH
    next_build = _next_build(arch, rebuild)
    if next_build is None:
        return 0
    zamfono_version, latest_published = next_build
    workspace = Path(WORKSPACE_BASE) / _safe_name(zamfono_version)
    if _retry_pending(workspace):
        print(f"last attempt for {zamfono_version} failed; kept in {workspace}, retried 6h after it")
        return 0

    if primary:
        _fetch_and_patch(workspace, strip_zamfono_suffix(zamfono_version), zamfono_version)
    else:
        _fetch_published_source(workspace, zamfono_version, ARCHIVE_SOURCELIST_CONTENT, PACKAGE)

    build_result = run_build(str(workspace), FULL_BUILD_OPTS if primary else ARCH_BUILD_OPTS)
    # _fetch_and_patch creates the workspace; a run that never got that
    # far still reports the log on stderr below.
    if workspace.is_dir():
        (workspace / "build.log").write_text(build_result.log)
    if not build_result.success:
        print(build_result.log, file=sys.stderr)
        return 1

    _index_candidate_repo(workspace)
    cheap_failures = _run_gates_for_candidate(workspace, zamfono_version, build_result.log, primary)
    if cheap_failures:
        for name, errors in cheap_failures:
            print(f"gate ({name}) failed: {errors}", file=sys.stderr)
        return 1

    # A follower skips the upgrade gate: once the primary has published
    # a new version, the archive no longer holds the Architecture: all
    # packages (asterisk-config) the follower's previous version depends
    # on at exact version, so that version cannot be installed to
    # upgrade from. The transition itself is packaging, not code, and
    # identical on every architecture; the primary gated it.
    lifecycle_errors = _run_module_load_and_upgrade_gates(
        workspace, zamfono_version, latest_published if primary else None, primary
    )
    if lifecycle_errors:
        print("lifecycle gates failed:\n" + "\n".join(lifecycle_errors), file=sys.stderr)
        return 1

    _sign_and_upload(workspace, UPLOAD_DIR)
    # A run ends only once the archive serves its upload: sftp succeeds
    # even when the packages host later refuses the .changes, and the
    # next poll (queued by the timer while this run was busy)
    # can start the moment this one exits — reading the index before
    # ingest finished, it would build the same version again. A refused
    # upload keeps its workspace, like any failed attempt.
    if not _wait_until_published(arch, zamfono_version, INDEX_BASE_URL, PACKAGE):
        print(f"upload of {zamfono_version} not published after {INGEST_TIMEOUT_SECONDS}s", file=sys.stderr)
        return 1
    shutil.rmtree(workspace, ignore_errors=True)  # successful build trees are removed
    return 0


def _next_baresip_build(arch: str) -> "tuple[str, str | None, str | None] | None":
    """(version to build, latest published for this architecture, upstream
    version to fetch: None on a follower) or None when there is nothing to
    build. The primary follows the latest upstream release, a follower the
    archive's published source, as for asterisk."""
    if arch == PRIMARY_ARCH:
        upstream = baresip.latest_upstream_version()
        published = published_source_versions(baresip.INDEX_BASE_URL, baresip.PACKAGE)
        latest_published = published[0] if published else None
        if not should_build(baresip.debian_version(upstream), latest_published):
            print(f"baresip up to date: upstream={upstream} published={latest_published}")
            return None
        print(f"newer baresip release available: {upstream} (published: {latest_published})")
        version = compute_next_zamfono_version(baresip.debian_version(upstream), published)
        return version, latest_published, upstream

    sources = published_source_versions(baresip.INDEX_BASE_URL, baresip.PACKAGE)
    binaries = published_binary_versions(arch, baresip.INDEX_BASE_URL, baresip.PACKAGE)
    source_version = sources[0] if sources else None
    latest_published = binaries[0] if binaries else None
    if source_version is None or (
        latest_published is not None
        and not dpkg_compare_versions(source_version, "gt", latest_published)
    ):
        print(f"baresip up to date: source={source_version} {arch}={latest_published}")
        return None
    print(f"published baresip source not yet built for {arch}: {source_version} ({arch}: {latest_published})")
    return source_version, latest_published, None


def _run_baresip_gates(
    workspace: Path, version: str, build_log: str, published: "str | None"
) -> "list[str]":
    """baresip's gates: dh_missing, unresolved shared libraries, lintian, a
    clean install that loads every shipped module in a running baresip, the
    upgrade from the published version (primary only, `published` is None
    on a follower) and the purge. The package has no Architecture: all
    companions, so a follower's candidate installs on its own."""
    errors = (
        check_dh_missing(build_log)
        + check_unresolved_shlibs(build_log)
        + _run_lintian(workspace)
    )
    name = f"zamfono-baresip-{_safe_name(version)}"
    subprocess.run(["podman", "rm", "-f", name], capture_output=True)
    smoke = subprocess.run(
        [
            "podman", "run", "--rm", "--name", name,
            "-v", f"{workspace}:/out", "-w", "/out",
            "docker.io/library/debian:13", "sh", "-c",
            baresip.gate_script(CANDIDATE_REPO_SETUP),
        ],
        capture_output=True, text=True,
    )
    output = smoke.stdout + smoke.stderr
    if smoke.returncode != 0:
        errors.append(f"install, module load or purge failed: {output}")
    errors += baresip.module_load_failures(output)
    if published is not None and not _container_check(
        f"zamfono-baresip-upgrade-{_safe_name(version)}", workspace,
        baresip.upgrade_check_script(published, version),
    ):
        errors.append(f"upgrade from {published} failed")
    return errors


def _run_baresip(arch: str) -> int:
    primary = arch == PRIMARY_ARCH
    next_build = _next_baresip_build(arch)
    if next_build is None:
        return 0
    version, latest_published, upstream = next_build
    workspace = Path(WORKSPACE_BASE) / f"baresip-{_safe_name(version)}"
    if _retry_pending(workspace):
        print(f"last attempt for baresip {version} failed; kept in {workspace}, retried 6h after it")
        return 0

    if primary:
        baresip.fetch_upstream(workspace, upstream)
        subprocess.run(
            baresip.create_changelog_cmd(version),
            cwd=workspace / "source", check=True,
            env={**os.environ, "DEBEMAIL": CHANGELOG_EMAIL, "DEBFULLNAME": CHANGELOG_NAME},
        )
    else:
        _fetch_published_source(
            workspace, version, baresip.ARCHIVE_SOURCELIST_CONTENT, baresip.PACKAGE
        )

    build_result = run_build(str(workspace), FULL_BUILD_OPTS if primary else ARCH_BUILD_OPTS)
    (workspace / "build.log").write_text(build_result.log)
    if not build_result.success:
        print(build_result.log, file=sys.stderr)
        return 1

    _index_candidate_repo(workspace)
    errors = _run_baresip_gates(
        workspace, version, build_result.log, latest_published if primary else None
    )
    if errors:
        print("baresip gates failed:\n" + "\n".join(errors), file=sys.stderr)
        return 1

    _sign_and_upload(workspace, baresip.UPLOAD_DIR)
    if not _wait_until_published(arch, version, baresip.INDEX_BASE_URL, baresip.PACKAGE):
        print(f"upload of baresip {version} not published after {INGEST_TIMEOUT_SECONDS}s", file=sys.stderr)
        return 1
    shutil.rmtree(workspace, ignore_errors=True)
    return 0


def _cmd_run(args) -> int:
    lock = acquire_lock(LOCK_PATH)
    if lock is None:
        print("another run holds the lock; exiting without changes")
        return 0
    try:
        arch = _native_arch()
        # One product's failure (a red gate, an unreachable upstream) never
        # keeps the other from building; the run fails if either did.
        status = 0
        for run in (lambda: _run_asterisk(arch, args.rebuild), lambda: _run_baresip(arch)):
            try:
                status = max(status, run())
            except Exception:
                traceback.print_exc()
                status = 1
        return status
    finally:
        lock.close()


def main(argv: "list[str]") -> int:
    parser = argparse.ArgumentParser(prog="orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("check").set_defaults(func=_cmd_check)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument(
        "--rebuild", action="store_true",
        help="on the primary, rebuild sid's asterisk as the next +zamfono13.N even if already published",
    )
    run_parser.set_defaults(func=_cmd_run)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
