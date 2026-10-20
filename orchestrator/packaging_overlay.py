"""Applies Zamfono's own packaging changes to a dpkg-source -x extracted
Debian source tree, and stamps the computed Zamfono version into
debian/changelog before the build ever starts — dpkg-buildpackage reads the
package version solely from the top changelog entry, so skipping this step
leaves every built artifact carrying the raw sid version instead of
+zamfono13.N. This module only edits the source tree and builds the dch
command line; orchestrator.cli._fetch_and_patch runs both, in this order, on
the host before the source tree is bind-mounted into the container."""
import hashlib
import shutil
import subprocess
from pathlib import Path

# Debian patches left out of the build: name -> (SHA-256 of the exact file
# it was reviewed as, the reason recorded in debian/changelog). A changed or
# vanished patch stops the build for a human to decide again, instead of the
# deviation drifting silently.
DROPPED_DEBIAN_PATCHES = {
    "2016_opus_plc.patch": (
        "63d984e124dcb6aa3265b4cd2b2d48266f50c036208c8f049319a483f4118b9f",
        "it silently drops every frame on a transcoded call leg after the far"
        " end changes SSRC to a slightly lower RTP sequence number, often for"
        " the rest of the call (asterisk/asterisk#1324). main/translate.c is"
        " upstream's again; Opus only loses its native concealment of lost"
        " packets.",
    ),
}


def drop_debian_patches(
    source_dir: "str | Path", dropped: "dict[str, tuple[str, str]]" = DROPPED_DEBIAN_PATCHES,
) -> None:
    """Takes each dropped patch out of a dpkg-source -x extracted tree: that
    tree already has every series patch applied and recorded in .pc/, so the
    patch is reversed and removed from .pc/ — otherwise dpkg-source -b sees
    the still-patched files as unrecorded upstream changes and aborts. In
    series it is commented out, Debian's own way of disabling a patch, and
    the patch file stays: `apt source` shows exactly what was left out."""
    source_dir = Path(source_dir)
    patches = source_dir / "debian" / "patches"
    for name, (sha256, _reason) in dropped.items():
        patch_file = patches / name
        if not patch_file.is_file():
            raise RuntimeError(f"dropped Debian patch {name} is no longer in the source; review the drop")
        if hashlib.sha256(patch_file.read_bytes()).hexdigest() != sha256:
            raise RuntimeError(f"dropped Debian patch {name} changed in Debian; review it before dropping again")
        subprocess.run(
            [
                "patch", "--reverse", "--strip=1", "--fuzz=0", "--no-backup-if-mismatch",
                "--input", str(patch_file),
            ],
            cwd=source_dir,
            check=True,
        )
        series = patches / "series"
        series.write_text("".join(
            f"#{line}" if line.strip() == name else line
            for line in series.read_text().splitlines(keepends=True)
        ))
        applied = source_dir / ".pc" / "applied-patches"
        if applied.is_file():
            applied.write_text("".join(
                line for line in applied.read_text().splitlines(keepends=True) if line.strip() != name
            ))
        shutil.rmtree(source_dir / ".pc" / name, ignore_errors=True)


def changelog_drop_cmds(
    dropped: "dict[str, tuple[str, str]]" = DROPPED_DEBIAN_PATCHES,
) -> "list[list[str]]":
    """dch lines appended to the Zamfono changelog entry, one per dropped
    patch, so `apt changelog` states every deviation from Debian's source."""
    return [
        ["dch", "--append", f"Don't apply Debian's {name}: {reason}"]
        for name, (_sha256, reason) in dropped.items()
    ]


def install_patches(source_dir: "str | Path", overlay_dir: "str | Path") -> "list[str]":
    """Applies every overlay patch in sorted-filename order, with zero fuzz
    tolerance, so a rejected or ambiguous change aborts before the container
    ever starts. The overlay edits debian/ packaging files, which the quilt
    patch system does not cover: dpkg-source -b takes debian/ verbatim into
    the .debian.tar and then applies debian/patches/series on top of it, so a
    series patch touching debian/ is applied a second time and fails."""
    source_dir = Path(source_dir)
    # patch runs with cwd=source_dir, so a caller-relative overlay path is
    # resolved here, against the caller's cwd, and not inside the source
    # tree.
    overlay_dir = Path(overlay_dir).resolve()
    applied = sorted(p.name for p in overlay_dir.glob("*.patch"))
    for name in applied:
        subprocess.run(
            [
                "patch", "--strip=1", "--fuzz=0", "--no-backup-if-mismatch",
                "--input", str(overlay_dir / name),
            ],
            cwd=source_dir,
            check=True,
        )
    return applied


def set_changelog_version_cmd(new_version: str) -> "list[str]":
    return [
        "dch",
        "--newversion", new_version,
        "--distribution", "trixie",
        "--force-distribution",
        "Zamfono rebuild for Debian 13",
    ]
